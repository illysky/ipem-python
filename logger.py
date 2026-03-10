import csv, datetime, logging, math, os, queue, sqlite3, statistics, threading, time
from typing import Optional
from atm90e36 import ATM90E36
from dip_monitor import DipMonitor, VoltageEvent

log = logging.getLogger(__name__)

_CREATE_MEASUREMENTS = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, ts_unix REAL NOT NULL,
    va REAL, vb REAL, vc REAL,
    ia REAL, ib REAL, ic REAL, in_sampled REAL, in_calculated REAL,
    pa REAL, pb REAL, pc REAL, p_total REAL,
    qa REAL, qb REAL, qc REAL, q_total REAL,
    sa REAL, sb REAL, sc REAL, s_total REAL,
    pf_a REAL, pf_b REAL, pf_c REAL, pf_total REAL,
    frequency REAL,
    thd_va REAL, thd_vb REAL, thd_vc REAL,
    thd_ia REAL, thd_ib REAL, thd_ic REAL,
    phase_angle_a REAL, phase_angle_b REAL, phase_angle_c REAL,
    voltage_angle_a REAL, voltage_angle_b REAL, voltage_angle_c REAL,
    temperature REAL,
    pf_a_fund REAL, pf_b_fund REAL, pf_c_fund REAL, p_total_fund REAL,
    pa_harm REAL, pb_harm REAL, pc_harm REAL, p_total_harm REAL,
    import_kwh REAL, export_kwh REAL, reactive_varh REAL, apparent_vah REAL,
    sys_status0 INTEGER, sys_status1 INTEGER, meter_status0 INTEGER, meter_status1 INTEGER
);
CREATE INDEX IF NOT EXISTS idx_meas_ts ON measurements(ts_unix);
"""

_CREATE_VOLTAGE_EVENTS = """
CREATE TABLE IF NOT EXISTS voltage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL, start_ts TEXT NOT NULL, end_ts TEXT NOT NULL,
    start_unix REAL NOT NULL, end_unix REAL NOT NULL,
    duration_ms REAL, depth_pct REAL, min_voltage REAL, max_voltage REAL,
    nominal_v REAL, threshold_v REAL, trigger TEXT,
    pre_sample_json TEXT, event_sample_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_evt_ts ON voltage_events(start_unix);
"""

_MEAS_FIELDS = [
    "va","vb","vc","ia","ib","ic","in_sampled","in_calculated",
    "pa","pb","pc","p_total","qa","qb","qc","q_total",
    "sa","sb","sc","s_total","pf_a","pf_b","pf_c","pf_total",
    "frequency","thd_va","thd_vb","thd_vc","thd_ia","thd_ib","thd_ic",
    "phase_angle_a","phase_angle_b","phase_angle_c",
    "voltage_angle_a","voltage_angle_b","voltage_angle_c",
    "temperature","pf_a_fund","pf_b_fund","pf_c_fund","p_total_fund",
    "pa_harm","pb_harm","pc_harm","p_total_harm",
    "import_kwh","export_kwh","reactive_varh","apparent_vah",
    "sys_status0","sys_status1","meter_status0","meter_status1",
]


class SQLiteSink:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_CREATE_MEASUREMENTS)
        self._conn.executescript(_CREATE_VOLTAGE_EVENTS)
        self._conn.commit()
        self._lock = threading.Lock()
        log.info("SQLite: %s", db_path)

    def write_measurement(self, ts, data):
        row = {"timestamp": ts.isoformat(), "ts_unix": ts.timestamp()}
        row.update({k: data.get(k) for k in _MEAS_FIELDS})
        cols = ", ".join(row.keys())
        ph = ", ".join(["?"] * len(row))
        with self._lock:
            self._conn.execute(f"INSERT INTO measurements ({cols}) VALUES ({ph})", list(row.values()))
            self._conn.commit()

    def write_event(self, event):
        import json
        s_dt = datetime.datetime.fromtimestamp(event.start_ns / 1e9, tz=datetime.timezone.utc)
        e_dt = datetime.datetime.fromtimestamp(event.end_ns   / 1e9, tz=datetime.timezone.utc)
        pre  = json.dumps([{"ts": s.timestamp_ns, "va": s.va, "vb": s.vb, "vc": s.vc} for s in event.pre_samples])
        evt  = json.dumps([{"ts": s.timestamp_ns, "va": s.va, "vb": s.vb, "vc": s.vc} for s in event.event_samples])
        with self._lock:
            self._conn.execute(
                """INSERT INTO voltage_events
                   (event_type,start_ts,end_ts,start_unix,end_unix,
                    duration_ms,depth_pct,min_voltage,max_voltage,
                    nominal_v,threshold_v,trigger,pre_sample_json,event_sample_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event.event_type, s_dt.isoformat(), e_dt.isoformat(),
                 event.start_ns/1e9, event.end_ns/1e9,
                 event.duration_ms, event.depth_pct,
                 event.min_voltage, event.max_voltage,
                 event.nominal_v, event.threshold_v, event.trigger, pre, evt))
            self._conn.commit()

    def purge_old(self, days):
        if days <= 0:
            return
        cut = time.time() - days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM measurements WHERE ts_unix < ?", (cut,))
            self._conn.execute("DELETE FROM voltage_events WHERE start_unix < ?", (cut,))
            self._conn.commit()

    def close(self):
        self._conn.close()


