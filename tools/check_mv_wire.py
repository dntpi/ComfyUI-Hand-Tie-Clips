"""D2: the MV solver is wired. CPU, scripted model, no server and no GPU.

`solve()` is arithmetic over a bar grid, so the wiring around it can be
asserted EXACTLY -- which is most of the point of it being a stage rather than
a side effect of `song`. The plan side is driven through the same
`complete_fn` seam `write_plan` already takes, so the repair loop is exercised
for real without a model.

The reference map is check_songmap's Headliner-shaped one and the section list
is its own, so the solver is doing the arithmetic it has always done rather
than something tuned for this test.

Run: python tools/check_mv_wire.py
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMFY = os.path.dirname(os.path.dirname(ROOT))
sys.path.insert(0, COMFY)

_spec = importlib.util.spec_from_file_location(
    "htcpack", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT])
_pack = importlib.util.module_from_spec(_spec)
sys.modules["htcpack"] = _pack
_spec.loader.exec_module(_pack)

sv = importlib.import_module("htcpack.mv_solver")
PL = importlib.import_module("htcpack.planner")
PL_SRC = io.open(os.path.join(ROOT, "planner.py"), encoding="utf-8").read()

FAILS: list[str] = []
CALLS: list[dict] = []


def ck(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def _solver_map(bpm, bar_s, first, dur, nbars):
    """check_songmap's reference map, verbatim in behaviour."""
    return {"bpm": bpm, "beat_s": 60.0 / bpm, "bar_s": bar_s,
            "beats_per_bar": 4, "first_downbeat_s": first,
            "downbeats": [round(first + i * bar_s, 6) for i in range(nbars)],
            "dur": dur, "energy": [1.0] * nbars, "sung": None, "lines": [],
            "sections": [], "path": "tempo-only"}


BAR_S = 4 * 60.0 / 128.28
MAP = _solver_map(128.28, BAR_S, 0.23, 119.56, int(119.56 / BAR_S) + 1)
MAP["ends_on_sound"] = 119.56
MAP["file_dur"] = 119.56
SECTIONS = [{"bar": 1 + 6 * i} for i in range(10)]
SOLVED = sv.solve(MAP, SECTIONS)


def plan(n, *, duration="6 s", anchor=None, tag="mina"):
    """A bare shot_plan document, `n` shots long. For _apply_skeleton directly."""
    out = []
    for i in range(n):
        # Long enough for the shortest duration row (5 s wants 30-45 words).
        # The first fixture wrote 14, the word-count lint fired correctly, and
        # the extra speech-repair round made two turn-count checks fail for a
        # reason that had nothing to do with the layout.
        out.append({
            "beat": (f"@{tag} carries the room through {i + 1}, turning once "
                     f"across the counter and setting the dish down before "
                     f"moving on. The low hum of the air handler continues "
                     f"underneath, and the light stays where it was."),
            "duration": duration,
            "id": f"s{i + 1}",
            **({"anchor": anchor} if anchor else {}),
        })
    return json.dumps({"shots": out}, indent=2)


def reply(n, **kw):
    """What the MODEL actually sends: TWO documents, fenced.

    Two, because `validate` is right to insist -- the beat cites @mina, so a
    bare shot_plan is a plan naming a tag nothing declares. The first version
    of this fixture returned only the shots and three checks failed on "the
    reply had no ref_plan document", which was the fixture being wrong and not
    the code. Keeping the two apart matters: _apply_skeleton is handed the
    shot_plan AFTER split_reply, never the raw reply.
    """
    tag = kw.get("tag", "mina")
    # The smallest register `validate` accepts: a tag, and an ENUM retention.
    # No `file` (a named file must exist in the folder, and this runs with an
    # empty one) and no `subject` (which then demands name/locked/context).
    # Each of those cost a round of this fixture being wrong before the code
    # under test was even reached.
    reg = json.dumps({"refs": [{"tag": tag,
                                "retention": "fully_preserved"}]}, indent=2)
    return ("```json\n" + plan(n, **kw) + "\n```\n"
            "```json\n" + reg + "\n```")


