r"""Offline tests for the audio refine pass -- no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_audio_refine.py

The pass is off by default and absent from the hop key until it is on.
The mask freezes the whole picture and opens the whole soundtrack.
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
    print("  %-4s %-62s %s" % ("ok" if cond else "FAIL", name, detail))
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
    import torch
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    opt = H3.HandTieClips.INPUT_TYPES()["optional"]
    sig = inspect.signature(H3.HandTieClips.run).parameters

    print("widgets, appended after swap_refs, off by default")
    names = []
    for section in ("required", "optional"):
        for name, spec in H3.HandTieClips.INPUT_TYPES()[section].items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in ("MODEL", "CLIP", "VAE", "IMAGE",
                                            "AUDIO", "LATENT", "CONDITIONING",
                                            "VIDEO"):
                continue
            if cfg.get("forceInput"):
                continue
            names.append(name)
    tail = ["audio_refine", "audio_denoise", "audio_steps",
            "audio_sampler", "audio_scheduler", "audio_seed_offset"]
    ck("the six audio widgets are the tail", names[-6:] == tail, str(names[-8:]))
    ck("they follow swap_refs", names[names.index("swap_refs") + 1] == "audio_refine")
    ck("audio_refine defaults off", opt["audio_refine"][1]["default"] == "off")
    ck("audio_denoise defaults to 0.5", opt["audio_denoise"][1]["default"] == 0.50)
    ck("audio_steps defaults to 5", opt["audio_steps"][1]["default"] == 5)
    ck("audio_model is a socket", "audio_model" in opt and "audio_model" not in names)
    ck("run() takes them and unique_id stays last",
       sig["audio_refine"].default == "off"
       and sig["audio_model"].default is None
       and list(sig)[-1] == "unique_id",
       str(list(sig)[-3:]))

    print("\nthe hop key carries the pass only when it is on")
    _key_at = src.index('hop_payload["audio_refine"]')
    ck("the key field is inside the on branch",
       'if str(audio_refine) == "on":' in src[_key_at - 400:_key_at])
    ck("off is refused with a locked soundtrack and with split",
       "audio_refine does not run with master_audio_file" in src
       and "audio_refine does not run with hop_refine=split" in src)

    print("\nthe mask freezes the picture and opens the soundtrack")
    video = torch.zeros(1, 4, 6, 2, 2)
    audio = torch.zeros(1, 8, 2, 10)
    vmask, amask = H3._audio_refine_masks(video, audio)
    ck("video mask is all frozen",
       vmask.shape == (1, 1, 6, 2, 2) and float(vmask.max()) == 0.0
       and float(vmask.min()) == 0.0, tuple(vmask.shape))
    ck("audio mask is all live",
       amask.shape == (1, 1, 2, 10) and float(amask.min()) == 1.0
       and float(amask.max()) == 1.0, tuple(amask.shape))
    _call = src.index("sampled = _audio_refine_sampled(")
    ck("the pass runs after the tail drop and before the decode",
       _call > src.index("_drop_latent_tail(sampled, _xtra)")
       and _call < src.index("imgs, audio = _decode_av"))
    ck("a moved picture is put back",
       "the frozen video is put back" in src and "_latents.rebuild(" in src)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
