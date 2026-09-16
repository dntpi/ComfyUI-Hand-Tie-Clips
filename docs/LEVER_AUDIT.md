# Lever audit -- what actually does work on same-frame consistency

**Status: evidence only. No widget is removed by this document.** The removal
call is the user's, made after reading the table.

The exploration that preceded this found **zero dead levers**: all 81 declared
inputs are read by the node, and the ones that do not touch pixels
(`cache_budget_gb`, `dry_run`, `contact_sheet`, `render_from`/`render_through`)
are non-rendering *by design*, not vestigial. So "is it wired" is already
answered and is not the question here. The question is narrower and harder:

> **does this lever do real work on same-frame consistency?**

Same-frame consistency means the scope the refine work was built for -- one or
two people talking to camera, locked framing, the shot that must look like the
same person in the same room at hop 9 as at hop 1. A lever can be perfectly
functional, valuable, and still score `cand.` or `I/O` here, because it is aimed
at something else.

## How to read the table

| class | meaning |
|---|---|
| **DRIFT** | measured to change cross-hop consistency, in either direction |
| **DRIFT-NULL** | measured, and the honest answer is "it does nothing here" |
| **cand.** | sits in the pin / conditioning / schedule path, so it *could*; **not measured** |
| **RENDER** | changes the picture or sound inside a hop; no cross-hop claim |
| **I/O** | decides what content goes in; not how the chain holds it |
| **NONE** | does not render by design (cache, preview, range, dry run) |

`key` marks the nine levers that enter the per-hop cache payload
(`h3_ref_chain.py:3829-3844`), i.e. changing them re-renders rather than serving
a stale hop. That list is a correctness property, not a ranking.

## The instruments, and their limits

Read this before trusting any number below.

* **Flat-wall ROI, fixed box, never the mic boom or the window.** Whole-frame
  mean texture is blind to the defect: on chain_00179 the wall ROI read **+248%
  at hop 2** while the whole frame read **+0.7%** and did not move until hop 6.
  Do not auto-select "the flattest region" per frame -- that is selection bias
  toward the very thing you are trying to detect.
* **The texture metric's noise floor is +/-5%.** Calibrated on content that was
  provably bit-identical (hop 1 under a lever that cannot reach hop 1, PSNR
  `inf`) and still read +5.3%. Anything under ~5% is instrument noise.
* **Judge the hop1 -> hop2 step, not the endpoint**, and judge the **last** frame
  of a hop, not a mid-hop frame. Mid-hop-3 once read "0.15 looks best" and the
  last frame reversed it.
* **A softer sampler fakes a flatter ratchet.** Always check the hop-1 baseline
  before reading "less degraded".
* **Seam continuity is GEOMETRY** -- the 8x8 block-pooled number, not grain and
  not tone. Stock chains read 1.00x there; that is what a seam with no defect
  looks like.
* **Zoom is a separate failure from texture**, and a fixed ROI cannot tell them
  apart, because the content marches into the box. Measure head scale with the
  dark-mass + hair-top-row probe.
* **An off-widget confound outranks several widgets below:** the H3Utils SLA
  setting (house 0.90 / 64x64 / 8192) materially changes texture decay and is
  invisible in HTC's run header. Log-check `[H3Utils] SLA installed` before
  comparing any two chains on texture.

---

## Required inputs

