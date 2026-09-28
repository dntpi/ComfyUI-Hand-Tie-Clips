r"""Offline checks for the H3 Tiled Upsampler -- no GPU, no H3 weights.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_tiled_upsampler.py
    D:\ComfyUI\venv\Scripts\python.exe tools\check_tiled_upsampler.py --weights

The node is HTC's own code for a recipe proven in bbaudio-2025's
Comfyui-MMH3-UltimateUpscale graph, and "the same recipe" is only true if it
produces the same latent. So when that pack is installed, this runs both on
the same input and requires them to be EQUAL, not close:

* the chunk plan, tile-grid solver, tile axes and feather against theirs, over
  a sweep of sizes;
* the megapixel size against LD's H3UpscaleSizeLD (the node that fed the
  recipe its size), when that pack is installed;
* the whole pipeline end to end -- chunking, upscale call, keyframe trimming,
  resizing and cropping, the frame-0 pin, the shared noise field, joint
  tiling, the feathered merge and the chunk crossfade -- with the diffusion
  sampler and the upscaler network replaced by the same deterministic fakes
  on both sides, so nothing but the orchestration can differ.

`--weights` also loads the real upscaler file into both networks and requires
identical output in fp32 on the CPU (slow: ~1 GB of weights, twice).

Without their pack only the self-contained checks run, and the output says so.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUSTOM = os.path.dirname(HERE)
COMFY = os.path.dirname(CUSTOM)
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-60s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def self_checks(tu):
    print("self-contained")
    w, h = tu.target_size(480, 608, 4.0)
    ck("480x608 at 4 MP lands on the 32 grid near 4 MP",
       w % 32 == 0 and h % 32 == 0 and abs(w * h / 1048576 - 4.0) < 0.1,
       f"{w}x{h}")
    for tv in (7, 27, 42, 57, 72, 107):
        plan = tu.chunks(tv)
        cover = plan[0][0] == 0 and plan[-1][2] == tv and all(
            plan[i][0] <= plan[i - 1][2] and plan[i][0] % 5 == 0
            for i in range(1, len(plan)))
        ck(f"chunk plan covers {tv} latent steps on key steps", cover,
           str([(a, c) for a, _b, c, _d in plan]))
    tiles, grid = tu.tile_grid(128, 112, 30)
    worst = tu.seq_len(grid[2], grid[3], 30)
    ck("tile grid for 1792x2048 x 30 steps fits the budget",
       worst <= tu.TOKEN_BUDGET, f"{grid[0]}x{grid[1]}, {worst} tokens")
    cover = torch.zeros(128, 112)
    for r0, c0, rows, cols, st, sl, t, lft, b, rgt in tiles:
        cover[r0:r0 + rows, c0:c0 + cols] += tu.feather(
            rows, cols, st, sl, t, lft, b, rgt, "cpu")
    ck("feathered tiles cover every cell", bool((cover > 0).all()))
    v = torch.randn(1, 24, 12, 4, 4)
    a = torch.randn(1, 32, 2, 50)
    a0 = tu.audio_span(tu.frames_in(5), 0)[0]
    jv, ja = tu.join_chunk(v[:, :, :7], a[..., :a0 + 12], v[:, :, 5:],
                           a[..., a0:], 5, tu.frames_in(5))
    ck("joining two overlapping cuts of one clip returns the clip",
       torch.allclose(jv, v) and torch.allclose(ja, a))


# --- against their pack -----------------------------------------------------

class FakeNet(nn.Module):
    """The same deterministic stand-in for the upscaler on both sides."""

    def __init__(self):
        super().__init__()
        self.p = nn.Parameter(torch.zeros(1))

    def forward(self, x, *args, **kw):
        if "target_size" in kw:
            size, scale = kw["target_size"], kw["scale"]
        else:
            size, scale = args
        y = F.interpolate(x.float(), size=size, mode="trilinear",
                          align_corners=False)
        return (y * (1.0 + 0.01 * float(scale)) + 0.05).to(x.dtype)


def fake_draw(xt, noise_v, sigmas, cond):
    """The same deterministic stand-in for one tile's sampling on both sides.

    It reads everything the orchestration is responsible for: the tile, its
    slice of the noise field, the step's sigmas, every keyframe latent (which
    must be cropped to the tile, or the add fails), its frame index, and the
    cond noise aug the frame-0 pin sets.
    """
    s0 = float(sigmas[0])
    out = xt.float() * (1.0 - s0) + noise_v.float() * s0
    d = cond[0][1]
    for n, kf in enumerate(d.get("minimax_keyframes") or []):
        lt = kf.get("latent")
        if lt is not None:
            out = out + 0.01 * (n + 1) * lt.float().mean(dim=2, keepdim=True) \
                + 0.001 * float(kf["resolved_frame_index"])
    out = out + 0.1 * float(d.get("minimax_visual_cond_noise_aug", 0.0))
    return out.to(xt.dtype)


def against_mmh3(tu, mm, sz):
    print("against Comfyui-MMH3-UltimateUpscale")
    ok = all(tu.chunks(tv) == mm.compute_segments(tv, 102, 17)[0]
             for tv in range(2, 180))
    ck("chunk plan identical for 2..179 latent steps", ok)

    bad = []
    for wpx in range(512, 3073, 160):
        for hpx in range(512, 3073, 224):
            for t in (7, 22, 30, 42):
                a = tu.solve_grid(wpx, hpx, t)
                b = mm.solve_grid_for_budget(wpx, hpx, t, 128, 128, 70000, 256)
                if a != b:
                    bad.append((wpx, hpx, t, a, b))
    ck("tile-grid solver identical over a size sweep", not bad,
       str(bad[:2]) if bad else "")

    bad = []
    for size in range(4, 140, 3):
        for tile in (16, 24, 40, 64):
            for ov in (0, 4, 8):
                if ov >= tile:
                    continue
                if tu._axis(size, tile, ov, 16) != mm._grid_1d(size, tile, ov, 16):
                    bad.append((size, tile, ov))
    ck("tile axes identical", not bad, str(bad[:3]) if bad else "")

    same = True
    for args in ((40, 36, 8, 8, True, True, True, True),
                 (40, 36, 8, 0, False, True, False, False),
                 (20, 20, 12, 12, True, False, True, True)):
        a = tu.feather(*args[:4], args[4], args[5], args[6], args[7], "cpu")
        b = mm._feather_mask(*args[:4], top=args[4], left=args[5],
                             bottom=args[6], right=args[7], device="cpu",
                             dtype=torch.float32)
        same = same and torch.equal(a, b)
    ck("feather identical", same)

    if sz is not None:
        node = sz.H3UpscaleSizeLD()
        bad = []
        for w, h in ((480, 608), (608, 480), (768, 1344), (1344, 768),
                     (512, 512), (896, 512), (736, 1280)):
            for mp in (1.0, 2.0, 3.3, 4.0, 6.0):
                theirs = node.run(w, h, "megapixels", mp, 2.0, 1440, 32)[:2]
                if tu.target_size(w, h, mp) != tuple(theirs):
                    bad.append((w, h, mp))
        ck("megapixel size identical to H3UpscaleSizeLD", not bad,
           str(bad[:3]) if bad else "")
    else:
        print("  --   ComfyUI-H3-UpscaleSize not installed; size not compared")

    end_to_end(tu, mm)


def end_to_end(tu, mm):
    import comfy.nested_tensor as nt

    torch.manual_seed(7)
    tv, h, w = 72, 10, 8                        # 243 frames at 128x160 px
    video = torch.randn(1, 24, tv, h, w)
    audio = torch.randn(1, 32, 2, 405)
    kf0 = {"resolved_frame_index": 0, "latent": torch.randn(1, 24, 1, h, w)}
    kf_mid = {"resolved_frame_index": 120, "latent": torch.randn(1, 24, 2, h, w)}
    kf_aud = {"resolved_frame_index": 40, "audio_latent": torch.randn(1, 32, 2, 30)}
    refs = [{"kind": "image", "latent": torch.randn(1, 24, 1, 6, 6),
             "latent_h": 99, "latent_w": 99},
            {"kind": "image", "latent_h": 4, "latent_w": 4},
            {"kind": "audio", "ref_audio_t": 10}]
    cond = [[torch.zeros(1, 4, 8),
             {"minimax_keyframes": [kf0, kf_mid, kf_aud], "minimax_refs": refs}]]
    latent = {"samples": nt.NestedTensor((video, audio))}
    sigmas = torch.tensor([0.35, 0.0])
    w_px, h_px = tu.target_size(w * 16, h * 16, 4.0)

    net = FakeNet()
    mm.load_upscale_model = lambda name, dev, prec: net
    mm.unload_upscale_model = lambda name, dev, prec: None

    def their_piece(piece, c, model, noise, sampler, sg, negative, cfg):
        xt, a = piece["samples"].tensors
        nz = noise.generate_noise(piece)
        return nt.NestedTensor((fake_draw(xt, nz.tensors[0], sg, c), a))

    mm.sample_piece = their_piece

    saved = (tu.load_upscaler, tu._device, tu.recipe_sigmas, tu.sample_tile)
    tu.load_upscaler = lambda name: net
    tu._device = lambda: torch.device("cpu")
    tu.recipe_sigmas = lambda model: sigmas
    tu.sample_tile = (lambda model, c, sampler, sg, v, a, noise, seed:
                      fake_draw(v, noise[0], sg, c))
    try:
        up = mm.MMH3LatentUpscaleWithModelParams.execute(
            "fake.safetensors", w_px, h_px, "cpu", "fp16").result[0]
        tp = mm.MMH3TemporalSplitParams.execute(102, 17, 0.999).result[0]
        sp = mm.MMH3SpatialSplitParams.execute(
            2560, 1920, "auto", 736, 736, 3, 1, 128, 128, 32, 32, 256,
            "earlier", "linear", True, 70000).result[0]

        class Seed:
            seed = tu.SEED

        theirs = mm.MMH3UltimateUpscale.execute(
            latent=latent, conditioning=cond, model=object(), noise=Seed(),
            sampler=None, sigmas=sigmas, temporal_split_param=tp,
            spatial_split_param=sp, latent_upscale_param=up).result[0]
        ours = tu.upsample(object(), cond, latent, None, "fake.safetensors", 4.0)
    finally:
        (tu.load_upscaler, tu._device, tu.recipe_sigmas, tu.sample_tile) = saved

    tv_, ta_ = theirs["samples"].tensors
    ov_, oa_ = ours["samples"].tensors
    ck("end to end: video latent identical",
       tv_.shape == ov_.shape and torch.equal(tv_, ov_),
       f"{tuple(ov_.shape)}")
    ck("end to end: audio latent identical",
       ta_.shape == oa_.shape and torch.equal(ta_, oa_), f"{tuple(oa_.shape)}")
    ck("end to end: the clip was really upscaled and tiled",
       ov_.shape[3] * 16 == h_px and ov_.shape[4] * 16 == w_px,
       f"{w_px}x{h_px}")


def weights_check(tu, mm):
    print("real weights, fp32 on the CPU")
    import folder_paths
    tu.register_folder()
    path = folder_paths.get_full_path(tu.FOLDER, tu.UPSCALER_FILE)
    if not path:
        print(f"  --   {tu.UPSCALER_FILE} not installed; skipped")
        return
    from safetensors.torch import load_file
    sd = load_file(path, device="cpu")
    ours = tu.LatentUpscaler(sd)
    ours.load_state_dict(sd, strict=True)
    ours = ours.float().eval()
    theirs = mm.load_upscale_model(tu.UPSCALER_FILE, torch.device("cpu"), "fp32")
    x = torch.randn(1, 24, 7, 6, 5)
    with torch.inference_mode():
        a = ours(x, (7, 9, 8), 1.55)
        b = theirs(x, scale=1.55, target_size=(7, 9, 8))
    ck("upscaler network identical to theirs", torch.equal(a, b),
       f"max diff {float((a - b).abs().max()):.2e}")


def main():
    torch.set_grad_enabled(False)
    tu = load(os.path.join(HERE, "tiled_upsampler.py"), "htc_tiled_upsampler")
    self_checks(tu)
    mm_path = os.path.join(CUSTOM, "Comfyui-MMH3-UltimateUpscale", "nodes.py")
    sz_path = os.path.join(CUSTOM, "ComfyUI-H3-UpscaleSize", "upscale_size.py")
    if os.path.isfile(mm_path):
        mm = load(mm_path, "mmh3_reference")
        sz = load(sz_path, "h3_upscale_size") if os.path.isfile(sz_path) else None
        if "--weights" in sys.argv:
            weights_check(tu, mm)
        against_mmh3(tu, mm, sz)
    else:
        print("  --   Comfyui-MMH3-UltimateUpscale not installed; the recipe "
              "was not compared")
    print()
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    print("all tiled upsampler checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
