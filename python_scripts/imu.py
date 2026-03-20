"""
imu.py
======
Kaputnik IMU Driver
LSM6DSOX  – 6-axis accelerometer + gyroscope  (I²C addr: 0x6A)
LIS3MDL   – 3-axis magnetometer               (I²C addr: 0x1C)

Together they form the full 9-DOF (degrees of freedom) IMU suite.

Install dependencies:
    pip install adafruit-blinka adafruit-circuitpython-lsm6ds adafruit-circuitpython-lis3mdl
"""

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("kaputnik.imu")

# ── Try importing real hardware libs ──────────────────────────────────────────
try:
    import board
    import busio
    from adafruit_lsm6ds.lsm6dsox import LSM6DSOX
    import adafruit_lis3mdl
    IMU_AVAILABLE = True
except (ImportError, NotImplementedError):
    IMU_AVAILABLE = False
    log.warning("IMU libraries not found or no I²C bus — running in simulation mode.")

# ── Sensor instances (initialised once in init_imu) ───────────────────────────
_lsm6dsox = None   # Accel + Gyro
_lis3mdl  = None   # Magnetometer

# ─────────────────────────────────────────────
# INITIALISATION
# ─────────────────────────────────────────────

def init_imu() -> bool:
    """
    Initialise both IMU sensors over I²C.
    Returns True if successful, False if hardware unavailable.
    Call once at mission startup.
    """
    global _lsm6dsox, _lis3mdl

    if not IMU_AVAILABLE:
        log.warning("IMU not initialised (libraries unavailable).")
        return False

    try:
        i2c = busio.I2C(board.SCL, board.SDA)

        # LSM6DSOX: accelerometer + gyroscope (default I²C addr 0x6A)
        _lsm6dsox = LSM6DSOX(i2c)
        log.info("LSM6DSOX (accel/gyro) initialised at I²C 0x6A.")

        # LIS3MDL: magnetometer (default I²C addr 0x1C)
        _lis3mdl = adafruit_lis3mdl.LIS3MDL(i2c)
        log.info("LIS3MDL (magnetometer) initialised at I²C 0x1C.")

        return True

    except Exception as e:
        log.error(f"IMU initialisation failed: {e}")
        log.error("Check wiring: SDA→GPIO2, SCL→GPIO3, 3.3V, GND.")
        log.error("Also verify with: i2cdetect -y 1  (should show 0x6A and 0x1C)")
        return False

# ─────────────────────────────────────────────
# RAW SENSOR READS
# ─────────────────────────────────────────────

def read_accelerometer() -> tuple[float, float, float]:
    """
    Read linear acceleration from LSM6DSOX.
    Returns (ax, ay, az) in m/s².
    """
    if _lsm6dsox:
        try:
            return _lsm6dsox.acceleration  # (x, y, z) m/s²
        except Exception as e:
            log.warning(f"Accelerometer read failed: {e}")
    # Simulation: return 1g on Z axis (resting flat)
    return (0.0, 0.0, 9.81)


def read_gyroscope() -> tuple[float, float, float]:
    """
    Read angular velocity from LSM6DSOX.
    Returns (gx, gy, gz) in radians/s.
    """
    if _lsm6dsox:
        try:
            return _lsm6dsox.gyro  # (x, y, z) rad/s
        except Exception as e:
            log.warning(f"Gyroscope read failed: {e}")
    return (0.0, 0.0, 0.0)


def read_magnetometer() -> tuple[float, float, float]:
    """
    Read magnetic field from LIS3MDL.
    Returns (mx, my, mz) in microteslas (µT).
    """
    if _lis3mdl:
        try:
            return _lis3mdl.magnetic  # (x, y, z) µT
        except Exception as e:
            log.warning(f"Magnetometer read failed: {e}")
    # Simulation: approximate lunar surface field (~0 — Moon has no global field)
    return (2.1, -1.4, 0.8)


def read_imu_temperature() -> float:
    """
    Read die temperature from LSM6DSOX (useful for thermal monitoring in space).
    Returns temperature in °C.
    """
    if _lsm6dsox:
        try:
            return _lsm6dsox.temperature
        except Exception as e:
            log.warning(f"IMU temperature read failed: {e}")
    return -40.0  # Simulation default

# ─────────────────────────────────────────────
# DERIVED QUANTITIES
# ─────────────────────────────────────────────

def compute_attitude(ax: float, ay: float, az: float) -> dict[str, float]:
    """
    Estimate roll and pitch angles from accelerometer data.
    Uses tilt sensing (valid when satellite is not under significant linear acceleration).
    Returns angles in degrees.
    """
    try:
        roll  = math.degrees(math.atan2(ay, az))
        pitch = math.degrees(math.atan2(-ax, math.sqrt(ay**2 + az**2)))
    except Exception:
        roll, pitch = 0.0, 0.0
    return {"roll_deg": round(roll, 2), "pitch_deg": round(pitch, 2)}


