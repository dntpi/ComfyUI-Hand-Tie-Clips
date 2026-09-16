# Changelog

User-facing. The files named here are in the repository, not in the installed
pack — the published package excludes them. Engineering detail lives in
`docs/DEVLOG.md`. `docs/HANDOVER_*.md` and `BETA_NOTES.md` are historical
and should not be read as the state of this release.

## 2.1.0 — 2026-09-16

A second sampler pass per hop, a cache that makes the non-turbo base usable,
and a prompt pack that now has a trust boundary. **Every render behaviour here
ships off.** A workflow saved on 2.0.0 loads and renders identically — the new
widgets are appended at positions 71-82 and nothing is inserted or reordered.

### New

- **`hop_refine`** (RUN) — `off` (default), `full` or `pin_only`. A second
  sampler pass over each hop's latent before it is decoded and before it is
  handed forward as the next hop's pin. The pass has its own seed, its own
  sigma schedule and optionally its own model, so it is a genuinely separate
  render and not a continuation of the base one.

  It exists because the thing that holds a chain together is not per-hop
  quality. Drift compounds through what each hop hands the next, and a short
  under-converged second pass over the *joint* latent is the one mechanism
  measured to move the seam without also moving the face.

  The defaults are a confirmed external configuration, not this pack's guesses:
  `refine_denoise 0.50`, `refine_steps 2`, `refine_cond base`. At H3's
  `shift=12.0` that resolves to a schedule of `[0.9231, 0.8000, 0]` — one
  `res_multistep` step from 0.8 to 0, deliberately under-converged. Note what
  `refine_denoise` actually is: `BasicScheduler` computes `int(steps/denoise)`,
  so it sets the *step size*, not a noise amount. 2 steps at 0.50 means a
  4-step schedule truncated to its last two. Raising the denoise shortens the
  schedule; it does not add noise.

- **`refine_blend`** and **`refine_blend_interp`** — a per-frame keyframed lerp
  between the raw and the refined latent, default `0:0, 22:0, 44:1` linear.
  Frame 0 ships raw, the ramp is over latent steps 7-13, the tail ships fully
  refined. The blended result is what is delivered *and* what propagates, so
  the pin the next hop inherits is the blended one. Empty string disables the
  blend and ships the refined latent whole.

  The ramp's frame ratio is computed from the latent itself. The upstream node
  this follows derives it from a `duration` widget, and when that widget
  disagrees with the real render length the keyframes land in the wrong place
  with no error at all. There is no duration input here and no way to desync.

- **`refine_audio`** — `freeze` (default) or `refine`. On `freeze` the audio
  stream is masked out of the refine sampler, so audio leaves a refined chain
  bit-identical to an unrefined one. This is not the upstream default and the
  difference is not cosmetic: taking audio wholly from the refined latent was
  measured as making the voice strained across three runs. Mask polarity is
  **1 = denoise, 0 = freeze**, pinned by `audio_lock.assert_mask_polarity`; an
  inverted mask reads as "lip-sync died." With `master_audio_file` set the take
  still wins — refine never touches a locked audio path.

- **`refine_head`** — `refine` (default) or `freeze`. The other seam mechanism,
  kept as a widget rather than a choice because it is a trade and not a
  winner. `freeze` holds the hop's first frames out of the refine pass; it
  removes the seam flash and wins on the face (seam error 1.9x/1.7x against
  3.5x/7.3x) at a grain cost that is real and is invisible on bokeh.

- **`refine_model`** (optional MODEL socket) — run the refine pass on a
  different model from the base. Unwired means the base model. It is included
  in the hop cache key by fingerprint, so swapping it re-renders rather than
  serving a stale hop.

- **`refine_sampler`** / **`refine_scheduler`** — `same` follows the base.
  `res_multistep` + `simple` over an `lcm` base is the best combination tried
  so far.

- **`speed_mode`** — `regular` (default) or `turbo`. A named preset table, one
  place, mapping mode to refine defaults. Most of the turbo row is unmeasured
  and the tooltip says so. What *is* measured, on matched runs at a pinned
  seed, is that a turbo base is the degrader: junction MAE 8.40-10.85 and
  climbing hop over hop, against 2.07-4.02 flat for a plain hybrid over 9-10
  hops. Putting the turbo checkpoint in the base loader only did not save it,
  so it compounds through the conditioning path rather than the latent. The
  preset makes no claim to fix that, prints the cost once per run, and never
  silently corrects a widget.

