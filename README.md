# IPEM PiHat — Mains Power Monitor

Python stack for the [DitroniX IPEM PiHat](https://github.com/DitroniX/IPEM-PiHat-IoT-Power-Energy-Monitor) with ATM90E36 energy metering IC on Raspberry Pi.

## Features

- Full ATM90E36 metering: voltage, current, active/reactive/apparent power, power factor, frequency, THD+N, phase angles, temperature, energy (import/export)
- Hardware voltage dip detection via ATM90E36 SAG comparator + GPIO interrupt
- Software swell detection (>110% nominal)
- Fast voltage polling at 50–100 Hz with 2 s pre-event capture buffer
- Slow full metering snapshot at configurable interval (default 10 s)
- Local storage: SQLite + optional CSV
- InfluxDB v2 export (optional — add credentials to config to enable)

---

## Hardware setup

### 1. Fit the PiHat

- Seat the IPEM PiHat on the 40-pin GPIO header
- Connect your voltage reference transformer to V1P/V1N (single phase: bridge V2P=V1P, V3P=V1P on-board via solder pads or DIP switch)
- Clip your SCT-013-000 CT clamp on the phase conductor; connect to CT1 (I1P/I1N)
- For the hardware dip interrupt: **connect the WarnOut pin (or IRQ0) from the PiHat to GPIO17 on the Pi** (BCM numbering). This is configured in `config.yaml` under `gpio.sag_interrupt_pin`.

### 2. Enable SPI and I2C

```bash
sudo raspi-config
# Interface Options → SPI → Enable
# Interface Options → I2C → Enable  (only needed if stacking boards)
sudo reboot
```

Verify SPI device is present after reboot:

```bash
ls /dev/spidev0.*
# Should show: /dev/spidev0.0
```

### 3. Add user to hardware groups

```bash
sudo usermod -aG spi,gpio $USER
# Log out and back in, or reboot
```

---

## Software setup

### Install Python dependencies

Debian/Pi OS Bookworm enforces isolated environments (PEP 668), so use a virtual environment:

```bash
cd /home/illysky/ipem
sudo apt-get install -y python3-pip python3-venv   # one-time
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Activate the venv for interactive use:

```bash
source .venv/bin/activate
python3 atm90e36.py       # smoke test
deactivate                # when done
```

> **Note:** `lgpio` requires Pi OS Bookworm or newer. On older Pi OS (`bullseye`) use `RPi.GPIO` instead and update the import in `dip_monitor.py`.

### Create your config

```bash
cp config.example.yaml config.yaml
nano config.yaml
```

Key values to check for UK single-phase:

```yaml
mains:
  nominal_voltage: 230
  line_frequency: 50
  phases: 1

calibration:
  mmode0: 0x0187      # 50 Hz, 3P4W
  ugain: 20200        # Calibrate this first (see below)
  igain_a: 33500      # For SCT-013-000
  igain_b: 0          # Not connected
  igain_c: 0          # Not connected

events:
  sag_threshold_volts: 207    # 90% of 230 V

gpio:
  enabled: true
  sag_interrupt_pin: 17       # BCM GPIO pin wired to WarnOut
```

---

## Calibration

Calibration is required for accurate readings. Always calibrate **voltage first**, then **current**.

### Step 1 — Calibrate voltage (Ugain)

1. Connect the voltage reference transformer. Apply mains power.
2. Run the smoke test to get an initial reading:

```bash
python3 atm90e36.py
```

3. Note the reported `Va` value and compare to a known-good multimeter reading on the same circuit.
4. Scale Ugain: `new_ugain = old_ugain * (true_voltage / reported_voltage)`
5. Update `config.yaml` → `calibration.ugain`, re-run and verify.

### Step 2 — Calibrate current (Igain)

1. Connect the CT clamp to the phase wire. Apply a **known resistive load** (e.g. an electric heater with a rated wattage — avoid anything with a motor or electronics).
2. Run the smoke test and note `Ia` and `Pa`.
3. Scale Igain: `new_igain = old_igain * (true_current / reported_current)`
   - true_current ≈ rated_watts / measured_voltage
4. Update `config.yaml` → `calibration.igain_a`, re-run and verify.

### Step 3 — Verify power and PF

With the resistive load connected, `Pa` should equal `Va × Ia` and `PF` should be very close to 1.000. If PF is off, you may need phase compensation — see the ATM90E36 datasheet section 4.2.6.

---

## Running

### Smoke test (single snapshot, no hardware loop)

```bash
source .venv/bin/activate
python3 atm90e36.py
```

Prints 10 readings then a full register snapshot. Useful to verify SPI comms and calibration.

### One-shot snapshot via logger

```bash
source .venv/bin/activate
python3 logger.py --once
```

### Continuous logging

```bash
source .venv/bin/activate
python3 logger.py --config config.yaml --log-level INFO
```

Data is written to `data/ipem.db` (SQLite) and `data/measurements.csv` / `data/voltage_events.csv`.

Stop with `Ctrl+C` — the logger shuts down cleanly.

### Debug mode (verbose register output)

```bash
python3 logger.py --log-level DEBUG
```

---

## Running as a systemd service

### Create the service file

```bash
sudo nano /etc/systemd/system/ipem.service
```

Paste:

```ini
[Unit]
Description=IPEM PiHat Mains Monitor
After=network.target

[Service]
Type=simple
User=illysky
WorkingDirectory=/home/illysky/ipem
ExecStart=/home/illysky/ipem/.venv/bin/python3 /home/illysky/ipem/logger.py --config /home/illysky/ipem/config.yaml
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable ipem
sudo systemctl start ipem
sudo systemctl status ipem
```

### View live logs

```bash
journalctl -u ipem -f
# or the local log file:
tail -f /home/illysky/ipem/ipem.log
```

---

## InfluxDB setup (optional)

1. Install InfluxDB v2 on your Pi or a server (see [InfluxDB docs](https://docs.influxdata.com/influxdb/v2/)).
2. Create an organisation, bucket (`ipem`), and API token.
3. Add to `config.yaml`:

```yaml
influxdb:
  url: "http://localhost:8086"
  token: "your-token-here"
  org: "your-org"
  bucket: "ipem"
```

4. Restart the service. Data will flow to InfluxDB alongside the local SQLite.

### Grafana dashboards

Install Grafana, add InfluxDB as a data source, then create:

- **Time series panel** — query `va` from `power_mains`; add threshold lines at 207 V (red) and 253 V (orange)
- **Annotations** from `voltage_event` measurement — shows dip/swell markers on all panels
- **Stat panels** for `p_total`, `pf_total`, `frequency`, `temperature`
- **Event table** from `voltage_event` — sorted by time, showing depth and duration

For local SQLite before setting up InfluxDB, the [Grafana SQLite plugin](https://grafana.com/grafana/plugins/frser-sqlite-datasource/) can read `data/ipem.db` directly.

---

## Project layout

```
/home/illysky/ipem/
├── requirements.txt          # Python dependencies
├── config.example.yaml       # Template — copy to config.yaml
├── config.yaml               # Your local config (git-ignored)
├── atm90e36.py               # ATM90E36 SPI driver (run directly for smoke test)
├── dip_monitor.py            # Fast Urms polling + hardware interrupt + event detection
├── logger.py                 # Orchestrator: slow loop + event drain + all sinks
├── data/
│   ├── ipem.db               # SQLite database
│   ├── measurements.csv      # All metering snapshots
│   └── voltage_events.csv    # Dip and swell events
└── ipem.log                  # Application log
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| All readings 0.00 or 65535 | SPI comms failure | Check `spi.mode` (try 3 instead of 0); check wiring; verify `/dev/spidev0.0` exists |
| Voltage reads but current/power are 0 | CT not connected or Igain=0 | Check CT clamp is fitted; set `igain_a` in config |
| Calibration error in log | Checksum mismatch after init | Run smoke test; if persistent, try power-cycling the PiHat |
| GPIO interrupt not firing | WarnOut pin not wired | Wire WarnOut to GPIO17 (or change `sag_interrupt_pin`); software detection still works without it |
| lgpio import error | Old Pi OS | Install `python3-lgpio` or switch to `RPi.GPIO` in dip_monitor.py |
| InfluxDB not receiving data | Token/URL wrong or Influx not running | Check URL is reachable: `curl http://localhost:8086/health` |

---

## SPI mode note

DitroniX's C code uses **SPI mode 3**; CircuitSetup uses **mode 0**. Start with `mode: 0` in config. If all readings are 0xFFFF or 0x0000, change to `mode: 3` and restart. The correct mode depends on which revision of PiHat firmware/hardware you have.
