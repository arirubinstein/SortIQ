"""Prove end-of-brass behavior against the CS7.2 firmware simulator.

Two flush strategies shipped on untested drop-timing models and misplaced
real brass (PR #50: park-slot force-feeds; PR #52: forced xf:N reissue —
both reverted in #53). This harness drives the REAL Cs72Transport over
sorter/cs72_sim.py with each historical run-loop strategy replayed from its
diff, and demands:

  1. the simulator reproduces mid-run correctness (why live runs are pure)
  2. it reproduces BOTH field failures — with the bench-true wheel geometry
     (sensor -> camera = 2 feeds) the #50 replay matches
     the original bug report LITERALLY: case #8 (HORNADY) into case #9's (MONARCH)
     slot, the last case into slot 0 (UNMATCHED)
  3. the new flush loop places every case in its true slot — including the
     tail case the run never photographs (it passes the sensor but hasn't
     reached the camera when the gate goes dry) — from cold starts, warm
     starts, every edge state we could think of, and a randomized fuzz
     across wheel geometries

Historical replays start with a WARM wheel (camera+pockets loaded from
prior collect/run activity) because that is how real runs
actually start; the warm leftover ahead of the first
photographed case always falls into slot 0 (the catch-all) — true in the
field too, where it would sit unnoticed in the UNMATCHED tray.

Only a green run here earns motion changes a trip to the machine.

Usage: python tools/cs72_flush_selftest.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sorter.cs72 import Cs72Transport, PARK_SLOT, cancel_wait
from sorter.cs72_sim import Cs72Sim, Cs72ForkSim, DelayedCase

WAIT_LIMIT = 4      # matches webui/server.py
FLUSH_FEEDS = 4     # PR #50/#52 budget
FLUSH_WAITS = 3     # consecutive WAITINGs on a sort reply before flushing
EMPTY_FEEDS = 4     # forced feeds for empty-camera photos: the cold-start
                    # walk-in and mid-wheel bubbles from collator hiccups


def _rl(t, tries=8):
    """readline with a few zero-timeout retries: translated-away lines (ok,
    swallowed dones) surface as None ticks even when more output is queued."""
    for _ in range(tries):
        line = t.readline(timeout=0)
        if line is not None:
            return line
    return None


def _sort_reply(t):
    """Mirror of the run loop's sort_reply(): DONE, JAM, WAITING (means
    FLUSH_WAITS consecutive dry lines) or None."""
    reply, waits = None, 0
    for _ in range(80):
        reply = _rl(t)
        if reply is None or reply in ("DONE", "JAM"):
            return reply
        if reply == "WAITING":
            waits += 1
            if waits >= FLUSH_WAITS:
                return reply
    return reply


def _await_feed(t):
    """Mirror of the run loop's await_feed(): SEATED, JAM or None."""
    for _ in range(60):
        line = _rl(t)
        if line in ("SEATED", "JAM", None):
            return line
    return None


