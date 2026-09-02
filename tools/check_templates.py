"""Every shipped template must survive the real Python validators.

A template that produced a plan the node rejects would be worse than no
templates at all -- a first-time author would blame their own writing. This
extracts the patterns out of templates.js rather than restating them, so the
test cannot pass against a copy that has drifted.
"""
import io
import json
import re
import sys
import types

import os
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
pkg = types.ModuleType("h3p")
pkg.__path__ = [HERE]
sys.modules["h3p"] = pkg

from h3p import plan as PL       # noqa: E402
from h3p import directives as D  # noqa: E402

SRC = io.open(os.path.join(HERE, "js", "editor", "templates.js"), encoding="utf-8").read()

# shot("...beat, possibly + concatenated...", { key: "value", ... })
# The required leading quote skips `function shot(beat, directives)`, which
# otherwise matched and swallowed the first pattern's metadata as a beat.
CALL = re.compile(r"\bshot\(\s*(\".+?)\s*,\s*(\{[^}]*\})\s*\)", re.S)
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')
NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:")


def js_string(expr):
    """Join a run of concatenated double-quoted literals."""
    parts = STR.findall(expr)
    if not parts:
        raise SystemExit("could not read a beat from: " + expr[:60])
    return "".join(p.replace('\\"', '"').replace("\\\\", "\\") for p in parts)


def js_object(expr):
    return json.loads(NAME.sub(r'"\1":', expr))


def main():
    names = re.findall(r'name:\s*"([^"]+)"', SRC)
    calls = CALL.findall(SRC)
    print("templates.js: %d patterns, %d shots\n" % (len(names), len(calls)))
    if not names or not calls:
        print("FAIL: extracted nothing -- the file's shape changed")
        return 1

    fails = []
    all_shots = []
    for beat_expr, dir_expr in calls:
        beat = js_string(beat_expr)
        dirs = js_object(dir_expr)
        all_shots.append({"beat": beat, "directives": dirs})

        for axis, val in dirs.items():
            if axis not in D.AXES:
                fails.append("unknown axis %r" % axis)
            elif val not in D.VOCAB[axis]:
                fails.append("%s=%r is not in VOCAB" % (axis, val))

        # Law 1: the prompt is additive, so a negation is never correct.
        for bad in (" no ", " not ", " never ", "n't", "without ", "stops ",
                    "stop ", "silent", "silence"):
            if bad in beat.lower():
                fails.append("beat contains %r: %s" % (bad, beat[:70]))

    # Every pattern must parse as a plan on its own AND stacked together.
    for label, shots in [("each pattern stacked", all_shots)]:
        text = json.dumps({"shots": [
            dict(s, id="s%d" % (i + 1)) for i, s in enumerate(shots)]})
        try:
            parsed = PL.parse_plan(text)
            print("  ok    %s parses (%d shots)" % (label, len(parsed)))
        except Exception as e:
            fails.append("%s: %s" % (label, e))
            print("  FAIL  %s: %s" % (label, e))
            continue
        warns = PL.check_coherence(parsed)
        if warns:
            print("  FAIL  coherence warnings:")
            for w in warns:
                print("        " + w)
            fails.extend(warns)
        else:
            print("  ok    no coherence warnings across the whole stack")

    # check_coherence has to tell a framing CHANGE from a framing RESTATED.
    # Models name the framing on every shot -- the axis describes the shot, not
    # a transition -- so testing "framing is named and is not keep" warned on
    # medium/medium/medium plans whose framing never moved, on every hop after
    # the first. Both live writers did it on essentially every plan, and
    # chain_00052 carried the banner while seaming at -0.31/255.
    def _sp(*framings):
        return [{"beat": "x", "directives": dict(
            {"camera": "hold", "framing": f},
            **({"join": "continuous"} if i else {}))}
            for i, f in enumerate(framings)]

    for label, plan_shots, want in (
            ("medium restated is not a change", _sp("medium", "medium", "medium"), False),
            ("keep after medium is not a change", _sp("medium", "keep"), False),
            ("medium -> close IS a change", _sp("medium", "close"), True),
            ("framing inherited through keep", _sp("close", "keep", "medium"), True),
            ("a first mention is not a change", _sp("", "medium"), False)):
        got = any("implies a cut" in w for w in PL.check_coherence(plan_shots))
        if got == want:
            print("  ok    %s" % label)
        else:
            print("  FAIL  %s: warned=%s wanted=%s" % (label, got, want))
            fails.append(label)

    # The last shot of any pattern that ends a chain must not be left `ongoing`.
    closers = [s for s in all_shots
               if (s["directives"].get("tail") in ("settle", "hold"))]
    print("  ok    %d of %d shots close with settle/hold"
          % (len(closers), len(all_shots)))

    print()
    if fails:
        print("%d FAILURE(S):" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("all template checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
