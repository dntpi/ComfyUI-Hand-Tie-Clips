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

    # ...but the writer loop repairs it before validate ever sees it, because
    # the rail already holds the answer. gemma renamed gibsonlethal.webp to
    # hero_face.webp live on 2026-09-02: one attempt for the mismatch itself,
    # and a second because a file error is not a prose gap, so it suppressed
    # the tightened-schema repair for a whole round.
    healed, refiled = PL._restore_pinned_files(json.dumps(stolen), pinned)
    ck("the rail's filename is put back", refiled == {"ref_1": "cook_face.png"},
       repr(refiled))
    errs2, _ = PL.validate(json.dumps(rail_shots), healed,
                           hops=2, known_files=[p["file"] for p in pinned],
                           pinned=pinned)
    ck("and the repaired plan then validates", not errs2, "; ".join(errs2[:2]))
    ck("a register needing no repair is returned unchanged",
       PL._restore_pinned_files(healed, pinned) == (healed, {}))

    # NOT a RAIL_ONLY_FIELD. That loop drops what the rail cannot supply, which
    # would delete the only filename a brief-only write has.
    loose = json.dumps({"refs": [{"tag": "someone", "file": "found.png",
                                  "subject": 1, "desc": "a face"}],
                        "subjects": {}})
    ck("a brief-only write keeps the filename the model chose",
       PL._restore_pinned_files(loose, []) == (loose, {}))
    ck("and so does a tag the rail does not pin",
       PL._restore_pinned_files(loose, pinned) == (loose, {}))

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

    # The two reference lints. Both are warnings: the node renders these
    # plans, and promoting a prose rule into the retry loop rewrites the parts
    # that were already right.
    def two_plates(ret3, shots3, shots1=(1, 2)):
        return json.dumps({
            "refs": [
                {"tag": "ref_1", "file": "cook_face.png", "subject": 1,
                 "retention": "fully_preserved", "desc": "a face",
                 "shots": list(shots1)},
                {"tag": "ref_3", "file": "apron.png", "subject": 1,
                 "retention": ret3, "desc": "a whole outfit",
                 "shots": list(shots3)},
                {"tag": "ref_2", "file": "kitchen.png",
                 "retention": "reference", "desc": "a kitchen",
                 "shots": [1, 2]},
            ],
            "subjects": GOOD_REFS["subjects"]})

    kf = ["cook_face.png", "apron.png", "kitchen.png"]
    errs, warns = PL.validate(json.dumps(rail_shots),
                              two_plates("fully_preserved", [1, 2]),
                              hops=2, known_files=kf)
    ck("two fully_preserved plates on one subject is a warning",
       any("fully_preserved" in w and "wardrobe" in w for w in warns),
       "; ".join(warns[:1])[:110])
    ck("it names both plates",
       any("@ref_1" in w and "@ref_3" in w for w in warns))
    ck("and it is never an error", not errs, "; ".join(errs[:1]))

    _, warns = PL.validate(json.dumps(rail_shots),
                           two_plates("partially_copy", [1]),
                           hops=2, known_files=kf)
    ck("a wardrobe plate marked partially_copy is quiet",
       not any("wardrobe" in w for w in warns), "; ".join(warns[:1])[:100])

    # The mirror hazard: a hop with no plate for a subject.
    _, warns = PL.validate(json.dumps(rail_shots),
                           two_plates("partially_copy", [1], shots1=(1,)),
                           hops=2, known_files=kf)
    ck("a subject with no plate on hop 2 is a warning",
       any("no picture on hop" in w for w in warns), "; ".join(warns[:1])[:110])
    ck("it says drift does not self-correct",
       any("self-correct" in w for w in warns))

    _, warns = PL.validate(json.dumps(rail_shots),
                           two_plates("partially_copy", [1]),
                           hops=2, known_files=kf)
    ck("a face plate on every hop is quiet",
       not any("no picture on hop" in w for w in warns))

    # The live plan of 2026-09-02, which tripped exactly one of the two.
    live, _ = PL.validate(json.dumps(rail_shots),
                          two_plates("fully_preserved", [1, 2]),
                          hops=2, known_files=kf)
    ck("the live plan is still accepted", not live)

    # `mp` is the rail's, not the model's. It is absent from the schema, so
    # every written register comes back without one -- and Accept overwrites
    # the rail with that. chain_00047 ran three plates at 0.54 MP; the next
    # Write plan put them back at native size (1.58 MP -> 3.23 MP against a
    # 0.72 MP canvas) and the render came back as the reference photograph.
    rail_mp = [{"tag": "ref_1", "file": "cook_face.png", "mp": 0.54},
               {"tag": "ref_2", "file": "kitchen.png", "mp": 0.3}]
    written = json.dumps({"refs": [{"tag": "ref_1", "file": "cook_face.png",
                                    "subject": 1, "desc": "a face"},
                                   {"tag": "ref_2", "file": "kitchen.png",
                                    "retention": "reference",
                                    "desc": "a kitchen"}],
                          "subjects": {}})
    back = json.loads(PL._restore_rail_only(written, rail_mp))
    by_tag = {r["tag"]: r for r in back["refs"]}
    ck("a written register gets the rail's `mp` back",
       by_tag["ref_1"].get("mp") == 0.54 and by_tag["ref_2"].get("mp") == 0.3,
       f"got {by_tag['ref_1'].get('mp')!r} / {by_tag['ref_2'].get('mp')!r}")
    ck("and a rail with no caps set changes nothing",
       PL._restore_rail_only(written, [{"tag": "ref_1", "file": "cook_face.png"}])
       == written)

    # The rail is authoritative, not merely restorative. gemma4-26b read `mp`
    # as pixels and returned 1000000000; it cleared the floor, survived into
    # the register and filled the rail dropdown with zeroes.
    junk = json.dumps({"refs": [{"tag": "ref_1", "file": "cook_face.png",
                                 "subject": 1, "desc": "a face",
                                 "mp": 1000000000}], "subjects": {}})
    cleared = json.loads(PL._restore_rail_only(
        junk, [{"tag": "ref_1", "file": "cook_face.png"}]))
    ck("a model-authored `mp` is dropped when the rail has no cap",
       "mp" not in cleared["refs"][0], f"got {cleared['refs'][0].get('mp')!r}")
    overruled = json.loads(PL._restore_rail_only(junk, rail_mp))
    ck("and overruled when the rail has one",
       overruled["refs"][0].get("mp") == 0.54)

    # Spoken WORDS, not lines. A line runs from six words to twenty, and six
    # words is 2.4 s of a 10 s hop: chain_00059 was written one six-word line
    # per 10 s hop and rendered 17.8% voiced, longest run 0.5 s.
    ck("apostrophes are not quote delimiters",
       PL.count_beat("She says, 'Today's class was long.' She waits.") == (8, 1, 4),
       repr(PL.count_beat("She says, 'Today's class was long.' She waits.")))
    ck("two spans are counted as two lines",
       PL.count_beat("'I am happy!' and 'I never want to leave.'")[1:] == (2, 8),
       repr(PL.count_beat("'I am happy!' and 'I never want to leave.'")))

    def _talk(spoken):
        return json.dumps({"shots": [
            {"id": "s1", "beat": "@ref_1 walks and says, '" + spoken
                                 + "' She keeps going as the clip ends.",
             "directives": {"tail": "hold"}}]})

    thin = " ".join(["word"] * 6)          # 2.4 s of a 10 s hop
    full = " ".join(["word"] * 25)         # about what 10 s holds
    for label, spoken, want in (("a six-word line in a 10 s hop is flagged", thin, True),
                                ("twenty-five words is not", full, False)):
        _, ws = PL.validate(_talk(spoken), json.dumps(GOOD_REFS), hops=1,
                            known_files=kf, duration="10 s")
        ck(label, any("spoken lines run about" in w for w in ws) == want,
           "; ".join(w for w in ws if "spoken lines run" in w)[:110])

    # A plan may be written in any language; only the beats around the quotes
    # have to be English. Counting a Korean line's whitespace tokens with the
    # word rate called a real 4-5 s line 3.2 s, and the "roughly N words"
    # target it derived asked for about 85 syllables in a 10 s hop. Live, on
    # 2026-09-02: a 3-hop Korean vlog warned on every shot for being too thin
    # when two of the three were fine.
    KO = chr(0xd55c) + chr(0xad6d) + chr(0xc5d0) + chr(0xc11c) + chr(0xc758)
    KO_LINE = KO + " " + chr(0xc2dc) + chr(0xac04) + chr(0xc740)   # 8 syllables
    ck("a CJK line is counted in syllables, not tokens",
       abs(PL.speech_seconds(KO_LINE) - 8 / PL.SPEECH_SPS) < 0.01,
       "%.2f" % PL.speech_seconds(KO_LINE))
    ck("English still uses the word rate",
       abs(PL.speech_seconds("one two three four five") - 5 / PL.SPEECH_WPS) < 0.01,
       "%.2f" % PL.speech_seconds("one two three four five"))
    ck("mixed text counts both scripts",
       abs(PL.speech_seconds(KO_LINE + " and five more words here")
           - (8 / PL.SPEECH_SPS + 5 / PL.SPEECH_WPS)) < 0.01,
       "%.2f" % PL.speech_seconds(KO_LINE + " and five more words here"))
    ck("stranded punctuation is not a spoken word",
       PL.speech_seconds(KO_LINE + " ! . ?") == PL.speech_seconds(KO_LINE),
       "%.2f vs %.2f" % (PL.speech_seconds(KO_LINE + " ! . ?"),
                         PL.speech_seconds(KO_LINE)))
    ck("the word rate would have undercounted it",
       len(KO_LINE.split()) / PL.SPEECH_WPS < PL.speech_seconds(KO_LINE),
       "%.2f vs %.2f" % (len(KO_LINE.split()) / PL.SPEECH_WPS,
                         PL.speech_seconds(KO_LINE)))

    # ...and the shortfall warning has to name the target in the unit the line
    # is written in. "roughly 25 words" of Korean is three times the hop.
    thin_ko = json.dumps({"shots": [
        {"id": "s1", "beat": "@ref_1 walks and says, '" + KO
                             + "' She keeps going as the clip ends.",
         "directives": {"tail": "hold"}}]})
    _, ws = PL.validate(thin_ko, json.dumps(GOOD_REFS), hops=1,
                        known_files=kf, duration="10 s")
    short = [w for w in ws if "spoken lines run about" in w]
    ck("a thin CJK hop is still flagged", bool(short), "; ".join(ws)[:110])
    ck("and its target is in syllables",
       bool(short) and "syllables" in short[0],
       short[0][:120] if short else "")

    # Rule 3 applies INSIDE a hop. A beat that opens on action and speaks later
    # has frames with a picture and no sound, and the model fills them with
    # dialogue nobody wrote. Live on 2026-09-02: hop 1 ended silent -- which
    # rule 5 asks for, and the audio pin duly carried one second of silence --
    # and hop 2 still opened on invented speech over the walk. Nothing in the
    # pack covered a hop's own opening; rules 3 and 6 are both whole-hop.
    WALK = ("@ref_1 walks the whole length of the platform past the yellow "
            "line and turns to face the camera, then says, ")
    LINE = "'" + " ".join(["word"] * 25) + "' still walking as the clip ends."

    def _lead(beat):
        return json.dumps({"shots": [
            {"id": "s1", "beat": beat, "directives": {"tail": "ongoing"}}]})

    for label, beat, want in (
            ("action before the first line with no sound is flagged",
             WALK + LINE, True),
            ("the same beat naming a sound is not",
             WALK.replace("platform past", "platform, her boots knocking on "
                          "the concrete, past") + LINE, False),
            ("a beat that speaks straight away is not",
             "@ref_1 says, " + LINE, False),
            ("a beat with no dialogue at all is not",
             "@ref_1 walks the whole length of the platform past the yellow "
             "line and turns to face the camera, still walking as it ends.",
             False)):
        _, ws = PL.validate(_lead(beat), json.dumps(GOOD_REFS), hops=1,
                            known_files=kf, duration="10 s")
        got = any("before the first spoken line" in w for w in ws)
        ck(label, got == want,
           "; ".join(w for w in ws if "first spoken line" in w)[:130])

    # `context` is injected verbatim on EVERY continuation hop, so a posture
    # written into it is asserted on hops whose beats disagree. Live: a wardrobe
    # plate correctly held to shots [1] leaked its POSE anyway -- "a white
    # ribbed crop top, sitting at a wooden table" against beats walking a train
    # platform -- and hop 3 obeyed the context and sat her down. Presence is not
    # the fault: chain_00052 carried "sitting on a wooden counter" against beats
    # that had her sitting on one, and was right. The contradiction is.
    def _ctx(context, *beats):
        sp = json.dumps({"shots": [{"id": "s%d" % (i + 1), "beat": b,
                                    "directives": {"tail": "hold"}}
                                   for i, b in enumerate(beats)]})
        rf = json.dumps({"refs": [{"tag": "ref_1", "file": "cook_face.png",
                                   "subject": 1, "retention": "fully_preserved",
                                   "desc": "a face", "shots": [1, 2]}],
                         "subjects": {"1": {"name": "her", "locked": "same face",
                                            "context": context}}})
        _, ws = PL.validate(sp, rf, hops=len(beats), known_files=kf, duration="10 s")
        return any("context says" in w for w in ws)

    for label, got, want in (
            ("a seated context against walking beats is flagged",
             _ctx("a white crop top, sitting at a wooden table",
                  "she walks along the platform", "she moves past the lines"), True),
            ("a seated context against seated beats is not",
             _ctx("a white crop top, sitting on a wooden counter",
                  "she sits on the counter", "she stays seated, shifting"), False),
            ("wardrobe alone never warns",
             _ctx("a white ribbed crop top and denim shorts",
                  "she walks along the platform", "she slows her pace"), False)):
        ck(label, got == want, "warned=%s wanted=%s" % (got, want))

    # A wardrobe plate on a continuation hop brings its own room. chain_00034
    # was this on hop 2; the 3x10 s portrait chain on 2026-09-02 was this on
    # hop 3 -- podcast_host.jpg is her sitting in a studio, it rode all three
    # hops, and "she turns the camera toward her face" cut to that room and
    # back. SYSTEM_PROMPT.md:317 states the rule; nothing checked it.
    def _wardrobe(shots3):
        return json.dumps({"refs": [
            {"tag": "ref_1", "file": "cook_face.png", "subject": 1,
             "retention": "fully_preserved", "desc": "a face", "shots": [1, 2]},
            {"tag": "ref_3", "file": "apron.png", "subject": 1,
             "retention": "partially_copy", "desc": "an outfit", "shots": shots3},
        ], "subjects": GOOD_REFS["subjects"]})

    for label, s3, want in (("a wardrobe plate riding hop 2 is flagged", [1, 2], True),
                            ("on hop 1 only it is not", [1], False)):
        _, ws = PL.validate(json.dumps(rail_shots), _wardrobe(s3),
                            hops=2, known_files=kf)
        ck(label, any("wardrobe plate" in w for w in ws) == want,
           "; ".join(w for w in ws if "wardrobe" in w)[:110])

    # ...including when the model kept its own tag, so the tag lookup misses.
    # `_remap_pinned_tags` restores rail names by filename but only when the
    # model supplied one it can match; live, gemma4-26b left @woman_face in
    # place with mp 1e+15 and spent an attempt on a rejection the rail already
    # had the answer to.
    unrenamed = json.dumps({"refs": [{"tag": "woman_face", "file": "cook_face.png",
                                      "subject": 1, "desc": "a face",
                                      "mp": 1e15}], "subjects": {}})
    byfile = json.loads(PL._restore_rail_only(
        unrenamed, [{"tag": "ref_1", "file": "cook_face.png"}]))
    ck("an unrenamed tag is matched by filename",
       "mp" not in byfile["refs"][0], f"got {byfile['refs'][0].get('mp')!r}")

    # ...and with an EMPTY rail, which is a brief-only write. Gated on `pinned`
    # this no-opped, the invented value reached parse_ref_plan, and that RAISES
    # -- short-circuiting every other check, so three attempts died on one line
    # about megapixels and the missing files were never reported. Live: -1,
    # then 1e+15, from gemma4-26b on 2026-09-02.
    invented = json.dumps({"refs": [
        {"tag": "influencer_face", "file": "ref_1.jpg", "subject": 1,
         "desc": "a face", "mp": -1},
        {"tag": "platform", "file": "ref_2.jpg", "retention": "reference",
         "desc": "a platform", "mp": 1e15}], "subjects": {}})
    for label, pin in (("with an empty rail", []), ("with no rail at all", None)):
        got = json.loads(PL._restore_rail_only(invented, pin))
        ck(f"an invented `mp` is dropped {label}",
           all("mp" not in r for r in got["refs"]),
           repr([r.get("mp") for r in got["refs"]]))

    bad = json.dumps({"refs": [{"tag": "ref_1", "file": "cook_face.png",
                                "retention": "reference", "desc": "a face",
                                "mp": 1000000000}], "subjects": {}})
    errs, _ = PL.validate(json.dumps(rail_shots), bad, hops=2, known_files=kf)
    ck("and a register that keeps one is rejected, naming the units",
       any("MEGApixels" in e for e in errs), "; ".join(errs)[:130])

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

    # 1.0.2 replaced an address-set intersection with a bind test. The old code
    # asked the OS for this machine's own name and enumerated every address
    # behind it, which is host enumeration however local the intent, and the
    # Comfy registry's YARA scan is right to read it that way. A bind answers
    # the same question and looks like nothing. Asserted at source level rather
    # than by behaviour because the two implementations agree on every input a
    # checker can name -- the difference is only visible in the text.
    with open(LM.__file__, encoding="utf-8") as _fh:
        _llm_src = _fh.read()
    ck("no host enumeration in llm.py",
       "gethostname" not in _llm_src and "gethostbyname" not in _llm_src)

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

    # A truncated reply is the context window, not the model's formatting.
    # This cost three attempts and an error blaming the wrong thing.
    def cut(prompt, completion, reasoned=0,
            content='{ "shot_plan": {'):
        return {"choices": [{"finish_reason": "length",
                             "message": {"content": content}}],
                "usage": {"prompt_tokens": prompt,
                          "completion_tokens": completion,
                          "completion_tokens_details":
                              {"reasoning_tokens": reasoned}}}

    def why(d, max_tokens=12288):
        try:
            LM._content(d, max_tokens=max_tokens)
        except LM.LLMError as exc:
            return str(exc)
        return ""

    msg = why(cut(7929, 263))
    ck("a truncated reply is an error, not half a plan", bool(msg))
    ck("it names what the prompt cost", "7929" in msg, msg[:80])
    ck("it names a context size that would fit", "32768" in msg, msg[:120])
    ck("it does not blame the model's formatting",
       "JSON" not in msg and "format" not in msg, msg[:80])
    ck("a small prompt still gets a floor of 16384",
       "16384" in why(cut(400, 30)), why(cut(400, 30))[:100])
    ck("reasoning tokens are called out when present",
       "reasoning" in why(cut(7929, 210, reasoned=210)))
    ck("our own ceiling is reported as ours, not the context",
       "ceiling" in why(cut(900, 12288)) and "context" not in
       why(cut(900, 12288)), why(cut(900, 12288))[:90])
    ck("a clean reply is still returned untouched",
       LM._content({"choices": [{"finish_reason": "stop",
                                 "message": {"content": " ok "}}]}) == "ok")


    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
