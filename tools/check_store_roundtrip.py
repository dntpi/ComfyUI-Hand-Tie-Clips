r"""A rendered hop and its cache hit must be the same bytes -- no ComfyUI, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_store_roundtrip.py

Hand-tie keys every hop on digests of its previs windows. The first run cut
them from the decoder's float frames; a re-queue loaded the previs from the
cache, a 16-bit copy, so every digest moved and every hop of a finished native
run rendered again (2026-10-12, upsample switched off and re-queued at once).
`store.as_stored` hands the render the bytes a hit returns; this holds it to
`HopStore.get` through a real FFV1 write and read, and holds `run()` to calling
it after both puts that feed a hand-tie key.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_store():
    # store.py and its one relative import, latents.py, as a bare package: the
    # pack's __init__ imports ComfyUI, and this must run beside a render.
    pkg = types.ModuleType("htcmini")
    pkg.__path__ = [HERE]
    sys.modules["htcmini"] = pkg
    spec = importlib.util.spec_from_file_location(
        "htcmini.store", os.path.join(HERE, "store.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcmini.store"] = m
    spec.loader.exec_module(m)
    return m


def main():
    store = load_store()
    g = torch.Generator().manual_seed(7)
    # Out-of-range values too: the decoder overshoots 0..1 and the cache clamps.
    imgs = torch.rand(5, 24, 40, 3, generator=g) * 1.1 - 0.05
    wav = torch.randn(1, 2, 4800, generator=g, dtype=torch.float64)

    with tempfile.TemporaryDirectory() as root:
        hs = store.HopStore(root, budget_gb=1.0)
        hs.put("ab" * 8, imgs, wav, 48000, {"hop": 1})
        g_imgs, g_wav, _sr, _lat = hs.get("ab" * 8)

    raw = store.tensor_digest(imgs)
    hit = store.tensor_digest(g_imgs)
    ck("the decoder's floats digest apart from their cache hit", raw != hit,
       "(the bug: if these agree the check proves nothing)")

    s_imgs, s_wav = store.as_stored(imgs.clone(), wav)
    ck("as_stored frames == HopStore.get frames, byte for byte",
       store.tensor_digest(s_imgs) == hit)
    ck("as_stored sound == HopStore.get sound, byte for byte",
       store.audio_digest({"waveform": s_wav, "sample_rate": 48000})
       == store.audio_digest({"waveform": g_wav, "sample_rate": 48000}))
    again, _ = store.as_stored(s_imgs.clone(), s_wav)
    ck("as_stored is idempotent (a hit put again keeps its key)",
       store.tensor_digest(again) == hit)

    f32 = imgs.clone().contiguous()
    out, _ = store.as_stored(f32, wav)
    ck("cpu float32 frames are rewritten in place (no second previs copy)",
       out.data_ptr() == f32.data_ptr())
    half, _ = store.as_stored(imgs.to(torch.float16), wav)
    ck("other dtypes come back float32, as get() returns",
       half.dtype == torch.float32)

    with open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8") as fh:
        src = fh.read()
    p_put = src.find("hop_store.put(_p_key,")
    p_win = src.find("pics.append(_p_imgs[w:w + _ht.WINDOW]")
    p_as = src.find("_p_imgs, _p_wav = _store.as_stored(_p_imgs, _p_wav)", p_put)
    ck("run(): the previs goes on as stored, before its windows are cut",
       -1 not in (p_put, p_win, p_as) and p_put < p_as < p_win)
    h_put = src.find("hop_store.put(hop_key, imgs, wav, sr,")
    h_as = src.find("imgs, wav = _store.as_stored(imgs, wav)", h_put)
    h_tone = src.find("# Tone compensation, at the one point both the render and the", h_put)
    ck("run(): a rendered hop goes on as stored, before tone and the pin",
       -1 not in (h_put, h_as, h_tone) and h_put < h_as < h_tone)

    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        sys.exit(1)
    print("all ok")


if __name__ == "__main__":
    main()
