"""
analyse_changes.py
==================
Kaputnik Surface Change Analyser
Runs on your laptop. Takes a diff image (grayscale) produced by Kaputnik
and finds how many distinct regions (blobs) of change exist.

Dependencies:
    pip install pillow numpy scipy

Usage:
    python3 analyse_changes.py diff_image.jpg
    python3 analyse_changes.py diff_image.jpg --threshold 30 --min-blob 50
    python3 analyse_changes.py                  (opens file picker)

Output:
    - Number of distinct change blobs found
    - Size of each blob in pixels
    - Percentage of surface area changed
    - Annotated image saved showing each blob outlined and numbered
    - JSON report saved alongside the image
"""

import sys
import os
import argparse
import json
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

try:
    from scipy import ndimage
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("[WARN] scipy not found — install with: pip install scipy")
    print("       Falling back to basic blob detection.\n")

# ─────────────────────────────────────────────
# CONFIGURATION DEFAULTS
# ─────────────────────────────────────────────

DEFAULT_THRESHOLD = 25   # Pixels brighter than this in the diff are "changed"
                          # Lower = more sensitive. Raise to ignore noise.
DEFAULT_MIN_BLOB  = 100  # Ignore blobs smaller than this many pixels (noise filter)
DEFAULT_MAX_BLOB  = None # No upper limit by default

# ─────────────────────────────────────────────
# IMAGE LOADING
# ─────────────────────────────────────────────

def load_diff_image(path: str) -> np.ndarray:
    """Load diff image as grayscale numpy array."""
    img = Image.open(path).convert("L")
    arr = np.array(img, dtype=np.uint8)
    print(f"  Image size   : {img.width} x {img.height} px")
    print(f"  Total pixels : {img.width * img.height:,}")
    print(f"  Value range  : {arr.min()} to {arr.max()}")
    return arr

# ─────────────────────────────────────────────
# THRESHOLDING
# ─────────────────────────────────────────────

def threshold_image(arr: np.ndarray, threshold: int) -> np.ndarray:
    """Return binary mask — 1 where pixel > threshold (changed), 0 elsewhere."""
    binary     = (arr > threshold).astype(np.uint8)
    changed_px = int(binary.sum())
    total_px   = binary.size
    print(f"  Changed pixels (threshold={threshold}): "
          f"{changed_px:,} / {total_px:,} "
          f"({changed_px / total_px * 100:.2f}%)")
    return binary

# ─────────────────────────────────────────────
# BLOB DETECTION — scipy (preferred)
# ─────────────────────────────────────────────

def find_blobs_scipy(binary: np.ndarray, min_blob: int,
                     max_blob) -> tuple:
    """
    Use scipy connected component labelling to find blobs.
    Diagonal neighbours count as connected (8-connectivity).
    """
    structure          = ndimage.generate_binary_structure(2, 2)
    labelled, n_feats  = ndimage.label(binary, structure=structure)

    blobs = []
    for label_id in range(1, n_feats + 1):
        mask = labelled == label_id
        size = int(mask.sum())

        if size < min_blob:
            labelled[mask] = 0
            continue
        if max_blob and size > max_blob:
            continue

        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        r_min, r_max = np.where(rows)[0][[0, -1]]
        c_min, c_max = np.where(cols)[0][[0, -1]]
        cy, cx = ndimage.center_of_mass(mask)

        blobs.append({
            "id"      : len(blobs) + 1,
            "size_px" : size,
            "centroid": (int(cx), int(cy)),
            "bbox"    : (int(c_min), int(r_min), int(c_max), int(r_max)),
            "width"   : int(c_max - c_min + 1),
            "height"  : int(r_max - r_min + 1),
        })

    return blobs, labelled

# ─────────────────────────────────────────────
# BLOB DETECTION — basic fallback
# ─────────────────────────────────────────────

def find_blobs_basic(binary: np.ndarray, min_blob: int,
                     max_blob) -> tuple:
    """Flood-fill blob detection. Used when scipy is unavailable."""
    visited  = np.zeros_like(binary, dtype=bool)
    labelled = np.zeros_like(binary, dtype=np.int32)
    blobs    = []

    def flood_fill(sy, sx):
        stack, pixels = [(sy, sx)], []
        while stack:
            y, x = stack.pop()
            if not (0 <= y < binary.shape[0]): continue
            if not (0 <= x < binary.shape[1]): continue
            if visited[y, x] or binary[y, x] == 0: continue
            visited[y, x] = True
            pixels.append((y, x))
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    if dy == 0 and dx == 0: continue
                    stack.append((y + dy, x + dx))
        return pixels

    label_id = 0
    for y in range(binary.shape[0]):
        for x in range(binary.shape[1]):
            if binary[y, x] == 1 and not visited[y, x]:
                pixels = flood_fill(y, x)
                size   = len(pixels)
                if size < min_blob: continue
                if max_blob and size > max_blob: continue
                label_id += 1
                ys = [p[0] for p in pixels]
                xs = [p[1] for p in pixels]
                for py, px in pixels:
                    labelled[py, px] = label_id
                blobs.append({
                    "id"      : label_id,
                    "size_px" : size,
                    "centroid": (int(np.mean(xs)), int(np.mean(ys))),
                    "bbox"    : (min(xs), min(ys), max(xs), max(ys)),
                    "width"   : max(xs) - min(xs) + 1,
                    "height"  : max(ys) - min(ys) + 1,
                })

    return blobs, labelled

