"""Validate the two shipped workflows against the live node, not against a guess.

Checks the things that actually break on someone else's machine: a node type
that is not registered, a widget count that disagrees with INPUT_TYPES, a link
that points at nothing, and a plan that does not survive the real parsers.
"""
import io
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ComfyUI root is two levels above the pack. Derived, not hardcoded, so this
# survives the folder being renamed or the checkout living somewhere else.
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
pkg = types.ModuleType("h3p")
pkg.__path__ = [HERE]
sys.modules["h3p"] = pkg

from h3p import refs as R      # noqa: E402
from h3p import plan as PL     # noqa: E402

# The widget list straight from INPUT_TYPES, so this cannot drift from the node.
from h3p import h3_ref_chain as H3   # noqa: E402

CORE = {"UNETLoader", "CLIPLoader", "VAELoader", "CreateVideo", "SaveVideo",
        "Note", "MarkdownNote"}
OURS = {"HandTieClips", "HTCChainPreview"}
# Declared dependencies, not accidents: the turbo stack this node is actually
# run with. Anything outside these three sets is a pack the reader never asked
# for and must not appear in a shipped example.
DEPS = {"LTX_lora_loader": "ComfyUI-PlagueKind-Nodes",
        "H3AdaLNLoRAFix": "ComfyUI-PlagueKind-Nodes",
        "H3SLAAttention": "ComfyUI-PlagueKind-Nodes",
        "MiniMaxLowVRAMAttention": "ComfyUI-KJNodes (experimental)",
        "ModelPreviewOverrideKJ": "ComfyUI-KJNodes"}
# The MODEL wire, in order, from the loader to the chain.
MODEL_PATH = ["UNETLoader", "LTX_lora_loader", "H3AdaLNLoRAFix",
              "MiniMaxLowVRAMAttention", "H3SLAAttention",
              "ModelPreviewOverrideKJ", "HandTieClips"]
# CLIP must reach the chain THROUGH the LoRA loader, or the text half of every
# LoRA is silently dropped -- a wire that looks fine and costs you the LoRA.
CLIP_PATH = ["CLIPLoader", "LTX_lora_loader", "HandTieClips"]
FAIL = []


def ck(label, ok, detail=""):
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", label,
                          ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(label)


SOCKET_TYPES = {"MODEL", "CLIP", "VAE", "IMAGE", "AUDIO", "LATENT",
                "CONDITIONING", "VIDEO"}


def widget_names():
    """The flat widget list, in the order widgets_values is indexed by.

    A widget is any input whose type is a primitive or a combo AND which is not
    marked `forceInput` -- `continuity_state` is a STRING but arrives on a wire.
    `seed` occupies TWO entries because the frontend appends
    control_after_generate directly after it.
    """
    it = H3.HandTieClips.INPUT_TYPES()
    names = []
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in SOCKET_TYPES:
                continue
            if cfg.get("forceInput"):
                continue
            names.append(name)
            if name == "seed":
                names.append("control_after_generate")
    return names