def drive(sim, strategy, slots_of, max_iters=500):
    """Replay one run-loop strategy over the sim; protocol-faithful port of
    the server.py loop as it existed in each PR ("flush" = current)."""
    t = Cs72Transport(sim)
    counted, jams = [], 0
    end_reason, flushed = None, 0
    waiting, flush_left = 0, FLUSH_FEEDS
    prev_slot = PARK_SLOT               # the prime feed's slot
    dry = False                         # current strategy's flush-loop mode
    empty_left = EMPTY_FEEDS
    it = 0
    while it < max_iters:
        it += 1
        line = _rl(t)
        if line is None:
            end_reason = end_reason or "stall"
            break
        if line == "WAITING":
            waiting += 1
            if strategy in ("pr50", "pr52"):
                if waiting >= WAIT_LIMIT:
                    if flush_left <= 0:
                        end_reason = "out_of_brass"
                        break
                    flush_left -= 1
                    waiting = 0
                    t.send("FORCE_FEED")
            else:                        # pr53 + flush: idle waits = stop
                if waiting >= WAIT_LIMIT:
                    end_reason = "out_of_brass"
                    break
            continue
        if line == "JAM":
            jams += 1
            t.send("HOME")
            prev_slot = PARK_SLOT        # HOME re-primes with xf:PARK_SLOT
            continue
        if line != "SEATED":
            continue
        waiting = 0

        case = sim.camera                # the photograph
        if case is None:                 # empty camera in frame
            if strategy in ("pr50", "pr52"):
                if flush_left <= 0:
                    end_reason = "out_of_brass"
                    break
                flush_left -= 1
                t.send("FEED")
                continue
            if strategy in ("flush", "pipeline"):
                if dry:                  # flush loop done: final forced
                    t.send("FEED")       # feed drops the last counted case
                    ok = _await_feed(t) == "SEATED"
                    end_reason = "out_of_brass" if ok else "flush_jam"
                    break
                if empty_left > 0:       # cold-start walk-in OR a bubble
                    empty_left -= 1      # from a collator hiccup: forced
                    t.send("FEED")       # feed self-heals the alignment
                    continue
            end_reason = "out_of_brass"
            break
        flush_left = FLUSH_FEEDS
        empty_left = EMPTY_FEEDS
        slot = slots_of[case]

        if strategy == "pr53":
            t.send(f"SORT:{slot}")
            reply = None
            for _ in range(60):
                reply = _rl(t)
                if reply in ("DONE", "WAITING", "JAM", None):
                    break
            if reply == "WAITING":
                end_reason = "out_of_brass"
                break
            if reply != "DONE":
                if reply == "JAM":
                    jams += 1
                t.send("HOME")
                prev_slot = PARK_SLOT
                continue
            counted.append((case, slot))
            prev_slot = slot

        elif strategy == "pr50":
            t.send(f"SORT:{slot}")
            reply = None
            for _ in range(60):
                reply = _rl(t)
                if reply in ("DONE", "WAITING", "JAM", None):
                    break
            if reply == "WAITING":
                waiting = 1              # ...and the case still counts (#50)
            elif reply != "DONE":
                if reply == "JAM":
                    jams += 1
                t.send("HOME")
                prev_slot = PARK_SLOT
                continue
            counted.append((case, slot))
            prev_slot = slot

        elif strategy == "pr52":
            t.send(f"SORT:{slot}")
            reply, fsort_sent = None, False
            for _ in range(60):
                reply = _rl(t)
                if reply is None or reply in ("DONE", "JAM"):
                    break
                if reply == "WAITING" and not fsort_sent:
                    fsort_sent = True
                    t.send(f"FSORT:{slot}")
            if reply != "DONE":
                if reply == "JAM":
                    jams += 1
                t.send("HOME")
                prev_slot = PARK_SLOT
                continue
            counted.append((case, slot))
            prev_slot = slot

        elif strategy in ("flush", "pipeline"):  # mirrors of the run loop:
            # "flush" = the sequential path; "pipeline" = the SS2 path,
            # where PFEED goes out before classification and PSLOT lands
            # mid-cycle. Everything downstream (WAITING/cancel/dry flush/
            # JAM) is byte-identical between the two — that equivalence is
            # what the pipeline scenarios assert.
            if strategy == "pipeline" and not dry:
                t.send("PFEED")          # mechanics start before the slot
                t.send(f"PSLOT:{slot}")  # classification arrives mid-cycle
            else:
                t.send(f"FLUSH:{prev_slot}:{slot}" if dry else f"SORT:{slot}")
            reply = _sort_reply(t)
            if reply == "WAITING":
                outcome = cancel_wait(t)     # the PRODUCTION cancel
                if outcome == "resumed":
                    reply = "DONE"
                elif outcome == "clean":
                    dry = True
                    t.send(f"FLUSH:{prev_slot}:{slot}")
                    reply = _sort_reply(t)
                else:
                    reply = "JAM"
            if reply != "DONE":
                if reply == "JAM":
                    jams += 1
                if dry:                  # never home-and-refeed blind here
                    end_reason = "out_of_brass"
                    break
                t.send("HOME")
                prev_slot = PARK_SLOT
                continue
            counted.append((case, slot))
            prev_slot = slot
            if dry:
                flushed += 1
        else:
            raise ValueError(strategy)

    return {"counted": counted, "end_reason": end_reason, "jams": jams,
            "flushed": flushed, "iters": it}


# --------------------------------------------------------------- assertions
def placements(sim, slots_of):
    """(case, want, got) for every case that physically fell."""
    return [(c, slots_of.get(c), s) for c, s in sim.fell]


def misplaced(sim, slots_of):
    """Cases with a known slot that fell somewhere else."""
    return [(c, w, g) for c, w, g in placements(sim, slots_of)
            if w is not None and w != g]


def base_checks(sim, name):
    errs = []
    if sim.drift_events:
        errs.append(f"{name}: arm/queue drift {sim.drift_events}")
    return errs


# ---------------------------------------------------------------- scenarios
TEN = [("c1", 1), ("c2", 2), ("c3", 3), ("c4", 4), ("c5", 5),
       ("c6", 6), ("c7", 7), ("c8", 2), ("c9", 5), ("c10", 3)]


