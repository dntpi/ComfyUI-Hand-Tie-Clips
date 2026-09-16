r"""Offline tests for what the refine pass does to audio -- no server, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_refine_audio.py

The refine exists to fix texture, which is a picture property, and every run
that let it touch audio came back worse: a strained voice at the end of each
hop, and re-cooking hop 1 alone was enough to carry that through a whole chain
because hop 1's voice is what every later pin continues. So `refine_audio`
defaults to `freeze` and the audio stream comes out of a refine chain
bit-identical to a no-refine one.

That is a mask, and a mask has a polarity. `audio_lock.assert_mask_polarity`
pins the convention -- **1 = denoise, 0 = freeze** -- and getting it backwards
does not raise: it freezes the picture and re-generates the voice, which reads
as "lip-sync died and she stopped moving". Nothing in a log says so.

The second thing asserted here is precedence. Under `master_audio_file` the
hop already carries a noise mask locking the take, and the refine builds its
own. If the refine replaced that mask it would hand itself a live audio stream
to re-cook -- the locked take, denoised, silently. The hold ANDs instead.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys

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
    return m


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    import torch
    import comfy.nested_tensor as nt

    def latent(mask=None):
        """A joint AV latent shaped like an 8 s hop, 48 video steps."""
        v = torch.randn((1, 16, 48, 8, 8))
        a = torch.randn((1, 8, 2, 320))
        out = {"samples": nt.NestedTensor((v, a))}
        if mask is not None:
            out["noise_mask"] = mask
        return out

    def masks(lat):
        return list(lat["noise_mask"].unbind())[:2]

    freeze = H3._refine_head_freeze

    print("polarity -- 1 = denoise, 0 = freeze")
    out, steps, held = freeze(latent(), 22, freeze_head=True, freeze_audio=True)
    vm, am = masks(out)
    ck("22 f of pin is 7 latent video steps", steps == 7, str(steps))
    ck("audio is reported held", held is True)
    ck("the audio mask is all zeros -- the whole stream is frozen",
       float(am.abs().max()) == 0.0, f"mean {float(am.mean()):.4f}")
    ck("no boundary anywhere in the audio mask",
       float(am.min()) == float(am.max()),
       "a magnitude step between held and refined audio is an audible warble")
    ck("the audio mask covers the audio stream, not the video one",
       tuple(am.shape) == (1, 1, 2, 320), str(tuple(am.shape)))
    ck("the head of the video mask is frozen",
       float(vm[0, 0, 0].abs().max()) == 0.0)
    ck("every delivered video step is fully live",
       float(vm[:, :, steps:].min()) == 1.0,
       "step 7 is the first frame anyone sees")

    print("\nthe ramp lives INSIDE the head")
    # Ramping forward from the first delivered step puts a partial denoise on
    # the seam, which is a texture dent exactly where the join is.
    ramp = [float(vm[0, 0, k].mean()) for k in range(steps)]
    ck("the head ends on a ramp, not a cliff",
       0.0 < ramp[-1] < 1.0, f"steps 0-6 = {[round(x, 3) for x in ramp]}")
    ck("the ramp is the last REFINE_HEAD_RAMP steps of the head",
       sum(1 for x in ramp if 0.0 < x < 1.0) == H3.REFINE_HEAD_RAMP,
       f"REFINE_HEAD_RAMP = {H3.REFINE_HEAD_RAMP}")
    ck("at least one step stays fully frozen", ramp[0] == 0.0)
    ck("the ramp climbs", all(a <= b for a, b in zip(ramp, ramp[1:])))

    print("\nhop 1, and an overlap off the latent grid")
    out1, steps1, held1 = freeze(latent(), 22, freeze_head=False,
                                 freeze_audio=True)
    vm1, am1 = masks(out1)
    ck("hop 1 freezes no head", steps1 == 0)
    ck("...and still holds its audio", held1 is True
       and float(am1.abs().max()) == 0.0,
       "hop 1's voice is what every later hop's pin continues")
    ck("...leaving the picture entirely live", float(vm1.min()) == 1.0)
    off, steps_off, held_off = freeze(latent(), 23, freeze_head=True,
                                      freeze_audio=True)
    ck("an off-grid overlap freezes no head rather than guessing",
       steps_off == 0, "23 f is not a whole number of tokens")
    ck("...and the audio hold survives it", held_off is True)
    same, steps_n, held_n = freeze(latent(), 22, freeze_head=False,
                                   freeze_audio=False)
    ck("holding neither returns the latent untouched",
       steps_n == 0 and held_n is False and "noise_mask" not in same)

    print("\nthe master_audio_file lock still wins")
    # 1 on video, 0 on audio: the mask _splice_locked_audio installs.
    lock = nt.NestedTensor((torch.ones((1, 1, 48, 8, 8)),
                            torch.zeros((1, 1, 2, 320))))
    got, _, _ = freeze(latent(lock), 22, freeze_head=True, freeze_audio=False)
    lvm, lam = masks(got)
    ck("refine_audio=refine cannot unlock a locked take",
       float(lam.abs().max()) == 0.0,
       "the hold ANDs with the existing mask instead of replacing it")
    ck("the head freeze still applies under the lock",
       float(lvm[0, 0, 0].abs().max()) == 0.0)
    ck("and the delivered picture is still live",
       float(lvm[:, :, 7:].min()) == 1.0)

    class _Junk:
        def unbind(self):
            raise RuntimeError("not a mask")

    try:
        freeze(latent(_Junk()), 22)
        ck("an unreadable existing mask raises rather than dropping it", False)
    except RuntimeError as e:
        ck("an unreadable existing mask raises rather than dropping it",
           "refuse" in str(e).lower() or "refusing" in str(e).lower())

    print("\nthe blend is video only")
    rb = sys.modules["htcpack.refine_blend"]
    raw = latent()
    ref = {"samples": nt.NestedTensor((torch.ones((1, 16, 48, 8, 8)),
                                       torch.full((1, 8, 2, 320), 7.0)))}
    keys = rb.parse(rb.DEFAULT_RAMP)
    mixed = H3._refine_blend_latent(raw, ref, keys, "linear", hop_no=1)
    m_v, m_a = list(mixed["samples"].unbind())[:2]
    r_v, r_a = list(raw["samples"].unbind())[:2]
    ck("audio passes through the blend untouched",
       float((m_a - 7.0).abs().max()) == 0.0,
       "xyzdist's node ramps audio too; that is the warble, not a feature")
    ck("the pinned head ships the RAW video",
       float((m_v[:, :, :7] - r_v[:, :, :7]).abs().max()) == 0.0,
       "so the join continues a hop sampled the way it was")
    ck("the far end ships the refined video",
       float((m_v[:, :, 14:] - 1.0).abs().max()) == 0.0)
    ck("no keys means the refine ships whole",
       H3._refine_blend_latent(raw, ref, [], "linear") is ref)

    print("\nrun(): what is gated on what")
    src = inspect.getsource(H3.HandTieClips.run)
    ck("the audio hold is NOT gated on hop_is_start",
       '_do_aud = (str(refine_audio) == "freeze")' in src,
       "gating it with the head hold left hop 1 re-cooked, and carried")
    ck("the head hold IS gated on hop_is_start",
       '_do_head = (str(refine_head) == "freeze"' in src
       and "not hop_is_start" in src)
    ck("the freeze mask does not ride out on the result",
       '_ref["noise_mask"] = sampled["noise_mask"]' in src,
       "SamplerCustomAdvanced copies the input dict")
    ck("the blend is fed the RAW sample as its floor",
       "_refine_blend_latent(\n                            sampled, _ref" in src)
    ck("pin_only feeds the next hop, not the decode",
       "refined_for_pin = _latent_cpu(_ref)" in src
       and "this_sampled = (refined_for_pin" in src)
    ck("the refine runs before the decode",
       src.index("_refine_sampled(") < src.index("_decode_av(vae"))
    ck("the master-audio replacement still runs after it",
       src.index("_refine_sampled(") < src.index("_slice_take_audio(locked"))

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
