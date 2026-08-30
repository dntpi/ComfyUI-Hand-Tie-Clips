r"""Offline tests for the LLM plan writer -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_planner.py

The repair loop is the reason the Write-plan button is worth having, and it is
also the part that cannot be checked by looking at it. `planner.write_plan`
takes its completion function as an argument precisely so this file can drive it
with a scripted model: one reply carrying a known fault, then a clean one. If
attempt 2 does not fix attempt 1's mistake, that is a failure here rather than a
surprise in front of a user with a 27B loaded.

The planted fault is the real one. The A/B run that chose the shipped prompt had
two unrelated model families both write a beat citing `@kitchen` while the
register declared only the people -- so `resolve_tags` raised and the queue
stopped. That is the fault attempt 1 makes below.

Covers:
  * split_reply     -- structured output, two fences, one bare object
  * validate        -- accepts a good plan; catches the tag mismatch, a bad
                       duration label, an invented filename, a hop-count miss
  * write_plan      -- repairs on attempt 2, reports warnings, and never
                       returns an unvalidated plan when it cannot converge
"""
from __future__ import annotations

import asyncio
import importlib.util
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


# -- the fixtures ----------------------------------------------------------

FILES = ["cook_face.png", "apron.png", "kitchen.png"]

GOOD_REFS = {
    "refs": [
        {"tag": "cook_face", "file": "cook_face.png", "subject": 1,
         "retention": "fully_preserved"},
        {"tag": "apron", "file": "apron.png", "subject": 1,
         "retention": "partially_copy"},
        {"tag": "kitchen", "file": "kitchen.png", "retention": "reference"},
    ],
    "subjects": {
        "1": {"name": "the cook",
              "locked": "a woman in her forties, dark hair tied back",
              "context": "she is plating a dish"},
    },
}

GOOD_SHOTS = [
    {"id": "s1", "beat": "@cook_face works at the counter in @kitchen, "
                         "setting a plate down and wiping her hands.",
     "directives": {}},
    {"id": "s2", "beat": "She lifts the plate and turns toward the doorway, "
                         "the light shifting across her shoulders.",
     "directives": {}},
    {"id": "s3", "beat": "She steps into the hallway, the low hum of a light "
                         "overhead, and walks on out of frame.",
     "directives": {}},
]

# Attempt 1's fault: the beat cites @kitchen, the register never declares it.
BAD_REFS = json.loads(json.dumps(GOOD_REFS))
BAD_REFS["refs"] = [r for r in BAD_REFS["refs"] if r["tag"] != "kitchen"]


def fence(shots, refs):
    return ("Here is the plan.\n\n```json\n" + json.dumps(shots, indent=2)
            + "\n```\n\n```json\n" + json.dumps(refs, indent=2) + "\n```\n")


def scripted(replies):
    """A fake model. Hands back the next canned reply and records the messages
    it was given, so the test can assert the error was actually fed back."""
    seen = []

    async def complete_fn(messages, schema=None):
        seen.append([dict(m) for m in messages])
        return replies[min(len(seen), len(replies)) - 1]

    complete_fn.seen = seen
    return complete_fn


