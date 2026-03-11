#!/usr/bin/env python3
"""
IPEM PiHat CLI — ATM90E36 Power Energy Monitor

Usage:
    ipem.py --smoke-test          Quick SPI comms check with live readings
    ipem.py --snapshot            One-shot full register dump
    ipem.py --log                 Continuous logging to SQLite/CSV/InfluxDB
    ipem.py --dip-test            Simulate voltage events through state machine (no hardware)

Options:
    --config CONFIG               Path to config file (default: config.yaml)
    --log-level LEVEL             DEBUG, INFO, WARNING, or ERROR (default: INFO)
"""

import argparse
import logging
import os
import signal
import sys
import time

log = logging.getLogger("ipem")


def _load_config(path):
    import yaml
    if not os.path.exists(path):
        alt = os.path.join(os.path.dirname(path), "config.example.yaml")
        if os.path.exists(alt):
            log.warning("config.yaml not found; falling back to %s", alt)
            path = alt
        else:
            log.error("Config file not found: %s", path)
            sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def _build_meter(cfg):
    from atm90e36 import ATM90E36
    spi = cfg.get("spi", {})
    i2c = cfg.get("i2c", {})
    cal = cfg.get("calibration", {})

    i2c_bus = i2c.get("bus", 1) if i2c.get("enabled", False) else None

    meter = ATM90E36(
        spi_bus=spi.get("bus", 0),
        spi_device=spi.get("device", 0),
        speed_hz=spi.get("speed_hz", 200_000),
        mode=spi.get("mode", 0),
        i2c_bus=i2c_bus,
        pca9671_addr=i2c.get("pca9671_address", 0x20),
        board_cs_bit=i2c.get("board_cs_bit", 9),
        pca_idle_state=i2c.get("pca_idle_state", 0x03FF),
    )

    meter.init_meter(
        line_freq_reg=cal.get("mmode0", 0x0187),
        pga_gain=cal.get("pga_gain", 0x0000),
        ugain=cal.get("ugain", 20200),
        igain_a=cal.get("igain_a", 33500),
        igain_b=cal.get("igain_b", 33500),
        igain_c=cal.get("igain_c", 33500),
        igain_n=cal.get("igain_n", 0xFD7F),
        ct_polarity_a=cal.get("ct_polarity_a", 1),
        ct_polarity_b=cal.get("ct_polarity_b", 1),
        ct_polarity_c=cal.get("ct_polarity_c", 1),
    )

    events = cfg.get("events", {})
    meter.configure_sag(threshold_v=events.get("sag_threshold_volts", 207))

    return meter


# -----------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------

def cmd_smoke_test(cfg):
    """Quick SPI comms check — 10 readings at 0.5 s intervals."""
    meter = _build_meter(cfg)

    print("\n--- ATM90E36 Smoke Test ---")
    print(f"SysStatus0 : 0x{meter.get_sys_status0():04X}")
    print(f"CalibError : {meter.calibration_error()}")
    print()

    for i in range(10):
        va, vb, vc = meter.get_voltages()
        freq = meter.get_frequency()
        ia = meter.get_current_a()
        pa = meter.get_active_power_a()
        pf_a = meter.get_pf_a()
        temp = meter.get_temperature()
        print(
            f"[{i+1:2d}] Va={va:7.2f}V  Vb={vb:7.2f}V  Vc={vc:7.2f}V  "
            f"Ia={ia:6.3f}A  Pa={pa:8.2f}W  PF={pf_a:+.3f}  "
            f"Freq={freq:.2f}Hz  Temp={temp:.0f}°C"
        )
        time.sleep(0.5)

    print("\n--- Full register snapshot ---")
    snapshot = meter.read_all()
    for key, val in snapshot.items():
        if isinstance(val, float):
            print(f"  {key:<22}: {val:.4f}")
        else:
            print(f"  {key:<22}: {val}")

    meter.close()


def cmd_snapshot(cfg):
    """One-shot full register dump to stdout (and sinks if configured)."""
    from logger import IPEMLogger
    ipem = IPEMLogger(cfg)
    ipem.snapshot()
    ipem._meter.close()


def cmd_log(cfg):
    """Continuous logging — runs until Ctrl-C or SIGTERM."""
    from logger import IPEMLogger
    ipem = IPEMLogger(cfg)

    def _shutdown(sig, frame):
        log.info("Signal %d — shutting down", sig)
        ipem.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ipem.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        ipem.stop()


def cmd_dip_test(_cfg):
    """Simulate voltage dips/swells through the state machine (no hardware)."""
    from queue import Queue
    from dip_monitor import DipMonitor, VoltageSample

    class FakeMeter:
        def __init__(self, voltages):
            self._voltages = iter(voltages)
        def get_voltages(self):
            v = next(self._voltages, 230.0)
            return (v, v, v)

    script = (
        [230.0] * 10
        + [195.0] * 5
        + [230.0] * 15
        + [260.0] * 5
        + [230.0] * 15
    )

    q = Queue()
    monitor = DipMonitor(
        meter=FakeMeter(script),
        nominal_v=230.0,
        sag_threshold_v=207.0,
        sag_hysteresis_v=5.0,
        swell_threshold_v=253.0,
        swell_hysteresis_v=5.0,
        poll_hz=100.0,
        pre_buffer_s=0.5,
        gpio_pin=None,
        event_queue=q,
    )

    print("DipMonitor self-test: feeding simulated samples...\n")
    ts = 1_000_000_000
    for v in script:
        s = VoltageSample(timestamp_ns=ts, va=v, vb=v, vc=v)
        monitor._process_sample(s)
        ts += 10_000_000

    if monitor._state != "normal" and monitor._event_samples:
        monitor._end_event(monitor._event_samples[-1])

    print(f"Captured {q.qsize()} event(s):")
    while not q.empty():
        evt = q.get()
        print(f"  {evt.summary()}")
        print(f"    pre_samples={len(evt.pre_samples)}  event_samples={len(evt.event_samples)}")


# -----------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------

COMMANDS = {
    "smoke_test": cmd_smoke_test,
    "snapshot":   cmd_snapshot,
    "log":        cmd_log,
    "dip_test":   cmd_dip_test,
}


def main():
    parser = argparse.ArgumentParser(
        prog="ipem.py",
        description="IPEM PiHat — ATM90E36 Power Energy Monitor",
    )

    actions = parser.add_argument_group("commands (pick one)")
    mx = actions.add_mutually_exclusive_group(required=True)
    mx.add_argument("--smoke-test", action="store_true",
                     help="Quick SPI comms check with live readings")
    mx.add_argument("--snapshot", action="store_true",
                     help="One-shot full register dump")
    mx.add_argument("--log", action="store_true",
                     help="Continuous logging to SQLite/CSV/InfluxDB")
    mx.add_argument("--dip-test", action="store_true",
                     help="Simulate voltage events (no hardware needed)")

    parser.add_argument("--config", default="config.yaml",
                        help="Path to config YAML (default: config.yaml)")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity (default: INFO)")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("ipem.log"),
        ],
    )

    cfg = _load_config(args.config)

    if args.smoke_test:
        cmd_smoke_test(cfg)
    elif args.snapshot:
        cmd_snapshot(cfg)
    elif args.log:
        cmd_log(cfg)
    elif args.dip_test:
        cmd_dip_test(cfg)


if __name__ == "__main__":
    main()
