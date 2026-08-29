"""Tone compensation for chained H3 segments.

The H3 denoiser applies a tone bias to each generated segment, which shows up as
a brightness step at the seam between hops. This module estimates that bias on
the overlap -- the segment's first frames against the source frames they
reconstruct -- and undoes it across the whole segment.

The estimator, and the reasoning behind the three modes, is ported from
`rkfg/ComfyUI-MiniMaxH3-ToneCompensate` (MIT). This pack is MIT too, so the code
travels; the credit does not travel by itself, hence this paragraph. Changes
from upstream: the overlap default is our 22 frames rather than his 48, `"off"`
is a real mode so callers need no branch, and the math is split from the node so
it can be tested without a running server (same reason `plan.py` and `refs.py`
have no ComfyUI imports).

The three modes, most to least specific:

- frame_shift: per-frame per-channel additive shift. The target's first frames
  are the model's regeneration of the source (similar content, not a pixel-wise
  transform), so the bias is best captured as a per-frame shift of the mean.
  Matches the overlap exactly and applies the last overlap frame's shift to the
  continuation (no seam).
- gain_bias:   global per-channel affine  s = A*g + C. Robust, extrapolates
  cleanly; good when the drift is a roughly uniform shift/compression.
- lut:         per-pixel piecewise-linear tone curve. Captures nonlinear drift;
  flexible, but overfits when the target is regenerated content (pixels differ
  from the source).

Alignment: source's last `overlap` frames are paired with target's first
`overlap` frames, so the whole previous segment can be passed as `source` and it
auto-crops to the tail.
"""
from __future__ import annotations

import torch

TAG = "HTCTone"
MODES = ["off", "frame_shift", "gain_bias", "lut"]

_TABLE = 4096  # dense LUT resolution used when applying the lut mode

# Our native overlap: 0.9 s at 24 fps, the H3 continuation length. Upstream
# defaults to 48 (2 s) because that is what his workflow pins with.
DEFAULT_OVERLAP = 22


def _fit_affine(src, tgt):
    """Regress source on generated per channel: s = A*g + C. Return (A, C) as [1,1,1,C]."""
    c_out = src.shape[-1]
    gain = torch.ones(1, 1, 1, c_out, dtype=torch.float32, device=src.device)
    bias = torch.zeros(1, 1, 1, c_out, dtype=torch.float32, device=src.device)
    for c in range(c_out):
        s = src[..., c].reshape(-1).float()
        g = tgt[..., c].reshape(-1).float()
        gm = g.mean()
        sm = s.mean()
        dg = g - gm
        den = (dg * dg).sum()
        if den < 1e-12:
            A, C = 1.0, float(sm - gm)
        else:
            A = float((dg * (s - sm)).sum() / den)
            C = float(sm - A * gm)
        if abs(A) < 1e-6:
            A = 1.0
        gain[0, 0, 0, c] = A
        bias[0, 0, 0, c] = C
    return gain, bias


def _monotone(ys):
    """Make ys non-decreasing (guards against bin-mean inversions from noise)."""
    v = ys.tolist()
    best = v[0]
    out = []
    for y in v:
        if y > best:
            best = y
        out.append(best)
    return torch.tensor(out, dtype=torch.float32, device=ys.device)


def _lut_control(s, g, bins):
    """Build per-channel control points from paired pixels.

    For each generated-value bin that actually occurs, store (mean generated,
    mean source). Using the means as x keeps boundary segments exact -- bin
    centres would skew the outer slopes. Returns sorted (xs, ys).
    """
    dev = g.device
    idx = torch.clamp(torch.floor(g * bins), 0, bins - 1).long()
    sums_s = torch.zeros(bins, dtype=torch.float32, device=dev)
    sums_g = torch.zeros(bins, dtype=torch.float32, device=dev)
    counts = torch.zeros(bins, dtype=torch.float32, device=dev)
    sums_s.index_add_(0, idx, s)
    sums_g.index_add_(0, idx, g)
    counts.index_add_(0, idx, torch.ones_like(idx, dtype=torch.float32))
    nz = counts > 0
    xs = (sums_g / counts)[nz]
    ys = (sums_s / counts)[nz]
    return xs, _monotone(ys)


def _linfit(x, y):
    """Least-squares line y = slope*x + intercept. Return (slope, intercept)."""
    xm, ym = x.mean(), y.mean()
    dx = x - xm
    den = (dx * dx).sum()
    if den < 1e-12:
        return 0.0, ym.item()
    slope = float((dx * (y - ym)).sum() / den)
    return slope, float(ym - slope * xm)


def _pwl(query, xs, ys):
    """Piecewise-linear evaluation of the (xs, ys) control points at query.

    The interior is interpolated; the ends extrapolate with a robust slope
    (least-squares on the outermost K points) so a single noisy boundary bin
    cannot skew the extrapolation.
    """
    n = xs.numel()
    if n == 1:
        return torch.full_like(query, ys.item())
    k = min(5, n)
    ls, lb = _linfit(xs[:k], ys[:k])
    rs, rb = _linfit(xs[-k:], ys[-k:])
    i = torch.clamp(torch.searchsorted(xs, query), 1, n - 1)
    xl, xr = xs[i - 1], xs[i]
    yl, yr = ys[i - 1], ys[i]
    out = yl + (yr - yl) * (query - xl) / (xr - xl)
    left, right = query < xs[0], query > xs[-1]
    out = torch.where(left, ls * query + lb, out)
    out = torch.where(right, rs * query + rb, out)
    return out


