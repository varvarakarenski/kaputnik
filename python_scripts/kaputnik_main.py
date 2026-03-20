"""
kaputnik_main.py
================
Kaputnik CubeSat – Lunar Surface Change Monitor
Hardware: RPi 4 | PiSugar 3 | DFRobot DFR0559 | Pi Camera | LSM6DSOX + LIS3MDL IMU

Folder structure:
  /home/kaputnik/kaputnik_repo/
  ├── python_scripts/     ← all code lives here
  ├── kaputnik_images/    ← mission images pushed to GitHub here
  ├── Logs/               ← mission logs pushed to GitHub here
  └── storage/            ← runtime data (pending, archive, telemetry)
"""

import os, time, shutil, logging, json, socket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from PIL import Image
import numpy as np
import imufusion
import board
from adafruit_lsm6ds.lsm6dsox import LSM6DSOX
from adafruit_lis3mdl import LIS3MDL
from git import Repo
from picamera2 import Picamera2

from power_manager  import battery_is_healthy, log_power_telemetry, deep_sleep_until, sync_time_from_rtc, get_full_power_status
from mission_logger import (
    init_mission_logger,
    log_mission_start, log_mission_complete,
    log_orbit_start,   log_orbit_end,
    log_health,        log_imu,
    log_capture_start, log_image_saved, log_capture_complete,
    log_diff_result,   log_no_reference_found,
    log_downlink_start, log_downlink_tx, log_downlink_cap, log_downlink_complete,
    log_sleep,         log_wakeup,
    log_error,         log_warning, log_low_battery_skip,
    get_log_paths,
)

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
REPO_PATH     = Path("/home/kaputnik/kaputnik_repo")
IMAGES_DIR    = REPO_PATH / "kaputnik_images"
LOGS_DIR      = REPO_PATH / "Logs"
STORAGE_DIR   = REPO_PATH / "storage"
PENDING_DIR   = STORAGE_DIR / "pending"
ARCHIVE_DIR   = STORAGE_DIR / "archive"
TELEMETRY_DIR = STORAGE_DIR / "telemetry"
LOG_FILE      = STORAGE_DIR / "kaputnik.log"
BASE_DIR      = REPO_PATH   # used by mission_logger

# ─────────────────────────────────────────────
# MISSION CONSTANTS
# ─────────────────────────────────────────────
TOTAL_ORBITS        = 383
CAPTURE_PHASE_END   = 375
IMAGES_PER_ORBIT    = 20
MAX_DOWNLINK_BYTES  = 54 * 1024**2
ORBIT_PERIOD_S      = 111 * 60
SIMULATED_LATENCY_S = 1.3
COMPARISON_HOURS    = 708.55
MIN_BATTERY_PCT     = 15.0

GROUND_STATION_IP   = "192.168.1.100"   # ← your laptop's IP
GROUND_STATION_PORT = 5005

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )

log = logging.getLogger("kaputnik")

