"""Regenerate stamp/ and crimp/ training crops from the raw/ originals.

Thin CLI wrapper around sorter.dataset.rebuild_crops — the web UI calls the
same function after every dataset edit.

Usage: python tools/rebuild_crops.py [--data data/real]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sorter.config import Config
from sorter.dataset import dataset_counts, rebuild_crops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/real")
    ap.add_argument("--config", default="config.json")
    args = ap.parse_args()
    Config(args.config)   # applies pocket/donut crop params process-wide
    root = Path(args.data)
    n_stamp, n_crimp = rebuild_crops(root)
    print(f"rebuilt {n_stamp} stamp crops, {n_crimp} crimp crops from {root / 'raw'}")
    for sub in ("stamp", "crimp"):
        for name, n in dataset_counts(root / sub).items():
            print(f"  {sub}/{name}: {n}")


if __name__ == "__main__":
    main()