def main():
    load_pack()
    # `planner` is imported lazily by the route, so it is not pulled in by
    # __init__ -- name it explicitly rather than fishing it out of sys.modules.
    PL = importlib.import_module("htcpack.planner")

    # ------------------------------------------------------------ split_reply
    print("\nplanner.split_reply")
    s, r = PL.split_reply(fence(GOOD_SHOTS, GOOD_REFS))
    ck("two fences: shots recovered", json.loads(s or "null") == GOOD_SHOTS)
    ck("two fences: refs recovered", json.loads(r or "null") == GOOD_REFS)

    s, r = PL.split_reply(json.dumps(
        {"shot_plan": GOOD_SHOTS, "ref_plan": GOOD_REFS}))
    ck("structured output splits", json.loads(s or "null") == GOOD_SHOTS
       and json.loads(r or "null") == GOOD_REFS)

    # Order must not decide identity: a register emitted first still lands in
    # ref_plan rather than being read as the script.
    s, r = PL.split_reply(fence(GOOD_REFS, GOOD_SHOTS))
    ck("blocks identified by shape, not order",
       json.loads(r or "null") == GOOD_REFS
       and json.loads(s or "null") == GOOD_SHOTS)

    s, _ = PL.split_reply(json.dumps(GOOD_SHOTS))
    ck("bare array is taken as the script", json.loads(s or "null") == GOOD_SHOTS)

    # --------------------------------------------------------------- validate
    print("\nplanner.validate")
    errs, warns = PL.validate(json.dumps(GOOD_SHOTS), json.dumps(GOOD_REFS),
                              hops=3, known_files=FILES)
    ck("a good plan passes", not errs, "; ".join(errs[:2]))

    # Regression: `refs.check` used to be handed an empty wired-slot set, so a
    # plan whose pictures are all on disk collected one "this ref is inactive"
    # warning per reference. False warnings are worse than none -- they teach
    # the reader to skip the tier that carries the real ones.
    ck("a good plan warns about nothing that is present",
       not any("inactive" in w or "could not be read" in w for w in warns),
       "; ".join(warns[:1]))

    errs, _ = PL.validate(json.dumps(GOOD_SHOTS), json.dumps(BAD_REFS),
                          hops=3, known_files=FILES)
    ck("undeclared @tag is caught",
       any("@kitchen" in e for e in errs), "; ".join(errs[:1]))
    ck("the message says what to do",
       any("character for character" in e for e in errs))

    bad_dur = json.loads(json.dumps(GOOD_SHOTS))
    bad_dur[0]["duration"] = "10s"          # the label is "10 s"
    errs, _ = PL.validate(json.dumps(bad_dur), json.dumps(GOOD_REFS),
                          hops=3, known_files=FILES)
    ck("a near-miss duration label is caught",
       any("10s" in e for e in errs), "; ".join(errs[:1]))

    inv = json.loads(json.dumps(GOOD_REFS))
    inv["refs"][0]["file"] = "cook_face_v2.png"
    errs, _ = PL.validate(json.dumps(GOOD_SHOTS), json.dumps(inv),
                          hops=3, known_files=FILES)
    ck("an invented filename is caught",
       any("cook_face_v2.png" in e for e in errs), "; ".join(errs[:1]))

    errs, _ = PL.validate(json.dumps(GOOD_SHOTS), json.dumps(GOOD_REFS),
                          hops=6, known_files=FILES)
    ck("the wrong hop count is caught", any("6 hop" in e for e in errs))

    errs, _ = PL.validate("not json at all", json.dumps(GOOD_REFS), hops=3)
    ck("unparseable JSON is one clean error", len(errs) == 1)
    ck("the log prefix is stripped for the model",
       errs and not errs[0].startswith("HandTieClips"), errs[0][:44])

    # ------------------------------------------------------------- write_plan
    print("\nplanner.write_plan -- the repair loop")
    fn = scripted([fence(GOOD_SHOTS, BAD_REFS),      # attempt 1: the A/B fault
                   fence(GOOD_SHOTS, GOOD_REFS)])    # attempt 2: fixed
    steps = []
    out = asyncio.run(
        PL.write_plan("a cook in a kitchen", 3, complete_fn=fn, files=FILES,
                      use_schema=False, on_step=steps.append))
    ck("converges", out["ok"] is True, "; ".join(out["errors"][:1]))
    ck("takes exactly two attempts", out["attempts"] == 2, str(out["attempts"]))
    ck("returns the repaired plan",
       json.loads(out["ref_plan"] or "null") == GOOD_REFS)
    ck("progress was reported per attempt", len(steps) == 2, str(len(steps)))

    # The retry has to carry the node's own words, or the model is guessing.
    second = fn.seen[1]
    ck("the error was fed back verbatim",
       any("@kitchen" in str(m.get("content")) for m in second))
    ck("the rejected reply was kept in the conversation",
       any(m.get("role") == "assistant" for m in second))
    ck("the system prompt is the shipped file",
       second[0]["role"] == "system" and len(second[0]["content"]) > 2000,
       "%d chars" % len(second[0]["content"]))
    ck("the file list reached the model",
       all(f in second[1]["content"] for f in FILES))

    # A loop that gives up must not hand back the broken plan as if it passed.
    fn = scripted([fence(GOOD_SHOTS, BAD_REFS)])
    out = asyncio.run(
        PL.write_plan("a cook", 3, complete_fn=fn, files=FILES,
                      use_schema=False))
    ck("gives up rather than returning a bad plan", out["ok"] is False)
    ck("says why it gave up", bool(out["errors"]), "; ".join(out["errors"][:1]))
    ck("stops at the attempt cap", out["attempts"] == PL.MAX_ATTEMPTS,
       str(out["attempts"]))

    # An empty reply is a distinct failure from a wrong one, and the message
    # a user sees should say so.
    fn = scripted([""])
    out = asyncio.run(
        PL.write_plan("a cook", 3, complete_fn=fn, files=FILES,
                      use_schema=False))
    ck("an empty reply is reported as such",
       out["ok"] is False and any("no JSON" in e for e in out["errors"]),
       "; ".join(out["errors"][:1]))

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
