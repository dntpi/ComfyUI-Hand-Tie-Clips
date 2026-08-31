"""Reading and rebuilding H3 sampler latents, without importing ComfyUI.

Split out of `h3_ref_chain.py` so `tools/texture_probe.py` can measure a cached
latent -- the same reason `plan.py`, `refs.py` and `tone.py` carry no ComfyUI
imports. The node file cannot be imported without a running server, and an
instrument that has to guess at the container it is measuring is not an
instrument.

The guessing is the point. H3 hands back a `comfy.nested_tensor.NestedTensor`
-- the video and audio latents in one object -- and it is not a Tensor. It has
no `.std()`, and the attributes it *does* expose are traps: `.shape` returns
`tensors[0].shape`, i.e. the video component's shape while silently speaking
for both. A caller that trusts `.shape` sizes its work to the video and
broadcasts that onto the audio, and nothing raises. Reading the components is
the only honest way to touch the numbers.
"""
from __future__ import annotations

import torch


def parts(x):
    """The component tensors inside a sampler `samples`, as a flat list.

    Returns None for anything unrecognised, which callers treat as "leave this
    latent alone" -- never a silently-wrong single-component guess.
    """
    if isinstance(x, torch.Tensor):
        return [x]
    if getattr(x, "is_nested", False) and hasattr(x, "unbind"):
        got = list(x.unbind())
        if got and all(isinstance(t, torch.Tensor) for t in got):
            return got
    return None


def rebuild(x, new_parts):
    """Put conditioned components back into the container they came from."""
    if isinstance(x, torch.Tensor):
        return new_parts[0]
    return type(x)(new_parts)


def from_dict(lat):
    """`parts()` of a `{"samples": ...}` latent dict. -> list or None."""
    if not isinstance(lat, dict) or "samples" not in lat:
        return None
    return parts(lat["samples"])


def _gauss1d(sigma, device, dtype):
    r = max(1, int(round(3.0 * float(sigma))))
    x = torch.arange(-r, r + 1, dtype=torch.float32, device=device)
    k = torch.exp(-(x * x) / (2.0 * float(sigma) ** 2))
    return (k / k.sum()).to(dtype)


def band_split(t, sigma=2.0):
    """Separable Gaussian low/high split over the last two dims. -> (lo, hi).

    Returns None when the tensor has no spatial extent to speak of -- an audio
    component is a different object and blurring across it means nothing.

    Replicate padding, not reflect: a latent's spatial dims are small (a
    736x1280 frame is 46x80 here) and reflect needs the pad to be smaller than
    the dimension, which stops being true for a large sigma on a small latent.
    """
    if t.dim() < 2 or t.shape[-1] < 8 or t.shape[-2] < 8:
        return None
    import torch.nn.functional as F  # noqa: PLC0415
    h, w = int(t.shape[-2]), int(t.shape[-1])
    flat = t.reshape(-1, 1, h, w)
    k = _gauss1d(sigma, t.device, t.dtype)
    pad = k.numel() // 2
    lo = F.conv2d(F.pad(flat, (pad, pad, 0, 0), mode="replicate"),
                  k.view(1, 1, 1, -1))
    lo = F.conv2d(F.pad(lo, (0, 0, pad, pad), mode="replicate"),
                  k.view(1, 1, -1, 1))
    lo = lo.reshape(t.shape)
    return lo, t - lo


def band_ratio(t, sigma=2.0):
    """High-band sigma as a fraction of total sigma. -> float, or None.

    This is the statistic the texture ratchet actually moves. Measured across a
    3-hop chain it went 0.3643 -> 0.3673 -> 0.3702, monotone, while the total
    sigma it is normalised by FELL 1.2% -- which is why matching total sigma
    cannot see this and, worse, corrects the wrong way.
    """
    got = band_split(t, sigma)
    if got is None:
        return None
    tot = float(t.float().std())
    if not tot or tot != tot:
        return None
    return float(got[1].float().std()) / tot


def match_band(t, target_ratio, sigma=2.0, clamp=(0.5, 2.0)):
    """Rescale t's high band so its high-band fraction becomes `target_ratio`.

    -> (tensor, k), or (t, None) when the tensor has no bands to match.

    Only the high half is scaled, so the low-frequency structure that carries
    the scene is bit-identical and the correction is one scalar. It cannot blur,
    sharpen unevenly, or invent detail; the worst it can do is get the gain
    wrong, which is why `clamp` exists.

    Note the fraction is against the tensor's own sigma, so restoring the ratio
    does move total sigma a little. That is deliberate: the ratio is the drifting
    statistic and sigma is the one that lies.
    """
    got = band_split(t, sigma)
    if got is None or not target_ratio:
        return t, None
    lo, hi = got
    cur_hi = float(hi.float().std())
    if not cur_hi or cur_hi != cur_hi:
        return t, None

    # The naive `k = target * sigma / hi_sigma` is wrong, and quietly so:
    # scaling the high band changes the sigma it is a fraction OF, so the
    # target moves as you apply it. Measured, it undershot by 5% on a
    # latent-shaped tensor -- a correction that silently does most, but not
    # all, of its job is the worst kind to ship.
    #
    # First guess solves the fixed point assuming lo and hi are orthogonal:
    # k*H / sqrt(L^2 + k^2*H^2) = r, so k = r*L / (H*sqrt(1 - r^2)) ...
    r = min(float(target_ratio), 0.999)
    lo_sd = float(lo.float().std())
    k = (r * lo_sd) / (cur_hi * max(1e-6, (1.0 - r * r) ** 0.5))
    # ... then refine against the statistic as actually measured, because a
    # difference of Gaussians is not an exact orthogonal projection. Two or
    # three passes converge, and a latent is small enough that this is free.
    for _ in range(4):
        k = min(max(k, clamp[0]), clamp[1])
        got_r = band_ratio(lo + hi * k, sigma)
        if not got_r:
            break
        if abs(got_r - r) <= 1e-4 * max(r, 1e-6):
            break
        k *= r / got_r
    k = min(max(k, clamp[0]), clamp[1])
    return lo + hi * k, k