| lever | default | class | evidence |
|---|---|---|---|
| `model` | socket | **DRIFT** | **The largest measured lever in the pack.** Single-variable pair on `OP_CleanRoom_V2.json`, pinned seed: plain hybrid base held junction MAE **2.07-4.02, flat over 9 hops**; turbo-merged base **8.40-10.85, climbing from segment 3**, visibly gone by hop 4. It compounds through the conditioning path even though each hop's base latent is fresh. [T] |
| `clip` | socket | cand. `key` | Only one text encoder has ever been run. In the hop key, so a swap re-renders correctly; effect unmeasured. |
| `vae` | socket | RENDER | Decode path only. |
| `audio_vae` | socket | RENDER | Decode path only. |
| `prompt` | prose | cand. | Upstream's ten-run study found the drift is **content-based, not scale-based**, which puts the wording inside the causal path. Never isolated as a variable here. [U] |
| `chains` | `3` | RENDER | Sets exposure, not resistance. Worth stating plainly: **upstream's own conclusion is "3-5 hops is the honest limit"**, and they do not claim 8-10 holds. Exceeding it is this project's goal, not its baseline. [U] |
| `resolution` | `768p` | cand. | Confounded, never isolated. chain_00202 ran 576x1024 against a 768x1344 ladder and read flat texture -- with a different checkpoint and a different reference, so the resolution share is unknown. [Z] |
| `aspect` | `16:9` | RENDER | Framing choice. |
| `duration` | `10 s` | cand. | 192 f / 8 s is the tested hop. Motion-Context's author: **124 f is H3's sweet spot; long clips send sound degradation off the rails.** Hard project rule: never past 15 s. Not swept. [O] |
| `overlap` | `0.9 s` | cand. `key` | 22 f context / 24 f audio are upstream's documented known-good values and ours match. Changing it moves the audio frame grid, which already slips ~8 ms per hop and stacks at every join. Not swept. [U][G] |
| `seed` | `0` | RENDER `key` | Not a quality lever; it is the **precondition** for every A/B in this document. |
| `seed_per_shot` | `True` | RENDER `key` | Reproducibility control. |
| `steps` | `14` | **DRIFT** | Two results, pointing opposite ways. **Video:** one n=1 3-hop probe where hop 1 was inert and hops 2+ at 3 steps *halved* drift vs a flat 4 -- seeds unconfirmed, not yet a lever. **Audio:** single-hop A/B at 576p, pinned seed, chain_00212/213/214 at 6/10/14 steps -- spectral flatness **0.0079 -> 0.0067 -> 0.0060** (less noise-like, more harmonic), voiced share 83.1 -> 86.5 -> 86.7%, in-hop laplacian flat (3.181 / 3.185 / 3.130). Most of the gain lands by 10. Under `refine_audio=freeze` this widget alone sets voice quality. [B][W] |
| `sampler_name` | `res_multistep` | **DRIFT** | Judged by eye over 3-hop samples: `lcm/simple` gives the crispest hop-1 baseline; `res_multistep/sgm_uniform` starts softer, *appears* less degraded for that reason, and shows more visible tone change at the seams. The trap is in the instrument notes above. [S] |
| `scheduler` | `beta` | **DRIFT** | Same result; the pair is the lever, not either half. [S] |
| `shift_video` | `12.0` | cand. | H3's own value. It is what makes `simple/2/denoise 0.5` resolve to `[0.9231, 0.8000, 0]` -- one step from sigma 0.8, deliberately under-converged -- so the entire refine tuning sits on top of it. Never swept, and sweeping it invalidates the refine defaults. |
| `shift_audio` | `3.0` | cand. | Never swept. |
| `ref_image_size` | `match` | cand. | Feeds the reference conditioning path, which is where the measured framing failure lives. Not isolated. |

## Optional inputs -- pin and conditioning path

This block is where same-frame consistency is won or lost.

