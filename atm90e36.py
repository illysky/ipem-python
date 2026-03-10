"""
ATM90E36 Python driver for the IPEM PiHat on Raspberry Pi.

Communicates over SPI (Pi as master, using /dev/spidev).
Optionally uses a PCA9671 I2C GPIO expander for /CS when boards are stacked.

SPI frame format (from datasheet section 4.2):
  - 4 bytes per transaction: [addr_hi, addr_lo, data_hi, data_lo]
  - bit15 of address = R/W (1 = read, 0 = write)
  - All bytes MSB first
  - CS assert → 4 µs delay → 2-byte address → 4 µs delay → 2-byte data → CS deassert

Register scaling from CircuitSetup ATM90E36.h/.cpp and DitroniX atm90e36a.c.
"""

import math
import time
import logging
import spidev

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register addresses (from CircuitSetup ATM90E36.h and DitroniX atm30e36a.h)
# ---------------------------------------------------------------------------

# Status / control
REG_SOFT_RESET   = 0x00
REG_SYS_STATUS0  = 0x01
REG_SYS_STATUS1  = 0x02
REG_FUNC_EN0     = 0x03
REG_FUNC_EN1     = 0x04
REG_ZX_CONFIG    = 0x07
REG_SAG_TH       = 0x08
REG_PHASE_LOSS_TH= 0x09
REG_IN_WARN_TH0  = 0x0A
REG_IN_WARN_TH1  = 0x0B
REG_THD_NU_TH    = 0x0C
REG_THD_NI_TH    = 0x0D
REG_DMA_CTRL     = 0x0E
REG_LAST_SPI     = 0x0F

# Configuration
REG_CONFIG_START = 0x30
REG_PL_CONST_H   = 0x31
REG_PL_CONST_L   = 0x32
REG_MMODE0       = 0x33
REG_MMODE1       = 0x34
REG_P_START_TH   = 0x35
REG_Q_START_TH   = 0x36
REG_S_START_TH   = 0x37
REG_P_PHASE_TH   = 0x38
REG_Q_PHASE_TH   = 0x39
REG_S_PHASE_TH   = 0x3A
REG_CS_ZERO      = 0x3B

# Calibration
REG_CAL_START    = 0x40
REG_P_OFFSET_A   = 0x41
REG_Q_OFFSET_A   = 0x42
REG_P_OFFSET_B   = 0x43
REG_Q_OFFSET_B   = 0x44
REG_P_OFFSET_C   = 0x45
REG_Q_OFFSET_C   = 0x46
REG_GAIN_A       = 0x47
REG_PHI_A        = 0x48
REG_GAIN_B       = 0x49
REG_PHI_B        = 0x4A
REG_GAIN_C       = 0x4B
REG_PHI_C        = 0x4C
REG_CS_ONE       = 0x4D

# Harmonic calibration
REG_HARM_START   = 0x50
REG_P_OFFSET_AF  = 0x51
REG_P_OFFSET_BF  = 0x52
REG_P_OFFSET_CF  = 0x53
REG_P_GAIN_AF    = 0x54
REG_P_GAIN_BF    = 0x55
REG_P_GAIN_CF    = 0x56
REG_CS_TWO       = 0x57

# Measurement calibration
REG_ADJ_START    = 0x60
REG_UGAIN_A      = 0x61
REG_IGAIN_A      = 0x62
REG_UOFFSET_A    = 0x63
REG_IOFFSET_A    = 0x64
REG_UGAIN_B      = 0x65
REG_IGAIN_B      = 0x66
REG_UOFFSET_B    = 0x67
REG_IOFFSET_B    = 0x68
REG_UGAIN_C      = 0x69
REG_IGAIN_C      = 0x6A
REG_UOFFSET_C    = 0x6B
REG_IOFFSET_C    = 0x6C
REG_IGAIN_N      = 0x6D
REG_IOFFSET_N    = 0x6E
REG_CS_THREE     = 0x6F

# Energy registers
REG_AP_ENERGY_T  = 0x80
REG_AP_ENERGY_A  = 0x81
REG_AP_ENERGY_B  = 0x82
REG_AP_ENERGY_C  = 0x83
REG_AN_ENERGY_T  = 0x84
REG_AN_ENERGY_A  = 0x85
REG_AN_ENERGY_B  = 0x86
REG_AN_ENERGY_C  = 0x87
REG_RP_ENERGY_T  = 0x88
REG_RP_ENERGY_A  = 0x89
REG_RP_ENERGY_B  = 0x8A
REG_RP_ENERGY_C  = 0x8B
REG_RN_ENERGY_T  = 0x8C
REG_SA_ENERGY_T  = 0x90
REG_S_ENERGY_A   = 0x91
REG_S_ENERGY_B   = 0x92
REG_S_ENERGY_C   = 0x93
REG_EN_STATUS0   = 0x95
REG_EN_STATUS1   = 0x96

