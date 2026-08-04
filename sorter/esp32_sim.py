"""Simulated ESP32-S3 firmware + virtual machine state.

Mirrors the real firmware's state machine (HOMING -> FEEDING -> WAIT_SEAT ->
WAIT_CMD -> back to FEEDING) and speaks the identical line protocol:

  sim -> app   SEATED | DONE | JAM | HOMED
  app -> sim   LIGHT:A | LIGHT:B | LIGHT:OFF | SORT:<n> | HOME | STATUS

VirtualMachine holds the physical truth the firmware can't know: which case
is actually in the nest (ground truth for scoring) and which bin it fell in.
The SyntheticCamera reads the same VirtualMachine, so the "photo" always
matches the case the sim just seated — exactly like the real nest.
"""
import random
import threading
import time

from .synth import CaseSpec


class VirtualMachine:
    def __init__(self, config, n_cases=50, jam_rate=0.0, seed=None):
        rng = random.Random(seed)
        state = random.getstate()
        random.seed(rng.random())
        self.hopper = [CaseSpec.random(config) for _ in range(n_cases)]
        random.setstate(state)
        self.jam_rate = jam_rate
        self.rng = rng
        self.current_case = None          # CaseSpec seated in the nest
        self.light = "OFF"                # A | B | OFF
        self.bins = {}                    # bin_id -> [CaseSpec, ...]
        self.results = []                 # (CaseSpec, bin_id) in sort order

    def feed_next(self):
        self.current_case = self.hopper.pop(0) if self.hopper else None
        return self.current_case

    def drop_into(self, bin_id):
        case, self.current_case = self.current_case, None
        self.bins.setdefault(bin_id, []).append(case)
        self.results.append((case, bin_id))


class Esp32Sim:
    FEED_TIME = 0.08     # s to advance one case into the nest
    SORT_TIME = 0.04     # s per bin position moved
    DROP_TIME = 0.06     # s solenoid actuation

    def __init__(self, machine):
        self.m = machine
        self.transport = None
        self.disc_pos = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._state = "HOMING"
        self._lock = threading.Lock()

    def attach(self, transport):
        self.transport = transport
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---- app -> firmware --------------------------------------------------
    def handle_line(self, line):
        if line.startswith("LIGHT:"):
            self.m.light = line.split(":", 1)[1]
        elif line.startswith("SORT:"):
            bin_id = int(line.split(":", 1)[1])
            threading.Thread(target=self._do_sort, args=(bin_id,), daemon=True).start()
        elif line == "HOME":
            with self._lock:
                self._state = "HOMING"
        elif line == "STATUS":
            self.transport.emit(f"STATE:{self._state} DISC:{self.disc_pos}")

    # ---- firmware behavior -------------------------------------------------
    def _do_sort(self, bin_id):
        time.sleep(abs(bin_id - self.disc_pos) * self.SORT_TIME + self.DROP_TIME)
        self.disc_pos = bin_id
        self.m.drop_into(bin_id)
        self.transport.emit("DONE")
        with self._lock:
            self._state = "FEEDING"

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                state = self._state
            if state == "HOMING":
                time.sleep(0.05)
                self.disc_pos = 0
                self.transport.emit("HOMED")
                with self._lock:
                    self._state = "FEEDING"
            elif state == "FEEDING":
                time.sleep(self.FEED_TIME)
                if self.m.rng.random() < self.m.jam_rate:
                    self.transport.emit("JAM")
                    with self._lock:
                        self._state = "JAMMED"   # waits for HOME
                    continue
                if self.m.feed_next() is None:
                    self.transport.emit("EMPTY")  # hopper exhausted (sim-only signal)
                    with self._lock:
                        self._state = "IDLE"
                    continue
                self.transport.emit("SEATED")
                with self._lock:
                    self._state = "WAIT_CMD"
            else:
                time.sleep(0.02)