def init_storage():
    for d in (PENDING_DIR, ARCHIVE_DIR, TELEMETRY_DIR, IMAGES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────
# IMU  (uses imufusion Kalman filter)
# ─────────────────────────────────────────────
_i2c        = None
_sox        = None   # LSM6DSOX — accel + gyro
_lis3       = None   # LIS3MDL  — magnetometer
_ahrs       = None   # imufusion AHRS filter
_last_time  = None

def init_imu() -> bool:
    global _i2c, _sox, _lis3, _ahrs, _last_time
    try:
        _i2c   = board.I2C()
        _sox   = LSM6DSOX(_i2c)
        _lis3  = LIS3MDL(_i2c)
        _ahrs  = imufusion.Ahrs()
        _last_time = time.monotonic()
        log.info("IMU initialised — LSM6DSOX + LIS3MDL + imufusion AHRS")
        return True
    except Exception as e:
        log.warning(f"IMU init failed: {e} — using simulation values")
        return False

def read_full_imu() -> dict:
    global _last_time
    ts = datetime.now(timezone.utc).isoformat()

    if _sox and _lis3 and _ahrs:
        try:
            current_time = time.monotonic()
            dt = current_time - (_last_time or current_time)
            _last_time = current_time

            accel = np.array(_sox.acceleration)          # m/s²
            gyro  = np.array([d * 57.2958 for d in _sox.gyro])  # rad/s → deg/s
            mag   = np.array(_lis3.magnetic)             # µT

            _ahrs.update(gyro, accel, mag, dt)
            euler = _ahrs.quaternion.to_euler()           # [roll, pitch, yaw] degrees

            return {
                "accel_x_ms2"  : round(float(accel[0]), 4),
                "accel_y_ms2"  : round(float(accel[1]), 4),
                "accel_z_ms2"  : round(float(accel[2]), 4),
                "gyro_x_degs"  : round(float(gyro[0]),  4),
                "gyro_y_degs"  : round(float(gyro[1]),  4),
                "gyro_z_degs"  : round(float(gyro[2]),  4),
                "mag_x_uT"     : round(float(mag[0]),   3),
                "mag_y_uT"     : round(float(mag[1]),   3),
                "mag_z_uT"     : round(float(mag[2]),   3),
                "roll_deg"     : round(float(euler[0]),  2),
                "pitch_deg"    : round(float(euler[1]),  2),
                "yaw_deg"      : round(float(euler[2]),  2),
                "timestamp"    : ts,
            }
        except Exception as e:
            log.warning(f"IMU read failed: {e}")

    # Simulation fallback
    return {
        "accel_x_ms2": 0.0, "accel_y_ms2": 0.0, "accel_z_ms2": 9.81,
        "gyro_x_degs": 0.0, "gyro_y_degs": 0.0, "gyro_z_degs": 0.0,
        "mag_x_uT"   : 2.1, "mag_y_uT"   :-1.4, "mag_z_uT"   : 0.8,
        "roll_deg"   : 0.0, "pitch_deg"  : 0.0, "yaw_deg"    : 0.0,
        "timestamp"  : ts,
    }

def check_imu_anomaly(imu_data: dict) -> list[str]:
    warnings = []
    accel_mag = (imu_data["accel_x_ms2"]**2 +
                 imu_data["accel_y_ms2"]**2 +
                 imu_data["accel_z_ms2"]**2) ** 0.5
    gyro_mag  = (imu_data["gyro_x_degs"]**2 +
                 imu_data["gyro_y_degs"]**2 +
                 imu_data["gyro_z_degs"]**2) ** 0.5
    if accel_mag > 50.0:
        warnings.append(f"HIGH ACCELERATION: {accel_mag:.2f} m/s²")
    if gyro_mag > 57.3:   # >1 rad/s in deg/s
        warnings.append(f"HIGH ROTATION: {gyro_mag:.2f} deg/s — possible tumble")
    return warnings

# ─────────────────────────────────────────────
# TELEMETRY
# ─────────────────────────────────────────────
def read_cpu_temperature() -> float:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000.0
    except Exception:
        return -40.0

def collect_telemetry(orbit: int) -> dict:
    power     = log_power_telemetry(orbit)
    imu_data  = read_full_imu()
    anomalies = check_imu_anomaly(imu_data)
    cpu_temp  = read_cpu_temperature()

    log_health(orbit, power, cpu_temp)
    log_imu(orbit, imu_data, anomalies)

    telemetry = {
        "orbit"        : orbit,
        "timestamp"    : datetime.now(timezone.utc).isoformat(),
        "power"        : power,
        "imu"          : imu_data,
        "cpu_temp_c"   : cpu_temp,
        "imu_anomalies": anomalies,
    }

    tfile = TELEMETRY_DIR / f"telemetry_orbit_{orbit:04d}.json"
    with open(tfile, "w") as f:
        json.dump(telemetry, f, indent=2)

    return telemetry

# ─────────────────────────────────────────────
# IMAGE SUBTRACTION
# ─────────────────────────────────────────────
def subtract_images(image_path1: str, image_path2: str, output_path: str) -> Image.Image:
    img1 = Image.open(image_path1).convert("RGB")
    img2 = Image.open(image_path2).convert("RGB")
    if img1.size != img2.size:
        img2 = img2.resize(img1.size, Image.LANCZOS)
    arr1 = np.array(img1, dtype=np.int16)
    arr2 = np.array(img2, dtype=np.int16)
    diff = np.abs(arr1 - arr2).astype(np.uint8)
    diff_image = Image.fromarray(diff, mode="RGB").convert("L")
    if out_dir := os.path.dirname(output_path):
        os.makedirs(out_dir, exist_ok=True)
    diff_image.save(output_path)
    return diff_image

# ─────────────────────────────────────────────
# CAMERA
# ─────────────────────────────────────────────
def capture_images(orbit: int, count: int = IMAGES_PER_ORBIT) -> list[Path]:
    saved  = []
    failed = 0
    ts_base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_capture_start(orbit, count)

    try:
        cam = Picamera2()
        cam.configure(cam.create_still_configuration(main={"size": (4056, 3040)}))
        cam.start()
        time.sleep(2)
        for i in range(count):
            fname = PENDING_DIR / f"kaputnik_o{orbit:04d}_{ts_base}_{i:02d}.jpg"
            try:
                cam.capture_file(str(fname))
                saved.append(fname)
                log_image_saved(orbit, i, fname.name, fname.stat().st_size)
                time.sleep(0.5)
            except Exception as e:
                failed += 1
                log_error(orbit, "CAMERA", f"Image {i} failed", str(e))
        cam.stop()
        cam.close()
    except Exception as e:
        log_error(orbit, "CAMERA", "Camera init failed", str(e))

    log_capture_complete(orbit, len(saved), failed)
    return saved

# ─────────────────────────────────────────────
# CHANGE DETECTION
# ─────────────────────────────────────────────
def find_comparison_image(new_image_path: Path) -> Path | None:
    target_s  = COMPARISON_HOURS * 3600
    tolerance = 3600
    try:
        img_idx = new_image_path.stem.split("_")[-1]
    except Exception:
        return None
    for archived in sorted(ARCHIVE_DIR.glob(f"*_{img_idx}.jpg")):
        try:
            old_dt = datetime.strptime(archived.stem.split("_")[2], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            new_dt = datetime.strptime(new_image_path.stem.split("_")[2], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            if abs(abs((new_dt - old_dt).total_seconds()) - target_s) <= tolerance:
                return archived
        except Exception:
            continue
    return None

def run_change_detection(orbit: int, new_images: list[Path]) -> list[Path]:
    diffs = []
    for img_path in new_images:
        match = find_comparison_image(img_path)
        if not match:
            log_no_reference_found(orbit, img_path.name)
            continue
        diff_path = PENDING_DIR / f"diff_{img_path.stem}_vs_{match.stem}.jpg"
        try:
            diff_img  = subtract_images(str(img_path), str(match), str(diff_path))
            mean_diff = float(np.array(diff_img).mean())
            changed   = mean_diff > 5.0
            log_diff_result(orbit, img_path.name, match.name,
                            diff_path.name, changed, diff_path.stat().st_size / 1024)
            diffs.append(diff_path)
        except Exception as e:
            log_error(orbit, "DIFF", f"Change detection failed for {img_path.name}", str(e))
    return diffs

# ─────────────────────────────────────────────
# GITHUB PUSH  (using gitpython — same as flatsat.py)
# ─────────────────────────────────────────────
def git_push(orbit: int):
    """
    Copy new images and logs into repo folders then
    stage, commit, and push to GitHub.
    """
    try:
        # Copy archived images → kaputnik_images/
        copied = 0
        for img in sorted(ARCHIVE_DIR.glob("*.jpg")):
            dest = IMAGES_DIR / img.name
            if not dest.exists():
                shutil.copy2(str(img), str(dest))
                copied += 1

        # Copy logs → Logs/
        for lpath in get_log_paths().values():
            if lpath and Path(lpath).exists():
                shutil.copy2(lpath, str(LOGS_DIR / Path(lpath).name))

        # Copy telemetry into repo storage
        dst_tel = REPO_PATH / "storage" / "telemetry"
        dst_tel.mkdir(parents=True, exist_ok=True)
        for f in TELEMETRY_DIR.glob("*.json"):
            dest = dst_tel / f.name
            if not dest.exists():
                shutil.copy2(str(f), str(dest))

        # Git stage, commit, push
        repo      = Repo(str(REPO_PATH))
        origin    = repo.remote("origin")
        origin.pull()
        repo.git.add(str(IMAGES_DIR))
        repo.git.add(str(LOGS_DIR))
        repo.git.add(str(REPO_PATH / "storage"))
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo.index.commit(f"Orbit {orbit:04d}/383 | {timestamp} | auto-downlink | {copied} new images")
        origin.push()
        log.info(f"[Orbit {orbit}] GitHub push successful — {copied} new image(s)")

    except Exception as e:
        log.warning(f"[Orbit {orbit}] GitHub push failed: {e} — will retry next pass")

# ─────────────────────────────────────────────
# DOWNLINK
# ─────────────────────────────────────────────
def transmit_telemetry(orbit: int, telemetry: dict) -> bool:
    payload = json.dumps({"type": "telemetry", "data": telemetry}).encode()
    try:
        time.sleep(SIMULATED_LATENCY_S)
        with socket.create_connection((GROUND_STATION_IP, GROUND_STATION_PORT), timeout=10) as s:
            s.sendall(len(payload).to_bytes(4, "big") + payload)
        log.info(f"[Orbit {orbit}] Telemetry TX OK ({len(payload)} bytes)")
        return True
    except Exception as e:
        log_warning(orbit, "DOWNLINK", f"Telemetry TX failed: {e}")
        return False

def transmit_file(orbit: int, file_path: Path) -> tuple[bool, int]:
    try:
        data   = file_path.read_bytes()
        header = json.dumps({"type": "image", "filename": file_path.name,
                             "size": len(data)}).encode()
        time.sleep(SIMULATED_LATENCY_S)
        with socket.create_connection((GROUND_STATION_IP, GROUND_STATION_PORT), timeout=30) as s:
            s.sendall(len(header).to_bytes(4, "big") + header)
            s.sendall(data)
        return True, len(data)
    except Exception as e:
        log_warning(orbit, "DOWNLINK", f"File TX failed {file_path.name}: {e}")
        return False, 0

def downlink_phase(orbit: int, telemetry: dict):
    pending    = sorted(PENDING_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    pending_mb = sum(p.stat().st_size for p in pending) / 1024**2
    log_downlink_start(orbit, len(pending), pending_mb)

    telem_ok   = transmit_telemetry(orbit, telemetry)
    total_sent = 0
    sent = failed = 0

    for img in pending:
        if total_sent >= MAX_DOWNLINK_BYTES:
            log_downlink_cap(orbit, sent, total_sent / 1024**2, len(pending) - sent)
            break
        success, nbytes = transmit_file(orbit, img)
        log_downlink_tx(orbit, img.name, img.stat().st_size / 1024,
                        success, (total_sent + nbytes) / 1024**2)
        if success:
            shutil.move(str(img), str(ARCHIVE_DIR / img.name))
            total_sent += nbytes
            sent += 1
        else:
            failed += 1

    log_downlink_complete(orbit, sent, total_sent / 1024**2, failed, telem_ok)

    # Push everything to GitHub
    git_push(orbit)

# ─────────────────────────────────────────────
# MAIN MISSION LOOP
# ─────────────────────────────────────────────
def run_mission():
    setup_logging()
    init_storage()

    state_file = STORAGE_DIR / "mission_state.json"
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
        starting_orbit = state["orbit"]
    else:
        state = {"orbit": 1, "mission_start": time.time()}
        starting_orbit = 1

    init_mission_logger(BASE_DIR, orbit=starting_orbit)

    if starting_orbit == 1:
        log_mission_start(TOTAL_ORBITS, CAPTURE_PHASE_END)
    else:
        log_wakeup(starting_orbit)

    log.info(f"=== KAPUTNIK | Orbit {starting_orbit}/{TOTAL_ORBITS} ===")

    sync_time_from_rtc()
    imu_ok = init_imu()
    if not imu_ok:
        log_warning(starting_orbit, "IMU", "IMU init failed — using simulation values")

    for orbit in range(starting_orbit, TOTAL_ORBITS + 1):
        orbit_start_dt = datetime.now(timezone.utc)
        orbit_start_ts = time.time()

        log_orbit_start(orbit, TOTAL_ORBITS, orbit_start_dt.isoformat())

        power_status = get_full_power_status()
        if not battery_is_healthy(MIN_BATTERY_PCT):
            log_low_battery_skip(orbit, power_status.get("battery_percent", 0), MIN_BATTERY_PCT)
        else:
            telemetry = collect_telemetry(orbit)

            if orbit <= CAPTURE_PHASE_END:
                log.info(f"[Orbit {orbit}] CAPTURE PHASE")
                new_images = capture_images(orbit)
                run_change_detection(orbit, new_images)
            else:
                log.info(f"[Orbit {orbit}] DOWNLINK-ONLY. Camera OFF.")

            downlink_phase(orbit, telemetry)

        orbit_duration = time.time() - orbit_start_ts
        log_orbit_end(orbit, orbit_duration)
        state["orbit"] = orbit + 1
        with open(state_file, "w") as f:
            json.dump(state, f)

        wake_time = orbit_start_dt + timedelta(seconds=ORBIT_PERIOD_S)
        log_sleep(orbit, (wake_time - datetime.now(timezone.utc)).total_seconds(),
                  wake_time.isoformat())
        deep_sleep_until(wake_time)

    log_mission_complete(TOTAL_ORBITS)
    log.info("=== MISSION COMPLETE ===")


if __name__ == "__main__":
    run_mission()