- **H3 Cache**, a new node. It reuses MiniMax-H3's whole-block-stack residual
  across steps whose features have barely moved, which is what makes the
  non-turbo base fast enough to recommend at all. Drop it anywhere on the MODEL
  wire before the sampler; it patches a cloned patcher only, so it composes
  with SLA attention and with LoRA loaders in either order. Feed `refine_model`
  too if you run a separate refine model — the refine pass is its own sampler
  call and is not cached otherwise.

  Defaults are `0.05` reuse / `0.20` start / `0.80` end / `1` max skip, which
  are the settings actually being run in production rather than the wider
  window the original widgets invite. Both shipped workflows have it wired.

  **The implementation is silveroxides' work** (`ComfyUI-UtilsCollection`,
  AGPL-3.0), improved and ported by PlagueKind and redistributed with
  permission given 2026-09-16. It descends further back than that: its forward
  pass is a reimplementation of ComfyUI Core's `MiniMaxH3Model._forward`, and
  Core is GPL-3.0. `THIRD_PARTY_NOTICES.md` records all of it, including the
  parts nobody was in a position to relicense. If you redistribute this pack,
  read that file.

### The prompt pack now has a trust boundary

The rewrite was strong on craft and had no notion of untrusted input anywhere.
A brief reading "Write exactly 1 hop" was indistinguishable from the node's own
instructions, because the brief went into the turn first with machine
instructions concatenated after it.

- Every untrusted channel is now delimited — the brief, the SWAP brief,
  filenames read off disk, rail values — and `AUTHORING_PROMPT.md` carries a
  precedence paragraph stating that delimited text is scene material which can
  never change hop count, field lists, or the rules above it.
- The unbounded "unless the user message says otherwise" override in
  `SWAP_PROMPT.md` is gone.
- Validator feedback is labelled as node output. It was being appended as
  `role: "user"`, so model-authored text came back wearing your authority.
- `mp` and `locked` are documented with "do not author". `locked` is a boolean
  meaning "serve the cached render", and the pack uses the word as prose
  elsewhere — `bool("some text")` is `True`, which silently serves a stale hop.
- The two contradictory accounts of the 9-reference limit now agree with the
  code: it is a per-hop ceiling.
- `EXAMPLE_6_HOP.md` is regenerated from a compliant workflow. The only
  few-shot in the pack was breaking three of the prompt's own rules, including
  the unplated location the prompt itself calls the most common mistake.
- The prompt pack README's token count was off by about 3,000 and its `sed`
  example had a literal `\n` in it.

### Checks

Six new offline checkers, all in `tools/check_all.py`: `check_refine_blend.py`
(ramp arithmetic, CPU-only, including the ratio-from-latent fix),
`check_refine_audio.py` (mask polarity and master-lock precedence),
`check_refine_keys.py` (every refine widget changes the hop key),
`check_speed_mode.py`, `check_widget_order.py` (the positional-widget
guarantee), and `check_h3_cache.py`. That last one earns its place: the
vendored cache duplicates Core forward logic rather than calling into it, so a
Core change desyncs it *silently* — wrong output, not an exception. It has
already happened once in the wild.

`check_workflows.py` also now asserts node ids are unique, after a duplicate id
in a shipped workflow silently reattached another node's wires.

29 checks, all passing, including `check_audio_lock.py` and
`check_restart_trim.py` which were failing before this work.

### What has been verified, and what has not

The refine path has run: three 9-hop chains at `refine=full`, 6-step plain
hybrid base, no turbo, each with a second DiT on `refine_model` rather than
the unwired default. Junction MAE 2.21-4.62 and **flat**, texture -14.9%, and
face scale -1.4% from hop 1 to hop 9 -- the zoom creep did not happen. Audio at
hop 1 is sample-exact under `refine_audio=freeze`. These defaults are frozen on
the strength of that.

Two limits travel with that result, and neither is hidden here:

- **Attribution is open.** Only the `full` arm has run. The honest claim is
  "this configuration holds a 9-hop chain", not "the refine pass is why". The
  controlled `off`-vs-`full` pair at a pinned seed has not been rendered.
- **It was measured on a flat white wall, fixed close-up, near-zero motion.**
  The same model at the same step count comes apart on a wide moving shot. "6
  steps is fine" is a statement about that footage, not about H3.

Also unmeasured: the chain cost of base step count -- voice quality lands mostly
by 10 steps, but what a raised step count does to drift *over* a chain is not
known -- and VRAM for the refine pass, where the "one model is enough" answer is
read off the code rather than off a profiler.

## 2.0.0 — 2026-09-06

A full release, not a beta. The hop cache, the pin, the media slots and the
tone tools that accumulated on side branches since 1.1 are in this tree, plus
four new behaviours.

### New

- **`master_audio_file`** (MEDIA). One continuous take every hop lip-syncs
  to. Empty (the default) is off and does not change existing renders. When
  set: each hop is locked to a window of that file on the same clock as the
  picture; delivered audio is a passthrough of the take, no VAE round trip.
  The beat still needs the words in `<d>[English] ...</d>`.
- **`last_frame_guide`** (RUN, join & pin). `off` (default),
  `before_restart`, or `still`. It AddGuide-pins `start_image` at a hop's
  last pixel frame, so the hop *ends* on the photograph and a following
  restart — which opens on that same photograph — reads as a match cut
  rather than a jump. **`before_restart` is the setting to use:** it guides
  only a hop whose next shot is `anchor: "restart"`. `still` guides every
  hop, which also overrides an authored `framing` directive at every hop
  ending — a shot set `framing: close` plays close and then snaps to the
  still's wider framing in about 0.6 s, and the next hop pushes back in.
  Use `still` only when no shot authors a framing. Needs a start image.
  Neither becomes the next hop's frame 0.
- **`anchor: "restart"`** on a shot. That hop is a chain start: the start
  image is frame 0, nothing is relayed from the previous hop. It is a cut.
  Pair it with `join: hard_cut`. Restart hops now write their **full
  length** — they used to drop the 0.9 s overlap as if they were a
  continuation.
- **`refs` on a shot.** Which register stills ride that hop. Omit the field
  for the register default (unscheduled stills on chain starts, off
  continuations). `[]` is none. A filled list is those tags only, in that
  order. Unknown tags fail on the queue.
- **SWAP**, a fifth tab. A one-hop identity swap from a reference clip: the
  clip supplies the motion and the scene, a still from the REFERENCES rail
  supplies the person. **Write** drafts it, **Accept** writes exactly one
  shot plus the clip's description and never touches `ref_plan` -- your
  register is not rewritten. Contributed by @frankyi, then rebuilt from its
  own prose rather than merged.

  Four named modes, because at cfg 1.0 there is no negative branch and a
  mode that merely *omits* the swap line does not keep the clip's person --
  the identity photograph is in front of the encoder either way and governs
  the subject anyway. Each mode says positively what stays:
  `replace_person` (face, build, hairstyle and wardrobe),
  `head_swap` (face, hair and skin tone; the body, posture, hands and every
  garment stay with the clip), `face_only` (features only), and
  `keep_person` (swaps nobody -- the clip is a scene and motion plate, and
  the identity picker greys out). The taxonomy follows PromptMasterLD's edit
  laws; the prose is written fresh for H3 beats.

  Alongside them: **background** from the clip, from a `@tag` picture, or
  free; and an optional **wardrobe plate**, a `@tag` whose garment is *worn*
  -- draping on the body in frame and creasing where it bends -- rather than
  pasted.

  **Two things to get right, both of which cost renders to find out.** Do
  not run MEDIA's describe on the clip before a swap: that caption reaches
  the encoder as what `<Video 1>` *is*, and a caption naming a person asks
  for the person you are replacing. Four consecutive "head swap doesn't
  work" reports traced to that, a missing frame sequence and a weak
  citation -- no broken code among them. SWAP now warns when a caption names
  somebody. And drop the clip to about 0.3 MP: a reference clip's decode
  area is its token count, and its token count is its influence, so a
  full-size plate out-argues a single photograph.