# ─────────────────────────────────────────────
# BLOB DETECTION — router
# ─────────────────────────────────────────────

def find_blobs(binary, min_blob, max_blob):
    if SCIPY_AVAILABLE:
        return find_blobs_scipy(binary, min_blob, max_blob)
    print("  [Using basic flood-fill — install scipy for better results]")
    return find_blobs_basic(binary, min_blob, max_blob)

# ─────────────────────────────────────────────
# ANNOTATED IMAGE
# ─────────────────────────────────────────────

COLOURS = [
    "#FF4444", "#44FF44", "#4488FF", "#FFFF44",
    "#FF44FF", "#44FFFF", "#FF8844", "#88FF44",
    "#FF4488", "#44FF88", "#8844FF", "#44AAFF",
]

def annotate_image(diff_path: str, blobs: list, output_path: str, threshold: int):
    """Draw bounding boxes and blob numbers onto the diff image."""
    img  = Image.open(diff_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font  = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        sfont = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except Exception:
        font  = ImageFont.load_default()
        sfont = font

    for i, blob in enumerate(blobs):
        colour       = COLOURS[i % len(COLOURS)]
        x1, y1, x2, y2 = blob["bbox"]
        cx, cy       = blob["centroid"]

        # Bounding box
        draw.rectangle([x1 - 2, y1 - 2, x2 + 2, y2 + 2],
                       outline=colour, width=2)
        # Number label at centroid
        draw.rectangle([cx - 9, cy - 9, cx + 9, cy + 9], fill="black")
        draw.text((cx - 5, cy - 6), str(blob["id"]), fill=colour, font=font)

    # Summary bar at top
    summary = (f"  Blobs found: {len(blobs)}  |  "
               f"Threshold: {threshold}  |  "
               f"Largest: {max((b['size_px'] for b in blobs), default=0):,} px")
    draw.rectangle([0, 0, img.width, 22], fill="black")
    draw.text((4, 4), summary, fill="white", font=sfont)

    img.save(output_path)
    print(f"  Annotated image : {output_path}")

# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────

def print_report(blobs: list, image_shape: tuple, threshold: int, min_blob: int):
    total_px   = image_shape[0] * image_shape[1]
    changed_px = sum(b["size_px"] for b in blobs)

    print("\n" + "=" * 55)
    print("  KAPUTNIK SURFACE CHANGE ANALYSIS REPORT")
    print("=" * 55)
    print(f"  Threshold          : {threshold} / 255")
    print(f"  Min blob size      : {min_blob} px")
    print(f"  Image dimensions   : {image_shape[1]} x {image_shape[0]} px")
    print(f"  Total pixels       : {total_px:,}")
    print(f"  Changed pixels     : {changed_px:,} ({changed_px/total_px*100:.3f}%)")
    print(f"\n  BLOBS DETECTED     : {len(blobs)}")

    if not blobs:
        print("\n  No significant surface changes detected.")
    else:
        sorted_blobs = sorted(blobs, key=lambda b: b["size_px"], reverse=True)
        print(f"\n  {'#':>3}  {'Size (px)':>10}  {'W x H':>10}  "
              f"{'Centroid':>14}  {'% image':>8}")
        print(f"  {'─'*3}  {'─'*10}  {'─'*10}  {'─'*14}  {'─'*8}")
        for b in sorted_blobs:
            cx, cy = b["centroid"]
            print(f"  {b['id']:>3}  {b['size_px']:>10,}  "
                  f"{b['width']:>4} x {b['height']:<4}  "
                  f"({cx:>5}, {cy:>5})  "
                  f"{b['size_px']/total_px*100:>7.3f}%")

        print(f"\n  Largest blob  : {sorted_blobs[0]['size_px']:,} px  "
              f"(blob #{sorted_blobs[0]['id']})")
        print(f"  Smallest blob : {sorted_blobs[-1]['size_px']:,} px  "
              f"(blob #{sorted_blobs[-1]['id']})")
        print(f"  Average size  : {changed_px // len(blobs):,} px")

    print("=" * 55)


def save_json_report(blobs: list, image_shape: tuple,
                     threshold: int, output_path: str):
    total_px   = image_shape[0] * image_shape[1]
    changed_px = sum(b["size_px"] for b in blobs)
    report = {
        "threshold"      : threshold,
        "image_width"    : image_shape[1],
        "image_height"   : image_shape[0],
        "total_pixels"   : total_px,
        "changed_pixels" : changed_px,
        "changed_percent": round(changed_px / total_px * 100, 4),
        "blob_count"     : len(blobs),
        "blobs"          : blobs,
    }
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"  JSON report     : {output_path}")

