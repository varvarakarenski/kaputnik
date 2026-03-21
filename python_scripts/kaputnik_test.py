"""
kaputnik_test.py
================
Kaputnik Full Mission Test
Runs the complete pipeline in ~30 seconds instead of 700 hours.

Differences from real mission:
  - 2 orbits instead of 383
  - 2 images per orbit instead of 20
  - 10 second comparison window instead of 708.55 hours
  - 12 second pause between orbits instead of deep sleep
  - No real shutdown/RTC wakeup

Everything else is identical — same imports, same git push,
same telemetry, same IMU, same logging, same downlink.

Run from python_scripts/:
    cd /home/kaputnik/kaputnik_repo/python_scripts
    python3 kaputnik_test.py
"""

import os, time, shutil, logging, json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from PIL import Image
import numpy as np
import imufusion
import board
from adafruit_lsm6ds.lsm6dsox import LSM6DSOX
from adafruit_lis3mdl import LIS3MDL
from git import Repo

from power_manager  import battery_is_healthy, log_power_telemetry, get_full_power_status, sync_time_from_rtc
from mission_logger import (
    init_mission_logger,
    log_mission_start, log_mission_complete,
    log_orbit_start,   log_orbit_end,
    log_health,        log_imu,
    log_capture_start, log_image_saved, log_capture_complete,
    log_diff_result,   log_no_reference_found,
    log_downlink_start, log_downlink_complete,
    log_sleep,         log_wakeup,
    log_error,         log_warning, log_low_battery_skip,
    get_log_paths,
)

try:
    from picamera2 import Picamera2
    CAMERA_AVAILABLE = True
except ImportError:
    CAMERA_AVAILABLE = False

# ─────────────────────────────────────────────
# PATHS  (same as main — uses real repo folders)
# ─────────────────────────────────────────────
REPO_PATH     = Path("/home/kaputnik/kaputnik_repo")
IMAGES_DIR    = REPO_PATH / "kaputnik_images"
LOGS_DIR      = REPO_PATH / "Logs"
STORAGE_DIR   = REPO_PATH / "storage" / "test_run"   # separate test storage
PENDING_DIR   = STORAGE_DIR / "pending"
ARCHIVE_DIR   = STORAGE_DIR / "archive"
TELEMETRY_DIR = STORAGE_DIR / "telemetry"
LOG_FILE      = STORAGE_DIR / "kaputnik_test.log"
BASE_DIR      = REPO_PATH

# ─────────────────────────────────────────────
# TEST PARAMETERS
# ─────────────────────────────────────────────
TEST_TOTAL_ORBITS     = 2
TEST_IMAGES_PER_ORBIT = 2
TEST_COMPARISON_SECS  = 10
TEST_COMPARISON_TOL_S = 30
TEST_ORBIT_PAUSE_S    = 12
MIN_BATTERY_PCT       = 15.0



# ─────────────────────────────────────────────
# TEST RESULT TRACKER
# ─────────────────────────────────────────────
class TestResults:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.warned = []

    def ok(self, name, detail=""):
        self.passed.append((name, detail))
        print(f"  ✓  {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name, detail=""):
        self.failed.append((name, detail))
        print(f"  ✗  {name}" + (f" — {detail}" if detail else ""))

    def warn(self, name, detail=""):
        self.warned.append((name, detail))
        print(f"  ⚠  {name}" + (f" — {detail}" if detail else ""))

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print("\n" + "═" * 55)
        print("  TEST SUMMARY")
        print("═" * 55)
        print(f"  Passed  : {len(self.passed)}/{total}")
        print(f"  Failed  : {len(self.failed)}")
        print(f"  Warnings: {len(self.warned)}")
        if self.failed:
            print("\n  Failed checks:")
            for name, detail in self.failed:
                print(f"    ✗ {name}: {detail}")
        if self.warned:
            print("\n  Warnings:")
            for name, detail in self.warned:
                print(f"    ⚠ {name}: {detail}")
        print("═" * 55)
        if not self.failed:
            print("  ✓ ALL TESTS PASSED — mission pipeline is functional")
        else:
            print("  ✗ SOME TESTS FAILED — review output above")
        print("═" * 55)

results = TestResults()

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────
def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
    )

