"""
Voltage dip and swell monitor for the IPEM PiHat / ATM90E36.

Runs as a background thread. Two detection layers:

  Layer 1 — Hardware interrupt (optional):
    The ATM90E36 WarnOut / IRQ0 pin is wired to a Pi GPIO.
    When voltage drops below SagTh, the pin goes high immediately.
    lgpio fires a callback which records a nanosecond-precision start
    timestamp and arms the software capture loop.

  Layer 2 — Fast RMS polling loop:
    Polls UrmsA/B/C at ~50-100 Hz.
    Maintains a rolling pre-event buffer (configurable seconds).
    On event detection (hardware IRQ or software threshold crossing):
      - Continues polling through the event
      - Saves pre-buffer + event + recovery to the event queue
      - Caller (logger.py) drains the queue and writes to storage

Swell detection is software-only (the SAG comparator only fires on low voltage).
"""

import time
import math
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from queue import Queue
from typing import Optional, List

log = logging.getLogger(__name__)

# How many samples of hysteresis margin before declaring recovery
_RECOVERY_CONFIRM_SAMPLES = 3


@dataclass
class VoltageSample:
    """One voltage reading from the fast poll loop."""
    timestamp_ns: int
    va: float
    vb: float
    vc: float


@dataclass
class VoltageEvent:
    """A captured dip or swell event, including pre-event history."""
    event_type: str           # "dip" or "swell"
    start_ns: int             # Timestamp when threshold was first crossed
    end_ns: int               # Timestamp when voltage recovered
    trigger: str              # "hardware" or "software"
    nominal_v: float          # Configured nominal voltage
    threshold_v: float        # Threshold that was crossed
    pre_samples: List[VoltageSample] = field(default_factory=list)
    event_samples: List[VoltageSample] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000.0

    @property
    def depth_pct(self) -> float:
        """
        For dips: how far below nominal the minimum voltage fell (positive = below).
        For swells: how far above nominal the peak voltage rose (positive = above).
        """
        if not self.event_samples:
            return 0.0
        va_values = [s.va for s in self.event_samples]
        if self.event_type == "dip":
            worst = min(va_values)
            return round((self.nominal_v - worst) / self.nominal_v * 100.0, 2)
        else:
            worst = max(va_values)
            return round((worst - self.nominal_v) / self.nominal_v * 100.0, 2)

    @property
    def min_voltage(self) -> float:
        if not self.event_samples:
            return float("nan")
        return min(s.va for s in self.event_samples)

    @property
    def max_voltage(self) -> float:
        if not self.event_samples:
            return float("nan")
        return max(s.va for s in self.event_samples)

    def summary(self) -> str:
        return (
            f"{self.event_type.upper()} | trigger={self.trigger} | "
            f"duration={self.duration_ms:.1f}ms | depth={self.depth_pct:.1f}% | "
            f"min_V={self.min_voltage:.2f} | max_V={self.max_voltage:.2f}"
        )


