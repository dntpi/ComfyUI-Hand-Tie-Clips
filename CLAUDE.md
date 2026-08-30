# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`ComfyUI-Hand-Tie-Clips` chains multiple MiniMax H3 Reference-to-Video generates into one longer clip. Each hop after the first is guided by the previous hop's last overlap frames + audio via stock `MiniMaxH3AddGuide` (requires a ComfyUI build with `MiniMaxH3AddGuide`, ComfyUI PR #15439). It is explicitly **not** the H3-Multishot/airlock pack; do not merge that pack's syntax in here.

Two nodes ship: `HandTieClips` (**H3 Ref2VA Chain**, the whole pipeline) and `HTCContinuityState` (legacy continuity text, superseded — see below).

It lives inside a full ComfyUI checkout (`D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips`) and imports directly from ComfyUI internals (`comfy.model_management`, `comfy.samplers`, `comfy.utils`, `comfy_extras.nodes_minimax_h3`, `comfy_extras.nodes_custom_sampler`, `comfy_extras.nodes_audio`, `nodes`, `folder_paths`). It only runs as a loaded custom node inside that ComfyUI instance — there is no standalone entrypoint, package manager, or test harness.

## The target sampling regime

**Turbo LoRA at 4-8 steps.** That is what the node is built for and what the shipped workflow uses (8 steps, `minimax_h3_ref2v_turbo_4step`). High-step runs (14+) are diagnostic instruments to check whether an effect exists at all; **any change must be re-validated at 8 steps before it counts as working**, because at low step counts seed structure dominates and can swamp conditioning differences. Never report a 14-step result as validation on its own.

## Development workflow

No build step, linter config, or test suite (`pyproject.toml` declares zero dependencies beyond ComfyUI). The dev loop is:

- **Python changes**: restart ComfyUI to reload the node.
- **JS changes** (anything under `js/`): hard-refresh the ComfyUI browser tab.
- Validate by loading `workflows/HandTieClips_Starter.json` and queuing a run. Two signals:
  - the `[HandTieClips]`-prefixed console lines (hop count, shot plan table, reference register table, model-patch log, cache hits, overlap drop, final frame/duration summary);
  - the node's third output, **`info`**, which carries the fully assembled per-hop prompts. Wire it to a Preview Text node to read exactly what each hop sent to the text encoder. The editor shows what you wrote; `info` shows what the compiler made of it, which is not the same thing.

## Architecture

Python modules feed four nodes, plus a DOM editor in `js/`. `h3_ref_chain.py` holds `HandTieClips.run()` and the prompt-assembly helpers; `plan.py`, `directives.py`, `refs.py`, `store.py` and `tone.py` are parsers, compilers and estimators with no ComfyUI imports in their math, which is what makes them testable outside a running server. `routes.py` serves the editor its vocabulary. `preview_node.py` and `tone.py` each register a standalone side node.

### Two independent reference channels — do not conflate them

The core invariant, and the thing most likely to be broken by a careless change:

1. **References** (files in `<ComfyUI input>/h3_refs`, named by each ref's `file`) → bound into the tokenizer as `<Picture 1..N>` *and* into the DiT as `minimax_refs`. Identity/place stills. On `hop_script=next`, they ride **hop 1** unless a ref's `shots` lists later hops. They are never the join pin. Riding a face/outfit plate of a different room on hop 2+ opens a new Ref2VA generate (chain_00030..00034: commercial-kitchen face+outfit beat the residential pin; hop 2 dropped the apron).
2. **The AddGuide pin** (`MiniMaxH3AddGuide.execute`, once per hop after 0) → DiT `minimax_keyframes` at t=0 only. **The text encoder never sees this pin.** The master throws the overlapped frames away after decode.

`pin_to_qwen` (`_attach_pin_to_qwen`) optionally makes the *text encoder* aware of the incoming state, since channel 2 is otherwise invisible to it: `last frame` is **`<Picture 1>`** of the previous hop's last frame (identity stills that still ride shift to Picture 2+), `pin clip` appends the overlap frames as an extra `<Video>` (~2 fps, no audio), `both` does both, `off` neither. Additive to channel 1's slots (max 9 images / 3 videos). **`last frame` is the default and stays that way.** Appending the pin after the stills made it Picture 4 against three stills (chain_00034).

**DiT pin (hop 2+):** keep the previous hop’s **sampler AV latent** and call `MiniMaxH3MotionContext` (already installed) with `context_length` matching overlap (22) and `audio_context_length=24`. That is the Multishot `context_pin` path: no pixel VAE round trip, audio end-aligned on this clip’s timeline. If the node is missing, fall back to `MiniMaxH3AddGuide` on decoded frames and log it. A cache hit no longer forces that fallback: the sampler latent is stored beside the frames and restored on a hit (see the hop store below). Hop 1 `start_image` still uses AddGuide.

**5 s is not a join-validation canvas.** Multishot: 124 f drops the airlock. `00028` 1→2 joined at 8 s. Verify after this pass is 2 × **8 s**. `join=continuous` at 5 s logs a note. A different seed on the 5 s pin-only graph did join (not a strict pass) — 777777 was join-hostile; `seed_per_shot` stays ON. Seed is not a substitute for the latent pin or an 8 s airlock budget.

### 1. Shot plan (`plan.py`)

A plan is one JSON string in the `shot_plan` widget: an ordered list of shots, one per hop. **The shot count IS the hop count**, which removes the whole class of "3 blocks but chains=4" mismatches — when `shot_plan` is non-empty it is authoritative, `hop_script` is forced to `next`, and `chains` is ignored with a log line (`h3_ref_chain.py`, `run()`; search `shot_plan present -> hop_script=next`).

Shot fields (all optional except `beat`): `beat`, `directives`, `prose`, `seed`, `steps`, `duration`, `locked`, `id`. There is no shot-level `refs` field -- a reference activates itself through its own `shots` list in `ref_plan`, and the shot-level one was removed 2026-08-27 (parsed and printed, read by nothing, and silently dropped by the editor). Per-shot `seed`/`steps`/`duration` really are per-shot — sigmas are built per distinct step count behind `sigma_cache` and durations are validated up front so a bad value fails before any sampling.

**`refs` on a shot is parsed and printed but not consumed.** Ref activation comes only from each ref's own `shots` field in `refs.py`. The editor deliberately does not expose it, so nothing implies it works. Either wire it up or drop the field.

`prompt` is the legacy path, read only when `shot_plan` is empty (`_parse_shots`/`_expand_shots` + `hop_script=verbatim|next`).

### 2. Directive compiler (`directives.py`) — the differentiator

`join` / `camera` / `framing` / `pace` / `tail` compile to vetted prose from `VOCAB`, so improving a phrase improves every existing plan at once. `join` is skipped on hop 0, which has nothing to join to; `tail` is the only axis with a default (`ongoing`).

**Phrasing rules — read before editing VOCAB:**

1. **AFFIRMATIVE ONLY.** Sampling runs through `BasicGuider` at cfg 1.0 with no negative branch, so every concept named is additive and cannot be subtracted. "no cut" puts `cut` in front of the encoder. This is not a style preference — it is the load-bearing finding the whole rework was built on.
2. **NO PRONOUNS.** The beat text owns the subject; plural "they" has been observed rendering two people from one reference.
3. **NO ENUMERATED DETAIL.** Naming a prop or garment to control it adds it. Directives cover camera, join, framing, pace, tail only.
4. **ONE SENTENCE PER ENTRY.** These get concatenated; long entries crowd the beat.

`check_coherence` warns (never raises) when `join=continuous` is asked for alongside a framing change with a held camera — that combination implies a cut, so the model picks one.

### 3. Reference register (`refs.py`)

Fixes two real bugs:

- **Ordinal instability.** Core assigns `<Picture N>` 1-based in list order (`comfy/text_encoders/minimax.py:148-202`) and refs are dense-packed, so unplugging a `Load Image` silently renumbers every later one, and any prompt naming a literal ordinal then points at the wrong picture. Here each ref carries a stable `@tag` and a **filename**; ordinals are **derived per hop** and `resolve_tags` rewrites the prose.
- **Subject collapse.** Declaring every picture as a photo of `<Subject 1>` makes the model render the average of two different people. `subject` numbers group pictures per person; `retention` (`fully_preserved` / `partially_copy` / `reference`) generates the carry-over prose.

`run()` keeps a raw `slot -> tensor` map *before* `_collect_ref_images` dense-packs it, because a `@tag` is pinned to one picture. Since 2026-08-28 `slot` is **derived from the ref's position in the rail**, not authored — it is an ordering index, not a socket number. The image dict and the cited ordinals are built from the same `hop_active` list, so they cannot drift apart.

**Only subject-bearing refs are identities.** `_identity_lock` takes `identity_ordinals` and `n_subjects` from the register; without them it falls back to "every wired still is an identity", which is right when nothing better is known but wrong the moment a register is wired — it told the encoder a photograph of a kitchen had a face and hairstyle to match exactly, on every hop. `n_subjects` also drives number agreement: two photographs of one person is one identity, and plural phrasing has been observed rendering two people from one reference.

**`HTCContinuityState` was meant to be retired by this and has not been.** It is still registered, still wireable, still parsed (`_parse_state` / `_state_header`). It cannot express two people at all (one `character_id`, one `characters` key) and names people by an arbitrary id while `<Picture N>` is positional, with nothing joining the two — precisely the subject-collapse setup. Prefer `ref_plan`; the `setting_*` half is the only part `ref_plan` does not cover. `run()` warns when both define identity text, but only when a character field actually holds content — the node emits a `characters` entry for its `character_id` even when every field is blank, so the key's presence means nothing.

### 4. Hop store (`store.py`)

Lossless FFV1 (`rgb48le`) video + a float32 `.npy` waveform + a `.latent.pt` sidecar per hop under ComfyUI's temp dir, enabled by `cache_hops`, LRU-evicted above `cache_budget_gb`. **Needs `ffmpeg` on PATH** or it raises.

**The latent sidecar is what makes the cache useful past hop 1** (added 2026-08-27). It stores this hop's sampler output so a hit can seed the *next* hop's Motion-Context pin. Without it, a hit left `prev_sampled` empty, the next hop predicted the AddGuide fallback, and because the mechanism is in the per-hop key that key no longer matched what was on disk -- so **nothing past hop 1 could ever hit, and the hop after a hit was joined by the weaker mechanism.** `cache_hops=on` was measurably worse than off. Verified in-browser: hop 1 hit, hop 2 logged `previous hop has no sampler latent (cache hit); AddGuide pixel pin`, hops 2-3 re-rendered.

The sidecar is optional in both directions: `has()` ignores it, so entries written before this change still hit (returning `latent=None` and the old fallback), and a latent that will not serialise is logged and skipped rather than failing the hop. It is `torch.save`/`torch.load(weights_only=False)` because `samples` is a `comfy.nested_tensor.NestedTensor` -- a plain Python class holding a tensor list, which `weights_only=True` refuses. Cost is ~2.9% of the entry (measured: 8.4 MB against a ~285 MB video at 0.3 MP / 243f). **Verified end to end 2026-08-27:** fresh render 186.9 s writing three sidecars, then a `cache_budget_gb` nudge re-queue at 17.97 s with all three hops logging `loaded from cache`, zero DiT loads, zero SLA passes, and the same `drift -80 ms` as the fresh run. Invalidation confirmed in the same sitting: LoRA strength 0.800 -> 1.000 produced **zero** cache hits and re-rendered every hop, so `_model_fingerprint` does see a strength change.

**The key chains**: each hop's key mixes in the previous hop's key plus a `chain_salt` of everything constant across the run — canvas, sampler, scheduler, shifts, `pin_to_qwen`, tensor digests of every wired ref / voice / reference video / start image, **and `_model_fingerprint`**, because a hop rendered under different LoRAs or a different attention path is not the same hop. The *pin mechanism* is keyed per hop rather than chain-wide (Motion-Context vs the AddGuide fallback produce different frames, and which one runs depends on whether the previous hop was a cache hit). So editing shot 1 correctly invalidates 2..N. That is correct behaviour and must be surfaced in any UI, or it reads as a bug.

**Anti-ratchet levers were dead until 2026-08-27.** `pin_renorm` and `pin_noise` both ran through `_condition_pin_latent`, which called `.std()` on `latent["samples"]` — a `comfy.nested_tensor.NestedTensor`, which has no `.std()`. Every hop logged `pin conditioning skipped (AttributeError(...))` and both widgets did nothing; the line looked like routine noise next to the other per-hop output. `NestedTensor` is a trap to write against: it *does* have `.float()`, `.cpu()` and `.shape`, but `.shape` returns `tensors[0].shape` — the video component's, silently speaking for both — so the noise draw would have been sized to the video and broadcast onto the audio.

Now conditioned **per component** via `_latent_parts` / `_rebuild_latent_samples`, with `anchor_std` a list of one sigma per stream. Per-stream is also the correct semantics, not just the working one: in a synthetic hop-3 case with video inflated 1.60x and audio 2.51x, the per-stream corrections were 0.6365 and 0.3988 — one global scale would have left the audio ~60% hot. Verified offline against the real class; not yet exercised in a render, because both levers ship off.

**Why 16-bit:** a cached hop's last frame becomes the next hop's Qwen pin and its AddGuide guide, so an 8-bit round trip would make a resumed chain diverge from an uninterrupted one — the cache would change the output, defeating the point.

A shot's `locked` flag pins it to its last render regardless of hash, resolved through `set_pointer`/`get_pointer` keyed on the shot `id` (the content key has moved by definition, so only the pointer can find it).

The master is **preallocated** — `total_frames` is known up front — and slice-written, rather than grown with `torch.cat`, which allocates a fresh full-size tensor every hop while `prev_imgs` and `imgs` are also live. Only the overlap tail stays resident between hops.

### 5. The MODEL wire stays drawn

The speed stack is **four separate nodes in the graph**, not a widget on this one:

    UNETLoader → LTX_lora_loader → H3AdaLNLoRAFix → MiniMaxLowVRAMAttention → H3SLAAttention → HandTieClips

CLIP comes off `LTX_lora_loader`'s second output, not straight from the encoder, or the text-side keys H3 LoRAs carry (`condition_proj`, `token_refiner`) are silently discarded.

A `model_patches` widget that folded all four into this node existed briefly and was **reverted by decision**: it made the graph shorter and the node harder to read, and legibility is the point. Do not re-collapse them. If it ever comes back it belongs behind the editor's advanced section, not as a bare JSON widget.

Note the wire order differs from the pre-rework graph, which ran SLA *before* low-VRAM. SLA's own documentation puts it last; that ordering was researched and is deliberate.

**`_model_fingerprint` exists because of this revert.** With the patches drawn upstream, this node cannot see their settings, so a hop cached under one LoRA stack would be served under another — silently wrong frames. The fingerprint hashes `sorted(model.patches.keys())`, the per-key `strength_patch` / `strength_model` scalars, and the scalar half of `model_options["transformer_options"]`, and that goes into `chain_salt`.

- Patch **values** are tensors and are deliberately not hashed.
- `model.patches_uuid` is *not* usable: `add_patches` assigns a fresh `uuid4()` on every call, so it would bust the cache every run.
- **Known collision:** two different LoRAs touching an identical key set at identical strengths fingerprint the same. Rare; the alternative is a full state-dict walk per run.

### Prompt assembly

`hop_script=next` (forced under a shot plan): hop 1's block is the only full prompt; later hops get the new beat plus a live-frame citation. Register `subject_prose` stays off hop 2+ (it is a `subject_definitions` block). Unscheduled stills stay off hop 2+; `@tag` of a person still resolves to `<Subject N>`. `_assemble_next` composes `state_header` ahead of the beat when `HTCContinuityState` is wired.

**Official H3 tags and prompt methods still work here** (user 2026-08-26). This pack is a hop compiler and a join pin, not a new prompt language. Do not invent beat-keywords or pack-specific interpretation (`Silence.` as a spell, “still talking”, “says one short line”, folklore about leftover duration). Use the MiniMax H3 Ref2VA contract the encoder already knows:

- six fields in order: `subject_definitions` / `summary` / `retention_analysis` / `detailed_description` / `overall_soundscape` / `non_diegetic_music`
- labels `<Subject N>`, `<Picture N>`, `<Video N>`, `<Audio N>`
- speakers `(S1)` and dialogue `<d>[English] …</d>` (exact words; `<cutoff>` if the line overruns the hop; `<scenetrans>` if a line crosses a join)
- `[Shot N]` / `At MM:SS.mmm` inside `detailed_description`
- `overall_soundscape` = ambience + physical + non-verbal only; `N/A` only when the hop is requested silent throughout; do not put dialogue there
- `non_diegetic_music: N/A` when there is no score

`_is_full_h3_prompt` already detects `subject_definitions:` / `integrated_multimodal_description:` and routes through `_continue_prompt`. A shot-card beat may *be* that official block. Short prose on a card is a convenience path, not a dialect. When a hop is a full H3 prompt, do not wrap it in invented closer sentences that fight the official fields.

Hop 2+ carries **no identity header at all**. `_assemble_next` has no `identity_header` parameter (removed 2026-08-27 along with the dead `_extract_identity_header`/`_section_block` pair). Register `subject_prose` is a `subject_definitions` block, and putting one on hop 2+ made it a second Ref2VA generate -- `chain_00033_`.

### 6. The editor (`js/editor/*`, `routes.py`)

The node is driven by a DOM panel, not by hand-written JSON. Five files:

| file | job |
|---|---|
| `js/h3_ref_chain_ui.js` | extension entry, the sample preview, mounts the panel |
| `js/editor/widget_utils.js` | widget hiding, the height guard, vocab fetch, DOM helpers |
| `js/editor/plan_editor.js` | Simple/Shots toggle, shot cards, JSON escape hatch |
| `js/editor/ref_rail.js` | reference register rows and subject blocks |
| `js/editor/run_panel.js` | the shot-independent dials, grouped (added 2026-08-27) |

**`shot_plan` and `ref_plan` remain the only source of truth.** The panel reads them and writes straight back, so a workflow authored in the editor and one typed by hand are the same file. Never add a parallel store.

**The run panel is the same rendering-layer contract, applied to the native widgets.** Every control writes to `widget.value` and then fires `widget.callback` — that callback is not optional decoration, it is how `control_after_generate` stays bound to `seed`. Four groups (`output`, `sampling`, `join & pin`, `cache`) in `GROUPS`; a name the build does not define is skipped, so the list can carry a widget that only exists on a newer Python side. Four things worth knowing before editing it:

- **Ownership decides hiding, not the group list.** `ownedNames()` reports the widgets the panel actually *drew*, and `applyVisibility` hides exactly those. A dial whose type the panel cannot render therefore stays visible as a native widget instead of vanishing off the node. `sync()` must run **before** `setWidgetVisibility`, or a stale build hides something the panel stopped drawing.
- **`chains` and `hop_script` are dropped in Shots mode**, with a note saying why. `run()` ignores `chains` and forces `hop_script=next` the moment a shot plan is present, so drawing them would be two controls that do nothing. The digest reads the hop count from the plan for the same reason.
- **Never read `w.type` or `w.options` directly — use `widgetType(w)` / `widgetOptions(w)`.** `hideWidget` overwrites `w.type` with `"hidden"` and swaps `w.options` for a flagged copy, stashing the originals in `w._h3Saved`. The run panel hid the twenty dials it owned on its first build; when a shot plan loaded and flipped the mode to Shots, the suppression key changed, `build()` re-ran, and every widget now reported type `"hidden"` — so `fieldFor` rejected all of them and the panel emptied itself. It only reproduces on a workflow that *arrives* in Shots mode, since a fresh node builds once and never rebuilds. An empty build also clears `builtFor`, so `sync()` retries instead of caching the failure for the life of the node.
- **Help text is `widget.options.tooltip`**, i.e. the `tooltip` from `INPUT_TYPES`. Do not retype those sentences in JS — same rule as the vocab route, same reason.

The summary line deliberately omits the seed: `control_after_generate` rewrites it after every queue without telling the panel, so a seed shown there would be wrong more often than right. That staleness is also why the panel re-reads every field when it is opened.

Two recipes here are load-bearing and both are ported from `PromptMasterLD/js/claude_prompt.js`:

- **Four-flag widget hiding.** Classic LiteGraph only needed `computeSize = [0,-4]`. Vue Nodes 2.0 filters on `options.hidden | hideInPanel | canvasOnly`; without those flags every hidden dial reappears as a raw form. Invisible in packs with three widgets, unmissable at 21. Multiline STRING widgets are real DOM textareas, so their element has to be hidden too or it floats over the panel.
- **The `_h` fixpoint height guard.** `_arrangeWidgets` runs every frame and grows the node when `panelTop + panelH + 4 > size[1]`; reporting a height derived from `node.size[1]` makes that true forever (~130 px of growth per frame). Report from an independent stored `_h`, updated in `onResize`. `chromeCompute`'s `_measuring` flag measures the frontend's own `computeSize` rather than re-deriving it.

**Dropdown options come from `routes.py`, never from a copy in JS.** `GET /h3_ref_chain/vocab` serves `directives.VOCAB` *with its prose*, so hovering an option shows the exact sentence it will put in the prompt. A second copy in JavaScript would defeat the reason `directives.py` exists. The route is read-only — no writes, no filesystem; the reference-upload route is a separate unbuilt thing.

Simple mode clears `shot_plan` (stashing it in `node.properties.h3_plan_backup` first) so `run()` cannot silently prefer a stale plan over the visible prompt.

### 7. Tone compensation (`tone.py`)

The H3 denoiser applies a tone bias to each generated segment, so the master steps in brightness at every seam. The estimator is ported from [`rkfg/ComfyUI-MiniMaxH3-ToneCompensate`](https://github.com/rkfg/ComfyUI-MiniMaxH3-ToneCompensate) (MIT, as is this pack). Three modes: `frame_shift` (per-frame per-channel additive), `gain_bias` (global affine), `lut` (tone curve). `frame_shift` is the one that suits our case, because the target's first frames are the model's *regeneration* of the source's last frames — same content, not a pixel-wise transform.

**A downstream node cannot do this job, and that is the whole reason `tone_compensate` is a widget on the chain node.** The estimate needs both copies of the overlap: hop N's tail and hop N+1's regeneration of it. The join drops the second (`master_imgs[write_pos:...] = imgs[overlap_n:]`). By the time images leave `run()`, only one copy survives, and a node there would be comparing frames ~0.9 s apart in scene time — measuring content change as much as tone. `HTCToneCompensate` ships anyway for hand-built chains and for A/B work; it is not the fix for this node.

**The call site is the design.** It sits in `run()` where the render and cache-hit paths converge, and its position relative to three neighbours is deliberate:

- **After `hop_store.put`** — the cache holds *raw* hops, so the mode stays out of the hop key. Switching modes costs nothing instead of invalidating ~285 MB per entry. Correct on the way out, hit or miss.
- **Before the master write** — the delivered video is corrected.
- **Before `prev_imgs = imgs[-tail_n:].clone()`** — so hop N+1 is measured against hop N's *corrected* tail, which is what makes each hop's shift cumulative and lands the whole chain on hop 1's tone. **This does not stop the generator drifting, and an earlier version of this note wrongly claimed it did.** Measured 2026-08-28: with tone on and all three hops rendering fresh, hop 3 was generated from a corrected `prev_imgs` and still came out +3.48/255 above raw hop 2 — so it needed a `2d` correction, not `d`. `prev_sampled`, the Motion-Context latent, dominates the conditioning and is never touched by a pixel fix. Correcting here is still right (it is free, and it keeps `prev_imgs` consistent with the master), but the benefit is a clean cumulative repaint, not a cure for the drift at source.

**Two consequences, both deliberate:**

- **Enabling any mode clamps the master to 0..1**, including hop 1, which is otherwise unclamped VAE output. Correcting hops 2+ and not hop 1 would make the master inconsistent with itself.
- **The latent path is untouched.** Motion-Context forwards `prev_sampled`, a latent; pixel correction never reaches it. `_condition_pin_latent` matches per-stream **std**, not mean — and a brightness shift is a shift in the *mean*. So nothing currently anchors latent mean. Known gap; do not build a latent-mean anchor speculatively.

**Measured, 2026-08-28 — the real number, off the hop cache** (`chain_00052`, 3 hops x 243f, overlap 22, 7 steps res_multistep, Motion-Context pin on both joins). `tools/tone_probe.py` pairs hop N's last 22 frames with hop N+1's first 22 -- the model's regeneration of the same content, which survives only in the cache:

| pair | r | g | b | luma |
|---|---|---|---|---|
| hop 1 -> 2 | +0.00995 | +0.00973 | +0.00914 | **+2.45/255** |
| hop 2 -> 3 | +0.01003 | +0.01129 | +0.01085 | **+2.73/255** |
| cumulative | +0.01998 | +0.02103 | +0.01999 | **+5.18/255** |

Three facts follow, and they are the justification for the feature. It is **achromatic** -- r/g/b move together within ~0.001, so it is a luma bias, not a colour cast. It **accumulates linearly** -- ~2.5/255 per hop with nothing pulling it back, so a 5-hop chain lands near +10/255. And it is **brighter**, the opposite sign to the upstream README's "runs darker"; likely because we pin with a Motion-Context latent and never take the decode->encode round trip his workflow does.

**Verified end to end, 2026-08-28** (seed `700637295460319`, `chain_00053` = `frame_shift`, `chain_00054` = `off`, both served from the same raw cache so content is identical):

- **The mode is not in the hop key.** The `off` run hit all three keys written by the `frame_shift` run (`c442d296`, `4854c20f`, `2e4c1e14`) — 17.5 s against 171 s, zero re-renders. Switching modes costs nothing, which is the whole reason the call site sits after `hop_store.put`.
- **Chain drift 5.57/255 -> 0.29/255**, a 95% reduction, and it stops accumulating. Applied shifts measured in the delivered video were `0`, `-1.71/255`, `-5.26/255`, matching the logged notes exactly. Segment 1 came out byte-identical, so hop 1's clamp was a no-op on this render.
- **Do not use "seam -> 0" as the success metric.** It is wrong and it will make you overcorrect. Seam steps went `+1.40 -> -0.16` and `+1.41 -> -2.06`; the second is not an overshoot, it is the scene's own darkening across the cut, which correction should leave alone. The arithmetic closes: uncorrected seam minus true tone bias predicts `-0.31` and `-2.28` against measured `-0.16` and `-2.06`. Judge on cumulative drift, via `tone_probe`.
- **Hop N needs a correction of N-1 times the per-hop bias** (hop 2 got `d`, hop 3 got `2d`), because `prev_sampled` — the Motion-Context latent the next hop is actually generated from — is never corrected. This is a pixel fix for a latent drift, so the required shift grows linearly: ~5/255 by hop 3, ~10/255 by hop 5, ~23/255 by hop 10. Since a positive drift is *subtracted*, the far-end failure is **crushed blacks** clipping at 0. Fine over the 3-5 hops this pack recommends; not a fix for long chains, and the first hard cost attached to the latent-mean gap below.
- **The per-frame drift is not flat across the overlap.** It ramps in over ~6 frames, plateaus, then dips sharply at frame 17 in *both* joins — likely a VAE temporal chunk boundary. `frame_shift` uses `drift[-1]`, which sits in that dip and under-corrects by ~0.38/255; the mean of the last 4 overlap frames would be marginally better and is the obvious tuning knob if the residual ever matters. Upstream's reason for `drift[-1]` (an exact internal join) does not apply here, because the master drops the overlap.

**All three modes measured, 2026-08-28** (seed `700637295460319`, four masters over identical cached pixels: `chain_00054` off, `00053` frame_shift, `00055` gain_bias, `00056` lut):

| mode | worst residual drift | dark-clipped px, seg 3 | notes |
|---|---|---|---|
| off | — | 0.32% | |
| `frame_shift` | +0.38/255 | **0.96%** | `rx-0.0076`, then `-0.0207` |
| `gain_bias` | **0.20/255** | 0.56% | `rx0.9854-0.0035`, then `rx0.9674-0.0071` |
| `lut` | 0.27/255 | **0.07%** | mean `0.4372->0.4284`, then `0.4520->0.4291` |

**On drift removal there is nothing to choose between them** -- all three land inside 0.4/255, below this measurement's noise. Pick on failure mode instead, and `frame_shift` wins on one argument: **every `gain_bias` slope came out below 1 and moving further away (0.985 -> 0.967), which is attenuation bias, not tone compression.** Fitting `s = A*g + C` where `g = s + delta + noise` yields `A = var(s)/(var(s)+var(noise)) < 1`, driven purely by content mismatch between the source and its regeneration. The drift is a *pure level shift* (r/g/b within 0.001), so any slope != 1 is fitting artifact, and it lands in the output as a contrast reduction that deepens along the chain. `lut` has the same defect with 64 free parameters per channel instead of one -- its dark clipping coming out *below* the uncorrected reference means it is reshaping the tone curve, not correcting level. `frame_shift` cannot make that class of error; it can only shift.

The cost is `frame_shift`'s alone: **dark clipping tripled, 0.32% -> 0.96%**, because subtracting a flat 5.26/255 pushes near-black pixels through zero. That is the crushed-blacks endgame already visible at hop 3. A gain-only mode anchored at black (`out = g * mean(src)/mean(tgt)`, no bias term) would fix it without introducing a fitted slope, and is the obvious next mode if long chains ever need one.

**Seam measurements understate it by about a third, so never use them to decide.** `tools/seam_probe.py` on the same master reads `+1.56/255` and `+1.83/255` (sum `+3.39/255`) against the cache's `+5.18/255`: the frames either side of a cut are ~0.9 s apart in scene time and the content change partly cancels the drift. Earlier estimates of `≈2/255` from `chain_00047`/`00050`/`00051` were this same floor, mistaken for the value. Seam numbers are still the right *after* instrument -- `tone_probe` reads the cache, which stores raw pre-correction hops by design -- but only as an A/B between two masters from the same seed and cache, where the contamination is identical in both and cancels. **Note `temp/` is wiped on ComfyUI start, so probe before restarting.**

Offline results (`frame_shift`, synthetic): recovers a planted additive shift to <1e-5; `gain_bias` recovers 0.92/+0.04 as 1.0870/−0.0435 exactly. A simulated 4-hop chain drifts +1.95/255 uncorrected and −0.13/255 corrected, with each hop's fitted shift staying at the planted per-hop bias rather than accumulating. That validates the mechanism, not the real-world magnitude.

### Progress preview (Python ↔ JS)

`_push_preview` sends a `h3_refchain_preview` websocket event (via `PromptServer.instance.send_sync`) with hop status and an optional base64 JPEG of the latest frame. `js/h3_ref_chain_ui.js` listens, resolves the target node (`findNodeByQualifiedId`, subgraph-aware), and updates a DOM widget it mounts on the node. If you change the preview payload shape in Python, update the JS listener in the same change.

**Status is a short label only.** First stress run ended by passing the full `info` dump (summary + every hop prompt) as `status`, so the preview strip became the prompt. Final push is `done · Nf · Ts`. Assembled prompts stay on the `info` output (Preview Text). The JS strip also ellipsizes anything over 80 chars so this cannot happen again from a long status.

**Next (user 2026-08-26, during first stress run):** the in-node preview should become a KJNodes `Model Preview Override`–shaped panel — large “waiting for sample…” frame, idle/status strip, optional a/Δ and step-time strip — not a 260 px strip at the bottom of the chain node. **OK to split it off** as its own node / sidebar on the MODEL (or IMAGE) wire **before** CreateVideo / SaveVideo. Do not fold more chrome onto `HandTieClips`. Reference implementation: `ComfyUI-KJNodes/nodes/preview_override_node.py` + `web/js/preview_override/preview_override.js`. Not built yet; current `mountPreview` stays until this lands.

### First 8-step stress run (user 2026-08-26)

4 hops × 8 s, seed 777777, turbo. Shipped plan: s1 `says one short line` / s2 `still talking` + `join=continuous` `pan_follow` `keep` / s3 drink + `continuous` `push_in` `close` / s4 `match_cut` `pull_back` `wide`.

- **1→2 continuous + keep: clean.** The join directive is doing work; this is the success case.
- **2→3 continuous + push_in + close: small jump.** `check_coherence` does **not** warn here (it only flags continuous + framing change + *held* camera). The jump is still expected at 8 steps: VOCAB concatenates “carry straight on from the pinned frames” with “A close shot, head and shoulders filling the frame,” so `framing: close` asserts the destination as the opening state while AddGuide is still the medium pin from s2. Next compiler pass should compose camera-move + framing as a *landing*, not as the opening, when `join=continuous`.
- **3→4 match_cut: ok.** Control cut is distinguishable from 1→2. Do not treat all three seams as identical.
- **Speech audio outlived the mouth.** s1–s2 ask for talking; s3–s4 do not (drink / lower mug). Picture followed the later beats (mouth busy, then quiet); soundtrack kept the line going for roughly the second half of the master. This is a pack gap, not a seam bug:
  1. AddGuide pins the previous hop’s overlap **audio** at t=0 (`nodes_minimax_h3.py` crops it to remaining duration — the pin is ~0.9 s of *speech*, which seeds the rest of the hop).
  2. cfg 1.0 cannot subtract speech. Omitting “talking” from s3/s4 is a no-op.
  3. Shot-plan compilation emits no `overall_soundscape`. There is no speech/soundtrack axis on `AXES`.
  4. `tail: ongoing` plus `_assemble_next`’s closer (“that action is still underway as the clip ends”) keep whatever the pin started, including a line.
  5. s1’s “says one short line” names speech without giving the words, inside an 8 s hop. H3 invents a line and pads the leftover duration with more speech.
  6. Vague leftover time is what H3 fills. User correction (2026-08-26): **every hop in a chain must carry either enough actual dialogue or a specific mention of silence.** Omitting both is gibberish, especially on hops 2+. “Says one short line” / “still talking” is vague and under-fills an 8 s hop. “No speech” / “no dialogue” is the negation form and also gibberish. The quiet word is **silence**, named in the beat. Spoken hops put the real words in the beat, enough to occupy the duration.
- **End-of-run preview became the prompt dump.** `_push_preview(unique_id, info, …)` stuffed `info` into the status strip. Fixed to a short `done · Nf · Ts`; dump stays on the `info` output.
- **Hop-1 compile order.** ~~`tail` sits before the beat~~ **Fixed.** `compile_shot` splits `lead` (join/camera/framing/pace) from `tail` and appends `tail` last, on hop 1 and hop 2+ alike (`directives.py`, `compile_shot`). Verified 2026-08-27. Two residuals survive: ESTABLISH still prepends “Live-action, natural light, one continuous take.” on hop 1 even when the beat already opens “Live-action…”, and a hop-1 beat that *is* a full six-field H3 block returns early and drops every directive including `tail`.

**Contract (confirmed 2026-08-26, chain_00030..00032):** a complete six-field H3 / Ref2VA prompt is one generate. `[Shot 1]` is the opening of *that* generate. Hop 1 may be official. Hop 2+ must be a continuation beat only — pin-open + new action + sound. Wrapping hop 2 as another full official block (join splice, pin-open first, `keyframe completion`, “first frame of [Shot 1]”) still hard-cut at f124 onto the outfit still. `compile_shot` / `_assemble_next` flatten an official hop 2+ block to action + `overall_soundscape` + `non_diegetic_music` and drop `subject_definitions` / `summary` / `retention_analysis` / leading `[Shot 1]`.

**Join pass (user 2026-08-27, chain_00037_.mp4).** Smooth chain. Console: `2 hop(s), 192f (8.0s) @ 960x544`, `8 steps res_multistep/beta`, hop 2 `Qwen last frame -> <Picture 1>`, `0 identity stills`, `Motion-Context pin: previous hop latent (22f picture, 24f audio, trim 22)`, master **362 f / 15.1 s**. Register: all three stills `shots 1`. That is the working join recipe at 0.5 MP / 8 step: latent pin + pin-only hop 2 + 8 s airlock budget + hop-2 paragraph (no official fields). Native mask (Phase 2) is not needed for this seam.

**Next tests** (pack, not kitchen beats):

1. **Cache fingerprint.** LoRA strength 1.0 → 0.9, re-queue. Both hops re-render. (Before the latent sidecar, a hop-1 hit made hop 2 log `AddGuide pixel pin`; that line now means the entry predates the sidecar or its latent failed to load.)
   **Note ComfyUI's own node cache sits in front of this one:** re-queueing with *nothing* changed skips `run()` entirely (no `[HandTieClips]` lines at all, ~9 s), so it tests nothing. Nudge `cache_budget_gb` to force re-execution -- it is in neither `chain_salt` nor the hop key, so every key stays byte-identical.
2. **Chained re-roll.** Revert strength. Change one word in hop 2 only. Hop 1 cache hit, hop 2 renders.
3. Confirm the same join at **1.0 MP** (`1280×736`) before calling it shipped. Keep `seed_per_shot`. 777777 was join-hostile on the old 5 s pin-only graph; do not treat that seed as the only one.

**chain_00030_.mp4 / chain_00031_.mp4 / the keyframe-completion re-queue.** Hard cut at hop 2, f124, onto the outfit still’s commercial kitchen. Drink beat ran; pin did not. Official hop 2 cannot join at 8 steps.

**chain_00033_.mp4.** Short hop-2 drink beat, flatten did not fire (card was already short). Still hard-cut. Two findings: (1) `_assemble_next` still prepended register `subject_prose` (`subject_definitions:` + `retention_analysis:`), so hop 2 was a Ref2VA generate again. (2) Console: `8 steps euler/simple`, not `res_multistep/beta`. `tail: settle` also led the compiled beat, so hop 2 opened on “eases to a rest.” Header stripped; tail moved after the beat.

**chain_00034_.mp4.** Header-stripped hop 2, `8 steps res_multistep/beta`. Still hard-cut; apron gone on hop 2. Console: `Qwen last frame -> <Picture 4>`, `3 identity stills`. Face still (`h3_stress_hero_face.jpg`) is the same commercial kitchen as the outfit plate, grey shirt, **no apron in the crop**. Outfit still has the apron. Hop 2 followed the face plate (place + wardrobe), not the pin. Stills without `shots` stay off hop 2+ continue; live frame is Picture 1.

**H3 soundtrack (official methods, user 2026-08-26):** do not invent a pack dialect for quiet vs speech. Dialogue belongs in `detailed_description` as `(S1)` + `<d>[English] …</d>` with the actual words. Ambience/physical/non-verbal belong in `overall_soundscape`. Requested silence throughout a hop is `overall_soundscape: N/A` (the official complete-silence token), not the English word “Silence” stuffed into a beat. “No speech” / “no dialogue” is still negation and still gibberish. `_assemble_next` currently does not emit these fields; when the beat already contains them, leave them alone.

**Iteration canvas (user 2026-08-26):** further tests run at **0.5 MP** (`960×544` landscape) for speed. 8 steps, seed 777777, 4×8 s, overlap 0.9 s stay. Seam times do not move (still master frames 192 / 362 / 532). Resolution is in `chain_salt`, so the 1.0 MP cache will not hit. A 0.5 MP pass validates join / speech / cache behaviour, not 1.0 MP texture. Confirm anything that ships at 1.0 MP (`1280×736`) on that canvas before calling it done.

**chain_00028_.mp4 (0.5 MP, 960×544, 702 f / 29.25 s, 8 step).** Same stress plan. Frame-walked.

- 1→2 at f192: continuous. Then **an inside-hop cut at f220–228 (~9.2–9.5 s, ~1.3 s after the join)** — over-shoulder + mug → frontal talking head, mug leaves frame. Same class as the old 1.8 s inside-hop cut. Shot 2’s `still talking` beat beat `pan_follow` + the walk.
- 2→3 at f362: join itself is continuous (window, mug out). Close-up lands later inside hop 3 (~f432–456) on the drink, not at the seam.
- 3→4 at f532: still the drink CU. `match_cut` + `wide` does **not** cut at the seam; pull-back to wide is ~f576–624. Control cut is late / soft.
- Soundtrack is speech-level for almost the whole clip (integrated ~−11 LUFS). Brief dip at the 1→2 join; no quiet second half. Mouth can drink (f456–504) while the track keeps talking.
- Mug survives hops 1, 3, 4; missing during the hop-2 talking-head. Identity holds at 0.5 MP (cross necklace from locked text rendered).
- A/V: audio 29.131 s vs video 29.250 s (~119 ms short, ~40 ms × 3 hops).

**chain_00029_.mp4 (0.5 MP, explicit beats + silence).** Same 702 f / 29.25 s.

- Soundtrack: wall-to-wall speech is gone. Hop 1 line-burst ~4.4–7.3 s (mouth open at f120). **Random line at 15.41–16.71 s** (user-confirmed): 0.33 s after the 2→3 seam, 1.3 s of speech inside hop 3’s *kept* audio — the 0.9 s pin was already trimmed, so this is hop 3 inventing a line, not hop-2 leak. Hop 3 beat names Silence once, then “a swallow”, then `_assemble_next` still *ends* on “that action is still underway as the clip ends.” One silence mention in the middle of the prompt does not occupy an 8 s hop. Later hops also have a late spike (~23 s).
- Mug path: chest height on the walk; lifts to the mouth only on hop 3; no reach to the glass. Opening frame still has the mug **on the counter** because the kitchen still itself shows it there (`h3_stress_kitchen.jpg`) — the photograph is additive, not only the desc.
- 1→2 at f192: continuous, mug at chest. Camera swings toward the face ~f216 (mug stays). **Not** the 00028 talking-head cut.
- Place break inside hop 2, f240→f270 (~10.0–11.25 s): residential window kitchen → the **outfit still’s commercial kitchen** (stainless, SANITIZER buckets, range). `h3_stress_hero_outfit.jpg` is that room. Kitchen ref is `retention: reference` (weak); outfit is a subject-bearing still of a *different set*. Walking off the pin lets the outfit photograph’s room take over. Hops 3–4 stay there. 2→3 join is continuous *in the wrong room*. 3→4 is again a delayed pull-back, not a match_cut at f532.

## Writing beats (verified against live renders, 2026-08-27)

The first end-to-end runs of v1.1 produced three failures that all trace to the
same root: **sampling runs at cfg 1.0 with no negative branch, so the prompt is
purely additive.** Nothing can be subtracted by naming it. Each was diagnosed
from the assembled prompt and fixed by rewriting a beat, not by changing code.

**1. A reference is only used where the action line points.**
`retention_analysis` described `<Picture 3>` correctly ("the layout, surfaces,
and light carry over as the setting") and the model still took its background
from `<Picture 2>`, a full-length person still that carries its own room. The
register is a static header; the beat is what drives the frame. Writing the tag
into the action line fixed it:

> The cook stands at the counter in **@kitchen**, looks up from the chopping board...

Phrase the tag as a depicted place, not a container. `in @kitchen` alone once
produced a literal composite -- the figure pasted onto the reference photo with
the counter cutting through the body.

**2. Naming the thing you want to end adds it.**
`The cook stops talking, ...` kept her talking. "stops talking" and "talking"
condition on nearly the same thing. Describe the wanted state as a pose plus a
sound: `with her lips closed` gives the video branch something renderable, and
naming room tone ("the hum of the refrigerator") gives the *audio* branch a
target that is not a voice. H3 always generates audio for the full hop -- silence
has to be described as a sound, never as an absence. This is the same finding as
the "No speech / no dialogue is negation" bullet below, reached independently.

**4. `tail` now reaches hop 2+ (fixed 2026-08-27).**
`_assemble_next`'s closer used to end unconditionally on *"that action is still
underway as the clip ends"*, and being the last sentence it overrode the shot's
own `tail` directive -- `settle` and `hold` were unreachable on every hop after
the first. On the final hop nothing absorbs that instruction, so the model
invented late action to satisfy it: a line of dialogue in the last second of a
3-hop chain, and the ~23 s spike noted further down this file. The closer now
selects its terminal clause from `tail`, and every variant still ends on the
clip's terminal *state* rather than on the photographs, preserving the ordering
rule documented at the `return`. An absent or unrecognised `tail` keeps the old
"still underway" wording.

**5. `overall_soundscape` is hop-1 only.**
`_assemble_next` strips `^(overall_soundscape|non_diegetic_music):` on hop 2+,
leaving the value inline as prose, because official field names there made the
model start a new Ref2VA generate (`chain_00030..00034`). So ambience on
continuation hops is prose-only and is tuned by word choice. Choose narrowband,
specific sources: "faint street noise through the window" is broadband and
renders as a 5-second hiss; "the low hum of the refrigerator" does not.

**3. A state change belongs at the END of the previous hop.**
Affirmative phrasing alone did not stop the dialogue. Hop 2 was configured
`tail=ongoing` with the beat `...turns toward the window, still talking`, and
`_assemble_next` opens every hop 2+ with *"The clip opens already in progress
from the pinned frames... holds for a short beat... and only then the next
action begins."* With `audio_pin_frames=24`, the pin hop 3 inherits is literally
mid-sentence. No wording in hop 3 can undo a boundary defined as mid-speech.

The fix was in **shot 2**, not shot 3:

> The cook sets the knife down, turns toward the window, and **finishes her last word** as she looks out.

Generalises: to be silent/still/elsewhere in hop N, arrive at that state before
hop N-1 ends. Hop N opens by holding what it was handed.

## Known constraints (don't "fix" without reading this)
- **The join pin is a latent slice, not a VAE round trip** (corrected 2026-08-27). Hop 2+ passes the previous hop's sampler AV latent to `MiniMaxH3MotionContext` as `context_latent`, which never decodes and end-aligns the audio window. Core's `MiniMaxH3AddGuide` does take `IMAGE`, not `LATENT` (`nodes_minimax_h3.py:177`), so it remains the *fallback* -- used when Motion-Context is absent, the overlap has no matching `context_length`, or the cached entry has no latent sidecar (pre-2026-08-27 entries only; the latent is stored now). Do not "simplify" back to AddGuide-only: it is what `chain_00037_`/`chain_00038_` were pinned against.
- Overlap is fixed per-run to 22 f / 5 f / 39 f. A longer overlap does not fix continuity and can instead pin whatever content is in that longer tail — "increase overlap" is not a general fix for join artifacts.
- Each join hard-cuts video but crossfades audio ~40 ms, accumulating ~40 ms/hop of A/V desync. Known and deliberately deferred.
- `ref_image_size="max"` (2048 short-edge) is slower per step than `"match"` — an explicit tradeoff, not a default to silently upgrade.
- Fewer than 3 wired reference stills triggers a warning log; unconnected `Load Image` nodes do not count as wired.
- Soundtrack is official H3, not a pack dialect: `(S1)` + `<d>…</d>` for lines, `overall_soundscape` for ambience/physical (or `N/A` for requested silence). Do not invent beat-keywords. “No speech” / “no dialogue” remains negation/gibberish.

## The engineering log

Sections 8 onward -- the dated record of what was built, measured, and got
wrong -- live in [`docs/DEVLOG.md`](docs/DEVLOG.md). They are history, not
instructions. This file is the brief; the log is why the brief says what it
says, and it is worth reading before changing any of it.

Most recent: **section 21**, the first ComfyUI session, where two of the seven
features shipped in 0.4.0 turned out to be broken in ways no offline test could
have caught.