def compute_heading(mx: float, my: float, roll_deg: float, pitch_deg: float) -> float:
    """
    Compute tilt-compensated magnetic heading in degrees (0–360).
    Useful for orientation tracking relative to lunar surface features.
    """
    try:
        roll_r  = math.radians(roll_deg)
        pitch_r = math.radians(pitch_deg)
        mx2 = mx * math.cos(pitch_r) + my * math.sin(roll_r) * math.sin(pitch_r)
        my2 = my * math.cos(roll_r)
        heading = math.degrees(math.atan2(-my2, mx2))
        if heading < 0:
            heading += 360
        return round(heading, 1)
    except Exception:
        return 0.0


def compute_magnitude(x: float, y: float, z: float) -> float:
    """Compute vector magnitude of any 3-axis reading."""
    return round(math.sqrt(x**2 + y**2 + z**2), 4)

# ─────────────────────────────────────────────
# FULL IMU SNAPSHOT
# ─────────────────────────────────────────────

def read_full_imu() -> dict:
    """
    Read all 9 DOF axes + derived attitude.
    Returns a complete IMU snapshot dictionary ready for telemetry/logging.

    Fields:
        accel_*     m/s²      — linear acceleration (LSM6DSOX)
        gyro_*      rad/s     — angular velocity    (LSM6DSOX)
        mag_*       µT        — magnetic field      (LIS3MDL)
        accel_mag   m/s²      — total acceleration magnitude
        gyro_mag    rad/s     — total rotation rate magnitude
        mag_mag     µT        — total magnetic field magnitude
        roll_deg    °         — estimated roll  (from accel)
        pitch_deg   °         — estimated pitch (from accel)
        heading_deg °         — tilt-compensated magnetic heading
        imu_temp_c  °C        — IMU die temperature
        timestamp             — UTC ISO timestamp
    """
    ax, ay, az = read_accelerometer()
    gx, gy, gz = read_gyroscope()
    mx, my, mz = read_magnetometer()
    attitude    = compute_attitude(ax, ay, az)
    heading     = compute_heading(mx, my, attitude["roll_deg"], attitude["pitch_deg"])

    return {
        # Raw 9-DOF
        "accel_x_ms2"   : round(ax, 4),
        "accel_y_ms2"   : round(ay, 4),
        "accel_z_ms2"   : round(az, 4),
        "gyro_x_rads"   : round(gx, 6),
        "gyro_y_rads"   : round(gy, 6),
        "gyro_z_rads"   : round(gz, 6),
        "mag_x_uT"      : round(mx, 3),
        "mag_y_uT"      : round(my, 3),
        "mag_z_uT"      : round(mz, 3),
        # Magnitudes
        "accel_mag_ms2" : compute_magnitude(ax, ay, az),
        "gyro_mag_rads" : compute_magnitude(gx, gy, gz),
        "mag_mag_uT"    : compute_magnitude(mx, my, mz),
        # Derived attitude
        "roll_deg"      : attitude["roll_deg"],
        "pitch_deg"     : attitude["pitch_deg"],
        "heading_deg"   : heading,
        # IMU die temperature
        "imu_temp_c"    : round(read_imu_temperature(), 2),
        # Timestamp
        "timestamp"     : datetime.now(timezone.utc).isoformat(),
    }


def check_imu_anomaly(imu_data: dict) -> list[str]:
    """
    Basic sanity checks on IMU data to flag anomalies in the mission log.
    Returns a list of warning strings (empty if all OK).
    """
    warnings = []

    # Total acceleration should be close to 0 in freefall (orbital) or ~9.81 on ground
    accel_mag = imu_data.get("accel_mag_ms2", 0)
    if accel_mag > 50.0:
        warnings.append(f"HIGH ACCELERATION: {accel_mag:.2f} m/s² — possible impact or anomaly")

    # Excessive rotation rate
    gyro_mag = imu_data.get("gyro_mag_rads", 0)
    if gyro_mag > 1.0:
        warnings.append(f"HIGH ROTATION RATE: {gyro_mag:.4f} rad/s — satellite may be tumbling")

    # IMU temperature bounds (operating range of LSM6DSOX: -40°C to +85°C)
    imu_temp = imu_data.get("imu_temp_c", 0)
    if imu_temp > 80.0:
        warnings.append(f"IMU OVERTEMP: {imu_temp:.1f}°C (limit: 85°C)")
    elif imu_temp < -35.0:
        warnings.append(f"IMU UNDERTEMP: {imu_temp:.1f}°C (limit: -40°C)")

    return warnings


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== Kaputnik IMU Test ===")
    init_imu()
    data = read_full_imu()
    for k, v in data.items():
        print(f"  {k:20s}: {v}")
    anomalies = check_imu_anomaly(data)
    if anomalies:
        print("\nAnomalies detected:")
        for w in anomalies:
            print(f"  ⚠ {w}")
    else:
        print("\nAll IMU checks nominal.")
