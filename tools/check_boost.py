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
                 "boost_lora_2", "boost_lora_2_strength"):
        ck(f"{name} declared and accepted by run()",
           name in opt and name in sig)
    ck("realism_boost ships off",
       opt["realism_boost"][1].get("default") == "off"
       and sig["realism_boost"].default == "off")
    ck("the LoRA combos always offer None",
       "None" in opt["boost_lora_1"][0] and "None" in opt["boost_lora_2"][0])
    src = inspect.getsource(H3.HandTieClips.run)
    ck("run() refuses boost beside hop_refine",
       "realism_boost and hop_refine are both on" in src)
    ck("run() refuses boost beside master_audio_file",
       "realism_boost does not run with master_audio_file" in src)
    ck("the boost field is keyed only when on",
       'if boost_on:\n                    hop_payload["boost"]' in src)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        sys.exit(1)
    print("all boost checks passed")


if __name__ == "__main__":
    main()
