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

## The contributed SWAP tab — held for 2.1

A user sent a working tree adding a **SWAP tab**: video to identity swap, automatic frame
extraction, an LLM-drafted plan, a 3-way background mode, a 3-way wardrobe mode, five new
toggles, and a shared generate/validate/repair refactor. Reviewed in full; the report is
`CONTRIB_REVIEW.md`, kept outside the repo because it quotes their filenames.

**It is good work and it is not in 2.0.0.** Three reasons, in order:

1. **A data-loss bug that needs a design decision rather than a fix.** SWAP sends at most
   three refs where WRITE sends the whole rail, and Accept replaces `ref_plan` wholesale, so
   every other reference on the register is silently deleted. Separately its `railRefs()` is
   a copy of `writer_bar.js`'s with the `mp` field dropped, and `_restore_rail_only` is
   authoritative-not-restorative, so every SWAP write resets every megapixel cap to native --
   the `chain_00047` failure documented fifteen lines above the function they copied. The
   first of those is not a typo: "what should Accept do when a tab knows only a subset of the
   register" is a question about how the rail works.
2. **No validation budget.** A whole new tab with an LLM path, on a release that already
   ships more untested surface than is comfortable, after the GPU window closed. It would go
   out on reading alone.
3. **It is a working tree off v1.1 that keeps diverging.** 96% applies clean to v2, but the
   semantic cost is a day or two, dominated by SWAP assuming one clip where v2 has 3x3 media
   slots.

**Deliberately NOT cherry-picking the safe parts.** The media helpers, the refactor and
"describe frame only" are clean takes on their own, but pulling three pieces out of somebody
else's working tree fragments their contribution and makes their rebase harder, for features
nobody has asked for yet.

**Plan: invite a rebase onto v2 after 2.0.0 is tagged.** That is easier for them against a
tag than against a moving branch, and the collisions are already enumerated.

Worth sending them regardless of what we merge, because it is live in their tree today: the
`mp` drop and the wholesale `ref_plan` replacement are biting them right now. And the
wardrobe-drift diagnosis they report -- video pixels outlasting the text instruction -- is an
assertion: three prose comments, no chain ids, no measurements, in a pack that cites
`chain_000NN` in exactly that kind of comment everywhere else. If they have the evidence it is
worth a great deal; if it is a hunch it should read as one.

## More reference slots than Core declares

Raised while looking at the media strip: H3 is said to accept more references than the nine
it advertises, and if so the pack should let you add them.

**What Core declares** (`comfy_extras/nodes_minimax_h3.py`, the `io.Autogrow` templates):

    ref_images        max=9
    ref_videos        max=3
    ref_video_audios  max=3
    ref_audios        max=3

So the pack's three clips and three voices are not a design choice -- they are Core's
ceiling, and a fourth voice slot would have nowhere to go. The reference RAIL is already the
"+" for images and is capped at `MAX_SLOTS = 9` in `ref_rail.js`, matching.

**Why it is nonetheless possible.** This pack does not go through the Autogrow schema. It
calls `MiniMaxH3ReferenceToVideo.execute()` with a plain dict, and that method iterates
`(ref_images or {}).values()` without counting. A dict of twelve would be encoded and
attended like a dict of nine. The `max=9` is enforced by the frontend and validation, not by
the code that does the work.

**Which is exactly why it needs proving before it ships.** Passing more entries than a
declared maximum is relying on an undeclared property of somebody else's node -- the
"protocol boundaries are not pixel equality" rule in the other direction. Core may cap at 9
because the model was trained that way, because of a positional token budget, or because
nobody tried more; the schema does not say which, and neither do we.

**What it would take.** One render at ten and one at twelve references, against a nine-ref
control on the same seed, measured with `analyze_skin.py` and `texture_probe` in CACHE mode
-- does identity hold, does anything degrade, and what does it cost per step given references
ride every step of every hop. If it holds, the rail's `MAX_SLOTS` and the tooltips move
together and the README says plainly that it exceeds Core's declared limit deliberately.

Cheap to test, and it must not be assumed. A user reporting that it "works" is a report
that it did not crash.
