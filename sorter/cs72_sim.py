"""Event-level simulator of the CS7.2 firmware + the machine's case physics.

As a port of Seth Hahner's GPL-3.0 CS7.2 firmware
(https://github.com/sjseth/AI-Case-Sorter-CS7.2), this file is a
derivative work and carries GPL-3.0 (see LICENSE at the repo root).

Ported command-by-command from the user's exact firmware source
(CS72_Firmware.ino, 7.2.250925.6.1): the qPos1/qPos2 shift register in
moveSorterToNextPosition, the prox-gated scheduleRun, forced feeds (xf:),
sortto:, stop's side effects (including its swallowed "done"), and the
home-at-slot-0 re-sync. Where the .ino steps a motor, this advances a
station model; where it delays, this doesn't — placement is a pure
function of command ORDER, which is exactly what we need to prove.

Physical model (bench truth: it takes TWO feeds to move a case
from the prox sensor to the camera — there is an intermediate pocket):

    collator tube -> NEST (prox sensor) -> [pocket] -> CAMERA -> DROP -> falls

One feed cycle advances every case one station; the case at the DROP port
falls into whatever slot the arm is under during that advance. A case
photographed after feed k still physically drops during feed k+2 — the
drop-side geometry is what the firmware's one-command queue delay
compensates, which is why mid-run placement is always correct. The
sensor-side distance (`sensor_to_camera`, default 2) is what the queue can
NOT see: at end of brass it strands a case that passed the sensor but was
never photographed, and at run start the camera stays empty until the
first case has walked the whole way in. Both ends are the app's problem,
and this simulator exists to prove the app's answers.

Interface matches FakeCs72Link (write/readline/close), so it plugs straight
into Cs72Transport and the selftest loops exercise the REAL translation
layer. Extra inspection surface for tests: bins, fell, wheel state, and
drift_events (any place the physical arm and the firmware's queue state
disagree — should always stay empty).

Both prior flush strategies were shipped on untested drop-timing models and
misplaced real brass (PRs #50/#52, reverted in #53). Nothing motion-related
touches the run loop again unless it passes here first.
"""
import collections

from .cs72 import FakeCs72Link

MICRO = 16          # SORT_MICROSTEPS / FEED_MICROSTEPS (informational only)


class DelayedCase:
    """A hopper entry that keeps the nest dry for `polls` waiting-line polls
    before seating — models brass still falling from the collator (the
    transient the firmware's debounce logic exists for)."""

    def __init__(self, label, polls):
        self.label = label
        self.polls = polls


class TimedCase:
    """A hopper entry that arrives after `polls` readline polls once it
    reaches the hopper head — REGARDLESS of gate state. Models a collator
    that keeps delivering while the run has already concluded end-of-brass
    (the false-dry field incident); DelayedCase can't express that, since
    it only counts down while the gate is dry."""

    def __init__(self, label, polls):
        self.label = label
        self.polls = polls


