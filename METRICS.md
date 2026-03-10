# IPEM Metrics Glossary

All fields written to InfluxDB (`power_mains` measurement) by the IPEM PiHat logger.
Fields are sampled at **~30 Hz** and batch-written every 10 seconds, except
[slow fields](#slow-fields-every-30-s) which are written every 30 seconds.

> **Phase suffixes:** `_a` = Phase A (your live/Line 1), `_b` = Phase B, `_c` = Phase C.
> In a single-phase UK installation only `_a` and `_total` fields carry real data.
> `_b` / `_c` fields will read zero or noise.

---

## Voltage

| Field | Unit | Description |
|-------|------|-------------|
| `va` | V RMS | Phase A (Live) RMS voltage measured at the grid connection. UK nominal is 230 V; acceptable range is 216–253 V (±10%). Watching this field lets you spot dips, swells, and longer-term brownouts. |

---

## Current

| Field | Unit | Description |
|-------|------|-------------|
| `ia` | A RMS | Phase A current — measured by your CT clamp on the Live conductor. Combines all loads in the house and any net import/export current. |
| `in_sampled` | A RMS | Neutral current — directly measured by your CT clamp on the Neutral conductor. In a healthy single-phase circuit this should closely match `ia`. A persistent difference indicates current leakage or a wiring problem. |
| `in_calculated` | A RMS | Neutral current computed by the chip as the vector sum of all phase currents. On single phase this is mathematically equal to `ia`. Comparing `in_calculated` vs `in_sampled` is a useful cross-check of CT calibration. |

---

## Active Power (Real Power)

*This is the power that does actual work — the number on your energy bill.*

| Field | Unit | Description |
|-------|------|-------------|
| `pa` | W | Phase A active power. **Positive = importing from grid** (your loads exceed inverter output). **Negative = exporting to grid** (your inverter is producing more than the house is consuming). |
| `p_total` | W | Total active power across all phases. Equals `pa` on a single-phase installation. |
| `p_total_fund` | W | Total active power at the **fundamental frequency only** (50 Hz component). Excludes harmonic contributions. Compare with `p_total` to see how much of your power is at the base frequency. |

---

## Reactive Power

*Power that oscillates between the source and inductive/capacitive loads without doing useful work. It stresses wiring and transformers but doesn't spin meters (on most domestic tariffs).*

| Field | Unit | Description |
|-------|------|-------------|
| `qa` | VAr | Phase A reactive power. **Positive (lagging/inductive):** motors, transformers, older fluorescent ballasts. **Negative (leading/capacitive):** power-factor-correction capacitors, some inverter operating modes. |
| `q_total` | VAr | Total reactive power. |

---

## Apparent Power

*The total electrical "burden" on your wiring — the vector combination of real and reactive power.*

| Field | Unit | Description |
|-------|------|-------------|
| `sa` | VA | Phase A apparent power = `√(pa² + qa²)`. Determines the minimum cable and fuse ratings required. |
| `s_total` | VA | Total apparent power. |

---

## Power Factor

*How efficiently real work is being done relative to the total current flowing.*

| Field | Unit | Description |
|-------|------|-------------|
| `pf_a` | −1.0 … +1.0 | Phase A power factor = `pa / sa`. **+1.0** = purely resistive (kettle, heater — perfect). **0** = purely reactive (no useful work). **Negative** = net export (inverter production exceeds house load — expected with solar). |
| `pf_total` | −1.0 … +1.0 | Total power factor across all phases. The key headline PF metric. |
| `pf_a_fund` | −1.0 … +1.0 | Power factor calculated using only the 50 Hz fundamental component of current, ignoring harmonics. A cleaner measure of PF for loads with distorted current waveforms (inverters, switch-mode PSUs). |

---

## Harmonic Power

*Power contributed by non-50 Hz frequency components in the current waveform.*

| Field | Unit | Description |
|-------|------|-------------|
| `pa_harm` | W | Phase A harmonic active power — the real power delivered by harmonics (2nd, 3rd, 5th etc.). Switch-mode supplies and inverters are the main sources. Typically small; a large value indicates significant harmonic distortion. |
| `p_total_harm` | W | Total harmonic active power. `p_total ≈ p_total_fund + p_total_harm`. |

---

## Total Harmonic Distortion + Noise (THD+N)

*How close the waveform is to a pure sine wave, expressed as a percentage of the fundamental.*

| Field | Unit | Description |
|-------|------|-------------|
| `thd_va` | % | Voltage THD on Phase A. Healthy UK grid: typically 2–5%. Values above 8% indicate harmonic pollution from nearby industrial loads or a weak grid connection. Your inverter may contribute to this. |
| `thd_ia` | % | Current THD on Phase A. Switch-mode power supplies typically produce 60–100%+ THD at light loads. Grid-tied inverters aim for < 5% THD at rated power, but can be higher at low output. Very high `thd_ia` combined with low `ia` is normal at idle. |

---

## Phase Angle

| Field | Unit | Description |
|-------|------|-------------|
| `phase_angle_a` | degrees | Angle between the Phase A voltage and current waveforms. **0°** = purely resistive load. **+90°** = purely inductive. **−90°** = purely capacitive. Mathematically: `PF = cos(phase_angle_a)`. Useful for diagnosing load type and inverter behaviour. |

---

## Grid Frequency

| Field | Unit | Description |
|-------|------|-------------|
| `frequency` | Hz | Grid frequency. UK nominal is **50.00 Hz**. The grid operator (National Grid) keeps this within ±0.2 Hz under normal conditions. Frequency dips when generation cannot keep up with demand; it rises when there is surplus. Correlating `frequency` with `va` dips can distinguish local wiring events from grid-wide events. |

---

## Slow Fields (every 30 s)

These change slowly and are written as a separate point every `slow_read_interval_s` (default 30 s).

### Temperature

| Field | Unit | Description |
|-------|------|-------------|
| `temperature` | °C | **On-chip temperature of the ATM90E36 metering IC**, not ambient room temperature. Useful as a board health indicator. Normal range is roughly 30–55 °C depending on ambient conditions and board loading. Sustained readings above 70 °C warrant investigation. |

### Cumulative Energy Counters

*These are running totals that accumulate from the last chip reset (i.e. last service restart). They do not persist across reboots.*

| Field | Unit | Description |
|-------|------|-------------|
| `import_kwh` | kWh | Total energy drawn from the grid since last restart. Increments when `pa` is positive. |
| `export_kwh` | kWh | Total energy pushed to the grid since last restart. Increments when `pa` is negative (inverter surplus). |
| `reactive_varh` | VAr·h | Cumulative reactive energy — useful for power-quality analysis and some commercial tariffs that penalise poor power factor. |
| `apparent_vah` | VA·h | Cumulative apparent energy. |

### Status & Diagnostics

| Field | Description |
|-------|-------------|
| `sys_status0` | ATM90E36 system status register 0. Contains flags for: voltage SAG detection, voltage swell, phase-loss detection, zero-crossing errors, and current overflow. Non-zero values indicate the chip detected an anomaly. |
| `sys_status1` | System status register 1. Contains metering error and communication health flags. |
| `meter_status0` | Indicates which phases are actively metering and producing valid data. |
| `meter_status1` | DMA transfer status and additional metering health flags. |

---

## Sampling Architecture

```
ATM90E36 chip
     │
     ├─ read_fast() @ ~30 Hz ──► 300-sample buffer
     │       │                          │
     │       │                  every 10 seconds
     │       │               ┌──────────┴──────────┐
     │       │      ~300 individual points      1 mean row
     │       │        → InfluxDB                → SQLite / CSV
     │       │        (full resolution)         (compact)
     │
     └─ read_slow() every 30 s ──► 1 point → InfluxDB + SQLite
            (temperature, energy totals, status)

     DipMonitor @ 100 Hz ──► voltage_event points on threshold crossing
```

## InfluxDB Measurements

| Measurement | Tags | Fields | Written |
|-------------|------|--------|---------|
| `power_mains` | — | All fields above | Every ~33 ms (batched per 10 s) |
| `power_mains` | — | Slow fields above | Every 30 s |
| `voltage_event` | `event_type` (dip/swell), `trigger` (hardware/software) | `duration_ms`, `depth_pct`, `min_voltage`, `max_voltage`, `nominal_v`, `threshold_v` | On each detected event |
