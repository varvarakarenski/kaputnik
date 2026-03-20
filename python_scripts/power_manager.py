"""
power_manager.py
================
Kaputnik Power Management
Handles PiSugar 3 (battery + RTC) and DFRobot DFR0559 (solar charging).

PiSugar 3  → queried via its daemon socket (most reliable) OR direct I²C at 0x57
DFR0559    → analog-only board, no I²C. Battery voltage read via Pi ADC GPIO pin,
             OR inferred from PiSugar 3 data (recommended — PiSugar monitors the
             shared battery and is already connected).

Install PiSugar daemon first:
    curl https://cdn.pisugar.com/release/pisugar-power-manager.sh | sudo bash
    (select PiSugar 3 when prompted)

Then enable I²C on the Pi:
    sudo raspi-config → Interface Options → I2C → Enable
"""

import socket
import time
import logging
import subprocess
from datetime import datetime, timezone, timedelta

try:
    import smbus2
    I2C_AVAILABLE = True
except ImportError:
    I2C_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False

log = logging.getLogger("kaputnik.power")

# ─────────────────────────────────────────────
# PISUGAR 3 CONSTANTS
# ─────────────────────────────────────────────
PISUGAR_I2C_ADDR     = 0x57   # Default I²C address (check your unit with i2cdetect -y 1)
PISUGAR_I2C_BUS      = 1      # I²C bus 1 on RPi 4

# PiSugar 3 I²C register map (from official datasheet)
REG_WRITE_PROTECT    = 0x0B   # Write 0x29 to unlock, 0xFF to lock
REG_VOLTAGE_HIGH     = 0x22   # Battery voltage high byte
REG_VOLTAGE_LOW      = 0x23   # Battery voltage low byte
REG_PERCENT          = 0x2A   # Battery percentage (0–100)
REG_POWER_SOURCE     = 0x02   # Bit 7: 1 = external power connected (solar/USB charging)
REG_CHARGING_STATUS  = 0x02   # Same register, bit 6: 1 = actively charging

# PiSugar daemon socket (preferred method — more stable than raw I²C)
PISUGAR_SOCKET       = "/tmp/pisugar-server.sock"
PISUGAR_TCP_HOST     = "127.0.0.1"
PISUGAR_TCP_PORT     = 8423

# ─────────────────────────────────────────────
# DFR0559 SOLAR PANEL CONSTANTS
# ─────────────────────────────────────────────
# The DFR0559 has no I²C interface — it charges the battery autonomously.
# Battery voltage is readable via the VBAT pin wired to a Pi ADC input.
# If you have an ADS1115 ADC module (recommended), set USE_ADS1115 = True.
# Otherwise the PiSugar handles battery monitoring and DFR0559 just charges.
SOLAR_VBAT_GPIO_PIN  = 17     # GPIO pin for analog VBAT read (only if using raw ADC)
USE_ADS1115          = False   # Set True if you have an ADS1115 I²C ADC wired to VBAT

# ADS1115 settings (only used if USE_ADS1115 = True)
ADS1115_I2C_ADDR     = 0x48
ADS1115_CHANNEL      = 0       # AIN0 wired to DFR0559 VBAT pin

# ─────────────────────────────────────────────
# PISUGAR: DAEMON SOCKET INTERFACE (preferred)
# ─────────────────────────────────────────────

def _query_pisugar_daemon(command: str) -> str | None:
    """
    Send a command to the PiSugar power manager daemon via Unix socket.
    Returns the response string, or None on failure.

    Available commands:
        get battery          → e.g. "battery: 87.3"
        get battery_v        → e.g. "battery_v: 3.95"
        get battery_charging → e.g. "battery_charging: true"
        get battery_power_plugged → e.g. "battery_power_plugged: true"
        rtc_alarm_set <datetime> <weekday_repeat>
        rtc_pi2rtc
        rtc_rtc2pi
    """
    # Try Unix domain socket first (preferred)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(PISUGAR_SOCKET)
            s.sendall((command + "\n").encode())
            response = s.recv(256).decode().strip()
            return response
    except Exception:
        pass

    # Fall back to TCP socket
    try:
        with socket.create_connection((PISUGAR_TCP_HOST, PISUGAR_TCP_PORT), timeout=3) as s:
            s.sendall((command + "\n").encode())
            response = s.recv(256).decode().strip()
            return response
    except Exception as e:
        log.warning(f"PiSugar daemon unreachable: {e}")
        return None


def get_battery_percent() -> float:
    """Returns battery charge level as a percentage (0.0–100.0)."""
    response = _query_pisugar_daemon("get battery")
    if response:
        try:
            # Response format: "battery: 87.30"
            return float(response.split(":")[1].strip())
        except (IndexError, ValueError):
            pass
    # Fallback: read directly from I²C register
    return _read_battery_percent_i2c()


