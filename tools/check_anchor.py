r"""Offline tests for anchor=restart -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_anchor.py

`anchor: "restart"` makes a hop a chain start: the start image becomes its
frame 0 and nothing from the previous hop is relayed into it. It exists because
a chain decays by inheritance -- every hop is conditioned on its predecessor's
last frames, which are its most settled, so motion and lighting response fade
the further in you go (DEVLOG 51).

Its three guard rules shipped broken TWICE before anything tested them: once
naming a `join` value the vocabulary does not contain, once reading
`start_image` before it was assigned. Both failed on a real queue. Guards that
only a render exercises are guards nobody has run.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-54s %s" % ("ok" if cond else "FAIL", name, detail))
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


def raises(fn, needle):
    try:
        fn()
    except ValueError as e:
        return needle.lower() in str(e).lower()
    except Exception:
        return False
    return False


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    P = sys.modules["htcpack.plan"]
    D = sys.modules["htcpack.directives"]
    V = H3._validate_anchors

    def shot(anchor=None, join=None):
        s = {"beat": "x"}
        if anchor:
            s["anchor"] = anchor
        if join:
            s["directives"] = {"join": join}
        return s

    print("the field parses")
    got = P.parse_plan(json.dumps({"shots": [shot(), shot("restart")]}))
    ck("restart survives parse_plan", got[1]["anchor"] == "restart")
    ck("absent becomes empty, not None", got[0]["anchor"] == "")
    ck("an unknown value is refused",
       raises(lambda: P.parse_plan(json.dumps({"shots": [shot("resume")]})), "anchor"))

    print("the join it demands actually exists")
    # The bug: the error told users to "use join=cut", which is not a value.
    for v in ("hard_cut", "match_cut"):
        ck(f"{v} is a real join value", v in D.VOCAB["join"])
    ck("'cut' is NOT a join value, so no message may suggest it",
       "cut" not in D.VOCAB["join"], "hard_cut / match_cut are")
    try:
        V([shot(), shot("restart", "continuous")], "img.png")
        msg = ""
    except ValueError as e:
        msg = str(e)
    ck("the continuous refusal names only real values",
       "hard_cut" in msg and "join=cut " not in msg, msg[-46:] if msg else "no error raised")

    print("the three guards")
    ck("shot 1 cannot restart",
       raises(lambda: V([shot("restart")], "img.png"), "shot 1"))
    ck("a restart with no start image and no refs is refused",
       raises(lambda: V([shot(), shot("restart")], ""), "start image"))
    ck("whitespace is not a start image",
       raises(lambda: V([shot(), shot("restart")], "   "), "start image"))
    ck("refs: [] is no refs",
       raises(lambda: V([shot(), dict(shot("restart"), refs=[])], ""), "start image"))
    try:
        V([shot(), dict(shot("restart"), refs=["ld", "warehouse"])], "")
        ok = True
    except ValueError:
        ok = False
    ck("a restart on its own refs needs no start image", ok)
    ck("join=continuous on a restart is refused",
       raises(lambda: V([shot(), shot("restart", "continuous")], "img.png"), "continuous"))

    print("and it lets the valid cases through")
    for name, shots, img in (
            ("a restart with hard_cut and an image", [shot(), shot("restart", "hard_cut")], "img.png"),
            ("a restart with match_cut",             [shot(), shot("restart", "match_cut")], "img.png"),
            ("a plan with no restart and no image",  [shot(), shot(None, "continuous")], ""),
    ):
        try:
            V(shots, img)
            ok = True
        except Exception as e:
            ok = False
            name += f"  ({e})"
        ck(name, ok)

    print("it reads the filename, not a loaded image")
    # The second bug: validation ran before `start_image` was assigned.
    import inspect
    src = inspect.getsource(V)
    ck("no reference to the not-yet-loaded start_image",
       "start_image_file" in src and "start_image is None" not in src)

    print("a restart is a chain START where the references are bound")
    # The third way these guards shipped broken, and the only one a render
    # could not show you: the line that FILLS `hop_subject_prose` asked
    # `i == 0` while the line that USES it asked `hop_is_start`, so the
    # subject_definitions block was dead on every restart. Those hops carried
    # reference pictures nothing in the prose cited, and an uncited plate gets
    # free-associated into the frame -- a cast member who is not in the beat
    # walks into the shot. `@tag` did not rescue it, because `subject_names`
    # flattened tags to bare prose on the same `i > 0` test.
    #
    # Read with comment lines stripped: the comments here have to be free to
    # name `i == 0` while describing why it is wrong.
    chain = os.path.join(HERE, "h3_ref_chain.py")
    lines = [ln for ln in io.open(chain, encoding="utf-8").read().splitlines()
             if not ln.strip().startswith("#")]

    prod = [j for j, ln in enumerate(lines)
            if "hop_subject_prose = _refs.subject_prose(" in ln]
    ck("exactly one line fills hop_subject_prose", len(prod) == 1)
    if len(prod) == 1:
        before = [ln.strip() for ln in lines[:prod[0]] if ln.strip()][-1]
        ck("it is guarded on hop_is_start, not i == 0  (%s)" % before,
           before == "if hop_is_start:")

    names = [j for j, ln in enumerate(lines) if "subject_names=(" in ln]
    ck("exactly one subject_names= call site", len(names) == 1)
    if len(names) == 1:
        tail = " ".join(ln.strip() for ln in lines[names[0]:names[0] + 4])
        ck("subject_names is withheld from chain starts",
           "if not hop_is_start else None" in tail)

    ck("the consumer still asks hop_is_start",
       any("if hop_is_start and hop_subject_prose" in ln for ln in lines))

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