# Power and power factor
REG_PMEAN_T      = 0xB0
REG_PMEAN_A      = 0xB1
REG_PMEAN_B      = 0xB2
REG_PMEAN_C      = 0xB3
REG_QMEAN_T      = 0xB4
REG_QMEAN_A      = 0xB5
REG_QMEAN_B      = 0xB6
REG_QMEAN_C      = 0xB7
REG_SMEAN_T      = 0xB8
REG_SMEAN_A      = 0xB9
REG_SMEAN_B      = 0xBA
REG_SMEAN_C      = 0xBB
REG_PF_MEAN_T    = 0xBC
REG_PF_MEAN_A    = 0xBD
REG_PF_MEAN_B    = 0xBE
REG_PF_MEAN_C    = 0xBF

# 32-bit power LSB registers
REG_PMEAN_T_LSB  = 0xC0
REG_PMEAN_A_LSB  = 0xC1
REG_PMEAN_B_LSB  = 0xC2
REG_PMEAN_C_LSB  = 0xC3
REG_QMEAN_T_LSB  = 0xC4
REG_QMEAN_A_LSB  = 0xC5
REG_QMEAN_B_LSB  = 0xC6
REG_QMEAN_C_LSB  = 0xC7
REG_SMEAN_T_LSB  = 0xC8
REG_SMEAN_A_LSB  = 0xC9
REG_SMEAN_B_LSB  = 0xCA
REG_SMEAN_C_LSB  = 0xCB

# Fundamental / harmonic power and RMS
REG_PMEAN_TF     = 0xD0
REG_PMEAN_AF     = 0xD1
REG_PMEAN_BF     = 0xD2
REG_PMEAN_CF     = 0xD3
REG_PMEAN_TH     = 0xD4
REG_PMEAN_AH     = 0xD5
REG_PMEAN_BH     = 0xD6
REG_PMEAN_CH     = 0xD7
REG_IRMS_N1      = 0xD8   # Sampled N current
REG_URMS_A       = 0xD9
REG_URMS_B       = 0xDA
REG_URMS_C       = 0xDB
REG_IRMS_N0      = 0xDC   # Calculated N current
REG_IRMS_A       = 0xDD
REG_IRMS_B       = 0xDE
REG_IRMS_C       = 0xDF

# THD, frequency, phase angle, temperature
REG_THD_NUA      = 0xF1
REG_THD_NUB      = 0xF2
REG_THD_NUC      = 0xF3
REG_THD_NIA      = 0xF5
REG_THD_NIB      = 0xF6
REG_THD_NIC      = 0xF7
REG_FREQ         = 0xF8
REG_P_ANGLE_A    = 0xF9
REG_P_ANGLE_B    = 0xFA
REG_P_ANGLE_C    = 0xFB
REG_TEMP         = 0xFC
REG_U_ANGLE_A    = 0xFD
REG_U_ANGLE_B    = 0xFE
REG_U_ANGLE_C    = 0xFF

# Harmonic DFT result registers (section 6.7, order 2-32 per phase)
# Base addresses: voltage harmonic order n → base + (n-2)
REG_DFT_UA_BASE  = 0x100   # Placeholder — consult datasheet table 14 for exact addresses
REG_DFT_IA_BASE  = 0x120

# Magic values
_SOFT_RESET_VAL  = 0x789A
_BLOCK_START     = 0x5678
_BLOCK_LOCK      = 0x8765
_PL_CONST_H      = 0x0861
_PL_CONST_L      = 0xC468


