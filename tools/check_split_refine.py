r"""Offline checks for hop_refine=split -- no GPU, no H3 weights.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_split_refine.py

split is the refine as the hop's own finish: stage 1 samples the hop's
schedule down to the start of its last K steps, stage 2 finishes those K
steps, each halved, from the same latent with no new noise. Checked here:

* the sigma arithmetic: stage 1 is the hop's schedule to the hand-off, stage 2
  starts on that sigma and halves every one of the K gaps, total N - K + 2K;
* K: refine_steps, 0 -> SPLIT_DEFAULT_K, always leaving a step on each side;
* the hand-off is exact: with core's euler loop and KSAMPLER's own scaling
  arithmetic (noise_scaling in, inverse_noise_scaling out, CONST flow), stage 1
  then a DisableNoise stage 2 reproduces ONE uninterrupted run over the same
  sigmas -- the (1 - sigma) pair cancels -- with a fake denoiser;
* run() wiring: refused beside realism_boost, its own hop-key field holding
  only what split reads, and the second-pass refine block skipped under it.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-66s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"), submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return sys.modules["htcpack.h3_ref_chain"]


SIG8 = torch.tensor([1.0, 0.988, 0.973, 0.952, 0.923, 0.878, 0.8, 0.632, 0.0])


def sigma_checks(H3):
    print("sigmas")
    for k in (1, 2, 3):
        hi, lo = H3._split_sigmas(SIG8, {}, 8, k)
        n = 8
        ck(f"K={k}: stage 1 = the hop's schedule to the hand-off",
           torch.equal(hi, SIG8[:n - k + 1]))
        ck(f"K={k}: stage 2 starts on the hand-off sigma", float(lo[0]) == float(hi[-1]))
        ck(f"K={k}: stage 2 ends at 0 and keeps every hop sigma",
           float(lo[-1]) == 0.0 and all(any(abs(float(x) - float(y)) < 1e-6 for x in lo)
                                        for y in SIG8[n - k:]))
        ck(f"K={k}: {n - k} + {2 * k} = {n + k} model calls",
           int(hi.shape[-1]) - 1 + int(lo.shape[-1]) - 1 == n + k)
    _, lo = H3._split_sigmas(SIG8, {}, 8, 1)
    ck("K=1 finish: 0.632 -> 0.316 -> 0", [round(float(x), 3) for x in lo] == [0.632, 0.316, 0.0],
       str([round(float(x), 3) for x in lo]))
    cache = {}
    a = H3._split_sigmas(SIG8, cache, 8, 2)
    ck("cached per (steps, K)", H3._split_sigmas(SIG8, cache, 8, 2) is a and ("split", 8, 2) in cache)

    print("K")
    ck("refine_steps 2 -> 2, 0 -> default", H3._split_k(2, 8) == 2
       and H3._split_k(0, 8) == H3.SPLIT_DEFAULT_K)
    ck("never all of the hop, never none", H3._split_k(40, 8) == 7 and H3._split_k(1, 2) == 1)


def handoff_checks(H3):
    """Stage 1 + DisableNoise stage 2 == one run, through core's own sampler."""
    print("hand-off: core's euler loop with KSAMPLER's scaling arithmetic")
    import comfy.samplers
    from comfy.k_diffusion import sampling as kd

    class CONSTSampling:
        def noise_scaling(self, sigma, noise, latent_image, max_denoise=False):
            sigma = sigma.view(-1, *([1] * (noise.ndim - 1)))
            return sigma * noise + (1.0 - sigma) * latent_image

        def inverse_noise_scaling(self, sigma, latent):
            return latent / (1.0 - sigma.view(-1, *([1] * (latent.ndim - 1))))

    ms = CONSTSampling()
    torch.manual_seed(0)
    x0_target = torch.randn(1, 4, 3, 8, 8)

    def denoiser(x, sigma, **kw):            # an x0 predictor that drifts with sigma
        return x0_target * (1.0 - 0.3 * float(sigma)) + 0.1 * x

    def run(sigmas, noise, latent):
        # KSAMPLER.sample's own arithmetic: noise_scaling in, sample, inverse out
        x = ms.noise_scaling(sigmas[0], noise, latent)
        x = kd.sample_euler(denoiser, x, sigmas, disable=True)
        return ms.inverse_noise_scaling(sigmas[-1], x)

    _, lo = H3._split_sigmas(SIG8, {}, 8, 2)
    hi = SIG8[:7]
    full = torch.cat([hi, lo[1:]])                 # the same sigmas, one run
    noise = torch.randn_like(x0_target)
    empty = torch.zeros_like(x0_target)
    one = run(full, noise, empty)
    mid = run(hi, noise, empty)                   # stage 1, real noise
    two = run(lo, torch.zeros_like(noise), mid)   # stage 2, DisableNoise
    ck("stage 1 + DisableNoise stage 2 == one uninterrupted run",
       torch.allclose(one, two, atol=1e-5), f"max |diff| {float((one - two).abs().max()):.2e}")
    ck("core's KSAMPLER does noise_scaling in / inverse out (what this mirrors)",
       "noise_scaling(sigmas[0]" in open(comfy.samplers.__file__, encoding="utf-8").read()
       and "inverse_noise_scaling(sigmas[-1]" in open(comfy.samplers.__file__, encoding="utf-8").read())