| lever | default | class | evidence |
|---|---|---|---|
| `ref_plan` | -- | **DRIFT** | Two independent findings, both negative for heavy reference use. (1) **Face crops teach zoom:** chain_00202, ref `hero_face_1_1.jpg` marked `fully_preserved` on shots 1-6 -- the head grows to fill **+23.6%** more frame area, crops out of the top of frame by hop 3, wall visible 2.7% -> 0.2%, saturating at hop 3. (2) Motion-Context's author: **a character sheet on every segment causes MORE degradation than the refiner does**, though it does not blow up the join; dropping it later in the chain brings the fade back. A trade, not a bug. [Z][O] |
| `start_image_file` | -- | **DRIFT** | The same mechanism from the other end. Standing rule: **full-body start_image; face/head crops on hop 2+ teach zoom.** [Z] |
| `pin_mech` | `auto` | cand. `key` | Scoped, not swept. `reset` is being developed specifically for locked-off talking-head chains past the 15 s hop -- the locked framing is what makes the ORB warp near-identity. `addguide` is the only mode that closes the tone loop (see `tone_compensate`). No head-to-head drift measurement exists. [R][C] |
| `pin_to_qwen` | `last frame` | cand. `key` | A/B workflows exist on disk (`LAB_01_ROUTE_*`, `LAB_07_*`); no result in the record. |
| `last_frame_guide` | `off` | cand. | A/B workflows exist (`LAB_02_GUIDE_OFF/FADE/MATCHED`); no result in the record. |
| `pin_renorm` | `off` | cand., predicted null | Upstream's content-based finding says a renormalisation lever **will not buy identity -- it fixes a statistic, and the statistic is not what melts.** Predicted, not measured. Carries the only true relic in the pack: the legacy `"on" -> "sigma"` remap at `h3_ref_chain.py:3421`. [U] |
| `pin_noise` | `0.0` | cand. | The one lever upstream ranked **second-best** for holding the reference look (behind `anchor:"restart"`). Never measured here. Note the adjacent cautionary tale: `pin_taper`, which injected noise into the pin, was measured, retired, and **is absent from v2.0 entirely** -- it reversed the refine pass and put degradation back to no-refine levels. [U][P] |
| `audio_pin_frames` | `24` | cand. | Matches upstream's documented `audio_context_length 24`. Not swept. [U] |
| `tone_compensate` | `off` | RENDER | **Measured against the code, and cosmetic by construction:** under the Motion-Context latent join the correction reaches only the delivered frames; `pin_mech=addguide` is required for it to feed back into the pin. Production ran `anchor` for 7 hops and it never touched what the next hop conditioned on. Any argument of the form "tone is being corrected, so colour drift is handled" is wrong on this config. It cuts both ways -- tone_compensate is therefore also **not a confound** for pin-path measurements. [N] |
| `tone_anchor` | `0.35` | RENDER `key` | Strength of the above. |
| `tone_anchor_ref` | `hop1` | RENDER `key` | Reference for the above. `still` exists because **hop 1 is itself already short of the reference still** (chroma 33.6 -> 30, b* 26.6 -> 22), so hop-N-vs-hop-1 measures against a target that already moved. [U] |
| `shot_plan` | -- | I/O + inherits | Carries per-shot `steps` and `seed`, so it inherits those rows. The cheapest known route to the voice/drift trade is `steps: 10` on speaking shots only -- **proposed, not yet run as a chain.** [W] |
| `hop_script` | `verbatim` | I/O | Prompt routing. |
| `establish` | prose | I/O | Hop 1 prompt. |
| `continuity_state` | -- | I/O | Chain resume. |

## Optional inputs -- the refine block

Appended slots 65-74, plus `speed_mode` at 75 and the `refine_model` socket,
which has no widget slot. Ships inert (`hop_refine=off`).

**Read `refine_steps` and `refine_denoise` as one lever, not two.**
`BasicScheduler` computes `total = int(steps / denoise)` and then takes the last
`steps + 1` sigmas of that schedule, so `refine_denoise` does not set an amount
of noise -- it sets **N**, the length of the native schedule the refine's last
few moves are taken from. The shipped `refine_steps 2 / refine_denoise 0.50` is
`N = 4`: the native 4-step schedule is `[1.0, 0.9730, 0.9231, 0.8000, 0]` and
the refine runs its back half, `[0.9231, 0.8000, 0]` -- **two** model
evaluations, not one, so roughly +33% on a 6-step base. This is mechanism read
off ComfyUI's own source, not a measurement, and it does not reopen anything
below; it explains why the denoise sweep measured null. [M]

