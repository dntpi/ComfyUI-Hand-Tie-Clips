"""SWAP's instruct and its validator agree. CPU, scripted model, no server, no GPU.

Each check is a gap found in review (2026-10-11): keep_person carried the face
through the register default; the validator held none of the instruct's rules
(identity named twice, the hop's word band, negation); a VIDEO_DESC naming the
person went unchecked; the instruct's example reply hard-coded a held medium
shot, asked for spoken lines with no syntax against a clip with its own sound,
and never said what a clip shorter than the hop, or a second person, means.

Run: python tools/check_swap_instruct.py
"""
import asyncio, importlib.util, json, os, sys, types

HTC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("vp"); pkg.__path__ = [HTC]; sys.modules["vp"] = pkg
fake = types.ModuleType("vp.h3_ref_chain"); fake.DURATION_FRAMES = {"5 s": 124, "8 s": 192, "10 s": 243}; fake.FPS = 24
sys.modules["vp.h3_ref_chain"] = fake
for n in ("directives", "plan", "refs", "hand_tie", "planner"):
    s = importlib.util.spec_from_file_location("vp." + n, os.path.join(HTC, n + ".py"))
    m = importlib.util.module_from_spec(s); sys.modules["vp." + n] = m; s.loader.exec_module(m)
PL = sys.modules["vp.planner"]
FAIL = []


