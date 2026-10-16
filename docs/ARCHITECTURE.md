# ARCHITECTURE

The per-module reference for `ComfyUI-Hand-Tie-Clips`. Split out of `CLAUDE.md`
on 2026-09-20, when that file passed the 40k characters the harness will load
and started being truncated -- which meant the brief that is supposed to be read
first was being read in part.

The split is by *when you need it*. `CLAUDE.md` keeps what governs every task:
the target sampling regime, the two reference channels, the prompt contract, the
known constraints, the dev loop. This file keeps what you need only while you
are inside one module, and each section here is pointed at from the matching
section there. Everything below was moved verbatim; where it disagrees with
`CLAUDE.md`, `CLAUDE.md` is the brief and this is the detail behind it.

Dated measurement records live in [`DEVLOG.md`](DEVLOG.md), which is history
rather than instruction.

## Releasing

Moved from `CLAUDE.md`, "Pushing, when the change touches `docs/img/`".

### Pushing, when the change touches `docs/img/`

`origin` carries **two push URLs** (GitHub and HuggingFace) so one `git push origin main` mirrors both. Git LFS does not follow that: it uploads objects to the first URL only, so GitHub gets a pointer with no object behind it and rejects the push with `GH008: Your push referenced at least 2 unknown Git LFS objects`. Push the objects first:

```
git lfs push https://github.com/dntpi/ComfyUI-Hand-Tie-Clips.git main
git push origin main
```

**`git lfs push <url> <branch>` uploads only the objects reachable from that
branch**, so merge first, then push the objects, then push the branch. Getting
that order wrong is how two images sat on `v2` for a release cycle while the
`main` push reported success.

Three further traps behind that one, all silent:

- **`raw.githubusercontent.com` does not resolve an LFS pointer.** It serves the
  131-byte pointer file as `200 OK` / `text/plain`, so every `<img>` breaks with
  no error anywhere. Use `media.githubusercontent.com/media/<owner>/<repo>/<ref>/<path>`,
  which is what the README and the `Icon`/`Banner` fields point at.
  `github.com/<o>/<r>/raw/` behaves like `raw.`, not like `media.`.
- **HuggingFace refuses plain binaries regardless of size** -- it rejected PNGs
  of 262-545 KB, not just the >10 MB the old `.gitattributes` comment assumed.
  Anything binary under `docs/img/` must be LFS or the mirror push fails.
- **A missing object is not visible from this machine.** Tree, pointer and
  working copy are all correct locally whether or not the upload happened; only
  a fetch tells you. `tools/check_lfs_urls.py` fetches every image the README
  and the `Icon`/`Banner` fields reference and asserts each comes back 200, not
  starting with a pointer header, and the same byte count as disk -- which is
  what catches the `raw.` case, since that one is a successful response. It is
  the only checker that needs the network, so it is not in `check_all.py`: run
  it after the push and before `comfy node publish`.

## Hop store

Moved from `CLAUDE.md` section 4.

### 4. Hop store (`store.py`)

Lossless FFV1 (`rgb48le`) video + a float32 `.npy` waveform + a `.latent.safetensors` sidecar per hop under ComfyUI's temp dir, enabled by `cache_hops`, LRU-evicted above `cache_budget_gb`. Encoded in process with PyAV, which ComfyUI already depends on -- no `ffmpeg` binary on PATH is needed, and nothing here shells out.

**The latent sidecar is what makes the cache useful past hop 1** (added
2026-08-27). It stores this hop's sampler output so a hit can seed the *next*
hop's Motion-Context pin. Without it a hit left `prev_sampled` empty, the next
hop predicted the AddGuide fallback, and because the mechanism is in the per-hop
key that key no longer matched what was on disk -- so **nothing past hop 1 could
ever hit, and the hop after a hit was joined by the weaker mechanism.**
`cache_hops=on` was measurably worse than off.

The sidecar is optional in both directions: `has()` ignores it, so entries
written before this change still hit (returning `latent=None` and the old
fallback), and a latent that will not serialise is logged and skipped rather than
failing the hop. It is **`.latent.safetensors`**, not `torch.save`: `samples` is
a `comfy.nested_tensor.NestedTensor` and `latents.parts()` already decomposes it
into plain tensors, which is a safetensors payload. Other nested members go the
same way -- `master_audio_file` sets `noise_mask` as `NestedTensor((vmask,
amask))`, and refusing that used to log `latent not cached (not representable
without pickling)` on every locked hop.