def wiring_checks():
    print("run() wiring")
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    at = src.find("hop_refine=split does not run with realism_boost")
    ck("refused beside realism_boost, before any model is shifted or sampled",
       0 < at < src.find('MiniMaxH3SigmaShift, "the sigma shift",'))
    ck("own key field: split, K, sampler, refine_model_fp, SPLIT_EXTEND",
       '"split", _split_k(refine_steps, hop_payload["steps"]),' in src
       and "str(refine_sampler), refine_model_fp, SPLIT_EXTEND]" in src)
    ck("the second-pass refine block is skipped under split",
       'refined_for_pin = None\n                if str(hop_refine) not in ("off", "split"):' in src)
    ck("blend ramp not parsed under split",
       'if str(hop_refine) not in ("off", "split") else [])' in src)
    ck("finish guider uses the hop's own conditioning (the pin)",
       "finish_cond = cond\n" in src
       and 'BasicGuider, "the split finish guider",\n                            model=split_model, conditioning=finish_cond' in src)
    ck("split_finish_aug: stock core key, set on the finish conditioning only",
       "finish_cond = [[emb, dict(d, minimax_visual_cond_noise_aug=finish_aug)]" in src)
    ck("split_finish_aug keyed only when moved off 0.999",
       'if finish_aug < 0.999:\n                        hop_payload["refine"].append(' in src)
    ck("refine_model is sigma-shifted like the hops for the finish",
       '"the sigma shift (split refine_model)",\n                    model=refine_model' in src)


def widget_checks(H3):
    print("split_finish_aug widget")
    import inspect
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}
    spec = opt.get("split_finish_aug")
    ck("exists, FLOAT, default 0.999 (= split unchanged)",
       spec is not None and spec[0] == "FLOAT" and spec[1].get("default") == 0.999)
    ck("the last widget (only the upsample_dit socket after it)",
       list(opt)[-2:] == ["split_finish_aug", "upsample_dit"])
    ck("run() defaults it to 0.999",
       inspect.signature(H3.HandTieClips.run).parameters["split_finish_aug"].default == 0.999)


def aug_core_checks():
    """What core does with the key, read from its source: every picture
    condition (keyframes AND reference images) gets the noise and the label."""
    print("core reads the key")
    import comfy.ldm.minimax.model as mm
    src = open(mm.__file__, encoding="utf-8").read()
    ck("content: aug * r + (1 - aug) * noise on every cond video latent",
       'aug = payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP)' in src
       and "r = aug * r + (1.0 - aug) * noise" in src)
    ck("label: cond AND ref_img rows get max(t_v, vis_aug)",
       '"cond": max(t_v, vis_aug), "ref_img": max(t_v, vis_aug)' in src)


def main():
    H3 = load_pack()
    widget_checks(H3)
    aug_core_checks()
    sigma_checks(H3)
    handoff_checks(H3)
    wiring_checks()
    print("\nall split refine checks passed" if not FAIL else f"\n{len(FAIL)} FAILED: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
