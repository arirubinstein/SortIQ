"""Probe whether a UVC camera's exposure/gain controls actually work.

Born from an OV3660/Sonix investigation: that bridge ACCEPTS
every exposure value and changes nothing — and in MJPG mode even gain is
dead, while YUYV has working gain and 18x more light from its slower
readout. The lessons this script encodes:

  * "driver accepts the value" proves nothing — measure frame brightness
  * some bridges only latch controls at stream start — reopen per value
  * test BOTH fourccs; the ISP path can differ per format
  * run an AE-ON control sweep afterward — it should be flat; if the
    manual sweep is also flat, the control is decorative

Run ON the Pi with the SortIQ service stopped (it owns the device):

    sudo systemctl stop sortiq
    .venv/bin/python tools/camera_probe.py [device_index]
    sudo systemctl start sortiq

Verdict guide: a working exposure control shows mean brightness scaling
with the value in MANUAL mode and flat in AE mode. Use it on the next
camera (LightBurn 4K-W / Weinan WN-L2101.K203L, Sony IMX415) before
trusting any knob.
"""
import subprocess
import sys
import time

import cv2
import numpy as np

DEV = int(sys.argv[1]) if len(sys.argv) > 1 else 0
EXPOSURES = (10, 50, 156, 312, 625, 1250, 2500, 5000)
GAINS = (0, 25, 50, 100)


def vctl(args):
    return subprocess.run(f"v4l2-ctl -d /dev/video{DEV} {args}", shell=True,
                          capture_output=True, text=True).stdout.strip()


def fresh_frame(fourcc, width=1280, height=720, skip=6):
    """Brand-new open + stream start per capture (covers latch-at-stream-on
    bridges), returns the (skip+1)-th frame."""
    cap = cv2.VideoCapture(DEV, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    frame = None
    for _ in range(skip + 1):
        ok, f = cap.read()
        if ok:
            frame = f
    cap.release()
    return frame


def stats(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return (round(float(g.mean()), 1), int(np.percentile(g, 99)),
            round(100 * float((g > 250).sum()) / g.size, 1))


def sweep(fourcc, ctrl, values, label):
    print(f"\n--- {fourcc}: {label} ---")
    print(f"{'value':>6} {'accepted':>9} {'mean':>7} {'p99':>5} {'clip%':>6}")
    readings = []
    for v in values:
        vctl(f"--set-ctrl {ctrl}={v}")
        got = vctl(f"--get-ctrl {ctrl}").split(":")[-1].strip()
        img = fresh_frame(fourcc)
        if img is None:
            print(f"{v:>6} {got:>9}   (no frame)")
            continue
        m, p99, clip = stats(img)
        readings.append(m)
        print(f"{v:>6} {got:>9} {m:>7} {p99:>5} {clip:>6}")
    if len(readings) >= 2:
        spread = max(readings) - min(readings)
        verdict = "WORKS" if spread > 15 else "DECORATIVE (flat)"
        print(f"  -> brightness spread {spread:.1f}: {ctrl} {verdict}")
    time.sleep(0.3)


print("controls advertised by the driver:")
print(vctl("--list-ctrls"))

for fourcc in ("MJPG", "YUYV"):
    print(f"\n================ {fourcc} ================")
    vctl("--set-ctrl auto_exposure=1")
    ae = vctl("--get-ctrl auto_exposure")
    print(f"manual AE requested -> {ae}")
    sweep(fourcc, "exposure_time_absolute", EXPOSURES, "exposure, manual AE")
    sweep(fourcc, "gain", GAINS, "gain, manual AE")
    vctl("--set-ctrl gain=0")
    vctl("--set-ctrl auto_exposure=3")
    time.sleep(1.0)
    sweep(fourcc, "exposure_time_absolute", EXPOSURES[:4],
          "CONTROL: exposure with AE on (expect flat)")

vctl("--set-ctrl gain=0")
vctl("--set-ctrl auto_exposure=3")
print("\nrestored: AE auto, gain 0. Restart the service:"
      " sudo systemctl start sortiq")
