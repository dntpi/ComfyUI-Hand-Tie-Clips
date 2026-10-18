r"""Offline checks for the H3 Video Upsampler -- no GPU, no H3 weights.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_video_upsampler.py

* the helpers: valid H3 lengths against core's temporal_shape, the 32 px grid,
  padding, the soundtrack fitted to the clip's audio steps;
* the tile budget: the default is the recipe's (so check_tiled_upsampler's
  MMH3 identity still holds), and a lower budget cuts more, smaller tiles that
  each fit it;
* run(), with the VAE, the text encoder and the sampler faked: frames out ==
  frames in, the soundtrack handed back untouched, the budget and denoise
  reach the upsampler, a guide LoRA builds a guide and a plain one does not;
* the shipped workflow: every node type registered, our node's widget count
  matching INPUT_TYPES, every link consistent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)
FAIL = []
WORKFLOW = os.path.join(HERE, "workflows", "HandTieClips_Upsample_Video.json")


def ck(name, cond, detail=""):
    print("  %-4s %-64s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"), submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


def helper_checks(V):
    print("helpers")
    from comfy_extras.nodes_minimax_h3 import temporal_shape
    bad = [n for n in range(1, 400) if temporal_shape(V.valid_length(n))[0] != V.valid_length(n)
           or V.valid_length(n) < n or V.valid_length(n) - n >= 17]
    ck("valid_length: a real H3 length, >= n, < n + 17 (n = 1..399)", not bad, str(bad[:5]))
    ck("grid_size rounds to 32", V.grid_size(1080, 1920) == (1088, 1920)
       and V.grid_size(768, 1344) == (768, 1344))
    x = torch.arange(5.0).view(5, 1, 1, 1).expand(5, 2, 2, 3)
    p = V.pad_frames(x, 9)
    ck("pad repeats the last frame", p.shape[0] == 9 and torch.equal(p[5:], x[-1:].expand(4, 2, 2, 3)))
    z = torch.ones(1, 32, 2, 10)
    ck("soundtrack fitted: trimmed and zero-padded",
       V.fit_audio_latent(z, 6).shape[-1] == 6 and V.fit_audio_latent(z, 14).shape[-1] == 14
       and float(V.fit_audio_latent(z, 14)[..., 10:].abs().sum()) == 0.0)


def budget_checks(tu):
    print("tile budget")
    import inspect
    for fn in (tu.tile_grid, tu.redraw_chunk, tu.upsample):
        d = inspect.signature(fn).parameters["budget"].default
        ck(f"{fn.__name__}: budget defaults to the recipe's {tu.TOKEN_BUDGET}", d == tu.TOKEN_BUDGET)
    h, w, t = 2272 // 16, 1824 // 16, 30
    _, g_def = tu.tile_grid(h, w, t)
    _, g_low = tu.tile_grid(h, w, t, budget=25000)
    n_def, n_low = g_def[0] * g_def[1], g_low[0] * g_low[1]
    ck("lower budget -> more tiles", n_low > n_def, f"{n_def} -> {n_low}")
    ck("every low-budget tile fits it", tu.seq_len(g_low[2], g_low[3], t) <= 25000,
       str(tu.seq_len(g_low[2], g_low[3], t)))
    try:
        tu.tile_grid(h, w, t, budget=2000)
        ck("an impossible budget fails readably", False)
    except RuntimeError as e:
        ck("an impossible budget fails readably", "tile budget" in str(e))


def run_checks(V, tu, ug):
    print("run(), faked")
    import comfy.nested_tensor as nt
    import comfy_extras.nodes_minimax_h3 as core
    import nodes
    seen = {}

    class FakeVAE:
        audio_sample_rate = 44100

        def encode(self, x):
            if x.ndim == 3:        # audio [B, L, C]
                return torch.zeros(1, 32, 2, 200)
            k = -(-int(x.shape[0]) // 17) * 5 - 3
            return torch.zeros(1, 24, k, x.shape[1] // 16, x.shape[2] // 16)

        def decode(self, v):
            return torch.zeros(1, tu.frames_in(v.shape[2]), v.shape[3] * 16, v.shape[4] * 16, 3)

    def fake_upsample(model, cond, latent, sampler, name, mp, denoise, guide=None, budget=None, **kw):
        seen.update(cond=cond, mp=mp, denoise=denoise, guide=guide, budget=budget,
                    latent=latent, **kw)
        v = latent["samples"].tensors[0]
        return {"samples": nt.NestedTensor((torch.zeros(1, 24, v.shape[2], v.shape[3] * 2,
                                                        v.shape[4] * 2),
                                            latent["samples"].tensors[1]))}

    class FakeR2V:
        @staticmethod
        def execute(**kw):
            seen["r2v"] = kw
            return ([["CAPTION_COND", {}]], None)

    saved = (tu.upsample, tu._free, core.MiniMaxH3ReferenceToVideo, ug.lora_factor,
             nodes.LoraLoaderModelOnly, ug.install)
    tu.upsample, tu._free = fake_upsample, (lambda m: None)
    core.MiniMaxH3ReferenceToVideo = FakeR2V
    ug.install = lambda: None

    class FakeLoader:
        def load_lora_model_only(self, model, name, s):
            seen["lora"] = (name, s)
            return ("PATCHED",)
    nodes.LoraLoaderModelOnly = FakeLoader
    import folder_paths
    saved_fp = folder_paths.get_full_path_or_raise
    folder_paths.get_full_path_or_raise = lambda kind, name: name
    try:
        node = V.HTCVideoUpsampler()
        imgs = torch.rand(100, 360, 640, 3)
        audio = {"waveform": torch.rand(1, 2, 44100 * 4), "sample_rate": 44100}
        ug.lora_factor = lambda path: 0
        out_i, out_a, rep = node.run("MODEL", "CLIP", FakeVAE(), imgs, "a prompt", "x.safetensors",
                                     2.0, 0.1, "None", 1.0, 40000, "euler", audio=audio,
                                     audio_vae=FakeVAE())
        ck("frames out == frames in (padding trimmed)", out_i.shape[0] == 100, str(tuple(out_i.shape)))
        ck("canvas fitted to 32 px (640x360 -> 640x352)",
           seen["r2v"]["width"] == 640 and seen["r2v"]["height"] == 352)
        ck("length is the valid H3 length (100 -> 107)", seen["r2v"]["length"] == 107)
        ck("soundtrack handed back untouched", out_a is audio)
        ck("budget and denoise reach the upsampler", seen["budget"] == 40000 and seen["denoise"] == 0.1)
        ck("no LoRA -> no guide", seen["guide"] is None)
        ck("the chain's re-draw: 2 steps on the denoise schedule, 192 px overlap, no detail merge",
           seen.get("steps") == 2 and seen.get("start_sigma") == 0.0
           and seen.get("overlap_px") == 192 and seen.get("detail_sigma") == 0.0,
           str({k: seen.get(k) for k in ("steps", "start_sigma", "overlap_px", "detail_sigma")}))
        chain_src = open(os.path.join(HERE, "tiled_upsampler.py"), encoding="utf-8").read()
        chain_src = chain_src.split("def upsample_chain(", 1)[1]
        ck("upsample_chain still calls it with those values (node follows the chain)",
           "_chain_steps = 2 if float(denoise) > 0.0 else STEPS" in chain_src
           and "steps=_chain_steps, start_sigma=0.0," in chain_src
           and "overlap_px=192, detail_sigma=0.0)" in chain_src)
        opt = V.HTCVideoUpsampler.INPUT_TYPES().get("optional") or {}
        ck("no steps / start_sigma / tile_overlap widgets",
           not ({"steps", "start_sigma", "tile_overlap"} & set(opt)), str(sorted(opt)))
        ck("audio latent sized to the clip's audio steps",
           seen["latent"]["samples"].tensors[1].shape[-1]
           == core.temporal_shape(107)[2])
        ug.lora_factor = lambda path: 2
        node.run("MODEL", "CLIP", FakeVAE(), imgs, "a prompt", "x.safetensors", 2.0, 0.1,
                 "h3upscale.safetensors", 1.0, 70000, "euler")
        ck("guide LoRA -> guide built (factor 2), LoRA applied",
           seen["guide"] is not None and seen["guide"].factor == 2
           and seen["lora"] == ("h3upscale.safetensors", 1.0))
        ug.lora_factor = lambda path: 0
        node.run("MODEL", "CLIP", FakeVAE(), imgs, "a prompt", "x.safetensors", 2.0, 0.1,
                 "realism.safetensors", 0.8, 70000, "euler")
        ck("plain LoRA -> applied, no guide", seen["guide"] is None and seen["lora"][1] == 0.8)
        _, a2, _ = node.run("MODEL", "CLIP", FakeVAE(), imgs, "p", "x.safetensors", 2.0, 0.1,
                            "None", 1.0, 70000, "euler")
        ck("no audio in -> silent audio out, the clip's length",
           float(a2["waveform"].abs().sum()) == 0.0
           and abs(a2["waveform"].shape[-1] - round(100 / 24 * a2["sample_rate"])) <= 1)
    finally:
        (tu.upsample, tu._free, core.MiniMaxH3ReferenceToVideo, ug.lora_factor,
         nodes.LoraLoaderModelOnly, ug.install) = saved
        folder_paths.get_full_path_or_raise = saved_fp


def streamed_decode_checks(V, tu):
    print("streamed decode")

    def fake_decode(z):
        # Causal-ish: each token's frames see 3 tokens back and 1 ahead.
        t = int(z.shape[2])
        frames = []
        for k in range(t):
            v = float(z[0, 0, max(0, k - 3):k + 2, 0, 0].sum())
            frames += [v] * tu.FRAME_PER_TOKEN[k % 5]
        return torch.tensor(frames).view(1, -1, 1, 1, 1).expand(1, -1, 2, 3, 3).clone()

    for tv, n in ((80, tu.frames_in(80)), (97, tu.frames_in(97) - 9), (31, 100)):
        vid = torch.randn(1, 4, tv, 1, 1)
        full = fake_decode(vid).reshape(-1, 2, 3, 3)[:n]
        got = V.decode_streamed(fake_decode, vid, n)
        ck(f"{tv} tokens, {n} frames: streamed == full decode",
           got.shape == full.shape and torch.equal(got, full.float()),
           f"{tuple(got.shape)} vs {tuple(full.shape)}")
    src = open(os.path.join(HERE, "video_upsampler.py"), encoding="utf-8").read()
    ck("run() decodes streamed, as upsample_chain",
       "decode_streamed(vae.decode, out[\"samples\"].tensors[0], n)" in src)


def workflow_checks(pack, V):
    print("shipped workflow")
    w = json.load(open(WORKFLOW, encoding="utf-8"))
    core_types = {"UNETLoader", "CLIPLoader", "VAELoader", "LoadVideo", "GetVideoComponents",
                  "CreateVideo", "SaveVideo", "PreviewAny", "MarkdownNote"}
    types_ = {n["type"] for n in w["nodes"]}
    ck("only core nodes and this pack", types_ <= core_types | set(pack.NODE_CLASS_MAPPINGS),
       str(sorted(types_ - core_types - set(pack.NODE_CLASS_MAPPINGS))))
    node = next(n for n in w["nodes"] if n["type"] == "HTCVideoUpsampler")
    req = V.HTCVideoUpsampler.INPUT_TYPES()["required"]
    widgets = [k for k, v in req.items() if not (isinstance(v[0], str) and v[0] in
                                                 ("MODEL", "CLIP", "VAE", "IMAGE", "AUDIO"))]
    ck("widget count matches INPUT_TYPES", len(node["widgets_values"]) == len(widgets),
       f"{len(node['widgets_values'])} vs {len(widgets)}")
    ck("default lora None, denoise 0.1, recipe budget",
       node["widgets_values"][widgets.index("lora")] == "None"
       and node["widgets_values"][widgets.index("denoise")] == 0.1
       and node["widgets_values"][widgets.index("tile_tokens")] == 70000)
    by = {n["id"]: n for n in w["nodes"]}
    bad = [l[0] for l in w["links"]
           if l[0] not in by[l[1]]["outputs"][l[2]]["links"] or by[l[3]]["inputs"][l[4]]["link"] != l[0]]
    ck("every link consistent", not bad, str(bad))
    model_src = next(l for l in w["links"] if by[l[3]]["type"] == "HTCVideoUpsampler"
                     and by[l[3]]["inputs"][l[4]]["name"] == "model")
    ck("model comes straight from the plain UNETLoader", by[model_src[1]]["type"] == "UNETLoader")


def chain_budget_checks(H3, tu):
    print("chain upsample tile budget")
    import inspect
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}
    spec = opt.get("upsample_tile_tokens")
    ck("upsample_tile_tokens: INT, default = recipe, min 0",
       spec is not None and spec[0] == "INT" and spec[1].get("default") == tu.TOKEN_BUDGET
       and spec[1].get("min") == 0)
    opt_keys = list(opt)
    ck("upsample_dit sits immediately after upsample_tile_tokens",
       "upsample_tile_tokens" in opt_keys and "upsample_dit" in opt_keys
       and opt_keys.index("upsample_dit") == opt_keys.index("upsample_tile_tokens") + 1)
    ck("run() defaults it to the recipe",
       inspect.signature(H3.HandTieClips.run).parameters["upsample_tile_tokens"].default
       == tu.TOKEN_BUDGET)
    ck("upsample_chain passes budget through",
       inspect.signature(tu.upsample_chain).parameters["budget"].default == tu.TOKEN_BUDGET)
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    ck("0 / a stale split_finish_aug value -> the recipe budget",
       "if int(upsample_tile_tokens) >= UPSAMPLE_MIN_TILE_TOKENS" in src
       and "else _tu.TOKEN_BUDGET)" in src and int(0.999) < H3.UPSAMPLE_MIN_TILE_TOKENS
       and int(0.85) < H3.UPSAMPLE_MIN_TILE_TOKENS)
    ck("the floor is one widget step: no real budget is overridden",
       H3.UPSAMPLE_MIN_TILE_TOKENS == opt["upsample_tile_tokens"][1]["step"])
    ck("the chain hands it to upsample_chain", "budget=_up_budget)" in src)
    keyblock = src.split("hop_payload = {", 1)[1].split("hop_key = _store.hop_key(", 1)[0]
    ck("not in any hop key (delivery only)", "upsample_tile_tokens" not in keyblock)
    for f in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
        w = json.load(open(os.path.join(HERE, "workflows", f), encoding="utf-8"))
        n = next(x for x in w["nodes"] if x["type"] == "HandTieClips")
        ck(f"{f}: upsample_tile_tokens [95] is the recipe budget",
           len(n["widgets_values"]) > 95 and n["widgets_values"][95] == tu.TOKEN_BUDGET)


def main():
    pack = load_pack()
    V = sys.modules["htcpack.video_upsampler"]
    tu = sys.modules["htcpack.tiled_upsampler"]
    ug = sys.modules["htcpack.upscale_guide"]
    helper_checks(V)
    budget_checks(tu)
    run_checks(V, tu, ug)
    streamed_decode_checks(V, tu)
    workflow_checks(pack, V)
    chain_budget_checks(sys.modules["htcpack.h3_ref_chain"], tu)
    print("\nall video upsampler checks passed" if not FAIL else f"\n{len(FAIL)} FAILED: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
