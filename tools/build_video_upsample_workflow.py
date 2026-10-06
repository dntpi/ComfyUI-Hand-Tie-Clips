"""Write workflows/HandTieClips_Upsample_Video.json -- the shipped example for
the H3 Video Upsampler: a finished clip in, a bigger one out, one arm.

    python tools/build_video_upsample_workflow.py

Regenerate rather than hand-edit; the link table is easy to get wrong by hand.
Only core nodes and this pack, so it loads on any machine with the H3 models.
"""
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "workflows", "HandTieClips_Upsample_Video.json")

CAPTION = ("h3upscale, increase resolution and restore fine details while preserving the "
           "original content and visual style.")
UPSCALER = "minimax_h3_latent_upscaler_3d_bf16.safetensors"

NOTE = """## H3 Video Upsampler: upscale a finished clip

1. **Load Video**: pick the clip (put it in `ComfyUI/input`). Any size; 24 fps is what H3
   assumes, other rates work but the soundtrack conditions the re-draw loosely.
2. **Queue.** It saves to `video/HANDTIECLIPS/upsampled` at the clip's own fps, with its
   own soundtrack untouched.

### The node
- **megapixels**: output size, aspect kept (2 = about 1920x1080).
- **denoise**: how hard the one re-draw step works. 0.1 is the tested setting; 0 only enlarges.
- **lora**: an **h3upscale** LoRA is detected from its header and gets its guide (the clip at
  half the output size) in that same step, so the re-draw restores the clip's own detail
  instead of inventing texture. Wire the **plain** UNETLoader as `model` for it (as here):
  it was trained on the bare DiT. Other LoRAs just sit on the re-draw.
- **tile_tokens**: VRAM. Lower = more, smaller tiles, slower, less memory. 70000 is the
  recipe; try 40000 or 25000 on a smaller card.
- **audio_vae** wired = the re-draw hears the soundtrack; unwired = it hears silence.

Mouths and motion come from the clip; only fine detail is re-drawn."""


class G:
    def __init__(self):
        self.nodes, self.links, self.nid, self.lid = [], [], 0, 0

    def node(self, type_, pos, size, inputs=(), outputs=(), widgets=None, title=None):
        self.nid += 1
        n = {"id": self.nid, "type": type_, "pos": list(pos), "size": list(size), "flags": {},
             "order": self.nid - 1, "mode": 0,
             "inputs": [dict({"name": nm, "type": tp, "link": None}, **(ex[0] if ex else {}))
                        for nm, tp, *ex in inputs],
             "outputs": [{"name": nm, "type": tp, "links": [], "slot_index": i}
                         for i, (nm, tp) in enumerate(outputs)],
             "properties": {"Node name for S&R": type_}}
        if widgets is not None:
            n["widgets_values"] = widgets
        if title:
            n["title"] = title
        self.nodes.append(n)
        return n

    def link(self, a, slot, b, name):
        self.lid += 1
        i = next(k for k, x in enumerate(b["inputs"]) if x["name"] == name)
        b["inputs"][i]["link"] = self.lid
        a["outputs"][slot]["links"].append(self.lid)
        self.links.append([self.lid, a["id"], slot, b["id"], i, a["outputs"][slot]["type"]])


def build():
    g = G()
    g.node("MarkdownNote", (-900, -560), (640, 520), widgets=[NOTE])
    unet = g.node("UNETLoader", (-900, 0), (420, 82), outputs=[("MODEL", "MODEL")],
                  widgets=["Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors", "default"],
                  title="Load Diffusion Model (plain, no LoRAs)")
    clip = g.node("CLIPLoader", (-900, 130), (420, 106), outputs=[("CLIP", "CLIP")],
                  widgets=["qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "minimax", "default"])
    vae = g.node("VAELoader", (-900, 280), (420, 58), outputs=[("VAE", "VAE")],
                 widgets=["minimax_h3_video_vae_int8_convrot.safetensors"])
    avae = g.node("VAELoader", (-900, 380), (420, 58), outputs=[("VAE", "VAE")],
                  widgets=["minimax_h3_audio_vae_fp32.safetensors"], title="Load VAE (audio)")
    load = g.node("LoadVideo", (-420, -560), (320, 420), outputs=[("VIDEO", "VIDEO")],
                  widgets=["", "image"], title="clip to upscale")
    comp = g.node("GetVideoComponents", (-420, -100), (260, 140),
                  inputs=[("video", "VIDEO")],
                  outputs=[("images", "IMAGE"), ("audio", "AUDIO"), ("fps", "FLOAT"),
                           ("bit_depth", "COMBO"), ("color_space", "COMBO")])
    up = g.node("HTCVideoUpsampler", (-60, -560), (420, 520),
                inputs=[("model", "MODEL"), ("clip", "CLIP"), ("vae", "VAE"), ("images", "IMAGE"),
                        ("audio", "AUDIO", {"shape": 7}), ("audio_vae", "VAE", {"shape": 7})],
                outputs=[("images", "IMAGE"), ("audio", "AUDIO"), ("report", "STRING")],
                widgets=[CAPTION, UPSCALER, 2.0, 0.1, "None", 1.0, 70000, "euler"])
    rep = g.node("PreviewAny", (-60, 0), (420, 120), inputs=[("source", "*")], title="report")
    cv = g.node("CreateVideo", (420, -560), (260, 130),
                inputs=[("images", "IMAGE"), ("audio", "AUDIO", {"shape": 7}),
                        ("fps", "FLOAT", {"widget": {"name": "fps"}}),
                        ("bit_depth", "COMBO", {"shape": 7, "widget": {"name": "bit_depth"}}),
                        ("color_space", "COMBO", {"shape": 7, "widget": {"name": "color_space"}})],
                outputs=[("VIDEO", "VIDEO")], widgets=[24, "auto", "sRGB"])
    sv = g.node("SaveVideo", (420, -380), (420, 420),
                inputs=[("video", "VIDEO"),
                        ("filename_prefix", "STRING", {"widget": {"name": "filename_prefix"}}),
                        ("format", "COMFY_DYNAMICCOMBO_V3", {"widget": {"name": "format"}}),
                        ("format.codec", "COMFY_DYNAMICCOMBO_V3", {"widget": {"name": "format.codec"}}),
                        ("codec", "COMFY_DYNAMICCOMBO_V3", {"shape": 7, "widget": {"name": "codec"}})],
                outputs=[("video", "VIDEO")],
                widgets=["video/HANDTIECLIPS/upsampled", "auto", "auto", "auto"], title="SAVE")
    g.link(load, 0, comp, "video")
    for src, slot, name in ((unet, 0, "model"), (clip, 0, "clip"), (vae, 0, "vae"),
                            (comp, 0, "images"), (comp, 1, "audio"), (avae, 0, "audio_vae")):
        g.link(src, slot, up, name)
    g.link(up, 2, rep, "source")
    g.link(up, 0, cv, "images")
    g.link(up, 1, cv, "audio")
    g.link(comp, 2, cv, "fps")
    g.link(cv, 0, sv, "video")
    doc = {"id": "handtieclips-upsample-video", "revision": 0, "last_node_id": g.nid,
           "last_link_id": g.lid, "nodes": g.nodes, "links": g.links, "groups": [],
           "config": {}, "extra": {"ds": {"scale": 0.7, "offset": [950, 620]}}, "version": 0.4}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {OUT} ({len(g.nodes)} nodes, {len(g.links)} links)")


if __name__ == "__main__":
    build()
