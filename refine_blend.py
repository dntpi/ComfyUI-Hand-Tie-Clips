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

import math
import re

import torch
import torch.nn.functional as F

# Pixel frames carried by each H3 video latent step, cycling. The first step is
# the odd one: one frame, then fours.
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)

# xyzdist's published ramp. Raw across a 22 f overlap, crossing to fully
# refined over the 22 frames after it.
DEFAULT_RAMP = "0:0, 22:0, 44:1"

# How far past the pin the crossover runs, in pixel frames. The HOLD is
# overlap-coupled by definition -- it is the pin -- but nothing measured says
# the crossover should scale with it, so it stays the published 22 and only the
# hold moves. At overlap 22 that reproduces DEFAULT_RAMP exactly.
CROSS_FRAMES = 22

# `refine_blend=auto`. The literal 22 in DEFAULT_RAMP is this run's `overlap`,
# and the two were only ever equal because 0.9 s is the default overlap: at
# 0.2 s (5 f) the published ramp holds 17 frames raw that were refined and paid
# for, and at 1.6 s (39 f) it crosses to fully refined at f44 while the pin runs
# to f39, so f22-f39 ship refined inside the pinned head -- which is the exact
# seam cost the ramp exists to remove, reintroduced silently.
#
# Resolved to a literal BEFORE the hop key is built, so `auto` at a 22 f overlap
# keys byte-identically to a workflow carrying the published string and every
# cached hop still hits.
AUTO = "auto"

INTERP = ("linear", "smooth", "step")

# 2-D spatial Gaussian blur sigma (in latent pixels; 1 latent px = 16x16 image px
# on H3's VideoVAE) used to separate low-frequency scene/colour drift from
# high-frequency detail in `blend_video_detail`. `detail=0` restores the pure
# crossfade lerp bit-for-bit.
DETAIL_SIGMA_DEFAULT = 2.0

_KEY = re.compile(r"^\s*(-?\d+)\s*:\s*(-?\d*\.?\d+)\s*$")
_DETAIL_SUFFIX = re.compile(r"\|\s*detail\s*=\s*([^\s|]+)\s*$", re.I)


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


def auto_ramp(overlap):
    """The published ramp shape, re-derived for THIS run's overlap. -> str."""
    o = max(0, int(overlap))
    return f"0:0, {o}:0, {o + CROSS_FRAMES}:1"


def _split_detail(text):
    """Return `(ramp_part, detail_sigma_or_None)` after validating any `| detail=...` suffix."""
    s = str(text or "").strip()
    if "|" not in s:
        return s, None
    m = _DETAIL_SUFFIX.search(s)
    if not m:
        raise ValueError(
            f"refine_blend: invalid `|` suffix in {s!r}. Expected "
            "`| detail=<sigma>`, e.g. `'0:0, 22:0, 44:1 | detail=2.0'` "
            "(or `detail=0` for the plain crossfade).")
    try:
        val = float(m.group(1))
    except ValueError as e:
        raise ValueError(
            f"refine_blend: detail={m.group(1)!r} is not a number.") from e
    if val < 0.0:
        raise ValueError(
            f"refine_blend: detail={val:g} must be >= 0 (0 disables the high-pass filter).")
    head = s[:m.start()].strip()
    return head, val


def parse_detail(text, default=DETAIL_SIGMA_DEFAULT):
    """Extract the detail-blend Gaussian sigma from `text` (default `2.0`)."""
    _, val = _split_detail(text)
    return float(default if val is None else val)


def resolve(text, overlap):
    """`auto` -> the literal ramp for `overlap`. Anything else, untouched.

    Called before `parse` and before the hop key, so the key never sees the
    word and the console can print what auto meant. Preserves an explicit
    `| detail=...` suffix when attached to `auto`.
    """
    raw = str(text or "")
    s = raw.strip()
    if s.lower() == AUTO:
        return auto_ramp(overlap)
    if "|" in s:
        head, tail = s.split("|", 1)
        if head.strip().lower() == AUTO:
            return f"{auto_ramp(overlap)} | {tail.strip()}"
    return text


def parse(text):
    """`"0:0, 22:0, 44:1"` -> [(0.0, 0.0), (22.0, 0.0), (44.0, 1.0)].

    Frames on the left, weight on the right, weight 0 = raw and 1 = refined.
    Empty or whitespace returns [], which callers read as "no blend" -- the
    refine is taken whole, which is the pre-blend behaviour and a real setting.
    """
    text, _ = _split_detail(text)
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


