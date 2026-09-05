r"""Offline tests for master_audio_file -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_audio_lock.py

An off-by-one in the hop window costs 42 ms per hop and compounds. An
inverted noise_mask freezes the picture and generates a new voice. Neither
is visible to any other checker. This file is the table those two cannot
hide from.

The window function is pure and has no ComfyUI import. The widget and
cache-key claims need the node, so those run after a pack load.
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
sys.path.insert(0, HERE)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_lock():
    spec = importlib.util.spec_from_file_location(
        "htc_audio_lock", os.path.join(HERE, "audio_lock.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


def splice_checks(ck, H3, torch):
    """_splice_locked_audio against the object run() actually builds.

    Everything else in this file tables the window arithmetic or feeds
    `assert_mask_polarity` a pair of toy tensors. Neither touches the splice,
    and the splice is where this feature's two unrecoverable failures live: a
    mask on the wrong stream generates a voice over a frozen picture, and a
    video component that does not come back bit-identical is a silently
    different render.

    The shapes here are read off a real cached hop rather than invented --
    video [1, 24, T, H/16, W/16] and audio [1, 32, 2, T40] -- because a fixture
    that does not match production is not a test. That is not a hypothetical:
    check_latent_sidecar built its noise_mask as a plain tensor where the code
    makes a NestedTensor, passed, and the hop cache was silently off for every
    locked hop until somebody read a log line (DEVLOG 63).
    """
    from comfy.nested_tensor import NestedTensor

    print("\nsplice: the joint AV latent run() really builds")
    T_LAT, H_LAT, W_LAT, T40 = 57, 72, 40, 320
    torch.manual_seed(0)
    video = torch.randn(1, 24, T_LAT, H_LAT, W_LAT)
    audio = torch.randn(1, 32, 2, T40)
    latent = {"samples": NestedTensor((video, audio))}
    z = torch.randn(1, 32, 2, T40)

    out = H3._splice_locked_audio(latent, z)
    got = list(out["samples"].unbind())

    ck("the video component is bit-identical", torch.equal(got[0], video),
       "the sampler still has to denoise this")
    ck("the audio component is the locked slice", torch.equal(got[1], z))
    ck("the container survives as a NestedTensor",
       type(out["samples"]).__name__ == "NestedTensor")

    mask = out.get("noise_mask")
    ck("a noise_mask is set at all", mask is not None)
    ck("the mask is NESTED, one per stream",
       type(mask).__name__ == "NestedTensor",
       "a plain tensor here is what broke the latent sidecar")
    mv, ma = list(mask.unbind())
    # song_lock polarity: 1 denoises, 0 freezes. Inverted, the picture is held
    # and a NEW voice is generated over it -- which reads as "lip-sync died and
    # she stopped moving", and is the failure nobody would attribute to a mask.
    ck("ones on video: the picture is denoised",
       float(mv.min()) == 1.0 and float(mv.max()) == 1.0, str(tuple(mv.shape)))
    ck("zeros on audio: the take is frozen",
       float(ma.min()) == 0.0 and float(ma.max()) == 0.0, str(tuple(ma.shape)))
    ck("the video mask spans the video's own dims",
       tuple(mv.shape[2:]) == tuple(video.shape[2:]), str(tuple(mv.shape)))
    ck("the audio mask spans the audio's own dims",
       tuple(ma.shape[2:]) == tuple(audio.shape[2:]), str(tuple(ma.shape)))

    print("\nsplice: what it refuses")
    try:
        H3._splice_locked_audio({"samples": video}, z)
        ck("a video-only latent is refused", False, "it accepted one")
    except RuntimeError as e:
        ck("a video-only latent is refused", "joint AV latent" in str(e))
    try:
        H3._splice_locked_audio(latent, torch.randn(1, 32, 2, T40 - 1))
        ck("a length mismatch is refused", False, "it accepted one")
    except RuntimeError as e:
        ck("a length mismatch is refused, naming both numbers",
           str(T40 - 1) in str(e) and str(T40) in str(e), str(e)[:70])

    # The encoder can hand back an unbatched or differently-batched copy; the
    # splice normalises rather than raising, and the result must still be the
    # slice rather than a broadcast of the wrong thing.
    out2 = H3._splice_locked_audio(latent, torch.zeros(32, 2, T40))
    ck("an unbatched slice is accepted and batched",
       tuple(list(out2["samples"].unbind())[1].shape) == (1, 32, 2, T40))
    ck("and the video is still untouched",
       torch.equal(list(out2["samples"].unbind())[0], video))


def main():
    L = load_lock()

    print("window function vs the hand-computed table")
    # 8 s = 192 f, 0.9 s overlap = 22 f. 15 s = 362 f, same overlap.
    for (label, hop), (t0, t1) in sorted(L.WINDOW_TABLE.items()):
        hop_f = 192 if label == "8s" else 362
        got = L.hop_audio_window_s(hop, hop_f, 22, 24.0)
        ck(f"{label} hop {hop} starts at {t0:.6f}s",
           abs(got[0] - t0) < 1e-9, f"got {got[0]:.6f}")
        ck(f"{label} hop {hop} ends at {t1:.6f}s",
           abs(got[1] - t1) < 1e-9, f"got {got[1]:.6f}")

    print("0-based: hop 0 is t=0, not one stride in")
    t0, _ = L.hop_audio_window_s(0, 192, 22, 24.0)
    ck("hop 0 starts at 0", t0 == 0.0, f"got {t0}")
    t0, _ = L.hop_audio_window_s(1, 192, 22, 24.0)
    ck("hop 1 starts at 170/24 s (not 340/24)",
       abs(t0 - 170.0 / 24.0) < 1e-9, f"got {t0}")

    print("a 1-based function would fail these")
    # If someone writes hop_index * stride with hop_index starting at 1 for
    # "hop 1", hop 0 cannot exist and hop 1 starts at 7.08 s. The table
    # above already forbids that; this names the failure.
    ck("nine hops 0..8 cover the 64.67 s tester chain",
       abs(L.hop_audio_window_s(8, 192, 22, 24.0)[1] - 64.666666) < 1e-4)

    # No assertions for `recording_digest` here any more: the function is gone.
    # It was defined, tested, and called by nothing -- h3_ref_chain digests the
    # loaded waveform through _store.audio_digest instead. Its docstring argued
    # for size+mtime "because the take is minutes long and this runs on every
    # queue", a performance case for a function nobody ran, while the path that
    # does run hashes the samples. A checker asserting the behaviour of dead
    # code is the same fixture-versus-production problem as sections 63 and 68
    # in a third costume: not a fixture that fails to match production, but an
    # assertion about something that is not production at all.
    print("mask polarity: 1 on video, 0 on audio")
    v = torch.ones((1, 1, 4, 2, 2))
    a = torch.zeros((1, 1, 4, 2))
    ck("correct masks pass", L.assert_mask_polarity(v, a) is True)
    inverted_ok = False
    try:
        L.assert_mask_polarity(torch.zeros_like(v), torch.ones_like(a))
    except RuntimeError as e:
        inverted_ok = "stopped moving" in str(e) or "inverted" in str(e).lower() or "freeze" in str(e).lower()
    ck("inverted masks raise, and the message names the failure", inverted_ok)

    print("widget is last, default empty")
    load_pack()
    node = sys.modules["htcpack.h3_ref_chain"]
    names = []
    it = node.HandTieClips.INPUT_TYPES()
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in ("MODEL", "CLIP", "VAE", "IMAGE",
                                            "AUDIO", "LATENT", "CONDITIONING",
                                            "VIDEO"):
                continue
            if cfg.get("forceInput"):
                continue
            names.append(name)
    ck("master_audio_file is declared",
       "master_audio_file" in names, str(names[-5:]))
    ck("master_audio_file is after voice_3_end_s (appended, not inserted)",
       names.index("master_audio_file") > names.index("voice_3_end_s"))
    opt = (it.get("optional") or {})
    default = (opt.get("master_audio_file") or [None, {}])[1].get("default", "MISSING")
    ck("default is the empty string", default == "")

    print("xfade: locked hop 1 is [C,T], hop 2 trim is [B,C,T]")
    # GPU test 1 died here: hop 1 locked [0.00s-8.00s], then hop 2
    # `_xfade_audio(master_wav, trimmed["waveform"], sr)` with 2 vs 3 dims.
    xfade = node._xfade_audio
    left = torch.zeros(2, 32000)          # take slice, squeezed
    right = torch.zeros(1, 2, 28000)      # hop-2 AUDIO dict
    try:
        out = xfade(left, right, 32000)
        ok = True
        err = ""
    except Exception as e:
        ok, out, err = False, None, repr(e)
    ck("2D x 3D does not raise", ok, err)
    if ok:
        ck("output is batched [B,C,T]", out.dim() == 3 and out.shape[0] == 1
           and out.shape[1] == 2, str(tuple(out.shape)))
        ck("length is concat minus the 40 ms overlap",
           out.shape[-1] == 32000 + 28000 - int(32000 * 0.04),
           str(out.shape[-1]))
    else:
        ck("output is batched [B,C,T]", False, "xfade raised")
        ck("length is concat minus the 40 ms overlap", False, "xfade raised")
    src = inspect.getsource(node._xfade_audio)
    ck("xfade coerces both sides through _batch_wav",
       "_batch_wav(left)" in src and "_batch_wav(right)" in src)

    print()
    # The take-length pre-flight. A take shorter than the chain runs the last
    # hops past its end, fit_samples zero-pads them, and those hops come back
    # mute -- after the render is paid for. Asserted on the source, because
    # driving run() to the point where `locked` and `total_frames` both exist
    # needs a model.
    print(chr(10) + "take length is checked on the queue")
    with open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8") as _fh:
        src = _fh.read()
    ck("run() compares the take against the chain",
       "master_audio_file is" in src and "is not used" in src,
       "both the refusal and the unused-tail note")
    ck("the refusal names both durations",
       "but this chain" in src and "{total_frames}f" in src)
    ck("it raises rather than warning",
       "raise ValueError(" in src.split("master_audio_file is")[0][-400:],
       "a mute final hop nobody asked for is not a warning")

    splice_checks(ck, sys.modules["htcpack.h3_ref_chain"], torch)

    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