class DipMonitor:
    """
    Background voltage event monitor.

    Parameters
    ----------
    meter : ATM90E36
        Initialised ATM90E36 driver instance.
    nominal_v : float
        Nominal mains voltage (e.g. 230.0).
    sag_threshold_v : float
        Voltage below which a dip is declared (e.g. 207.0 for 90% of 230 V).
    sag_hysteresis_v : float
        Voltage must recover to (sag_threshold_v + hysteresis) to end a dip.
    swell_threshold_v : float
        Voltage above which a swell is declared (e.g. 253.0 for 110% of 230 V).
    swell_hysteresis_v : float
        Voltage must drop to (swell_threshold_v - hysteresis) to end a swell.
    poll_hz : float
        Fast polling rate in Hz (50–100 recommended).
    pre_buffer_s : float
        Seconds of history to keep before an event starts.
    gpio_pin : int or None
        BCM GPIO pin connected to WarnOut/IRQ0.  None = software-only detection.
    event_queue : Queue
        Completed VoltageEvent objects are put() here for the logger to drain.
    """

    def __init__(
        self,
        meter,
        nominal_v: float = 230.0,
        sag_threshold_v: float = 207.0,
        sag_hysteresis_v: float = 5.0,
        swell_threshold_v: float = 253.0,
        swell_hysteresis_v: float = 5.0,
        poll_hz: float = 100.0,
        pre_buffer_s: float = 2.0,
        gpio_pin: Optional[int] = None,
        event_queue: Optional[Queue] = None,
    ):
        self._meter = meter
        self._nominal_v = nominal_v
        self._sag_th = sag_threshold_v
        self._sag_hyst = sag_hysteresis_v
        self._swell_th = swell_threshold_v
        self._swell_hyst = swell_hysteresis_v
        self._interval = 1.0 / poll_hz
        self._pre_buffer_size = int(pre_buffer_s / self._interval) + 1
        self._gpio_pin = gpio_pin
        self._event_queue: Queue = event_queue if event_queue is not None else Queue()

        # Rolling pre-event ring buffer
        self._pre_buffer: deque = deque(maxlen=self._pre_buffer_size)

        # State machine
        self._state = "normal"          # "normal" | "dip" | "swell"
        self._event_samples: List[VoltageSample] = []
        self._event_start_ns: int = 0
        self._event_trigger: str = "software"
        self._hw_triggered: bool = False
        self._hw_trigger_ns: int = 0
        self._recovery_counter: int = 0

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._gpio_handle = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start(self):
        """Start the background polling thread and GPIO interrupt (if configured)."""
        if self._running:
            return
        self._running = True

        if self._gpio_pin is not None:
            self._setup_gpio()

        self._thread = threading.Thread(
            target=self._poll_loop,
            name="DipMonitorPoll",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "DipMonitor started | sag=%.0fV swell=%.0fV poll=%.0fHz gpio=%s",
            self._sag_th, self._swell_th, 1.0 / self._interval,
            self._gpio_pin if self._gpio_pin is not None else "disabled",
        )

    def stop(self):
        """Stop the monitor gracefully."""
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._teardown_gpio()
        log.info("DipMonitor stopped")

    @property
    def event_queue(self) -> Queue:
        return self._event_queue

    # ------------------------------------------------------------------
    # GPIO interrupt (hardware layer 1)
    # ------------------------------------------------------------------

    def _setup_gpio(self):
        try:
            import lgpio
            self._lgpio = lgpio
            self._gpio_handle = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(self._gpio_handle, self._gpio_pin)
            lgpio.gpio_claim_alert(
                self._gpio_handle,
                self._gpio_pin,
                lgpio.RISING_EDGE,
            )
            lgpio.callback(
                self._gpio_handle,
                self._gpio_pin,
                lgpio.RISING_EDGE,
                self._on_hw_sag,
            )
            log.info("GPIO interrupt armed on BCM pin %d (WarnOut/IRQ0)", self._gpio_pin)
        except Exception as exc:
            log.warning(
                "Could not set up GPIO interrupt on pin %d: %s — falling back to software-only detection",
                self._gpio_pin, exc,
            )
            self._gpio_handle = None

    def _teardown_gpio(self):
        if self._gpio_handle is not None:
            try:
                self._lgpio.gpiochip_close(self._gpio_handle)
            except Exception:
                pass
            self._gpio_handle = None

    def _on_hw_sag(self, chip, gpio, level, tick):
        """lgpio callback — fires when WarnOut/IRQ0 pin goes high."""
        self._hw_trigger_ns = time.perf_counter_ns()
        self._hw_triggered = True
        log.debug("Hardware SAG interrupt at %d ns", self._hw_trigger_ns)

    # ------------------------------------------------------------------
    # Main polling loop (layer 2)
    # ------------------------------------------------------------------

    def _poll_loop(self):
        next_sample_time = time.perf_counter()

        while self._running:
            # Pace the loop precisely
            now = time.perf_counter()
            sleep_for = next_sample_time - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            next_sample_time += self._interval

            ts_ns = time.perf_counter_ns()
            try:
                va, vb, vc = self._meter.get_voltages()
            except Exception as exc:
                log.warning("SPI read error in fast poll: %s", exc)
                continue

            sample = VoltageSample(timestamp_ns=ts_ns, va=va, vb=vb, vc=vc)
            self._process_sample(sample)

    def _process_sample(self, sample: VoltageSample):
        va = sample.va

        if self._state == "normal":
            self._pre_buffer.append(sample)

            # Check hardware trigger first (gives most precise start time)
            if self._hw_triggered:
                self._hw_triggered = False
                self._start_event("dip", sample, trigger="hardware", ts_ns=self._hw_trigger_ns)
                return

            # Software detection: dip
            if va < self._sag_th and va > 0.1:  # va > 0.1 avoids false trigger on 0V startup
                self._start_event("dip", sample, trigger="software")
                return

            # Software detection: swell
            if va > self._swell_th:
                self._start_event("swell", sample, trigger="software")
                return

        elif self._state == "dip":
            self._event_samples.append(sample)

            # Recovery check: voltage must be above (threshold + hysteresis) for N consecutive samples
            recovery_v = self._sag_th + self._sag_hyst
            if va >= recovery_v:
                self._recovery_counter += 1
                if self._recovery_counter >= _RECOVERY_CONFIRM_SAMPLES:
                    self._end_event(sample)
            else:
                self._recovery_counter = 0

        elif self._state == "swell":
            self._event_samples.append(sample)

            recovery_v = self._swell_th - self._swell_hyst
            if va <= recovery_v:
                self._recovery_counter += 1
                if self._recovery_counter >= _RECOVERY_CONFIRM_SAMPLES:
                    self._end_event(sample)
            else:
                self._recovery_counter = 0

    def _start_event(self, event_type: str, sample: VoltageSample, trigger: str, ts_ns: int = None):
        self._state = event_type
        self._event_start_ns = ts_ns if ts_ns is not None else sample.timestamp_ns
        self._event_trigger = trigger
        self._event_samples = [sample]
        self._recovery_counter = 0
        threshold = self._sag_th if event_type == "dip" else self._swell_th
        log.info(
            "%s STARTED | trigger=%s | Va=%.2fV | threshold=%.0fV",
            event_type.upper(), trigger, sample.va, threshold,
        )

    def _end_event(self, sample: VoltageSample):
        threshold = self._sag_th if self._state == "dip" else self._swell_th
        event = VoltageEvent(
            event_type=self._state,
            start_ns=self._event_start_ns,
            end_ns=sample.timestamp_ns,
            trigger=self._event_trigger,
            nominal_v=self._nominal_v,
            threshold_v=threshold,
            pre_samples=list(self._pre_buffer),
            event_samples=list(self._event_samples),
        )
        log.info("%s", event.summary())
        self._event_queue.put(event)

        # Reset state
        self._state = "normal"
        self._event_samples = []
        self._recovery_counter = 0
        self._pre_buffer.clear()