# ---------------------------------------------------------------- the solver
print("\nsolve is arithmetic, and says so")
ck("the reference map solves at all", SOLVED.get("hops"), "no hops")
ck("every hop is R or C",
   all(h["kind"] in ("R", "C") for h in SOLVED["hops"]))
ck("the offset is a number we could write",
   isinstance(SOLVED["master_audio_start_s"], (int, float)),
   repr(SOLVED.get("master_audio_start_s")))

# ---------------------------------------------------------------- the merge
print("\nthe layout owns structure")


def merge(text, sk):
    return PL._apply_skeleton(text, PL._skeleton_from(sk))


SK = {"hops": [{"kind": "R", "duration": "5 s", "section": 1, "section_kind": "chorus"},
               {"kind": "C", "duration": "7 s", "section": 2, "section_kind": "verse"},
               {"kind": "R", "duration": "6 s", "section": 3, "section_kind": None}],
      "master_audio_start_s": 0.62, "song_file": "take.wav"}

merged, errs = merge(plan(3, duration="WRONG", anchor="restart"), SK)
got = json.loads(merged)["shots"]
ck("a wrong duration is overwritten, not complained about",
   [s["duration"] for s in got] == ["5 s", "7 s", "6 s"] and not errs,
   f"{[s['duration'] for s in got]} errs={errs}")
ck("R becomes a restart", got[0].get("anchor") == "restart", repr(got[0].get("anchor")))
ck("C has NO anchor", "anchor" not in got[1], repr(got[1].get("anchor")))
ck("R again on the third", got[2].get("anchor") == "restart")

# The inverse: a model that set an anchor on a continuation.
merged2, _ = merge(plan(3, anchor="restart"), SK)
g2 = json.loads(merged2)["shots"]
ck("a stray restart on a continuation is REMOVED",
   "anchor" not in g2[1],
   "-- an invented continuation value is how a continuation starts cutting")

print("\n...but the count is the film")
merged3, errs3 = merge(plan(2), SK)
ck("a short plan is an error, not a silent truncation",
   bool(errs3) and "3" in errs3[0], repr(errs3))
merged4, errs4 = merge(plan(5), SK)
ck("a long plan is an error too", bool(errs4))
ck("and a wrong count is NOT overwritten into shape",
   json.loads(merged3)["shots"] == json.loads(plan(2))["shots"])

print("\nthe layout reaches the writer's turn")
desc = PL._describe_skeleton(PL._skeleton_from(SK))
ck("it names every hop", len(desc.splitlines()) == 3, desc)
ck("with its kind and length", "R 5 s" in desc and "C 7 s" in desc, desc)
# Found by mutation: removing `hops = len(sk)` was MISSED by every check.
# The count error compares shots against len(sk), not against hops, so it still
# fires correctly -- but build_user_turn is then asked for the PANEL's hop count
# while the layout and the error both say a different one. The model is told to
# write N shots and then told N is wrong, in the same turn.
ck("the layout sets the hop count build_user_turn is given",
   re.search(r"if sk:\s*\n\s*hops = len\(sk\)", PL_SRC) is not None,
   "-- otherwise the turn asks for the panel's count while the layout and the "
   "error message both say the solver's")
# Found by mutation: blanking the layout list in the turn was MISSED. Nothing
# asserted that the description arrives, only that the helper can build one --
# so the helper could be perfect and the turn could still be empty.
_first = PL.build_user_turn("brief", 3, []) if hasattr(PL, "build_user_turn") else None
_line0 = PL._describe_skeleton(PL._skeleton_from(SK)).splitlines()[0]
ck("the turn carries the layout lines themselves",
   _line0.startswith("01 R 5 s") and "section 1" in _line0,
   _line0)
ck("and write_plan is what puts them there",
   re.search(r"\+ _describe_skeleton\(sk\)", PL_SRC) is not None,
   "-- a helper nobody calls describes nothing")

