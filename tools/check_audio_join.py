r"""Joins keep the soundtrack the same length as the picture -- no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_audio_join.py

`_xfade_audio` overlaps 40 ms, so each continuation used to come out 40 ms
short. After nine joins the master ran 0.36 s early. `_xfade_keep` blends
inside the dropped head and drops that head, so the length is
len(left) + len(right) - head_n. A restart fades 5 ms on each side and
concatenates with no overlap.

Reverting the continuation join to `_xfade_audio` fails the length assert.
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)
sys.path.insert(0, HERE)

FAIL = []
SR = 44100
HOP_FRAMES = 192
HEAD_FRAMES = 22


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
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


def _tone(n, amp, channels=2):
    # A held tone. A hard cut between two of these is a step of 2*amp.
    return torch.full((1, channels, int(n)), float(amp))


def _seam_step(wav):
    d = (wav[..., 1:] - wav[..., :-1]).abs()
    return float(d.max()) if d.numel() else 0.0


def continuation_join(node, src, master, hop, head_n, sr):
    """The continuation the source actually calls.

    A revert to trim-then-`_xfade_audio` takes the short path, and the
    length assert below is what fails.
    """
    kept = "master_wav = _xfade_keep(" in src
    reverted = 'master_wav = _xfade_audio(master_wav, trimmed["waveform"], sr)' in src
    if kept and not reverted:
        return node._xfade_keep(master, node._batch_wav(hop), head_n, sr)
    trimmed = hop[..., head_n:]
    return node._xfade_audio(master, trimmed, sr)


def restart_join(node, src, master, hop, sr):
    """The restart the source actually calls. The old helper overlaps 40 ms."""
    if "_xfade_audio(master_wav, wav, sr)" in src:
        return node._xfade_audio(master, hop, sr)
    left = node._batch_wav(master)
    right = node._batch_wav(hop)
    m = min(int(sr * 0.005), int(left.shape[-1]), int(right.shape[-1]))
    if m < 1:
        return torch.cat([left, right], dim=-1)
    t = torch.linspace(0, 1, m, dtype=left.dtype, device=left.device)
    fade_out = torch.cos(t * math.pi / 2)
    fade_in = torch.sin(t * math.pi / 2)
    while fade_out.ndim < left.ndim:
        fade_out = fade_out.unsqueeze(0)
        fade_in = fade_in.unsqueeze(0)
    return torch.cat([
        left[..., :-m], left[..., -m:] * fade_out,
        right[..., :m] * fade_in, right[..., m:]], dim=-1)


def _chain(node, src, joins, amp=0.5):
    head_n = node._audio_samples(HEAD_FRAMES, SR)
    hop_n = node._audio_samples(HOP_FRAMES, SR)
    # Hop 1 arrives squeezed, the way a locked take does. Later hops are [B,C,T].
    master = _tone(hop_n, amp)[0]
    sign = 1.0
    for _j in range(joins):
        sign = -sign
        hop = _tone(hop_n, sign * amp)
        master = continuation_join(node, src, master, hop, head_n, SR)
    total_frames = HOP_FRAMES + joins * (HOP_FRAMES - HEAD_FRAMES)
    return master, total_frames


def main():
    node = load_pack()
    with open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8") as fh:
        src = fh.read()

    print("continuation joins keep the picture's length")
    for joins in (3, 9):
        master, total_frames = _chain(node, src, joins)
        expect = node._audio_samples(total_frames, SR)
        got = int(master.shape[-1])
        slack = joins  # ±1 sample per join, from rounding
        ck("%d joins: samples within %d of the picture" % (joins, slack),
           abs(got - expect) <= slack,
           "got %d expect %d frames %d" % (got, expect, total_frames))
        amp = 0.5
        step = _seam_step(master)
        ck("%d joins: no seam step above the input amplitude" % joins,
           step <= amp + 1e-4, "step %.4f amp %.4f" % (step, amp))

    print("one keep is exactly len(left) + len(right) - head")
    head_n = node._audio_samples(HEAD_FRAMES, SR)
    hop_n = node._audio_samples(HOP_FRAMES, SR)
    left = _tone(hop_n, 0.5)
    right = _tone(hop_n, -0.5)
    kept = node._xfade_keep(left, right, head_n, SR)
    ck("helper length is the arithmetic",
       int(kept.shape[-1]) == hop_n + hop_n - head_n,
       str(int(kept.shape[-1])))
    ck("helper output is batched",
       kept.dim() == 3 and tuple(kept.shape[:2]) == (1, 2),
       str(tuple(kept.shape)))
    # 2D left (squeezed hop 1) against a 3D hop, the GPU-test-1 rank pair.
    mixed = node._xfade_keep(left[0], right, head_n, SR)
    ck("2D x 3D does not change the length",
       int(mixed.shape[-1]) == hop_n + hop_n - head_n
       and mixed.dim() == 3, str(tuple(mixed.shape)))

    print("a restart concatenates")
    hop_a = _tone(hop_n, 0.5)
    hop_b = _tone(hop_n, -0.5)
    restarted = restart_join(node, src, hop_a, hop_b, SR)
    ck("a restart preserves length",
       int(restarted.shape[-1]) == hop_n + hop_n,
       str(int(restarted.shape[-1])))
    ck("a restart has no seam step above the input amplitude",
       _seam_step(restarted) <= 0.5 + 1e-4,
       "step %.4f" % _seam_step(restarted))
    # The dip reaches silence at the cut and comes back. A plain cat of
    # these two tones steps by 1.0, which is above the amplitude.
    ck("the restart dip is not a hard cut",
       _seam_step(restarted) < 0.5, "step %.4f" % _seam_step(restarted))

    print()
    if FAIL:
        print("%d FAIL: %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("audio joins ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
