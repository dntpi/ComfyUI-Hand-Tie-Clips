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
