"""Focus the camera lens by measurement — built for a sealed light tube.

The LightBurn 4K-W (Sunplus 1bcf:28c4, IMX415) advertises V4L2 focus
controls but they are decorative — the lens is a screw-thread MANUAL
focus (probed: focus_absolute 0 vs 1000 = pixel-identical).
The camera lives sealed inside a light-proof tube, so focusing is an
iterative hill-climb: remove housing -> turn lens a noted amount ->
reinsert -> measure -> repeat toward the peak.

Run ON the Pi with the SortIQ service stopped (it owns the device):

    sudo systemctl stop sortiq

    # one reading per insertion, appended to focus_log.txt:
    .venv/bin/python tools/camera_focus_assist.py --note "1/4 CW"

    # or, when the camera is out on the bench and you CAN see it,
    # live mode: prop a target at the tube's lens-to-case distance
    # and turn the lens until the number peaks:
    .venv/bin/python tools/camera_focus_assist.py --live

    sudo systemctl start sortiq

Keep the SAME case in the nest and the LED at the same level for every
measurement — the log records mean brightness so a lighting change that
would skew the comparison is visible. Sharpness is Laplacian variance
on an 8-frame-averaged crop around the brightest blob (the lit case
head); averaging keeps sensor noise from drowning the focus signal.
Each measurement also saves /tmp/focus_check.jpg for an eyeball check.
"""
import argparse
import datetime
import pathlib
import subprocess
import sys

import cv2
import numpy as np

LOG = pathlib.Path(__file__).resolve().parent.parent / "focus_log.txt"
AVG_FRAMES = 8


def open_cam(dev):
    subprocess.run(
        f"v4l2-ctl -d /dev/video{dev} --set-ctrl auto_exposure=3", shell=True)
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    for _ in range(15):   # AE settle
        cap.read()
    return cap


def head_crop(gray):
    _, _, _, (x, y) = cv2.minMaxLoc(cv2.GaussianBlur(gray, (51, 51), 0))
    return gray[max(0, y - 160):y + 160, max(0, x - 160):x + 160]


def averaged_reading(cap):
    """Sharpness + brightness on the mean of AVG_FRAMES frames."""
    acc, last = None, None
    n = 0
    while n < AVG_FRAMES:
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float64)
        acc = g if acc is None else acc + g
        last = f
        n += 1
    mean_img = (acc / n).astype(np.uint8)
    crop = head_crop(mean_img)
    return (cv2.Laplacian(crop, cv2.CV_64F).var(),
            float(mean_img.mean()), last)


def measure(dev, note):
    cap = open_cam(dev)
    sharp, bright, frame = averaged_reading(cap)
    cap.release()
    cv2.imwrite("/tmp/focus_check.jpg", frame)

    prev = []
    if LOG.exists():
        prev = [l for l in LOG.read_text().splitlines() if l.strip()]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    LOG.open("a").write(
        f"{stamp}  sharpness {sharp:8.1f}  frame-mean {bright:5.1f}"
        f"  {note or ''}\n")

    print(f"\nsharpness: {sharp:.1f}   (frame mean {bright:.1f};"
          f" big brightness swings make readings incomparable)")
    if prev:
        print("\nhistory:")
        for line in prev[-8:]:
            print("  " + line)
    print(f"  {stamp}  sharpness {sharp:8.1f}  frame-mean {bright:5.1f}"
          f"  {note or ''}   <- this reading")
    print(f"\nsnapshot: /tmp/focus_check.jpg   log: {LOG}")


def live(dev):
    cap = open_cam(dev)
    peak = 0.0
    print("Rotate the lens barrel; maximize the number. Ctrl-C to quit.\n")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                continue
            crop = head_crop(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            sharp = cv2.Laplacian(crop, cv2.CV_64F).var()
            peak = max(peak, sharp)
            bar = "#" * int(min(sharp / 60.0, 1.0) * 40)
            print(f"\rsharpness {sharp:7.1f}  peak {peak:7.1f}"
                  f"  |{bar:<40}|", end="", flush=True)
    except KeyboardInterrupt:
        print(f"\n\nbest seen: {peak:.1f}")
    finally:
        cap.release()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", nargs="?", type=int, default=0,
                    help="video device index (default 0)")
    ap.add_argument("--note", default="",
                    help='what you changed, e.g. "1/4 CW" (logged)')
    ap.add_argument("--live", action="store_true",
                    help="continuous readout for bench focusing")
    args = ap.parse_args()
    if args.live:
        live(args.device)
    else:
        measure(args.device, args.note)
