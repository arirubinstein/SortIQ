"""Exercise Cs72Transport against the in-process fake CS7.2 firmware.

Proves the protocol translation (SEATED/DONE/JAM <-> Ready/done/error and
SORT:n -> <slot>) end-to-end with no board attached. Mirrors the shape of
the server's run loop so a green run here means the real loop will drive
the real board the same way.

Usage: python tools/cs72_selftest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sorter.cs72 import Cs72Transport, FakeCs72Link


def main():
    fake = FakeCs72Link()
    # bin 7 is UNMATCHED in the default config; identity slot map here
    t = Cs72Transport(fake, slot_map=None)

    planned_bins = [2, 5, 0, 7, 3]      # what the "classifier" would decide
    sent, jam_injected = [], False
    sorted_n = 0
    transcript = []

    # loop shaped like the server's: wait SEATED -> "classify" -> SORT -> DONE
    for _ in range(200):                # generous cap; we stop when cases run out
        line = t.readline(timeout=2.0)
        transcript.append(f"  <- {line}")
        if line is None:
            transcript.append("  (idle timeout — hopper empty)")
            break
        if line == "JAM":
            transcript.append("  !! JAM -> HOME")
            t.send("HOME")
            continue
        if line != "SEATED":
            continue

        if sorted_n >= len(planned_bins):
            break                        # classified everything we planned
        bin_id = planned_bins[sorted_n]

        # arm a jam once, right before the 3rd sort, to test recovery
        if sorted_n == 2 and not jam_injected:
            fake.arm_jam()
            jam_injected = True

        t.send("LIGHT:A")                # no-op on CS7.2 (fixed LED)
        t.send(f"SORT:{bin_id}")
        transcript.append(f"  -> SORT:{bin_id}  (slot {t._slot(bin_id)})")
        reply = t.readline(timeout=5.0)
        transcript.append(f"  <- {reply}")
        if reply == "JAM":
            transcript.append("  !! JAM during sort -> HOME")
            t.send("HOME")
            continue
        if reply != "DONE":
            transcript.append(f"  !! expected DONE, got {reply!r}")
            break
        sent.append(bin_id)
        sorted_n += 1

    t.close()

    print("transcript:")
    print("\n".join(transcript))
    print(f"\nsorted bins in order: {sent}")

    expected = [b for b in planned_bins if True][:len(sent)]
    ok = sent == expected and len(sent) == len(planned_bins)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
