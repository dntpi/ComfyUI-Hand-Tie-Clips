r"""Offline checks for the guided upsample re-draw -- no GPU, no H3 weights.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_upscale_guide.py

With an h3upscale LoRA on `upsample_lora`, the upsampler's one re-draw step
also carries the LoRA's guide. What can go wrong without a GPU, and is
checked here:

* the LoRA header is read right, and guide geometry this pack cannot pack is
  refused rather than packed wrong;
* every guided tile, over a sweep of canvases, sizes and lengths, lands on
  the guide's grid and fits the token budget with the guide's rows counted;
* a tile's guide window is cut from the film's guide at (r0, c0) / factor and
  never resized, while a plain keyframe is still resized and cropped exactly
  as before;
* core's PackedLayout, patched, builds a chunk-start pin and the guide side by
  side, and the guide's rows sit where they sit with the guide alone; when the
  fork's packing.py is on disk (HTC_FORK_PACKING or the scratchpad copy) the
  guide-only layout is compared with the trainer's row for row;
* `upsample()` hands every tile of every chunk the caption conditioning, the
  pin (chunks after the first) and a guide of the tile's shape, sizes the
  output on the guide's grid, and never decodes for a guide when there is no
  re-draw.

The recipe path is tools/check_tiled_upsampler.py's job (identical to MMH3).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import torch
import torch.nn.functional as F

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []
FORK_PACKING = [
    os.environ.get("HTC_FORK_PACKING", ""),
    r"C:\Users\User\AppData\Local\Temp\claude\D--ComfyUI-custom-nodes-ComfyUI-Hand-Tie-Clips"
    r"\f5e24d67-7518-4f24-94b9-4a8f3d2e046b\scratchpad\fork\packing.py",
]
V2_LORA = os.path.join(COMFY, "models", "loras", "h3upscale", "v2_x2",
                       "h3upscale_v2_x2.safetensors")


def ck(name, cond, detail=""):
    print("  %-4s %-62s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


GOOD_META = {
    "reference_downscale_factor": "2",
    "minimax_h3_guide_spatial_version": "target_crop_v2",
    "minimax_h3_guide_position_version": "target_grid_stride_v1",
    "guide_latent_only": "true",
    "minimax_h3_reference_rope": '{"guide_rope_layout": "overlap"}',
}


def header_checks(ug):
    print("LoRA header")
    ck("h3upscale v2 header -> factor 2", ug.factor_from_meta(GOOD_META) == 2)
    ck("no header -> not a guide LoRA", ug.factor_from_meta({}) == 0)
    ck("factor 1 -> not a guide LoRA",
       ug.factor_from_meta(dict(GOOD_META, reference_downscale_factor="1")) == 0)
    for key, bad in (("minimax_h3_guide_spatial_version", "target_crop_v1"),
                     ("minimax_h3_guide_position_version", "other"),
                     ("guide_latent_only", "false"),
                     ("minimax_h3_reference_rope", '{"guide_rope_layout": "sidecar"}')):
        try:
            ug.factor_from_meta(dict(GOOD_META, **{key: bad}))
            ck(f"refuses {key}={bad}", False)
        except ValueError:
            ck(f"refuses {key}={bad}", True)
    if os.path.isfile(V2_LORA):
        ck("the real v2 file reads factor 2", ug.lora_factor(V2_LORA) == 2)
    else:
        print("  (v2 LoRA not on disk, file check skipped)")


def grid_checks(tu):
    print("guided tile grids")
    bad, n = [], 0
    for cw, ch in ((1344, 768), (960, 544), (704, 704), (576, 576), (768, 1344), (1280, 736)):
        for mp in (1.0, 1.5, 2.0, 3.0):
            w, h = tu.target_size(cw, ch, mp, align=64)
            for t in (2, 7, 22, 30, 57):
                n += 1
                if w % 64 or h % 64:
                    bad.append(f"{w}x{h} off 64")
                    continue
                try:
                    tiles, got = tu.tile_grid(h // 16, w // 16, t, 2)
                except RuntimeError as e:
                    bad.append(f"{w}x{h} t{t}: {e}")
                    continue
                cover = torch.zeros(h // 16, w // 16, dtype=torch.bool)
                for (r0, c0, rows, cols, *_rest) in tiles:
                    if any(x % 4 for x in (r0, c0, rows, cols)):
                        bad.append(f"{w}x{h} t{t} tile {(r0, c0, rows, cols)}")
                    cover[r0:r0 + rows, c0:c0 + cols] = True
                if not bool(cover.all()):
                    bad.append(f"{w}x{h} t{t} uncovered")
                if tu.guided_seq_len(got[2], got[3], t, 2) > tu.TOKEN_BUDGET:
                    bad.append(f"{w}x{h} t{t} over budget")
    ck(f"{n} grids on the 64 px guide grid, covered, in budget", not bad,
       "; ".join(bad[:3]))
    w, h = tu.target_size(1344, 768, 2.0)
    ck("factor 1 solves exactly as the recipe",
       tu.solve_grid(w, h, 30) == tu.solve_grid(w, h, 30, factor=1))


def tile_cond_checks(tu):
    print("per-tile guide window")
    h, w, t = 64, 112, 7
    guide = torch.randn(1, 24, t, h // 2, w // 2)
    plain = torch.randn(1, 24, 1, 32, 56)
    cond = [["emb", {"minimax_keyframes": [
        {"latent": plain, "resolved_frame_index": 0},
        {"latent": guide, "resolved_frame_index": 0, tu.STRIDE_KEY: 2}]}]]
    r0, c0, rows, cols = 16, 40, 48, 72
    kfs = tu.cond_for_tile(cond, h, w, r0, c0, rows, cols)[0][1]["minimax_keyframes"]
    g = kfs[1]["latent"]
    ck("guide window shape is the tile / 2", tuple(g.shape) == (1, 24, t, rows // 2, cols // 2),
       str(tuple(g.shape)))
    ck("guide window is cut, not resized",
       torch.equal(g, guide[:, :, :, r0 // 2:(r0 + rows) // 2, c0 // 2:(c0 + cols) // 2]))
    want = tu._resize_planes(plain, h, w, torch.float32)[:, :, :, r0:r0 + rows, c0:c0 + cols]
    ck("plain keyframe resized + cropped as before", torch.equal(kfs[0]["latent"], want))
    ck("stride key kept on the guide", kfs[1].get(tu.STRIDE_KEY) == 2)


def layout_checks(ug):
    print("packed layout")
    import comfy.ldm.minimax.model as mm
    ug.install()
    ck("PackedLayout patched", getattr(mm.PackedLayout.__init__, "_htc_strided", False))
    for (text, lt, lh, lw, at) in ((40, 30, 48, 80, 170), (40, 7, 32, 32, 40)):
        pin = {"latent": torch.zeros(1, 24, 1, lh, lw), "resolved_frame_index": 0}
        g = {"latent": torch.zeros(1, 24, lt, lh // 2, lw // 2), "resolved_frame_index": 0,
             ug.STRIDE_KEY: 2}
        both = mm.PackedLayout(text, lt, lh, lw, at, keyframes=[pin, g])
        only_pin = mm.PackedLayout(text, lt, lh, lw, at, keyframes=[pin])
        only_g = mm.PackedLayout(text, lt, lh, lw, at, keyframes=[g])
        g_rows = lt * (lh // 4) * (lw // 4)
        ck(f"{lw * 16}x{lh * 16} T{lt}: pin + guide rows add up",
           both.seq_len == only_pin.seq_len + g_rows, f"{both.seq_len}")
        cond = [s for s in both.segments if s[2] == "cond"]
        gseg = cond[1]
        g_alone = [s for s in only_g.segments if s[2] == "cond"][0]
        ck("  guide rows sit where they sit alone",
           torch.equal(both.position_ids[gseg[0]:gseg[1]],
                       only_g.position_ids[g_alone[0]:g_alone[1]]))
        ck("  only target video rows update",
           int(both.img_update.sum()) == lt * (lh // 2) * (lw // 2))
    fork = next((p for p in FORK_PACKING if p and os.path.isfile(p)), None)
    if fork is None:
        print("  (fork packing.py not on disk, trainer comparison skipped)")
        return
    sys.path.insert(0, os.path.dirname(fork))      # its `toolkit` package
    try:
        fp = load(fork, "fork_packing")
    except ImportError as e:
        print(f"  (fork packing.py found but not importable: {e}; comparison skipped)")
        return
    for (text, lt, lh, lw, at) in ((40, 30, 48, 80, 170), (40, 7, 32, 32, 40)):
        ref = fp.build_packed_sequence(torch.ones(text, dtype=torch.long), lt, lh, lw, at,
                                       ref_blocks=((lt, lh // 2, lw // 2),),
                                       aligned_refs=(True,), reference_downscale_factor=2)
        g = {"latent": torch.zeros(1, 24, lt, lh // 2, lw // 2), "resolved_frame_index": 0,
             ug.STRIDE_KEY: 2}
        ours = mm.PackedLayout(text, lt, lh, lw, at, keyframes=[g])
        same = (ours.seq_len == ref.sequence_length
                and float((ours.position_ids - ref.position_ids).abs().max()) < 1e-9)
        ck(f"trainer packing, {lw * 16}x{lh * 16} T{lt} tile", same)


def orchestration_checks(tu):
    print("upsample() with a guide")
    import comfy.nested_tensor as nt
    seen, decoded = [], []
    saved = (tu._device, tu.recipe_sigmas, tu.upscale_chunk, tu.sample_tile, tu._free)
    tu._device = lambda: torch.device("cpu")
    tu._free = lambda m: None
    tu.upscale_chunk = lambda v, name, w, h: F.interpolate(
        v[0].permute(1, 0, 2, 3), size=(h // 16, w // 16), mode="nearest"
    ).permute(1, 0, 2, 3)[None]

    def fake_sample(model, cond, sampler, sg, v, a, noise, seed):
        seen.append((cond, tuple(v.shape)))
        return v
    tu.sample_tile = fake_sample

    def decode(v):
        decoded.append(int(v.shape[2]))
        return torch.rand(tu.frames_in(v.shape[2]), v.shape[3] * 16, v.shape[4] * 16, 3)

    def encode(px):
        # H3's real rule (vae.py encode_temporal): 5 steps per 17-frame clip,
        # minus the last 3. 102 frames -> 27, which is what broke on the GPU.
        k = -(-int(px.shape[0]) // 17) * 5 - 3
        return torch.zeros(1, 24, k, px.shape[1] // 16, px.shape[2] // 16)

    def pixels(frames, w, h, f):
        x = F.interpolate(frames.permute(0, 3, 1, 2), size=(h // f, w // f), mode="area")
        return x.permute(0, 2, 3, 1)

    # encode_steps against the real rule, for every chunk length a plan makes
    bad = []
    for steps in range(1, 80):
        try:
            got = tu.encode_steps(encode, torch.zeros(tu.frames_in(steps), 32, 32, 3), steps)
            if int(got.shape[2]) != steps:
                bad.append(steps)
        except RuntimeError:
            bad.append(steps)
    ck("encode_steps returns exactly N steps, N = 1..79", not bad, str(bad[:8]))

    cap = [["CAPTION", {}]]
    guide = tu.Guide(factor=2, cond=cap, decode=decode, encode=encode, pixels=pixels)
    tokens = 62                                   # 3 chunks at the recipe's 102 / 17
    lat = {"samples": nt.NestedTensor((torch.randn(1, 24, tokens, 48, 84),
                                       torch.randn(1, 32, 2, 400)))}
    try:
        tu.recipe_sigmas = lambda model, denoise=0.2, *a, **kw: torch.tensor([0.57, 0.0])
        out = tu.upsample(None, lambda f0, f1: [["HOP", {}]], lat, None, "x", 2.0,
                          0.1, guide=guide)
        v = out["samples"].tensors[0]
        ck("output on the 64 px grid", v.shape[3] % 4 == 0 and v.shape[4] % 4 == 0,
           f"{v.shape[4] * 16}x{v.shape[3] * 16}")
        ck("every tile conditioned on the caption, not the hop",
           seen and all(c[0][0] == "CAPTION" for c, _ in seen))
        ok_shape, ok_pin, chunk_starts = True, True, 0
        for c, vs in seen:
            kfs = c[0][1].get("minimax_keyframes") or []
            gk = [k for k in kfs if k.get(tu.STRIDE_KEY)]
            pins = [k for k in kfs if not k.get(tu.STRIDE_KEY)]
            if len(gk) != 1 or tuple(gk[0]["latent"].shape[2:]) != (vs[2], vs[3] // 2, vs[4] // 2):
                ok_shape = False
            chunk_starts += bool(pins)
            if pins and (pins[0]["resolved_frame_index"] != 0 or pins[0]["latent"].shape[2] != 1):
                ok_pin = False
        ck("every tile carries one guide of its own shape / 2", ok_shape)
        ck("chunks after the first keep their frame-0 pin beside it",
           ok_pin and chunk_starts > 0 and chunk_starts < len(seen))
        ck("one guide decode per chunk", len(decoded) == len(tu.chunks(tokens)),
           f"{decoded}")
        decoded.clear()
        tu.recipe_sigmas = lambda model, denoise=0.2, *a, **kw: None
        tu.upsample(None, cap, lat, None, "x", 2.0, 0.0, guide=guide)
        ck("denoise 0: no guide decode at all", not decoded)
    finally:
        (tu._device, tu.recipe_sigmas, tu.upscale_chunk, tu.sample_tile, tu._free) = saved


def chain_wiring_checks():
    """upsample_dit: a socket appended last, defaulted off, shifted before use."""
    print("chain wiring")
    import ast
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HandTieClips")
    run = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "run")
    names = [a.arg for a in run.args.args]
    defaults = dict(zip(names[-len(run.args.defaults):], run.args.defaults))
    ck("run() takes upsample_dit=None",
       "upsample_dit" in defaults and isinstance(defaults["upsample_dit"], ast.Constant)
       and defaults["upsample_dit"].value is None)
    opt = src[src.index('"optional": {'):src.index('"hidden": {')]
    keys = [ln.strip().split('"')[1] for ln in opt.splitlines()
            if ln.startswith('                "') and '": (' in ln]
    ck("upsample_dit is declared after upsample_tile_tokens",
       "upsample_dit" in keys and "upsample_tile_tokens" in keys
       and keys.index("upsample_dit") == keys.index("upsample_tile_tokens") + 1,
       keys[-5:] if keys else "")
    ck("upsample_dit is a MODEL socket", '"upsample_dit": ("MODEL"' in opt)
    at = src.index("if upsample_dit is not None:\n                # Its own model")
    ck("upsample_dit is sigma-shifted before the re-draw uses it",
       "MiniMaxH3SigmaShift" in src[at:at + 600] and "model=upsample_dit" in src[at:at + 600])


def main():
    tu = load(os.path.join(HERE, "tiled_upsampler.py"), "htc_tiled_upsampler")
    ug = load(os.path.join(HERE, "upscale_guide.py"), "htc_upscale_guide")
    header_checks(ug)
    grid_checks(tu)
    tile_cond_checks(tu)
    layout_checks(ug)
    orchestration_checks(tu)
    chain_wiring_checks()
    print("\nall upscale guide checks passed" if not FAIL
          else f"\n{len(FAIL)} FAILED: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
