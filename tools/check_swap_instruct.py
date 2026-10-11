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


GOOD = ("The subject spins across the kitchen tiles, looking like @her_face, arms flung wide as the "
        "skirt flares and settles. The face, hair and skin tone of @her_face are the subject's own, "
        "on the clip's body and clothes. Boots squeak on tile, a fridge hums, the spin slows to a "
        "standstill facing the window.")
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


print("1. keep_person never carries the face")
o = run("keep_person", [{"beat": "The subject spins across the kitchen tiles, arms flung wide, the skirt "
                                  "flaring and settling as the spin slows. Boots squeak on tile, a fridge hums "
                                  "low, and the subject comes to rest facing the window.",
                         "directives": {"tail": "settle"}}])
ck("ok", o.get("ok"), o)
ck("refs are explicitly [] (not absent -> register default)",
   json.loads(o["shot_plan"])["shots"][0].get("refs") == [], o.get("shot_plan"))
o = run("keep_person", [{"beat": "The subject spins, looking like @her_face, " + GOOD[60:], "directives": {"tail": "settle"}},
                        {"beat": "The subject spins across the kitchen tiles, arms flung wide, the skirt flaring "
                                 "and settling as the spin slows. Boots squeak on tile, a fridge hums low, and the "
                                 "subject comes to rest facing the window.", "directives": {"tail": "settle"}}])
ck("citing the identity under keep_person is repaired, not queued broken",
   o.get("ok") and len(calls) == 2 and "swaps nobody" in calls[1], (o.get("errors"), len(calls)))
o = run("keep_person", [{"beat": "The subject spins in @kitchen, arms flung wide, the skirt flaring and settling "
                                  "as the spin slows. Boots squeak on tile, a fridge hums low, and the subject "
                                  "comes to rest facing the window.", "directives": {"tail": "settle"}}],
        background="picture", background_tag="kitchen")
ck("keep_person with a picture background rides exactly that plate",
   o.get("ok") and json.loads(o["shot_plan"])["shots"][0].get("refs") == ["kitchen"], o)

print("\n2. the validator holds the instruct's rules")
once = {"beat": "The subject spins across the kitchen tiles looking like @her_face, arms flung wide as the "
                "skirt flares and settles, boots squeaking on tile while a fridge hums low, the spin slowing "
                "to a standstill facing the window in the late light.", "directives": {"tail": "settle"}}
o = run("head_swap", [once, dict(once, beat=GOOD)])
ck("one mention is an error and goes back once", o.get("ok") and len(calls) == 2 and "Name it twice" in calls[1],
   (len(calls), o.get("errors")))
o = run("head_swap", [dict(once, beat=GOOD.replace("Boots squeak", "No hat, not smiling. Boots squeak"))])
ck("a negation is linted", any("negation" in w for w in o.get("warnings") or []), o.get("warnings"))
o = run("head_swap", [dict(once, beat=GOOD + ' (S1) says, <d>[English] I will not stop now.</d>')])
ck("...but not one inside a spoken line", not any("negation" in w for w in o.get("warnings") or []), o.get("warnings"))
o = run("head_swap", [dict(once, beat="The subject spins, looking like @her_face. The face of @her_face is the subject's own.")])
ck("a short beat is linted against the hop", any("words; a 8 s hop wants" in w for w in o.get("warnings") or []),
   o.get("warnings"))

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
ck("head_swap rule carries no negation to copy", "nothing below" not in hs and "head alone" in hs, hs)
ck("face_only rule carries no pronoun to copy", "are theirs" not in fo and "the subject's" in fo, fo)
doc = PL.swap_prompt()
ck("the example reply no longer hard-codes camera hold / framing medium",
   '"camera": "hold"' not in doc and '"framing": "medium"' not in doc)
ck("the table has no spoken-lines column", "| spoken lines |" not in doc)
ck("speech has its syntax and its condition", "<d>[English]" in doc and "only when the brief gives" in doc)
ck("two people and pronouns are covered", "a second" in doc and "never as" in doc)
ck("the word count is not written into the beat", "write the count\ndown" not in doc and "never part of the beat" in doc)
ck("the caption rule names what to leave out", "sex, age, build" in doc)
print()
if FAIL:
    print("SWAP INSTRUCT: %d check(s) failed: %s" % (len(FAIL), ", ".join(FAIL)))
    sys.exit(1)
print("SWAP INSTRUCT: all passed.")
