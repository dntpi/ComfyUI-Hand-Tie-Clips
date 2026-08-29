"""Write the instruction board into the Starter workflow, in place.

Idempotent: cards are identified by `properties.htc_card`, so re-running
replaces the board rather than stacking a second copy beside it. Operates on the
shipped `.json` itself rather than rebuilding from a private source workflow, so
anyone with the pack can run it.

It also strips `widgets_values_named` from every node. Both shipped workflows
carried a stale copy inherited from the dev workflow they were cloned out of --
`chains: 3`, `duration: 10 s`, `control_after_generate: randomize`, and a legacy
`ref_plan` naming pictures that do not ship, 25 entries against a 28-widget node.
It is dormant while `Comfy.Workflow.NamedValuesRestore` stays off (experimental,
default false), but anyone who turns that on would load a Starter that randomizes
its seed and dies on a missing reference. The frontend re-emits the block
correctly on the next save.
"""
import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notes  # noqa: E402

WF = os.path.join(HERE, "workflows", "HandTieClips_Starter.json")
# The board is Starter-only, but the stale-named-values bug is in both shipped
# workflows, so the strip runs over both.
ALSO_STRIP = [os.path.join(HERE, "workflows", "HandTieClips_Showcase.json")]
MARKER = "htc_card"
FIRST_ID = 20


def strip_named(path):
    """Drop `widgets_values_named`, and refresh the Showcase's plain Note.

    The Showcase carries one `Note` rather than the card board; its text lives
    in `notes.SHOWCASE_NOTE` so there is still a single source for it.
    """
    wf = json.load(io.open(path, encoding="utf-8"))
    n = sum(1 for node in wf["nodes"]
            if node.pop("widgets_values_named", None) is not None)
    for node in wf["nodes"]:
        if node["type"] == "Note" and node.get("widgets_values") != [notes.SHOWCASE_NOTE]:
            node["widgets_values"] = [notes.SHOWCASE_NOTE]
            # The dependency block pushed it past what 400x700 shows without
            # scrolling.
            node["size"] = [440, 900]
            n += 1
    if n:
        bak = path + ".bak-notes"
        if not os.path.exists(bak):
            shutil.copyfile(path, bak)
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            json.dumps(wf, indent=2) + "\n")
    return n


def main():
    wf = json.load(io.open(WF, encoding="utf-8"))

    bak = WF + ".bak-notes"
    if not os.path.exists(bak):
        shutil.copyfile(WF, bak)

    # Out with the old board, and with the single legacy `Note` whose text the
    # cards now carry between them.
    before = len(wf["nodes"])
    wf["nodes"] = [n for n in wf["nodes"]
                   if MARKER not in (n.get("properties") or {})
                   and n["type"] != "Note"]
    dropped = before - len(wf["nodes"])

    order = max((n.get("order", 0) for n in wf["nodes"]), default=0)
    for i, (key, title, pos, size, colour, text) in enumerate(notes.CARDS):
        order += 1
        wf["nodes"].append({
            "id": FIRST_ID + i,
            "type": "MarkdownNote",
            "pos": list(pos),
            "size": list(size),
            "flags": {},
            "order": order,
            "mode": 0,
            "inputs": [],
            "outputs": [],
            "title": title,
            "properties": {MARKER: key},
            "widgets_values": [text],
            "color": colour[0],
            "bgcolor": colour[1],
        })

    wf["groups"] = [dict(notes.GROUP)]
    wf["extra"]["ds"] = dict(notes.DS)
    wf["last_node_id"] = max(wf["last_node_id"], FIRST_ID + len(notes.CARDS) - 1)

    stripped = 0
    for n in wf["nodes"]:
        if n.pop("widgets_values_named", None) is not None:
            stripped += 1

    io.open(WF, "w", encoding="utf-8", newline="\n").write(
        json.dumps(wf, indent=2) + "\n")

    print("%s\n  dropped %d old note/card node(s), wrote %d card(s)"
          % (os.path.basename(WF), dropped, len(notes.CARDS)))
    print("  stripped widgets_values_named from %d node(s)" % stripped)
    print("  %d nodes, %d bytes" % (len(wf["nodes"]), os.path.getsize(WF)))

    for path in ALSO_STRIP:
        print("%s\n  stripped widgets_values_named from %d node(s)"
              % (os.path.basename(path), strip_named(path)))


if __name__ == "__main__":
    main()
