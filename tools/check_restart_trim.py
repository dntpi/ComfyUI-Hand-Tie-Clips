r"""Offline tests for restart hops writing full length -- no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_restart_trim.py

A restart is a chain start. It overlaps with nothing, so dropping the
leading 22 frames throws away 0.9 s of new content per restart, and the
preallocation `sum(lengths) - overlap * (n - 1)` sizes the master short
by the same amount. Two restarts on the tester's 9 x 8 s chain is 44
frames / 1.83 s of silence at the end, or an overrun raise.

The audio-lock window has to move with it: a restart that kept the
uniform stride would freeze lips to a take 0.9 s earlier than the
picture.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)
sys.path.insert(0, HERE)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_lock():
    spec = importlib.util.spec_from_file_location(
        "htc_audio_lock", os.path.join(HERE, "audio_lock.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


def main():
    L = load_lock()

    print("master length vs the hand-computed table")
    # 9 x 192 f, overlap 22. Hop 0 is always a start.
    lengths = [192] * 9
    no_restart = [True] + [False] * 8
    two = [True, False, False, True, False, False, True, False, False]
    count = getattr(L, "master_frame_count", None)
    if count is None:
        ck("no restarts: 1552 (the old formula)", False, "helper missing")
        ck("restarts at hops 4 and 7: 1596, not 1552", False, "helper missing")
        ck("the old formula is 44 frames short of that",
           (sum(lengths) - 22 * 8) == 1552 and 1596 - 1552 == 44)
        ck("a single hop is its own length", False, "helper missing")
    else:
        ck("no restarts: 1552 (the old formula)",
           count(lengths, 22, no_restart) == 1552)
        # Restarts at 0-based 3 and 6 (hops 4 and 7). n_trims = 6, not 8.
        ck("restarts at hops 4 and 7: 1596, not 1552",
           count(lengths, 22, two) == 1596,
           "9*192 - 22*6 = 1596")
        ck("the old formula is 44 frames short of that",
           (sum(lengths) - 22 * 8) == 1552
           and 1596 - 1552 == 44)
        ck("a single hop is its own length",
           count([192], 22, [True]) == 192)

    print("audio window follows the master head, not a uniform stride")
    # Uniform case must still match the original table (no lengths/start_at).
    t0, t1 = L.hop_audio_window_s(3, 192, 22, 24.0)
    ck("uniform hop 3 starts at 510/24 (unchanged default)",
       abs(t0 - 510.0 / 24.0) < 1e-9, f"got {t0}")
    try:
        t0, _ = L.hop_audio_window_s(3, 192, 22, 24.0,
                                     lengths=lengths, start_at=two)
        restart_ok = abs(t0 - 532.0 / 24.0) < 1e-9
        got = f"got {t0}"
    except TypeError as e:
        restart_ok, got = False, str(e)
    ck("restart hop 3 starts at 532/24 (master head, not 510/24)",
       restart_ok, got)
    try:
        t0, t1 = L.hop_audio_window_s(4, 192, 22, 24.0,
                                      lengths=lengths, start_at=two)
        ck("hop 4 after a restart starts at 702/24",
           abs(t0 - 702.0 / 24.0) < 1e-9, f"got {t0}")
        ck("hop 4 window is still 8 s",
           abs((t1 - t0) - 8.0) < 1e-9, f"got {t1 - t0}")
        _, t1 = L.hop_audio_window_s(8, 192, 22, 24.0,
                                     lengths=lengths, start_at=two)
        ck("last hop of the restart chain ends at 1596/24 s",
           abs(t1 - 1596.0 / 24.0) < 1e-9, f"got {t1}")
    except TypeError as e:
        ck("hop 4 after a restart starts at 702/24", False, str(e))
        ck("hop 4 window is still 8 s", False, str(e))
        ck("last hop of the restart chain ends at 1596/24 s", False, str(e))

    print("run() writes full length on a start, trims otherwise")
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    src = inspect.getsource(H3.HandTieClips.run)
    ck("master length uses master_frame_count",
       "master_frame_count(" in src)
    ck("the write path keys on hop_is_start, not i==0",
       "if hop_is_start:" in src
       and "restart, wrote all" in src)
    ck("render_from's start_at is not reused as the start-flag list",
       "hop_starts" in src
       and "start_at=hop_starts" in src)
    ck("contact sheet first-frame uses hop_is_start",
       "imgs[0] if hop_is_start" in src)
    ck("overlap leaves the hop key on a restart",
       "overlap_n if not hop_is_start" in src)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
