"""
github_sync.py
==============
Kaputnik GitHub Auto-Sync
Target repo: https://github.com/varvarakarenski/kaputnik_repo.git

Folder structure on Pi and in GitHub:
  /home/kaputnik/kaputnik_repo/
  ├── kaputnik_images/    ← all archived images and diff images
  ├── Logs/               ← mission log files
  ├── storage/telemetry/  ← per-orbit telemetry JSON files
  ├── storage/mission_state.json
  └── *.py                ← code files

What does NOT get pushed:
  storage/pending/        ← images not yet downlinked
  test_run/               ← test data
"""

import subprocess
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("kaputnik.github")

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

GIT_BRANCH    = "main"
REPO_URL      = "https://github.com/varvarakarenski/kaputnik_repo.git"
REPO_DIR      = Path("/home/kaputnik/kaputnik_repo")   # root of the cloned repo
IMAGES_DIR    = REPO_DIR / "kaputnik_images"      # images land here
LOGS_DIR      = REPO_DIR / "Logs"                 # logs land here

# ─────────────────────────────────────────────
# CORE GIT HELPER
# ─────────────────────────────────────────────

def _run_git(args: list[str], cwd: Path = REPO_DIR) -> tuple[bool, str]:
    """Run a git command in cwd. Returns (success, output)."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout.strip() + result.stderr.strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        log.warning(f"Git command timed out: git {' '.join(args)}")
        return False, "timeout"
    except Exception as e:
        log.warning(f"Git command failed: {e}")
        return False, str(e)


# ─────────────────────────────────────────────
# IMAGE SYNC → kaputnik_images/
# ─────────────────────────────────────────────

def _sync_images(archive_dir: Path) -> int:
    """
    Copy all images from storage/archive/ into kaputnik_images/.
    Only copies files not already there.
    Returns number of new files copied.
    """
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    if not archive_dir.exists():
        log.info("No archive folder yet — skipping image sync.")
        return 0

    copied = 0
    for img in sorted(archive_dir.glob("*.jpg")):
        dest = IMAGES_DIR / img.name
        if not dest.exists():
            shutil.copy2(str(img), str(dest))
            copied += 1
            log.info(f"  Copied to kaputnik_images/: {img.name}")

    log.info(f"  {copied} new image(s) added to kaputnik_images/") if copied else \
    log.info("  kaputnik_images/ already up to date.")
    return copied


# ─────────────────────────────────────────────
# LOG SYNC → Logs/
# ─────────────────────────────────────────────

def _sync_logs(mission_log: Path, events_jsonl: Path) -> int:
    """
    Copy mission log files into Logs/.
    Overwrites existing files so Logs/ always has the latest version.
    Returns number of files copied.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src in [mission_log, events_jsonl]:
        if src and Path(src).exists():
            dest = LOGS_DIR / Path(src).name
            shutil.copy2(str(src), str(dest))
            copied += 1
            log.info(f"  Copied to Logs/: {Path(src).name}")

    return copied


# ─────────────────────────────────────────────
# MAIN SYNC FUNCTION
# ─────────────────────────────────────────────

