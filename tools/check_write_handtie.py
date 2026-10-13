"""WRITE under hand-tie, and Edit on the editor's plan shape -- no ComfyUI, no GPU.

Loads planner.py in a stub package with a fake h3_ref_chain (DURATION_FRAMES / FPS
only), so it runs beside a render. Pins (2026-10-11):
- write_edit accepts the editor's {"shots": [...]} (plan_editor.js) as well as a
  bare list and hands back the shape it was given; it failed on every plan the
  editor had saved;
- the edit prompt says an omitted field STAYS (the code merges), and `clear`
  removes a field;
- pin_mech=hand_tie swaps the ~20-word spoken target for hand_tie_rules (one
  place, one line of line_budget words, a closed-mouth close, one mouth action
  per hop) in develop, edit and propose, and validate() adds hand_tie.line_lints.
"""
import asyncio, importlib.util, json, os, sys, types

HTC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pkg = types.ModuleType("vetpack"); pkg.__path__ = [HTC]; sys.modules["vetpack"] = pkg
fake = types.ModuleType("vetpack.h3_ref_chain")
fake.DURATION_FRAMES = {"5 s": 124, "8 s": 192, "10 s": 243}; fake.FPS = 24
sys.modules["vetpack.h3_ref_chain"] = fake
for name in ("directives", "plan", "refs", "hand_tie", "planner"):
    spec = importlib.util.spec_from_file_location("vetpack." + name, os.path.join(HTC, name + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules["vetpack." + name] = mod; spec.loader.exec_module(mod)
PL = sys.modules["vetpack.planner"]
FAIL = []


def ck(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  -- " + str(detail)[:300]))
    if not cond:
        FAIL.append(name)


ref = json.dumps({"refs": [{"tag": "x", "file": "x.png", "subject": 1, "retention": "fully_preserved",
                            "desc": "a woman"}],
                  "subjects": {"1": {"name": "X", "locked": "a woman", "context": "a street"}}})
shots = [{"id": "a", "duration": "8 s", "beat": "@x walks down the street, smiling at the camera with her lips closed. Traffic hum, footsteps, the chatter of the crowd."},
         {"id": "b", "duration": "8 s", "beat": "@x sits on a bench, smiling at the camera with her lips closed. The hum of traffic, birdsong in the trees above."}]
edit = {"edits": [{"shot_id": "b", "beat": "@x sits down slowly on a bench, smiling with her lips closed. The hum of traffic, birdsong in the trees above."}]}


def run_edit(plan_text, reply, **kw):
    seen = {}
    async def fc(messages, schema=None):
        seen["messages"] = messages
        return json.dumps(reply)
    out = asyncio.run(PL.write_edit("make b slower", plan_text, ref, complete_fn=fc, hops=2,
                                    files=["x.png"], duration="8 s", **kw))
    return out, seen

print("edit")
o, _ = run_edit(json.dumps({"shots": shots}, indent=2), edit)
ck("an editor-format plan {shots:[...]} edits", o.get("ok"), o)
ck("and comes back in the same shape", o.get("ok") and isinstance(json.loads(o["shot_plan"]), dict))
ck("shot a is untouched", o.get("ok") and json.loads(o["shot_plan"])["shots"][0] == shots[0])
o, _ = run_edit(json.dumps(shots, indent=2), edit)
ck("a bare-list plan still edits, and stays a list", o.get("ok") and isinstance(json.loads(o["shot_plan"]), list), o)
withm = [dict(shots[0], moment="@x waves."), shots[1]]
o, _ = run_edit(json.dumps(withm), {"edits": [{"shot_id": "a", "clear": ["moment"]}]})
ck("clear removes a field", o.get("ok") and "moment" not in json.loads(o["shot_plan"])[0], o)
o, _ = run_edit(json.dumps(withm), {"edits": [{"shot_id": "a", "clear": ["beat", "id"]}]})
ck("clear never removes beat or id", not o.get("ok"), o)
o, seen = run_edit(json.dumps(shots), edit)
txt = seen["messages"][-1]["content"]
ck("the edit prompt says an omitted field stays", "A field you leave out stays exactly as it is" in txt
   and "replaced by a default" not in txt)
ck("no hand-tie rules without pin_mech", "HAND-TIED" not in txt)
o, seen = run_edit(json.dumps(shots), edit, pin_mech="hand_tie")
ck("hand-tie rules ride the edit prompt under hand_tie", "HAND-TIED" in seen["messages"][-1]["content"])

print("\nthe user turn")
plain = PL.build_user_turn("a vlog", 3, ["x.png"], duration="8 s")
ht = PL.build_user_turn("a vlog", 3, ["x.png"], duration="8 s", pin_mech="hand_tie")
ck("a plain plan keeps its spoken-word target", "Speech runs about" in plain and "HAND-TIED" not in plain)
ck("a hand-tied plan swaps it for the rules", "Speech runs about" not in ht and "HAND-TIED" in ht)
ck("the 8 s budget is the node's (14 words)", "ONE line of 14 words or fewer" in ht, ht[-1500:])
ck("all five rules are there", all(s in ht for s in ("One place", "ONE line", "Never write closed or pressed lips", "One mouth action", "Open each hop")))

print("\nvalidate")
long_line = [{"id": "a", "duration": "8 s", "beat": "@x walks. (S1) says, <d>[English] " + " ".join(["word"] * 16)
              + ".</d> She smiles with her lips closed. Traffic hum, footsteps."},
             {"id": "b", "duration": "8 s", "beat": "@x holds a skewer. (S1) says, <d>[English] Here goes.</d> "
              "She takes a big bite. Crunching, traffic hum."}]
e, w = PL.validate(json.dumps(long_line), ref, hops=2, known_files=["x.png"], duration="8 s", pin_mech="hand_tie")
ck("hand-tie: a 16-word line and a line+bite hop are linted", any("speaks 16 words" in x for x in w)
   and any("bite" in x and "same hop" in x for x in w), w)
ck("hand-tie: the talk-throughout target is off", not any("talking throughout" in x for x in w), w)
e2, w2 = PL.validate(json.dumps(long_line), ref, hops=2, known_files=["x.png"], duration="8 s")
ck("plain: no hand-tie lints", not any("hand-tie" in x for x in w2), w2)

print("\nwrite_plan's one speech round speaks hand-tie")
calls = []
fixed = [dict(long_line[0], beat="@x walks. (S1) says, <d>[English] What's up, everybody, welcome back.</d> She smiles with her lips closed. Traffic hum, footsteps."),
         dict(long_line[1], beat="@x holds a skewer up and grins with her lips closed. Crunching, traffic hum, footsteps on the street.")]
async def fc(messages, schema=None):
    calls.append(messages[-1]["content"] if isinstance(messages[-1]["content"], str) else "")
    plan = long_line if len(calls) == 1 else fixed
    return "```json\n" + json.dumps(plan) + "\n```\n```json\n" + ref + "\n```"
out = asyncio.run(PL.write_plan("a vlog", 2, complete_fn=fc, files=["x.png"], duration="8 s", pin_mech="hand_tie"))
ck("the plan is accepted", out.get("ok"), out)
ck("the repair turn is the hand-tie one", len(calls) >= 2 and "break the hand-tie rules" in calls[1], calls[1:2])
print("\nFAILED:" if FAIL else "\nall ok", FAIL)
sys.exit(1 if FAIL else 0)