class Cs72Sim:
    """The CS7.2 board + wheel + collator, at command/event granularity."""

    def __init__(self, hopper=(), nest=None, camera=None, drop=None,
                 sensor_to_camera=2):
        # --- physical state ---
        self.hopper = collections.deque(hopper)  # cases still in the collator
        self.nest = nest          # case sitting on the prox sensor
        # pockets between the sensor and the camera: a case needs
        # `sensor_to_camera` feeds to travel nest -> camera (bench: 2)
        self.between = [None] * (sensor_to_camera - 1)
        self.camera = camera      # case at the camera port
        self.drop = drop          # case at the drop port (falls on next feed)
        self.arm = 0              # physical arm slot (homed at boot)
        self.bins = collections.defaultdict(list)  # slot -> fallen cases
        self.fell = []            # (case, slot) in fall order
        self.feeds = 0            # completed feed cycles (incl. empty ones)

        # --- firmware state (names as in the .ino) ---
        self.qPos1 = 0
        self.qPos2 = 0
        self.FeedScheduled = False
        self.forceFeed = False
        self._jam_next = False

        self.drift_events = []    # (context, arm, qPos1) — must stay empty
        self._out = collections.deque()
        self._delay_pending = None      # DelayedCase counting down to the nest
        self._timed_pending = None      # TimedCase counting down to the nest
        self._refill_nest()
        self._out.append("Ready")

        # config/setters: reuse the fake link's tables for getconfig parity
        self.config = dict(FakeCs72Link.DEFAULT_CONFIG)

    # ------------------------------------------------------------------ api
    def arm_jam(self):
        """Next feed cycle grinds out an overtravel error instead of done."""
        self._jam_next = True

    def add_brass(self, *cases):
        self.hopper.extend(cases)
        if self.nest is None:
            self._refill_nest()
        self._pump()

    def wheel_cases(self):
        """Cases physically in the machine but not yet in a bin."""
        return [c for c in (self.nest, *self.between, self.camera, self.drop)
                if c is not None and not isinstance(c, DelayedCase)]

    # --------------------------------------------------------- link surface
    def write(self, s):
        for line in s.strip().splitlines():
            line = line.strip()
            if line:
                self._command(line)

    def readline(self, timeout=None):
        self._tick_timed_case()         # collator keeps delivering
        if self._out:
            return self._out.popleft()
        if self.FeedScheduled and not self._ready_to_feed():
            # firmware prints this ~1/s while the gate is dry; one per poll
            self._tick_delayed_case()
            self._pump()               # brass may have just landed
            if self._out:
                return self._out.popleft()
            return "waiting for brass"
        return None

    def close(self):
        pass

    # ------------------------------------------------------ checkSerial port
    def _command(self, s):
        if s == "stop":
            # .ino: clears the scheduled feed and sets FeedCycleComplete,
            # which makes onFeedComplete print a "done" even when idle —
            # the ack Cs72Transport swallows via _expect_stop_done
            self.FeedScheduled = False
            self._out.append("done")
            self.forceFeed = False       # cleared by onFeedComplete
            return
        if s[0].isdigit():               # .ino: isDigit(input[0])
            self._move_sorter_to_next_position(int(s))
            self.FeedScheduled = True
            self._pump()
            return
        if s.startswith("xf:"):
            self.forceFeed = True
            self._move_sorter_to_next_position(int(s.split(":", 1)[1]))
            self.FeedScheduled = True
            self._pump()
            return
        if s.startswith("sortto:"):
            self._move_sorter_to_position(int(s.split(":", 1)[1]))
            self._out.append("ok")
            return
        if s == "homesorter":
            # jogSorter + homing search: arm physically re-syncs to slot 0
            self.arm = 0
            self.qPos1 = self.qPos2 = 0
            self._out.append("ok")
            return
        if s == "homefeeder":
            self._out.append("ok")       # wheel already on a homing node
            return
        if s == "getconfig":
            import json
            self._out.append(json.dumps(self.config))
            return
        if s == "ping":
            self._out.append(" ok")
            return
        if s == "version":
            self._out.append(FakeCs72Link.FW_VERSION)
            return
        if ":" in s:
            key, _, val = s.partition(":")
            cfg_key = FakeCs72Link.SETTERS.get(key)
            if cfg_key is not None:
                try:
                    self.config[cfg_key] = int(float(val))
                except ValueError:
                    pass
            self._out.append("ok")
            return
        self._out.append("ok")           # firmware acks anything unknown

    # --------------------------------------------------- sorter (the queue)
    def _move_sorter_to_next_position(self, position):
        # .ino: steps = (qPos1 - qPos2) * sortSteps * MICRO; the arm moves to
        # the slot commanded one command AGO, then the queue shifts
        steps = self.qPos1 - self.qPos2
        if steps != 0:
            self.arm -= steps            # positive steps run toward slot 0
        self.qPos1, self.qPos2 = self.qPos2, position
        self._sort_arrived()

    def _move_sorter_to_position(self, position):
        # sortto: immediate move, queue collapsed to the target
        steps = self.qPos1 - position
        if steps != 0:
            self.arm -= steps
        self.qPos1 = self.qPos2 = position
        self._sort_arrived()

    def _sort_arrived(self):
        if self.arm != self.qPos1:
            # the firmware has no idea this happened; the sim flags it
            self.drift_events.append(("arm/queue disagree", self.arm, self.qPos1))
        if self.qPos1 == 0:
            self.arm = 0                 # runSortMotor homes every trip to 0

    # ----------------------------------------------------- feed (scheduleRun)
    def _ready_to_feed(self):
        return self.forceFeed or self.nest is not None

    def _pump(self):
        if self.FeedScheduled and self._ready_to_feed():
            self.FeedScheduled = False
            if self._jam_next:
                self._jam_next = False
                self.forceFeed = False
                self._out.append("error:feed overtravel detected")
                return
            self._feed_cycle()
            self._out.append("done")     # after notificationDelay, in reality
            self.forceFeed = False       # onFeedComplete

    def _feed_cycle(self):
        """One wheel advance: every case moves one station; the drop-port
        case falls into the slot the arm is under RIGHT NOW."""
        self.feeds += 1
        if self.drop is not None:
            self.bins[self.arm].append(self.drop)
            self.fell.append((self.drop, self.arm))
        self.drop = self.camera
        if self.between:
            self.camera = self.between[-1]
            self.between = [self.nest] + self.between[:-1]
        else:
            self.camera = self.nest
        self.nest = None
        self._refill_nest()

    def _refill_nest(self):
        if self.nest is not None or not self.hopper:
            return
        nxt = self.hopper[0]
        if isinstance(nxt, DelayedCase):
            self._delay_pending = nxt    # counts down in readline polls
            return
        if isinstance(nxt, TimedCase):
            self._timed_pending = nxt    # counts down on EVERY poll
            return
        self.nest = self.hopper.popleft()

    def _tick_timed_case(self):
        d = self._timed_pending
        if d is None:
            return
        d.polls -= 1
        if d.polls <= 0:
            self.hopper.popleft()
            self._timed_pending = None
            self.nest = d.label
            self._pump()                 # the arrival may unblock a feed

    def _tick_delayed_case(self):
        d = self._delay_pending
        if d is None:
            return
        d.polls -= 1
        if d.polls <= 0:
            self.hopper.popleft()
            self._delay_pending = None
            self.nest = d.label


