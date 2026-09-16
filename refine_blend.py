"""Keyframed blend between the raw and the refined copy of one hop's latent.

The refine pass re-samples a finished hop at low denoise. Taken whole, its
output is what ships and what the next hop continues -- and the head of the hop
is exactly the region that has to continue the PREVIOUS hop, which was never
put through a second sampler. That mismatch is the measured seam cost: on an
8x8-pooled geometry difference a stock chain's joins sit at 1.00x its own
ordinary motion and a fully refined chain puts them at 2.4-2.8x.

xyzdist's `H3BlendLatents` answers it by not taking the refine whole: the
delivered latent is a per-frame lerp from the raw sample to the refined one,
`0:0, 22:0, 44:1` -- raw across the pinned overlap, crossing to fully refined
22 frames later. This module is that ramp, with one bug fixed.

**His node derives the ramp position from a `duration` widget.** It computes
`pixel_total_frames = int(round(duration * fps))` and then
`ratio = total_frames / pixel_total_frames`, so the widget is what says where
frame 22 lives on the latent grid. Set it to 5 on an 8 s render and every
keyframe lands in the wrong place, silently -- no error, just a ramp somewhere
else. Nothing needs to be typed: the latent knows how many steps it has and the
caller knows how many frames the hop is.

It is also not a uniform ratio. H3's video tokens cover `(1, 4, 4, 4, 4)`
frames on a repeating cycle, so 22 frames take 7 latent steps and 44 take 13 and
a quarter, not `22 * T/length` of the way along. `step_for_frame` walks the real
cycle -- the same walk the backup's `_video_steps_for_frames` does, which maps
5/22/39/56 to 2/7/12/17 -- so `0:0, 22:0, 44:1` comes out as latent steps
7.00 -> 13.25, which is the 7-13 band the published run was ramping over.

No ComfyUI imports, for the reason `latents.py`, `plan.py` and `tone.py` have
none: the arithmetic here decides which frames ship raw, and it should be
checkable without a GPU, a server, or a model on disk.
"""
from __future__ import annotations

import re

import torch

# Pixel frames carried by each H3 video latent step, cycling. The first step is
# the odd one: one frame, then fours.
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# xyzdist's published ramp. Raw across a 22 f overlap, crossing to fully
# refined over the 22 frames after it.
DEFAULT_RAMP = "0:0, 22:0, 44:1"

INTERP = ("linear", "smooth", "step")

_KEY = re.compile(r"^\s*(-?\d+)\s*:\s*(-?\d*\.?\d+)\s*$")


def step_for_frame(n):
    """Where pixel frame `n` sits on the latent grid, as a float step index.

    Fractional on purpose. Frame 44 lands a quarter of the way into step 13,
    and rounding it to a whole step is a token of slop in a ramp whose whole job
    is to put the crossover in a particular place.
    """
    n = float(n)
    if n <= 0.0:
        return 0.0
    k, covered = 0, 0.0
    while k < 4096:
        w = float(FRAME_PER_TOKEN[k % len(FRAME_PER_TOKEN)])
        if covered + w >= n:
            return float(k) + (n - covered) / w
        covered += w
        k += 1
    return float(k)


def whole_steps_for_frames(n):
    """Latent video steps covering EXACTLY n pixel frames. -> int, or None.

    None when n does not land on a token boundary: 5/22/39/56 -> 2/7/12/17, and
    23 -> None. The caller freezing a pinned head needs a whole number of steps
    or it would freeze a token that is half delivered picture, so "no answer" is
    a real answer here and not an error.
    """
    n = int(n)
    if n <= 0:
        return None
    s = step_for_frame(n)
    return int(round(s)) if abs(s - round(s)) < 1e-9 else None


