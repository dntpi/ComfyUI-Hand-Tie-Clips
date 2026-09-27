r"""Offline tests for the realism boost spine -- no GPU, no weights.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_boost.py

Boost is The AI Brief's Singularity dual-sampling recipe inside the hop
sampler. Two things about it are silent when wrong:

**The schedule.** ExtendIntermediateSigmas(2) DOUBLES a schedule (one midpoint
per gap), it does not triple it, and SplitSigmas(2) must leave both stages
sharing the hand-off sigma. Base 6 at shift 12 / simple has to come out as
2 + 10 with the hand-off at ~0.984 -- the numbers the original prints.

**The hand-off.** Stage 2 re-noises stage 1's x0 and hands it over in the
sampler's inverse-scaled space; stage 3's DisableNoise scales it back on the
way in. The pair only cancels to `sigma*noise + (1-sigma)*x0` if both use the
same sigma and stage 2 really adds noise. Get either wrong and the render is
the orange/quilt failure, with no error anywhere.

The real Core sigma nodes run here against a real flow model_sampling. The two
samplers are stubbed, so what is checked is the wiring between them.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return sys.modules["htcpack.h3_ref_chain"]


class FakeModel:
    """Just enough ModelPatcher for BasicScheduler and the hand-off maths."""

    def __init__(self):
        import comfy.model_sampling as cms

        class MS(cms.ModelSamplingAV, cms.CONST):
            pass

        self.ms = MS(None)
        self.ms.set_parameters(shift=12.0, audio_shift=3.0)

    def get_model_object(self, name):
        assert name == "model_sampling", name
        return self.ms


def main():
    H3 = load_pack()
    from comfy.nested_tensor import NestedTensor
    from comfy_extras.nodes_custom_sampler import BasicScheduler

    model = FakeModel()
    ms = model.ms

    def base(steps):
        return H3._result(BasicScheduler.execute(
            model=model, scheduler="simple", steps=steps, denoise=1.0))[0]

    print("schedule: Extend(2) doubles, Split(2) shares the hand-off")
    for steps, want in ((6, (2, 10)), (4, (2, 6)), (8, (2, 14))):
        hi, lo = H3._boost_sigmas(base(steps), {}, steps)
        got = (int(hi.shape[-1]) - 1, int(lo.shape[-1]) - 1)
        ck(f"base {steps} -> {want[0]} + {want[1]}", got == want, str(got))
        ck(f"base {steps}: both stages share the hand-off sigma",
           float(hi[-1]) == float(lo[0]), f"{float(lo[0]):.4f}")
    hi, lo = H3._boost_sigmas(base(6), {}, 6)
    ck("base 6 hands off at ~0.984 (the original's number)",
       abs(float(lo[0]) - 0.984) < 0.001, f"{float(lo[0]):.4f}")
    ck("stage 1 is the first base step, halved",
       abs(float(hi[-1]) - float(base(6)[1])) < 1e-6)
    cache = {}
    first = H3._boost_sigmas(base(6), cache, 6)
    ck("boost schedules cache apart from the hop's int keys",
       ("boost", 6) in cache and 6 not in cache
       and H3._boost_sigmas(base(6), cache, 6) is first)

    print("\nhand-off: stage 2 + DisableNoise cancel to a noised latent")
    calls = []
    real = H3.SamplerCustomAdvanced
    torch.manual_seed(0)
    v_out = torch.randn(1, 24, 3, 4, 4)
    v_den = torch.randn(1, 24, 3, 4, 4)
    a_out = torch.randn(1, 32, 8)
    a_den = torch.randn(1, 32, 8)

    class Stub:
        @staticmethod
        def execute(noise, guider, sampler, sigmas, latent_image):
            calls.append({"noise": noise, "guider": guider, "sigmas": sigmas,
                          "latent": latent_image})
            if len(calls) == 1:
                return ({"samples": NestedTensor((v_out, a_out))},
                        {"samples": NestedTensor((v_den, a_den))})
            return (latent_image, latent_image)

    H3.SamplerCustomAdvanced = Stub
    try:
        latent = {"samples": NestedTensor((torch.zeros_like(v_out),
                                           torch.zeros_like(a_out)))}
        H3._boost_sampled(
            latent, model=model, guider="G1", boost_guider="G3",
            sampler=None, hop_sigmas=base(6), sigma_cache={}, hop_steps=6,
            seed=1234, hop_no=1, label="res_multistep/simple")
    finally:
        H3.SamplerCustomAdvanced = real

    ck("two sampler calls, stage 2 costs none", len(calls) == 2,
       f"{len(calls)} calls")
    if len(calls) == 2:
        s1, s3 = calls
        ck("stage 1: model guider on the high band",
           s1["guider"] == "G1" and torch.equal(s1["sigmas"], hi))
        ck("stage 1: RandomNoise on the shot seed",
           getattr(s1["noise"], "seed", None) == 1234)
        ck("stage 3: boost guider on the low band",
           s3["guider"] == "G3" and torch.equal(s3["sigmas"], lo))
        z = s3["noise"].generate_noise({"samples": v_den})
        ck("stage 3: DisableNoise", not z.any().item())
        v3, a3 = s3["latent"]["samples"].unbind()
        s = lo[0]
        n = s1["noise"].generate_noise({"samples": v_den})
        entered = ms.noise_scaling(s, torch.zeros_like(v3), v3)
        want = s * n + (1.0 - s) * v_den
        ck("video enters stage 3 as sigma*noise + (1-sigma)*x0",
           torch.allclose(entered, want, atol=1e-5),
           f"max err {float((entered - want).abs().max()):.2e}")
        ck("video re-noises from stage 1's DENOISED, not its output",
           not torch.allclose(entered, s * n + (1.0 - s) * v_out, atol=1e-3))
        ck("audio passes on from stage 1's OUTPUT", torch.equal(a3, a_out))

    print("\nstage 3 needs steps")
    try:
        H3._boost_sampled(
            latent, model=model, guider="G1", boost_guider="G3",
            sampler=None, hop_sigmas=base(1), sigma_cache={}, hop_steps=1,
            seed=1, hop_no=1, label="x")
        ck("steps=1 is refused", False, "ran")
    except RuntimeError as e:
        ck("steps=1 is refused", "steps >= 2" in str(e))

    print("\nwidgets and run()")
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}
    sig = inspect.signature(H3.HandTieClips.run).parameters
    for name in ("realism_boost", "boost_lora_1", "boost_lora_1_strength",
                 "boost_lora_2", "boost_lora_2_strength",
                 "boost_lora_3", "boost_lora_3_strength"):
        ck(f"{name} declared and accepted by run()",
           name in opt and name in sig)
    ck("realism_boost ships off",
       opt["realism_boost"][1].get("default") == "off"
       and sig["realism_boost"].default == "off")
    ck("the LoRA combos always offer None",
       all("None" in opt[f"boost_lora_{k}"][0] for k in (1, 2, 3)))
    ck("boost_lora_3 ships empty",
       opt["boost_lora_3"][1].get("default") == "None"
       and sig["boost_lora_3"].default == "None")
    src = inspect.getsource(H3.HandTieClips.run)
    ck("run() allows boost beside hop_refine and says so",
       "realism_boost and hop_refine are both on" not in src
       and "realism_boost + hop_refine=" in src)
    ck("run() refuses boost beside master_audio_file",
       "realism_boost does not run with master_audio_file" in src)
    ck("the boost field is keyed only when on",
       'if boost_on:\n                    hop_payload["boost"]' in src)

    print("\nlatent upscale: the recipe's step between stage 1 and the re-noise")
    for name in ("boost_upscale", "boost_upscale_by"):
        ck(f"{name} declared and accepted by run()",
           name in opt and name in sig)
    ck("boost_upscale ships None (inert)",
       opt["boost_upscale"][1].get("default") == "None"
       and sig["boost_upscale"].default == "None")
    ck("boost_upscale_by defaults to the recipe's 1.25",
       opt["boost_upscale_by"][1].get("default") == 1.25
       and sig["boost_upscale_by"].default == 1.25)
    ck("the upscale is keyed only when it runs",
       "if boost_up:\n                        hop_payload[\"boost\"].append" in src)
    ck("stage 3's guider takes the upscaled guides",
       "conditioning=(cond_hi if cond_hi is not None" in src)
    ck("the master is allocated at the delivered size",
       "_alloc_master(total_frames, out_h, out_w)" in src)

    LU = H3._lup
    # Their node: pixel = latent*16*scale, rounded to 32, then to 16 -> cells.
    for (w, h, f) in ((896, 512, 1.25), (1344, 768, 1.25), (768, 768, 1.5),
                      (640, 1152, 1.25), (1024, 576, 2.0)):
        tw, th = LU.upscaled_canvas(w, h, f)
        ref = tuple(round(round(v * f / 32) * 32 / 16) * 16 for v in (w, h))
        ck(f"{w}x{h} x{f:g} -> {tw}x{th}, their node's size",
           (tw, th) == ref and tw % 32 == 0 and th % 32 == 0)

    calls.clear()
    seen = []

    def fake_up(x):
        seen.append(tuple(x.shape))
        return torch.nn.functional.interpolate(
            x, size=(x.shape[2], 8, 8), mode="nearest")

    H3.SamplerCustomAdvanced = Stub
    try:
        H3._boost_sampled(
            latent, model=model, guider="G1", boost_guider="G3",
            sampler=None, hop_sigmas=base(6), sigma_cache={}, hop_steps=6,
            seed=1234, hop_no=1, label="x", upscale=fake_up)
    finally:
        H3.SamplerCustomAdvanced = real
    ck("the upscaler sees stage 1's DENOISED video, once",
       seen == [tuple(v_den.shape)])
    if len(calls) == 2:
        s1, s3 = calls
        ck("stage 1 samples on the canvas",
           tuple(s1["latent"]["samples"].unbind()[0].shape[-2:]) == (4, 4))
        v3, a3 = s3["latent"]["samples"].unbind()
        x_up = fake_up(v_den)
        seen.pop()
        n = s1["noise"].generate_noise({"samples": x_up})
        entered = ms.noise_scaling(lo[0], torch.zeros_like(v3), v3)
        want = lo[0] * n + (1.0 - lo[0]) * x_up
        ck("stage 3 gets the upscaled video, noised at the NEW size",
           tuple(v3.shape[-2:]) == (8, 8)
           and torch.allclose(entered, want, atol=1e-5),
           f"max err {float((entered - want).abs().max()):.2e}")
        ck("audio still passes on from stage 1's output",
           torch.equal(a3, a_out))

    big = {"samples": NestedTensor((torch.randn(1, 24, 3, 8, 8),
                                    torch.randn(1, 32, 8))),
           "noise_mask": "x"}
    small = {"samples": NestedTensor((torch.zeros(1, 24, 3, 4, 4),
                                      torch.zeros(1, 32, 8)))}
    shr = H3._pin_on_canvas(big, small)
    sv, sa = shr["samples"].unbind()
    bv, ba = big["samples"].unbind()
    ck("the stage-1 pin is the previous hop area-shrunk, audio untouched",
       tuple(sv.shape) == (1, 24, 3, 4, 4) and torch.equal(sa, ba)
       and torch.allclose(sv[..., 0, 0], bv[..., :2, :2].mean((-2, -1)),
                          atol=1e-5)
       and "noise_mask" not in shr)
    ck("no pin stays no pin", H3._pin_on_canvas(None, small) is None)
    rz = H3._latent_resized(small, 8, 10)
    rv, ra = rz["samples"].unbind()
    ck("the stage-3 guide latent is the hop latent on the bigger grid",
       tuple(rv.shape) == (1, 24, 3, 8, 10) and ra.shape == (1, 32, 8))

    print("\nlatent upscaler: HTC's runner")
    torch.manual_seed(0)

    def fake_sd(c=32, emb=8, cin=24):
        # Two residual blocks and one temporal conv each side, in the
        # interleave the shipped weights use: res, temporal, res.
        d = {"conv_in.weight": torch.zeros(c, cin, 3, 3, 3),
             "embed.0.weight": torch.zeros(emb, 1)}
        for side in ("in_blocks", "out_blocks"):
            d[f"{side}.0.in_layers.2.weight"] = torch.zeros(1)
            d[f"{side}.1.dwconv.weight"] = torch.zeros(c, 1, 5, 1, 1)
            d[f"{side}.2.in_layers.2.weight"] = torch.zeros(1)
        return d
    net = LU.LatentUpscaler(fake_sd())
    kinds = [type(b).__name__ for b in net.in_blocks]
    ck("the block list is read from the keys", kinds == ["_Res", "_Temporal", "_Res"],
       str(kinds))
    for prm in net.parameters():
        torch.nn.init.normal_(prm, std=0.05)
    net.eval()
    x = torch.randn(1, 24, 6, 4, 4)
    with torch.inference_mode():
        whole = net(x, (6, 5, 5), 1.25)
        win = LU._windowed(net, x, (6, 5, 5), 1.25, torch.device("cpu"))
    ck("a clip shorter than one window is identical windowed or whole",
       torch.allclose(whole, win, atol=1e-5))
    x = torch.randn(1, 24, 60, 4, 4)
    with torch.inference_mode():
        win = LU._windowed(net, x, (60, 5, 5), 1.25, torch.device("cpu"))
    ck("windows cover a long clip, no gaps",
       tuple(win.shape) == (1, 24, 60, 5, 5) and torch.isfinite(win).all())
    try:
        LU.load("x.pth")
        ck("pickle files are refused", False, "loaded")
    except RuntimeError as e:
        ck("pickle files are refused", "no pickle" in str(e))
    for f in [c for c in LU.choices() if c != "None"]:
        try:
            LU._CACHE.clear()
            m = LU.load(f)
            ck(f"installed {f} loads strictly", m is not None)
        except Exception as e:  # noqa: BLE001
            ck(f"installed {f} loads strictly", False, repr(e)[:80])
    LU._CACHE.clear()

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        sys.exit(1)
    print("all boost checks passed")


if __name__ == "__main__":
    main()
