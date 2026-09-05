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
- lut:         per-pixel piecewise-linear tone curve. Captures nonlinear drift.
  Long described here as overfitting on regenerated content; MEASURED on a
  3-hop chain it tracks `gain_bias` to within 0.02/255 at both seams, so on
  that evidence the overfitting is not observable and the warning was
  inherited rather than tested. Left in the list; treat the caution as
  unproven, not established.

Alignment: source's last `overlap` frames are paired with target's first
`overlap` frames, so the whole previous segment can be passed as `source` and it
auto-crops to the tail.
"""
from __future__ import annotations

import torch

TAG = "HTCTone"
MODES = ["off", "frame_shift", "gain_bias", "lut", "anchor"]

# --- anchor mode ------------------------------------------------------------
# The other three modes are seam-LOCAL: they measure the denoiser's tone bias on
# the overlap and cancel it, which makes each join exact. They do not touch the
# exposure falloff that happens *inside* a hop, and that part still compounds --
# hop 2 starts where hop 1 ended, darkens across its own 362 frames, hands that
# darker tail to hop 3, and so on. The 8x15s chain measured a luma slide of
# 46 -> 11 across hops 2-6 with the seam step already corrected.
#
# `anchor` is frame_shift plus a second stage that pulls each hop's overall look
# back toward HOP 1's, which is the only tone in the chain nobody drifted into.
# Two properties make it safe to stack on top of the seam correction:
#
#   * The pull is RAMPED from zero over the first `ANCHOR_RAMP` frames, so the
#     seam itself is untouched -- frame 0 of a hop still matches the previous
#     hop's last frame exactly. Without the ramp, a per-hop constant offset
#     would re-introduce precisely the step frame_shift just removed.
#   * It is CAPPED per hop (`ANCHOR_MAX_SHIFT`) and scaled by `strength`, so it
#     corrects a slide over several hops rather than snapping one hop back and
#     visibly pumping the exposure.
#
# A scene that is *meant* to get darker looks identical to drift from here, so
# a shot can opt out (`"tone": "free"`) or move the anchor to itself
# (`"tone": "rebase"`). See plan.py's `tone` field.
#
# The anchor measures and corrects in CIE Lab, not in RGB. It used to match a
# per-channel RGB mean, and a ten-run 9-hop study showed why that is not enough:
# across every relay chain the skin lost about a fifth of its colourfulness
# (chroma 33.6 -> 23.8) and darkened (L* 52.3 -> 45.0) while the hue angle did
# not move. That is a loss of chroma plus a loss of level -- exactly the two
# things an RGB mean cannot separate, because pulling three channel means toward
# a target restores the average colour of the frame and leaves it just as grey.
# Lab splits them: L* carries the level, a*/b* carry the colour, and the SPREAD
# of L* carries how much contrast the hop has left. All four are pulled.
#
# The cap and the ramp are unchanged in spirit. `ANCHOR_MAX_SHIFT` is still
# quoted in 0..1 RGB units because it is user-facing and has muscle memory
# behind it; it is converted to Lab units on use (0.06 -> 6 Lab units, which is
# about what 15/255 is worth around mid grey).
ANCHOR_STRENGTH = 0.35   # fraction of the measured gap closed per hop
ANCHOR_MAX_SHIFT = 0.06  # hard cap per hop, in 0..1 units (~15/255, ~6 Lab units)
ANCHOR_RAMP = 48         # frames to reach full correction (2 s at 24 fps)
ANCHOR_MAX_GAIN = 0.15   # hard cap on the L* spread correction, as a ratio
ANCHOR_STAT_FRAMES = 24  # frames sampled when measuring a hop (0 = all of them)
# Pixels converted to Lab at once. The conversion needs several full-size
# intermediates (X, Y, Z, then f() of each), so a 362-frame 1344x768 hop done in
# one go would ask for tens of gigabytes it does not need. Chunking holds the
# working set at roughly one 8 MPix slab whatever the hop length.
_LAB_CHUNK_PIXELS = 8_000_000

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


# --- sRGB <-> CIE Lab --------------------------------------------------------
# Standard sRGB D65 primaries and the CIE 1976 L*a*b* transfer, written out in
# torch rather than pulled from a library: this module deliberately imports
# nothing but torch so it can be tested without a running server, and OpenCV's
# 8-bit Lab (which quantises L* to 0..255) would throw away the precision the
# spread measurement needs.
_SRGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_XYZ_TO_SRGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)
_WHITE = (0.95047, 1.00000, 1.08883)  # D65
_LAB_EPS = 216.0 / 24389.0
_LAB_KAPPA = 24389.0 / 27.0


# f(XYZ/white) -> Lab and back are linear, so they fold into the same matmul
# form as the colour primaries. Written as matrices rather than as three
# expressions and a `torch.stack` because on a full 1344x768 frame the matmul is
# an order of magnitude quicker (0.003 s against 0.032 s measured), and this
# runs six times over every hop.
_F_TO_LAB = ((0.0, 116.0, 0.0), (500.0, -500.0, 0.0), (0.0, 200.0, -200.0))
_LAB_TO_F = ((1.0 / 116.0, 1.0 / 500.0, 0.0),
             (1.0 / 116.0, 0.0, 0.0),
             (1.0 / 116.0, 0.0, -1.0 / 200.0))
_LAB_BIAS = (-16.0, 0.0, 0.0)
_F_BIAS = (16.0 / 116.0, 16.0 / 116.0, 16.0 / 116.0)

_CACHE = {}


def _const(name, values, like):
    """A [3] or [3,3] constant on the right device and dtype, made once."""
    key = (name, like.device, like.dtype)
    hit = _CACHE.get(key)
    if hit is None:
        hit = torch.tensor(values, dtype=like.dtype, device=like.device)
        if hit.dim() == 2:
            hit = hit.transpose(0, 1).contiguous()  # row-major -> x @ m
        _CACHE[key] = hit
    return hit


def _srgb_to_lab(rgb):
    """[..., 3] sRGB in 0..1 -> [..., 3] Lab (L* 0..100, a*/b* roughly +-128)."""
    c = rgb.clamp(0.0, 1.0)
    lin = torch.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    t = (lin @ _const("srgb2xyz", _SRGB_TO_XYZ, lin)
         / _const("white", _WHITE, lin)).clamp_min(0.0)
    f = torch.where(t > _LAB_EPS,
                    t.clamp_min(1e-12) ** (1.0 / 3.0),
                    (_LAB_KAPPA * t + 16.0) / 116.0)
    return f @ _const("f2lab", _F_TO_LAB, f) + _const("labbias", _LAB_BIAS, f)


def _lab_to_srgb(lab):
    """Inverse of `_srgb_to_lab`, clamped back into 0..1."""
    f = lab @ _const("lab2f", _LAB_TO_F, lab) + _const("fbias", _F_BIAS, lab)
    f3 = f ** 3
    t = torch.where(f3 > _LAB_EPS, f3, (116.0 * f - 16.0) / _LAB_KAPPA)
    lin = ((t * _const("white", _WHITE, t))
           @ _const("xyz2srgb", _XYZ_TO_SRGB, t)).clamp_min(0.0)
    srgb = torch.where(lin <= 0.0031308,
                       12.92 * lin,
                       1.055 * lin.clamp_min(1e-12) ** (1.0 / 2.4) - 0.055)
    return srgb.clamp(0.0, 1.0)


def _lab_chunk(n_frames, h, w):
    """How many frames to convert at a time, given the frame size."""
    per = max(1, int(h) * int(w))
    return max(1, min(int(n_frames), int(_LAB_CHUNK_PIXELS // per) or 1))


def anchor_stats(imgs, sample=ANCHOR_STAT_FRAMES):
    """The tone reference for `anchor` mode: a hop's Lab moments.

    Four numbers, not a per-frame curve: the thing being corrected is the hop's
    overall look, and anything finer would start fighting the content.

        [0] mean L*  -- how light the hop is
        [1] mean a*  -- red/green
        [2] mean b*  -- yellow/blue
        [3] std  L*  -- how much contrast it has left

    a* and b* are the pair that a per-channel RGB mean could not hold on its
    own, and the L* spread is what hardens when a face bakes. Accumulated in
    frame chunks so a long hop never materialises its own Lab copy.

    Read from at most `sample` evenly spaced frames. These are four moments over
    tens of millions of pixels; a 362-frame hop and 24 frames drawn from it
    agree to well under a tenth of a Lab unit, and the Lab conversion is the
    expensive part of the whole mode. Pass `sample=0` to read every frame.

    Returns a [4] float32 CPU tensor, or None for an empty input.
    """
    if imgs is None or int(imgs.shape[0]) == 0:
        return None
    x = imgs
    n = int(x.shape[0])
    k = int(sample or 0)
    if 0 < k < n:
        x = x[torch.linspace(0, n - 1, k).round().long().unique()]
    n, h, w = int(x.shape[0]), int(x.shape[1]), int(x.shape[2])
    step = _lab_chunk(n, h, w)
    tot = torch.zeros(3, dtype=torch.float64)
    sq = torch.zeros((), dtype=torch.float64)
    count = 0
    for s in range(0, n, step):
        lab = _srgb_to_lab(x[s:s + step].float())
        flat = lab.reshape(-1, 3).double()
        tot += flat.sum(dim=0).cpu()
        sq += (flat[:, 0] * flat[:, 0]).sum().cpu()
        count += int(flat.shape[0])
    if count == 0:
        return None
    mean = tot / count
    var = max(0.0, float(sq / count - mean[0] * mean[0]))
    return torch.tensor([float(mean[0]), float(mean[1]), float(mean[2]),
                         var ** 0.5], dtype=torch.float32)


def anchor_note(stats):
    """One line describing an anchor reference, for the hop-1 log."""
    if stats is None:
        return "none"
    v = [float(t) for t in stats.reshape(-1).tolist()]
    return (f"L*{v[0]:.1f} a*{v[1]:+.1f} b*{v[2]:+.1f} "
            f"(chroma {(v[1] * v[1] + v[2] * v[2]) ** 0.5:.1f}, "
            f"L* spread {v[3]:.1f})")


def anchor_pull(target, ref_stats, strength=ANCHOR_STRENGTH,
                max_shift=ANCHOR_MAX_SHIFT, ramp=ANCHOR_RAMP,
                max_gain=ANCHOR_MAX_GAIN):
    """Ease `target`'s look back toward `ref_stats`. -> (images, note).

    Applied AFTER the seam correction, so `target` is already continuous with
    the previous hop. The correction ramps in from zero across the first `ramp`
    frames and holds after that, which is what keeps the seam exact: frame 0 is
    returned unchanged, and by the hop's tail the full (capped) correction is in
    effect. The next hop's seam correction then matches that corrected tail, so
    the offset carries forward on its own and never has to be tracked.

    Four corrections, each closing `strength` of its own measured gap:

      * L* mean   -- shifted, capped at `max_shift` in Lab units
      * a*, b*    -- shifted, same cap; this is the chroma the chain loses
      * L* spread -- scaled about the hop's own mean, capped at `max_gain`

    The spread is a gain rather than a shift, which is why it has its own cap:
    a ratio applied to a hop whose contrast has genuinely collapsed (a fade, a
    cut to a flat wall) would otherwise stretch it hard.

    Returns `(target, "")` unchanged when there is nothing worth doing, so the
    caller needs no branch.
    """
    if target is None or ref_stats is None:
        return target, ""
    strength = float(strength)
    if strength <= 0.0:
        return target, ""

    n = int(target.shape[0])
    if n == 0:
        return target, ""

    cur = anchor_stats(target)
    if cur is None:
        return target, ""
    ref = ref_stats.reshape(-1).float().cpu()

    cap = abs(float(max_shift)) * 100.0  # 0..1 RGB units -> Lab units
    want = (ref[:3] - cur[:3]) * strength
    # Whether the cap bound is worth saying out loud. A capped hop prints the
    # same number on every axis -- a 10-hop run logged "anchor r-0.0600 g-0.0600
    # b-0.0600" at a location cut -- which reads like a measurement of the scene
    # and is actually the limit, identical because it is one constant. The
    # correction is right; the line was not, so it names itself.
    capped = float((want.abs() - cap).max())
    want = want.clamp(-cap, cap)

    # Spread: ratio toward the reference, eased by the same strength.
    gcap = abs(float(max_gain))
    if float(cur[3]) > 1e-3 and float(ref[3]) > 1e-3:
        gain = 1.0 + (float(ref[3]) / float(cur[3]) - 1.0) * strength
        g_capped = abs(gain - 1.0) > gcap + 1e-9
        gain = min(1.0 + gcap, max(1.0 - gcap, gain))
    else:
        gain, g_capped = 1.0, False

    # Below ~0.1 Lab units the correction is not visible and not worth the copy.
    if float(want.abs().max()) < 0.1 and abs(gain - 1.0) < 1e-3:
        return target, ""

    # `ramp=0` means there is no join to protect -- a chain's first hop, or a
    # restart hop, which opens on the photograph rather than on a previous
    # frame. Those take the correction whole from frame 0; ramping them would
    # spend the opening seconds fading into the look.
    w = torch.ones(n, dtype=torch.float32)
    r = 0 if int(ramp) <= 0 else max(2, min(int(ramp), n))
    if n > 1 and r:
        w[:r] = torch.linspace(0.0, 1.0, r, dtype=torch.float32)

    h, wd = int(target.shape[1]), int(target.shape[2])
    step = _lab_chunk(n, h, wd)
    out = torch.empty_like(target)
    # The whole correction is diagonal in Lab -- L* scaled about the hop's own
    # mean then shifted, a* and b* shifted -- so it is one multiply and one add,
    # not three expressions and a stack.
    L0 = float(cur[0])
    lab_gain = torch.tensor([gain, 1.0, 1.0], dtype=torch.float32)
    lab_bias = torch.tensor([L0 * (1.0 - gain) + float(want[0]),
                             float(want[1]), float(want[2])],
                            dtype=torch.float32)
    for s in range(0, n, step):
        src = target[s:s + step].float()
        g_ = lab_gain.to(device=src.device)
        b_ = lab_bias.to(device=src.device)
        rgb = _lab_to_srgb(_srgb_to_lab(src) * g_ + b_)
        if s >= r:
            # Past the ramp every frame takes the correction whole, and the
            # blend below would be a full-size multiply-add for nothing. That is
            # most of a hop.
            out[s:s + step] = rgb.to(target.dtype)
            continue
        # Blend in RGB, not in Lab: the ramp exists to keep frame 0 EXACTLY as
        # it came in, and only a blend against the untouched source guarantees
        # that through a non-linear round trip.
        wt = w[s:s + step].to(device=src.device).view(-1, 1, 1, 1)
        out[s:s + step] = (src * (1.0 - wt) + rgb * wt).to(target.dtype)

    d = [float(t) for t in want.tolist()]
    gap = float((ref[0] - cur[0]))
    note = (f"anchor L*{d[0]:+.2f} a*{d[1]:+.2f} b*{d[2]:+.2f} "
            f"spread x{gain:.3f} (L* gap {gap:+.1f}, ramp {r}f)")
    if capped > 1e-6 or g_capped:
        note += (f" -- CAPPED at {cap:.1f} Lab / x{1 + gcap:.2f}, "
                 f"{max(capped, 0.0):.1f} Lab short of the pull it asked for. "
                 f"A gap this size is usually the shot genuinely changing, not "
                 f"drift; the cap is what stops it chasing that.")
    return out, note


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
    if mode == "anchor":
        # The seam half of `anchor` IS frame_shift. The chain-wide half lives in
        # `anchor_pull`, which the caller stages separately because it needs
        # hop 1's statistics -- state this function has never carried.
        mode = "frame_shift"
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
                        "frame_shift: per-frame additive shift. gain_bias: global "
                        "affine, robust. lut: tone curve, captures nonlinear drift -- "
                        "measured indistinguishable from gain_bias, not the overfitter "
                        "this once claimed. On a measured 3-hop chain frame_shift was "
                        "the WEAKEST correction of the four. "
                        "anchor behaves as frame_shift HERE -- its chain-wide half needs "
                        "hop 1's statistics, which only HandTieClips carries."
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
