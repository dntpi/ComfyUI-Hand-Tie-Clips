"""Wire the turbo stack onto both shipped workflows, matching the dev graph.

    UNETLoader ->  LoRA Loader Stack  ->  H3 AdaLN LoRA Fix
               ->  MiniMax H3 Low VRAM Attention
               ->  H3 SLA Attention
               ->  Model Preview Override (KJ)  ->  Hand Tie Clips

and, off the same LoRA loader, **CLIP goes to the chain from the loader, not
from the encoder** -- that is what makes the text half of every LoRA land.

None of these five nodes belong to this pack. They are here because this is the
graph the node is actually run with: `steps` is 7, which only works with a turbo
LoRA, and the AdaLN fix exists because the LoRA needs it. Shipping the examples
without them ships a graph nobody uses. Every dependency is named on the START
HERE card, in the Showcase note and in the README.

Idempotent: inserted nodes carry `properties.htc_speed`, so a re-run rewires
rather than stacking a second copy. Widget values are POSITIONAL (CLAUDE.md) and
are copied from the proven dev workflow rather than reconstructed.
"""
import io
import json
import os
import shutil

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = ["HandTieClips_Starter.json", "HandTieClips_Showcase.json"]
MARKER = "htc_speed"

TURBO_LORA = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
LORA_STACK = json.dumps([{"on": True, "lora": TURBO_LORA,
                          "str": 1, "v": 1, "a": 1, "t": 1}],
                        separators=(",", ":"))

# id, type, title, pos, size, widgets
#
# Widget order is INPUT_TYPES order with sockets skipped. The trailing "" on the
# LoRA loader and the preview override is carried over from the dev workflow --
# a value past the last widget is ignored, and dropping one that turns out to
# belong to a widget would silently shift every value after it.
STACK = [
    ("lora", "LTX_lora_loader", "LoRA Loader Stack (turbo)",
     [520, -40], [420, 240], ["minimax", LORA_STACK, ""]),
    ("adaln", "H3AdaLNLoRAFix", "H3 AdaLN LoRA Fix",
     [520, 240], [340, 150], ["port"]),
    ("lowvram", "MiniMaxLowVRAMAttention", "MiniMax H3 Low VRAM Attention",
     [520, 430], [330, 58], [4]),
    # sparsity_ratio, block_size, min_seq_len, dense_last_steps, protect_audio,
    # enabled, dense_steps, dense_backend, disable_fp16_accum, stabilize_motion,
    # reference_protection -- the last one post-dates the dev workflow's saved
    # values, so it is written out explicitly here.
    ("sla", "H3SLAAttention", "H3 SLA Attention",
     [520, 530], [340, 322],
     [0.9, "64", 8192, 0, True, True, "0", "comfy_kitchen", True, True, "Light"]),
    ("preview", "ModelPreviewOverrideKJ", "Model Preview Override",
     [520, 900], [360, 480], [512, 80, True, 100, 8, "taeh3.safetensors", ""]),
]
FIRST_ID = 30

MODEL_PATH = ["UNETLoader"] + [t for _, t, _, _, _, _ in STACK] + ["HandTieClips"]


