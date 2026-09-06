r"""The media slots: dense numbering, and the JS half exists.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_media_slots.py

H3 takes 9 reference images, 3 reference videos and 3 standalone reference
audios. The pack matched the 9 and passed exactly one of the others until the
slots were added, so two whole channels sat at a third of capacity.

Two things about that are easy to get wrong and impossible to see afterwards.

**Numbering is dense.** Core numbers reference blocks by the order it iterates
them and the prompt cites those ordinals, so a gap must not survive: filling
slots 1 and 3 has to produce <Video 1> and <Video 2>, not 1 and 3. If it ever
produced a gap, a beat naming "the second clip" would cite something else and
nothing would report it.

**A file widget with no slot in the strip falls through to a native dial.**
That is the 0.4.0 failure recorded at run_panel.js:53 and it has recurred since.
Nothing checked it, so this does: every `*_file` widget the node declares must
be claimed by `media_strip.js`, and every widget the strip expects must exist.
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-52s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    dense = H3._dense_media

    print("dense numbering")
    ck("all three present number 1..3",
       list(dense("ref_audio_", ["a", "b", "c"])) == ["ref_audio_1", "ref_audio_2", "ref_audio_3"])
    ck("a gap in the middle does NOT survive",
       list(dense("ref_video_", ["a", None, "c"])) == ["ref_video_1", "ref_video_2"],
       "slot 3 becomes <Video 2>")
    ck("only the last slot filled still starts at 1",
       list(dense("ref_audio_", [None, None, "c"])) == ["ref_audio_1"])
    ck("order is preserved, not sorted",
       list(dense("x", ["z", "a"]).values()) == ["z", "a"])
    ck("nothing filled is None, not an empty dict",
       dense("ref_audio_", [None, None, None]) is None,
       "core takes None to mean the channel is unused")

    print("the node declares what core accepts")
    it = H3.HandTieClips.INPUT_TYPES()
    allw = dict(it.get("required", {}))
    allw.update(it.get("optional", {}))
    vids = [w for w in allw if re.fullmatch(r"reference_video(_[23])?_file", w)]
    auds = [w for w in allw if re.fullmatch(r"voice(_[23])?_file", w)]
    ck("three reference video slots", len(vids) == 3, str(sorted(vids)))
    ck("three voice slots", len(auds) == 3, str(sorted(auds)))
    for w in vids + auds:
        trim = w[:-len("_file")]
        ck(f"{w} has both trim widgets",
           f"{trim}_start_s" in allw and f"{trim}_end_s" in allw)

    print("the JS half exists for every file widget")
    js = io.open(os.path.join(HERE, "js/editor/media_strip.js"), encoding="utf-8").read()
    block = js.split("const SLOTS = [", 1)[1].split("\n];", 1)[0]
    slot_files = re.findall(r'\["([a-z0-9_]+_file)"', block)
    trims = re.findall(r'"([a-z0-9_]+)",\s*null,\s*null\]', block)

    unclaimed = [w for w in allw
                 if w.endswith("_file") and w not in slot_files]
    ck("no file widget falls through to a native dial",
       not unclaimed, f"unclaimed: {unclaimed}" if unclaimed else f"{len(slot_files)} slots")

    expected = set(slot_files)
    for t in trims:
        expected |= {f"{t}_start_s", f"{t}_end_s"}
    missing = sorted(w for w in expected if w not in allw)
    ck("every widget the strip expects exists in Python",
       not missing, f"missing: {missing}" if missing else f"{len(expected)} checked")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
