"""Load and validate the sorter configuration for the ACTIVE model.

config.json holds only global settings (camera, serial, model input size) plus
an `active` pointer {cartridge, model}. Everything caliber- or model-specific
(labels, bins, floors, imaging/crop mode) lives in the active model's model.json
under calibers/. See sorter/profiles.py for the layout.
"""
import json
from pathlib import Path

from . import profiles


def normalize_bin(entry):
    """One slot's assignment -> list of stamp names.

    Slots hold LISTS so several variants of one make can share a physical
    chute (e.g. FC-01 + FC-07 -> slot 3, mirroring the original app's
    per-slot checkboxes). Legacy model.json entries were a single string,
    "UNMATCHED", or null — they normalize transparently and are written
    back as lists on the next save."""
    if entry is None or entry == "":
        return []
    if isinstance(entry, str):
        return [entry]
    return [s for s in entry if isinstance(s, str) and s]


class Config:
    def __init__(self, path="config.json"):
        path = Path(path)
        self.root = path.parent if path.parent != Path("") else Path(".")
        # migrate a legacy flat config.json on first load; returns global cfg
        raw = profiles.ensure_migrated(self.root, path)

        self.active = raw.get("active", {})
        self.cartridge = self.active.get("cartridge")
        self.model_name = self.active.get("model")
        mj = profiles.read_model(self.root, self.cartridge, self.model_name)

        self.stamp_labels = mj["stamp_labels"]
        # bins is the PHYSICAL layout: one slot per real chute, each holding
        # a LIST of stamp names (variants of one make can share a chute),
        # ["UNMATCHED"] for the catch-all, or [] (empty). Stamps without a
        # slot route to the unmatched bin at sort time. The machine section
        # says how many chutes exist (slots_total) and which the user
        # actually loads trays into (slots_enabled) — bin ids stay physical
        # slot numbers throughout, disabled slots just never get assigned.
        machine = raw.get("machine", {})
        self.slots_total = int(machine.get("slots_total") or mj.get("bin_count", 8))
        enabled = machine.get("slots_enabled") or []
        enabled = sorted({int(s) for s in enabled if 0 <= int(s) < self.slots_total})
        self.slots_enabled = enabled or list(range(self.slots_total))
        self.bin_count = self.slots_total
        self.bins = [normalize_bin(b) for b in
                     list(mj.get("bins") or [])[:self.bin_count]]
        self.bins += [[] for _ in range(self.bin_count - len(self.bins))]
        # families: a named group of classes assignable to a slot as one
        # unit. Slots store the intent as a "family:NAME" token (so future
        # members follow automatically); expansion to real classes happens
        # here, and ONLY here — the decider and run loop never see
        # families. An explicit class assignment always beats its
        # family's slot.
        self.families = {str(n): [s for s in normalize_bin(m)]
                         for n, m in (mj.get("families") or {}).items()}
        self.bin_map = {(s, None): i for i, group in enumerate(self.bins)
                        for s in group
                        if s != "UNMATCHED" and s in self.stamp_labels
                        and not s.startswith("family:")}
        for i, group in enumerate(self.bins):
            for s in group:
                if s.startswith("family:"):
                    for m in self.families.get(s[len("family:"):], []):
                        if (m in self.stamp_labels
                                and (m, None) not in self.bin_map):
                            self.bin_map[(m, None)] = i
        self.unmatched_bin = next((i for i, g in enumerate(self.bins)
                                   if "UNMATCHED" in g), self.bin_count - 1)
        self.floors = mj["floors"]
        self.imaging = mj.get("imaging", dict(profiles.DEFAULT_IMAGING))
        # classes the user benched from training (Dataset page checkbox):
        # still collected, labeled, in the gallery, and offered in reject
        # review — just left out of the next training passes until their
        # image count matures. The 10-image automatic minimum is separate
        # and still applies.
        self.train_disabled = [s for s in (mj.get("train_disabled") or [])
                               if s in self.stamp_labels]

        self.camera = raw.get("camera", {})
        self.serial = raw.get("serial", {})
        # per-model directories (dataset + trained weights live with the model)
        md = profiles.model_dir(self.root, self.cartridge, self.model_name)
        self.data_dir = md / "data"
        self.models_dir = md / "models"
        input_size = raw.get("models", {}).get("input_size", 160)
        self.models = {"stamp": str(self.models_dir / "stamp.tflite"),
                       "input_size": input_size}

        self._validate()
        # apply crop parameters process-wide so every caller of
        # imaging.crop_head()/crop_pocket() uses the active model's values
        from . import imaging
        imaging.configure(self.imaging)

    def _validate(self):
        problems = []
        assigned = [s for g in self.bins for s in g if s != "UNMATCHED"]
        if len(assigned) != len(set(assigned)):
            problems.append("a headstamp is assigned to more than one bin")
        if sum("UNMATCHED" in g for g in self.bins) != 1:
            problems.append("exactly one bin must be UNMATCHED")
        frac = self.imaging.get("pocket_frac", 0.42)
        if not 0.1 <= float(frac) <= 1.0:
            problems.append(f"imaging.pocket_frac {frac} outside 0.1–1.0")
        if problems:
            raise ValueError(f"model {self.cartridge}/{self.model_name} invalid:\n  "
                             + "\n  ".join(problems))