def ck(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  -- " + str(detail)[:300]))
    if not cond:
        FAIL.append(name)


# The scripted model's answer, in the shape the instruct now asks for: the
# COMPLETE six-field block, with the task type in `summary:`.
#
# It used to be a prose beat. Three checks downstream failed on the fixture
# rather than on the code, which is the fourth time this project a stale test
# fixture has out-reported a real defect. The contract gate added with the
# rework rejects a prose beat outright, so a prose fixture here is no longer a
# fixture -- it is the failure the gate exists to catch.


def contract(defs, visual, prefix="[video generation + reference]"):
    return ("subject_definitions:\n" + defs
            + "\n\nsummary:\n" + prefix + " Replace the performer in <Video 1>, keeping the "
            "scene, camera and the complete motion of the guide video."
            + "\n\ndetailed_description:\n[Shot 1] 0 to 8 s\n(VISUAL) " + visual
            + "\n(SOUND) The room tone.\n\noverall_soundscape:\nThe room tone."
            + "\n\nnon_diegetic_music:\nN/A")


HEAD_DEFS = ("<Subject 1> is the person in <Video 1>. The face, hair and skin tone of <Subject 1> "
             "come from @her_face. <Subject 1> keeps the movement, the camera path and the shot "
             "timing of <Video 1>.")
GOOD_VISUAL = ("<Subject 1> spins across the kitchen tiles, arms flung wide as the skirt "
               "flares and settles, boots squeaking on tile while a fridge hums low. The spin "
               "slows to a standstill facing the window, hair, face and surface steady in "
               "every frame with no texture crawling.")
GOOD = contract(HEAD_DEFS, GOOD_VISUAL)

KEEP_DEFS = ("<Subject 1> is the person in <Video 1>. The face, hair, skin, build and "
             "clothing of <Subject 1> stay those of <Video 1> in every frame. <Video 1> "
             "supplies the movement, the camera path, the shot timing and the setting.")
KEEP_VISUAL = ("<Subject 1> spins across the kitchen tiles, arms flung wide, the skirt "
               "flaring and settling as the spin slows. Boots squeak on tile, a fridge "
               "hums low, and <Subject 1> comes to rest facing the window, the surface "
               "steady in every frame.")
KEEP = contract(KEEP_DEFS, KEEP_VISUAL)
calls = []


def run(mode, replies, **kw):
    calls.clear()
    async def fc(messages, schema=None):
        calls.append(messages[-1]["content"] if isinstance(messages[-1]["content"], str) else
                     next((c.get("text") for c in messages[-1]["content"] if c.get("type") == "text"), ""))
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        return r if isinstance(r, str) else json.dumps({"shot_plan": {"shots": [r]}})
    kw.setdefault("identity_tag", "her_face")
    kw.setdefault("rail_tags", ["her_face", "kitchen"])
    return asyncio.run(PL.write_swap_plan("spin", complete_fn=fc, duration="8 s", mode=mode, **kw))


print("0. the gate fails closed")
ck("a complete block passes", PL.swap_contract_errors(GOOD, identities=["her_face"]) == ([], []),
   PL.swap_contract_errors(GOOD, identities=["her_face"]))
ck("a prose beat is refused, naming the header",
   any("not a complete H3 block" in e for e in
       PL.swap_contract_errors("The subject spins across the tiles.", identities=["her_face"])[0]),
   "a beat is what the gate exists to catch")
ck("...and a typo'd header is caught, not compiled",
   any("subject_definitions:" in e for e in
       PL.swap_contract_errors(GOOD.replace("subject_definitions:", "subject definitions:"),
                               identities=["her_face"])[0]))
ck("the wrong task type is an error",
   any("must open with" in e for e in
       PL.swap_contract_errors(GOOD.replace("video generation + reference", "video editing"),
                               identities=["her_face"])[0]))
ck("the drift clause the instruct asks for is NOT a negation",
   PL.swap_contract_errors(GOOD, identities=["her_face"]) == ([], [])
   and any("negation" in e for e in
           PL.swap_contract_errors(GOOD.replace("boots squeaking on tile", "no boots on tile"),
                                   identities=["her_face"])[0]),
   # Mutated on the sentence's own wording. The earlier version replaced "hold
   # steady", which stopped existing when the clause was reworded -- so the
   # mutation became a no-op, the gate returned clean, and the check passed
   # without having proved anything. A text check whose mutation goes quiet is
   # a check that has stopped checking.
   "the gate must not reject the sentence its own instruct requires")

print("\n0b. the render-time pass, which is the one that must hold")
# shape_only is what run() calls, with no identity list. It has to catch the
# misparse -- the one that silently prepends the register's identity prose --
# and it has to pass a block whose identity is real but unknown to it.
_TYPO = GOOD.replace("subject_definitions:", "subject definitions:")
ck("a hand-edited swap is refused at render time",
   any("not a complete H3 block" in e for e in PL.swap_contract_errors(_TYPO, shape_only=True)[0]),
   "a hand-edit or a stale workflow is what the write-time gate cannot see")
ck("...but a valid swap with an unknown identity is not refused for that",
   PL.swap_contract_errors(GOOD, shape_only=True) == ([], []),
   PL.swap_contract_errors(GOOD, shape_only=True))
ck("the render-time pass still catches the task type",
   any("must open with" in e for e in PL.swap_contract_errors(
       GOOD.replace("video generation + reference", "video editing"), shape_only=True)[0]))
ck("...and a dropped drift clause",
   any("drift clause" in e for e in PL.swap_contract_errors(
       GOOD.replace("steady in every frame with no texture crawling", "in place"),
       shape_only=True)[0]))
ck("...and plural pronouns",
   any("two people" in e.lower() for e in PL.swap_contract_errors(
       GOOD + "\n\nThey settle at the window.", shape_only=True)[0]))
ck("the identity half is skipped, not stubbed",
   PL.swap_contract_errors(GOOD, identities=[], shape_only=True) == ([], [])
   and PL.swap_contract_errors(GOOD, identities=[], mode="replace_person") != ([], []),
   "shape_only must skip the identity block, not quietly check nothing")

print("\n1. keep_person never carries the face")
o = run("keep_person", [{"beat": KEEP, "directives": {"tail": "settle"}}])
ck("ok", o.get("ok"), o)
ck("refs are explicitly [] (not absent -> register default)",
   json.loads(o["shot_plan"])["shots"][0].get("refs") == [], o.get("shot_plan"))
o = run("keep_person", [{"beat": contract(KEEP_DEFS, KEEP_VISUAL.replace(
                                   "<Subject 1> spins", "<Subject 1> spins, looking like @her_face,")),
                                   "directives": {"tail": "settle"}},
                        {"beat": KEEP, "directives": {"tail": "settle"}}])
ck("citing the identity under keep_person is repaired, not queued broken",
   o.get("ok") and len(calls) == 2 and "swaps nobody" in calls[1], (o.get("errors"), len(calls)))
o = run("keep_person", [{"beat": contract(KEEP_DEFS, KEEP_VISUAL.replace(
                                   "The subject spins", "The subject spins in @kitchen,")),
                                   "directives": {"tail": "settle"}}],
        background="picture", background_tag="kitchen")
ck("keep_person with a picture background rides exactly that plate",
   o.get("ok") and json.loads(o["shot_plan"])["shots"][0].get("refs") == ["kitchen"], o)

print("\n2. the validator holds the instruct's rules")
once = {"beat": contract("The subject spins across the kitchen tiles looking like @her_face.",
                         GOOD_VISUAL), "directives": {"tail": "settle"}}
o = run("head_swap", [once, dict(once, beat=GOOD)])
ck("a block that never defines <Subject 1> is an error and goes back once",
   o.get("ok") and len(calls) == 2 and "<Subject 1>" in calls[1],
   # `calls[1] if len(calls) > 1 else ""` rather than `calls[1]`: when the rule
   # is removed the repair turn never happens, calls has one entry, and the
   # detail expression raised IndexError -- the check CRASHED on exactly the
   # failure it exists to report. That is the same failure as a check that
   # passes on nothing, wearing a louder hat.
   (len(calls), o.get("errors"), calls[1] if len(calls) > 1 else "(no repair turn)"))
o = run("head_swap", [dict(once, beat=GOOD.replace("boots squeaking on tile", "no boots on tile"))])
# Asserted on the result, not on the call count: a single-reply fixture retries
# the same bad block until MAX_ATTEMPTS, so the number of turns is an artefact
# of the loop's budget rather than of this rule.
ck("a negation is repaired, not queued",
   not o.get("ok") and any("negation" in e for e in o.get("errors") or []), o.get("errors"))
o = run("head_swap", [dict(once, beat=GOOD + ' (S1) says, <d>[English] I will not stop now.</d>')])
ck("...but not one inside a spoken line", o.get("ok") and len(calls) == 1, (o.get("errors"), len(calls)))
o = run("head_swap", [dict(once, beat=contract(HEAD_DEFS, "The subject spins, looking like @her_face."))])
ck("a short detailed_description is linted against the hop",
   any("words; a 8 s hop wants" in w for w in o.get("warnings") or []), o.get("warnings"))

print("\n3. a caption that names the person is flagged")
o = run("head_swap", [json.dumps({"shot_plan": {"shots": [dict(once, beat=GOOD)]}})
                      + "\nVIDEO_DESC: A blonde woman in an orange tank top spins in a kitchen."])
ck("identity words in VIDEO_DESC warn", any("caption describes the person" in w for w in o.get("warnings") or []),
   o.get("warnings"))
o = run("head_swap", [json.dumps({"shot_plan": {"shots": [dict(once, beat=GOOD)]}})
                      + "\nVIDEO_DESC: The subject spins across a sunlit kitchen floor."])
ck("a clean caption does not", not any("caption" in w for w in o.get("warnings") or []), o.get("warnings"))

print("\n4-9. the turn and the instruct")
t = PL.build_swap_user_turn("spin", "her_face", "8 s", mode="head_swap", clip_s=3.2, clip_sound=True)
ck("a short clip is said, with where its motion ends", "The clip runs 3.2 s of this 8 s hop" in t, t)
ck("a clip with sound is said, and lines only from the brief",
   "has its own soundtrack" in t and "only with words the brief gives" in t and "<d>[English]" in t)
ck("a silent clip is said", "the clip is silent" in PL.build_swap_user_turn("x", "f", "8 s", clip_sound=False))
ck("a clip as long as the hop says nothing about it",
   "The clip runs" not in PL.build_swap_user_turn("x", "f", "8 s", clip_s=7.9))
ck("no facts, no claims", "Sound:" not in PL.build_swap_user_turn("x", "f", "8 s"))
hs = PL.build_swap_user_turn("x", "f", "8 s", mode="head_swap")
fo = PL.build_swap_user_turn("x", "f", "8 s", mode="face_only")
ck("head_swap rule carries no negation to copy", "nothing below" not in hs, hs)
ck("head_swap rule says what crosses and what stays",
   "come from <Picture 1>" in hs and "<Video 1>'s own" in hs, hs)
ck("face_only rule carries no pronoun to copy", "are theirs" not in fo, fo)
ck("face_only rule names the features that cross",
   "facial features" in fo and "come from <Picture 1>" in fo, fo)
doc = PL.swap_prompt()
ck("the example reply no longer hard-codes camera hold / framing medium",
   '"camera": "hold"' not in doc and '"framing": "medium"' not in doc)
ck("the table has no spoken-lines column", "| spoken lines |" not in doc)
ck("speech has its syntax and its condition", "<d>[English]" in doc and "only when the brief gives" in doc)
ck("two people and pronouns are covered", "a second" in doc and "never as" in doc)
ck("the word count is not written into the script", "write the count\ndown" not in doc and "never part of the script" in doc)
ck("the caption rule names what to leave out", "sex, age, build" in doc)

# --- D1q: the rules the rewrite exists to get right -----------------------
# None of the above pinned the instruct's LOAD-BEARING claim, which is why it
# could contradict MiniMax's own guide -- "the frames have no @tag" -- for as
# long as it did with a green suite. Three mutations of the prompt file went
# uncaught before these existed.
ck("the instruct no longer forbids a label for the clip's person",
   "no @tag" not in doc and "content gets a label" in doc,
   "-- the official guide: content reused from a reference video still belongs "
   "under a <Subject N>; <Video N> names the asset and does not replace it")
ck("...and it says so in the official words",
   "does not replace subject labels" in doc)
ck("the instruct assigns the clip a job",
   "movement, camera path and shot timing" in doc,
   "-- ComfyUI's H3 docs: assign each reference a job, explicit assignments "
   "work much better. Only assigning the photograph was mistake #1")
_flat = " ".join(doc.split())      # the file is hard-wrapped at 79 cols, so a
                                    # literal phrase never appears on one line
_wc = doc.split("## What comes from where")[1].split("##")[0]
ck("...and every mode is written as a subject definition",
   all(("- **%s**" % m) in _wc for m in
       ("replace_person", "head_swap", "face_only", "keep_person")),
   "a mode the instruct never defines is a mode the model cannot follow")
ck("...and the written-out forms use the official shape",
   _flat.count("`<Subject 1>` is the person in `<Video 1>`") >= 2,
   "found %d -- replace_person and keep_person are written in full, the other "
   "two abbreviate"
   % _flat.count("`<Subject 1>` is the person in `<Video 1>`"))
ck("the instruct splits appearance from movement",
   "appearance" in doc and "movement" in doc,
   "-- preserve and replace on the same property in one sentence was "
   "mistake #2 on the community list")
ck("the reply example demonstrates the shape it asks for",
   '<Subject 1> is the person in <Video 1>' in doc.split("## Reply shape")[-1],
   "an example that does not show the shape is an instruction nobody follows")
ck("the beat keeps the @tag as the secondary handle",
   "@her_face" in doc.split("## Reply shape")[-1],
   "-- 'name the identity once loses' is a MEASURED result on this node; the "
   "<Subject 1> form is the official handle, @tag is the insurance")
print()
if FAIL:
    print("SWAP INSTRUCT: %d check(s) failed: %s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("SWAP INSTRUCT: all passed.")
