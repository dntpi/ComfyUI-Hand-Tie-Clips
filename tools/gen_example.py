"""Emit the prompt pack's worked example FROM the showcase workflow.

Typing the example out by hand would give the pack a third copy of the same
plan, free to drift from the one that is actually validated. This reads the
shipped workflow, so the example and the example workflow are the same bytes.
"""
import io
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(HERE, "workflows", "HandTieClips_Showcase.json")
OUT = os.path.join(HERE, "prompt_pack", "EXAMPLE_6_HOP.md")

wf = json.load(io.open(WF, encoding="utf-8"))
# Both ids: the pack registers the pre-rename `HandTieClips` as an alias, so a
# workflow saved before 2026-08-29 is still a legal input here.
CHAIN_TYPES = ("HandTieClips", "HandTieClips")
chain = next(n for n in wf["nodes"] if n["type"] in CHAIN_TYPES)
names = [i["name"] for i in chain["inputs"] if i.get("widget")]
names.insert(names.index("seed") + 1, "control_after_generate")
wv = dict(zip(names, chain["widgets_values"]))

shot = json.loads(wv["shot_plan"])
ref = json.loads(wv["ref_plan"])

# The schedule, derived rather than described, so the table cannot lie.
rows = []
for i, s in enumerate(shot["shots"], start=1):
    active = [r["tag"] for r in ref["refs"]
              if r.get("shots") is None or i in r["shots"]]
    rows.append("| %d | %s | %s |" % (
        i, s["id"], ", ".join("@" + t for t in active) or "**none**"))

body = """# Worked example: six hops, three pictures

This is the plan inside `workflows/HandTieClips_Showcase.json`, reproduced here so
it can be shown to a model as an example of the shape and the reasoning. It is
generated from that workflow, so the two cannot drift apart.

The scene: a cook in a kitchen speaks a line, crosses the room, leaves through a
doorway into a hallway the register has no picture of, speaks again there, and
comes back.

## What each hop is for

| hop | id | references active |
|---|---|---|
%s

Three things in that table are the whole point:

- **Hop 3** carries the kitchen while she is leaving it, and hop 4 does not.
  The hallway is a space no reference describes, so the model must invent it —
  and a kitchen plate riding hop 4 would drag her back into the kitchen.
- **Hop 4** re-asserts the face. Entering an unseen space is where identity
  drift starts, and re-asserting there is cheaper than recovering on 5 and 6.
- **Hop 5 has no references at all.** Identity, wardrobe and voice ride on the
  frame pin plus `subjects.1.locked` and `.context` alone. If she is still the
  same person in the same apron with the same voice, the register works.

Note also that the dialogue lands on hops 1 and 5 — the establishing shot and
the one with no pictures — and that both use single quotes.

## shot_plan

```json
%s
```

## ref_plan

```json
%s
```
""" % ("\n".join(rows),
       json.dumps(shot, indent=2, ensure_ascii=False),
       json.dumps(ref, indent=2, ensure_ascii=False))

io.open(OUT, "w", encoding="utf-8", newline="\n").write(body)
print("wrote %s (%d bytes)" % (OUT, len(body)))
non_ascii = [c for c in body if ord(c) > 127]
print("non-ASCII in output:", sorted(set(non_ascii)) or "none")
