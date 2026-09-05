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

    print("digest: empty is None, so it cannot move a cache key")
    ck("empty string digests to None", L.recording_digest("") is None)
    ck("whitespace digests to None", L.recording_digest("  ") is None)

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

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