log = logging.getLogger("kaputnik.test")

def init_storage():
    for d in (PENDING_DIR, ARCHIVE_DIR, TELEMETRY_DIR, IMAGES_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    results.ok("Storage folders created")

# ─────────────────────────────────────────────
# IMU
# ─────────────────────────────────────────────
_sox = _lis3 = _ahrs = _last_time = None

def init_imu():
    global _sox, _lis3, _ahrs, _last_time
    try:
        i2c    = board.I2C()
        _sox   = LSM6DSOX(i2c)
        _lis3  = LIS3MDL(i2c)
        _ahrs  = imufusion.Ahrs()
        _last_time = time.monotonic()
        results.ok("IMU initialised", "LSM6DSOX + LIS3MDL + imufusion")
        return True
    except Exception as e:
        results.warn("IMU init", f"hardware not found ({e}) — using simulation")
        return False

def read_full_imu() -> dict:
    global _last_time
    ts = datetime.now(timezone.utc).isoformat()
    if _sox and _lis3 and _ahrs:
        try:
            current_time = time.monotonic()
            dt    = current_time - (_last_time or current_time)
            _last_time = current_time
            accel = np.array(_sox.acceleration)
            gyro  = np.array([d * 57.2958 for d in _sox.gyro])
            mag   = np.array(_lis3.magnetic)
            _ahrs.update(gyro, accel, mag, dt)
            euler = _ahrs.quaternion.to_euler()
            return {
                "accel_x_ms2": round(float(accel[0]), 4),
                "accel_y_ms2": round(float(accel[1]), 4),
                "accel_z_ms2": round(float(accel[2]), 4),
                "gyro_x_degs": round(float(gyro[0]),  4),
                "gyro_y_degs": round(float(gyro[1]),  4),
                "gyro_z_degs": round(float(gyro[2]),  4),
                "mag_x_uT"   : round(float(mag[0]),   3),
                "mag_y_uT"   : round(float(mag[1]),   3),
                "mag_z_uT"   : round(float(mag[2]),   3),
                "roll_deg"   : round(float(euler[0]),  2),
                "pitch_deg"  : round(float(euler[1]),  2),
                "yaw_deg"    : round(float(euler[2]),  2),
                "timestamp"  : ts,
            }
        except Exception as e:
            log.warning(f"IMU read failed: {e}")
    return {
        "accel_x_ms2": 0.0, "accel_y_ms2": 0.0, "accel_z_ms2": 9.81,
        "gyro_x_degs": 0.0, "gyro_y_degs": 0.0, "gyro_z_degs": 0.0,
        "mag_x_uT"   : 2.1, "mag_y_uT"   :-1.4, "mag_z_uT"   : 0.8,
        "roll_deg"   : 0.0, "pitch_deg"  : 0.0, "yaw_deg"    : 0.0,
        "timestamp"  : ts,
    }

def check_imu_anomaly(imu_data):
    warnings = []
    accel_mag = (imu_data["accel_x_ms2"]**2 +
                 imu_data["accel_y_ms2"]**2 +
                 imu_data["accel_z_ms2"]**2) ** 0.5
    if accel_mag > 50.0:
        warnings.append(f"HIGH ACCELERATION: {accel_mag:.2f} m/s²")
    return warnings

# ─────────────────────────────────────────────
# TELEMETRY
# ─────────────────────────────────────────────
def read_cpu_temperature():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read()) / 1000.0
    except Exception:
        return -40.0