**Verified end to end 2026-08-27:** a fresh 186.9 s render writing three
sidecars, then a `cache_budget_gb` nudge re-queue at 17.97 s with all three hops
`loaded from cache`, zero DiT loads, zero SLA passes and the same `drift -80 ms`.
Invalidation confirmed in the same sitting: LoRA strength 0.800 -> 1.000 produced
**zero** hits, so `_model_fingerprint` does see a strength change. Sidecar
details: DEVLOG sections 50-52.

**The key chains**: each hop's key mixes in the previous hop's key plus a `chain_salt` of everything constant across the run — canvas, sampler, scheduler, shifts, `pin_to_qwen`, tensor digests of every wired ref / voice / reference video / start image, **and `_model_fingerprint`**, because a hop rendered under different LoRAs or a different attention path is not the same hop. The *pin mechanism* is keyed per hop rather than chain-wide (Motion-Context vs the AddGuide fallback produce different frames, and which one runs depends on whether the previous hop was a cache hit). So editing shot 1 correctly invalidates 2..N. That is correct behaviour and must be surfaced in any UI, or it reads as a bug.

**`voice_every_hop`** (RUN, default `off`). Whether `voice_file` 1-3 stay cited as
`<Audio 1..3>` after hop 1. `off` is the shipped gate at `_assemble`: under
`hop_script=next` the timbre clip is a hop-1 reference and later hops inherit the
voice through the audio pin. `speaking` rides every hop whose block carries a
spoken line (`planner.spoken_spans` or a `<d>` tag) and skips the quiet ones,
which is precisely the chain_00038 failure the gate protects against: an uncited
timbre clip on a hop with no line fills the leftover frames with that recording.
`on` rides unconditionally and re-opens that risk. Per-hop, so the existing
`voice_on` key field re-keys exactly the hops it changes; `off` is byte-identical
to 2.0.0.

**`master_audio_file`** (MEDIA, empty = off). One continuous take every hop
lip-syncs to, sliced on the picture clock (hop 1 starts at 0.00 s), encoded on
the 40 Hz audio-latent grid, frozen with a NestedTensor `noise_mask` so only the
picture is denoised. Delivered audio is a passthrough, no VAE round trip. The
beat still needs the words in `<d>[English] ...</d>` -- unmatched text can pull
the mouth off the take. Empty string does not move a cache key; a set file does.
A restart hop's window follows the master *head*, not a uniform stride, or lips
lock 0.9 s early of the picture.

**Anti-ratchet levers were dead until 2026-08-27.** `pin_renorm` and `pin_noise`
both ran through `_condition_pin_latent`, which called `.std()` on
`latent["samples"]` -- a `NestedTensor`, which has no `.std()`. Every hop logged
`pin conditioning skipped (AttributeError(...))` and both widgets did nothing, in
a line that looked like routine per-hop output. **`NestedTensor` is a trap to
write against:** it *does* have `.float()`, `.cpu()` and `.shape`, but `.shape`
returns `tensors[0].shape` -- the video component's, silently speaking for both
-- so the noise draw would have been sized to the video and broadcast onto audio.

Now conditioned **per component** via `_latent_parts` / `_rebuild_latent_samples`,
with `anchor_std` a list of one sigma per stream. Per-stream is also the correct
semantics, not just the working one: with video inflated 1.60x and audio 2.51x,
the per-stream corrections were 0.6365 and 0.3988 -- one global scale would have
left the audio ~60% hot. Verified offline against the real class; not yet
exercised in a render, because both levers ship off.

**Why 16-bit:** a cached hop's last frame becomes the next hop's Qwen pin and its AddGuide guide, so an 8-bit round trip would make a resumed chain diverge from an uninterrupted one — the cache would change the output, defeating the point.

A shot's `locked` flag pins it to its last render regardless of hash, resolved through `set_pointer`/`get_pointer` keyed on the shot `id` (the content key has moved by definition, so only the pointer can find it).

The master is **preallocated** — `total_frames` is known up front — and slice-written, rather than grown with `torch.cat`, which allocates a fresh full-size tensor every hop while `prev_imgs` and `imgs` are also live. Only the overlap tail stays resident between hops.

## The editor

Moved from `CLAUDE.md` section 6.

### 6. The editor (`js/editor/*`, `routes.py`)

The node is driven by a DOM panel, not by hand-written JSON. Six files:

