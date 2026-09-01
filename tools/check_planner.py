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
                       duration label, an invented filename, a hop-count miss,
                       a missing subject box, a rail tag the model dropped
  * write_plan      -- repairs on attempt 2, reports warnings, and never
                       returns an unvalidated plan when it cannot converge;
                       a pinned rail locks tags; stills ride only attempt 1
  * unload_all      -- the killswitch refuses to evict a writer that is not on
                       this machine, without opening a connection to find out
  * free_for_render -- quiet when unconfigured; does not raise from inside a
                       running event loop (ComfyUI's async execute path)
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
    # `tail` matters here, and this fixture went without it for a while:
    # SYSTEM_PROMPT rule 13 asks the final shot to settle or hold, and a
    # fixture called "the good plan" should not quietly break a shipped rule.
    {"id": "s3", "beat": "She steps into the hallway, the low hum of a light "
                         "overhead, and walks on out of frame.",
     "directives": {"tail": "settle"}},
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

    s, r = PL.split_reply(json.dumps({"subjects": GOOD_REFS["subjects"]}))
    ck("a subjects-only reply is not a script",
       not s and json.loads(r)["subjects"]["1"]["name"] == "the cook")

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

    # A prose rule the node does not enforce belongs in the WARN tier: shown,
    # never retried. Found by tools/grade_plan.py, which the loop's own
    # validator was passing straight over.
    no_tail = json.loads(json.dumps(GOOD_SHOTS))
    no_tail[-1]["directives"] = {}
    errs, warns = PL.validate(json.dumps(no_tail), json.dumps(GOOD_REFS),
                              hops=3, known_files=FILES)
    ck("a final shot with no tail warns", any("tail" in w for w in warns),
       "; ".join(warns[:1]))
    ck("and does not fail the plan over it", not errs, "; ".join(errs[:1]))

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

    nosub = json.loads(json.dumps(GOOD_REFS))
    nosub["subjects"] = {}
    errs, _ = PL.validate(json.dumps(GOOD_SHOTS), json.dumps(nosub),
                          hops=3, known_files=FILES)
    ck("a subject without continuity text is an error",
       any("subject 1" in e and "locked" in e for e in errs),
       "; ".join(errs[:1]))
    ck("and asks for context too",
       any("context" in e for e in errs), "; ".join(errs[:1]))

    pinned = [{"tag": "ref_1", "file": "cook_face.png"},
              {"tag": "ref_2", "file": "kitchen.png"}]
    rail = {
        "refs": [{"tag": "ref_1", "file": "cook_face.png", "subject": 1,
                  "retention": "fully_preserved",
                  "desc": "a woman facing the camera, dark hair tied back"},
                 {"tag": "ref_2", "file": "kitchen.png",
                  "retention": "reference",
                  "desc": "a kitchen counter and window in daylight"}],
        "subjects": GOOD_REFS["subjects"],
    }
    rail_shots = [
        {"id": "s1", "beat": "@ref_1 looks up in @ref_2 and speaks.",
         "directives": {}},
        {"id": "s2", "beat": "She sets the knife down and looks out.",
         "directives": {"tail": "settle"}},
    ]
    errs, _ = PL.validate(json.dumps(rail_shots), json.dumps(rail),
                          hops=2, known_files=[p["file"] for p in pinned],
                          pinned=pinned)
    ck("a rail-pinned plan passes", not errs, "; ".join(errs[:2]))

    stolen = json.loads(json.dumps(rail))
    stolen["refs"][0]["file"] = "apron.png"
    errs, _ = PL.validate(json.dumps(rail_shots), json.dumps(stolen),
                          hops=2, known_files=[p["file"] for p in pinned],
                          pinned=pinned)
    ck("a pinned row cannot change file",
       any("cook_face.png" in e for e in errs), "; ".join(errs[:1]))

    extra = json.loads(json.dumps(rail))
    extra["refs"].append({"tag": "hallway", "file": "apron.png"})
    errs, _ = PL.validate(json.dumps(rail_shots), json.dumps(extra),
                          hops=2, known_files=[p["file"] for p in pinned],
                          pinned=pinned)
    ck("a pinned rail rejects extra tags",
       any("hallway" in e for e in errs), "; ".join(errs[:1]))

    nodesc = json.loads(json.dumps(rail))
    nodesc["refs"][0]["desc"] = ""
    errs, _ = PL.validate(json.dumps(rail_shots), json.dumps(nodesc),
                          hops=2, known_files=[p["file"] for p in pinned],
                          pinned=pinned)
    ck("a pinned row needs a desc",
       any("desc" in e and "@ref_1" in e for e in errs), "; ".join(errs[:1]))

    # The live failure of 2026-09-02: a model that looked at the stills named
    # the rows for what it saw. The files were right; only the names moved.
    renamed = {
        "refs": [{"tag": "girl_face", "file": "cook_face.png", "subject": 1,
                  "retention": "fully_preserved",
                  "desc": "a woman facing the camera, dark hair tied back"},
                 {"tag": "kitchen", "file": "kitchen.png",
                  "retention": "reference",
                  "desc": "a kitchen counter and window in daylight"}],
        "subjects": GOOD_REFS["subjects"],
    }
    renamed_shots = [
        {"id": "s1", "beat": "@girl_face looks up in @kitchen and speaks.",
         "directives": {}},
        {"id": "s2", "beat": "She sets the knife down and looks out.",
         "directives": {"tail": "settle"}},
    ]
    st, rt, mapping = PL._remap_pinned_tags(
        json.dumps(renamed_shots), json.dumps(renamed), pinned)
    ck("an invented tag is mapped back by filename",
       mapping.get("girl_face") == "ref_1"
       and mapping.get("kitchen") == "ref_2", repr(mapping))
    ck("the beat is rewritten with it",
       "@ref_1 looks up in @ref_2" in st, st[:90])
    ck("no @girl_face survives the rewrite",
       "girl_face" not in st and "girl_face" not in rt)
    errs, _ = PL.validate(st, rt, hops=2,
                          known_files=[p["file"] for p in pinned],
                          pinned=pinned)
    ck("and the remapped plan validates clean", not errs, "; ".join(errs[:2]))

    ck("a rail row that kept its name is left alone",
       PL._remap_pinned_tags(json.dumps(rail_shots), json.dumps(rail),
                             pinned)[2] == {})
    ck("an unpinned write is never remapped",
       PL._remap_pinned_tags(json.dumps(renamed_shots), json.dumps(renamed),
                             [])[2] == {})

    # A ref whose file is not on the rail has no identity to map to, so the
    # name stands and validate still calls it out.
    invented = json.loads(json.dumps(renamed))
    invented["refs"].append({"tag": "hallway", "file": "nowhere.png",
                             "desc": "a hallway"})
    ck("a file the rail never had is not remapped",
       "hallway" not in PL._remap_pinned_tags(
           json.dumps(renamed_shots), json.dumps(invented), pinned)[2].values())

    kept = PL._merge_register(
        json.dumps({"refs": [{"tag": "ref_1", "file": "a.jpg",
                              "desc": "a woman in green"}],
                    "subjects": {}}),
        json.dumps({"refs": [{"tag": "ref_1", "file": "a.jpg",
                              "subject": 1}],
                    "subjects": {}}))
    ck("a later turn does not wipe desc",
       json.loads(kept)["refs"][0]["desc"] == "a woman in green")

    turn = PL.build_user_turn("she talks about life", 2, FILES)
    ck("empty rail lists the folder", "cook_face.png" in turn
       and "These reference files are on disk" in turn)
    turn = PL.build_user_turn("she talks about life", 2, FILES, pinned=pinned)
    ck("a filled rail does not dump the folder",
       "@ref_1" in turn and "apron.png" not in turn
       and "already on the rail" in turn)
    ck("empty subjects is named as a reject",
       "empty" in turn.lower() and "subjects" in turn)

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

    # Stills ride the first user turn only. Repair stays text so attempt 2
    # does not re-send two JPEGs.
    fn = scripted([fence(rail_shots, rail)])
    out = asyncio.run(
        PL.write_plan("she talks about life", 2, complete_fn=fn,
                      pinned=pinned,
                      images=[{"tag": "ref_1",
                               "data_url": "data:image/jpeg;base64,xx"}],
                      use_schema=False))
    ck("a pinned write converges", out["ok"] is True, "; ".join(out["errors"][:1]))
    first = fn.seen[0][1]["content"]
    ck("attempt 1 is multimodal",
       isinstance(first, list)
       and any((p or {}).get("type") == "image_url" for p in first))
    ck("the still is labelled with its tag",
       any("@ref_1 is this picture" in str((p or {}).get("text") or "")
           for p in first))

    # End to end on the live fault. Attempt 1 renames every row and never
    # takes it back; before the remap this could not converge at all, because
    # the merge carried attempt 1's names into every later register.
    fn = scripted([fence(renamed_shots, renamed)])
    out = asyncio.run(
        PL.write_plan("she talks about her day", 2, complete_fn=fn,
                      pinned=pinned, use_schema=False))
    ck("a write that renames the rail still converges",
       out["ok"] is True, "; ".join(out["errors"][:1]))
    ck("and it converges on the first attempt", out["attempts"] == 1)
    ck("the accepted register carries the rail's tags",
       {r["tag"] for r in json.loads(out["ref_plan"])["refs"]}
       == {"ref_1", "ref_2"}, out["ref_plan"][:120])
    ck("the accepted beat cites the rail's tags",
       "@ref_1" in out["shot_plan"] and "girl_face" not in out["shot_plan"])

    # One attempt's invented name must not outlive it. This is what made the
    # live run fail on `@girl_face` even after the model had corrected itself.
    stale = PL._merge_register(
        json.dumps(renamed), json.dumps(rail), keep={"ref_1", "ref_2"})
    ck("a stale off-rail tag is dropped from the merge",
       {r["tag"] for r in json.loads(stale)["refs"]} == {"ref_1", "ref_2"},
       stale[:120])

    # ------------------------------------------------- the repair-turn schema
    print()
    print("planner._tighten_schema")
    base = PL.schema()
    ck("the shipped schema loads", isinstance(base, dict))
    # The permissive shape is deliberate and must stay: a plan with no people
    # in it is legitimate, so the FIRST attempt may still say subjects: {}.
    subs0 = base["properties"]["ref_plan"]["properties"]["subjects"]
    ck("the shipped schema still allows an empty subjects",
       "minProperties" not in subs0 and not subs0.get("required"))
    ck("and still leaves desc optional",
       "desc" not in (base["properties"]["ref_plan"]["properties"]["refs"]
                      ["items"].get("required") or []))

    tight = PL._tighten_schema(base, ["1"])
    titem = tight["properties"]["ref_plan"]["properties"]["refs"]["items"]
    tsubs = tight["properties"]["ref_plan"]["properties"]["subjects"]
    ck("the repair schema requires desc on every ref",
       "desc" in titem["required"]
       and titem["properties"]["desc"]["minLength"] == 1)
    ck("it requires the subject the errors named",
       tsubs["required"] == ["1"] and "1" in tsubs["properties"])
    ck("with name, locked and context all mandatory",
       tsubs["properties"]["1"]["required"] == ["name", "locked", "context"])
    ck("and none of the three may be empty",
       all(tsubs["properties"]["1"]["properties"][k]["minLength"] == 1
           for k in ("name", "locked", "context")))
    ck("patternProperties is gone from the repair schema",
       "patternProperties" not in tsubs)
    ck("the shipped schema is not mutated",
       "minProperties" not in
       base["properties"]["ref_plan"]["properties"]["subjects"]
       and "desc" not in (base["properties"]["ref_plan"]["properties"]["refs"]
                          ["items"].get("required") or []))
    ck("two subjects are both required",
       PL._tighten_schema(base, ["1", "2"])["properties"]["ref_plan"]
       ["properties"]["subjects"]["required"] == ["1", "2"])
    ck("no numbers means no tightening", PL._tighten_schema(base, []) is None)
    ck("no schema means no tightening", PL._tighten_schema(None, ["1"]) is None)

    # The repair turn must actually be sent the tightened grammar.
    seen_sch = []

    def sch_recorder(replies):
        n = {"i": 0}

        async def complete_fn(messages, schema=None):
            seen_sch.append(schema)
            n["i"] += 1
            return replies[min(n["i"], len(replies)) - 1]
        return complete_fn

    blank = json.loads(json.dumps(rail))
    blank["subjects"] = {}
    out = asyncio.run(PL.write_plan(
        "she talks about life", 2,
        complete_fn=sch_recorder([fence(rail_shots, blank),
                                  fence(rail_shots, rail)]),
        pinned=pinned, attempts=2))
    ck("the prose repair converges", out["ok"] is True,
       "; ".join(out["errors"][:1]))
    ck("attempt 1 gets the shipped schema",
       seen_sch[0] is not None
       and "minProperties" not in (seen_sch[0]["properties"]["ref_plan"]
                                   ["properties"]["subjects"]))
    ck("attempt 2 gets the tightened one",
       (seen_sch[1]["properties"]["ref_plan"]["properties"]["subjects"]
        .get("required")) == ["1"], repr(len(seen_sch)))

    empty_sub = json.loads(json.dumps(GOOD_REFS))
    empty_sub["subjects"] = {}
    fn = scripted([
        fence(GOOD_SHOTS, empty_sub),
        json.dumps({"subjects": GOOD_REFS["subjects"]}),
    ])
    out = asyncio.run(
        PL.write_plan("a cook in a kitchen", 3, complete_fn=fn, files=FILES,
                      use_schema=False))
    ck("empty subjects repair merges", out["ok"] is True, "; ".join(out["errors"][:1]))
    ck("in two attempts", out["attempts"] == 2, str(out["attempts"]))
    ck("the original refs survived",
       json.loads(out["ref_plan"])["refs"][0]["tag"] == "cook_face")
    ck("subjects landed",
       json.loads(out["ref_plan"])["subjects"]["1"]["name"] == "the cook")

    fn = scripted([fence(GOOD_SHOTS, empty_sub)] * 3)
    out = asyncio.run(
        PL.write_plan("a cook in a kitchen", 3, complete_fn=fn, files=FILES,
                      use_schema=False, attempts=3))
    ck("a stub fills subjects after three empty replies",
       out["ok"] is True, "; ".join(out["errors"][:1]))
    ck("and says they were filled in",
       any("filled from the photo" in w for w in out["warnings"]),
       "; ".join(out["warnings"][-1:]))

    # ------------------------------------------------------- the killswitch
    # Only the parts that need no server. `unload_all` short-circuits on a
    # non-local host BEFORE it opens a session, so this asserts the guard
    # without touching the network.
    print("\nllm.unload_all -- the local-only guard")
    LM = importlib.import_module("htcpack.llm")

    ck("localhost shares this GPU", LM.shares_this_gpu("http://127.0.0.1:1234"))
    ck("a bare host with no scheme still resolves",
       LM.shares_this_gpu("localhost:1234"))
    # TEST-NET-1, guaranteed unroutable and never this machine.
    ck("a remote host does not",
       not LM.shares_this_gpu("http://192.0.2.1:1234"))

    n, note = asyncio.run(LM.unload_all("http://192.0.2.1:1234", "some-model"))
    ck("a remote writer is never evicted", n == 0, note)
    ck("and it says why", "another machine" in note, note)

    n, note = asyncio.run(LM.unload_all("", "some-model"))
    ck("no server configured is not an error", n == 0, note)

    # -- the render-time eviction ------------------------------------------
    #
    # `free_for_render` runs at the top of run(), so each of its failure modes
    # is a failure mode of EVERY render -- including the overwhelming majority
    # that never open the plan writer. When anything is wrong it has exactly one
    # acceptable behaviour: return quietly and cost no wall time. `settle_sleep`
    # exists so this file can prove the second half without waiting 5 s for it.
    #
    # `_conn_path` is redirected at a temp file first. These cases call
    # `save_conn`, and a checker that overwrites the user's real writer
    # settings as a side effect of passing is not a checker anyone should run.
    import tempfile
    _saved_path, _saved_conn = LM._conn_path, dict(LM.CONN)
    _tmp = os.path.join(tempfile.mkdtemp(), "htc_llm.json")
    LM._conn_path = lambda: _tmp
    _slept = []
    try:
        ck("an unconfigured writer is free, and instant",
           LM.free_for_render(settle_sleep=_slept.append) == "" and not _slept)

        # Configured, but pointed at TEST-NET-1. `shares_this_gpu` must refuse
        # it before any eviction, and nothing may sleep: waiting for a driver on
        # someone else's machine to release memory it never allocated would be
        # dead time on every single queue.
        LM.save_conn({"model": "some-model",
                      "server_url": "http://192.0.2.1:1234",
                      "unload_on_run": True})
        _slept.clear()
        ck("a remote writer costs no settle",
           LM.free_for_render(settle_sleep=_slept.append) == "" and not _slept,
           str(_slept))

        LM.save_conn({"unload_on_run": False})
        _slept.clear()
        ck("unload_on_run off does nothing at all",
           LM.free_for_render(settle_sleep=_slept.append) == "" and not _slept)

        LM.save_conn({"model": "some-model",
                      "server_url": "http://192.0.2.1:1234",
                      "unload_on_run": True})
        _slept.clear()

        async def _from_loop():
            return LM.free_for_render(settle_sleep=_slept.append)

        nested = asyncio.run(_from_loop())
        ck("free_for_render from a running loop does not raise",
           nested == "" and not _slept, repr(nested))

        ck("text_only drops image parts",
           LM.text_only([{"role": "user", "content": [
               {"type": "text", "text": "hello"},
               {"type": "image_url", "image_url": {"url": "data:,"}},
           ]}])[0]["content"] == "hello")
        ck("has_images sees a vision turn",
           LM.has_images([{"role": "user", "content": [
               {"type": "image_url", "image_url": {"url": "data:,"}}]}]))

        ck("the settle is capped",
           LM.save_conn({"vram_settle_s": 9999})["vram_settle_s"] == 60.0)
        ck("a nonsense settle is ignored, not stored",
           LM.save_conn({"vram_settle_s": "soon"})["vram_settle_s"] == 60.0)
    finally:
        LM._conn_path = _saved_path
        LM.CONN.clear()
        LM.CONN.update(_saved_conn)

    ck("warm/evict/settle are real settings",
       {"keep_warm", "unload_on_run", "vram_settle_s"} <= set(LM._CONN_KEYS))
    ck("unload_after is gone", "unload_after" not in LM._CONN_KEYS)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