def _apply_lut(x, xs, ys, table=_TABLE):
    """Apply a per-channel control LUT to x (values in [0,1])."""
    dense = _pwl(torch.linspace(0, 1, table, device=x.device), xs, ys)
    idx = torch.clamp(torch.floor(x * table), 0, table - 1).long()
    return dense[idx]


def compensate(source, target, mode, overlap=DEFAULT_OVERLAP, lut_bins=64):
    """Correct `target`'s tone to match `source`. -> (images, note).

    `source` is the previous segment (or just its tail); `target` is the whole
    generated segment. Both float [N,H,W,3] in 0..1. Returns the corrected
    target and a short human-readable note for the log, or (target, "") when
    there is nothing to do.

    `mode="off"` returns `target` untouched, so a caller can pass the widget
    value straight through without branching on it. Anything unrecognised is
    treated the same way rather than raising: a bad mode should not lose a
    render that has already been sampled.
    """
    if mode is None or str(mode) == "off" or str(mode) not in MODES:
        return target, ""
    if source is None or target is None:
        return target, ""

    src = source.float()
    tgt = target.float()
    n = min(int(overlap), int(src.shape[0]), int(tgt.shape[0]))
    if n <= 0:
        return target, ""
    fit_src = src[-n:]
    fit_tgt = tgt[:n]

    mode = str(mode)
    if mode == "frame_shift":
        # Per-frame per-channel drift (mean target - mean source), applied
        # per-frame on the overlap and as the last overlap frame's value on the
        # continuation -- which is what makes the seam itself exact.
        drift = fit_tgt.mean(dim=(1, 2), keepdim=True) - fit_src.mean(dim=(1, 2), keepdim=True)
        out = tgt.clone()
        out[:n] = out[:n] - drift
        out[n:] = out[n:] - drift[-1]
        d = drift[-1].reshape(-1)
        note = ("frame_shift " + " ".join(f"{c}{v:+.4f}" for c, v in zip("rgb", d.tolist())))
    elif mode == "gain_bias":
        gain, bias = _fit_affine(fit_src, fit_tgt)
        out = gain * tgt + bias
        g = gain.reshape(-1).tolist()
        b = bias.reshape(-1).tolist()
        note = ("gain_bias " + " ".join(f"{c}x{gv:.4f}{bv:+.4f}"
                                        for c, gv, bv in zip("rgb", g, b)))
    else:  # lut
        out = torch.empty_like(tgt)
        for c in range(tgt.shape[-1]):
            xs, ys = _lut_control(fit_src[..., c].reshape(-1),
                                  fit_tgt[..., c].reshape(-1), int(lut_bins))
            out[..., c] = _apply_lut(tgt[..., c], xs, ys)
        before = float(tgt.mean())
        after = float(out.mean())
        note = f"lut mean {before:.4f} -> {after:.4f} ({int(lut_bins)} bins)"

    out = out.clamp_(0.0, 1.0).to(target.dtype)
    return out, note


class HTCToneCompensate:
    """Undo the denoiser's tone bias on a generated H3 segment.

    Wire it between two hand-chained H3 generations: `source` is the previous
    segment, `target` the one to correct.

    Note this cannot do the same job downstream of `HandTieClips`. That node joins
    its hops internally and drops each hop's first `overlap` frames at the seam,
    so the regenerated copies this estimator needs no longer exist by the time
    images leave it. Use the chain node's own `tone_compensate` widget for that;
    this node is for hand-built chains, and for A/B-ing the correction.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source": ("IMAGE", {"tooltip": "The previous segment. The whole thing is fine -- only its last `overlap` frames are read."}),
                "target": ("IMAGE", {"tooltip": "The generated segment to correct."}),
                "mode": (MODES, {
                    "default": "frame_shift",
                    "tooltip": (
                        "frame_shift: per-frame additive shift; best when the target is "
                        "regenerated content, which it is here. gain_bias: global affine, "
                        "robust. lut: tone curve, captures nonlinear drift but overfits."
                    ),
                }),
                "overlap": ("INT", {
                    "default": DEFAULT_OVERLAP, "min": 1, "max": 4096,
                    "tooltip": (
                        "Number of keyframe frames: last `overlap` of source vs first "
                        "`overlap` of target. Must equal the keyframe count used for "
                        "generation (22 = 0.9 s @ 24 fps), NOT the whole segment."
                    ),
                }),
                "lut_bins": ("INT", {
                    "default": 64, "min": 16, "max": 512,
                    "tooltip": "Aggregation bins for lut mode. Ignored otherwise.",
                }),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"

    def run(self, source, target, mode, overlap, lut_bins):
        out, note = compensate(source, target, mode, overlap, lut_bins)
        if note:
            print(f"[{TAG}] {note}", flush=True)
        return (out,)




# -- pre-rename ids ----------------------------------------------------------
# A plain alias in NODE_CLASS_MAPPINGS keeps old workflows loading, but it also
# lists the node a second time in search: ComfyUI falls back to the mapping key
# when NODE_DISPLAY_NAME_MAPPINGS has no entry. Subclassing and setting
# DEPRECATED gets both -- server.py publishes `deprecated: True`, and the
# frontend's `Comfy.Node.ShowDeprecated` (off by default) hides it from search
# while leaving it fully functional in workflows that name it.


class _LegacyH3ToneCompensate(HTCToneCompensate):
    DEPRECATED = True


NODE_CLASS_MAPPINGS = {
    "HTCToneCompensate": HTCToneCompensate,
    "H3ToneCompensate": _LegacyH3ToneCompensate,
}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCToneCompensate": "H3 Tone Compensate"}