| lever | default | class | evidence |
|---|---|---|---|
| `hop_refine` | `off` | **DRIFT** | `chain_00210`: 9 hops, `full`, base 6 steps res_multistep/simple, no turbo, two loaders. Junction MAE **2.21-4.62, flat**; texture **-14.9%**; **face scale -1.4% hop1 -> hop9 -- the zoom creep did not happen.** Two caveats travel with that result: **attribution is open** (only the `full` arm ran, so the claim is "refine=full holds a 9-hop chain", not "refine is why"), and the whole thing was measured on a flat white wall, fixed close-up, near-zero motion. |
| `refine_steps` | `2` | **DRIFT** | The real delta in OP's stack is **step count (2 vs 3), not denoise** -- confirmed node-by-node in the clean-room read. At `shift=12.0`, `simple/2/0.50` runs from sigma 0.9231 to 0.8 to 0. Read with the note above: step count and denoise jointly pick **N**, and the shipped pair lands on `N = 4`. What that means depends on the base, which is why one row cannot serve both. Against the **turbo** merge, whose previews are reported flat by 4 steps, `N = 4` is the model's own converged schedule and the refine is its soft back half -- texture added without re-deciding content. Against a **6-step plain base**, the same `N = 4` is *coarser* than the pass you actually ship, so the refine's moves are larger than the base's. Same widgets, opposite behaviour. [K][M] |
| `refine_denoise` | `0.5` | **DRIFT-NULL** | Closed. Seam geometry does **not** track it -- 0.20 measured *worse* geometrically (2.75x) than 0.50 (2.41x), so it is not a "too much denoise" problem; lower denoise improves only TEXTURE continuity, which is why 0.20 *looks* cleaner while measuring worse. Swept again at 0.4/0.5/0.6 and closed. Motion-Context's author: colour shift is intrinsic to resampling and visible **even at denoise 0.08**. OP on his own 0.5: "I push extreme see effect" -- a demonstration, not a recommendation. [E][D][O] |
| `refine_head` | `refine` | **DRIFT** | The clearest trade in the pack, chain_00183 vs chain_00187, identical but for this widget. **Freeze buys the seam flash:** per-frame luma step at the joins 1.9x / 1.7x (freeze) vs 3.5x / 7.3x (refine) -- under refine the two seams are the #1 and #2 largest luma transitions in the whole clip, and the flash compounds. **Freeze costs background grain:** -16.1% / -21.8% at hops 2 / 3. The user's eye: the face looks better with freeze and the background difference **is not visible at all** -- consistent, since 21.8% of ~1.17 grey levels is ~0.25 grey levels in a defocused region. **The cost is footage-dependent; the bokeh hid it. Do not generalise "freeze is free" to a genuine flat wall.** [H] |
| `refine_audio` | `freeze` | **DRIFT** | Settled twice. Three runs (00175/00176/00177) failed because the audio hold shared a gate with the video head freeze, so **hop 1 -- which has no pinned head -- was never gated in**, and its voice was re-sampled every run; hop 1's voice is what every later pin continues, so the damage was applied once and carried down the chain. Fixed by splitting the gates; `freeze` holds audio on **every** hop including hop 1. Confirmed clean by ear over 8 hops, and hop 1 audio is **sample-exact** in chain_00210. Note the failure signature: it measured *flat* per hop, because it was uniformly wrong, not progressively wrong. [A] |
| `refine_blend` | `0:0, 22:0, 44:1` | **DRIFT** (mechanism verified) | Ported natively; the ratio is computed from the latent, not from a `duration` widget, so OP's silent desync bug cannot occur here. Maps through H3's real `(1,4,4,4,4)` cycle to **latent steps 7.00-13.25**, matching the published node. Side effect: it is a free within-run A/B -- frames 0-21 are pure raw, 44+ pure refined -- and on chain_00210 that read **+3.6% laplacian / +0.7% high-frequency**, i.e. inside content variation. The refine pass does not amplify the base render. [L] |
| `refine_blend_interp` | `linear` | cand. | OP uses linear, not the node's own default. `smooth` / `step` unmeasured. |
| `refine_sampler` | `same` | **DRIFT** | Splitting beats matching: base `lcm/simple` (crisp start, gives the refine something real to work from) + refine `res_multistep/simple` (gentle, avoids re-cooking it). Matching both to lcm gives a great hop 1 that then bakes; matching both to res_multistep hides the damage under softness and adds seam tone steps. `same` is therefore **not** the best value, only the safe one. [S] |
| `refine_scheduler` | `simple` | **DRIFT** | The other half of that pair. [S] |
| `refine_cond` | `base` | cand. | OP wires `Get_base_conditioning`; `hop` was the backup's old guess and measured worse in aggregate, never in isolation. |
| `speed_mode` | `regular` | RENDER (preset only) | Not a lever in its own right -- a named table mapping mode to refine defaults, so "make turbo work" later is a table edit rather than a branch hunt. Its one evidence-backed entry is `refine_head=freeze`, because turbo fails at the seam first and freeze is the only refine lever measured to move a seam. **Everything else in the turbo row is unmeasured, and the preset makes no claim to recover the ~2.5x accelerating seam cost recorded under *What the table actually says*.** It prints the cost once per run and never silently corrects a widget. [T][H] |
| `refine_model` | socket | **DRIFT-NULL** | Wired to a LoRA-free branch on chain_00202 -- **no visible difference with or without.** Consistent: it targets texture, and texture was already flat on that stack. **Do not re-propose it as a drift lever.** [Z] |

