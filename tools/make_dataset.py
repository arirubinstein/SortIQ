"""Generate a synthetic training dataset.

Layout (mirrors the app's real-brass dataset layout, so the training
tools work on either):

  data/synth/stamp/<LABEL>/nnnn_A.png        head crop
  data/synth/stamp/OTHER/nnnn_A.png          unknown headstamps (open-set class)

Usage: python tools/make_dataset.py [--per-class 300] [--out data/synth]
"""
import argparse
import random
import string
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sorter.config import Config
from sorter.synth import CaseSpec, render
from sorter import imaging


def save(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-class", type=int, default=300)
    ap.add_argument("--out", default="data/synth")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    random.seed(args.seed)
    cfg = Config(args.config)
    out = Path(args.out)

    # known stamps
    for label in cfg.stamp_labels:
        for i in range(args.per_class):
            spec = CaseSpec(label, crimped=False)
            img = render(spec, "A", seed=random.randrange(1 << 30))
            save(imaging.crop_head(img), out / "stamp" / label / f"{i:04d}_A.png")
        print(f"stamp/{label}: {args.per_class}")

    # stage 1: OTHER — random unknown headstamps so the model learns an
    # explicit reject class instead of force-fitting strangers to known bins
    known = set(cfg.stamp_labels)
    for i in range(args.per_class):
        while True:
            fake = "".join(random.choices(string.ascii_uppercase, k=random.randint(2, 4)))
            if fake not in known:
                break
        spec = CaseSpec(fake, crimped=False)
        img = render(spec, "A", seed=random.randrange(1 << 30))
        save(imaging.crop_head(img), out / "stamp" / "OTHER" / f"{i:04d}_A.png")
    print(f"stamp/OTHER: {args.per_class}")

if __name__ == "__main__":
    main()