# ─────────────────────────────────────────────
# MAIN ANALYSIS FUNCTION
# ─────────────────────────────────────────────

def analyse(diff_path: str,
            threshold: int  = DEFAULT_THRESHOLD,
            min_blob: int   = DEFAULT_MIN_BLOB,
            max_blob        = DEFAULT_MAX_BLOB,
            save_annotated  = True,
            save_json       = True) -> list:
    """
    Analyse a Kaputnik diff image and return list of blob dicts.
    Can also be imported and called from other scripts.
    """
    print(f"\nAnalysing: {diff_path}")
    print("─" * 55)

    arr    = load_diff_image(diff_path)
    binary = threshold_image(arr, threshold)

    print(f"  Finding blobs (min={min_blob} px)...")
    blobs, _ = find_blobs(binary, min_blob, max_blob)
    print(f"  Found {len(blobs)} blob(s)")

    print_report(blobs, arr.shape, threshold, min_blob)

    stem = Path(diff_path).stem
    parent = Path(diff_path).parent

    if save_annotated and blobs:
        annotate_image(diff_path, blobs,
                       str(parent / f"{stem}_annotated.jpg"), threshold)

    if save_json:
        save_json_report(blobs, arr.shape, threshold,
                         str(parent / f"{stem}_report.json"))

    return blobs

# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def prompt_int(message: str, default: int, min_val: int, max_val: int) -> int:
    """Ask the user for an integer value with a default and validation."""
    while True:
        try:
            raw = input(f"{message} (default={default}, range={min_val}-{max_val}): ").strip()
            if raw == "":
                return default
            val = int(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  Please enter a number between {min_val} and {max_val}.")
        except ValueError:
            print("  Please enter a whole number.")


def main():
    print("=" * 55)
    print("  KAPUTNIK SURFACE CHANGE ANALYSER")
    print("=" * 55)

    # ── Step 1: Select image ──────────────────────────────────
    image_path = None

    # Check if path was passed as command line argument
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        image_path = sys.argv[1]
    else:
        # Try file picker first
        try:
            import tkinter as tk
            from tkinter import filedialog
            print("
  Opening file picker — select your diff image...")
            root = tk.Tk()
            root.withdraw()
            root.lift()
            image_path = filedialog.askopenfilename(
                title="Select Kaputnik diff image",
                filetypes=[("JPEG", "*.jpg"), ("PNG", "*.png"), ("All", "*.*")]
            )
            root.destroy()
            if not image_path:
                print("  No file selected.")
                sys.exit(0)
        except Exception:
            # Fall back to typed path
            image_path = input("
  Enter path to diff image: ").strip()

    if not image_path or not os.path.exists(image_path):
        print(f"  File not found: {image_path}")
        sys.exit(1)

    print(f"
  Selected: {image_path}")

    # ── Step 2: Threshold prompt ──────────────────────────────
    print("
  THRESHOLD — how bright a pixel needs to be to count as changed.")
    print("  Lower = more sensitive (catches subtle changes).")
    print("  Higher = less sensitive (only catches obvious changes).")
    threshold = prompt_int("  Enter threshold", DEFAULT_THRESHOLD, 1, 254)

    # ── Step 3: Min blob size prompt ──────────────────────────
    print("
  MIN BLOB SIZE — minimum number of pixels a change region must")
    print("  have to be counted. Smaller values catch more detail but may")
    print("  include noise. Larger values only count significant regions.")
    min_blob = prompt_int("  Enter min blob size (px)", DEFAULT_MIN_BLOB, 1, 100000)

    print()

    # ── Run analysis ──────────────────────────────────────────
    if not os.path.exists(image_path):
        print(f"  File not found: {image_path}")
        sys.exit(1)

    analyse(
        diff_path     = image_path,
        threshold     = threshold,
        min_blob      = min_blob,
        save_annotated= True,
        save_json     = True,
    )

if __name__ == "__main__":
    main()