## Optional inputs -- media and audio I/O

None of these are consistency levers; they decide what content goes in. One of
them is nonetheless the most important row in this document for anyone who cares
about voice.

| lever | default | class | note |
|---|---|---|---|
| `master_audio_file` | -- | I/O | **The escape hatch, and it ships today.** A real recording; generated audio is discarded entirely, which decouples voice quality from step count completely. This is the honest answer for anyone who cares about voice fidelity, and it belongs in the docs plainly rather than buried. |
| `voice_file`, `voice_2_file`, `voice_3_file` | -- | I/O | Voice references. |
| `voice_start_s`, `voice_end_s` | `0.0` | I/O | Take window for `voice_file`. |
| `voice_2_start_s`, `voice_2_end_s` | `0.0` | I/O | Take window for `voice_2_file`. |
| `voice_3_start_s`, `voice_3_end_s` | `0.0` | I/O | Take window for `voice_3_file`. |
| `voice_every_hop` | `off` | I/O | Rides the voice reference on every speaking hop. |
| `reference_video_file`, `reference_video_2_file`, `reference_video_3_file` | -- | I/O | Motion references. |
| `reference_video_start_s`, `reference_video_end_s` | `0.0` | I/O | Take window for `reference_video_file`. |
| `reference_video_2_start_s`, `reference_video_2_end_s` | `0.0` | I/O | Take window for reference video 2. |
| `reference_video_3_start_s`, `reference_video_3_end_s` | `0.0` | I/O | Take window for reference video 3. |
| `reference_video_desc` | -- | I/O | Caption for the above. |
| `reference_video_size` | `MAX` | cand. | Sits in the reference conditioning path alongside `ref_image_size`; unmeasured. |
| `soundtrack` / `soundtrack_file` | -- | I/O | Music input. |
| `music_gain_db` | `-14.0` | I/O | Post-mix. |
| `music_duck` | `0.6` | I/O | Post-mix. |
| `music_fit` | `loop` | I/O | Post-mix. |
| `music_fade_s` | `1.0` | I/O | Post-mix. |
| `music_start_s`, `music_end_s` | `0.0` | I/O | Take window for the soundtrack. |

## Optional inputs -- operational

Non-rendering by design. Listed so that "it does not affect the picture" is on
the record as intent, rather than read later as a defect.

| lever | default | class | note |
|---|---|---|---|
| `cache_hops` | `off` | NONE | Serves completed hops from cache. **Relevant here as a hazard, not a lever:** any two runs compared on texture must have identical cache state, or the comparison is measuring the cache. `seam_probe.py` requires two masters rendered at the same seed for exactly this reason. |
| `cache_budget_gb` | `20.0` | NONE | Cache ceiling. |
| `render_from` / `render_through` | `0` | NONE | Hop range. |
| `dry_run` | `off` | NONE | Plan without rendering. |
| `contact_sheet` | `off` | NONE | Preview grid. |
| `quality` | `final` | RENDER | Encode quality of the delivered master. |