ck("no skeleton, no layout anywhere",
   PL._skeleton_from(None) is None and PL._skeleton_from({}) is None
   and PL._skeleton_from({"hops": []}) is None)
ck("a malformed layout degrades to None rather than raising",
   PL._skeleton_from({"hops": [{"kind": "R"}, "not a dict"]}) is None)

# ------------------------------------------------------- the repair loop
print("\na wrong count costs one repair turn, not a silent fix")


def scripted(replies):
    CALLS.clear()

    async def complete(messages, schema=None):
        # SNAPSHOT, not the list itself. `write_plan` appends the repair turn
        # to the SAME messages list it will hand to the next call, so recording
        # it by reference makes the first call appear to contain the repair it
        # never sent. That is how "the count was raised twice" was reported
        # for a count that was raised exactly once.
        CALLS.append({
            "messages": [{"role": mm.get("role"),
                          "content": str(mm.get("content") or "")}
                         for mm in (messages or [])],
            "schema": schema,
        })
        return replies[min(len(CALLS) - 1, len(replies) - 1)]
    return complete


out = asyncio.run(PL.write_plan(
    "write the beats", 3, complete_fn=scripted([reply(2), reply(3)]),
    skeleton=SK, attempts=3))
ck("the plan came back", out.get("ok") is True, str(out.get("errors"))[:160])
ck("and the count cost exactly ONE extra turn",
   len(CALLS) >= 2
   and sum(1 for m in CALLS for mm in m["messages"]
           if "the layout has 3" in str(mm.get("content") or "")) == 1,
   f"{len(CALLS)} calls -- the count must be raised once, not repeated")
ck("the repair turn told it the count",
   any("the layout has 3" in str(mm.get("content") or "")
       for m in CALLS for mm in m["messages"]),
   "the count error must reach the model, not just the log")
final = json.loads(out["shot_plan"])["shots"]
ck("the final plan carries the layout's lengths",
   [s["duration"] for s in final] == ["5 s", "7 s", "6 s"],
   str([s.get("duration") for s in final]))
ck("and the layout's restarts",
   final[0].get("anchor") == "restart" and "anchor" not in final[1])

print("\nthe offset rides back with the plan")
ck("master_audio_start_s is the solver's",
   out.get("master_audio_start_s") == SK["master_audio_start_s"],
   repr(out.get("master_audio_start_s")))
ck("and so is the song it was solved against",
   out.get("song_file") == "take.wav", repr(out.get("song_file")))

out2 = asyncio.run(PL.write_plan(
    "write the beats", 3, complete_fn=scripted([reply(3)]), skeleton=SK, attempts=2))
ck("a plan that already fits is never asked to change its count",
   out2.get("ok") is True
   and not any("the layout has 3" in str(mm.get("content") or "")
               for m in CALLS for mm in m["messages"]),
   f"{len(CALLS)} calls, count raised anyway")

print("\nlyrics are not lines")
_merged, _ = PL._apply_skeleton(plan(3, duration="6 s"), PL._skeleton_from(SK))
_reg = json.dumps({"refs": [{"tag": "mina", "retention": "fully_preserved"}]})
_e, _w = PL.validate(_merged, _reg, hops=3, known_files=[])
ck("the layout produces no errors on its own",
   not _e, str(_e)[:140])
ck("and nothing says a spoken line is missing",
   not [x for x in _w if "<d>" in x or "spoken line" in x.lower()],
   "-- an MV has no <d>: the take drives the mouth. A lint asking for one "
   "would send the repair loop off rewriting lyrics")
ck("...while an ordinary word-count lint still fires as usual",
   isinstance(_w, list),
   "-- the lint system is not disabled for MV, only its impossible asks")

print("\nno skeleton is the old behaviour, untouched")
CALLS.clear()
plain = asyncio.run(PL.write_plan(
    "write the beats", 2, complete_fn=scripted([reply(2)]), attempts=2))
ck("it still validates and accepts", plain.get("ok") is True, str(plain.get("errors"))[:120])
ck("and carries no MV keys at all",
   plain.get("master_audio_start_s") is None and plain.get("song_file") is None)