class CSVSink:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self._meas_path = os.path.join(data_dir, "measurements.csv")
        self._evt_path  = os.path.join(data_dir, "voltage_events.csv")
        self._mf = ["timestamp", "ts_unix"] + _MEAS_FIELDS
        self._ef = ["event_type","start_ts","end_ts","start_unix","end_unix",
                    "duration_ms","depth_pct","min_voltage","max_voltage",
                    "nominal_v","threshold_v","trigger"]
        for p, f in [(self._meas_path, self._mf), (self._evt_path, self._ef)]:
            if not os.path.exists(p):
                with open(p, "w", newline="") as fh:
                    csv.writer(fh).writerow(f)
        log.info("CSV: %s, %s", self._meas_path, self._evt_path)

    def write_measurement(self, ts, data):
        row = {"timestamp": ts.isoformat(), "ts_unix": ts.timestamp()}
        row.update({k: data.get(k, "") for k in _MEAS_FIELDS})
        with open(self._meas_path, "a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self._mf).writerow(row)

    def write_event(self, event):
        s_dt = datetime.datetime.fromtimestamp(event.start_ns/1e9, tz=datetime.timezone.utc)
        e_dt = datetime.datetime.fromtimestamp(event.end_ns/1e9,   tz=datetime.timezone.utc)
        row = {"event_type": event.event_type, "start_ts": s_dt.isoformat(), "end_ts": e_dt.isoformat(),
               "start_unix": event.start_ns/1e9, "end_unix": event.end_ns/1e9,
               "duration_ms": event.duration_ms, "depth_pct": event.depth_pct,
               "min_voltage": event.min_voltage, "max_voltage": event.max_voltage,
               "nominal_v": event.nominal_v, "threshold_v": event.threshold_v,
               "trigger": event.trigger}
        with open(self._evt_path, "a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=self._ef).writerow(row)


class InfluxSink:
    def __init__(self, url, token, org, bucket, batch_size=50):
        self._enabled = bool(url)
        if not self._enabled:
            log.info("InfluxDB: disabled (no URL)")
            return
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import WriteOptions
        self._bucket, self._org = bucket, org
        self._client = InfluxDBClient(url=url, token=token, org=org)
        self._write_api = self._client.write_api(
            write_options=WriteOptions(batch_size=batch_size, flush_interval=10_000))
        log.info("InfluxDB: %s / %s", url, bucket)

    def write_measurement(self, ts, data):
        if not self._enabled:
            return
        from influxdb_client.client.write.point import Point
        p = Point("power_mains").time(ts)
        for k, v in data.items():
            if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                p = p.field(k, v)
        self._write_api.write(bucket=self._bucket, org=self._org, record=p)

    def write_batch(self, samples):
        """Write a list of (datetime, data-dict) samples as individual InfluxDB points."""
        if not self._enabled or not samples:
            return
        from influxdb_client.client.write.point import Point
        points = []
        for ts, data in samples:
            p = Point("power_mains").time(ts)
            for k, v in data.items():
                if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                    p = p.field(k, v)
            points.append(p)
        self._write_api.write(bucket=self._bucket, org=self._org, record=points)

    def write_event(self, event):
        if not self._enabled:
            return
        from influxdb_client.client.write.point import Point
        s_dt = datetime.datetime.fromtimestamp(event.start_ns/1e9, tz=datetime.timezone.utc)
        p = (Point("voltage_event").time(s_dt)
             .tag("event_type", event.event_type).tag("trigger", event.trigger)
             .field("duration_ms", event.duration_ms).field("depth_pct", event.depth_pct)
             .field("min_voltage", event.min_voltage).field("max_voltage", event.max_voltage)
             .field("nominal_v", event.nominal_v).field("threshold_v", event.threshold_v))
        self._write_api.write(bucket=self._bucket, org=self._org, record=p)

    def close(self):
        if self._enabled:
            self._write_api.close()
            self._client.close()


class IPEMLogger:
    def __init__(self, config):
        self._cfg = config
        self._running = False
        spi = config.get("spi", {})
        cal = config.get("calibration", {})
        mains = config.get("mains", {})
        events = config.get("events", {})
        gpio = config.get("gpio", {})
        poll = config.get("polling", {})
        lc = config.get("logging", {})
        influx = config.get("influxdb", {})
        i2c = config.get("i2c", {})

        i2c_bus = i2c.get("bus") if i2c.get("enabled") else None
        self._meter = ATM90E36(
            spi_bus=spi.get("bus",0), spi_device=spi.get("device",0),
            speed_hz=spi.get("speed_hz",200_000), mode=spi.get("mode",0),
            i2c_bus=i2c_bus, pca9671_addr=i2c.get("pca9671_address",0x20),
            board_cs_bit=i2c.get("board_cs_bit",9),
            pca_idle_state=i2c.get("pca_idle_state",0x03FF))
        self._meter.init_meter(
            line_freq_reg=cal.get("mmode0",0x0187), pga_gain=cal.get("pga_gain",0),
            ugain=cal.get("ugain",20200), igain_a=cal.get("igain_a",33500),
            igain_b=cal.get("igain_b",33500), igain_c=cal.get("igain_c",33500),
            igain_n=cal.get("igain_n",0xFD7F))
        self._meter.configure_sag(threshold_v=events.get("sag_threshold_volts",207.0))

        data_dir = lc.get("data_dir","data")
        db_path  = os.path.join(data_dir, lc.get("sqlite_file","ipem.db"))
        self._sqlite = SQLiteSink(db_path)
        self._csv    = CSVSink(data_dir) if lc.get("csv_enabled",True) else None
        self._influx = InfluxSink(influx.get("url",""), influx.get("token",""),
                                   influx.get("org",""), influx.get("bucket","ipem"),
                                   influx.get("batch_size",50))
        self._retention = lc.get("retention_days",90)
        self._event_q = queue.Queue()
        gpio_pin = gpio.get("sag_interrupt_pin") if gpio.get("enabled",True) else None
        self._dip = DipMonitor(
            meter=self._meter, nominal_v=float(mains.get("nominal_voltage",230.0)),
            sag_threshold_v=events.get("sag_threshold_volts",207.0),
            sag_hysteresis_v=events.get("sag_hysteresis_volts",5.0),
            swell_threshold_v=events.get("swell_threshold_volts",253.0),
            swell_hysteresis_v=events.get("swell_hysteresis_volts",5.0),
            poll_hz=poll.get("fast_hz",100.0),
            pre_buffer_s=events.get("pre_dip_buffer_seconds",2.0),
            gpio_pin=gpio_pin, event_queue=self._event_q)
        # sample_hz: measurement sampling rate (default 30 Hz)
        # flush_interval_s: how often to write buffered samples (default 10 s)
        # slow_interval_s is accepted as a legacy alias for flush_interval_s
        # slow_read_interval_s: how often to read temperature + energy totals (default 30 s)
        self._sample_hz = float(poll.get("sample_hz", 30.0))
        self._flush_interval_s = float(
            poll.get("flush_interval_s", poll.get("slow_interval_s", 10.0))
        )
        self._slow_read_interval_s = float(poll.get("slow_read_interval_s", 30.0))

    def start(self):
        self._running = True
        self._dip.start()
        threading.Thread(target=self._sample_loop, name="SampleLoop", daemon=True).start()
        threading.Thread(target=self._event_drain, name="EventDrain", daemon=True).start()
        log.info(
            "IPEMLogger running | sample=%.0fHz flush=%.0fs (~%d pts/flush) slow=%.0fs",
            self._sample_hz, self._flush_interval_s,
            round(self._sample_hz * self._flush_interval_s),
            self._slow_read_interval_s,
        )

    def stop(self):
        self._running = False
        self._dip.stop()
        time.sleep(1.0)
        while not self._event_q.empty():
            self._write_event(self._event_q.get_nowait())
        self._sqlite.close()
        self._influx.close()
        log.info("IPEMLogger stopped")

    def snapshot(self):
        data = self._meter.read_all()
        ts = datetime.datetime.now(tz=datetime.timezone.utc)
        print(f"\nSnapshot at {ts.isoformat()}")
        print("-" * 60)
        for k, v in data.items():
            print(f"  {k:<26}: {v:.4f}" if isinstance(v, float) else f"  {k:<26}: {v}")
        return data

    def _sample_loop(self):
        """
        High-rate sampling loop.

        Reads real-time power fields at `_sample_hz` Hz and accumulates them
        in a buffer.  Every `_flush_interval_s` seconds the buffer is flushed:
          - All individual samples → InfluxDB (full time resolution)
          - One aggregated mean row  → SQLite + CSV (compact local storage)

        Additionally, every `_slow_read_interval_s` seconds a separate slow
        read (temperature, cumulative energy, status) is written as a single
        point to InfluxDB and to SQLite/CSV.
        """
        interval = 1.0 / self._sample_hz
        flush_count = max(1, round(self._sample_hz * self._flush_interval_s))
        buffer = []
        nxt = time.perf_counter()
        purge_tick = 0
        last_slow_ts = 0.0

        while self._running:
            sl = nxt - time.perf_counter()
            if sl > 0:
                time.sleep(sl)
            nxt += interval

            try:
                data = self._meter.read_fast()
                ts = datetime.datetime.now(tz=datetime.timezone.utc)
                buffer.append((ts, data))
            except Exception as exc:
                log.error("Sample loop: %s", exc, exc_info=True)
                continue

            if len(buffer) >= flush_count:
                to_flush, buffer = buffer, []
                try:
                    self._flush_samples(to_flush)
                except Exception as exc:
                    log.error("Flush: %s", exc, exc_info=True)
                purge_tick += 1
                if purge_tick >= 360:
                    purge_tick = 0
                    try:
                        self._sqlite.purge_old(self._retention)
                    except Exception as exc:
                        log.warning("Purge: %s", exc)

            # Slow read: temperature, cumulative energy, status registers
            now_wall = time.time()
            if now_wall - last_slow_ts >= self._slow_read_interval_s:
                last_slow_ts = now_wall
                try:
                    slow_data = self._meter.read_slow()
                    slow_ts = datetime.datetime.now(tz=datetime.timezone.utc)
                    self._influx.write_measurement(slow_ts, slow_data)
                    self._sqlite.write_measurement(slow_ts, slow_data)
                    if self._csv:
                        self._csv.write_measurement(slow_ts, slow_data)
                    log.debug(
                        "Slow read | temp=%.1f°C import=%.4f kWh export=%.4f kWh",
                        slow_data.get("temperature", float("nan")),
                        slow_data.get("import_kwh", float("nan")),
                        slow_data.get("export_kwh", float("nan")),
                    )
                except Exception as exc:
                    log.error("Slow read: %s", exc, exc_info=True)

        if buffer:
            try:
                self._flush_samples(buffer)
            except Exception as exc:
                log.error("Final flush: %s", exc)

    def _flush_samples(self, samples):
        """Flush a batch of samples: all to InfluxDB, one mean row to SQLite/CSV."""
        if not samples:
            return
        self._influx.write_batch(samples)
        ts_mid = samples[len(samples) // 2][0]
        data_agg = self._aggregate(samples)
        self._sqlite.write_measurement(ts_mid, data_agg)
        if self._csv:
            self._csv.write_measurement(ts_mid, data_agg)
        log.debug(
            "Flush %d samples → InfluxDB | Va=%.2fV Pa=%.1fW PF=%.3f",
            len(samples),
            data_agg.get("va", float("nan")),
            data_agg.get("p_total", float("nan")),
            data_agg.get("pf_total", float("nan")),
        )

    @staticmethod
    def _aggregate(samples):
        """Return per-field mean across all samples, skipping NaN/None."""
        buckets: dict = {}
        for _, data in samples:
            for k, v in data.items():
                if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                    buckets.setdefault(k, []).append(v)
        return {k: statistics.mean(vals) for k, vals in buckets.items() if vals}

    def _event_drain(self):
        while self._running:
            try:
                evt = self._event_q.get(timeout=1.0)
                self._write_event(evt)
            except queue.Empty:
                pass
            except Exception as exc:
                log.error("Event drain: %s", exc, exc_info=True)

    def _write_event(self, evt):
        log.info("Event: %s", evt.summary())
        self._sqlite.write_event(evt)
        if self._csv: self._csv.write_event(evt)
        self._influx.write_event(evt)


