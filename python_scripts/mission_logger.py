"""
mission_logger.py
=================
Kaputnik Mission Event Logger

Produces two log streams:
  1. kaputnik_mission.log  — human-readable structured event log (primary)
  2. kaputnik_events.jsonl — machine-readable JSON Lines file (one JSON obj per line)
                             for post-mission analysis and downlink to ground station

Log levels / event types:
  BOOT        System wakeup and initialisation
  HEALTH      Battery, temperature, and power status
  IMU         IMU snapshot and anomaly flags
  CAPTURE     Camera events (start, each image saved, stop)
  DIFF        Change-detection results
  DOWNLINK    Transmission events (link open, file TX, link close, cap reached)
  SLEEP       Deep sleep scheduling
  ORBIT       Orbit boundary markers
  WARNING     Non-fatal issues
  ERROR       Failures requiring attention
  MISSION     Top-level mission milestones
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── File paths (set by init_mission_logger) ───────────────────────────────────
_log_dir: Path | None = None
_mission_log_path: Path | None = None
_events_jsonl_path: Path | None = None

# ── Python logger instance ────────────────────────────────────────────────────
_mlog = logging.getLogger("kaputnik.mission")

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────

def init_mission_logger(base_dir: Path, orbit: int = 0):
    """
    Initialise the mission logger. Call once at startup.
    Creates log files and writes the BOOT header.

    base_dir : root directory of the mission (where 'storage/' lives)
    orbit    : current orbit number (for resume-after-sleep labelling)
    """
    global _log_dir, _mission_log_path, _events_jsonl_path

    _log_dir = base_dir / "storage" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)

    _mission_log_path  = _log_dir / "kaputnik_mission.log"
    _events_jsonl_path = _log_dir / "kaputnik_events.jsonl"

    # Attach a file handler to the mission logger
    if not _mlog.handlers:
        fh = logging.FileHandler(_mission_log_path)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ"
        ))
        _mlog.addHandler(fh)
        _mlog.setLevel(logging.DEBUG)
        _mlog.propagate = True   # Also flows to root logger → console + kaputnik.log

    _write_event("BOOT", orbit=orbit, details={
        "message"    : "Kaputnik mission logger initialised",
        "log_file"   : str(_mission_log_path),
        "events_file": str(_events_jsonl_path),
    })


# ─────────────────────────────────────────────
# CORE WRITE
# ─────────────────────────────────────────────

def _write_event(event_type: str, orbit: int = 0, details: dict | None = None,
                 level: str = "INFO"):
    """
    Internal: write a structured event to both log files.
    """
    ts = datetime.now(timezone.utc).isoformat()

    # ── Human-readable log ──
    detail_str = ""
    if details:
        # Flatten to a compact single-line summary for the .log file
        parts = []
        for k, v in details.items():
            if isinstance(v, float):
                parts.append(f"{k}={v:.4g}")
            elif isinstance(v, dict):
                pass   # nested dicts skipped in summary line, appear in JSONL
            else:
                parts.append(f"{k}={v}")
        detail_str = "  |  " + "  ".join(parts) if parts else ""

    log_line = f"[{event_type:<10}]  orbit={orbit:04d}{detail_str}"

    if level == "ERROR":
        _mlog.error(log_line)
    elif level == "WARNING":
        _mlog.warning(log_line)
    elif level == "DEBUG":
        _mlog.debug(log_line)
    else:
        _mlog.info(log_line)

    # ── Machine-readable JSONL ──
    if _events_jsonl_path:
        record = {
            "ts"        : ts,
            "event"     : event_type,
            "orbit"     : orbit,
            "level"     : level,
            "details"   : details or {},
        }
        try:
            with open(_events_jsonl_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            _mlog.warning(f"Failed to write JSONL event: {e}")


# ─────────────────────────────────────────────
# PUBLIC EVENT FUNCTIONS
# ─────────────────────────────────────────────

def log_orbit_start(orbit: int, total_orbits: int, timestamp: str):
    _write_separator(f"ORBIT {orbit}/{total_orbits}")
    _write_event("ORBIT", orbit=orbit, details={
        "status"       : "START",
        "orbit"        : orbit,
        "total_orbits" : total_orbits,
        "utc_time"     : timestamp,
        "phase"        : "CAPTURE" if orbit <= 375 else "DOWNLINK_ONLY",
    })


def log_orbit_end(orbit: int, duration_s: float):
    _write_event("ORBIT", orbit=orbit, details={
        "status"      : "END",
        "duration_s"  : round(duration_s, 1),
    })


def log_health(orbit: int, power: dict, cpu_temp_c: float):
    """Log battery, solar, and thermal status."""
    level = "INFO"
    warnings = []

    batt_pct = power.get("battery_percent", 100)
    if batt_pct < 20:
        warnings.append(f"LOW BATTERY: {batt_pct:.1f}%")
        level = "WARNING"
    if cpu_temp_c > 75:
        warnings.append(f"HIGH CPU TEMP: {cpu_temp_c:.1f}°C")
        level = "WARNING"

    _write_event("HEALTH", orbit=orbit, level=level, details={
        "battery_pct"   : round(batt_pct, 1),
        "battery_v"     : round(power.get("battery_voltage_v", 0), 3),
        "battery_wh"    : round(power.get("battery_wh", 0), 3),
        "charging"      : power.get("is_charging", False),
        "solar_present" : power.get("solar_connected", False),
        "cpu_temp_c"    : round(cpu_temp_c, 1),
        "warnings"      : warnings,
    })


def log_imu(orbit: int, imu_data: dict, anomalies: list[str]):
    """Log full 9-DOF IMU snapshot and any detected anomalies."""
    level = "WARNING" if anomalies else "INFO"
    _write_event("IMU", orbit=orbit, level=level, details={
        **imu_data,
        "anomalies": anomalies,
        "anomaly_count": len(anomalies),
    })
    for a in anomalies:
        _write_event("WARNING", orbit=orbit, level="WARNING",
                     details={"source": "IMU", "message": a})


def log_capture_start(orbit: int, image_count: int):
    _write_event("CAPTURE", orbit=orbit, details={
        "status"       : "START",
        "images_planned": image_count,
    })


def log_image_saved(orbit: int, index: int, filename: str, size_bytes: int):
    _write_event("CAPTURE", orbit=orbit, details={
        "status"    : "IMAGE_SAVED",
        "index"     : index,
        "filename"  : filename,
        "size_kb"   : round(size_bytes / 1024, 1),
    })


def log_capture_complete(orbit: int, saved: int, failed: int):
    level = "WARNING" if failed > 0 else "INFO"
    _write_event("CAPTURE", orbit=orbit, level=level, details={
        "status" : "COMPLETE",
        "saved"  : saved,
        "failed" : failed,
    })


def log_diff_result(orbit: int, new_image: str, reference_image: str,
                    diff_image: str, changed: bool, diff_size_kb: float):
    """Log a change-detection result."""
    _write_event("DIFF", orbit=orbit, details={
        "new_image"  : new_image,
        "ref_image"  : reference_image,
        "diff_image" : diff_image,
        "change_detected": changed,
        "diff_size_kb"   : round(diff_size_kb, 1),
    })


def log_no_reference_found(orbit: int, image: str):
    _write_event("DIFF", orbit=orbit, details={
        "status"  : "NO_REFERENCE",
        "image"   : image,
        "message" : "No archive image found within comparison window (~708.55h)",
    })


def log_downlink_start(orbit: int, pending_count: int, pending_mb: float):
    _write_event("DOWNLINK", orbit=orbit, details={
        "status"       : "LINK_OPEN",
        "pending_files": pending_count,
        "pending_mb"   : round(pending_mb, 2),
    })


def log_downlink_tx(orbit: int, filename: str, size_kb: float, success: bool,
                    total_sent_mb: float):
    level = "WARNING" if not success else "INFO"
    _write_event("DOWNLINK", orbit=orbit, level=level, details={
        "status"        : "TX_OK" if success else "TX_FAIL",
        "filename"      : filename,
        "size_kb"       : round(size_kb, 1),
        "total_sent_mb" : round(total_sent_mb, 2),
    })


def log_downlink_cap(orbit: int, sent: int, total_mb: float, deferred: int):
    _write_event("DOWNLINK", orbit=orbit, details={
        "status"      : "CAP_REACHED",
        "files_sent"  : sent,
        "total_mb"    : round(total_mb, 2),
        "deferred"    : deferred,
    })


def log_downlink_complete(orbit: int, sent: int, total_mb: float,
                          deferred: int, telemetry_ok: bool):
    _write_event("DOWNLINK", orbit=orbit, details={
        "status"       : "LINK_CLOSED",
        "files_sent"   : sent,
        "total_mb"     : round(total_mb, 2),
        "deferred"     : deferred,
        "telemetry_ok" : telemetry_ok,
    })


def log_sleep(orbit: int, sleep_s: float, wake_utc: str):
    _write_event("SLEEP", orbit=orbit, details={
        "status"      : "ENTERING_DEEP_SLEEP",
        "sleep_min"   : round(sleep_s / 60, 1),
        "wake_utc"    : wake_utc,
        "rtc_source"  : "PiSugar3",
    })


def log_wakeup(orbit: int):
    _write_event("BOOT", orbit=orbit, details={
        "status"  : "WAKEUP",
        "message" : "Pi woke from deep sleep via PiSugar 3 RTC alarm",
    })


def log_mission_start(total_orbits: int, capture_end: int):
    _write_separator("MISSION START")
    _write_event("MISSION", orbit=0, details={
        "status"           : "START",
        "total_orbits"     : total_orbits,
        "capture_phase_end": capture_end,
        "satellite"        : "Kaputnik",
        "mission"          : "Lunar Surface Change Monitor",
    })


def log_mission_complete(total_orbits: int):
    _write_separator("MISSION COMPLETE")
    _write_event("MISSION", orbit=total_orbits, details={
        "status"       : "COMPLETE",
        "total_orbits" : total_orbits,
        "message"      : "All orbits executed successfully.",
    })


def log_error(orbit: int, source: str, message: str, exception: str = ""):
    _write_event("ERROR", orbit=orbit, level="ERROR", details={
        "source"   : source,
        "message"  : message,
        "exception": exception,
    })


def log_warning(orbit: int, source: str, message: str):
    _write_event("WARNING", orbit=orbit, level="WARNING", details={
        "source" : source,
        "message": message,
    })


def log_low_battery_skip(orbit: int, battery_pct: float, threshold_pct: float):
    _write_event("WARNING", orbit=orbit, level="WARNING", details={
        "source"        : "POWER",
        "message"       : "Battery below threshold — orbit ops skipped",
        "battery_pct"   : round(battery_pct, 1),
        "threshold_pct" : threshold_pct,
    })


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _write_separator(label: str = ""):
    """Write a visual separator to the human-readable log."""
    if _mission_log_path:
        line = f"\n{'─'*60}\n  {label}\n{'─'*60}\n" if label else f"\n{'─'*60}\n"
        try:
            with open(_mission_log_path, "a") as f:
                f.write(line)
        except Exception:
            pass


def get_log_paths() -> dict[str, str]:
    """Return paths to all log files (for downlinking or display)."""
    return {
        "mission_log"  : str(_mission_log_path) if _mission_log_path else "",
        "events_jsonl" : str(_events_jsonl_path) if _events_jsonl_path else "",
    }