def warm_ten(cls=Cs72Sim):
    """The warm-start condition: wheel warm from prior activity —
    c1 already at the camera, c2 in the intermediate pocket, c3 on the
    sensor. c1 is ahead of the first photograph, so every strategy sends
    it to slot 0 (the catch-all) — the unavoidable warm-start leftover."""
    slots = dict(TEN)
    sim = cls(hopper=[c for c, _ in TEN[3:]], nest="c3", camera="c1")
    sim.between = ["c2"]
    return sim, slots


def scenario_mid_run_pr53():
    """Baseline: #53 on a warm wheel. Mid-run all correct; c9's sort answers
    waiting, so the run counts c2..c8 and leaves THREE stragglers — the
    '~3 cases past the sensor' the #53 banner promised."""
    sim, slots = warm_ten()
    r = drive(sim, "pr53", slots)
    errs = base_checks(sim, "pr53")
    bad = misplaced(sim, slots)
    if bad != [("c1", 1, 0)]:            # warm leftover -> catch-all only
        errs.append(f"pr53: expected only the warm leftover in slot 0, "
                    f"got {bad}")
    if [c for c, _ in r["counted"]] != [f"c{i}" for i in range(2, 9)]:
        errs.append(f"pr53: expected c2..c8 counted, got {r['counted']}")
    if sorted(sim.wheel_cases()) != ["c10", "c8", "c9"]:
        errs.append(f"pr53: stragglers should be c8+c9+c10, got "
                    f"{sim.wheel_cases()}")
    if r["end_reason"] != "out_of_brass":
        errs.append(f"pr53: end_reason {r['end_reason']}")
    return errs


def scenario_pr50_field_bug():
    """#50 must reproduce the original bug report LITERALLY under the bench-true
    geometry: case #8 (HORNADY) into case #9's (MONARCH) slot, and the
    last case (MONARCH) into slot 0 (UNMATCHED). c9 lands in c10's slot —
    invisible in the field because both were MONARCH."""
    sim, slots = warm_ten()
    drive(sim, "pr50", slots)
    errs = base_checks(sim, "pr50")
    bad = misplaced(sim, slots)
    expected_bad = [("c1", 1, 0),        # warm leftover -> catch-all
                    ("c8", 2, 5),        # case #8 into case #9's slot
                    ("c9", 5, 3),        # case #9 into case #10's slot
                    ("c10", 3, 0)]       # last case under the parked arm
    if bad != expected_bad:
        errs.append(f"pr50: field signature not reproduced: {bad}")
    if sim.wheel_cases():
        errs.append(f"pr50: wheel should be drained, got {sim.wheel_cases()}")
    return errs


def scenario_pr52_field_bug():
    """#52 commits the same root error (case #8 into #9's slot, #9 into
    #10's) and burns extra empty feed cycles; the last case lands right by
    accident."""
    sim, slots = warm_ten()
    drive(sim, "pr52", slots)
    errs = base_checks(sim, "pr52")
    bad = misplaced(sim, slots)
    expected_bad = [("c1", 1, 0), ("c8", 2, 5), ("c9", 5, 3)]
    if bad != expected_bad:
        errs.append(f"pr52: expected {expected_bad}, got {bad}")
    got = dict(sim.fell)
    if got.get("c10") != 3:
        errs.append(f"pr52: c10 should land in its own slot 3, got "
                    f"{got.get('c10')}")
    if sim.feeds <= 12:                  # the empty-cycle burn
        errs.append(f"pr52: expected extra empty feed cycles, got {sim.feeds}")
    return errs


def scenario_flush_warm_ten():
    """The money test, warm wheel: the current loop places c2..c10 ALL in
    their true slots (c8 included — the case every old strategy misfiled)
    and drains the wheel; only the warm leftover goes to the catch-all."""
    sim, slots = warm_ten()
    r = drive(sim, "flush", slots)
    errs = base_checks(sim, "flush-warm")
    bad = misplaced(sim, slots)
    if bad != [("c1", 1, 0)]:
        errs.append(f"flush-warm: misplaced {bad}")
    if len(sim.fell) != 10:
        errs.append(f"flush-warm: {len(sim.fell)}/10 placed: {sim.fell}")
    if sim.wheel_cases():
        errs.append(f"flush-warm: wheel not empty: {sim.wheel_cases()}")
    if r["end_reason"] != "out_of_brass" or not r["flushed"]:
        errs.append(f"flush-warm: end={r['end_reason']} "
                    f"flushed={r['flushed']}")
    return errs


