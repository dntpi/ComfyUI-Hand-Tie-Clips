r"""Offline CPU tests for Refine v2 (detail-only high-pass blend + universal low-sigma schedule).

    D:\ComfyUI\venv\Scripts\python.exe tools\check_refine_detail.py

No server, no GPU, no model weights on disk.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
COMFY = os.path.dirname(os.path.dirname(PACK))
sys.path.insert(0, PACK)
sys.path.insert(0, COMFY)

import torch  # noqa: E402

import refine_blend as rb  # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-62s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_h3():
    pkg = types.ModuleType("htcpack")
    pkg.__path__ = [PACK]
    sys.modules["htcpack"] = pkg
    spec = importlib.util.spec_from_file_location(
        "htcpack.h3_ref_chain", os.path.join(PACK, "h3_ref_chain.py"),
        submodule_search_locations=[PACK],
    )
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack.h3_ref_chain"] = m
    spec.loader.exec_module(m)
    return m


def main():
    torch.manual_seed(42)

    print("1. w=0 everywhere -> output == raw (exact)")
    raw = torch.randn((1, 24, 12, 16, 16), dtype=torch.float32)
    ref = raw + torch.randn_like(raw) * 0.5
    w0 = [0.0] * 12
    out0 = rb.blend_video_detail(raw, ref, w0, sigma=2.0)
    ck("w=0 everywhere equals raw bit-for-bit",
       torch.equal(out0, raw),
       f"max diff = {float((out0 - raw).abs().max()):g}")

    print("\n2. Constant D (pure colour/level shift) is rejected (output == raw within 1e-5)")
    d_const = torch.full_like(raw, 0.75)
    out_const = rb.blend_video_detail(raw, raw + d_const, [1.0] * 12, sigma=2.0)
    max_const_err = float((out_const - raw).abs().max())
    ck("constant offset across (H,W) is removed by high-pass",
       max_const_err < 1e-5,
       f"max diff = {max_const_err:.2e}")

    print("\n3. 1-px checkerboard D passes through (~raw + w*D within 5%)")
    yy = torch.arange(32, dtype=torch.float32).view(1, 1, 1, 32, 1)
    xx = torch.arange(32, dtype=torch.float32).view(1, 1, 1, 1, 32)
    checker = ((yy + xx) % 2) * 2.0 - 1.0  # +1 / -1 alternating at 1 latent px
    raw32 = torch.zeros((1, 4, 6, 32, 32), dtype=torch.float32)
    ref32 = raw32 + checker
    out_chk = rb.blend_video_detail(raw32, ref32, [1.0] * 6, sigma=2.0)
    # Check interior away from reflect boundary
    rel_err = float((out_chk[:, :, :, 4:-4, 4:-4] - checker[:, :, :, 4:-4, 4:-4]).abs().mean())
    ck("1-px checkerboard detail preserved within 5%",
       rel_err < 0.05,
       f"mean abs diff = {rel_err:.4f}")

    print("\n4. No temporal leakage across latent steps")
    raw_t = torch.randn((1, 8, 10, 16, 16), dtype=torch.float32)
    ref_t = raw_t.clone()
    ref_t[:, :, 5] += torch.randn((1, 8, 16, 16), dtype=torch.float32)
    out_t = rb.blend_video_detail(raw_t, ref_t, [1.0] * 10, sigma=2.0)
    other_frames_diff = max(
        float((out_t[:, :, t] - raw_t[:, :, t]).abs().max())
        for t in range(10) if t != 5
    )
    ck("perturbing frame 5 leaves all other frames untouched",
       other_frames_diff == 0.0,
       f"other frames max diff = {other_frames_diff:g}")

    print("\n5. Shapes, dtypes (fp32 & bf16), and tiny latents (< kernel size)")
    for dt in (torch.float32, torch.bfloat16):
        r_dt = torch.randn((1, 24, 5, 16, 16), dtype=dt)
        f_dt = r_dt + torch.randn_like(r_dt) * 0.2
        o_dt = rb.blend_video_detail(r_dt, f_dt, [0.0, 0.25, 0.5, 0.75, 1.0], sigma=2.0)
        ck(f"preserves shape and dtype={dt}",
           o_dt.shape == r_dt.shape and o_dt.dtype == dt)
    for tiny_hw in ((1, 1), (2, 3), (4, 4)):
        r_tiny = torch.randn((1, 4, 3, tiny_hw[0], tiny_hw[1]), dtype=torch.float32)
        f_tiny = r_tiny + torch.randn_like(r_tiny) * 0.1
        o_tiny = rb.blend_video_detail(r_tiny, f_tiny, [0.0, 0.5, 1.0], sigma=2.0)
        ck(f"tiny latent HxW={tiny_hw} clamps kernel without crashing",
           o_tiny.shape == r_tiny.shape and not torch.isnan(o_tiny).any())

    print("\n6. Suffix parsing and detail=0 bit-for-bit crossfade equivalence")
    ck("default detail sigma is 2.0 when omitted",
       rb.parse_detail("0:0, 22:0, 44:1") == 2.0
       and rb.parse_detail(rb.resolve("auto", 22)) == 2.0)
    ck("explicit '| detail=1.5' parses keys and sigma",
       rb.parse("0:0, 22:0, 44:1 | detail=1.5") == [(0.0, 0.0), (22.0, 0.0), (44.0, 1.0)]
       and rb.parse_detail("0:0, 22:0, 44:1 | detail=1.5") == 1.5)
    ck("'auto | detail=2.0' resolves ramp and keeps suffix",
       rb.resolve("auto | detail=2.0", 22) == "0:0, 22:0, 44:1 | detail=2.0")
    w_ramp = rb.step_weights(12, rb.parse(rb.DEFAULT_RAMP), "linear")
    lerp_ref = rb.blend_video(raw, ref, w_ramp)
    det0_ref = rb.blend_video_detail(raw, ref, w_ramp, sigma=0.0)
    ck("detail=0 reproduces blend_video bit-for-bit",
       torch.equal(lerp_ref, det0_ref))

    hot_ref = raw + (raw - rb.gauss2d_per_frame(raw, 2.0)) * 2.5
    capped = rb.blend_video_detail(raw, hot_ref, [1.0] * 12, sigma=2.0, cap_high=True)
    raw_hi_rms = float((raw - rb.gauss2d_per_frame(raw, 2.0)).pow(2).mean().sqrt())
    cap_hi_rms = float((capped - rb.gauss2d_per_frame(capped, 2.0)).pow(2).mean().sqrt())
    ck("cap_high=True prevents refined high-band RMS from exceeding raw",
       cap_hi_rms <= raw_hi_rms * 1.02,
       f"raw_hi_rms={raw_hi_rms:.4f}, cap_hi_rms={cap_hi_rms:.4f}")

    # Coarse tone pull-back test: when raw drifts +1.0 brighter than refined,
    # tone_pull=0.35 pulls the mean back by ~0.35 toward refined.
    drifted_raw = raw + 1.0
    pulled = rb.blend_video_detail(drifted_raw, raw, [1.0] * 12, sigma=2.0,
                                   cap_high=True, tone_pull=0.35)
    mean_pull = float((drifted_raw - pulled).mean().item())
    ck("tone_pull=0.35 pulls ultra-coarse exposure ~35% toward refined",
       0.33 <= mean_pull <= 0.37,
       f"mean_pull={mean_pull:.4f}")

    # Hop-1 high-band RMS anchor test: when Hop 4's raw AND refined both ratchet
    # +25% hotter than Hop 1, high_rms_ref pulls Hop 4 back to Hop 1's level.
    hop1_hi_ref = rb.high_rms_stats(raw, sigma=2.0)
    ratcheted_raw = rb.gauss2d_per_frame(raw, 2.0) + (raw - rb.gauss2d_per_frame(raw, 2.0)) * 1.25
    ratcheted_ref = rb.gauss2d_per_frame(ref, 2.0) + (ref - rb.gauss2d_per_frame(ref, 2.0)) * 1.30
    anchored = rb.blend_video_detail(
        ratcheted_raw, ratcheted_ref, [1.0] * 12, sigma=2.0,
        cap_high=True, high_rms_ref=hop1_hi_ref,
    )
    anc_hi_rms = float((anchored - rb.gauss2d_per_frame(anchored, 2.0)).pow(2).mean().sqrt())
    ck("high_rms_ref pulls a +25% ratcheted single-DiT hop back to Hop 1's high-band RMS",
       anc_hi_rms <= raw_hi_rms * 1.03,
       f"hop1_hi_rms={raw_hi_rms:.4f}, anchored_hi_rms={anc_hi_rms:.4f}")

    print("\n7. Low-sigma schedule builder & cache key isolation")
    H3 = load_h3()

    class _StubSampling:
        sigmas = torch.linspace(0.02, 1.0, 1000)

    class _StubInner:
        model_sampling = _StubSampling()

    class _StubModel:
        model = _StubInner()

    stub = _StubModel()
    for steps_k in (2, 3):
        sched = H3._build_lowsig_schedule(stub, "simple", steps_k, 0.25)
        ck(f"lowsig steps={steps_k}: shape is ({steps_k + 1},), starts <= 0.25, ends at 0.0",
           isinstance(sched, torch.Tensor)
           and sched.numel() == steps_k + 1
           and 0.0 < float(sched[0].item()) <= 0.25 + 1e-5
           and float(sched[-1].item()) == 0.0,
           f"sigmas = {[round(float(x), 4) for x in sched.tolist()]}")

    low_key = ("refine_lowsig", 2, round(0.25, 4), "simple", id(stub.model.model_sampling))
    old_key = ("refine", 2, round(0.25, 4), "simple")
    hop_key = 8
    ck("lowsig cache key cannot collide with hop int key or legacy ('refine', ...) key",
       low_key != old_key and low_key != hop_key and low_key[0] == "refine_lowsig")

    print("\n8. Motion-Context architectural join locks (v2.4)")
    import comfy.nested_tensor as nt
    prev_v = torch.randn((1, 24, 57, 16, 16), dtype=torch.float32)
    prev_a = torch.randn((1, 32, 2, 320), dtype=torch.float32)
    prev_lat = {"samples": nt.NestedTensor((prev_v, prev_a))}
    tgt_v = torch.zeros((1, 24, 57, 16, 16), dtype=torch.float32)
    tgt_a = torch.zeros((1, 32, 2, 320), dtype=torch.float32)
    tgt_lat = {"samples": nt.NestedTensor((tgt_v, tgt_a))}
    inp_lat, v_hold, prior_m = H3._pin_inpaint_head(tgt_lat, prev_lat, 22)
    iv, _ = list(inp_lat["samples"].unbind())[:2]
    vm, am = list(inp_lat["noise_mask"].unbind())[:2]
    ck("_pin_inpaint_head copies prev[-7:] into target[:7] and holds first 6 steps at 0.0",
       v_hold == 6 and prior_m is None
       and torch.equal(iv[:, :, :7], prev_v[:, :, -7:])
       and float(vm[:, :, :6].abs().max()) == 0.0
       and float(vm[:, :, 6:].min()) == 1.0
       and float(am.min()) == 1.0)

    # Simulate an over-cooked hop (+12% std, +0.08 mean) and calibrate it back.
    # The reference is the mod5-aligned window (prev[-12:-7] against cur[5:10]),
    # not prev[-5:], so the latent here is stationary in time: every 5-step
    # window has the same statistics and any of them is a fair yardstick.
    stat_v = torch.randn((1, 24, 1, 16, 16), dtype=torch.float32).expand(1, 24, 57, 16, 16).clone()
    stat_v += 0.01 * torch.randn_like(stat_v)
    stat_lat = {"samples": nt.NestedTensor((stat_v, prev_a.clone()))}
    st_cooked_v = stat_v * 1.12 + 0.08
    st_cooked_lat = {"samples": nt.NestedTensor((st_cooked_v, prev_a.clone()))}
    cal_lat = H3._calibrate_overlap_latent(st_cooked_lat, stat_lat, 22, hop_no=2)
    cv, _ = list(cal_lat["samples"].unbind())[:2]
    ck("_calibrate_overlap_latent restores exact 7-step causal prefix and locks opening mean/std",
       torch.equal(cv[:, :, :7], stat_v[:, :, -7:])
       and abs(float(cv[:, :, 7:12].mean() - stat_v[:, :, -5:].mean())) < 1e-3
       and abs(float(cv[:, :, 7:12].std() / stat_v[:, :, -5:].std()) - 1.0) < 0.02
       and abs(float(cv[:, :, -5:].std() / st_cooked_v[:, :, -5:].std()) - 1.0) < 1e-4)

    cooked_v = prev_v * 1.12 + 0.08
    cooked_lat = {"samples": nt.NestedTensor((cooked_v, prev_a.clone()))}

    # Head-internal refine ramp (`head_steps=7`, `detail_sigma=2.0`) makes step 7 already w=1.0:
    keys_auto = rb.parse(rb.resolve("auto", 22))
    blended_lat = H3._refine_blend_latent(
        prev_lat, cooked_lat, keys_auto, "linear", hop_no=2,
        detail_sigma=2.0, head_steps=7,
    )
    bv, _ = list(blended_lat["samples"].unbind())[:2]
    ck("head-internal refine ramp keeps step 0 raw (w=0) and reaches w=1.0 by step 7",
       torch.equal(bv[:, :, 0], prev_v[:, :, 0])
       and not torch.equal(bv[:, :, 7], prev_v[:, :, 7]))

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