def parse(text):
    """`"0:0, 22:0, 44:1"` -> [(0.0, 0.0), (22.0, 0.0), (44.0, 1.0)].

    Frames on the left, weight on the right, weight 0 = raw and 1 = refined.
    Empty or whitespace returns [], which callers read as "no blend" -- the
    refine is taken whole, which is the pre-blend behaviour and a real setting.
    """
    text = str(text or "").strip()
    if not text:
        return []
    keys = []
    for chunk in re.split(r"[,\n;]+", text):
        if not chunk.strip():
            continue
        m = _KEY.match(chunk)
        if not m:
            raise ValueError(
                f"refine_blend: {chunk.strip()!r} is not a keyframe. Write "
                f"frame:weight pairs, e.g. {DEFAULT_RAMP!r} -- frame number, "
                "colon, then 0 for the raw sample or 1 for the refined one.")
        f, w = float(m.group(1)), float(m.group(2))
        if f < 0:
            raise ValueError(f"refine_blend: frame {f:g} is before the hop.")
        if not (0.0 <= w <= 1.0):
            raise ValueError(
                f"refine_blend: weight {w:g} at frame {f:g} is outside 0..1. "
                "0 is the raw sample and 1 is the refined one; there is "
                "nothing to extrapolate toward.")
        keys.append((f, w))
    keys.sort(key=lambda kv: kv[0])
    return keys


def step_weights(n_steps, keys, interp="linear"):
    """One weight per latent video step: 0 keeps the raw sample, 1 the refined.

    Outside the keyframes the curve is held flat at the nearest one rather than
    extrapolated. A ramp that ends `44:1` means "fully refined from there on",
    not "keep climbing past 1".
    """
    n_steps = int(n_steps)
    if n_steps <= 0:
        return []
    if not keys:
        # No ramp is the refine taken whole, not the refine discarded.
        return [1.0] * n_steps
    interp = str(interp or "linear")
    if interp not in INTERP:
        raise ValueError(f"refine_blend: unknown interpolation {interp!r}; "
                         f"expected one of {', '.join(INTERP)}.")

    xs = [step_for_frame(f) for f, _ in keys]
    ys = [float(w) for _, w in keys]
    out = []
    for j in range(n_steps):
        x = float(j)
        if x <= xs[0]:
            out.append(ys[0])
            continue
        if x >= xs[-1]:
            out.append(ys[-1])
            continue
        i = 0
        while i + 1 < len(xs) and xs[i + 1] < x:
            i += 1
        x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
        if interp == "step" or x1 <= x0:
            out.append(y0)
            continue
        t = (x - x0) / (x1 - x0)
        if interp == "smooth":
            t = t * t * (3.0 - 2.0 * t)
        out.append(y0 + (y1 - y0) * t)
    return out


def blend_video(raw, refined, weights):
    """Per-step lerp of two video latents, `(B, C, T, H, W)`.

    A lerp and not a mask: both inputs are finished samples, so there is no
    sampler left to hand a mask to. That is also why it can be checked on
    numbers -- nothing here calls a model.
    """
    if raw.shape != refined.shape:
        raise ValueError(
            f"refine_blend: raw {tuple(raw.shape)} and refined "
            f"{tuple(refined.shape)} video latents differ; the refine pass "
            "must return the same shape it was given.")
    t = int(raw.shape[2])
    if len(weights) != t:
        raise ValueError(
            f"refine_blend: {len(weights)} weights for {t} latent steps.")
    w = torch.as_tensor(weights, device=refined.device, dtype=refined.dtype)
    w = w.view(1, 1, t, *([1] * (raw.dim() - 3)))
    return raw.to(device=refined.device, dtype=refined.dtype) * (1.0 - w) \
        + refined * w


def describe(weights, keys=None, interp="linear"):
    """One log line. Says where the ramp actually landed, not what was typed."""
    if not weights:
        return "no video steps"
    raw = sum(1 for w in weights if w <= 0.0)
    full = sum(1 for w in weights if w >= 1.0)
    mid = len(weights) - raw - full
    where = ""
    if keys:
        span = [step_for_frame(f) for f, _ in keys]
        where = (f", keys f{keys[0][0]:g}-{keys[-1][0]:g} = steps "
                 f"{span[0]:.2f}-{span[-1]:.2f}")
    return (f"{raw} raw / {mid} mixed / {full} refined of {len(weights)} "
            f"video steps [{interp}{where}]")