class ATM90E36:
    """
    Full-featured driver for the ATM90E36 poly-phase energy metering IC.

    Parameters
    ----------
    spi_bus : int
        SPI bus number (usually 0 for /dev/spidev0.x).
    spi_device : int
        SPI device/CS index (0 = CE0, 1 = CE1).
    speed_hz : int
        SPI clock speed in Hz. 200_000 is conservative and reliable.
    mode : int
        SPI mode. Start with 0; fall back to 3 if readings are garbage.
    i2c_bus : int or None
        If set, use a PCA9671 on this I2C bus to drive /CS (for stacked boards).
    pca9671_addr : int
        I2C address of the PCA9671 expander (default 0x20).
    board_cs_bit : int
        Which bit of the PCA9671 output to use as /CS for this board.
    """

    def __init__(
        self,
        spi_bus: int = 0,
        spi_device: int = 0,
        speed_hz: int = 200_000,
        mode: int = 0,
        i2c_bus=None,
        pca9671_addr: int = 0x20,
        board_cs_bit: int = 9,
        pca_idle_state: int = 0x03FF,
    ):
        self._spi = spidev.SpiDev()
        self._spi.open(spi_bus, spi_device)
        self._spi.max_speed_hz = speed_hz
        self._spi.mode = mode
        self._spi.no_cs = False

        self._i2c = None
        self._pca9671_addr = pca9671_addr
        self._cs_mask = 1 << board_cs_bit
        self._pca_state = pca_idle_state

        if i2c_bus is not None:
            import smbus2
            # PCA9671 owns /CS; spidev must not auto-toggle the CE0 pin
            self._spi.no_cs = True
            self._i2c = smbus2.SMBus(i2c_bus)
            self._pca_write(self._pca_state)  # set idle state (CS deasserted)

        self._ugain = 20200
        self._igain_a = 33500
        self._igain_b = 33500
        self._igain_c = 33500
        self._igain_n = 0xFD7F

    # ------------------------------------------------------------------
    # Low-level SPI communication
    # ------------------------------------------------------------------

    def _cs_assert(self):
        """Assert /CS via PCA9671 if using I2C expander; spidev handles it otherwise."""
        if self._i2c is not None:
            self._pca_state &= ~self._cs_mask
            self._pca_write(self._pca_state)

    def _cs_deassert(self):
        """Deassert /CS via PCA9671."""
        if self._i2c is not None:
            self._pca_state |= self._cs_mask
            self._pca_write(self._pca_state)

    def _pca_write(self, value: int):
        """Write 16-bit output to PCA9671 (two bytes, low byte first)."""
        lo = value & 0xFF
        hi = (value >> 8) & 0xFF
        self._i2c.write_i2c_block_data(self._pca9671_addr, lo, [hi])

    def _transfer(self, rw: int, address: int, value: int) -> int:
        """
        Execute one 4-byte SPI transaction.

        rw      : 1 = read, 0 = write
        address : register address (10-bit)
        value   : 16-bit data to write (ignored on read; send 0xFFFF)
        returns : 16-bit register value (on read); echoed value (on write)
        """
        addr_word = (address & 0x03FF) | (rw << 15)
        tx = [
            (addr_word >> 8) & 0xFF,
            addr_word & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]

        if self._i2c is not None:
            self._cs_assert()
            time.sleep(4e-6)
            rx = self._spi.xfer2(tx)
            time.sleep(4e-6)
            self._cs_deassert()
        else:
            rx = self._spi.xfer2(tx)

        result = (rx[2] << 8) | rx[3]
        return result

    def read_reg(self, address: int) -> int:
        """Read a 16-bit register."""
        return self._transfer(1, address, 0xFFFF)

    def write_reg(self, address: int, value: int):
        """Write a 16-bit register."""
        self._transfer(0, address, value & 0xFFFF)

    def read_reg32(self, addr_hi: int, addr_lo: int) -> int:
        """
        Read a 32-bit value from two consecutive registers.
        Reads hi twice (per CircuitSetup pattern) to ensure consistency.
        """
        hi = self.read_reg(addr_hi)
        lo = self.read_reg(addr_lo)
        hi = self.read_reg(addr_hi)   # second read for stability
        return (hi << 16) | lo

    # ------------------------------------------------------------------
    # Checksum calculation (required before locking each config block)
    # ------------------------------------------------------------------

    def _checksum(self, start_addr: int, end_addr: int) -> int:
        """
        Compute the ATM90E36 checksum for a register range.
        Algorithm from CircuitSetup ATM90E36.cpp checkSum().
        """
        tmp_l = 0
        tmp_h = 0
        for addr in range(start_addr, end_addr + 1):
            val = self.read_reg(addr)
            lo = val & 0xFF
            hi = (val >> 8) & 0xFF
            tmp_l += lo + hi
            tmp_h ^= lo ^ hi
        cs = ((tmp_l % 256 + 256) % 256) | (((tmp_h & 0xFF) << 8) & 0xFF00)
        return cs

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def init_meter(
        self,
        line_freq_reg: int = 0x0187,
        pga_gain: int = 0x0000,
        ugain: int = 20200,
        igain_a: int = 33500,
        igain_b: int = 33500,
        igain_c: int = 33500,
        igain_n: int = 0xFD7F,
    ):
        """
        Initialise the ATM90E36 for metering.

        line_freq_reg : MMode0 register value.
                        0x0187 = 50 Hz 3P4W (UK/EU default)
                        0x0D87 = 60 Hz 3P4W (US)
        pga_gain      : MMode1 — PGA gain. 0x0000 = ×1, 0x5555 = ×2.
        ugain         : Voltage RMS gain (all three phases share this on PiHat).
        igain_a/b/c/n : Current RMS gain per channel.
        """
        self._ugain   = ugain
        self._igain_a = igain_a
        self._igain_b = igain_b
        self._igain_c = igain_c
        self._igain_n = igain_n

        log.info("Resetting ATM90E36...")
        self.write_reg(REG_SOFT_RESET, _SOFT_RESET_VAL)
        time.sleep(0.1)

        self.write_reg(REG_FUNC_EN0, 0x0000)
        self.write_reg(REG_FUNC_EN1, 0x0000)
        self.write_reg(REG_SAG_TH, 0x0001)  # placeholder; call configure_sag() after

        # --- Configuration block ---
        self.write_reg(REG_CONFIG_START, _BLOCK_START)
        self.write_reg(REG_PL_CONST_H,  _PL_CONST_H)
        self.write_reg(REG_PL_CONST_L,  _PL_CONST_L)
        self.write_reg(REG_MMODE0,      line_freq_reg)
        self.write_reg(REG_MMODE1,      pga_gain)
        self.write_reg(REG_P_START_TH,  0x0000)
        self.write_reg(REG_Q_START_TH,  0x0000)
        self.write_reg(REG_S_START_TH,  0x0000)
        self.write_reg(REG_P_PHASE_TH,  0x0000)
        self.write_reg(REG_Q_PHASE_TH,  0x0000)
        self.write_reg(REG_S_PHASE_TH,  0x0000)
        cs0 = self._checksum(REG_PL_CONST_H, REG_S_PHASE_TH)
        self.write_reg(REG_CS_ZERO, cs0)
        log.debug("CSZero = 0x%04X", cs0)

        # --- Calibration block ---
        self.write_reg(REG_CAL_START,   _BLOCK_START)
        self.write_reg(REG_GAIN_A,      0x0000)
        self.write_reg(REG_PHI_A,       0x0000)
        self.write_reg(REG_GAIN_B,      0x0000)
        self.write_reg(REG_PHI_B,       0x0000)
        self.write_reg(REG_GAIN_C,      0x0000)
        self.write_reg(REG_PHI_C,       0x0000)
        self.write_reg(REG_P_OFFSET_A,  0x0000)
        self.write_reg(REG_Q_OFFSET_A,  0x0000)
        self.write_reg(REG_P_OFFSET_B,  0x0000)
        self.write_reg(REG_Q_OFFSET_B,  0x0000)
        self.write_reg(REG_P_OFFSET_C,  0x0000)
        self.write_reg(REG_Q_OFFSET_C,  0x0000)
        cs1 = self._checksum(REG_P_OFFSET_A, REG_PHI_C)
        self.write_reg(REG_CS_ONE, cs1)
        log.debug("CSOne = 0x%04X", cs1)

        # --- Harmonic block ---
        self.write_reg(REG_HARM_START,  _BLOCK_START)
        self.write_reg(REG_P_OFFSET_AF, 0x0000)
        self.write_reg(REG_P_OFFSET_BF, 0x0000)
        self.write_reg(REG_P_OFFSET_CF, 0x0000)
        self.write_reg(REG_P_GAIN_AF,   0x0000)
        self.write_reg(REG_P_GAIN_BF,   0x0000)
        self.write_reg(REG_P_GAIN_CF,   0x0000)
        cs2 = self._checksum(REG_P_OFFSET_AF, REG_P_GAIN_CF)
        self.write_reg(REG_CS_TWO, cs2)
        log.debug("CSTwo = 0x%04X", cs2)

        # --- Measurement/adjustment block ---
        self.write_reg(REG_ADJ_START,   _BLOCK_START)
        self.write_reg(REG_UGAIN_A,     ugain)
        self.write_reg(REG_IGAIN_A,     igain_a)
        self.write_reg(REG_UOFFSET_A,   0x0000)
        self.write_reg(REG_IOFFSET_A,   0x0000)
        self.write_reg(REG_UGAIN_B,     ugain)
        self.write_reg(REG_IGAIN_B,     igain_b)
        self.write_reg(REG_UOFFSET_B,   0x0000)
        self.write_reg(REG_IOFFSET_B,   0x0000)
        self.write_reg(REG_UGAIN_C,     ugain)
        self.write_reg(REG_IGAIN_C,     igain_c)
        self.write_reg(REG_UOFFSET_C,   0x0000)
        self.write_reg(REG_IOFFSET_C,   0x0000)
        self.write_reg(REG_IGAIN_N,     igain_n)
        self.write_reg(REG_IOFFSET_N,   0x0000)
        cs3 = self._checksum(REG_UGAIN_A, REG_IOFFSET_N)
        self.write_reg(REG_CS_THREE, cs3)
        log.debug("CSThree = 0x%04X", cs3)

        # Lock all blocks
        self.write_reg(REG_CONFIG_START, _BLOCK_LOCK)
        self.write_reg(REG_CAL_START,    _BLOCK_LOCK)
        self.write_reg(REG_HARM_START,   _BLOCK_LOCK)
        self.write_reg(REG_ADJ_START,    _BLOCK_LOCK)

        log.info("ATM90E36 initialised. Calibration error: %s", self.calibration_error())

    def configure_sag(self, threshold_v: float, ugain: int = None):
        """
        Set the hardware voltage SAG detection threshold.

        threshold_v : Threshold in Volts RMS (e.g. 207 for 90% of 230 V).
        ugain       : Voltage gain (uses stored value from init_meter if not supplied).

        Formula from datasheet:
            SagTh = (Vth * 100 * sqrt(2)) / (2 * Ugain / 32768)
        """
        if ugain is None:
            ugain = self._ugain
        sag_th = int((threshold_v * 100.0 * math.sqrt(2.0)) / (2.0 * ugain / 32768.0))
        sag_th = max(0, min(0xFFFF, sag_th))
        log.info("Setting SagTh = 0x%04X (%.1f V threshold)", sag_th, threshold_v)
        self.write_reg(REG_SAG_TH, sag_th)

    # ------------------------------------------------------------------
    # Voltage
    # ------------------------------------------------------------------

    def get_voltage_a(self) -> float:
        return self.read_reg(REG_URMS_A) / 100.0

    def get_voltage_b(self) -> float:
        return self.read_reg(REG_URMS_B) / 100.0

    def get_voltage_c(self) -> float:
        return self.read_reg(REG_URMS_C) / 100.0

    def get_voltages(self) -> tuple:
        """Return (Va, Vb, Vc) in one call — minimises SPI round-trips for fast polling."""
        return (self.get_voltage_a(), self.get_voltage_b(), self.get_voltage_c())

    # ------------------------------------------------------------------
    # Current
    # ------------------------------------------------------------------

    def get_current_a(self) -> float:
        return self.read_reg(REG_IRMS_A) / 1000.0

    def get_current_b(self) -> float:
        return self.read_reg(REG_IRMS_B) / 1000.0

    def get_current_c(self) -> float:
        return self.read_reg(REG_IRMS_C) / 1000.0

    def get_current_n_sampled(self) -> float:
        """Sampled neutral current (IrmsN1)."""
        return self.read_reg(REG_IRMS_N1) / 1000.0

    def get_current_n_calculated(self) -> float:
        """Calculated neutral current (IrmsN0)."""
        return self.read_reg(REG_IRMS_N0) / 1000.0

    # ------------------------------------------------------------------
    # Active power (W)
    # ------------------------------------------------------------------

    def get_active_power_a(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_A)) / 1000.0

    def get_active_power_b(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_B)) / 1000.0

    def get_active_power_c(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_C)) / 1000.0

    def get_active_power_total(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_T)) / 250.0

    def get_active_power_a_32(self) -> float:
        """32-bit active power for phase A — higher resolution."""
        return self.read_reg32(REG_PMEAN_A, REG_PMEAN_A_LSB) * 0.00032

    def get_active_power_b_32(self) -> float:
        return self.read_reg32(REG_PMEAN_B, REG_PMEAN_B_LSB) * 0.00032

    def get_active_power_c_32(self) -> float:
        return self.read_reg32(REG_PMEAN_C, REG_PMEAN_C_LSB) * 0.00032

    # ------------------------------------------------------------------
    # Reactive power (VAR)
    # ------------------------------------------------------------------

    def get_reactive_power_a(self) -> float:
        return self._signed16(self.read_reg(REG_QMEAN_A)) / 1000.0

    def get_reactive_power_b(self) -> float:
        return self._signed16(self.read_reg(REG_QMEAN_B)) / 1000.0

    def get_reactive_power_c(self) -> float:
        return self._signed16(self.read_reg(REG_QMEAN_C)) / 1000.0

    def get_reactive_power_total(self) -> float:
        return self._signed16(self.read_reg(REG_QMEAN_T)) / 250.0

    # ------------------------------------------------------------------
    # Apparent power (VA)
    # ------------------------------------------------------------------

    def get_apparent_power_a(self) -> float:
        return self._signed16(self.read_reg(REG_SMEAN_A)) / 1000.0

    def get_apparent_power_b(self) -> float:
        return self._signed16(self.read_reg(REG_SMEAN_B)) / 1000.0

    def get_apparent_power_c(self) -> float:
        return self._signed16(self.read_reg(REG_SMEAN_C)) / 1000.0

    def get_apparent_power_total(self) -> float:
        return self._signed16(self.read_reg(REG_SMEAN_T)) / 250.0

    # ------------------------------------------------------------------
    # Power factor (-1.0 to +1.0; sign = leading/lagging)
    # ------------------------------------------------------------------

    def get_pf_a(self) -> float:
        return self._signed16(self.read_reg(REG_PF_MEAN_A)) / 1000.0

    def get_pf_b(self) -> float:
        return self._signed16(self.read_reg(REG_PF_MEAN_B)) / 1000.0

    def get_pf_c(self) -> float:
        return self._signed16(self.read_reg(REG_PF_MEAN_C)) / 1000.0

    def get_pf_total(self) -> float:
        return self._signed16(self.read_reg(REG_PF_MEAN_T)) / 1000.0

    # ------------------------------------------------------------------
    # Frequency (Hz)
    # ------------------------------------------------------------------

    def get_frequency(self) -> float:
        return self.read_reg(REG_FREQ) / 100.0

    # ------------------------------------------------------------------
    # THD+N (Total Harmonic Distortion + Noise)
    # Values are raw register counts; divide by 100 to get percentage.
    # ------------------------------------------------------------------

    def get_thd_voltage_a(self) -> float:
        return self.read_reg(REG_THD_NUA) / 100.0

    def get_thd_voltage_b(self) -> float:
        return self.read_reg(REG_THD_NUB) / 100.0

    def get_thd_voltage_c(self) -> float:
        return self.read_reg(REG_THD_NUC) / 100.0

    def get_thd_current_a(self) -> float:
        return self.read_reg(REG_THD_NIA) / 100.0

    def get_thd_current_b(self) -> float:
        return self.read_reg(REG_THD_NIB) / 100.0

    def get_thd_current_c(self) -> float:
        return self.read_reg(REG_THD_NIC) / 100.0

    # ------------------------------------------------------------------
    # Phase and voltage angles (degrees)
    # ------------------------------------------------------------------

    def get_phase_angle_a(self) -> float:
        return self._signed16(self.read_reg(REG_P_ANGLE_A)) / 10.0

    def get_phase_angle_b(self) -> float:
        return self._signed16(self.read_reg(REG_P_ANGLE_B)) / 10.0

    def get_phase_angle_c(self) -> float:
        return self._signed16(self.read_reg(REG_P_ANGLE_C)) / 10.0

    def get_voltage_angle_a(self) -> float:
        return self._signed16(self.read_reg(REG_U_ANGLE_A)) / 10.0

    def get_voltage_angle_b(self) -> float:
        return self._signed16(self.read_reg(REG_U_ANGLE_B)) / 10.0

    def get_voltage_angle_c(self) -> float:
        return self._signed16(self.read_reg(REG_U_ANGLE_C)) / 10.0

    # ------------------------------------------------------------------
    # Temperature (°C)
    # ------------------------------------------------------------------

    def get_temperature(self) -> float:
        return float(self._signed16(self.read_reg(REG_TEMP)))

    # ------------------------------------------------------------------
    # Fundamental and harmonic active power
    # ------------------------------------------------------------------

    def get_fundamental_power_a(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_AF)) / 1000.0

    def get_fundamental_power_b(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_BF)) / 1000.0

    def get_fundamental_power_c(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_CF)) / 1000.0

    def get_fundamental_power_total(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_TF)) / 250.0

    def get_harmonic_power_a(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_AH)) / 1000.0

    def get_harmonic_power_b(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_BH)) / 1000.0

    def get_harmonic_power_c(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_CH)) / 1000.0

    def get_harmonic_power_total(self) -> float:
        return self._signed16(self.read_reg(REG_PMEAN_TH)) / 250.0

    # ------------------------------------------------------------------
    # Energy (Wh)
    # ------------------------------------------------------------------

    def get_import_energy_wh(self) -> float:
        """Total forward active energy in Wh."""
        raw = self.read_reg(REG_AP_ENERGY_T)
        return (raw / 32.0) * 3600.0

    def get_export_energy_wh(self) -> float:
        """Total reverse active energy in Wh."""
        raw = self.read_reg(REG_AN_ENERGY_T)
        return (raw / 32.0) * 3600.0

    def get_import_energy_kwh(self) -> float:
        return self.get_import_energy_wh() / 1000.0

    def get_export_energy_kwh(self) -> float:
        return self.get_export_energy_wh() / 1000.0

    def get_reactive_energy_total_wh(self) -> float:
        raw = self.read_reg(REG_RP_ENERGY_T)
        return (raw / 32.0) * 3600.0

    def get_apparent_energy_total_wh(self) -> float:
        raw = self.read_reg(REG_SA_ENERGY_T)
        return (raw / 32.0) * 3600.0

    # ------------------------------------------------------------------
    # Status registers
    # ------------------------------------------------------------------

    def get_sys_status0(self) -> int:
        return self.read_reg(REG_SYS_STATUS0)

    def get_sys_status1(self) -> int:
        return self.read_reg(REG_SYS_STATUS1)

    def get_meter_status0(self) -> int:
        return self.read_reg(REG_EN_STATUS0)

    def get_meter_status1(self) -> int:
        return self.read_reg(REG_EN_STATUS1)

    def calibration_error(self) -> bool:
        """
        Return True if any checksum error flag is set in SysStatus0.
        Bits 14, 12, 10, 8 correspond to CS0–CS3 errors.
        """
        status = self.get_sys_status0()
        error_mask = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 8)
        if status & error_mask:
            for bit, name in [(14, "CS0"), (12, "CS1"), (10, "CS2"), (8, "CS3")]:
                if status & (1 << bit):
                    log.warning("Calibration checksum error: %s", name)
            return True
        return False

    # ------------------------------------------------------------------
    # Full snapshot — all metering values in one dict
    # ------------------------------------------------------------------

    def read_all(self) -> dict:
        """
        Read all metering registers and return as a flat dictionary.
        Use for the slow logging thread.
        """
        return {
            # Voltage
            "va":              self.get_voltage_a(),
            "vb":              self.get_voltage_b(),
            "vc":              self.get_voltage_c(),
            # Current
            "ia":              self.get_current_a(),
            "ib":              self.get_current_b(),
            "ic":              self.get_current_c(),
            "in_sampled":      self.get_current_n_sampled(),
            "in_calculated":   self.get_current_n_calculated(),
            # Active power
            "pa":              self.get_active_power_a(),
            "pb":              self.get_active_power_b(),
            "pc":              self.get_active_power_c(),
            "p_total":         self.get_active_power_total(),
            # Reactive power
            "qa":              self.get_reactive_power_a(),
            "qb":              self.get_reactive_power_b(),
            "qc":              self.get_reactive_power_c(),
            "q_total":         self.get_reactive_power_total(),
            # Apparent power
            "sa":              self.get_apparent_power_a(),
            "sb":              self.get_apparent_power_b(),
            "sc":              self.get_apparent_power_c(),
            "s_total":         self.get_apparent_power_total(),
            # Power factor
            "pf_a":            self.get_pf_a(),
            "pf_b":            self.get_pf_b(),
            "pf_c":            self.get_pf_c(),
            "pf_total":        self.get_pf_total(),
            # Frequency
            "frequency":       self.get_frequency(),
            # THD+N
            "thd_va":          self.get_thd_voltage_a(),
            "thd_vb":          self.get_thd_voltage_b(),
            "thd_vc":          self.get_thd_voltage_c(),
            "thd_ia":          self.get_thd_current_a(),
            "thd_ib":          self.get_thd_current_b(),
            "thd_ic":          self.get_thd_current_c(),
            # Phase / voltage angles
            "phase_angle_a":   self.get_phase_angle_a(),
            "phase_angle_b":   self.get_phase_angle_b(),
            "phase_angle_c":   self.get_phase_angle_c(),
            "voltage_angle_a": self.get_voltage_angle_a(),
            "voltage_angle_b": self.get_voltage_angle_b(),
            "voltage_angle_c": self.get_voltage_angle_c(),
            # Temperature
            "temperature":     self.get_temperature(),
            # Fundamental / harmonic power
            "pf_a_fund":       self.get_fundamental_power_a(),
            "pf_b_fund":       self.get_fundamental_power_b(),
            "pf_c_fund":       self.get_fundamental_power_c(),
            "p_total_fund":    self.get_fundamental_power_total(),
            "pa_harm":         self.get_harmonic_power_a(),
            "pb_harm":         self.get_harmonic_power_b(),
            "pc_harm":         self.get_harmonic_power_c(),
            "p_total_harm":    self.get_harmonic_power_total(),
            # Energy
            "import_kwh":      self.get_import_energy_kwh(),
            "export_kwh":      self.get_export_energy_kwh(),
            "reactive_varh":   self.get_reactive_energy_total_wh(),
            "apparent_vah":    self.get_apparent_energy_total_wh(),
            # Status
            "sys_status0":     self.get_sys_status0(),
            "sys_status1":     self.get_sys_status1(),
            "meter_status0":   self.get_meter_status0(),
            "meter_status1":   self.get_meter_status1(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _signed16(value: int) -> int:
        """Convert an unsigned 16-bit integer to a signed Python int."""
        if value >= 0x8000:
            return value - 0x10000
        return value

    def close(self):
        """Release SPI (and I2C if open)."""
        self._spi.close()
        if self._i2c is not None:
            self._i2c.close()


# ---------------------------------------------------------------------------
# Smoke test — run directly to confirm SPI comms and basic readings
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import os

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if not os.path.exists(config_path):
        config_path = os.path.join(os.path.dirname(__file__), "config.example.yaml")

    import yaml
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    spi_cfg = cfg.get("spi", {})
    i2c_cfg = cfg.get("i2c", {})
    cal = cfg.get("calibration", {})
    mains = cfg.get("mains", {})

    i2c_bus_arg = i2c_cfg.get("bus", 1) if i2c_cfg.get("enabled", False) else None

    meter = ATM90E36(
        spi_bus=spi_cfg.get("bus", 0),
        spi_device=spi_cfg.get("device", 0),
        speed_hz=spi_cfg.get("speed_hz", 200_000),
        mode=spi_cfg.get("mode", 0),
        i2c_bus=i2c_bus_arg,
        pca9671_addr=i2c_cfg.get("pca9671_address", 0x20),
        board_cs_bit=i2c_cfg.get("board_cs_bit", 9),
        pca_idle_state=i2c_cfg.get("pca_idle_state", 0x03FF),
    )

    meter.init_meter(
        line_freq_reg=cal.get("mmode0", 0x0187),
        pga_gain=cal.get("pga_gain", 0x0000),
        ugain=cal.get("ugain", 20200),
        igain_a=cal.get("igain_a", 33500),
        igain_b=cal.get("igain_b", 33500),
        igain_c=cal.get("igain_c", 33500),
        igain_n=cal.get("igain_n", 0xFD7F),
    )

    events_cfg = cfg.get("events", {})
    meter.configure_sag(
        threshold_v=events_cfg.get("sag_threshold_volts", 207),
    )

    print("\n--- ATM90E36 Smoke Test ---")
    print(f"SysStatus0 : 0x{meter.get_sys_status0():04X}")
    print(f"CalibError : {meter.calibration_error()}")
    print()

    for i in range(10):
        va, vb, vc = meter.get_voltages()
        freq = meter.get_frequency()
        ia   = meter.get_current_a()
        pa   = meter.get_active_power_a()
        pf_a = meter.get_pf_a()
        temp = meter.get_temperature()
        print(
            f"[{i+1:2d}] Va={va:6.2f}V  Vb={vb:6.2f}V  Vc={vc:6.2f}V  "
            f"Ia={ia:6.3f}A  Pa={pa:7.2f}W  PF={pf_a:+.3f}  "
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