def get_battery_voltage() -> float:
    """Returns battery voltage in volts (nominally 3.0–4.2 V for LiPo)."""
    response = _query_pisugar_daemon("get battery_v")
    if response:
        try:
            return float(response.split(":")[1].strip())
        except (IndexError, ValueError):
            pass
    return _read_battery_voltage_i2c()


def get_battery_wh(capacity_mah: float = 1200.0) -> float:
    """
    Estimate remaining energy in Wh.
    Default capacity_mah = 1200 (PiSugar 3 standard) — change to 5000 if using PiSugar 3 Plus.
    """
    voltage = get_battery_voltage()
    percent = get_battery_percent() / 100.0
    return voltage * (capacity_mah / 1000.0) * percent


def is_charging() -> bool:
    """Returns True if the battery is currently being charged (solar or USB input)."""
    response = _query_pisugar_daemon("get battery_charging")
    if response:
        return "true" in response.lower()
    return False


def is_solar_connected() -> bool:
    """Returns True if external power (solar panel or USB) is connected to the DFR0559."""
    response = _query_pisugar_daemon("get battery_power_plugged")
    if response:
        return "true" in response.lower()
    return False


def get_full_power_status() -> dict:
    """Returns a complete snapshot of battery and solar status."""
    return {
        "battery_percent"  : get_battery_percent(),
        "battery_voltage_v": get_battery_voltage(),
        "battery_wh"       : get_battery_wh(),
        "is_charging"      : is_charging(),
        "solar_connected"  : is_solar_connected(),
        "timestamp"        : datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────
# PISUGAR: DIRECT I²C FALLBACK
# ─────────────────────────────────────────────

def _read_battery_voltage_i2c() -> float:
    """Read battery voltage directly from PiSugar 3 I²C registers (fallback)."""
    if not I2C_AVAILABLE:
        log.warning("smbus2 not available. Returning simulated voltage.")
        return 3.85  # Simulated mid-charge voltage

    try:
        bus = smbus2.SMBus(PISUGAR_I2C_BUS)
        high = bus.read_byte_data(PISUGAR_I2C_ADDR, REG_VOLTAGE_HIGH)
        low  = bus.read_byte_data(PISUGAR_I2C_ADDR, REG_VOLTAGE_LOW)
        bus.close()
        # PiSugar 3 voltage = (high << 8 | low) * 0.001 volts
        raw = (high << 8) | low
        return raw * 0.001
    except Exception as e:
        log.warning(f"I²C battery voltage read failed: {e}")
        return 3.85


def _read_battery_percent_i2c() -> float:
    """Read battery percentage directly from PiSugar 3 I²C register (fallback)."""
    if not I2C_AVAILABLE:
        return 85.0  # Simulated

    try:
        bus = smbus2.SMBus(PISUGAR_I2C_BUS)
        raw = bus.read_byte_data(PISUGAR_I2C_ADDR, REG_PERCENT)
        bus.close()
        return float(raw)
    except Exception as e:
        log.warning(f"I²C battery percent read failed: {e}")
        return 85.0


# ─────────────────────────────────────────────
# DFR0559 SOLAR: VBAT ADC READ (optional)
# ─────────────────────────────────────────────

def read_solar_vbat_ads1115() -> float | None:
    """
    Read battery voltage via ADS1115 ADC wired to DFR0559 VBAT pin.
    Returns voltage in volts, or None if unavailable.
    Only needed if you want independent solar-side voltage monitoring
    separate from PiSugar. Most builds can skip this.

    Wiring:
        DFR0559 VBAT → ADS1115 AIN0
        ADS1115 SDA/SCL → Pi SDA/SCL (GPIO 2/3)
        ADS1115 VDD → 3.3V, GND → GND
    """
    if not USE_ADS1115 or not I2C_AVAILABLE:
        return None

    try:
        bus = smbus2.SMBus(PISUGAR_I2C_BUS)
        # Configure ADS1115: single-shot, AIN0 vs GND, ±4.096V FSR, 128 SPS
        config = 0b1_100_000_0_0_0_0_0_0_1_1_1  # OS=1 MUX=100 PGA=001 MODE=1 DR=100 COMP_MODE=0 etc
        config_high = (config >> 8) & 0xFF
        config_low  = config & 0xFF
        bus.write_i2c_block_data(ADS1115_I2C_ADDR, 0x01, [config_high, config_low])
        time.sleep(0.01)  # Wait for conversion

        result = bus.read_i2c_block_data(ADS1115_I2C_ADDR, 0x00, 2)
        bus.close()

        raw = (result[0] << 8) | result[1]
        if raw > 32767:
            raw -= 65536
        volts = raw * (4.096 / 32768.0)
        return round(volts, 3)
    except Exception as e:
        log.warning(f"ADS1115 solar VBAT read failed: {e}")
        return None


# ─────────────────────────────────────────────
# PISUGAR 3: RTC SCHEDULED WAKEUP
# ─────────────────────────────────────────────

def schedule_rtc_wakeup(wake_time: datetime):
    """
    Program the PiSugar 3's onboard RTC to wake the Pi at a specific UTC time.
    This replaces time.sleep() for true deep sleep — the Pi fully powers off
    and the RTC fires a wake signal at the scheduled time.

    wake_time: timezone-aware datetime in UTC
    """
    # PiSugar daemon RTC command format: "rtc_alarm_set 2026-01-01T12:00:00 127"
    # 127 = repeat every day (weekday bitmask: all 7 days = 0b1111111 = 127)
    dt_str = wake_time.strftime("%Y-%m-%dT%H:%M:%S")
    command = f"rtc_alarm_set {dt_str} 127"

    response = _query_pisugar_daemon(command)
    if response:
        log.info(f"RTC wakeup scheduled for {dt_str} UTC. Response: {response}")
    else:
        log.warning("Failed to set RTC wakeup — falling back to time.sleep().")

    # Sync Pi system clock TO RTC before sleeping
    _query_pisugar_daemon("rtc_pi2rtc")


def sync_time_from_rtc():
    """Sync Pi system clock FROM RTC on wakeup (call at start of each orbit)."""
    _query_pisugar_daemon("rtc_rtc2pi")
    try:
        subprocess.run(["sudo", "hwclock", "-s"], check=True)
        log.info("System clock synced from PiSugar 3 RTC.")
    except Exception as e:
        log.warning(f"hwclock sync failed: {e}")


def deep_sleep_until(wake_time: datetime):
    """
    Full deep sleep:
      1. Program RTC alarm on PiSugar 3.
      2. Issue a clean shutdown — PiSugar cuts power.
      3. RTC wakes the Pi at wake_time.

    On the real flight unit this actually powers off the Pi.
    In simulation (no PiSugar daemon), it falls back to time.sleep().
    """
    now = datetime.now(timezone.utc)
    sleep_s = (wake_time - now).total_seconds()

    if sleep_s <= 0:
        log.warning("Wake time already passed — skipping sleep.")
        return

    log.info(f"Scheduling deep sleep for {sleep_s/60:.1f} min until {wake_time.isoformat()}")

    schedule_rtc_wakeup(wake_time)

    # Check daemon is reachable before attempting shutdown
    test = _query_pisugar_daemon("get battery")
    if test:
        log.info("Issuing system shutdown — PiSugar RTC will wake at scheduled time.")
        time.sleep(2)  # Brief pause so logs flush
        subprocess.run(["sudo", "shutdown", "-h", "now"])
    else:
        # Dev/simulation: just sleep
        log.warning("PiSugar daemon not available — using time.sleep() fallback.")
        time.sleep(sleep_s)


# ─────────────────────────────────────────────
# SAFETY CHECKS
# ─────────────────────────────────────────────

def battery_is_healthy(min_percent: float = 15.0) -> bool:
    """
    Returns True if battery level is above the minimum safe threshold.
    Default 15% — below this, skip non-critical operations to protect the battery.
    """
    pct = get_battery_percent()
    if pct < min_percent:
        log.error(f"Battery critically low: {pct:.1f}% (threshold: {min_percent}%)")
        return False
    return True


def log_power_telemetry(orbit: int) -> dict:
    """Collect and return a full power status snapshot for the telemetry log."""
    status = get_full_power_status()
    log.info(
        f"[Orbit {orbit}] Power | "
        f"{status['battery_percent']:.1f}% | "
        f"{status['battery_voltage_v']:.3f}V | "
        f"{status['battery_wh']:.3f}Wh | "
        f"Charging: {status['is_charging']} | "
        f"Solar: {status['solar_connected']}"
    )
    return status


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Kaputnik Power Manager Test ===")
    status = get_full_power_status()
    for k, v in status.items():
        print(f"  {k:25s}: {v}")

    print(f"\n  Battery healthy (>15%): {battery_is_healthy()}")

    # Test RTC wakeup scheduling (does NOT actually shut down in test mode)
    wake = datetime.now(timezone.utc) + timedelta(minutes=5)
    print(f"\n  Scheduling test wakeup for {wake.isoformat()}")
    schedule_rtc_wakeup(wake)
