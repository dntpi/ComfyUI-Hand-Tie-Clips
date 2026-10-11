"""D1n: the ASK stage, driven by a scripted model.

`write_ask` takes its completion function as an argument for exactly this
reason -- the same seam `write_plan` has -- so the whole stage runs offline
with no server and no GPU.

What is actually being pinned here is the PROMPT, because that is the part no
static check can vouch for. The plumbing is three lines; whether the stage
stays a conversation depends on the system message telling the model what it
is doing, and on nothing in the code quietly converting an answer into a plan.

Run: python tools/check_ask_stage.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys

# HERE is the PACK ROOT, two levels up from tools/ -- the same anchor
# check_planner.py uses. Naming a second ROOT and taking dirname(HERE) sent it
# one level too far, to custom_nodes/, which has no __init__.py.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE
# The pack's __init__ imports ComfyUI, which imports torch. Same three lines
# check_planner.py uses, for the same reason.
COMFY = os.path.dirname(os.path.dirname(ROOT))
sys.path.insert(0, COMFY)

FAILS: list[str] = []


def ck(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def load_pack():
    """Load the pack as a package so planner.py's relative imports resolve.

    The same trick check_planner.py uses. Loading planner.py by path alone
    gives "attempted relative import with no known parent package".
    """
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(ROOT, "__init__.py"),
        submodule_search_locations=[ROOT])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = mod
    spec.loader.exec_module(mod)
    return mod


load_pack()
planner = importlib.import_module("htcpack.planner")

SEEN: list[dict] = []


def fake_complete(reply, *, calls=None):
    """A scripted model. Records what it was asked, returns a fixed answer."""

    async def complete(messages, schema=None):
        SEEN.append({"messages": messages, "schema": schema})
        if calls is not None:
            return calls.pop(0) if calls else reply
        return reply
    return complete


def flat(messages):
    return "\n".join(str(m.get("content") or "") for m in messages)


print("\nwhat the model is actually told")
SEEN.clear()
asyncio.run(planner.write_ask(
    "what is this scene about?",
    complete_fn=fake_complete("A cook plating a dish, alone, before she leaves."),
    shot_text='[{"beat": "1"}]', thread=[{"role": "user", "content": "earlier"},
                                         {"role": "assistant", "content": "noted"}]))
seen = SEEN[0]
sys_text = str(seen["messages"][0].get("content") or "")

ck("it asks for no schema at all", seen["schema"] is None,
   f"got schema={seen['schema']!r}")
ck("and the reply came back as plain prose, not a structure",
   isinstance(seen["messages"], list))
ck("the system message says this turn is a conversation",
   "CONVERSATION" in sys_text,
   "-- the whole stage rests on this sentence; validation does not exist here")
ck("...and forbids producing a plan unasked",
   "Do not write a shot plan" in sys_text)
ck("...and forbids JSON, tags and field names",
   "JSON" in sys_text and "@tags" in sys_text)
ck("...because a chatty model asked a question tends to helpfully emit one",
   "Write beats" in sys_text,
   "-- the panel cannot tell a plan in prose from an answer, so the silence "
   "where a card should be would be invisible")
ck("it is told to be brief, with a number",
   "Two to four sentences" in sys_text)
ck("it is told to admit what it cannot know",
   "not knowable" in sys_text)
ck("the question itself arrives as the last user turn",
   "what is this scene about?" in str(seen["messages"][-1].get("content") or ""))
ck("and the plan is resent with that turn, not left in the thread",
   "current_shot_plan" in flat(seen["messages"]),
   "-- the plan can be accepted or reverted between two turns of talking")
ck("the earlier turns are still there",
   "earlier" in flat(seen["messages"]) and "noted" in flat(seen["messages"]))

print("\nwhat it returns")
out = asyncio.run(planner.write_ask(
    "why is hop 3 like that?", complete_fn=fake_complete("It carries the pin.")))
ck("ok", out.get("ok") is True)
ck("prose under `text`", "It carries the pin." in str(out.get("text") or ""))
ck("and NO plan key anywhere",
   "shot_plan" not in out and "ref_plan" not in out and "proposal" not in out,
   f"keys={sorted(out)}")
ck("no repair-loop bookkeeping",
   out.get("attempts") == 1 and not out.get("errors"))

print("\nedge cases")
empty = asyncio.run(planner.write_ask("   ", complete_fn=fake_complete("x")))
ck("an empty question is refused, not sent",
   empty.get("ok") is False and "nothing to ask" in str(empty.get("error")))

blank = asyncio.run(planner.write_ask("hi", complete_fn=fake_complete("   ")))
ck("an empty answer says so instead of rendering a blank turn",
   blank.get("ok") is False and blank.get("warnings"),
   "-- the person asked a question; 'nothing' does not say whether to retry")

long_reply = ("word " * 900).strip()
cut = asyncio.run(planner.write_ask(
    "explain everything", complete_fn=fake_complete(long_reply)))
ck("a runaway answer is cut, not scrolled for four minutes",
   len(cut.get("text") or "") < len(long_reply)
   and any("cut at" in w for w in cut.get("warnings") or []),
   "-- a length instruction the model ignores is only a suggestion")

under = asyncio.run(planner.write_ask("hi", complete_fn=fake_complete("short")))
ck("a normal answer is left alone and warns about nothing",
   under["text"] == "short" and not under.get("warnings"))

print("\nwhat it must NOT do")
reached = {"n": 0}


async def counting_complete(messages, schema=None):
    reached["n"] += 1
    return "one answer"


asyncio.run(planner.write_ask("q", complete_fn=counting_complete))
ck("exactly one call -- no retry, no repair",
   reached["n"] == 1, f"made {reached['n']} calls")

print()
if FAILS:
    print("ASK STAGE: %d check(s) failed." % len(FAILS))
    sys.exit(1)
print("ASK STAGE: all passed.")