---

## What the table actually says

1. **The evidence is concentrated in about eight rows.** `model`, `steps`,
   `sampler_name`/`scheduler`, `ref_plan`/`start_image_file`, and the refine
   block (`hop_refine`, `refine_steps`, `refine_head`, `refine_audio`) carry
   nearly all of it. Everything else is measured-null, plumbing, or a candidate
   nobody has swept.

2. **The biggest lever is not in the refine block at all.** It is the base
   checkpoint. A turbo base costs ~2.5x seam error and *accelerates*, which is
   precisely the failure mode this project exists to kill. No refine setting
   recovers it.

3. **Two measured nulls are worth as much as the positives**, because they stop
   future sessions re-litigating them: `refine_denoise` (seam geometry is
   invariant to it) and `refine_model` (no visible difference). Both are closed.

4. **The pin block is the largest body of unswept candidates** -- `pin_mech`,
   `pin_renorm`, `pin_noise`, `pin_to_qwen`, `last_frame_guide`,
   `audio_pin_frames`. Upstream predicts `pin_renorm` is a dead end and ranks
   `pin_noise` second-best of everything they tried, and `pin_taper`'s
   retirement is a warning about this exact neighbourhood. If GPU time goes
   anywhere next, it goes here: one lever, 3 hops, pinned seed, flat-wall ROI,
   hop1 -> hop2 step.

5. **Nothing here justifies a deletion.** The closest thing to a removable
   artefact is the legacy `"on" -> "sigma"` remap at `h3_ref_chain.py:3421`,
   which is a compatibility shim rather than a lever. And `widgets_values` is
   positional, so removing any widget breaks every saved workflow. Deletion is a
   separate decision with a separate cost.

## Evidence keys

| key | source |
|---|---|
| [T] | `htc-turbo-is-the-degrader` -- chain pair on `OP_CleanRoom_V2.json`, single variable |
| [W] | plan W2b -- chain_00212/213/214, 6/10/14 steps, one hop, pinned seed |
| [B] | `htc-base-steps-downstream` -- n=1 3-hop probe, seeds unconfirmed |
| [S] | `htc-sampler-split-refine` -- 3-hop samples, judged by eye |
| [Z] | `htc-zoom-not-ratchet` -- chain_00202, dark-mass + hair-top-row probe |
| [H] | `htc-refine-head-trade` -- chain_00183 vs chain_00187 |
| [A] | `htc-refine-audio-hop1` -- 00175/00176/00177, then chain_00179 by ear |
| [E] | `htc-refine-seam-geometry` -- three chains, geometry/texture split |
| [D] | `htc-drift-is-within-hop` |
| [K] | `htc-op-cleanroom-result` -- node-by-node confirmation of OP's stack |
| [L] | `htc-op-blend-latents-ramp` + the chain_00210 within-hop A/B |
| [O] | `htc-origin-thread` -- Motion-Context's author, in replies |
| [U] | `htc-upstream-readmes` -- both upstream READMEs |
| [P] | `htc-pin-taper-retired` -- four-point dose response; the lever is absent from v2.0 |
| [N] | `htc-tone-compensate-not-pinned` |
| [R] | `htc-reset-goal` |
| [C] | `htc-ratchet-measure-flat-wall` |
| [M] | `htc-refine-denoise-is-step-size` -- arithmetic off `BasicScheduler` + `ModelSamplingDiscreteFlow` at H3's `shift=12.0`; source reading, **not a run** |
| [G] | `htc-audio-frame-grid` |

Probes: `tools/texture_probe.py`, `tools/tone_probe.py`, `tools/seam_probe.py`
over `tools/hopcache.py`. The settings behind any run are recoverable from the
output mp4 itself -- HTC embeds the whole workflow JSON under
`format_tags.workflow`.