| file | job |
|---|---|
| `js/h3_ref_chain_ui.js` | extension entry, the sample preview, mounts the panel |
| `js/editor/widget_utils.js` | widget hiding, the height guard, vocab fetch, DOM helpers |
| `js/editor/plan_editor.js` | Simple/Shots toggle, shot cards, JSON escape hatch |
| `js/editor/ref_rail.js` | reference register rows and subject blocks |
| `js/editor/run_panel.js` | the shot-independent dials, grouped (added 2026-08-27) |
| `js/editor/writer_bar.js` | the optional LLM plan writer, collapsed (added 2026-08-30, ALPHA) |

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

## The plan writer

Moved from `CLAUDE.md` section 6b.

### 6b. The plan writer (`llm.py`, `planner.py`) — ALPHA

Optional, off until configured, and **never** on the execution path. The panel's
WRITE section posts a brief to `POST /h3_ref_chain/plan`; the route generates,
validates, repairs and returns two JSON strings. The bar **holds them as a
draft** until Accept, which then writes `shot_plan` and `ref_plan` exactly as
a paste would. Discard leaves the cards alone. A plan silently rewritten under
you is worse than no plan. `run()` is untouched, there is no new
`INPUT_TYPES` entry and no `IS_CHANGED` change, so a queued graph stays
deterministic and renders with the network unplugged.

Four rules, each of which cost something to learn:

- **No child processes, ever.** 0.4.1–0.4.3 were registry-Flagged under
  `python_command_injection_risk` until `store.py` stopped spawning one.
  (The banned call is named in `tools/check_publish.py`, not here: the
  registry scanner matches the bare token in prose, so documenting it by name
  is itself a Flag — which is why this paragraph reads the way it does.)
  PromptMasterLD's unload ladder ends in `lms unload --all`; that rung is
  deliberately absent here and the four HTTP ones are enough. `unload_all` is
  the killswitch that rung existed for: it lists loaded models over HTTP and
  walks each through the same ladder. `unload` alone was not enough, because
  it only ever targets the configured model -- and the three cases that
  actually OOM a render are the ones where that is not what is resident (the
  checkbox was off, the write failed early, or JIT loaded something else). The sibling pack
  can afford it because it has no `pyproject.toml` and is never scanned.
- **No blocking I/O.** These functions are awaited inside aiohttp handlers, so
  `urllib.request` — which is what PromptMasterLD uses from its worker thread —
  would freeze ComfyUI's event loop for the length of a generation.
- **`validate()` re-runs the real checkers.** `parse_plan`, `parse_ref_plan`,
  `resolve_tags`, the duration table, `check_coherence`, `check_place_handoff`,
  `check_over_delivery`, `refs.check`. A second copy of the rules is a second
  thing to get wrong. Errors are what the node would *reject* and go back to the
  model; warnings are lints, shown but never retried — a model asked to fix a
  lint rewrites the parts that were fine.
- **`wired` is derived from the file list, not left empty.** A ref whose picture
  is not wired is not active on any hop, so `resolve_tags` then rejects a tag
  that was perfectly declared. Getting this wrong makes every good plan look
  broken; `tools/check_planner.py` caught exactly that.

`write_plan()` takes its completion function as an **argument** so the loop runs
against a scripted fake with no server. That is the whole of
`tools/check_planner.py`, and it is the only proof the repair path works that
does not need a GPU, a server and someone watching. The fault it plants is the
real one from the A/B: a beat citing `@kitchen` that the register never declares.

Two LM Studio specifics worth not rediscovering: `/v1/models` lists what is
*installed*, not what is in memory, so the dropdown reads `/api/v0/models` for a
`state` field and marks loaded models `●` — otherwise the first Write plan fails
with `HTTP 400: Model unloaded by user or API request`, which the list implied
was impossible. And a reasoning model returns the plan in `reasoning_content`
with an **empty** `content`; both payload switches are sent, and a `/no_think`
retry covers builds that ignore them.

Settings live in a gitignored `htc_llm.json` beside the node — a machine
property, never a widget, so a shared workflow cannot point at someone else's
server. No API keys: local servers only.

## SWAP

Moved from `CLAUDE.md` section 6c.

### 6c. SWAP (`js/editor/video_swap.js`, `planner.write_swap_plan`)

SWAP is a standalone one-hop guided identity-swap helper, not a chain writer.
It writes its own `swap_script` (`[98]`, the complete six-field H3 contract
prefixed with `body_swap:` or `head_swap:`) and `swap_refs` (`[99]`, the plate
whitelist: `""` = register default, `"[]"` = no plates for `keep_person`,
`"tag1, tag2"` = explicit ordered plates) plus `reference_video_desc`, and may
bind MEDIA clip slot 1. It must not write `shot_plan` (leaving the user's
multi-shot chain untouched), must not write `ref_plan`, must not add, remove or
rewrite rail rows, and must not share WRITE's system prompt or WRITE's
generate/validate/repair loop. Clearing SWAP (`Clear swap`) empties
`swap_script` and `swap_refs` and hands the node back to `shot_plan`. A change
that is about hops 2+, the pin, the tone anchor, the audio lock, or the hop
cache is not a SWAP change.