class Cs72ForkSim(Cs72Sim):
    """The SortIQ firmware fork (7.2.250925.6.2-SS2), at the same
    event granularity: the per-slot position table, its setters, feedstats
    telemetry, the fork getconfig keys, and the SS2 pipelined feed
    (pf / ps:<slot> — the slotQueued guard ported line-for-line). Motion
    DYNAMICS (trapezoid ramps, two-stage homing, feed launch profile) have
    no event-level footprint — the desk-check for those is the compile +
    code review + bench; what this sim proves is that the fork's PROTOCOL
    is a strict superset and the queue/placement semantics are untouched.

    Serial-ordering fidelity for the pipeline: on the real board, ps: sent
    mid-cycle waits in the Uno's hardware buffer and is processed after
    the cycle completes (checkSerial is gated off during motion), and
    DURING a dry "waiting for brass" spell checkSerial runs, so ps: lands
    immediately. This sim's synchronous write() reproduces exactly that
    ordering: pf runs its whole cycle (or parks on the dry gate) before
    the next write is seen."""

    FW_VERSION = "7.2.250925.6.2-SS2"
    MAX_SLOTS = 12
    FORK_CONFIG = {"SortAccelFactor": 1200, "SortHomeBackoff": 32,
                   "SortHomeSlowDelay": 900, "FeedLaunchSteps": 48,
                   "FeedDecelOverOffset": 1, "ArmDwellMs": 0,
                   "MaxSlots": MAX_SLOTS}
    FORK_SETTERS = {"sortaccel": "SortAccelFactor",
                    "sorthomebackoff": "SortHomeBackoff",
                    "sorthomeslow": "SortHomeSlowDelay",
                    "feedlaunch": "FeedLaunchSteps",
                    # armdwell: extra hold before every arm move (v6.2);
                    # pure time — no event-level footprint beyond the
                    # config round-trip this table provides
                    "armdwell": "ArmDwellMs"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config.update(self.FORK_CONFIG)
        self.slot_positions = []
        self._fill_slot_positions()
        self.arm_usteps = 0              # physical arm position, microsteps
        self.next_homing_steps = 96      # injectable feedstats seek length
        self.last_homing = 0
        self.max_homing = 0
        self.slotQueued = True           # .ino: boot queue 0/0 is assigned

    def _fill_slot_positions(self):
        step = self.config["SortSteps"] * MICRO
        self.slot_positions = [i * step for i in range(self.MAX_SLOTS)]

    def _clamp_slot(self, i):
        return max(0, min(i, self.MAX_SLOTS - 1))

    # ---- fork commands ----------------------------------------------------
    def _command(self, s):
        if s.startswith("ps:"):
            # silent by design: a reply would interleave with the cycle's
            # done; misuse is audited by the pf guard below
            self.qPos2 = int(s.split(":", 1)[1])
            self.slotQueued = True
            return
        if s == "pf" or s.startswith("pf"):
            if not self.slotQueued:
                self._out.append("error:no slot queued")
                return
            self._move_sorter_to_next_position(self.qPos2)
            self.slotQueued = False      # placeholder until the next ps:
            self.FeedScheduled = True
            self._pump()
            return
        if s.startswith("slotpos:"):
            parts = s.split(":")
            if len(parts) == 3:
                idx, val = int(parts[1]), int(parts[2])
                if 0 <= idx < self.MAX_SLOTS:
                    self.slot_positions[idx] = val
            self._out.append("ok")
            return
        if s == "feedstats":
            import json
            self._out.append(json.dumps(
                {"LastHomingSteps": self.last_homing,
                 "MaxHomingSteps": self.max_homing,
                 "FeedCycles": self.feeds}))
            return
        if s == "version":
            self._out.append(self.FW_VERSION)
            return
        if s.startswith("feeddecel:"):
            val = s.split(":", 1)[1].strip().lower()
            self.config["FeedDecelOverOffset"] = int(val in ("true", "1"))
            self._out.append("ok")
            return
        key = s.split(":", 1)[0]
        if key in self.FORK_SETTERS:
            try:
                self.config[self.FORK_SETTERS[key]] = int(float(s.split(":", 1)[1]))
            except ValueError:
                pass
            self._out.append("ok")
            return
        if s == "getconfig":
            import json
            cfg = dict(self.config)
            cfg["SlotPositions"] = ",".join(str(v) for v in self.slot_positions)
            self._out.append(json.dumps(cfg))
            return
        super()._command(s)
        if s.startswith("sortsteps:"):    # the fork refills the whole table
            self._fill_slot_positions()
        if s == "homesorter":             # .ino: queue rebuilt, assigned
            self.slotQueued = True

    # ---- fork motion: table lookup instead of index*sortSteps --------------
    def _move_sorter_to_next_position(self, position):
        super()._move_sorter_to_next_position(position)
        self.slotQueued = True           # .ino: every legacy caller passes a
        self._arm_arrived()              # real slot; pf overrides after

    def _move_sorter_to_position(self, position):
        super()._move_sorter_to_position(position)
        self.slotQueued = True           # .ino: sortto collapses to a real slot
        self._arm_arrived()

    def _arm_arrived(self):
        # the arm lands on the TABLE position of the slot the queue says it
        # is at; slot-index semantics (which the placement math runs on) are
        # identical to stock, which is exactly the compatibility claim
        self.arm_usteps = self.slot_positions[self._clamp_slot(self.qPos1)]

    def _feed_cycle(self):
        super()._feed_cycle()
        self.last_homing = self.next_homing_steps
        self.max_homing = max(self.max_homing, self.next_homing_steps)