def collect_telemetry(orbit):
    try:
        power     = log_power_telemetry(orbit)
        imu_data  = read_full_imu()
        anomalies = check_imu_anomaly(imu_data)
        cpu_temp  = read_cpu_temperature()
        log_health(orbit, power, cpu_temp)
        log_imu(orbit, imu_data, anomalies)
        telemetry = {
            "orbit": orbit, "timestamp": datetime.now(timezone.utc).isoformat(),
            "power": power, "imu": imu_data, "cpu_temp_c": cpu_temp,
            "imu_anomalies": anomalies,
        }
        tfile = TELEMETRY_DIR / f"telemetry_orbit_{orbit:04d}.json"
        with open(tfile, "w") as f:
            json.dump(telemetry, f, indent=2)
        results.ok(f"Orbit {orbit} telemetry",
                   f"batt={power.get('battery_percent',0):.1f}%  temp={cpu_temp:.1f}°C")
        return telemetry
    except Exception as e:
        results.fail(f"Orbit {orbit} telemetry", str(e))
        return {"orbit": orbit, "timestamp": datetime.now(timezone.utc).isoformat()}

# ─────────────────────────────────────────────
# IMAGE SUBTRACTION
# ─────────────────────────────────────────────
def subtract_images(image_path1, image_path2, output_path):
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
def capture_images(orbit):
    saved = []
    ts_base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    log_capture_start(orbit, TEST_IMAGES_PER_ORBIT)

    if CAMERA_AVAILABLE:
        try:
            cam = Picamera2()
            cam.configure(cam.create_still_configuration(main={"size": (1920, 1080)}))
            cam.start(); time.sleep(2)
            for i in range(TEST_IMAGES_PER_ORBIT):
                fname = PENDING_DIR / f"kaputnik_o{orbit:04d}_{ts_base}_{i:02d}.jpg"
                cam.capture_file(str(fname))
                saved.append(fname)
                log_image_saved(orbit, i, fname.name, fname.stat().st_size)
                time.sleep(0.5)
            cam.stop(); cam.close()
            results.ok(f"Orbit {orbit} camera", f"{len(saved)} real images captured")
        except Exception as e:
            results.fail(f"Orbit {orbit} camera", str(e))
    else:
        for i in range(TEST_IMAGES_PER_ORBIT):
            fname = PENDING_DIR / f"kaputnik_o{orbit:04d}_{ts_base}_{i:02d}.jpg"
            brightness = 100 + (orbit - 1) * 40
            img = Image.new("RGB", (320, 240), color=(brightness, brightness // 2, 50))
            pixels = img.load()
            for x in range(50 + orbit * 20, 80 + orbit * 20):
                for y in range(50, 80):
                    pixels[x, y] = (255, 255, 255)
            img.save(str(fname))
            saved.append(fname)
            log_image_saved(orbit, i, fname.name, fname.stat().st_size)
        results.ok(f"Orbit {orbit} camera (simulated)", f"{len(saved)} images")

    log_capture_complete(orbit, len(saved), 0)
    return saved

# ─────────────────────────────────────────────
# CHANGE DETECTION
# ─────────────────────────────────────────────
def find_comparison_image_test(new_image_path):
    try:
        img_idx = new_image_path.stem.split("_")[-1]
    except Exception:
        return None
    for archived in sorted(ARCHIVE_DIR.glob(f"*_{img_idx}.jpg")):
        try:
            old_dt = datetime.strptime(archived.stem.split("_")[2], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            new_dt = datetime.strptime(new_image_path.stem.split("_")[2], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            delta  = (new_dt - old_dt).total_seconds()
            if 0 < delta <= (TEST_COMPARISON_SECS + TEST_COMPARISON_TOL_S):
                return archived
        except Exception:
            continue
    return None

def run_change_detection(orbit, new_images):
    diffs = []
    for img_path in new_images:
        match = find_comparison_image_test(img_path)
        if not match:
            log_no_reference_found(orbit, img_path.name)
            if orbit == 2:
                results.warn(f"Orbit {orbit} change detection", "no reference found — check timestamp gap")
            continue
        diff_path = ARCHIVE_DIR / f"diff_{img_path.stem}_vs_{match.stem}.jpg"
        try:
            diff_img  = subtract_images(str(img_path), str(match), str(diff_path))
            mean_diff = float(np.array(diff_img).mean())
            changed   = mean_diff > 5.0
            log_diff_result(orbit, img_path.name, match.name,
                            diff_path.name, changed, diff_path.stat().st_size / 1024)
            diffs.append(diff_path)
            results.ok(f"Orbit {orbit} change detection",
                       f"mean_diff={mean_diff:.1f}/255  changed={changed}")
        except Exception as e:
            results.fail(f"Orbit {orbit} change detection", str(e))
    return diffs

# ─────────────────────────────────────────────
# GITHUB PUSH
# ─────────────────────────────────────────────
def git_push(orbit):
    try:
        # Copy ALL images and diffs from archive → kaputnik_images/
        # Overwrite every time so latest version is always in the repo
        copied = 0
        for img in sorted(ARCHIVE_DIR.glob("*.jpg")):
            dest = IMAGES_DIR / img.name
            shutil.copy2(str(img), str(dest))
            copied += 1
            log.info(f"  -> kaputnik_images/: {img.name}")

        # Safety net: also copy any diffs still in pending
        for img in sorted(PENDING_DIR.glob("diff_*.jpg")):
            dest = IMAGES_DIR / img.name
            shutil.copy2(str(img), str(dest))
            copied += 1
            log.info(f"  -> kaputnik_images/ (from pending): {img.name}")

        # Copy logs → Logs/
        for lpath in get_log_paths().values():
            if lpath and Path(lpath).exists():
                shutil.copy2(lpath, str(LOGS_DIR / Path(lpath).name))

        repo   = Repo(str(REPO_PATH))
        origin = repo.remote("origin")
        origin.pull()
        repo.git.add(str(IMAGES_DIR))
        repo.git.add(str(LOGS_DIR))
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        diffs = list(IMAGES_DIR.glob("diff_*.jpg"))
        repo.index.commit(
            f"TEST Orbit {orbit:04d} | {timestamp} | "
            f"{copied} images | {len(diffs)} diff(s)"
        )
        origin.push()
        results.ok(f"Orbit {orbit} GitHub push",
                   f"{copied} image(s), {len(diffs)} diff(s) -> kaputnik_images/")
    except Exception as e:
        results.warn(f"Orbit {orbit} GitHub push", str(e))

# ─────────────────────────────────────────────
# DOWNLINK
# ─────────────────────────────────────────────
def downlink_phase(orbit, telemetry):
    """Move pending images to archive then push to GitHub."""
    pending    = sorted(PENDING_DIR.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    pending_mb = sum(p.stat().st_size for p in pending) / 1024**2
    log_downlink_start(orbit, len(pending), pending_mb)

    moved = 0
    for img in pending:
        shutil.move(str(img), str(ARCHIVE_DIR / img.name))
        moved += 1

    log_downlink_complete(orbit, moved, pending_mb, 0, True)
    results.ok(f"Orbit {orbit} downlink", f"{moved} images archived")
    git_push(orbit)

# ─────────────────────────────────────────────
# VERIFICATION
# ─────────────────────────────────────────────
def verify_outputs():
    print("\n── Verifying outputs ───────────────────────────")

    for folder in (PENDING_DIR, ARCHIVE_DIR, TELEMETRY_DIR, IMAGES_DIR, LOGS_DIR):
        if folder.exists():
            results.ok(f"Folder exists: {folder.relative_to(REPO_PATH)}")
        else:
            results.fail(f"Folder missing: {folder.relative_to(REPO_PATH)}")

    for orbit in range(1, TEST_TOTAL_ORBITS + 1):
        tfile = TELEMETRY_DIR / f"telemetry_orbit_{orbit:04d}.json"
        if tfile.exists():
            results.ok(f"Telemetry orbit {orbit}", f"{tfile.stat().st_size} bytes")
        else:
            results.fail(f"Telemetry orbit {orbit} missing")

    archived = list(ARCHIVE_DIR.glob("kaputnik_o0001_*.jpg"))
    if archived:
        results.ok("Orbit 1 images archived", f"{len(archived)} files")
    else:
        results.fail("No orbit 1 images in archive")

    diffs = list(ARCHIVE_DIR.glob("diff_*.jpg")) + list(PENDING_DIR.glob("diff_*.jpg"))
    if diffs:
        results.ok("Diff images generated", f"{len(diffs)} file(s)")
    else:
        results.warn("No diff images found", "change detection may not have matched")

    images_in_repo = list(IMAGES_DIR.glob("*.jpg"))
    if images_in_repo:
        results.ok("kaputnik_images/ populated", f"{len(images_in_repo)} files")
    else:
        results.fail("kaputnik_images/ is empty")

    logs_in_repo = list(LOGS_DIR.glob("*.log")) + list(LOGS_DIR.glob("*.jsonl"))
    if logs_in_repo:
        results.ok("Logs/ populated", f"{len(logs_in_repo)} files")
    else:
        results.fail("Logs/ is empty")

    for lpath in get_log_paths().values():
        if lpath and Path(lpath).exists():
            results.ok(f"Log file: {Path(lpath).name}", f"{Path(lpath).stat().st_size} bytes")
        else:
            results.fail(f"Log file missing: {lpath}")

    state_file = STORAGE_DIR / "mission_state.json"
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
        results.ok("mission_state.json", f"next orbit = {state.get('orbit')}")
    else:
        results.fail("mission_state.json missing")

# ─────────────────────────────────────────────
# MAIN TEST RUNNER
# ─────────────────────────────────────────────
def run_test():
    setup_logging()
    init_storage()

    print("\n" + "═" * 55)
    print("  KAPUTNIK MISSION TEST")
    print(f"  {TEST_TOTAL_ORBITS} orbits | {TEST_IMAGES_PER_ORBIT} images/orbit | {TEST_COMPARISON_SECS}s window")
    print("═" * 55)

    init_mission_logger(BASE_DIR, orbit=1)
    log_mission_start(TEST_TOTAL_ORBITS, TEST_TOTAL_ORBITS)
    results.ok("Mission logger initialised")

    try:
        sync_time_from_rtc()
        results.ok("RTC clock sync")
    except Exception as e:
        results.warn("RTC clock sync", str(e))

    init_imu()

    state_file = STORAGE_DIR / "mission_state.json"
    state = {"orbit": 1, "mission_start": time.time()}
    with open(state_file, "w") as f:
        json.dump(state, f)

    for orbit in range(1, TEST_TOTAL_ORBITS + 1):
        orbit_start_dt = datetime.now(timezone.utc)
        orbit_start_ts = time.time()

        print(f"\n── Orbit {orbit}/{TEST_TOTAL_ORBITS} {'─' * 40}")
        log_orbit_start(orbit, TEST_TOTAL_ORBITS, orbit_start_dt.isoformat())

        power_status = get_full_power_status()
        batt = power_status.get("battery_percent", 0)

        if not battery_is_healthy(MIN_BATTERY_PCT):
            log_low_battery_skip(orbit, batt, MIN_BATTERY_PCT)
            results.warn(f"Orbit {orbit} battery", f"{batt:.1f}% below threshold")
        else:
            results.ok(f"Orbit {orbit} battery check", f"{batt:.1f}%")
            telemetry = collect_telemetry(orbit)

            if orbit == 1:
                print("  [INFO] Orbit 1 — no archive yet, change detection will find nothing (expected)")
            new_images = capture_images(orbit)
            run_change_detection(orbit, new_images)
            downlink_phase(orbit, telemetry)

        orbit_duration = time.time() - orbit_start_ts
        log_orbit_end(orbit, orbit_duration)
        state["orbit"] = orbit + 1
        with open(state_file, "w") as f:
            json.dump(state, f)

        if orbit < TEST_TOTAL_ORBITS:
            print(f"\n  Pausing {TEST_ORBIT_PAUSE_S}s before orbit {orbit + 1}...")
            log_sleep(orbit, TEST_ORBIT_PAUSE_S, "N/A (test mode)")
            time.sleep(TEST_ORBIT_PAUSE_S)
            log_wakeup(orbit + 1)

    log_mission_complete(TEST_TOTAL_ORBITS)
    verify_outputs()
    results.summary()

    print(f"\n  Repo:   {REPO_PATH}")
    print(f"  Images: {IMAGES_DIR}")
    print(f"  Logs:   {LOGS_DIR}\n")


if __name__ == "__main__":
    run_test()
