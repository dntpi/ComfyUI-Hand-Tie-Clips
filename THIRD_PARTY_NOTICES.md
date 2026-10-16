# Third-party notices

Hand Tie Clips is MIT (see `LICENSE`). Two files in it carry other people's
work, and this document exists so that is impossible to miss.

## `h3_cache.py` — MiniMax-H3 block-stack residual cache

**Original author: [silveroxides](https://github.com/silveroxides).**

The cache is silveroxides' work from
[`ComfyUI-UtilsCollection`](https://github.com/silveroxides/ComfyUI-UtilsCollection),
`helpers/patcher_helpers.py` — `MiniMaxH3Cache`, `MiniMaxH3SamplingScope`,
`run_minimax_h3_blocks`, `minimax_h3_block_patch_forward` and
`patch_minimax_h3_cache_model`. That repository is **AGPL-3.0**.

Hand Tie Clips took its copy by way of
[PlagueKind](https://github.com/PlagueKind)'s `ComfyUI-H3-MiniMax-Cache`.
That is not a rename-only fork: PlagueKind added reference protection in
v1.5.1 and the narrower production defaults this node ships (`0.05` /
`0.20` / `0.80` / `1`). The two central functions remain close to
silveroxides' originals (239 lines each, 218 byte-identical).

**Redistributed with permission from silveroxides, given 2026-09-16.**
PlagueKind was informed and agreed. Attribution is the condition of that
permission, and it is kept in three places — the header of `h3_cache.py`, this
file, and the node's own description text in the ComfyUI sidebar. None of the
three is decorative; do not remove any of them.

### Changes made here

- The `cond_audio` segment kind was restored. PlagueKind's port dropped it, so
  `seg_t[kind]` raised `KeyError: 'cond_audio'` on any keyframe carrying an
  audio latent — every hop of an audio chain. Core ComfyUI and silveroxides'
  original both handle it, with the same values now used here.
- Residual scratch buffer reuse and CPU feature signatures from PlagueKind
  v1.5.2. The buffer is kept across reset()/finish() and overwritten in place
  so cuMemFreeAsync is not called after Comfy tears down the block-stack
  malloc scope; feature signatures are moved to CPU float.

- A double-patch guard. Both upstreams write an owner key into `model_options`
  and replace the same `("dit", "block_loop", 0)` slot. Two caches on one wire
  corrupt each other without raising, so this copy reads all three owner keys
  and refuses rather than stacking.
- Log tags, the owner key and the node's own schema are this pack's.

### The part nobody could relicense

`h3_cache_forward` is a reimplementation of ComfyUI Core's
`MiniMaxH3Model._forward` (`comfy/ldm/minimax/model.py`). **ComfyUI Core is
GPL-3.0.** That inheritance predates every party above and none of them was in
a position to grant terms over it — silveroxides said as much directly
("not even mine to begin with"). It is recorded here rather than papered over.

The practical consequence is the ordinary one for a ComfyUI custom node: the
pack's own code is MIT and this file carries the notices above. Anyone
redistributing Hand Tie Clips who needs a stronger answer than that should
treat `h3_cache.py` as carrying its upstream terms and take it up with the
upstream authors.

### Maintenance risk, stated

Because `h3_cache_forward` duplicates Core-internal forward logic rather than
calling into it, a Core change to `MiniMaxH3Model._forward` desyncs this file
**silently** — wrong output, not an exception. `tools/check_h3_cache.py` runs
on every `check_all.py` sweep to make that failure loud. Vendoring moved that
watch from PlagueKind's repo to this one; it is now this pack's job.

## `tiled_upsampler.py` — the H3 latent upscaler's layout and statistics

**Original author: [LBH-123-AI](https://github.com/LBH-123-AI).**
Code: [`Comfyui_Minimax_h3_latent_Upscaler`](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler),
MIT. Weights: [`Minimax_h3_latent_Upscaler`](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler),
Apache-2.0. No weights ship with this pack; the user downloads them.

A network only loads into the module layout it was trained in, so two things
in this file are theirs: the layout (a conv-in, residual blocks with a scale
embedding, depthwise temporal convs, a trilinear resize, a conv-out, under
their key names) and the 24 means and 24 standard deviations the weights were
trained against. `tools/check_tiled_upsampler.py --weights` runs the real
checkpoint through this module and through theirs and requires identical
output. Everything else in the file is this pack's code.

The node's recipe -- chunk and tile sizes, the joint-step tiling, one step at
denoise 0.2 -- is the one the user proved in bbaudio-2025's
[Comfyui-MMH3-UltimateUpscale](https://github.com/bbaudio-2025/Comfyui-MMH3-UltimateUpscale) graph (MIT).
No code was copied from it; the checker compares the two end to end when that
pack is installed and requires identical latents.

## `_HTCMotionContext` in `h3_ref_chain.py` — built-in latent motion & audio pin

**Concept credit: [NikoDemon80](https://github.com/NikoDemon80) ([`ComfyUI-H3-Motion-Context`](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)).**

Earlier versions of Hand Tie Clips called out to an externally installed
`ComfyUI-H3-Motion-Context` node to slice the previous hop's video and audio
latents into ComfyUI Core's `minimax_keyframes` list. `_HTCMotionContext` in
`h3_ref_chain.py` implements that keyframe placement natively on top of this
pack's own `latents.py` and `refine_blend.py` helpers so `motion_context` works
out of the box with zero external custom-node dependency and no registry or
version-drift conflicts.