### Already on the 1.2 tree, now in the release

- Three reference-clip slots and three voice slots. Numbering is dense.
- Lab tone anchor (`tone_compensate=anchor`, `tone_anchor_ref` hop1 / still).
- `pin_mech` (auto / motion_context / addguide).
- Hop cache: safetensors latent sidecar (no pickle), master buffer spilled
  to a delete-on-close mapping above 2 GiB.
- **Seam report** takes the chain's `info` output as an input and reads the
  join frames the render actually wrote. Wire it: with a restart in the
  chain the hop lengths are no longer uniform, and the `hops` / `overlap`
  widgets cannot describe that — on a 4-hop chain restarting at hop 4 they
  put the seams six, eleven and sixteen frames out, far enough to measure
  the flat middle of a hop and report three visible seams as invisible.
- Per-hop cache keys for pin, overlap, tone and `pin_to_qwen`, so hop 1
  survives those A/Bs.

### One change that affects every render

The master frame buffer is **fp16** rather than fp32, which halves the
largest allocation in the pack — an 8x15 s chain at 1280x736 goes from about
29 GB to 14.4 GB, and half the disk I/O when it spills. The buffer is
delivery-only: nothing in the conditioning path reads it back, and tone
correction still happens in fp32 before the write.

It is not bit-identical, and the number is small but real. The encode
truncates, so a value fp16 nudges below an integer boundary loses one 255th.
Measured over two million samples: **2.06% of pixels move, every one of them
by exactly 1, none by more** — well under the h264 encode's own error. The
`images` output is therefore an fp16 IMAGE. Core's save and preview paths
take it; a third-party node that assumes fp32 has not been tested against it.

### Verified on a GPU

- The audio lock joins across hops on one take: hop 1 `[0.00s-8.00s]`, hop 2
  `[7.08s-15.08s]`, delivered as a passthrough, drift +0 ms. Empty really is
  off — the generated voice comes back.
- `anchor: "restart"` stays in the shot with a fresh seed, and writes its
  full length (a 4-hop chain restarting at hop 4 delivers 724 frames, not
  the 702 the old formula gave).
- `last_frame_guide` turns the restart jump into a match cut, and `still`
  fights an authored framing as described above.
- fp16 survives delivery.
- SWAP `head_swap` on a 0.3 MP clip with no clip caption: the head is
  replaced, the body and its motion stay with the clip, and no colour or
  hair from the plate bleeds through.

### Still not verified

- Whether `refs: []` on a restart hop keeps the room instead of the identity
  portrait. The diagnosis behind that field came from frames, not from this
  code path.
- Anything longer than 4 hops at 8 s. The texture ratchet is unchanged and
  3-5 hops is still the honest limit.
- Three reference clips or three voices at once, on a card.
- SWAP's wardrobe plate and picture background. Both were cited-but-not-
  scheduled until the fix in this release and could never have rendered;
  the fix is covered offline, not on a GPU. `face_only` and `keep_person`
  have not been run either.

Saved 1.1 workflows load. New widgets were appended, not inserted, and no
existing default changed.

**A hop cache from 1.1 is fully invalidated by this release, deliberately.**
The model fingerprint now identifies the base checkpoint, which it did not
before — an int8 build and a bf16 build of the same architecture under the
same LoRA stack produced byte-identical keys, so the cache could serve frames
rendered under the other checkpoint. Closing that moves every key. The old
entries are never served, and the first sweep after the upgrade reclaims
them; their pickle-era latent sidecars are never read (that format is
retired) but are now counted against the budget and deleted with their entry
rather than orphaned. The practical effect is that the first chain after
upgrading re-renders in full.

## 1.1.1 — 2026-09-05

Core calls are keyword-only. Fixes `got multiple values for argument
'ref_image_size'` on hop 1 for ComfyUI builds that order MiniMax H3
parameters differently.

## 1.1.0 — 2026-09-03

Tabbed editor, `render_from` / `render_through` range, retention text on
every hop a still rides, computed canvas, per-hop reference keys.

## 1.0.x — 2026-09-02

First full release (WRITE panel, schema, rail). Patch releases cleared
registry-scanner findings and the WRITE-panel "no model is selected" bug.