def sync_to_github(base_dir: Path, orbit: int,
                   mission_log: str = "", events_jsonl: str = "") -> bool:
    """
    Sync all mission data to GitHub:
      - Images   → kaputnik_images/
      - Logs     → Logs/
      - Telemetry → storage/telemetry/
      - State    → storage/mission_state.json

    base_dir      : mission working directory (where storage/ lives)
    orbit         : current orbit number (used in commit message)
    mission_log   : path to kaputnik_mission.log
    events_jsonl  : path to kaputnik_events.jsonl
    """
    log.info(f"[Orbit {orbit}] Starting GitHub sync → {REPO_URL}")

    # ── Check repo exists ─────────────────────────────────────────
    if not (REPO_DIR / ".git").exists():
        log.error(f"No .git found at {REPO_DIR}")
        log.error(f"Clone the repo first: git clone {REPO_URL} /home/kaputnik/kaputnik_repo")
        return False

    # ── Sync images ───────────────────────────────────────────────
    archive_dir = base_dir / "storage" / "archive"
    _sync_images(archive_dir)

    # ── Sync logs ─────────────────────────────────────────────────
    _sync_logs(Path(mission_log) if mission_log else Path(""),
               Path(events_jsonl) if events_jsonl else Path(""))

    # ── Copy telemetry and state into repo ────────────────────────
    # Telemetry
    src_telemetry = base_dir / "storage" / "telemetry"
    dst_telemetry = REPO_DIR / "storage" / "telemetry"
    if src_telemetry.exists():
        dst_telemetry.mkdir(parents=True, exist_ok=True)
        for f in src_telemetry.glob("*.json"):
            dest = dst_telemetry / f.name
            if not dest.exists():
                shutil.copy2(str(f), str(dest))

    # Mission state
    src_state = base_dir / "storage" / "mission_state.json"
    if src_state.exists():
        dst_state_dir = REPO_DIR / "storage"
        dst_state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_state), str(REPO_DIR / "storage" / "mission_state.json"))

    # ── Stage everything ──────────────────────────────────────────
    paths_to_stage = [
        "kaputnik_images",
        "Logs",
        "storage/telemetry",
        "storage/mission_state.json",
        "kaputnik_main.py",
        "power_manager.py",
        "imu.py",
        "mission_logger.py",
        "github_sync.py",
        "kaputnik_test.py",
        ".gitignore",
    ]

    staged_any = False
    for path_str in paths_to_stage:
        if (REPO_DIR / path_str).exists():
            ok, out = _run_git(["add", path_str])
            if ok:
                staged_any = True
            else:
                log.warning(f"  Could not stage {path_str}: {out}")

    if not staged_any:
        log.info(f"[Orbit {orbit}] Nothing to stage — skipping push.")
        return True

    # ── Check for actual changes ──────────────────────────────────
    _, status = _run_git(["status", "--porcelain"])
    if not status.strip():
        log.info(f"[Orbit {orbit}] No changes since last push — skipping.")
        return True

    # ── Commit ────────────────────────────────────────────────────
    timestamp  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    commit_msg = f"Orbit {orbit:04d}/383 | {timestamp} | auto-downlink"

    ok, out = _run_git(["commit", "-m", commit_msg])
    if not ok:
        if "nothing to commit" in out.lower():
            log.info(f"[Orbit {orbit}] Nothing new to commit.")
            return True
        log.warning(f"[Orbit {orbit}] Commit failed: {out}")
        return False

    log.info(f"[Orbit {orbit}] Committed: {commit_msg}")

    # ── Push ──────────────────────────────────────────────────────
    ok, out = _run_git(["push", "origin", GIT_BRANCH])
    if ok:
        log.info(f"[Orbit {orbit}] Push successful → {REPO_URL}")
        return True
    else:
        log.warning(f"[Orbit {orbit}] Push failed: {out}")
        log.warning("Committed locally — will retry next downlink pass.")
        return False


# ─────────────────────────────────────────────
# ONE-TIME SETUP
# ─────────────────────────────────────────────

def setup_github_sync(username: str, token: str):
    """
    One-time setup. Run this once on the Pi after cloning to embed
    credentials so git never prompts for a password again.

    Usage:
        from github_sync import setup_github_sync
        setup_github_sync('varvarakarenski', 'ghp_yourtoken...')
    """
    log.info("Running one-time GitHub sync setup...")

    if not REPO_DIR.exists():
        log.error(f"{REPO_DIR} does not exist.")
        log.error(f"Clone the repo first:")
        log.error(f"  git clone {REPO_URL} /home/kaputnik/kaputnik_repo")
        return False

    # Embed token into remote URL
    authed_url = f"https://{username}:{token}@github.com/varvarakarenski/kaputnik_repo.git"
    _run_git(["config", "user.name",  "Kaputnik"])
    _run_git(["config", "user.email", "kaputnik@mission.local"])
    _run_git(["remote", "set-url", "origin", authed_url])
    log.info("Credentials embedded in remote URL.")

    # Create required folders with placeholders
    for folder, placeholder in [
        (IMAGES_DIR, ".gitkeep"),
        (LOGS_DIR,   ".gitkeep"),
        (REPO_DIR / "storage" / "telemetry", ".gitkeep"),
    ]:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / placeholder).touch()
    log.info("kaputnik_images/, Logs/, storage/telemetry/ created.")

    # Write .gitignore
    with open(REPO_DIR / ".gitignore", "w") as f:
        f.write(
            "# Kaputnik .gitignore\n"
            "storage/pending/\n"
            "__pycache__/\n"
            "*.pyc\n"
            ".DS_Store\n"
            "test_run/\n"
        )

    # Initial commit and push
    _run_git(["add", "."])
    _run_git(["commit", "-m", "Kaputnik initial setup"])
    ok, out = _run_git(["push", "-u", "origin", GIT_BRANCH])

    if ok:
        log.info("Initial push successful.")
        log.info(f"  Images → {REPO_URL}/tree/main/kaputnik_images")
        log.info(f"  Logs   → {REPO_URL}/tree/main/Logs")
        return True
    else:
        log.error(f"Push failed: {out}")
        log.error("Check your token has 'repo' scope.")
        return False


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("=== GitHub Sync Test ===")
    print(f"Repo dir : {REPO_DIR}")
    print(f"Images   : {IMAGES_DIR}")
    print(f"Logs     : {LOGS_DIR}")
    print(f".git     : {'exists' if (REPO_DIR/'.git').exists() else 'NOT FOUND'}")
    from pathlib import Path
    ok = sync_to_github(Path("/home/kaputnik/kaputnik_repo"), orbit=0)
    print(f"Result   : {'OK' if ok else 'FAILED'}")