def gauss2d_per_frame(x, sigma):
    """Separable 2-D spatial Gaussian over `(H, W)` per frame, in float32.

    No temporal blur: each latent step `t` is filtered strictly over `(H, W)`.
    Reflect padding is clamped when `H` or `W` is smaller than the ideal kernel
    radius `ceil(3 * sigma)` so tiny latents never raise.
    """
    sigma = float(sigma)
    if sigma <= 0.0 or x.dim() < 5:
        return x.float()
    b, c, t, h, w = x.shape
    radius = max(1, int(math.ceil(3.0 * sigma)))
    rad_h = min(radius, max(0, h - 1))
    rad_w = min(radius, max(0, w - 1))
    out = x.float().reshape(b * c * t, 1, h, w)
    if rad_w > 0:
        coords_w = torch.arange(-rad_w, rad_w + 1, device=out.device, dtype=torch.float32)
        kw = torch.exp(-0.5 * (coords_w / sigma) ** 2)
        kw = (kw / kw.sum()).view(1, 1, 1, -1)
        out = F.pad(out, (rad_w, rad_w, 0, 0), mode="reflect")
        out = F.conv2d(out, kw)
    if rad_h > 0:
        coords_h = torch.arange(-rad_h, rad_h + 1, device=out.device, dtype=torch.float32)
        kh = torch.exp(-0.5 * (coords_h / sigma) ** 2)
        kh = (kh / kh.sum()).view(1, 1, -1, 1)
        out = F.pad(out, (0, 0, rad_h, rad_h), mode="reflect")
        out = F.conv2d(out, kh)
    return out.reshape(b, c, t, h, w)


def high_rms_stats(x, sigma=DETAIL_SIGMA_DEFAULT):
    """Per-channel high-frequency RMS `(B, C, 1, 1, 1)` of a 5-D video latent."""
    sigma = float(sigma or 0.0)
    if sigma <= 0.0 or x is None or x.dim() < 5:
        return None
    xf = x.float()
    hi = xf - gauss2d_per_frame(xf, sigma)
    return hi.pow(2).mean(dim=(2, 3, 4), keepdim=True).add(1e-12).sqrt().detach().cpu()


def blend_video_detail(raw, refined, weights, sigma=DETAIL_SIGMA_DEFAULT,
                       cap_high=False, tone_pull=0.0, high_rms_ref=None):
    """Detail-only high-pass blend: `x0 + w(t) * (D - G_sigma(D))`, `(B, C, T, H, W)`.

    Keeps `raw` (`x0`)'s mid-band layout and background placement (`2..8 latent px`)
    while injecting `refined` (`x_r`)'s high-frequency spatial detail (`<= sigma`
    latent px) weighted by `w(t)`.
    - When `cap_high=True`, `refined`'s per-frame, per-channel high-frequency RMS
      is clamped so it never exceeds `raw`'s high-frequency RMS (or `high_rms_ref`,
      Hop 1's clean high-band RMS anchor, when provided — pulling single-DiT Turbo
      chains back to Hop 1's high-band energy even after a Motion-Context join step).
    - When `tone_pull > 0`, also pulls ultra-coarse regional exposure/colour
      (`> max(4*sigma, 8.0)` latent px = 128+ screen px, completely free of
      background edges) toward `refined`.
    - When `sigma <= 0`, falls back to `blend_video` bit-for-bit.
    """
    sigma = float(sigma)
    if sigma <= 0.0:
        return blend_video(raw, refined, weights)
    if raw.shape != refined.shape:
        raise ValueError(
            f"refine_blend: raw {tuple(raw.shape)} and refined "
            f"{tuple(refined.shape)} video latents differ; the refine pass "
            "must return the same shape it was given.")
    t = int(raw.shape[2])
    if len(weights) != t:
        raise ValueError(
            f"refine_blend: {len(weights)} weights for {t} latent steps.")
    raw_f = raw.to(device=refined.device, dtype=torch.float32)
    ref_f = refined.to(dtype=torch.float32)
    w = torch.as_tensor(weights, device=refined.device, dtype=torch.float32)
    w = w.view(1, 1, t, *([1] * (raw.dim() - 3)))
    if cap_high:
        raw_high = raw_f - gauss2d_per_frame(raw_f, sigma)
        ref_high = ref_f - gauss2d_per_frame(ref_f, sigma)
        rms_raw = raw_high.pow(2).mean(dim=(-2, -1), keepdim=True).add(1e-12).sqrt()
        rms_ref = ref_high.pow(2).mean(dim=(-2, -1), keepdim=True).add(1e-12).sqrt()
        ceil_rms = rms_raw
        if high_rms_ref is not None and high_rms_ref.shape[:2] == rms_raw.shape[:2]:
            ceil_rms = torch.minimum(
                rms_raw,
                high_rms_ref.to(device=rms_raw.device, dtype=rms_raw.dtype),
            )
        gain = torch.where(
            rms_raw > 1e-5,
            (ceil_rms / rms_ref).clamp(max=1.0),
            torch.ones_like(rms_raw),
        )
        d_high = ref_high * gain - raw_high
    else:
        d = ref_f - raw_f
        d_low = gauss2d_per_frame(d, sigma)
        d_high = d - d_low
    out = raw_f + w * d_high
    tone_pull = float(tone_pull or 0.0)
    if tone_pull > 0.0 and float(w.max().item()) > 0.0:
        coarse_sigma = max(sigma * 4.0, 8.0)
        d_coarse = gauss2d_per_frame(ref_f - raw_f, coarse_sigma)
        w_tone = 0.5 + 0.5 * w
        out = out + (tone_pull * w_tone) * d_coarse
    return out.to(dtype=refined.dtype)


def describe(weights, keys=None, interp="linear", detail_sigma=None):
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
    det = ""
    if detail_sigma is not None:
        det = (f", detail={float(detail_sigma):g}"
               if float(detail_sigma) > 0.0 else ", detail=0 (lerp)")
    return (f"{raw} raw / {mid} mixed / {full} refined of {len(weights)} "
            f"video steps [{interp}{where}{det}]")