When `swap_script` is non-empty, `run()` re-runs `swap_contract_errors(swap,
shape_only=True)` (fail-closed), clones `model` via `_swap_lora` to auto-load
`minimax_h3_character_swap_v1.0_ref2va_r64_gl.safetensors` (`body_swap:`) or
`minimax_h3_head_swap_v1.0_r32.safetensors` (`head_swap:`) @ `1.0` with
`H3AdaLNLoRAFix(mode="port")` before `_model_fingerprint(model)`, lays
`reference_video[:render_length]` onto `MiniMaxH3AddGuide(frame_idx=0)` for
guide-latent (`_gl`) alignment, and renders 1 hop (`hop_script="verbatim"`,
`chains=1`, `ht` forced `False`).

The generate/validate/repair *loop* (`planner._repair_loop`) is
mechanism, extracted so SWAP did not fork WRITE's policy loop to get one.

**Only SWAP calls it.** `write_plan` still runs its own copy, because it
rewrites the conversation between attempts -- remapping rail tags by
filename, merging the register, restoring pinned `mp` and `file` -- before
anything validates, which `consume` cannot express. Two loops, and a change
to the repair protocol has to land in both. SWAP's instruct
(`prompt_pack/SWAP_PROMPT.md`) and validator (`validate_swap` /
`swap_contract_errors`) are separate policy. WRITE's `validate()` and
`system_prompt()` are not on this path. `tools/check_swap_boundary.py`,
`tools/check_swap_instruct.py`, and `tools/check_swap_render_path.py`
enforce the contract and wiring invariants without a browser or GPU.

## Tone compensation

Moved from `CLAUDE.md` section 7. The nine-hop study behind
`tone_compensate=anchor` postdates these numbers and is DEVLOG section 25.

### 7. Tone compensation (`tone.py`)

