"""Synthetic case-head renderer.

Draws a plausible fired-brass case head: brass disc, embossed headstamp text
arranged around the rim, seated spent primer, and (optionally) a crimp —
either a ring swage or 4 stab stakes — plus an optional colored sealant
annulus. Renders two lighting variants matching the real rig:

  frame "A" (coaxial/diffuse): even light, text legible, crimp faint
  frame "B" (raking):          directional shading, crimp shadows strong

This exists to validate the *pipeline* (capture -> crop -> two-stage
inference -> bin decision), not to predict real-world accuracy. Real
accuracy comes from real photos collected with tools/collect.py.
"""
import math
import random

import cv2
import numpy as np

IMG = 480  # rendered frame is IMG x IMG


class CaseSpec:
    """Ground truth for one synthetic case."""

    def __init__(self, stamp, crimped, sealant=None, rotation=None):
        self.stamp = stamp            # headstamp text, e.g. "LC"
        self.crimped = crimped        # bool
        self.sealant = sealant        # None or BGR tuple
        self.rotation = rotation if rotation is not None else random.uniform(0, 360)

    @staticmethod
    def random(config):
        # crimp detection retired — random cases render uncrimped; the
        # crimp visuals remain for hand-built CaseSpecs (sim variety)
        stamp = random.choice(config.stamp_labels)
        return CaseSpec(stamp, False, None)


def _brass_base(rng):
    """Brass disc with rim groove on a dark background."""
    img = np.full((IMG, IMG, 3), 18, np.uint8)
    c = IMG // 2
    brass = (90 + rng.randint(-8, 8), 160 + rng.randint(-10, 10), 190 + rng.randint(-10, 10))
    cv2.circle(img, (c, c), 210, brass, -1, cv2.LINE_AA)
    cv2.circle(img, (c, c), 210, tuple(int(v * 0.55) for v in brass), 6, cv2.LINE_AA)   # rim edge
    cv2.circle(img, (c, c), 176, tuple(int(v * 0.75) for v in brass), 4, cv2.LINE_AA)   # extractor groove
    return img, brass


def _emboss_text(img, text, rotation, radius=140):
    """Stamped lettering around the top arc: dark glyph + light offset = embossed."""
    c = IMG // 2
    n = max(len(text), 1)
    arc = min(30 * n, 150)  # degrees of arc the text occupies
    for i, ch in enumerate(text):
        ang = math.radians(rotation - arc / 2 + (arc / (n - 1)) * i if n > 1
                           else rotation)
        x = int(c + radius * math.sin(ang))
        y = int(c - radius * math.cos(ang))
        glyph_rot = math.degrees(ang)
        _stamp_char(img, ch, (x, y), glyph_rot)


def _stamp_char(img, ch, center, rot_deg):
    size = 90
    canvas = np.zeros((size, size), np.uint8)
    cv2.putText(canvas, ch, (18, 66), cv2.FONT_HERSHEY_SIMPLEX, 2.0, 255, 8, cv2.LINE_AA)
    M = cv2.getRotationMatrix2D((size / 2, size / 2), -rot_deg, 1.0)
    canvas = cv2.warpAffine(canvas, M, (size, size))
    x0, y0 = center[0] - size // 2, center[1] - size // 2
    if x0 < 0 or y0 < 0 or x0 + size > IMG or y0 + size > IMG:
        return
    roi = img[y0:y0 + size, x0:x0 + size]
    mask = canvas > 100
    # incised bottom (dark) with a light catch-edge shifted up-left
    shifted = np.roll(np.roll(mask, -2, 0), -2, 1)
    roi[shifted & ~mask] = np.clip(roi[shifted & ~mask].astype(int) + 55, 0, 255)
    roi[mask] = (roi[mask] * 0.45).astype(np.uint8)


def _primer(img, spec, raking, rng):
    c = IMG // 2
    pr = 62  # primer radius in px
    tone = 150 + rng.randint(-10, 10)
    primer_col = (tone - 20, tone - 5, tone)  # slightly silvery vs brass
    cv2.circle(img, (c, c), pr, primer_col, -1, cv2.LINE_AA)
    # firing pin dent
    dent = (c + rng.randint(-8, 8), c + rng.randint(-8, 8))
    cv2.circle(img, dent, 16, tuple(int(v * 0.55) for v in primer_col), -1, cv2.LINE_AA)
    # pocket edge shadow
    cv2.circle(img, (c, c), pr + 2, (60, 70, 80), 2, cv2.LINE_AA)

    if spec.sealant is not None:
        cv2.circle(img, (c, c), pr + 4, spec.sealant, 5, cv2.LINE_AA)

    if spec.crimped:
        depth = 1.9 if raking else 1.25  # raking light deepens crimp shadows
        if rng.random() < 0.5:
            # ring swage: continuous dark circle just outside the pocket
            shade = tuple(int(v / depth) for v in (95, 150, 175))
            cv2.circle(img, (c, c), pr + 10, shade, 7 if raking else 4, cv2.LINE_AA)
        else:
            # stab crimp: 4 stakes at the case's rotation
            for k in range(4):
                ang = math.radians(spec.rotation + 90 * k)
                x = int(c + (pr + 11) * math.sin(ang))
                y = int(c - (pr + 11) * math.cos(ang))
                col = tuple(int(v / depth) for v in (90, 140, 165))
                axes = (11, 5) if raking else (9, 4)
                cv2.ellipse(img, (x, y), axes, math.degrees(ang), 0, 360, col, -1, cv2.LINE_AA)


def render(spec, light="A", seed=None):
    """Render one frame of the given case under light 'A' (coaxial) or 'B' (raking)."""
    rng = random.Random(seed)
    img, _ = _brass_base(rng)
    _emboss_text(img, spec.stamp, spec.rotation)
    _primer(img, spec, raking=(light == "B"), rng=rng)

    if light == "B":
        # raking light: directional brightness gradient + higher local contrast
        gy = np.linspace(1.25, 0.75, IMG).reshape(-1, 1, 1)
        img = np.clip(img.astype(np.float32) * gy, 0, 255).astype(np.uint8)
        img = cv2.addWeighted(img, 1.3, cv2.GaussianBlur(img, (0, 0), 9), -0.3, 0)

    # sensor noise + slight blur + exposure jitter
    noise = np.random.default_rng(seed).normal(0, 5, img.shape).astype(np.float32)
    img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    gain = 1.0 + rng.uniform(-0.08, 0.08)
    img = np.clip(img.astype(np.float32) * gain, 0, 255).astype(np.uint8)

    # small framing jitter, like a case not perfectly centered in the nest
    dx, dy = rng.randint(-8, 8), rng.randint(-8, 8)
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (IMG, IMG))