def scenario_flush_cold_ten():
    """Tonight's field condition: cold wheel, brass on the sensor only.
    The prime budget walks the first case to the camera (2 feeds) instead
    of declaring out-of-brass at 0 cases; then all 10 sort true."""
    slots = dict(TEN)
    sim = Cs72Sim(hopper=[c for c, _ in TEN])
    r = drive(sim, "flush", slots)
    errs = base_checks(sim, "flush-cold")
    if misplaced(sim, slots):
        errs.append(f"flush-cold: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 10:
        errs.append(f"flush-cold: {len(sim.fell)}/10 placed "
                    f"(end={r['end_reason']}, counted={len(r['counted'])})")
    if sim.wheel_cases():
        errs.append(f"flush-cold: wheel not empty: {sim.wheel_cases()}")
    if len(r["counted"]) != 10:
        errs.append(f"flush-cold: counted {len(r['counted'])}/10")
    return errs


def scenario_flush_edges():
    """Edge sweeps, cold starts: tiny runs, slot-0 tails, equal tails,
    empty hopper."""
    cases = [
        [("a", 4)],                      # single case, prev = park slot
        [("a", 0)],                      # single case to slot 0
        [("a", 3), ("b", 3)],            # two cases, same slot
        [("a", 0), ("b", 5)],            # prev slot 0
        [("a", 5), ("b", 0)],            # last slot 0
        [("a", 0), ("b", 0)],            # both slot 0
        [("a", 2), ("b", 7), ("c", 7)],  # equal tail pair
        [],                              # empty hopper: prime finds nothing
    ]
    errs = []
    for i, spec in enumerate(cases):
        slots = dict(spec)
        sim = Cs72Sim(hopper=list(slots))
        r = drive(sim, "flush", slots)
        tag = f"edge{i}:{spec}"
        errs += base_checks(sim, tag)
        if misplaced(sim, slots):
            errs.append(f"{tag}: misplaced {misplaced(sim, slots)}")
        if len(sim.fell) != len(spec):
            errs.append(f"{tag}: {len(sim.fell)}/{len(spec)} placed "
                        f"(end={r['end_reason']})")
        if sim.wheel_cases():
            errs.append(f"{tag}: wheel not empty: {sim.wheel_cases()}")
    return errs


def scenario_flush_transient_dry():
    """A case still falling from the collator must NOT trigger the flush:
    the sort reply sees <FLUSH_WAITS waitings, then the feed completes."""
    slots = {"a": 3, "b": 6, "c": 1, "d": 4}
    sim = Cs72Sim(hopper=["a", "b", DelayedCase("c", 2), "d"])
    r = drive(sim, "flush", slots)
    errs = base_checks(sim, "transient")
    if misplaced(sim, slots):
        errs.append(f"transient: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 4:
        errs.append(f"transient: {len(sim.fell)}/4 placed ({r})")
    return errs


def scenario_flush_sneak_feed():
    """THE race that killed the single-shot flush design: brass lands on
    the sensor after the flush decision but before the stop — the waiting
    feed fires first. cancel_wait must answer 'resumed', the run continues,
    and every case (including the latecomer) still lands true."""
    slots = {"a": 3, "b": 6, "c": 1, "late": 2}
    sim = Cs72Sim(hopper=["a", "b", "c"])
    t = Cs72Transport(sim)
    # hand-drive: prime + one walk-in feed, sort a, sort b -> 3 waitings
    assert _rl(t) == "SEATED" and sim.camera is None
    t.send("FEED")
    assert _rl(t) == "SEATED" and sim.camera == "a"
    t.send("SORT:3")
    assert _rl(t) == "DONE" and _rl(t) == "SEATED" and sim.camera == "b"
    t.send("SORT:6")
    ws = [_rl(t) for _ in range(3)]
    assert ws == ["WAITING"] * 3, ws
    sim.add_brass("late")                # lands as we decide to flush:
    outcome = cancel_wait(t)             # the pending feed fires FIRST
    errs = base_checks(sim, "sneak")
    if outcome != "resumed":
        errs.append(f"sneak: expected resumed, got {outcome}")
    if _rl(t) != "SEATED":
        errs.append("sneak: expected SEATED after the resumed sort")
    # resume exactly as the loop would: sort c, then the gate is dry again
    # (late is still walking in) — flush loop takes both stragglers
    if sim.camera != "c":
        errs.append(f"sneak: expected c at the camera, got {sim.camera}")
    t.send("SORT:1")
    if _sort_reply(t) != "WAITING":
        errs.append("sneak: expected dry gate on resume")
    elif cancel_wait(t) != "clean":
        errs.append("sneak: second cancel should be clean")
    else:
        t.send("FLUSH:6:1")              # prev=b's slot, last=c's slot
        if _sort_reply(t) != "DONE" or _rl(t) != "SEATED":
            errs.append("sneak: flush of c did not complete")
        if sim.camera != "late":
            errs.append(f"sneak: expected late at camera, got {sim.camera}")
        t.send("FLUSH:1:2")              # prev=c's slot, last=late's slot
        if _sort_reply(t) != "DONE" or _rl(t) != "SEATED":
            errs.append("sneak: flush of late did not complete")
        t.send("FEED")                   # camera empty: closing feed
        if _await_feed(t) != "SEATED":
            errs.append("sneak: closing FEED did not complete")
    if misplaced(sim, slots):
        errs.append(f"sneak: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 4:
        errs.append(f"sneak: {len(sim.fell)}/4 placed: {sim.fell}")
    if sim.wheel_cases():
        errs.append(f"sneak: wheel not empty: {sim.wheel_cases()}")
    return errs


def scenario_flush_jam():
    """A jam at a FLUSH step must surface as JAM; the loop aborts with no
    further motion, so nothing drops blind (the single-shot design failed
    exactly here)."""
    slots = {"a": 3, "b": 6}
    sim = Cs72Sim(hopper=["a", "b"])
    t = Cs72Transport(sim)
    assert _rl(t) == "SEATED"            # prime: camera still empty
    t.send("FEED")
    assert _rl(t) == "SEATED" and sim.camera == "a"
    t.send("SORT:3")
    ws = [_sort_reply(t)]                # b never reaches the sensor gate
    assert ws == ["WAITING"], ws
    assert cancel_wait(t) == "clean"
    sim.arm_jam()                        # the FLUSH step's feed will grind
    t.send("FLUSH:0:3")
    reply = _sort_reply(t)
    errs = base_checks(sim, "jam")
    if reply != "JAM":
        errs.append(f"jam: expected JAM at the flush step, got {reply}")
    if sim.fell:
        errs.append(f"jam: nothing should have dropped, got {sim.fell}")
    if sorted(sim.wheel_cases()) != ["a", "b"]:
        errs.append(f"jam: both cases should stay in the wheel, got "
                    f"{sim.wheel_cases()}")
    return errs


def scenario_leftover_wheel():
    """Leftover cases in the wheel at run start (aborted previous run):
    they fall into slot 0 during the prime walk-in; fresh cases all land
    true and the wheel still drains."""
    slots = {"c1": 4, "c2": 6, "c3": 1, "c4": 5, "c5": 2}
    sim = Cs72Sim(hopper=list(slots), camera="leftoverX", drop="leftoverY")
    drive(sim, "flush", slots)
    errs = base_checks(sim, "leftover")
    if misplaced(sim, slots):
        errs.append(f"leftover: fresh cases misplaced: "
                    f"{misplaced(sim, slots)}")
    got = dict(sim.fell)
    stray = {got.get(c) for c in ("leftoverX", "leftoverY")}
    if stray != {PARK_SLOT}:
        errs.append(f"leftover: leftovers should land in the park slot, "
                    f"got {stray}")
    if len(sim.fell) != 7:
        errs.append(f"leftover: {len(sim.fell)}/7 placed: {sim.fell}")
    if sim.wheel_cases():
        errs.append(f"leftover: wheel not empty: {sim.wheel_cases()}")
    return errs


def scenario_fuzz():
    """Randomized cold runs across wheel geometries (sensor->camera 1..3
    feeds): any length, any slots, occasional collator lag — the flush
    loop must never misplace a case, ever, and must always drain the
    wheel."""
    rng = random.Random(20260711)
    errs = []
    for trial in range(300):
        n = rng.randint(0, 14)
        gap = rng.choice([1, 2, 2, 3])   # bench says 2; stay robust anyway
        spec = [(f"t{trial}c{i}", rng.randint(0, 7)) for i in range(n)]
        slots = dict(spec)
        hopper = []
        for i, (label, _) in enumerate(spec):
            # collator hiccups that recover in time; never the first case —
            # a real run starts with brass already on the sensor
            if i > 0 and rng.random() < 0.12:
                hopper.append(DelayedCase(label, rng.randint(1, FLUSH_WAITS - 1)))
            else:
                hopper.append(label)
        sim = Cs72Sim(hopper=hopper, sensor_to_camera=gap)
        r = drive(sim, "flush", slots)
        bad = misplaced(sim, slots)
        if bad:
            errs.append(f"fuzz#{trial} gap={gap} {spec}: misplaced {bad}")
        if len(sim.fell) != n:
            errs.append(f"fuzz#{trial} gap={gap} {spec}: {len(sim.fell)}/{n} "
                        f"placed (end={r['end_reason']})")
        if sim.wheel_cases():
            errs.append(f"fuzz#{trial}: wheel not empty {sim.wheel_cases()}")
        errs += base_checks(sim, f"fuzz#{trial}")
        if errs:
            break                        # first failure is enough detail
    return errs


def scenario_fork_protocol_superset():
    """The firmware fork must be a drop-in: the CURRENT run loop (prime
    walk-in, sorting, end-of-brass flush loop) runs green against the fork
    simulator with its default table — placement identical to stock."""
    slots = dict(TEN)
    sim = Cs72ForkSim(hopper=[c for c, _ in TEN])
    r = drive(sim, "flush", slots)
    errs = base_checks(sim, "fork-superset")
    if misplaced(sim, slots):
        errs.append(f"fork-superset: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 10 or sim.wheel_cases():
        errs.append(f"fork-superset: {len(sim.fell)}/10 placed, wheel "
                    f"{sim.wheel_cases()} (end={r['end_reason']})")
    return errs


def scenario_fork_slot_table():
    """slotpos overrides: a scrambled, non-uniform table must not disturb
    queue/placement semantics (slot INDEX routing is unchanged), and the
    arm must land on the table's microstep positions."""
    import json
    slots = {"a": 1, "b": 5, "c": 3}
    sim = Cs72ForkSim(hopper=list(slots))
    assert sim.readline() == "Ready"     # boot banner
    custom = [0, 290, 655, 1010, 1300, 1633, 1980, 2333, 2660, 3000, 3100, 3150]
    for i, v in enumerate(custom):
        sim.write(f"slotpos:{i}:{v}")
    errs = base_checks(sim, "fork-table")
    drained = [sim.readline() for _ in range(len(custom))]
    if set(drained) != {"ok"}:
        errs.append(f"fork-table: setter acks wrong: {drained}")
    if sim.slot_positions != custom:
        errs.append(f"fork-table: table not applied: {sim.slot_positions}")
    r = drive(sim, "flush", slots)
    if misplaced(sim, slots):
        errs.append(f"fork-table: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 3 or r["end_reason"] != "out_of_brass":
        errs.append(f"fork-table: {len(sim.fell)}/3 placed ({r['end_reason']})")
    if sim.arm_usteps not in custom:
        errs.append(f"fork-table: arm off-table at {sim.arm_usteps}")
    # getconfig must round-trip the table + fork keys
    sim.write("getconfig")
    cfg = json.loads(sim.readline())
    if cfg.get("SlotPositions") != ",".join(str(v) for v in custom):
        errs.append(f"fork-table: getconfig table mismatch: "
                    f"{cfg.get('SlotPositions')}")
    for key in ("SortAccelFactor", "SortHomeBackoff", "SortHomeSlowDelay",
                "FeedLaunchSteps", "FeedDecelOverOffset", "ArmDwellMs",
                "MaxSlots"):
        if key not in cfg:
            errs.append(f"fork-table: getconfig missing {key}")
    # sortsteps refills the whole table (documented: push slotpos AFTER)
    sim.write("sortsteps:25")
    sim.readline()
    if sim.slot_positions[1] != 25 * 16:
        errs.append(f"fork-table: sortsteps refill broken: "
                    f"{sim.slot_positions[:3]}")
    return errs


def scenario_fork_telemetry():
    """feedstats reports homing telemetry and survives interleaving with
    normal sorting; version identifies the fork."""
    import json
    slots = {"a": 2, "b": 4}
    sim = Cs72ForkSim(hopper=list(slots))
    assert sim.readline() == "Ready"     # boot banner
    sim.next_homing_steps = 133
    errs = base_checks(sim, "fork-stats")
    sim.write("version")
    if sim.readline() != "7.2.250925.6.2-SS2":
        errs.append("fork-stats: version string wrong")
    drive(sim, "flush", slots)
    sim.write("feedstats")
    stats = json.loads(sim.readline())
    if stats.get("FeedCycles") != sim.feeds or sim.feeds == 0:
        errs.append(f"fork-stats: cycles {stats} vs feeds={sim.feeds}")
    if stats.get("LastHomingSteps") != 133 or stats.get("MaxHomingSteps") != 133:
        errs.append(f"fork-stats: homing steps wrong: {stats}")
    if misplaced(sim, slots):
        errs.append(f"fork-stats: misplaced {misplaced(sim, slots)}")
    return errs


def scenario_fork_armdwell():
    """armdwell: setter round-trips, clamps, and a mid-run change never
    disturbs placement (the dwell is pure time — event-level footprint is
    only the config; what this proves is the protocol accepts it at any
    point in a run without touching queue semantics)."""
    import json
    slots = {"a": 2, "b": 6, "c": 1}
    sim = Cs72ForkSim(hopper=list(slots))
    assert sim.readline() == "Ready"     # boot banner
    errs = base_checks(sim, "fork-dwell")
    sim.write("armdwell:130")
    if sim.readline() != "ok":
        errs.append("fork-dwell: setter not acked")
    sim.write("getconfig")
    if json.loads(sim.readline()).get("ArmDwellMs") != 130:
        errs.append("fork-dwell: getconfig round-trip failed")
    r = drive(sim, "flush", slots)
    if misplaced(sim, slots):
        errs.append(f"fork-dwell: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 3 or r["end_reason"] != "out_of_brass":
        errs.append(f"fork-dwell: {len(sim.fell)}/3 placed ({r['end_reason']})")
    return errs


def scenario_pipeline_warm_ten():
    """The SS2 money test: pipelined run on a warm wheel must clear the
    same bar as the sequential loop — every case in its true slot, wheel
    drained, only the warm leftover in the catch-all."""
    sim, slots = warm_ten(Cs72ForkSim)
    r = drive(sim, "pipeline", slots)
    errs = base_checks(sim, "pipe-warm")
    bad = misplaced(sim, slots)
    if bad != [("c1", 1, 0)]:
        errs.append(f"pipe-warm: misplaced {bad}")
    if len(sim.fell) != 10 or sim.wheel_cases():
        errs.append(f"pipe-warm: {len(sim.fell)}/10 placed, wheel "
                    f"{sim.wheel_cases()} (end={r['end_reason']})")
    if r["end_reason"] != "out_of_brass" or not r["flushed"]:
        errs.append(f"pipe-warm: end={r['end_reason']} flushed={r['flushed']}")
    return errs


def scenario_pipeline_cold_ten():
    """Pipelined cold start: the prime walk-in (empty-camera forced feeds)
    still runs sequentially, then pipelined sorting takes over — all 10
    true, all counted."""
    slots = dict(TEN)
    sim = Cs72ForkSim(hopper=[c for c, _ in TEN])
    r = drive(sim, "pipeline", slots)
    errs = base_checks(sim, "pipe-cold")
    if misplaced(sim, slots):
        errs.append(f"pipe-cold: misplaced {misplaced(sim, slots)}")
    if len(sim.fell) != 10 or sim.wheel_cases():
        errs.append(f"pipe-cold: {len(sim.fell)}/10 placed, wheel "
                    f"{sim.wheel_cases()} (end={r['end_reason']})")
    if len(r["counted"]) != 10:
        errs.append(f"pipe-cold: counted {len(r['counted'])}/10")
    return errs


def scenario_pipeline_guard():
    """pf without a queued slot must refuse with an error line (surfacing
    as JAM) and move NOTHING — the guard that turns a lost PSLOT into a
    visible fault instead of a silent off-by-one missort."""
    sim = Cs72ForkSim(hopper=["a", "b", "c", "d", "e"])   # enough brass that
                                                          # the gate stays wet
    t = Cs72Transport(sim)
    errs = []
    if _rl(t) != "SEATED":                # prime (camera still empty)
        return ["pipe-guard: prime failed"]
    t.send("FEED")                        # walk-in
    if _await_feed(t) != "SEATED":
        return ["pipe-guard: walk-in failed"]
    t.send("PFEED")                       # consumes the prime's queued slot 0
    if _sort_reply(t) != "DONE" or _rl(t) != "SEATED":
        errs.append("pipe-guard: first PFEED should complete")
    feeds_before = sim.feeds
    t.send("PFEED")                       # tail is a placeholder: must refuse
    reply = _sort_reply(t)
    if reply != "JAM":
        errs.append(f"pipe-guard: expected JAM from the guard, got {reply}")
    if sim.feeds != feeds_before:
        errs.append("pipe-guard: the refused pf still fed!")
    t.send("PSLOT:5")                     # queue repaired: pf runs again
    t.send("PFEED")
    if _sort_reply(t) != "DONE":
        errs.append("pipe-guard: pf after a repaired queue should run")
    return errs + base_checks(sim, "pipe-guard")


class _JamAtFeed(Cs72ForkSim):
    """Fork sim that grinds an overtravel error on feed cycle N."""

    def __init__(self, jam_on_feed, **kw):
        super().__init__(**kw)
        self._jam_on = jam_on_feed

    def _pump(self):
        if (self.FeedScheduled and self._ready_to_feed()
                and self.feeds + 1 == self._jam_on):
            self._jam_next = True
        super()._pump()


def _equivalence(build, tag):
    """Run the sequential and pipelined strategies over two identically
    built sims and demand IDENTICAL physical placements, counts, and end
    state — the pipeline's core promise is that it changes WHEN the cycle
    starts, never where brass lands."""
    sim_a, slots = build()
    sim_b, _ = build()
    ra = drive(sim_a, "flush", slots)
    rb = drive(sim_b, "pipeline", slots)
    errs = base_checks(sim_a, f"{tag}-seq") + base_checks(sim_b, f"{tag}-pipe")
    if sim_a.fell != sim_b.fell:
        errs.append(f"{tag}: placements diverge\n"
                    f"      seq : {sim_a.fell}\n"
                    f"      pipe: {sim_b.fell}")
    if ra["counted"] != rb["counted"]:
        errs.append(f"{tag}: counted diverge {ra['counted']} vs {rb['counted']}")
    if ra["end_reason"] != rb["end_reason"]:
        errs.append(f"{tag}: end {ra['end_reason']} vs {rb['end_reason']}")
    return errs


def scenario_pipeline_jam_equivalence():
    """A mid-run jam + HOME recovery must leave the pipelined run in
    exactly the sequential run's state — the case↔slot pairing may not
    slip by one (the failure mode that would silently missort everything
    after it)."""
    errs = []
    for jam_on in (3, 5, 7):
        def build(jam_on=jam_on):
            slots = dict(TEN)
            return _JamAtFeed(jam_on, hopper=[c for c, _ in TEN]), slots
        errs += _equivalence(build, f"pipe-jam@{jam_on}")
        sim, slots = build()
        drive(sim, "pipeline", slots)
        wrong = [(c, w, g) for c, w, g in misplaced(sim, slots) if g != 0]
        if wrong:
            errs.append(f"pipe-jam@{jam_on}: case in a WRONG bin (not "
                        f"catch-all): {wrong}")
    return errs


def scenario_pipeline_equivalence_fuzz():
    """Randomized equivalence sweep: cold runs across wheel geometries and
    collator lag — sequential and pipelined placements must be identical,
    trial after trial."""
    rng = random.Random(20260722)
    errs = []
    for trial in range(200):
        n = rng.randint(0, 14)
        gap = rng.choice([1, 2, 2, 3])
        spec = [(f"t{trial}c{i}", rng.randint(0, 7)) for i in range(n)]
        delayed = {i for i in range(1, n) if rng.random() < 0.12}
        polls = {i: rng.randint(1, FLUSH_WAITS - 1) for i in delayed}

        def build(spec=spec, delayed=delayed, polls=polls, gap=gap):
            slots = dict(spec)
            hopper = [DelayedCase(label, polls[i]) if i in delayed else label
                      for i, (label, _) in enumerate(spec)]
            return Cs72ForkSim(hopper=hopper, sensor_to_camera=gap), slots
        errs += _equivalence(build, f"pipe-fuzz#{trial} gap={gap}")
        if errs:
            break                        # first failure is enough detail
    return errs


SCENARIOS = [
    ("mid-run correctness + 3 stragglers (pr53 baseline)", scenario_mid_run_pr53),
    ("PR#50 field failure reproduced (case #8!)", scenario_pr50_field_bug),
    ("PR#52 field failure reproduced", scenario_pr52_field_bug),
    ("FLUSH: warm 10-case full placement", scenario_flush_warm_ten),
    ("FLUSH: cold 10-case with prime walk-in", scenario_flush_cold_ten),
    ("FLUSH: edge sweeps", scenario_flush_edges),
    ("FLUSH: transient dry spell recovers", scenario_flush_transient_dry),
    ("FLUSH: sneak feed race resolves via cancel_wait", scenario_flush_sneak_feed),
    ("FLUSH: jam mid-flush surfaces, drops nothing", scenario_flush_jam),
    ("FLUSH: leftover wheel cases at run start", scenario_leftover_wheel),
    ("FLUSH: 300-run fuzz across geometries", scenario_fuzz),
    ("FORK: protocol superset — run loop green as-is", scenario_fork_protocol_superset),
    ("FORK: slot table + getconfig round-trip", scenario_fork_slot_table),
    ("FORK: feedstats telemetry + version", scenario_fork_telemetry),
    ("FORK: armdwell setter + round-trip, placement untouched", scenario_fork_armdwell),
    ("SS2: pipelined warm 10-case full placement", scenario_pipeline_warm_ten),
    ("SS2: pipelined cold 10-case with walk-in", scenario_pipeline_cold_ten),
    ("SS2: pf-without-slot guard refuses safely", scenario_pipeline_guard),
    ("SS2: jam recovery ≡ sequential (no queue slip)", scenario_pipeline_jam_equivalence),
    ("SS2: 200-run sequential≡pipelined fuzz", scenario_pipeline_equivalence_fuzz),
]


def main():
    failures = 0
    for name, fn in SCENARIOS:
        errs = fn()
        status = "PASS" if not errs else "FAIL"
        print(f"[{status}] {name}")
        for e in errs:
            print(f"    - {e}")
        failures += bool(errs)
    print(f"\nRESULT: {'PASS' if failures == 0 else f'FAIL ({failures} scenario(s))'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