def main():
    for fn in WORKFLOWS:
        p = os.path.join(HERE, "workflows", fn)
        bak = p + ".bak-speed"
        if not os.path.exists(bak):
            shutil.copyfile(p, bak)
        wf = json.load(io.open(p, encoding="utf-8"))

        # Drop any previous stack and every link that touched it.
        old = {n["id"] for n in wf["nodes"]
               if MARKER in (n.get("properties") or {})}
        wf["nodes"] = [n for n in wf["nodes"] if n["id"] not in old]
        wf["links"] = [l for l in wf["links"]
                       if l[1] not in old and l[3] not in old]

        unet = next(n for n in wf["nodes"] if n["type"] == "UNETLoader")
        clipl = next(n for n in wf["nodes"] if n["type"] == "CLIPLoader")
        chain = next(n for n in wf["nodes"] if n["type"] == "HandTieClips")
        lid = max([l[0] for l in wf["links"]] or [0])

        # Removing the stack also removed the wires that fed it, so rebuild the
        # plain loader -> chain shape first. That makes this reentrant from
        # either state: a fresh workflow or one already wired.
        wf["links"] = [l for l in wf["links"]
                       if not (l[3] == chain["id"] and l[4] in (0, 1))]
        lid += 1
        model_link = [lid, unet["id"], 0, chain["id"], 0, "MODEL"]
        lid += 1
        clip_link = [lid, clipl["id"], 0, chain["id"], 1, "CLIP"]
        wf["links"] += [model_link, clip_link]

        ids = {}
        for i, (key, ntype, title, pos, size, widgets) in enumerate(STACK):
            ids[key] = FIRST_ID + i
            wf["nodes"].append({
                "id": FIRST_ID + i, "type": ntype,
                "pos": list(pos), "size": list(size),
                "flags": {}, "order": 0, "mode": 0,
                "inputs": [], "outputs": [],
                "title": title,
                "properties": {MARKER: key, "Node name for S&R": ntype},
                "widgets_values": list(widgets),
            })
        byid = {n["id"]: n for n in wf["nodes"]}

        def link(src, src_slot, dst, dst_slot, kind):
            """Append one link and record it on both endpoints."""
            nonlocal lid
            lid += 1
            wf["links"].append([lid, src, src_slot, dst, dst_slot, kind])
            return lid

        # MODEL, straight down the column.
        model_link[3], model_link[4] = ids["lora"], 0
        chain_model = model_link[0]
        prev = ids["lora"]
        for key in ("adaln", "lowvram", "sla", "preview"):
            chain_model = link(prev, 0, ids[key], 0, "MODEL")
            prev = ids[key]
        chain_model = link(prev, 0, chain["id"], 0, "MODEL")

        # CLIP through the LoRA loader, so the text half of every LoRA lands.
        clip_link[3], clip_link[4] = ids["lora"], 1
        chain_clip = link(ids["lora"], 1, chain["id"], 1, "CLIP")

        # Sockets and slots, now that every link id exists.
        byid[ids["lora"]]["inputs"] = [
            {"name": "model", "type": "MODEL", "link": model_link[0]},
            {"name": "clip", "type": "CLIP", "link": clip_link[0]},
        ]
        for key in ("adaln", "lowvram", "sla", "preview"):
            byid[ids[key]]["inputs"] = [
                {"name": "model", "type": "MODEL", "link": None}]
        byid[ids["preview"]]["inputs"].append(
            {"name": "vae", "type": "VAE", "link": None})

        outs = {"lora": [("model", "MODEL"), ("clip", "CLIP")],
                "adaln": [("MODEL", "MODEL")],
                "lowvram": [("model", "MODEL")],
                "sla": [("MODEL", "MODEL")],
                "preview": [("MODEL", "MODEL")]}
        for key, spec in outs.items():
            byid[ids[key]]["outputs"] = [
                {"name": nm, "type": ty, "links": []} for nm, ty in spec]

        # Walk the finished link table back onto the endpoints.
        for n in wf["nodes"]:
            for o in n.get("outputs", []):
                o["links"] = []
        for l in wf["links"]:
            src, sslot, dst, dslot = l[1], l[2], l[3], l[4]
            byid[src]["outputs"][sslot]["links"].append(l[0])
            byid[dst]["inputs"][dslot]["link"] = l[0]

        wf["last_link_id"] = lid
        wf["last_node_id"] = max(wf["last_node_id"], FIRST_ID + len(STACK) - 1)

        io.open(p, "w", encoding="utf-8", newline="\n").write(
            json.dumps(wf, indent=2) + "\n")
        print("%s\n  MODEL: %s\n  CLIP : CLIPLoader -> LoRA Loader Stack -> "
              "Hand Tie Clips (link %d)"
              % (fn, " -> ".join(MODEL_PATH), chain_clip))


if __name__ == "__main__":
    main()
