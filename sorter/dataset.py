"""Dataset management. raw/ is the source of truth — stamp/ training crops
are always derivable from it via rebuild_crops(), so every mutation
(delete, rename/merge) is: change raw/, then rebuild.
"""
import time
from pathlib import Path

import cv2

from . import imaging


def dataset_counts(root):
    """{class_name: n_images} for a directory-per-class dataset."""
    root = Path(root)
    if not root.is_dir():
        return {}
    return {d.name: sum(1 for f in d.iterdir()
                        if f.suffix.lower() in (".png", ".jpg", ".jpeg"))
            for d in sorted(root.iterdir()) if d.is_dir()}


def _unlink_retry(path, attempts=4):
    """Delete a file, riding out transient sync-tool (OneDrive) locks."""
    for i in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.15 * (i + 1))
    return False


def parse_label(label):
    """'LC_crimped' -> ('LC', 'crimped'); 'HORNADY' -> ('HORNADY', None)."""
    if label.endswith(("_crimped", "_noncrimp")):
        stamp, variant = label.rsplit("_", 1)
        return stamp, variant
    return label, None


def safe_label(label):
    """Reject anything that could escape the raw/ directory."""
    if not label or any(c in label for c in "/\\") or ".." in label:
        raise ValueError(f"invalid label {label!r}")
    return label


def raw_counts(root):
    """{label: n_pairs} for every collected raw label folder."""
    raw = Path(root) / "raw"
    if not raw.is_dir():
        return {}
    return {d.name: len(list(d.glob("*_A.png")))
            for d in sorted(raw.iterdir()) if d.is_dir()}


def rebuild_crops(root, labels=None):
    """Regenerate stamp/ crops from raw/. Returns (n_stamp, 0), where
    n_stamp counts every crop on disk afterwards (not just ones written).

    labels: raw label names to limit the rebuild to — a rename or delete
    only invalidates the classes it touched, and a full rebuild is
    ~30s of image decoding on the Pi at 1,400 images. None = everything
    (imaging config changes reshape every crop).

    Legacy variant labels (X_crimped / X_noncrimp) still contribute their
    frames to stamp/X — crimp detection itself is retired.
    """
    root = Path(root)
    raw = root / "raw"
    base = root / "stamp"
    stamps = (None if labels is None
              else {parse_label(l)[0] for l in labels})
    if base.exists():
        for png in base.rglob("*.png"):
            if stamps is None or png.parent.name in stamps:
                _unlink_retry(png)   # a still-locked file just gets overwritten below
    n_stamp = 0
    if raw.is_dir():
        for label_dir in sorted(p for p in raw.iterdir() if p.is_dir()):
            label = label_dir.name
            stamp, _variant = parse_label(label)
            if stamps is not None and stamp not in stamps:
                continue
            for a_path in sorted(label_dir.glob("*_A.png")):
                idx = a_path.name.split("_")[0]
                frame_a = cv2.imread(str(a_path))
                if frame_a is None:
                    continue
                d = root / "stamp" / stamp
                d.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(d / f"{label}_{idx}_A.png"), imaging.crop_head(frame_a))
                n_stamp += 1
    if stamps is not None and base.is_dir():
        # partial rebuild: report the whole tree, same as a full one
        n_stamp = sum(1 for _ in base.rglob("*.png"))
    # sweep class dirs that are now empty (best effort — sync tools like
    # OneDrive sometimes hold a lock on the folder itself)
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
    return n_stamp, 0


def delete_pair(root, label, index):
    """Delete one A/B pair from a raw label. Returns files removed."""
    d = Path(root) / "raw" / safe_label(label)
    removed = 0
    for suffix in ("A", "B"):
        p = d / f"{int(index):04d}_{suffix}.png"
        if p.exists() and _unlink_retry(p):
            removed += 1
    return removed


def delete_label(root, label):
    """Delete an entire raw label folder ('reset this class'). Returns files removed."""
    d = Path(root) / "raw" / safe_label(label)
    if not d.is_dir():
        return 0
    removed = 0
    for p in d.glob("*.png"):
        if _unlink_retry(p):
            removed += 1
    try:
        d.rmdir()
    except OSError:
        pass
    return removed


def move_pair(root, label, index, new_label):
    """Relabel one A/B pair: move it to another raw label folder, renumbered
    to that folder's next free index. Returns the new index."""
    src_d = Path(root) / "raw" / safe_label(label)
    dst_d = Path(root) / "raw" / safe_label(new_label)
    if label == new_label:
        raise ValueError("already has that label")
    a_path = src_d / f"{int(index):04d}_A.png"
    b_path = src_d / f"{int(index):04d}_B.png"
    if not a_path.exists() and not b_path.exists():
        raise ValueError(f"no pair #{index} in {label!r}")
    dst_d.mkdir(parents=True, exist_ok=True)
    existing = sorted(dst_d.glob("*_[AB].png"))
    next_i = int(existing[-1].name.split("_")[0]) + 1 if existing else 0
    for p, suffix in ((a_path, "A"), (b_path, "B")):
        if p.exists():
            p.rename(dst_d / f"{next_i:04d}_{suffix}.png")
    try:
        next(src_d.iterdir())
    except StopIteration:
        try:
            src_d.rmdir()
        except OSError:
            pass
    return next_i


def rename_label(root, src, dst):
    """Rename/merge a raw label. If dst exists, src pairs are renumbered to
    follow dst's — a merge, nothing overwritten. Returns pairs moved."""
    src_d = Path(root) / "raw" / safe_label(src)
    dst_d = Path(root) / "raw" / safe_label(dst)
    if not src_d.is_dir():
        raise ValueError(f"no such label {src!r}")
    if src == dst:
        return 0
    dst_d.mkdir(parents=True, exist_ok=True)
    existing = sorted(dst_d.glob("*_A.png"))
    next_i = int(existing[-1].name.split("_")[0]) + 1 if existing else 0
    moved = 0
    for a_path in sorted(src_d.glob("*_A.png")):
        idx = a_path.name.split("_")[0]
        for suffix in ("A", "B"):
            p = src_d / f"{idx}_{suffix}.png"
            if p.exists():
                p.rename(dst_d / f"{next_i:04d}_{suffix}.png")
        next_i += 1
        moved += 1
    try:
        src_d.rmdir()
    except OSError:
        pass
    return moved