The H3 denoiser applies a tone bias to each generated segment, so the master steps in brightness at every seam. The estimator is ported from [`rkfg/ComfyUI-MiniMaxH3-ToneCompensate`](https://github.com/rkfg/ComfyUI-MiniMaxH3-ToneCompensate) (MIT, as is this pack). Three modes: `frame_shift` (per-frame per-channel additive), `gain_bias` (global affine), `lut` (tone curve). `frame_shift` is the one that suits our case, because the target's first frames are the model's *regeneration* of the source's last frames — same content, not a pixel-wise transform.

**A downstream node cannot do this job, and that is the whole reason `tone_compensate` is a widget on the chain node.** The estimate needs both copies of the overlap: hop N's tail and hop N+1's regeneration of it. The join drops the second (`master_imgs[write_pos:...] = imgs[overlap_n:]`). By the time images leave `run()`, only one copy survives, and a node there would be comparing frames ~0.9 s apart in scene time — measuring content change as much as tone. `HTCToneCompensate` ships anyway for hand-built chains and for A/B work; it is not the fix for this node.

**The call site is the design.** It sits in `run()` where the render and
cache-hit paths converge, and its position relative to three neighbours is
deliberate: **after `hop_store.put`**, so the cache holds *raw* hops and the mode
stays out of the hop key (switching modes costs nothing instead of invalidating
~285 MB per entry); **before the master write**, so the delivered video is
corrected; and **before `prev_imgs = imgs[-tail_n:].clone()`**, so hop N+1 is
measured against hop N's *corrected* tail.

**That last one does not stop the generator drifting, and an earlier version of
this note wrongly claimed it did.** Measured 2026-08-28: with tone on and all
three hops fresh, hop 3 was generated from a corrected `prev_imgs` and still came
out +3.48/255 above raw hop 2 -- so it needed a `2d` correction, not `d`.
`prev_sampled`, the Motion-Context latent, dominates the conditioning and is
never touched by a pixel fix. Correcting here is still right (it is free, and it
keeps `prev_imgs` consistent with the master), but the benefit is a clean
cumulative repaint, not a cure for the drift at source.

**`tone_anchor_ref`** (`hop1` default / `still`) is what `tone_compensate=anchor`
pulls toward. `hop1` is the original behaviour, reasoning that hop 1 is the one
tone nothing has drifted into yet. An outside ten-run 9-hop study measured that
this is false: before any relay the still sat at chroma 33.6 / b* 26.6 / fine
detail 1.00 against hop 1 at 30 / 22 / 0.72-0.99. Hop 1 is the first casualty, so
a chain anchored on it holds a target that already fell short. `still` holds
`start_image`, which does not drift, and pulls hop 1 itself -- the only way that
gap closes. Needs `start_image_file` (refused on the queue otherwise). Under the
Motion-Context join the correction still reaches only the delivered frames, not
the next hop's pin; `pin_mech=addguide` is what closes that loop.

**Two consequences, both deliberate:**

- **Enabling any mode clamps the master to 0..1**, including hop 1, which is otherwise unclamped VAE output. Correcting hops 2+ and not hop 1 would make the master inconsistent with itself.
- **The latent path is untouched.** Motion-Context forwards `prev_sampled`, a latent; pixel correction never reaches it. `_condition_pin_latent` matches per-stream **std**, not mean — and a brightness shift is a shift in the *mean*. So nothing currently anchors latent mean. Known gap; do not build a latent-mean anchor speculatively.

**Measured 2026-08-28 off the hop cache** (`chain_00052`, 3 hops x 243f,
Motion-Context pin on both joins, via `tools/tone_probe.py`). Three facts, and
they are the justification for the feature. The drift is **achromatic** -- r/g/b
move together within ~0.001, so it is a luma bias and not a colour cast. It

**accumulates linearly**, ~2.5/255 per hop with nothing pulling it back. And it
is **brighter**, the opposite sign to the upstream README's "runs darker",
likely because we pin with a latent and never take his decode->encode round trip.

**Verified end to end 2026-08-28** (`chain_00053` `frame_shift` against
`chain_00054` `off`, same seed, both served from the same raw cache): chain
drift **5.57/255 -> 0.29/255**, a 95% reduction, and it stops accumulating.

**The mode is not in the hop key** -- the `off` run hit all three keys the
corrected run had written, 17.5 s against 171 s, which is the whole reason the
call site sits after `hop_store.put`. **Hop N needs a correction of N-1 times
the per-hop bias**, because `prev_sampled` -- the latent the next hop is
generated from -- is never corrected. A positive drift is *subtracted*, so the
far-end failure is **crushed blacks**. Linear to about hop 4; measured flat at
26-34/255 through hops 7-10 rather than running away, though no 10-hop chain
holding one location has been measured.

**Do not use "seam -> 0" as the success metric.** It is wrong and it will make
you overcorrect: part of a seam step is the scene's own change across the cut,
which correction should leave alone. Judge on cumulative drift, via `tone_probe`.

**On drift removal there is nothing to choose between the three modes** -- all
land inside 0.4/255, below the noise. Pick on failure mode. The drift is a pure
level shift, so any fitted slope != 1 is artifact: every `gain_bias` slope came
out below 1 and moving further away (0.985 -> 0.967), which is attenuation bias
driven by content mismatch, and it lands in the output as a contrast reduction
that deepens along the chain. `lut` has the same defect with 64 free parameters
per channel. `frame_shift` cannot make that class of error; it can only shift.
Its own cost is dark clipping tripling, 0.32% -> 0.96%. A gain-only mode
anchored at black would fix that without a fitted slope, and is the obvious next
mode if long chains ever need one.

Full tables, per-channel numbers, the applied per-hop shifts and the hop-10 run:

**DEVLOG section 70.1**. The nine-hop study that put `anchor` ahead of
`frame_shift` is section 25.

**Seam measurements do not just understate it, they can invert it, so never use
them to decide.** The frames either side of a cut are ~0.9 s apart in scene time
and the content change partly cancels the drift. `tools/seam_probe.py` read
`+3.39/255` total against the cache's `+5.18/255`; on two 2-hop kitchen renders it
read `-0.31/255` and `-1.05/255` -- labelled `invisible` and `marginal` -- against
true per-hop drift of `+2.17` and `+2.63/255`, because the scene darkened across
the cut by more than the generator brightened and the sign flipped. "About a
third low" is the benign case; the error is bounded in neither magnitude nor
direction. Earlier `~2/255` estimates from `chain_00047`/`00050`/`00051` were this
same floor mistaken for the value. Seam numbers remain the right *after*
instrument -- `tone_probe` reads the cache, which stores raw pre-correction hops
by design -- but only as an A/B between two masters from the same seed and cache,
where the contamination is identical in both and cancels. **`temp/` is wiped on
ComfyUI start, so probe before restarting.**