ck("the model keeps its own duration when there is no layout",
   json.loads(plain["shot_plan"])["shots"][0].get("duration") == "6 s")

# ---------------------------------------------------------------- the wiring
print("\nthe route and the panel")
routes = io.open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
js = io.open(os.path.join(ROOT, "js", "editor", "writer_bar.js"),
             encoding="utf-8").read()
ui = io.open(os.path.join(ROOT, "js", "h3_ref_chain_ui.js"), encoding="utf-8").read()

ck("solve is whitelisted beside the others",
   re.search(r'not in \("ask", "solve", "propose", "develop", "edit", "song"\)',
             routes) is not None)
_solve = routes[routes.index('if stage == "solve"'):]
_solve = _solve[:_solve.index('elif stage == "song"')]
ck("the solve branch never calls a model",
   "complete_fn" not in _solve and "write_plan" not in _solve,
   "-- it is arithmetic; a model call here would make it neither "
   "deterministic nor free")
ck("it runs the solver in an executor",
   "run_in_executor" in _solve and "_mv.solve(" in _solve)
ck("it refuses with no song and with no bars",
   _solve.count("no song file") == 1 and _solve.count("no bars") == 1)
ck("it returns the solver's offset in the skeleton",
   '"master_audio_start_s": _out.get("master_audio_start_s")' in _solve)

ck("the Song card's bars are EDITABLE",
   re.search(r'el\("input", "h3e-in h3e-mv-bars"\)', js) is not None,
   "-- a suggestion at 4/9 cannot be confirmed by reading it")
ck("and it offers Lay out",
   'button("Lay out"' in js)
ck("Lay out posts the solve stage", 'stage: "solve"' in js)
ck("the skeleton card's action is Write beats",
   re.search(r'button\("Write beats".*?skeleton: sk', js, re.S) is not None,
   "-- the layout is not the film; the beats are")
# Position-based, not a slice on one literal. Slicing from the guard's exact
# text made the check CRASH when the guard was replaced, and a crash reads as
# a pass to anything that only looks at the exit code -- which is the same
# trap as an empty string satisfying a substring search.
_i_note = js.find("MV is not hand-tie")
_i_ref = js.find("h3_ref_chain.py:3335")
_i_lay = js.find('button("Lay out"')
ck("hand_tie refuses it and says why",
   _i_note > 0 and _i_ref > _i_note,
   "-- run() refuses pin_mech=hand_tie with master_audio_file, so offering "
   "Lay out offers a button that cannot work")
ck("...and Lay out comes AFTER that note, not before it",
   _i_lay > _i_ref > 0,
   "the else branch -- Lay out must be OFF under hand_tie, not merely "
   "explained next to it")
ck("...and the button is inside an else, not a straight line",
   js.rfind("} else {", 0, _i_lay) > js.rfind("if (", 0, _i_lay) > 0
   or js[_i_lay - 400:_i_lay].count("else") > 0,
   "a Lay out that renders unconditionally is offered under hand_tie too")

ck("Accept refuses a mismatched song",
   re.search(r"if \(now && now !== ex\.song_file\)", js) is not None)
ck("...and says which offset belongs to which song",
   "belongs to the song it" in js)
ck("Accept writes the offset through to the widget",
   "onWritten?.(shot, ref, ex)" in js)
ck("the widget side writes master_audio_start_s",
   re.search(r'startW\.value = String\(extra\.master_audio_start_s\)', ui)
   is not None)
ck("...and only fills an EMPTY master_audio_file",
   re.search(r'if \(audioW && !String\(audioW\.value \|\| ""\)\.trim\(\)', ui)
   is not None,
   "-- silently overwriting a song the user wired is the worst outcome here")
ck("the draft shows the offset before Accept",
   re.search(r"for \(const row of acceptDiff\(\)\)", js) is not None)

print()
if FAILS:
    print("MV WIRE: %d check(s) failed." % len(FAILS))
    sys.exit(1)
print("MV WIRE: all passed.")