def main():
    expect = widget_names()
    print("node declares %d widgets (control_after_generate included)\n"
          % len(expect))

    for fn in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
        p = os.path.join(HERE, "workflows", fn)
        print(fn)
        wf = json.load(io.open(p, encoding="utf-8"))
        nodes = wf["nodes"]
        byid = {n["id"]: n for n in nodes}
        types_used = {n["type"] for n in nodes}

        allowed = CORE | OURS | set(DEPS)
        ck("only core, this pack and declared deps", types_used <= allowed,
           str(sorted(types_used - allowed)) if types_used - allowed
           else "%d types" % len(types_used))
        ck("no LoadImage", not any(n["type"] == "LoadImage" for n in nodes))

        chain = next(n for n in nodes if n["type"] == "HandTieClips")

        # The wires, walked link by link. A patch node that is present but
        # bypassed round the side is the failure this catches -- it looks right
        # on the canvas and does nothing.
        def walk_wire(path, kind):
            seen = [path[0]]
            cur = next(n for n in nodes if n["type"] == path[0])
            for _ in range(len(path)):
                nxt = [l for l in wf["links"]
                       if l[1] == cur["id"] and l[5] == kind]
                if not nxt:
                    break
                cur = byid[nxt[0][3]]
                seen.append(cur["type"])
            return seen

        m = walk_wire(MODEL_PATH, "MODEL")
        ck("MODEL wire runs the whole turbo stack", m == MODEL_PATH,
           " -> ".join(m))
        c = walk_wire(CLIP_PATH, "CLIP")
        ck("CLIP reaches the chain through the LoRA loader", c == CLIP_PATH,
           " -> ".join(c))
        ins = [i["name"] for i in chain["inputs"]]
        dead = [i for i in ins
                if i.startswith("ref_image_") and i != "ref_image_size"
                or i in ("reference_video", "voice", "start_image")]
        ck("no removed sockets", not dead, str(dead))
        ck("widget count matches INPUT_TYPES",
           len(chain["widgets_values"]) == len(expect),
           "%d vs %d" % (len(chain["widgets_values"]), len(expect)))

        wv = dict(zip(expect, chain["widgets_values"]))
        ck("control_after_generate=fixed", wv["control_after_generate"] == "fixed")

        # Links: every id referenced by a node must exist, and vice versa.
        lids = {l[0] for l in wf["links"]}
        used = set()
        for n in nodes:
            for i in n.get("inputs", []):
                if i.get("link") is not None:
                    used.add(i["link"])
            for o in n.get("outputs", []):
                used.update(o.get("links") or [])
        ck("link table consistent", lids == used,
           "table=%s used=%s" % (sorted(lids), sorted(used)))
        ck("every link endpoint exists",
           all(l[1] in byid and l[3] in byid for l in wf["links"]))
        ck("all four sockets fed",
           all(chain["inputs"][k]["link"] is not None for k in range(4)))

        # The plans, through the real parsers.
        shots = PL.parse_plan(wv["shot_plan"])
        n_shots = len(shots["shots"]) if isinstance(shots, dict) else len(shots)
        ck("shot plan parses", n_shots == int(wv["chains"]),
           "%d shots, chains=%s" % (n_shots, wv["chains"]))

        rp = R.parse_ref_plan(wv["ref_plan"])
        refs = rp["refs"]
        ck("ref plan parses", True, "%d refs" % len(refs))

        # Every @tag in every beat must be declared, or the run dies at hop N.
        declared = {r["tag"] for r in refs}
        import re
        body = [s["beat"] for s in (shots["shots"] if isinstance(shots, dict)
                                    else shots)]
        tags = set()
        for b in body:
            tags |= set(re.findall(r"@([A-Za-z0-9_]+)", b or ""))
        ck("every @tag is declared", tags <= declared,
           "undeclared=%s" % sorted(tags - declared) if tags - declared
           else "tags=%s" % sorted(tags))

        # And every declared tag must be active on the hops that use it.
        bad = []
        for hop, b in enumerate(body):
            want = set(re.findall(r"@([A-Za-z0-9_]+)", b or ""))
            active = R.active_refs(refs, hop, {r["slot"] for r in refs
                                               if r["file"]})
            have = set(R.ordinals(active)) | set(R.subjects(refs))
            miss = want - have
            if miss:
                bad.append("hop %d: %s" % (hop + 1, sorted(miss)))
        ck("every tag is scheduled onto the hops that use it", not bad,
           "; ".join(bad))

        warn = R.check(rp, {r["slot"] for r in refs if r["file"]})
        ck("no register warnings", not warn, " | ".join(warn))

        # Stale named values. Both shipped workflows once carried a copy
        # inherited from the dev workflow they were cloned out of, describing a
        # 3x10 s chain with a randomizing seed and references that do not ship.
        # It is dormant while Comfy.Workflow.NamedValuesRestore stays off, which
        # is exactly why it sat there unnoticed.
        named = [n["type"] for n in nodes if "widgets_values_named" in n]
        ck("no stale widgets_values_named", not named, str(sorted(set(named))))

        # The on-canvas board, where there is one.
        cards = [n for n in nodes if n["type"] == "MarkdownNote"]
        if cards:
            ck("every card is marked and non-empty",
               all((n.get("properties") or {}).get("htc_card")
                   and (n.get("widgets_values") or [""])[0].strip()
                   for n in cards),
               "%d cards" % len(cards))
            groups = wf.get("groups") or []
            ck("a group wraps the board", len(groups) == 1,
               "%d groups" % len(groups))
            if groups:
                gx, gy, gw, gh = groups[0]["bounding"]
                outside = [n["title"] for n in cards
                           if not (gx <= n["pos"][0]
                                   and gy <= n["pos"][1]
                                   and n["pos"][0] + n["size"][0] <= gx + gw
                                   and n["pos"][1] + n["size"][1] <= gy + gh)]
                ck("the group encloses every card", not outside, str(outside))
            # A card reaching past x=0 would sit on top of the loaders.
            ck("the board stays left of the graph",
               all(n["pos"][0] + n["size"][0] <= 0 for n in cards))

        print("      %d nodes, %d links, %d KB\n"
              % (len(nodes), len(wf["links"]), os.path.getsize(p) // 1024))

    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
