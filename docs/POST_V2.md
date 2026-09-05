# After v2 — deferred, with the reasoning

Things deliberately not in 2.0.0. Each has a reason to wait, not a reason to
drop. Written down so the reasoning survives the session that made the call.

## The VAE skip / Qwen-only conditioning

**The idea.** Every reference reaches H3 through two channels: vision tokens
(`<Picture N>`, the Qwen3-VL tower) and VAE latents (`minimax_refs`, attended
on every step of every hop). The second is the expensive one — reference tokens
are roughly pixel area / 256, so a 6.3 MP plate is ~24,600 tokens, about six
video frames, re-attended every hop. Eight identity plates at `max` is ~197k
tokens of conditioning bolted onto the sequence. Attention is superlinear in
length; this is the crawl.

silveroxides' result: eight references at 1024 reproducing a 12.25 s sequence
in seven steps **on a 16 GB GPU**, with the VAE reference encoding dropped.

**Why it is not a one-argument test, contrary to the handover.** The
`Spilling the Master` artifact (§03) said Core's loop is
`ref_items.append(...)` then `if vae is not None: z = vae.encode(...)`, so
passing `vae=None` would build the picture tokens and skip the latent blocks.
**Read against the installed Core, that is false.** In
`comfy_extras/nodes_minimax_h3.py` the loop is:

```python
resized = _resize(img[:1], tw, th, "disabled")
z = vae.encode(resized)          # unguarded
```

`vae=None` raises `AttributeError` on the first reference. Core also declares
`io.Vae.Input("vae")` as **required**, not optional, and its `ref_image_size`
options are `["match", "max"]` — there is no `none`. The README quote naming
`none` is against a different Core or a fork. Nobody caught this because nobody
ran it; it was carried three documents deep on the strength of the prose.

**What would actually work.** `add_object_patch()`, which is where §03 landed
after correcting itself: let Core build every reference block, then patch how
the model consumes `minimax_refs` — dropping or down-weighting individual
blocks at the consumption end. Scoped to the patcher, restored by
`unpatch_model`, survives `clone()`, no Core edit and no reimplementation of
the conditioning node. Per-reference channel selection and per-reference
strength become the same mechanism, and it lands on the MODEL wire beside the
LoRA stack, which is how H3 SLA Attention already installs itself.

**The synthesis worth building, once.** Apply `refs.py`'s own principle to
channels rather than pixels: a place plate needs composition, so its ~24,600
tokens of VAE block are waste; a face plate needs bone structure, so it needs
the block. Core's `ref_image_size` is global, but this pack already makes
per-reference decisions at load via `mp`.

**Why it waits.** It is speculative, it needs a GPU to evaluate, and it points
the OPPOSITE way from the texture work — channel 2 is plausibly where
`retention: fully_preserved` actually lives, so dropping it may cost identity.
silveroxides describes his own results as "approximate" and "stochastic":
strong compositional control, not pixel-exact identity. v2 already ships more
untested surface than is comfortable. Test it precisely because it disagrees
with the rest of the pack's direction, but test it on its own.

## Also waiting

- **Her §7.3** — substituting a Lab-corrected tail into the video component of
  the Motion-Context latent. Wants one measured run of
  `tone_anchor_ref=still` + `pin_mech=addguide` first; if that holds the chain,
  the latent surgery is unnecessary.
- **Subject and background corrected separately** — the skin loses chroma while
  the background gains edges, so one global correction under-serves both. Needs
  a segmentation the pack does not have and does not want as a dependency.
- **Keyframe chaining proper** — a guided last frame becoming the next hop's
  frame 0. `last_frame_guide` ships the conservative half (guide only, opt-in,
  default off). The full form changes what a chain *is* and is a v3 argument.
- **Peak RSS on a long chain** — the master spill's win has only ever been
  argued, never measured end to end. Instrument it on the next long render.
- **Block repetition** — running block N twice per step, re-feeding the same
  latent. An untested idea, noted only so it is not lost.
