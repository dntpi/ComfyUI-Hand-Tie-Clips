# Hand Tie Clips -- engineering log

The dated record behind [`CLAUDE.md`](../CLAUDE.md), which is the architecture
brief. Everything here is **history**: what was built on a given day, what it
measured, and what it got wrong. Later sections correct earlier ones -- read to
the end of a thread before trusting the top of it.

Numbering starts at 8 because sections 1-7 became the brief.

## 8. References are files, not wires (2026-08-28)

The node had **16 sockets** and twelve of them were user media: `ref_image_1..9`,
`reference_video`, `voice`, `start_image`. They occupied roughly 340px down the
left of the node before the editor started, each needing its own `Load Image`.
They are gone. The node now has **five**: `model`, `clip`, `vae`, `audio_vae`,
`continuity_state`.

A reference names a **file** under `<ComfyUI input>/h3_refs`. `media.py` owns
both halves of that:

- **`resolve(name)` is the only thing that turns a name into a path**, and it is
  used by the upload route and the loaders alike. Basename only, `normpath`,
  then a prefix check against the reference directory, then an extension
  whitelist. Verified against absolute paths, `../` traversal, and traversal
  carrying a legal extension — all refused.
- **The loaders return exactly what the sockets delivered** — float `[N,H,W,3]`
  in 0..1 — so `_ref_frames`' resize, `_collect_ref_images`' dense-pack and
  `store.tensor_digest`'s cache keying are all unchanged. That is why this was a
  small diff rather than a rewrite.

**Pixels never enter a widget.** Only the basename is stored. PromptMasterLD
measured 1.68 MB of widget value for nine base64 thumbnails and ComfyUI then
failed to save the workflow at all; previews are `/view?...&subfolder=h3_refs`
URLs, which cost nothing to rebuild and survive a reload. `/view` also brings
Range support, which a `<video>` needs to seek — so no thumbnail route was
needed.

**`IS_CHANGED` is new and load-bearing.** ComfyUI caches a node's output on its
inputs, and a filename is a stable input even when the bytes behind it change:
overwrite `face.png` and the previous render would be served. It hashes
`name:mtime` for every referenced file. Deliberately **not** `float("nan")` —
that is the blunt version PromptMasterLD's studio node uses, and it would force
a full re-render of an expensive node on every queue.

**Widget order is part of the saved-workflow format.** `widgets_values` is a
**positional array** -- ComfyUI restores `value[i]` into `widget[i]` and never
looks at the name. The three `*_file` widgets were first added at the *top* of
`optional`, which shifted `hop_script`..`tone_compensate` by +3; every workflow
saved before that change then loaded `audio_pin_frames`' integer into `ref_plan`
and the editor died with `(text || "").trim is not a function` on load. That
throw was the lucky part -- `hop_script`, `shot_plan` and `tone_compensate` were
being silently misassigned too, and only the type mismatch made any of it
visible.

**New widgets go at the bottom of `optional`, always.** Old workflows are then
*short* rather than *misaligned*, and the new widget takes its default. There is
an append-only marker comment in `INPUT_TYPES` saying so. Note `seed` costs
**two** array slots, not one: the frontend appends `control_after_generate`
right after it.

Both plan parsers now coerce (`String(text ?? "")`) and reject non-object JSON
instead of dereferencing it, so a future misalignment shows an empty JSON tab
rather than aborting the whole workflow load.

**The hop cache needed no change at all.** `chain_salt` already digests the
actual tensors, so different pixels behind the same filename still move the key.

**Legacy plans cannot be migrated automatically.** An old `ref_image_N` held a
tensor from a `Load Image`; there is no filename to recover. `refs.py` keeps the
authored slot as `legacy_slot`, `parse_ref_plan` does **not** raise on it, and
both the rail row and `check()` say *"was wired to ref_image_3 — pick its
picture"*. Failing the parse would have stopped the editor opening the very plan
the author needs to repair.

## 9. Presentation (2026-08-28)

**The node collapsed on first click and stayed collapsed.** Two causes, both in
`installHeightGuard` (`js/editor/widget_utils.js`), both ported from
`PromptMasterLD/js/claude_prompt.js:6285-6370`:

- `computeLayoutSize().minWidth` reported a **constant**. The layout pass
  re-reads it on every recompute — selecting the node is enough — and
  faithfully re-declares the node at its stated minimum. It now tracks
  `node.size[0]`. Safe against feedback: `max()` against a constant is a
  fixpoint, not an accumulator, and width never feeds height.
- A stale `widget.width` shadows `node.width` forever after load, because
  ComfyUI's DOM-widget position updater reads `(widget.width ?? node.width)`. A
  live getter with a dropped setter makes the stale value unrepresentable.

Two more fixes alongside: `node.computeSize()[0]` is floored at the panel width
(with no widget declaring one, LiteGraph falls back to `NODE_WIDTH * 1.5 = 210`
and every resize command is free to crush the panel), and `domWidget.computeSize`
now answers a **width-passing** caller with the minimum height and a
**no-argument** caller with the live height. Reporting the live height to both
pins the resize-drag floor to the current height, so the node could only ever
grow.

**`chrome()`'s memo key was stale by construction.** It keyed on
`inputs|outputs|widgets.length`, none of which change when a widget is *hidden*
— so every panel height computed after `applyVisibility` used a chrome
measurement taken before it. The hidden count is now part of the key.

**The reference rail was permanently crushed, and its rescue was dead code.**
The 7-track grid needs ~536px and the node offers ~510px at `NODE_WIDTH 560`.
There was a `@container (max-width: 460px)` block written to relieve it — but
**nothing in the codebase declared `container-type`**, so the query had no
containment context and never matched. `.h3e-section` now declares
`container-type: inline-size`, and there are two breakpoints.

**The palette committed to one look.** It used to derive surfaces from the host
theme via `color-mix()` while hardcoding every accent — the intent was
light-theme safety, but `--h3-bg` mixed toward `#111827` and `--h3-sunken`
toward `#000`, so a light theme got dark blue-grey islands anyway. It is now the
PromptMasterLD `.ldp-root` system: `#0a0a0a`, one hairline `#2a2a2a`, zero
radius, one accent `#e8ff47`. **Changing `--h3-accent` moves the whole panel.**
Emphasis **inverts** (accent fill, `--h3-on-accent` ink) rather than tinting,
because an acid accent at 30% behind unchanged text is olive mud.

A cascade trap worth remembering: the override block was first inserted *before*
the reference-rail section, so `.h3e-chip-on`, `.h3e-inactive .h3e-ord` and
`.h3e-subj-badge` all kept winning on source order. Overrides live at the end of
the sheet now. Without a browser, a token audit script is the only thing that
catches this class of bug.

**The panel did not fill the node, and the mirror was why.** `installHeightGuard`
kept an independent `_h`, updated through an `onResize` hook, so that the
arrange pass could not feed its own growth. Measured during a drag: `onResize`
and `setSize` each fired 57 times while `_h` sat at 876 and `node.size[1]`
climbed past 1400. `measuring` was not stuck, which left one gate --
`Array.isArray(size)`. **This frontend's `node.size` is not a plain Array**, so
every write was skipped and `_h` held the install-time height forever. A 1911px
node had a 742px panel.

The repair was to delete the mirror, not fix the hook. `_h` existed only to
break the arrange loop, and

    panelHeight() = max(minHeight, node.size[1] - chrome() - SLACK)   // SLACK 8

breaks it outright: the pass wants `panelTop + panelHeight + 4`, and `panelTop`
is chrome minus the node's bottom padding, so a panel of exactly `size - chrome`
asks for up to 4px more than the node has on every frame -- the ~130px/frame
runaway the header comment records. With slack the inequality holds, the loop
settles, and the node's height simply *is* the panel's height. Nothing to keep
in sync, no hook to get wrong. `sync()` now only ever grows a too-short node;
the height is the user's to choose.

**RUN is pinned and always open.** `.h3e-root` used to be the scroll container
with all four sections inside it, which put RUN below the script -- out of view
on any workflow with more than two shots, and it is the section touched on every
queue. The root is now a flex column holding `.h3e-scroll` (the authoring
sections) and RUN outside it.

Two flex details, both of which cost a round trip to learn:

- **`.h3e-scroll` needs `min-height: 0`.** A flex item defaults to
  `min-height: auto` and refuses to shrink below its content, which pushes RUN
  off the bottom of the node instead of scrolling.
- **`.h3e-run` must be `flex: 0 0 auto`, never `0 1 auto`.** Flex divides a
  deficit in proportion to each item's content height. The scroller's content is
  far taller, so a shrinkable RUN loses most of the contest and clips its lower
  groups. The scroller absorbs all the shrinking; its `min-height: 160px` is the
  floor that stops RUN owning the panel and RUN's `max-height: 55%` is the
  ceiling.

Always-open removed the only moment RUN re-read its widgets, so it now resyncs
on `api`'s `promptQueued` -- the client-side event that fires after
`control_after_generate` has bumped the seed, and the same one ComfyUI's own
change tracker uses. The listener is dropped in `node.onRemoved`.

**Not done:** PromptMasterLD's `--fsc` UI-scale multiplier, which makes every
dimension `calc(Npx * var(--fsc))` and puts a zoom slider on the panel. It is
mechanical churn across every rule in the sheet and was not worth doing blind.

## 10. Ported for public use (2026-08-29)

The prompting craft was written down and the pack was made installable by a
stranger. Four things, in the order they mattered.

**The shipped example workflows were broken.** All four in `workflows/` predated
the 2026-08-28 socket removal: twelve dead media inputs each, three `LoadImage`
nodes, and 21 widget values against a 28-widget node. Loading one is a new
user's *first* action, so this outranked any amount of documentation. They are
quarantined in `_disabled_custom_nodes/h3_legacy_workflows/` (the pack is not
under its own version control -- deleting would have been unrecoverable) and
replaced by two built from the verified `H3_Stress_6x7` structure:

- **`HandTieClips_Starter.json`** -- two hops, **no references at all**, runs the
  moment the loaders are pointed at files. The empty register is deliberate: an
  `@tag` whose picture is missing is a *hard* error in `resolve_tags`, so a
  starter that shipped with tags would fail on first queue for everyone.
- **`HandTieClips_Showcase.json`** -- the six-hop continuity test, with the three
  reference filenames generalised.

Both use **core ComfyUI plus this pack only**. The dev workflow reaches KJNodes
(`ModelPreviewOverrideKJ`, `MiniMaxLowVRAMAttention`) and PlagueKind
(`LTX_lora_loader`, `H3SLAAttention`, `H3AdaLNLoRAFix`); an example that fails
to load because of a pack the reader never asked for teaches nothing. The
speed stack is documented, not shipped.

**A declared-but-inactive `@tag` reported the wrong cause.** `check()` already
warned correctly that a picture was missing, but the run then died on
`resolve_tags` with *"unknown reference '@kitchen'"* -- pointing at the beat's
spelling, the one thing that was right. `resolve_tags` now takes `declared`
(every tag in the register, active this hop or not) and separates the two
failures. The old two-argument behaviour is unchanged when `declared` is
omitted.

**`PROMPTING.md` and `prompt_pack/`.** The craft rules were spread through
README prose; they are now a standalone guide, and a copy-paste system prompt
that gets a language model to emit valid plans. Two files under `prompt_pack/`
are **generated, never hand-written**:

- `tools/gen_schema.py` builds `SCHEMA.json` from `directives.VOCAB`,
  `refs.RETENTION` and the duration table, and **asserts** against
  `plan._SHOT_KEYS`, `refs.REF_FIELDS` and `refs.SUBJECT_FIELDS`. Add a camera
  move and the schema follows; add a shot field and the generator fails loudly
  rather than emitting a stale schema. `--check` is the CI form.
- `tools/gen_example.py` builds `EXAMPLE_6_HOP.md` from the showcase workflow,
  including its hop/reference table, so the worked example and the shipped
  workflow cannot disagree.

**Template patterns in the editor.** `js/editor/templates.js` plus a
**Templates** button in the SCRIPT header. They **append**, never replace --
replacing would be the one destructive control on the node, and stacking is how
a chain is actually built. `freeId()` mints the lowest unused `sN` because `id`
is the hop cache's pointer and two shots sharing one would make `locked` reuse
the wrong render.

No template contains an `@tag`, for the same reason the starter workflow has no
references. `tools/check_templates.py` extracts the patterns *out of the JS*
and runs them through `plan.parse_plan` and `plan.check_coherence`, and lints
every beat for negation -- a template that produced a plan the node rejects
would be worse than no templates, because a first-time author would blame their
own writing. It caught two: a beat reading "They stop at the window" (naming a
cessation, which is law 2) and a line of dialogue containing "did not".

That second one is worth recording as an open question: **whether the additive
prompt bites inside quoted dialogue was never tested.** The templates avoid it,
and `PROMPTING.md` says plainly that this is untested rather than inventing a
rule.

## 11. What chain_00057 taught (2026-08-29)

The six-hop showcase was rendered for the first time. Three failures, and the
one that mattered was a code gap the documentation had already claimed was
fixed.

**`locked` and `context` reached hop 1 only.** The register's whole promise is
that a subject's continuity text carries identity across a hop where the
photograph is absent. It did not. `subject_prose` is called under `if i == 0`,
and `_identity_lock` returns `""` when no subject-bearing ref is active -- so on
the showcase, hops 2, 3, 5 and 6 carried **no identity text of any kind**, and
hop 5, scheduled with no references at all, lost the character entirely.
Identity was riding on the pinned frames alone, which is the exact failure the
register exists to prevent.

`refs.continuity_line()` now emits that text on every hop 2+, and
`_assemble_next` injects it between the lock and the live-frame citation. The
reason it was suppressed in the first place is real and is preserved: naming
*pictures* on a pin-only hop sent the encoder back to the plates (chain_00034 --
commercial kitchen, grey shirt, no apron). So the new line carries **no
`<Picture N>` and no `<Subject N>`**. It is a description of what stays the
same, not a citation of anything. `<Subject N>` is excluded specifically because
there is no `subject_definitions` block on a continuation hop to bind it to.

Note this changes the assembled block on hop 2+ of every chain with a register,
so it invalidates the hop cache. That is correct, not a regression.

**Dialogue propagated through five hops.** Shot 1 ended on its spoken line with
`tail: ongoing`. The audio pin carries the previous hop's tail, so the last
second of hop 1 -- speech -- opened hop 2, and "action is still underway" was
the closing instruction; the model satisfied it with the action it could hear.
Nothing in hops 2-6 gave the audio anywhere else to go.

This is the guide's own law 3, broken by the plan written to demonstrate it. The
fix is authorial, not code: land the line **mid-hop** and leave a non-verbal
action running into the seam (the knife on the board), and give every
dialogue-free hop a narrowband sound of its own. Both `PROMPTING.md` and the
authoring prompt now carry this as a named rule, and the shipped templates were
rewritten to obey it.

**`join: continuous` across a location change morphed one room into the other.**
Hop 6 walked back from the hallway and the kitchen appeared mid-turn. A
continuous join asks for one unbroken take between two different rooms, which
is not a thing. `match_cut` is what a walk through a doorway is. The beat also
read "steps into @kitchen" -- the container phrasing the README warns produces a
composite of the photograph -- and now names the counter *in* the kitchen.

Hop 5's beat is unchanged in the revised plan **on purpose**: it is the
measurement, and changing it would forfeit the comparison.

The revised plan is in the shipped showcase and in
`user/default/workflows/H3_Stress_6x7_v2.json`; the original that produced
chain_00057 is left alone so the A/B survives.


## 12. Renamed to Hand Tie Clips (2026-08-29)

The pack was `ComfyUI-H3-Ref-Chain`. It is now `ComfyUI-Hand-Tie-Clips`, and the
four registered ids moved with it:

| was | is |
|---|---|
| `H3RefChain` | `HandTieClips` |
| `H3ContinuityState` | `HTCContinuityState` |
| `H3ChainPreview` | `HTCChainPreview` |
| `H3ToneCompensate` | `HTCToneCompensate` |

Display names are unchanged (`H3 Ref2VA Chain`, `H3 Chain Preview`, ...) because
the pack only drives MiniMax H3 checkpoints and a name that hides that costs
somebody an afternoon. The menu category is `Hand Tie Clips`. `TAG` -- and so the
console prefix -- is `[HandTieClips]`.

**The old ids are still registered, as `DEPRECATED` subclasses.** A type id is
what every saved `.json` carries, and an unregistered one is a red missing-node
box, not a warning. A plain alias in `NODE_CLASS_MAPPINGS` would have worked but
listed each node twice in search: ComfyUI falls back to the mapping key when
`NODE_DISPLAY_NAME_MAPPINGS` has no entry for it. Subclassing and setting
`DEPRECATED = True` gets both -- `server.py:783` publishes `deprecated: True`,
and the frontend's `Comfy.Node.ShowDeprecated` (off by default) keeps it out of
search while leaving it fully functional in workflows that name it.

**The JS had to learn both ids or the aliases would have been worse than
useless.** `js/h3_ref_chain_ui.js` and `js/h3_chain_preview.js` each compared
`nodeData.name` against a single string; a legacy node would have loaded with no
editor at all, which looks exactly like the rename having broken the pack. Both
now test membership of a `Set`. `js/h3_chain_preview.js` needs it twice: once
for its own type, once for `CHAIN_TYPES`, which is how a preview walks back up
`images` to find the chain feeding it.

**What deliberately kept the `h3` naming**, and must not be "finished" later:
module filenames (`h3_ref_chain.py`), the `h3e-` CSS class prefix and `--h3-`
tokens (515 occurrences -- one missed class silently breaks styling), the
`/h3_ref_chain/*` routes, the `h3_refchain_preview` event name, and
`input/h3_refs`. Renaming that folder would orphan every reference photo already
on disk.

The two shipped workflows moved to `HandTieClips_Starter.json` /
`HandTieClips_Showcase.json` and were rewritten onto the new ids, with their
`SaveVideo` prefix now `video/HANDTIECLIPS/chain`. The three under
`user/default/workflows/` were left on the legacy ids on purpose: re-running
`H3_Stress_6x7_v2.json` is then a live test of the alias path, and their existing
renders stay together under `output/video/H3REFCHAIN/`.


## 13. The on-canvas board (2026-08-29)

`workflows/HandTieClips_Starter.json` carries six `MarkdownNote` cards to the
left of the loaders, wrapped in a group titled READ ME. The text lives in
`tools/notes.py` and is written into the workflow by `tools/build_notes.py`.

Why on the canvas: the craft was in `PROMPTING.md`, `prompt_pack/` and the
Templates panel, and all three require leaving the graph. The rules that decide
whether a first render works are needed while beats are being written, which is
on the canvas. The cards are a **condensation**, not a copy -- `PROMPTING.md`
stays the authority and every card says so.

Mechanics worth not rediscovering:

- `MarkdownNote` is a core virtual node. The frontend renders it with `marked`
  at `gfm: true` and sanitises with DOMPurify, so headings, GFM tables, bold and
  code fences all work. It needs frontend >= ~1.16.
- Cards are identified by `properties.htc_card`, which is what makes
  `build_notes.py` idempotent -- it drops marked nodes before writing, so
  re-running replaces the board instead of stacking a second copy.
- `extra.ds` is **restored** on load, not fitted. Without setting it, a board at
  negative x sits off-screen and is never found. Screen is
  `(world + offset) * scale`, so the offset is what brings it into view.
- Group serialisation is `{id?, title, bounding:[x,y,w,h], color?, font_size?,
  locked?}` -- confirmed against the frontend's own zod schema, not guessed.

**`widgets_values_named`, found while doing this.** Both shipped workflows
carried a stale copy: `chains: 3`, `duration: 10 s`,
`control_after_generate: randomize`, and a legacy `ref_plan` naming pictures that
do not ship -- 25 entries against a 28-widget node. It came from the builder
deep-copying nodes out of the dev workflow and overwriting only
`widgets_values`. Dormant while `Comfy.Workflow.NamedValuesRestore` stays off
(experimental, default false), but anyone who turned that on would have loaded a
Starter that randomizes its seed and dies on a missing reference.
`build_notes.py` strips it from both, and `tools/check_workflows.py` now fails if
it ever comes back.

`tools/check_workflows.py` is the promoted version of the validator that guarded
the shipped workflows against socket/widget drift. It also checks the board:
every card marked and non-empty, exactly one group, the group enclosing every
card, and no card reaching past x=0 onto the loaders.


## 14. The turbo stack ships in the examples (2026-08-29)

Both shipped workflows now carry the dev graph's full MODEL wire:

    UNETLoader -> LTX_lora_loader -> H3AdaLNLoRAFix -> MiniMaxLowVRAMAttention
               -> H3SLAAttention -> ModelPreviewOverrideKJ -> HandTieClips

and **CLIP reaches the chain from the LoRA loader, not the encoder** -- that is
what makes the text half of every LoRA land, and it is the wire most likely to
get quietly "fixed" back to the encoder by someone tidying the graph.

This reverses the earlier "core ComfyUI and this pack only" rule for the
examples, at the user's direction: `steps` is 7, which only works with a turbo
LoRA, so an example without the LoRA stack is not a graph anyone can run at the
settings it ships with. PlagueKind supplies the loader, the AdaLN fix and SLA;
KJNodes supplies Low VRAM Attention and the preview override.

`tools/build_speed_stack.py` inserts and rewires the five nodes idempotently
(marker `properties.htc_speed`), rebuilding the plain loader -> chain shape
first so it is reentrant from either state. `tools/check_workflows.py` walks
**both** wires link by link -- a patch node that is present but bypassed round
the side looks right on the canvas and does nothing.

`H3SLAAttention` widget values are written out in full including
`reference_protection`, which post-dates the dev workflow's saved values. That
workflow has 10 entries against an 11-widget node, which is the positional rule
working as intended: the new widget was appended last, so old values still line
up and the missing one falls back to its default. The trailing `""` on the LoRA
loader and the preview override is carried over verbatim for the same reason --
a value past the last widget is ignored, and dropping one that turns out to
belong to a widget would shift every value after it.

`head_chunks` ships at 4 (the node's own default, safer on unknown VRAM) rather
than the 2 used here. Two files have to be on disk as well: the turbo LoRA the
loader names, and `taeh3.safetensors` for the preview override's `tiny_vae`.

## 15. What a shipped diagnosis looked like (2026-08-29)

The editor UI vanished after the folder rename -- raw widget boxes, no panel.
Not a code fault. ComfyUI had been restarted while the pack was still
`ComfyUI-H3-Ref-Chain`, so the running process held
`python_module: custom_nodes.ComfyUI-H3-Ref-Chain` and served its web assets
from a path the move had deleted. `/object_info` had all eight node types;
`/extensions` listed none of the pack's JS and a direct fetch 404'd.

Worth knowing for next time: `/object_info` carries `python_module`, which is
the fastest way to find out **which copy of a pack a running server actually
loaded**, and `/extensions` plus a direct fetch of one script separates "the JS
is broken" from "the JS is not being served at all".

## 16. What the Rain Kitchen renders taught (2026-08-29)

Three six-hop renders of the same 6x7 s chain, each isolating one variable. All
three findings are now in `PROMPTING.md`, the troubleshooting table, the
authoring prompt and the on-canvas board.

**Identity drift is permanent, and the old advice was backwards.** This file and
`PROMPTING.md` used to say a face plate riding a later hop *beats the pin* and
that `shots` should therefore be kept tight. chain_00059 falsified it: hop 4
carried a face plate photographed in a *different kitchen* through a walking
medium shot and held cleanly, while hop 5 — scheduled with no references at
all — came back a different person, and hop 6 never recovered even though a
place plate rode it and restored the room instantly. `locked` holds a face that
is still right; only a plate rebuilds one that is gone. **Face refs go on every
hop.** The old advice survives for *place* plates only.

**A beat must survive an over-delivered hop.** A hop routinely does more than it
was asked. Shot 3 asked for "a first slow step along the counter" and delivered
the whole walk; shot 4 was then handed an instruction its own live frame had
already satisfied, and the only way to obey was to reset the scene — a hard
cut 1.5 s *into* the hop, not at the seam, because `_assemble_next` holds the
incoming frames for a short beat first. Give one hop the whole movement, and
write the next beat true from either ending ("she *reaches* the window", "she
takes up the bowl *again*").

**A noun with no adjective drifts.** Three hops said only "the bowl"; it came
back stainless steel. Naming it "the white bowl" everywhere *and* stating it in
`context` as a property (never a location — "stays in her hands" fights the
beat the moment she puts it down) held it for six hops. The re-run then proved
the rule by accident: `context` read "the apron stays tied over the **grey**
t-shirt", and the t-shirt held all six hops while the apron, one clause away
with no colour, had turned denim blue by hop 6.

### The code change this bought

`refs.resolve_tags` now takes `subject_names` and the node passes it on
continuation hops only. `<Subject N>` is bound by `subject_definitions:`, which
is hop-1 material — so on hop 4 the ordinal dangled, exactly the same defect
as the undescribed bowl. From hop 2 a person tag resolves to the subject's
`name` instead ("The cook walks down the hallway"), which binds to the
`continuity_line` sentence every continuation hop already carries. No name means
the old `<Subject N>` fallback, so nothing regresses.

`tools/check_prompts.py` mirrors the new call, and gained the banned-word check
that previously ran only against `templates.js`. That gap is how the shipped
Showcase carried "**None** of the kitchen is visible" (law 1 — it *adds* a
kitchen at cfg 1.0) and "She **stops** at the window" (law 2) through six
versions. Both are fixed, and the Showcase's face ref now rides all six hops.

## 17. The pre-beta audit (2026-08-29)

Read for what a stranger hits, not for what we already check. Six findings; the
first two would each have cost a tester a run.

**The docs promised a stop the code never did.** `refs.check()` is *"warn, never
raise"*, and its caller only printed. A ref naming a file that is not in
`h3_refs` had its slot skipped and the chain rendered on — all six hops, with
the reference silently inactive, which is precisely the uncontrolled output the
register exists to prevent. Four shipped surfaces said otherwise, including the
Showcase's own note: *"the run stops and names the reference it could not find.
Nothing guesses."*

Fixed in the code rather than the docs, because there is no reading under which
rendering without a named-but-absent picture is what the author meant. New
`refs.missing_files()` returns `(tag, file)` for every ref whose named file did
not load; `h3_ref_chain.py` raises on it, after printing the register table so
the error arrives with its context. **The distinction that matters:** a ref with
*no* `file` stays a warning — the Starter ships that way on purpose so it runs
before any pictures exist. Only a *named* file that is absent is fatal.

**The on-canvas board never got §16's corrections.** `tools/notes.py` still
carried the row chain_00059 falsified — *"a plate riding a hop it does not
belong on, beating the pin → tighten `shots`"* — and was missing all three rows
`PROMPTING.md` gained. The board is what a beginner actually reads, so it was
shipping the inverse of the rule. Lesson: `PROMPTING.md` is the authority, but
`tools/notes.py` is a **second copy** of the same craft, and a correction is not
finished until both move. `build_notes.py` regenerates the Starter's cards *and*
the Showcase's `Note` from `notes.SHOWCASE_NOTE`, so one rebuild covers both.

The rest were packaging, not craft: the MIT `LICENSE` named no copyright holder;
`pyproject.toml` lacks both fields a Registry publish needs (`PublisherId` and a
`[project.urls] Repository`), now commented in place; the `README` had **no
install section at all**; `ComfyUI-H3-Motion-Context` was absent from Needs
despite the intro calling it the primary guidance path, so testers land on the
`MiniMaxH3AddGuide` fallback without knowing they changed code paths; and the
example workflows name quantised checkpoints by filename with no note that they
are one valid set among many, which reads as a broken graph rather than a
missing file.

Clean on the same pass, worth not re-checking: no personal data anywhere, no
hardcoded local paths in runtime code (only two dev-tool docstrings), 3.10-safe
syntax, no third-party dependencies, Motion-Context absence handled with fork
detection, `WEB_DIRECTORY` present, `h3_refs` auto-created.

## 18. What a 27B model got wrong, and what the prompt taught it (2026-08-29)

Qwen3 27B at temperature 0.3 was given an 8 x 15 s concept. The `ref_plan` came
back structurally perfect -- both subjects named, face plate on all eight hops,
places scheduled off the hops they do not belong on, zero register warnings. The
`shot_plan` carried four defects, and tracing each one back to
`prompt_pack/AUTHORING_PROMPT.md` found more than four gaps.

**The prompt taught one of the bugs.** Rule 9 illustrated a beat as
``"stands at the counter in `@kitchen`"`` -- backticks and all, because it was
the one place a tag appeared inside a beat. The model copied the formatting, and
literal backticks reach the encoder. Fixed, plus a rule that a beat is plain
prose.

**Beat length had no guidance at all.** The only budget in the file was for
dialogue, so a no-dialogue plan had nothing to size against and every beat came
back at 22-28 words regardless of a 15 s hop. There is now a word table -- and
an honest note on it: the shipped plans run 37-39 words at *both* 5 s and 7 s,
so beat length is near-constant across the only two hop lengths ever rendered.
The words-per-second reading that gives 70-100 for a 15 s hop is a reasoned
extrapolation, not a measurement, and the table says so.

**Two rules were simply missing.** Nothing told the model to state a visual style
-- for a "2D anime, Ufotable" concept neither returned block contained the word
anime, so the text asked for photoreal while the pictures asked for anime. And
the over-delivery rule from section 16 had never been carried into the prompt at
all, only into `PROMPTING.md` and the board.

**Two statements were wrong.** The prompt said "at most 9 pictures on any one
hop"; `parse_ref_plan` counts `len(refs)` over the **whole plan**. The code's own
error message said "on one hop" too, and has been corrected to match what it
checks. And the `file` bullet still told the model to invent placeholder
filenames without noting that, as of 0.3.1, a named file absent from `h3_refs`
**stops the run**.

Law 2 is the interesting non-fix. It is stated plainly, with the exact example
"The cook stops talking", and the model wrote "stops and looks up" anyway. A
principle is not something a 27B reliably applies to its own output; the literal
word list `check_prompts.py` enforces now appears in the prompt, because a word
list is checkable and a principle is not.

## 19. What 114 seconds of rendered film and two 27B models taught (2026-08-29)

Three sources landed together: a rendered 8 x 15 s anime chain (`chain_00003`,
2742 frames, 114.25 s), and Qwen and Gemma each answering two test prompts
written to trip specific rules. Shipped as **0.3.3**.

### The node was fighting every stylised plan

`directives.py` prepended `ESTABLISH = "Live-action, natural light, one
continuous take."` to hop 1 **unconditionally**. At cfg 1.0 with no negative
branch that is additive, and it landed *ahead* of the style declaration rule 12
requires. A stop-motion puppet plan compiled to "Live-action, natural light,
one continuous take. ... Hand-drawn stop-motion puppet animation in felt and
painted wood", and the two fought.

It also explains the anime chain's opening: hop 1 rendered as bright
naturalistic daylight (**mean luma 72**) against a night plan *and* a night
place plate, then fell to 46 on hop 2 the moment ESTABLISH stopped riding. That
had been read as drift; it was the node.

Never surfaced because both shipped workflows are live-action.

Fixed twice over, because either alone leaves a hole: an `establish` widget
(appended **last** in `optional`, per the positional-widget rule in section 8),
and `directives.declares_own_medium()` / `establish_for()`, which drop the
default when shot 1's opening names a medium. A model-authored plan never
touches a widget, which is why the automatic arm is the one that matters.

### Luminance drifts, and it only goes one way

Mean luma per hop across the eight: **72, 46, 35, 17, 11, 11, 19, 14**. Setting
the ESTABLISH artefact aside, hops 2-6 still slide 46 -> 11. The combat, the
point of the film, plays at the bottom of it.

This is the colour law one level up. `locked` holds a face; nothing holds an
exposure. Each hop inherits the last frame and darkens it slightly and the
error compounds. **Restate the light as a positive property in every beat** --
naming a light *source* ("pale moonlight") does not set a level, and at cfg 1.0
it only adds a moon.

### The seams were never the problem

7 of 7 hop joins are invisible: largest frame-to-frame difference at any seam is
13.6, inside the range of ordinary in-shot motion, and two seams score below the
film's own mean. Identity held 114 seconds on face plates riding every hop.

The one hard cut in the film is **inside** hop 4, 3.25 s in, at 7.1 sd -- more
than double any other jump. Shot 3 ended "ahead the trunks begin to thin toward
open ground"; shot 4 opened "Across the flat moonlit stone of @arena_clearing
the two of them square off". Hop 4 was handed a live frame of a man among trees
and a beat asserting he stood on open stone, held the forest for 3.25 s, then
reset the scene. The plan was clean under `check_coherence` and the banned-word
scan. Hence `plan.check_place_handoff()`.

### What the two models did

Near-identical answers from Qwen and Gemma on the same prompt -- same beats to
the word in 5 of 6 shots, same defects, same invented justification. The prompt
is prescriptive enough to collapse two models onto one answer; a shared blind
spot is then invisible from output alone.

**The word table was inert.** Both models, asked for six 15 s hops, returned
beats averaging **54 words** against a 70-100 band -- every beat under the floor
-- and the same models on much shorter hops returned 40-48. Beat length is
near-invariant to hop length in model output, now measured in a third setting.
A reference table does not move a model with a prior on paragraph length; the
rule is now an instruction to count, with a worked 74-word example.

**The banned-word list beat the principle, and made things worse.** Zero literal
banned words in all four plans -- and "The storm's roar begins to fade...
raindrops strike the glass with decreasing force" and "The storm has passed".
The list taught token avoidance and handed them a box to tick. It is now framed
as crude examples of an idea, with the test stated as: is this happening, or has
it finished happening?

**Both abandoned the second location.** Each plated the opening place, moved the
story elsewhere, gave the new place no plate, and justified it with a rule that
does not exist ("to avoid conflicting with the frame pin of the new space");
Qwen cited "rule 8/9", which does not say that. In the lighthouse plan that left
the lamp room -- four of six hops -- on beat text alone.

### `check_place_handoff` has two arms, and both were narrowed by real plans

1. **Handoff.** Shot N names a place tag shot N-1 never mentions, and shot N's
   own beat does not carry the journey. The arrival vocabulary had to widen: the
   Showcase's shot 6 ("walks back along the hallway and through the doorway to
   the counter in @kitchen") is correct and was being flagged.
2. **Abandonment**, not gaps. Warning on any unplated hop also flagged the
   Showcase, which deliberately walks her down an unplated hallway on 4-5 and
   returns the kitchen plate on 6. The rule that survives contact: warn only
   when the plates stop and **never resume**, so the film ends somewhere no
   picture describes.

Both shipped workflows are clean under the final version; the anime plan raises
exactly one warning, on shot 4.

### Audio, for the record

No background music and no speech, confirmed on the spectrogram: broadband
transients and noise, no harmonic bands. Seam levels hold within +/-2 dB on five
of seven joins, and the two exceptions are drops the beats themselves ask for.
The real audio issue is range, not seams: peaks reach -1.2 dBFS during the fight
while hop 8 averages -42 dBFS. There is no audio equivalent of
`HTCToneCompensate`. Left open.
## 20. Seven features built blind (2026-08-30)

Built in one pass with no browser and no GPU render available -- the user was
away and explicitly asked for the work anyway. Everything below was verified by
offline execution only. **Section 21 is what happened when it was finally
opened in ComfyUI**: two of the seven shipped broken in ways no offline test
could have caught, and the rest measured out. Read section 19 first for the
measurements that motivated most of it, and 21 for what survived contact.

### What shipped

| # | thing | where |
|---|---|---|
| 1 | `tone_compensate=anchor` + `tone_anchor` strength | `tone.py`, wired in `h3_ref_chain.py` |
| 2 | `dry_run` -- compile every prompt, render nothing | `h3_ref_chain.py` |
| 3 | `contact_sheet` -- a fourth IMAGE output | new `sheet.py` |
| 4 | `render_through` -- stop after hop N | `h3_ref_chain.py` |
| 5 | `quality=draft` -- 0.3 MP, 6 steps | `h3_ref_chain.py` |
| 6 | **H3 Seam Report** node | new `seam.py` |
| 7 | over-delivery lint | `plan.py` |

Five new widgets, appended LAST (29 -> 34 values). One new output, appended
LAST (3 -> 4). Both rules are in section 9; both were obeyed.

### The anchor, and why it is not just another tone mode

frame_shift/gain_bias/lut are **seam-local**: they cancel the denoiser's tone
bias on the overlap, which makes each join exact. They cannot see the exposure
falloff *inside* a hop, and that is what compounds -- hop N darkens across its
own frames, hands the darker tail to hop N+1, and every individual seam stays
perfect while the film dims. Section 19 measured 46 -> 11 across hops 2-6.

Worth stating plainly because it is counter-intuitive: **a synthetic 8-hop
chain showed frame_shift making the total slide WORSE** (66/255 vs 35/255 with
correction off). That is correct behaviour, not a bug. The denoiser's per-hop
bias happened to lift; cancelling it removed a lift that had been partly
offsetting the falloff. Seam correction fixes seams. It was never a level
control and should not be read as one.

`anchor` = frame_shift + a second stage pulling each hop's mean back toward
**hop 1's**. Two properties make it safe to stack:

- the pull **ramps from zero** over `ANCHOR_RAMP` (48f) frames, so frame 0 of
  a hop is returned untouched and the seam stays exactly as frame_shift left
  it. Without the ramp a per-hop constant offset re-introduces precisely the
  step frame_shift just removed -- this is the whole design, and the trap
  anyone re-implementing it will fall into;
- it is **capped** (`ANCHOR_MAX_SHIFT`, 0.06) and scaled by `tone_anchor`
  (0.35), so a slide is corrected across several hops instead of one hop
  snapping back.

The correction needs no carry variable between hops: because it is applied
before `prev_imgs` is taken, the next hop's seam correction matches the
already-corrected tail and the offset propagates on its own.

On the synthetic chain: slide 66 -> 18/255, worst seam step 2.22 -> 2.13/255.
The seam did not regress, which is the property that mattered.

Intent is indistinguishable from drift from the inside, hence the per-shot
`tone` field: `"free"` skips one hop's pull, `"rebase"` moves the anchor onto
that hop. A deliberate walk into a cellar needs `rebase` or the chain spends
the rest of the film brightening it back.

### dry_run: what it must not touch

The value is that it costs seconds, so every expensive thing is guarded:
`MiniMaxH3SigmaShift`, `KSamplerSelect`, `BasicScheduler`, `_model_fingerprint`
(it hashes patched weights), the hop store, and -- the big one -- the master
preallocation. `master_imgs` for 8 x 15 s at 1280x736 is 2742 float frames,
about **31 GB**. A dry run that allocated it would be worse than useless.

Hop 2+ needs *a* `prev_imgs` to compute `<Picture N>` ordinals. Content is
irrelevant to the compiled text, so a `[overlap, 8, 8, 3]` zero tensor stands
in and the text is byte-identical to a real run's.

The smoke test (`tmp/t_dry.py`) replaces all five sampler entry points with
objects that raise on **any** attribute access, so "did not touch the sampler"
is asserted rather than assumed. It caught one real bug: the dry block
referenced `pin_mech_pred` before its assignment, ~40 lines later. Which pin a
hop gets is decided at render time from whether a sampler latent exists, so a
dry run genuinely cannot know it -- the sheet reports the `pin_to_qwen`
*setting* instead. Reporting AddGuide for every hop would have been a lie.

### The over-delivery lint

The one defect class every other check structurally misses: both shots are
individually well-formed, the directives are individually legal, and only the
JOIN between them is wrong. `tail=settle|hold` promises rest; a following beat
that opens "She continues...", "Walking to...", "Mid-sentence..." asks the model
to carry on what the hop before was told to stop.

Narrow on purpose. Trailing spaces in `_MID_ACTION` are load-bearing ("keeps "
not "keepsake", "still " not "stillness"), and `_MID_ACTION_LEAD` is only
checked at position 0, which is what stops "Morning light..." and "Nothing
moves..." from firing. Verified against those exact traps, and both shipped
plans stay quiet.

It will miss a beat that opens mid-action without saying so. That is accepted:
a false positive that blocked a render would be worse than the defect.

### Notes for whoever picks this up

- `sheet.py` and `seam.py` catch every exception and return a placeholder
  image. A picture must never lose a finished chain. Do not "clean up" those
  handlers. The placeholder was 1x1 until section 21 -- see there for why an
  inert-looking image is not inert.
- The contact sheet stores frames through `sheet.small()` (168px tall). Two
  full frames per hop across eight hops is 180 MB held for the whole render for
  no reason.
- The sheet shows `imgs[overlap_n]` for hops 2+, not `imgs[0]`: the first
  `overlap` frames are trimmed at the join, so `imgs[0]` is a frame the master
  never contains.
- `tools/check_workflows.py` derives the expected widget list from the live
  `INPUT_TYPES`, so it needed no edit for the five new widgets -- only
  `SaveImage` added to `CORE`, for the Starter's new contact-sheet node.
  (Section 21 added `PreviewAny`, `PreviewImage` and `HTCSeamReport` to those
  allowlists when the seam report was wired into the Starter.)
- The Starter now ships `contact_sheet=on` with a `SaveImage` wired; the
  Showcase ships it off. Starter is the teaching graph, so the feature is on
  the canvas where it will be found.

### Unverified, in priority order

All five items that stood here were closed on 2026-08-30. See section 21.

## 21. What the first ComfyUI session measured (2026-08-30)

Section 20's seven features, opened in a browser and run on a GPU for the first
time. Five measured out. **Two were broken, and neither could have been caught
by any offline test that existed** -- both failures lived in the gap between
"the Python is correct" and "the graph runs".

### Bug 1: 0.4.0 never touched `js/`

`git show --stat` on the 0.4.0 commit lists twenty files and not one under
`js/`. The five new widgets were declared in `INPUT_TYPES` and never added to
`GROUPS` in `js/editor/run_panel.js`, so the run panel did not draw them.

It did not *look* broken, which is the interesting part. The panel hides only
the widgets it successfully drew -- a deliberate design so an undrawable dial
never vanishes from the node -- so all five fell through to native dials and
worked fine. `tools/check_workflows.py` passed throughout, because it derives
from `INPUT_TYPES` and `GROUPS` is display-only.

**The lesson: a Python-side widget list and a JS-side widget list are two
sources of truth, and nothing checks that they agree.** Adding a widget means
editing both. There is still no checker for this.

### Bug 2: a 1x1 image is not inert

A dry run returned `sheet.placeholder()` on `images` -- a 1x1 black frame. The
Starter wires `images` into `CreateVideo` -> `SaveVideo`. libx264 in yuv420p
subsamples chroma by 2 and **cannot open a codec context on an odd dimension**,
so every dry run died in `avcodec_open2` before writing a frame, with a
traceback naming ComfyUI's video node and nothing of ours.

Reproduced in isolation: 1x1 throws, 2x2 encodes. `placeholder()` now takes a
width and height, floors at 2x2, and rounds odd dimensions down to even; the
dry run passes the geometry the plan resolved to, so a dry run yields one black
frame at the real resolution and downstream video nodes are happy.

`tools/check_features.py` had asserted `shape == (1, 1, 1, 3)` -- the offline
suite was *pinning the bug in place*. It now asserts an `encodable()` contract
instead. **A test that encodes an exact wrong value is worse than no test.**

### `tone_compensate=anchor`, measured on a real chain

Three hops, 8 s, 0.3 MP, 6 steps, one seed, one cache. Because the hop store
writes *before* the tone block runs, flipping tone modes re-grades cached
renders in ~14 s instead of 164 s -- so the whole sweep is nearly free. Drift is
hop 3 mean minus hop 1 mean; seams are the step across the join.

| `tone_anchor` | drift | seam @192 | seam @362 | flicker |
|---|---|---|---|---|
| off | 13.5/255 | +0.9 | +2.1 | 0.4675 |
| 0.15 | 7.4 (-45%) | -1.3 | -1.0 | 0.4566 |
| 0.35 | 5.1 (-62%) | -1.9 | -1.6 | 0.4618 |
| 0.60 | 2.9 (-78%) | -2.6 | -1.8 | 0.4661 |

Seam figures are `seam.measure(window=6)` -- the shipped node's own method, not
an ad-hoc frame difference -- so the docs and the instrument a user runs agree.

Drift removal is even: 45 / 62 / 78% of the uncorrected slide, ~16pp per step.
**The seam is not monotonic.** `0.15` pulls it tighter than the uncorrected
chain (2.1 -> 1.3) before it grows again, so there is a shallow optimum below
the default rather than a straight trade. From 0.15 up it costs ~0.6/255 per
step. Note also that the seams flip sign: uncorrected they are positive, and
every corrected run overshoots slightly negative. Hop 1 is byte-identical across
all four, as the design requires. **The shipped 0.35 default stays** -- it
halves the drift while every seam still reads marginal or better.

The propagation claim in section 20 -- that no carry variable is needed --
is visible in the logs: hop 3's `frame_shift` grew with anchor strength
(`r+0.0185` -> `r+0.0217` -> `r+0.0256`) because hop 3 measures against hop 2's
already-corrected tail. It self-propagates, exactly as designed.

**Caveat that limits this measurement**: the test scene walks the subject toward
a bright window, so some of that 13.4/255 is light a real camera would also
produce. Anchor cannot distinguish motivated light from drift -- that is what
the per-shot `tone` field is for. A `camera=hold` scene is the run that would
justify moving off 0.35.

### The seam report node, validated

Never executed before this session. Wired into the Starter and checked against
an independent decode of the mp4: agreement to **±0.07/255** across six seam
readings under two different hop geometries. Its arithmetic is right.

A better result came out of the cross-check. On a 5-hop master the eight largest
frame-to-frame luma jumps were at f289, f368, f409, f412, f463, f464, f482,
f497 -- **not one of them a seam**. Every join is quieter than ordinary scene
motion. That is the pack's central claim, measured.

**Trap, and it cost a wrong diagnosis.** 5 hops x 124f and 3 hops x 192f both
total 532 frames at overlap 22. The node derives hop length from
`frames`, `hops` and `overlap`, so a wrong `hops` yields a plausible length and
four confidently wrong seam positions. There is no way for it to know better
from `images` alone -- but the chain's `info` output carries the real geometry,
so an optional `info` input that cross-checks would turn this class of mistake
into an error message. Worth doing.

### `quality=draft` is close to a no-op here

Draft's two levers are resolution -> 0.3 MP and steps -> 6. In the turbo regime
this pack targets, a "final" run is *already* 0.3 MP at 6-8 steps, so the first
lever does nothing and the second saves one step:

- draft, 6 steps: **42.1 s/hop**
- final, 7 steps: **43.3 / 45.2 / 50.2 / 44.1 s/hop**

About 7% apart. Draft only earns its place if final is genuinely heavier --
1.0 MP at 14 steps. Kept, because that configuration exists, but it is not the
fast-preview button it sounds like. `dry_run` is the fast-preview button.

### The rest

- **Contact sheet**: correct and genuinely useful on real renders -- first/last
  thumbnails per hop, directives, beat, tone line, seed, `cached`, pin
  mechanism. Legible at 1:1; **not** legible in a node preview, where a 1280px
  sheet scales to ~0.25 and 15px body text renders at ~4px. It is a
  click-to-enlarge document. Fonts left alone deliberately.
- **`render_through`**: truncates correctly (`rendering hops 1-1 of 2`), and
  re-extending works -- a 3-hop chain extended to 5 loaded hops 1-3 from cache
  and started rendering at hop 4.
- **Over-delivery lint**: exercised offline; never fired in ComfyUI because both
  shipped plans and the test plan stay clean. Unproven against a real positive.

## 22. Two models, one prompt, the same two mistakes (2026-08-30)

A 3-hop / 10 s brief was written to load six traps into thirty seconds, and
handed to two local models in LM Studio with `prompt_pack/SYSTEM_PROMPT.md` in
the system box, temperature 0.4, and nothing else. `EXAMPLE_6_HOP.md` was
deliberately withheld -- it argues for six hops and would have contaminated a
3-hop test. Grading ran every reply through the real parsers rather than by eye.

| | qwen | gemma 26b-a4b |
|---|---|---|
| FAIL | 4 | 2 |
| after the shared prompt bug | 2 | 0 |

**Both models made the same tag mistake, which makes it the prompt's.** Each
wrote `@kitchen` in the beat -- correctly, that is rule 10's own example -- and
then invented `"tag": "kitchen_plate"` for the register. The string `_plate`
appears nowhere in the prompt; they arrived at the same convention
independently. The cause was in the file: rule 10's only concrete place tag
lives in a *beat*, the register example held one ref (`hero_face`, a person),
and the two were never shown together. The invariant *was* stated, in a field
bullet 150 lines later -- and models copy examples, not bullets. `PROMPTING.md`,
the human guide, has had a three-ref example including `kitchen` all along; the
machine prompt was trimmed and lost it.

**The silent one.** qwen wrote `"name": "@cook_face"` into `subjects`. That
parses, resolves, renders, and is wrong: `name` is what `resolve_tags`
substitutes for a subject's tag from hop 2 on, so the tag resolved to itself and
a literal at-sign reached the encoder on two of three hops. Nothing caught it.
`check_prompts.py` would have, but only for the two shipped workflows.

**Where the models actually differed** is invisible to any parser. Both lifted
rule 2's worked answer verbatim (*"water runs in slow threads down the window
glass"*). But hop 3 moves to a hallway, and qwen also copied rule 4's example
*object* -- putting "a single click from the refrigerator" in a corridor, the
kitchen appliance following her out of the room. gemma copied rule 4's *method*
and wrote "the low hum of a hallway light". Recitation versus transfer, and only
one of them survives a change of location.

**Both** also left `tail` off hops 1-2, describing the arrival at rest in prose
instead of directing it -- so the over-delivery lint, which only arms after a
`settle`, still has not fired on a real positive.

Fixed here: the register example carries a place tag on both sides of the round
trip and says the two spellings are one string; `refs.py` rejects an `@tag` in
`name`, `locked` or `context`. Not fixed, because it is a brief-writing lesson
rather than a bug: ending a chain in a location no plate describes earns the
place-handoff warning, and that was the brief's fault, not either model's.

## 23. The hop cache stops shelling out (2026-08-30)

The Comfy registry flagged all three published versions. The reason is not in
the web UI and `status_detail` on the node is empty; it is behind
`https://api.comfy.org/versions?nodeId=<id>&include_status_reason=true`, which
returns the actual findings:

    scanner      yara_scan
    issue_type   python_command_injection_risk
    file_path    store.py   lines 131 and 215
    description  "Detects all os.system and subprocess usage"
    severity     info
    recommendation  null
    admin_tags   any-code-execute

Two findings, both the `subprocess.Popen` calls that ran `ffmpeg` for the FFV1
hop cache. The rule does no taint analysis, so a static argument list built from
`shutil.which` and run with `shell=False` matches exactly as hard as a shell
injection would. Its 95% confidence is confidence that the call *is* a
subprocess call, not that it is exploitable.

Appealing looked like the wrong move. `plaguekind-nodes` -- 22.5k downloads --
has 1.3.8 through 1.4.0 flagged with the same two findings and 1.4.1 onward
`Active` with `status_reason` = **"Passed automated checks"**, the string the
scanner writes when it finds nothing. That is a code change, not an admin
override.

**But the registry is the weakest reason to have done this.** `_ffmpeg()` raised
if no ffmpeg binary was on PATH, and ComfyUI never requires one -- so the
feature that makes a tone A/B cost 14 s instead of 164 s hard-failed for a large
share of users, on the pack's fastest path, at the exact moment a CivitAI post
would send new people at it.

PyAV is a hard dependency of ComfyUI itself (SaveVideo and CreateVideo are built
on it) and its ffv1 encoder lists `rgb48le` among 61 pixel formats, so the
format did not have to change: ffv1 / rgb48le / level 3 / coder 1 / context 1,
in matroska. Verified before writing any of it, and again through the real
`HopStore`:

- PyAV encode -> PyAV decode: **bit exact**, including 0, 65535 and midpoints
- **ffmpeg encode -> PyAV decode: bit exact** -- existing caches on disk still
  read, which is the part that protects users
- file sizes within 44 bytes of each other (145,780 vs 145,824)
- a frame-count mismatch still raises rather than returning a short clip

The decode path also got slightly better on the way: it decodes into one
preallocated `(n, h, w, 3)` array instead of building a list of frames and
stacking, so there is no second full-size copy. Five documents claimed ffmpeg
was required -- CLAUDE.md, PROMPTING.md, README twice, and the on-canvas card in
the Starter workflow via `tools/notes.py`. All corrected. README's existing
"No dependencies to install" line, which already listed `av`, is now true rather
than nearly true.

## 24. The panel learns to write its own plans (2026-08-30, ALPHA)

`prompt_pack/README.md` step 7 has said the same thing since the pack shipped:

> If the node rejects the plan, paste the error straight back into the chat --
> every message names the shot or reference it came from, and one round trip
> usually fixes it.

Section 22 measured how often that is needed. Two unrelated model families, one
prompt, and both wrote a beat citing `@kitchen` while the register declared only
the people -- so `resolve_tags` raised and the queue stopped. Both were fixed by
one round trip. An instruction that reliable is a feature that has not been
written yet, so this section writes it.

**What was built.** `llm.py` talks to any OpenAI-compatible server; `planner.py`
generates, validates with the node's own checkers, feeds any error back, and
tries again up to three times; `routes.py` gains `GET/POST /h3_ref_chain/llm`
and `POST /h3_ref_chain/plan`; `js/editor/writer_bar.js` is a collapsed WRITE
section at the top of the panel. About 900 lines including the tests.

**What was deliberately not built.** The prior art is the author's other pack,
`PromptMasterLD` -- 52k lines, an LTX shot writer with 47 accents and a dial
system, driving llama-server through `backend.py`. Three of its decisions were
copied without re-litigating them, because its comments record why: the LLM
never runs during a graph execution, `INPUT_TYPES` makes no network call (a GGUF
scan once fed ComfyUI's *Missing Models* panel and offered to download weights
for users touching no local file), and there are no API keys anywhere.

Its process management was not copied. `cpld_conn.json` carries `llama_exe`, and
`backend.py:353` shells out to `lms unload --all`. That pack has no
`pyproject.toml` and is never scanned; this one was Flagged under
`python_command_injection_risk` for 0.4.1-0.4.3 and only cleared it in section
23 by migrating `store.py` off `subprocess.Popen`. Same code, different
consequence. Every rung of the unload ladder here is HTTP, which leaves four of
its five. Its `urllib.request` was not copied either -- these calls are awaited
inside aiohttp handlers, where a blocking read freezes the whole ComfyUI UI for
the length of a generation.

**The part that matters is the loop, and the part that matters about the loop is
that it was tested.** `write_plan()` takes its completion function as an
argument, so `tools/check_planner.py` drives it with a scripted model: attempt 1
returns the real A/B fault, attempt 2 returns a clean plan, and the test asserts
that the node's own error text reached the model, that the rejected reply stayed
in the conversation, and that it converged in exactly two attempts. It also
asserts the loop **gives up rather than returning an unvalidated plan**. No
server, no GPU, 25 assertions.

That test immediately earned itself. `validate()` first passed an empty set as
`wired_slots`, so no ref was ever *active* on any hop and `resolve_tags`
rejected `@kitchen` -- a tag that was declared correctly. Every good plan looked
broken, and the failure was indistinguishable from the bug the loop exists to
fix. `wired` is now derived from the file list, which is what the node does with
files it actually decoded.

**Three LM Studio facts found by running it.** First, `/v1/models` lists what is
*installed*, not what is loaded: the first live call picked a model straight off
that list and came back `HTTP 400: Model unloaded by user or API request`. The
dropdown now reads `/api/v0/models` for a `state` field, marks loaded models
`●`, sorts them first, and the 400 is translated into a sentence naming the fix.
Second, `ttl` is still not sent, for the reason recorded in `backend.py:791` --
handing lifetime to LM Studio unloaded a 26B model thirty seconds after the
prompt finished, while the panel still said the writer was warm.

Third, and this one nearly shipped as a wrong diagnosis. `max_tokens` started at
4096 and every live run came back with an empty `content`, so the code announced
*"this model answers with reasoning only and ignores both thinking switches"*.
It was wrong. Measured against the shipped prompt:

| max_tokens | finish_reason | completion | of which reasoning | content |
|---|---|---|---|---|
| 4096 | `length` | 4096 | 4093 | **0 chars** |
| 12288 | `stop` | 8868 | 8010 | 2958 chars |

The model was not refusing to answer, it was still thinking when the budget ran
out. A short prompt to the same model returns `content` and `reasoning_content`
together, which is what proved it. `MAX_TOKENS` is now 12288, and an empty
`content` is split three ways: `finish_reason == "length"` names the truncation
and the token counts, a clean finish with reasoning still triggers the
`/no_think` retry, and neither is reported as the other.

**The A/B, re-run through the loop.** Section 22 graded these two models by
hand and gave gemma the win on 2 FAIL against qwen's 4. Through the repair loop
that verdict inverts, and then stops mattering -- 3 trials each, same brief,
same 31-file reference folder:

| model | converged | attempts | wall clock |
|---|---|---|---|
| gemma4-26b-a4b | 3/3 | always 2 | 58-97 s |
| qwen3.8-27b | 3/3 | always 1 | 105-130 s |

qwen writes an acceptable plan first time and is slower doing it; gemma is
roughly twice as fast per attempt and reliably spends the saving on one repair.
Both land in about the same place. **The loop is what makes the model choice
uninteresting**, which is the strongest argument for it -- section 22's careful
grading was work that no longer has to be done by a person.

gemma's repair is not one fixed mistake: across runs it put `join` at shot level
instead of inside `directives`, and invented `hallway_window.jpg` against a
folder whose real names are `jFJ7P.jpg` and `h3_stress_kitchen_1.jpg`. The
second is worth naming -- the invented-filename error used to print all 31
available names inside every retry turn, burying the one sentence that said what
to do. It now shows twelve and a count.

**Known limits, all documented rather than discovered.** A headless or
API-submitted run gets no plan writer, which is the price of keeping the model
off the execution path. Structured output degrades to plain-text extraction on
llama.cpp builds that reject `response_format`. And the loop only catches what
the parsers can decide -- a beat that is merely bad still passes, so the WARN
tier is shown and never auto-retried.

## 25. The seam report had the sign backwards (2026-08-30)

Two 2-hop renders of the same kitchen scene at 736x416, 7 steps, seed fixed:
one at 5 s (124f) with `tone_compensate=frame_shift`, one at 8 s (192f) with
`anchor`. The seam reports called them `invisible` and `marginal`. Both were
wrong, and not in the direction the docs already warned about.

| run | seam report | true per-hop drift (hop cache) |
| --- | --- | --- |
| 5 s, frame_shift | -0.31/255 `invisible` | **+2.17/255** |
| 8 s, anchor | -1.05/255 `marginal` | **+2.63/255** |

`CLAUDE.md`'s tone notes record that seam readings come in about a third low,
because the frames either side of a cut are ~0.9 s apart in scene time and the
content change partly cancels the drift. On this scene it does not partly cancel it --
it **reverses** it. The beat has the cook set a knife down and turn toward a
window, so the scene darkens across the cut by more than the generator's
+2.6/255 brightening, and the seam lands negative. `invisible` was sitting on
top of the largest per-hop drift measured on this scene.

So the existing rule is not conservative enough. "Never use a single seam
reading to decide" is right; the reason is stronger than stated, because the
error is not bounded in magnitude *or* direction. `tone_probe` against the hop
cache stays the only honest instrument, and its standing caveat applies --
`temp/` is wiped on ComfyUI start, so probe before restarting.

**`anchor` behaved exactly as specified, on the scene type that had never
tested it.** Shot 1 was `camera=hold` -- the case section 21 named as "the run that would
justify moving off 0.35", open ever since. Logged: `anchor r-0.0050 g-0.0047
b-0.0053 (gap -3.6/255, ramp 48f)`. The arithmetic closes -- 3.6/255 x 0.35 =
1.26/255 = 0.0049 against the logged 0.0050 -- so the strength is doing what it
says and the held camera did not perturb it.

**Nothing here argues for moving off 0.35, and a 2-hop chain never could.**
Both drift figures sit in family with the measurement behind the linear model in
`CLAUDE.md` (~5/255 by hop 3, ~10/255 by hop 5). At two hops there is not
enough cumulative drift for the strength to matter; a 35% pull on a 3.6/255 gap
is ample. The test that could decide it is **4-5 hops**, where cumulative
reaches 8-13/255 and an under-strength anchor would visibly fail to keep up.

One thing that makes that test cheap: `tone_compensate` is in neither
`chain_salt` nor the per-hop key. Re-queueing the same graph with a different
mode cache-hits every hop -- no DiT load, no resample -- and yields a master
differing *only* in the correction. That is the same-seed-same-cache condition
`CLAUDE.md` demands, and it costs about 18 s rather than a full render.

The probe itself needed fixing before any of this could be read: with both runs
in the cache it differenced across renders and reported `-30.37/255`. See the
`tone_probe` commit.

## 26. The texture metric everyone reaches for is the wrong one (2026-08-31)

*Numbered 26 because 24 and 25 are on the `llm-plan-writer` branch, which is on
hold. Nothing here depends on them.*

A user running H3 chains on a different rig -- `MiniMaxH3SongMaskedAVContext`,
`source_latent`, `context_length 39` -- reported "saturation and overbaking on
close shots": skin blotchy, hair frizzed into noise, the face restructuring by
segment 4. They came with a measured report over 81 chained clips and a
fixed-seed harness, and with a question aimed at this pack: *is the latent
hand-off amplifying high-frequency energy, or is the sampler over-sharpening
the generated region to match the sharpened context it was handed?*

Their metric was mean `|Laplacian|` over the frame, end of last segment over
start of first. It gave 1.060 / 1.180 / 1.204 for 2 / 3 / 4 hops.

**On the two clips they sent, that metric reads 0.961 and 0.973.** Both faces
are visibly destroyed by the end -- frame 5 against frame 1045 is not a
close call. The metric says one of them got slightly *better*.

It is confounded twice.

It is an **area average**. A face is about 6% of a 736x1312 portrait frame, and
these clips are a talking head against wood panelling, a fleece throw and two
sconces. The background does not change; it outvotes the face roughly sixteen
to one.

It **sums every spatial frequency into one number**, so energy moving between
bands cancels. Measured on the same clips:

| | luma | global sigma | fine <1px | mid 1-2.5px | coarse 2.5-6px |
|---|---|---|---|---|---|
| TEA2 | 92.2 -> 90.1 | 59.7 -> 58.0 | x1.09 | **x1.17** | x1.03 |
| TEA3 | 106.0 -> 104.1 | 56.9 -> 54.4 | x1.33 | **x1.35** | x1.30 |

Global contrast **falls** while mid-band energy **rises**. No single scalar can
represent that, and a correction tuned against one is tuned against noise.

Three things follow, and each changes what a fix should do.

**The band is mid, not high.** "Blotchy skin" is mottle at 1-2.5 px, not grain.
A fix aimed at high-frequency sharpening aims past it.

**The climb is continuous, with no step at the joins.** TEA2's background
mid-band, in 60-frame bins: 1.57 1.55 1.56 1.56 1.57 1.60 1.68 1.66 1.61 1.64
1.71 1.71 1.72 1.77 1.85 1.85 1.93. A ramp, not a staircase. So the hop
boundary is not where the damage is injected -- it is the ratchet pawl. It
carries the degraded state forward instead of resetting it, and
`h3_ref_chain.py` hands forward `imgs[-tail_n:]`, which by this finding is the
most degraded stretch of the hop. Every hop is seeded from the worst frames
available to it.

**It is global, not face-local.** TEA2's background ratcheted *more* than the
head (x1.21 vs x1.17). The face is where it becomes objectionable, not where it
happens -- we are simply far better at reading skin than wood. So a correction
can be global, but the measurement must still report a subject box, because
that is where the acceptance threshold lives.

Their exposure anchoring was on and working: luma holds at 92 -> 90 across 44
seconds. The texture ratchet is independent of it. That matches the table --
coarse band roughly flat, mid climbing -- and it is why the existing tone work
never touched this.

### What got built

`tools/texture_probe.py`. Three Gaussian-difference bands, a subject box against
a background control, and the within-hop slope as well as the per-hop step. It
reads the hop cache's pre-correction FFV1 frames, or any video via `--video`,
which is what makes it usable on someone else's rig. It prints mean
`|Laplacian|` next to its own numbers, because "the head gained 34% mid-band
and the Laplacian says 0.973" is a better argument against that metric than a
paragraph is.

`tools/check_texture.py` drives it against a synthetic cache with a ratchet of
**known** amplitude injected. This is not ceremony. §21's instrument shipped a
confident wrong number for weeks because nothing had ever read it against a
signal whose answer was known in advance, and this one caught two defects while
being written: `slope_pct` reported percent-per-frame under a per-100-frame
label -- a hop that doubled read as "+2.4%" -- and the first fixture's
"mid-only" injection was a full-window difference of Gaussians whose tails
landed squarely in the coarse band, so the test was measuring its own spectral
hygiene rather than the probe's.

`tools/hopcache.py` now holds the cache reader and the chain segmentation,
shared with `tone_probe` instead of copied. That segmentation is precisely what
was wrong in §21; it must not exist in two places. `latents.py` lifts the
NestedTensor shim out of `h3_ref_chain.py` so a tool can read a cached latent
without importing ComfyUI -- the same reason `plan.py` and `tone.py` have no
ComfyUI imports.

### What is deliberately not built yet

The lever. `_condition_pin_latent`'s `pin_renorm` matches one scalar sigma per
latent component, and the pixel evidence says sigma and the damaged band move in
opposite directions -- so a band-aware rescale is the obvious next move. But
that is an argument about pixels, and the lever acts on latents. Whether the
*latent's* band structure drifts the way the pixels' does is unmeasured, and
`texture_probe` now prints exactly that (`latent [0] sigma ... hi ...`) from a
cached hop.

Measure first. A 4-5 hop chain with `cache_hops=on`, probed before the restart
that wipes `temp/`. Two hops cannot show this: the reporter's own numbers only
separate at three.

### Also found, by reading

`_condition_pin_latent` is applied to `pin_latent = prev_sampled`, which only
the `motion_context` branch of `_pin_continue` consumes. The `addguide_pixels`
fallback takes raw `prev_imgs` and gets **no conditioning at all** -- and a
cache hit whose latent did not serialise lands there silently. AddGuide also
re-encodes decoded pixels, which is the decode/re-encode round trip the reporter
measured at 1.530, "much worse", on their own rig. A chain that quietly fell
back has both levers dead and the worse hand-off. The log says which pin ran;
it is worth reading before trusting any A/B.

### Correction, 2026-09-01: it is a staircase, not a ramp

The section above says the climb is continuous with no step at the joins. That
was wrong, and it was wrong in the way that matters most -- it is the claim
that decides where a correction belongs.

It came from binning the reporter's master at 60 frames **without knowing where
their joins were**. A step function sampled that way, with content noise on
top, reads as a ramp if you want it to. The inference was under-determined and
I did not say so.

The hop cache settles it, because there the boundaries are known. A 3 x 243f
chain, 736x1280, overlap 22, Motion-Context pin, `pin_renorm off`, head box,
mid band, with the regenerated overlap frames excluded:

    hop 1   0.00985 0.00976 0.00963 0.00992 0.00981    last/first 0.996
    hop 2   0.01050 0.01025 0.01029 0.01033 0.01053    last/first 1.003
    hop 3   0.01058 0.01025 0.01044 0.01058 0.01076    last/first 1.018

    join 1 -> 2   tail 0.01006 -> body start 0.01048   x1.042
    join 2 -> 3   tail 0.01068 -> body start 0.01113   x1.042

Flat inside every hop. **The same +4.2% at both joins.** Those two frames are
adjacent in scene time -- hop N+1's frame 22 continues from hop N's last -- so
it is a genuine discontinuity and not a gap the scene moved through.

Re-reading the reporter's bins with this in hand, theirs is a staircase too:
1.57 1.55 1.56 1.56 1.57 | 1.60 1.68 1.66 1.61 1.64 | 1.71 1.71 1.72 1.77 |
1.85 1.85 1.93 -- four plateaus at ~1.56, ~1.64, ~1.73, ~1.88, stepping +5%,
+6%, +9%, on a chain they told us was four hops. Both rigs agree. I had the
right data and read it wrong.

This is better news than the original reading. "Self-conditioning drift inside
the generation" could only ever be damped; a step injected at the hand-off can
be removed at the hand-off, and the hand-off copies are conditioning-only.

### And the latent measurement, which was the point

From the same cache, per hop: component [0] sigma `1.0414 -> 1.0376 -> 1.0289`,
its high band `0.3794 -> 0.3811 -> 0.3809`.

Sigma **falls 1.2%** while the pixel mid band climbs 8%. The high-band
*fraction* -- hi/sigma -- goes `0.3643 -> 0.3673 -> 0.3702`, up 1.6% and
monotone. So the latent does carry the tilt, and total sigma does not see it.

`pin_renorm=on` would have multiplied this pin by `1.0414/1.0289 = x1.012`,
scaling every band up uniformly, on a latent whose high band was already 1.6%
too hot. **On this chain the shipped lever pushes the wrong way.** That is not
a small correction to it; it is the wrong statistic, and Phase 2a's band-matched
rescale is now evidenced rather than assumed.

One caveat kept in view: 1.6% in the latent against 8% in pixels. The VAE
decode is nonlinear, so the two are not expected to be proportional, but the
gap is large enough that the lever's gain will have to be fitted against
measured output rather than derived from the latent ratio.

### Two probe defects the real data exposed

**Cached latents did not load at all.** `torch.load` has to import
`comfy.nested_tensor` to rebuild the object; without the ComfyUI root on
`sys.path`, `store._get_latent` caught the ModuleNotFoundError and the probe
printed "none cached for this hop" -- reporting a path problem as an absent
latent. `hopcache.enable_latent_reads()` appends the root and nothing else;
the module imports only torch when pickle reaches for it, so it is safe to run
beside a queued render.

**Band energy was not exposure-normalised.** The probe's own docstring claimed
band-pass output "does not care about the local mean", which is true of an
offset and false of a scale: brighten a frame 5% and every band grows with it.
The 3-hop chain's luma rose 4.7%, so whole-frame mid read `x1.084` when the
texture part was `n1.035`. Both columns are printed now. The head box was
unaffected either way -- the brightening was in the background -- which is
exactly the kind of thing a single whole-frame number cannot tell you.

## 27. The band lever, and why the old one could never have worked (2026-09-01)

Built after §26's correction, on the finding that the ratchet is a step at the
join: +4.2% mid-band, twice, identically, on a 3-hop chain. Two identical steps
is already a model -- constant multiplicative step per join, geometric in hop
count. It predicts hop 3 at 1.042^2 = 1.086 against 1.079 measured. So the
shape did not need a 4-5 hop run to pin down, which matters: those runs are
expensive enough that the user does not do them.

`pin_renorm` is now `["off", "sigma", "band"]`. `"on"` maps to `"sigma"`, so
pre-0.5 workflows keep their behaviour, and the combo keeps its widget slot --
adding options is safe, adding widgets is not.

### The old lever is a no-op, provably

The statistic that drifts is the high-band **fraction**, hi_sigma / sigma. A
fraction is invariant under uniform rescaling, and a uniform rescale is the
entirety of what `sigma` mode does. Driven end to end through
`_condition_pin_latent` with a 12.74% band drift planted in hop 2:

    mode=off     ratio after 0.3526 (anchor 0.3128)  err +12.74%
    mode=sigma   ratio after 0.3526 (anchor 0.3128)  err +12.74%   x0.9651 applied
    mode=band    ratio after 0.3127 (anchor 0.3128)  err  -0.04%   hi x0.8339

`sigma` applied a real scale factor and moved the drift by nothing at all. This
is stronger than §26's "corrects the wrong way": there is no gain, no strength
knob and no anchor choice that makes a scale-invariant statistic respond to a
scale. The lever was mis-specified, not mis-tuned. It is kept only for the
workflows that saved it.

### The fixed point that nearly shipped

`match_band` first computed `k = target * sigma / hi_sigma`. That is wrong in a
way that hides: scaling the high band changes the sigma it is a fraction of, so
the target moves while you apply it. It landed at 0.3331 against a 0.3168
target -- 5% short, in the right direction, which is the worst possible
signature because it looks like it works.

Now it solves the orthogonal fixed point in closed form,
`k = r*L / (H*sqrt(1-r^2))`, then refines two or three passes against the
statistic as actually measured, because a difference of Gaussians is not an
exact projection. Lands at 0.3167 against 0.3168.

### The fixture was also wrong, and would have hidden it

The first test used `torch.randn` for the latent. White noise has a high-band
fraction of **0.966** -- pinned against its ceiling of 1.0, where lifting the
high band moves the statistic by 0.6% and the clamp does all the "correcting".
Every assertion about the lever would have been measuring the clamp. Real
latents sit at 0.3643, so the fixture is now built to land near there and an
assertion holds it in that regime.

The safety property is asserted as "the entire change lies along the high band"
(cosine with `hi` > 0.99), not as "the low band is unchanged" -- re-splitting
the result does not hand back the same `lo`, because the split is not a
projection. The first version asserted the false one and failed correctly.

### One cache key narrowed

`pin_cond` was in every hop's key including hop 1, which has no pin --
`_pin_mech_for` returns `"none"` at index 0 and the conditioning branch is
`elif i > 0`. So flipping a lever discarded a byte-identical cached hop 1 and
re-rendered it. That is a third of the cost of every lever A/B, on the one hop
that provably could not have changed. Now keyed only from hop 2.

### Still unknown

The latent's band fraction moved 1.6% across the chain while the picture's mid
band moved 8%. The decode is nonlinear so they are not expected to be
proportional, but a full match to hop 1's fraction may therefore under-correct
the picture. That is one A/B to find out, and it is readable off a 3-hop run:
`texture_probe` reports each join separately, so two joins is two data points.
If `band` shrinks the +4.2% step but does not close it, the next move is a gain
above 1.0, fitted -- not guessed.
## 28. A music bed that does not bury the dialogue (2026-09-01)

*(Numbered 28 because 24-25 are on `llm-plan-writer` and 26-27 on
`texture-ratchet`, both unmerged. Section numbers are cheap; renumbering a
merged history is not.)*

A user asked for a soundtrack over the whole chain -- not an audio *reference*,
which H3 already takes as a voice, but a track laid under the finished thing.

The first decision was where it goes, and it decided everything else. H3 writes
its own audio per hop and `_xfade_audio` joins it at each seam, so the bed is
applied ONCE, after the last hop, immediately before `master_audio` is built. It
is therefore downstream of every latent, every pin and every cache key: it
cannot move a generated frame or sample, only decide what is laid over them. The
same property that made the texture work safe -- correcting something nothing
renders from -- is what makes this safe, for the opposite reason. It also means a
cached chain can be re-mixed at a new level for the price of the mix alone.

Three things in `music.py` are there because the obvious version is wrong:

**Resample explicitly.** A 48 kHz track dropped into a 44.1 kHz master plays 9%
fast and a semitone sharp. That reads as "the model generated bad music", not as
a bug in the node, so it would have been reported as anything but what it was.

**Crossfade the loop wrap.** Butt-joining a loop leaves a step discontinuity,
i.e. a click -- and a click on a fixed period is the most audible artifact
available, worse than the seam it came from. Equal-power cos/sin, the same law
`_xfade_audio` already uses; two different fade shapes in one output is an
argument waiting to happen.

**Duck against the 95th percentile, not the peak.** A single shouted word would
otherwise set the scale and leave ordinary dialogue barely ducking at all --
which is the common case in the podcast clip this was asked for. Fast attack,
slow release: the reverse lets the first syllable of every line collide with the
music, and the first syllable is the one a listener needs to follow a sentence.

The envelope runs at a 1 kHz control rate. A one-pole attack/release filter is
sequential, so at 44.1 kHz a 40 s master is 1.8M Python iterations -- about a
minute of dead time on a node whose whole job took ten. At 1 kHz it is 40,000,
and 1 ms resolution is far finer than the 10-400 ms attacks that matter.

The peak guard trims the whole mix rather than only the bed, and says so in
`info`. Ducking the bed further to fit would change the balance the user set, by
an amount they cannot predict, without telling them.

`tools/check_music.py` is 31 assertions on synthetic material, because every
defect here is inaudible in a still and invisible in a frame count. The one that
matters is the last: with nothing wired, `apply()` returns the master object
itself. That is the whole claim that the feature is opt-in.

**Four widgets, appended.** `widgets_values` is positional, so they go at the
bottom of `optional` and the two shipped workflows grew four values. The AUDIO
socket costs no widget slot -- `check_workflows.py` already knew that, and
caught the count mismatch before the workflows were updated.

## 29. The writer stays warm until the render asks for the card (2026-09-01, ALPHA)

*(29 because 26-27 are on `texture-ratchet` and 28 on `soundtrack`, both
unmerged. Numbers are cheap; renumbering a merged history is not.)*

`unload_after` handed the VRAM back the moment a plan was written. That was
right when there was nowhere else to put the eviction, and wrong for the way the
feature is actually used: nobody writes one plan. They write one, read it,
change the brief and write another -- and every one of those paid a full model
load, tens of seconds on a 27B, to free memory that nothing was waiting for.

The card is contended at exactly one moment, and it is a moment we can see
coming. So `keep_warm` is the default now and the eviction moved to the top of
`run()`, where the render is about to need the memory. `unload_on_run` is what
makes that safe rather than merely convenient; `keep_warm` off restores the old
behaviour for a machine too tight to hold the writer at all.

**`free_for_render` blocks, and that is correct.** The rule at the top of
`llm.py` -- never block -- is about aiohttp handlers, which run on ComfyUI's
event loop where a stall freezes the entire canvas. `run()` is the execution
worker thread, nothing waits on it but the render, and the render is what the
VRAM is being freed FOR. The docstring at the top now names this as the single
exception, because otherwise the next reader "fixes" it.

**It costs nothing when it does nothing**, which is the majority of renders. The
first gate is `configured()`: a filesystem check for a settings file that has
never been written. No socket, no DNS, no 4 s timeout against a port with
nothing behind it. The settle is skipped whenever the eviction found nothing
resident, and `shares_this_gpu` still refuses a writer on another machine before
any of it.

**The settle is a guess about someone else's hardware.** The unload endpoint
returns when the server drops its reference, not when the driver has released
the allocation, and on a slower card those are not the same instant. Default 5 s,
settable, capped at 60 -- a pause long enough to look like a hang is worse than
an OOM you can read -- and announced in the console, because it lands right
after the queue button, the moment a user is most primed to read a stall as a
crash.

`check_planner.py` proves the quiet paths without waiting for any of them:
`free_for_render` takes a `settle_sleep` callable, so a test can assert that
nothing slept. It also redirects `_conn_path` at a temp file first -- a checker
that overwrites the user's real writer settings as a side effect of passing is
not one anybody should run.

**Suggest, don't set.** `2450d96` wrote `shot_plan` and `ref_plan` the moment a
draft converged. A plan silently rewritten under you is worse than no plan, so
the bar now holds the JSON until Accept. Discard leaves the cards as they were.
The route is unchanged; only the last inch of the panel moved.

**`asyncio.run` cannot nest.** ComfyUI's execute path is async, so `run()` is
already inside a running loop when it calls `free_for_render`. `asyncio.run
(unload_all(...))` then raised `RuntimeError` and left the coroutine un-awaited
-- keep_warm + Queue printed that, and the 27B stayed resident. `_run_coro`
uses a side thread with its own loop; the function is still blocking, just not
on the UI loop. Proven in `check_planner.py` by calling `free_for_render` from
inside `asyncio.run`.

**The rail is the scene.** Write plan used to POST only `{brief, hops}` and
then list every file in `h3_refs`, so a user who had already put two pictures
in the boxes got a register full of files they never chose and an empty
subject card -- the model never saw the stills. The bar now sends the filled
rows; those tags and filenames are locked; the stills ride the first user turn
as vision parts (768 px JPEG, executor, not a widget). A text-only model that
400s on `image_url` falls back to filenames and says so. Missing
`subjects.{n}.name`/`locked` is an error for the writer, so the repair loop
fills the box instead of Accept writing an empty one.

qwen 3.8-27b then wrote a correct script and kept the rail's files, and still
emitted `"subjects": {}` on all three attempts -- structured output treats an
empty object as valid, and "rewrite the whole plan" never added the block.
A subjects-only repair overlays the last register; if that still comes back
empty, name/locked are filled from each ref's `desc` so the draft is not
thrown away. The panel shows a partial draft on give-up.

The next live run tagged the rows correctly, then a repair that re-emitted
the refs without `desc` and with `subjects: {}` wiped the describe-this-photo
and current-standing-state boxes. Empty values no longer win a merge.
Pinned writes now require `desc` on every ref and `name`/`locked`/`context`
on every subject -- those three fields are the point of attaching the stills,
not optional flavour.

## 30. Which nine seconds? (2026-09-01)

The soundtrack shipped and worked, and using it for ten minutes found the hole.
The track was 173 seconds, the chain was 9.4, and `music_fit=loop` takes the
first 9.4 seconds -- which on a mastered track is the intro. There was no way to
say *which* nine seconds.

The same hole was on the other two media inputs. On one of them it is not a
convenience issue at all. `MiniMaxH3ReferenceToVideo` passes the whole voice
file to `_encode_ref_audio` with no cap, and every latent frame that produces is
a token the DiT attends over on **every step of every hop**. An untrimmed
three-minute voice reference is a large, silent, permanent tax that nothing in
the UI ever mentioned. The reference clip was truncated to the hop length, but
only from frame 0, so you could not point at the motion you actually wanted.

**Peaks are computed on the server.** The first design decoded the file in the
browser with `decodeAudioData`. For this track that is roughly 66 MB of Float32
held in the tab, per control, to draw a picture 240 pixels wide. PromptMasterLD
has four separate trim controls and not one `decodeAudioData` between them --
it sends 240 numbers. So do we. The decode runs in an executor, because these
handlers share ComfyUI's event loop and 1.68 seconds on it stops the canvas, the
queue and the progress bar together.

**Bucket by max, not mean.** A mean flattens transients into a smooth sausage,
and transients are the only landmarks you can trim against. The whole reason to
look at the picture is to find the downbeat.

**`seconds` comes from the decoded sample count.** MP3 Xing/LAME headers
routinely report double the real duration, and a duration that lies makes every
position on the bar lie with it. We already decode, so the honest number is
free. Measured on the file that started this: 173.49 s, matching the samples.

**`end == 0` means "to the end of the file"**, and `media.clip_window` is the
only definition of what a window is. Four readers have to agree -- the voice,
the clip, the soundtrack and the peaks route -- and four copies of that
arithmetic would eventually disagree by a rounding rule. It can never return an
empty span: reversed, negative, past-the-end and shorter-than-50 ms all fall
back to the whole file. A trim that did not take is a puzzle; an empty tensor is
a crash from inside the model naming neither the file nor the widget.

**Per-reference megapixels, and where they are not.** H3 scales each reference
down from its native size and each becomes `latent_h * latent_w` entries in the
DiT payload, so a location plate costing what a face costs is waste. That is a
token dial and it is now a field in `ref_plan`. It is deliberately **not**
offered on the first frame: `MiniMaxH3AddGuide` does
`_resize(image, width, height, "center")`, so whatever you feed it becomes
exactly the canvas, and the control would have been wired to nothing. The
control the first frame actually lacks is a crop box -- a 9:16 source on a 16:9
canvas silently loses both sides -- and that is still open.

**The hop cache needed no change**, which is worth recording because it looked
like it would. `chain_salt` digests the loaded *tensors*, not the filenames or
the settings, so a trimmed voice is already a different key and a downsized
reference is already a different key. Keying on pixels rather than on parameters
paid for itself here without anyone planning it.

`tools/check_waveform.py`: 38 assertions, green first run. `widgets_values` went
39 -> 45; `check_workflows.py` caught it, which is the third time that checker
has earned its place.

**TASK 2, verified 2026-09-02.** Header-lie, soundtrack trim, and per-ref mp
were signed off in the GUI. Voice trim reaches the encoder (hop-cache miss);
male timbre will not override a woman in frame at 8-step turbo. Null check:
every window 0/0, empty `voice_file` / `soundtrack_file`, A's graph vs
`efd6a3e` — hop 1 and hop 2 FFV1 frames and `.npy` audio bit-identical
(contact_sheet_00039 vs 00043; pin σ 0.9578/0.4434). Hop *keys* differ because
this branch added `voice_on`; that is a key-field change, not a generate
change. Old workflows: widget names from 0.4.5 (34) and `efd6a3e` (39) are a
prefix of this branch (45); Starter.json from both revs maps seed / steps /
shot_plan onto the same widgets. `user/default/workflows` still use the
legacy `H3RefChain` id, on purpose.

## 31. The lints that cried wolf, and the field the writer ate (2026-09-02)

A day of GUI renders against two writer models. Nothing in the renderer was
wrong. Four things in the *plumbing around* it were, and three of them had been
telling the author to fix work that was already correct.

**A Write plan silently deleted every reference cap.** `railRefs()` and
`_pinned_refs()` both built their five fields and dropped `mp`; the model
cannot author one (it is not in the prompt's valid-field list); and Accept
overwrites the register wholesale. So the caps survived exactly until the next
write. chain_00047 ran three plates at 0.54 MP -- 1.58 MP of stills against a
0.72 MP canvas -- and came back coherent. One rewrite later the same three were
at native size, 3.23 MP against the same canvas, and the render opened on the
kitchen plate reproduced almost verbatim and warped the subject in. The ratio
tracked the result across four renders: 2.2x coherent, 3.4x coherent picture
with gibberish audio, 4.5x cooked. `load_image`'s own docstring had said why
for weeks -- "a location plate costing as much as a face is waste" -- and
nothing enforced it because nothing kept it.

The dial was also invisible. `REF_MP` offers 0.3/0.5/0.7/1.0/1.5/2.0, `select`
assigns a value matching no option, and a blank control is labelled `full`. A
row capped at 0.54 read as uncapped while it was working. Two hours were spent
tuning megapixels that had already been erased.

**The schema never required the ref_plan.** `properties` listed both documents
and there was no top-level `required`, so `{"shot_plan": ...}` alone was valid.
qwen3.8 volunteered both and hid it for the whole life of the feature.
gemma4-26b emitted only the shot plan, on every attempt: the register stayed
empty, `_remap_pinned_tags` bailed on the falsy `ref_text` before it could
restore the rail's names, and each repair turn was told its beats cited
undeclared tags -- so it rewrote the beats it had already got right and never
emitted the document that was missing. Three attempts, no convergence. With the
requirement in place: two attempts, correct register, zero warnings.

**`check_coherence` tested "framing is named" where it meant "framing
changed".** Its own docstring says a framing CHANGE fights a continuous join
with a held camera. The test never compared against the previous shot, and
models restate the framing on every shot because the axis describes the shot
rather than a transition -- so `medium/medium/medium` tripped it on every hop
after the first while the framing never moved. Both writers hit it on
essentially every plan. chain_00052 carried the banner and seamed at
**-0.31/255**, one of the cleanest joins measured here. That was the evidence,
and it read as a curiosity for hours before it read as a bug.

**`check_place_handoff` fired on chains that never leave.** The abandonment
half is about a destination the film moves to and then holds with nothing. It
tested only whether a place plate rides the last shot, so a two-hop kitchen
scene that drops the plate after hop 1 was called a defect -- when that is the
pin-only recipe the renderer is built for. Distinct places *cited in beats* is
the test now; under two, the film never leaves.

**What the renders actually taught, separately from the bugs.** A continuation
hop carries a small motion, not a relocation. Three pin configurations --
plates on hop 2, plates off hop 2 (`0 identity stills`), and overlap raised
from 0.9 s to 1.6 s -- all produced the same hard cut at frame 192 for a beat
reading "gets off the counter and stands to face the window", while a beat
reading "stays seated, shifting her weight" continued cleanly at -0.31/255. The
pin length is not the lever; what the beat asks of the first frame is.

`overlap` moved out of `chain_salt` into the per-hop key from hop 2, for the
reason `pin_mech` was never in it: the trim and the pin are both hop-2+ work,
so hop 1's pixels cannot depend on it, and keying it chain-wide made every
overlap A/B re-render a byte-identical hop 1.

**The writer was never told the hop length.** The prompt ships a five-row
length table and instructs the model to ask when it has not been told, which a
button cannot answer. The node has known the duration all along and nothing
carried it, so beats were sized to a guess -- 75 words into 8 s hops, one
spoken line where the row allows two, 26.7% voiced. The panel sends `duration`
now and the band is named outright rather than left as a table to look up.
`beat_table()` parses it out of SYSTEM_PROMPT.md rather than restating it, the
same reason `schema()` reads SCHEMA.json.

**Still open.** A desc can be confidently wrong about its own photograph --
gemma4-26b called a white ribbed crop top "a dark top" and a daylit wooden
kitchen "a dark kitchen interior with blue light and tiled surfaces", and no
lint can check prose against a picture. qwen3.8 did not make that class of
error on the same three plates.

## 32. A silent pin does not buy you a silent opening (2026-09-02)

Hop 2 opened on invented speech -- a burst of nothing-words over the walk,
before the line it was actually given. The chain was clean everywhere else and
the seed was fixed, so the first guesses were all mechanical, and all wrong.

Ruled out, in order. The **LoRA**: exonerated by the user across a run of
low-res A/Bs. **`pin_renorm=band`**: it skips the audio component outright --
"an audio component has no bands; leaving it alone is correct, not a
fallback" -- so it never touched the track. **A mid-utterance handover**: the
hypothesis was that one second of audio context caught hop 1 mid-word and hop 2
finished it. Hop 1 ends silent. There was no word to finish.

What was left is the beat. `SPEECH_MIN_SHARE`'s comment had already written the
mechanism down for the whole-hop case -- "the model fills them itself, as
fragments or as invented dialogue" -- and rules 3 and 6 of SYSTEM_PROMPT both
say to give silence a sound. Both are stated per hop. Hop 2 was walk-then-talk:
it *had* dialogue, so rule 6 did not reach it, and its opening seconds carried
a picture with no audio assigned. Rule 5 covers the join -- arrive silent
before the previous shot ends -- and the user had done exactly that. Ending hop
1 quiet gets you a quiet *pin*. It does not write hop 2's first two seconds.

So the hole was granularity: every rule about unassigned audio was whole-hop,
and the failure was sub-hop. Rule 3 now says so in both prompt documents, the
checklist gained a line, `build_user_turn` names the case, and `validate` warns
when more than `LEAD_IN_MAX_WORDS` of action run before the first spoken line
with no sound named anywhere in the beat. `spoken_spans()` is split out of
`count_beat` because where the first line *starts* turned out to be its own
question.

The user's own fix on hop 1 was the same shape, arrived at independently --
"she silent smiles at the camera and waves, after a pause she says". Worth
noting which half did the work: `check_templates` bans "silent" and "silence"
in a beat, because at cfg 1.0 with no negative branch nothing subtracts. The
smile and the wave are what filled the frames.

**Still open.** `SPEECH_WPS = 2.5` rests on one measurement (chain_00059 hop 1,
six words, 1.8 s voiced). Hops 2 and 3 of chain_00060 are confirmed-good
dialogue and would make a better basis. `LEAD_IN_MAX_WORDS = 8` is reasoned
from the length table, not measured at all.

## 33. The word rate is an English number (2026-09-02)

The first live write after section 32 was the proof it wanted: asked for a
three-hop Korean vlog, the writer opened every hop with a named sound before
the line -- *"The sound of heavy footsteps and distant city traffic fills the
air before she speaks"* -- and the lead-in lint stayed quiet on all three,
correctly.

The same run broke something else. `SPEECH_WPS` counts whitespace tokens, which
is only a speech rate in a script that puts spaces between words. The Korean
line was 8 tokens and 27 syllables: the lint called it 3.2 s against a real
4-5 s, warned on all three hops when two were fine, and -- the harmful half --
told the author to write "roughly 25 words", which in Hangul is about 85
syllables and three times the hop it has to fit in.

`speech_seconds()` now counts CJK by syllable at `SPEECH_SPS = 5.5` and
everything else by word, adding the two so a mixed beat is estimated correctly
rather than in whichever script dominates. Punctuation stranded by the split
stops counting as a spoken word, which was quietly buying every CJK line most
of a second. The shortfall warning and `build_user_turn` both name the target in
the unit the line is written in.

Nothing here is measured on this model. 5.5 syllables a second is a reference
figure for conversational Korean, Japanese and Mandarin alike, and `SPEECH_WPS`
still rests on the one English measurement in section 32. Both are honest
starting points and neither is evidence.

## 34. The dropdown was showing a model nobody had chosen (2026-09-02)

An Arch user, one day after 1.0.0: the node "isn't auto loading the model even
if it sees it, jit is enabled"; selecting one and pressing WRITE "says no model
selected even though it is"; and "i can eject the model which is odd".

Three symptoms, one empty string, and nothing to do with Linux.

`CONN["model"]` starts as `""`. The panel builds the dropdown and marks an
option `selected` only when `m.id === conn.model`, which never matches `""`, so
no option was selected and the browser fell back to displaying `option[0]`.
Falling back is not choosing: **no `change` event fires**, and `change` was the
only thing wired to save. The panel therefore displayed a model the server did
not have.

Everything downstream followed from that one value:

- `_plan` refuses on `if not conn.get("model")` with the exact words the user
  quoted back.
- Nothing is ever asked of the server, so JIT has nothing to load. "Sees it" is
  `/v1/models`, which lists what is *installed*, not what is resident -- the
  distinction section 29 already had to make once.
- Free VRAM still worked, because `unload_all()` ignores the configured model
  by design (section 29 again: the button exists for when JIT loaded something
  other than what was asked). The user read that as odd. It was the clue: the
  fault was in the saved value, not the server.

It is guaranteed for the ordinary case, not a corner. `models()` sorts loaded
models first, so the model you have loaded *is* `option[0]` -- and clicking the
entry already on screen fires nothing. Anyone running one model hits it on
every fresh install. It escaped a release only because the machine it was
developed on has had a populated `htc_llm.json` since 0.2.

`loadConn()` now adopts what is on screen when the stored value is empty, and
only then. The comment guarding this spot was right that `option[0]` must not
overwrite a working setting; it had over-corrected into never writing one.

**And a second hole, found underneath it.** `save_conn()` wrote `htc_llm.json`
into the pack directory, swallowed `OSError` with a `print`, and returned as
though it had saved -- the route answered `ok: true`. A read-only or root-owned
`custom_nodes`, which is how a system-wide ComfyUI or any container image is
laid out, produced the identical "no model is selected" from a completely
different cause, with the only evidence in a console nobody was reading. It now
returns `saved` and `save_error`, and the panel says "set for this session, but
not written to disk" -- which is true, because `CONN` is a live module global
and the setting really does work until a restart. Whether that file belongs in
the pack directory at all is still open; ComfyUI's `user/` survives a reinstall
and this does not.

The lesson is the older one, in a new place: a control that *displays* a value
it has not committed is worse than one that displays nothing. Section 31 caught
a lint that cried wolf; this is a dropdown that cried yes.

## 35. The rail already had the answer (2026-09-02)

The Arch reporter from section 34, writing their first plan once the panel
worked:

    attempt 1: subject 1 is missing name, locked, context;
               @hero_face must keep file 'gibsonlethal.webp', not
               'hero_face.webp'; @hero_face needs desc
    attempt 2: subject 1 is missing name, locked, context;
               @hero_face needs desc; @hero_outfit needs desc
    repair turn: desc and subject prose are required by the schema
    wrote a 3-hop plan in 3 attempt(s)

It converged, one attempt from failing. The interesting line is the middle one
on attempt 1, and not for the reason it looks like.

`_only_register_prose_gaps` fires the tightened-schema repair -- the one that
removes the empty path from the grammar, because section 31 established that
while `"subjects": {}` is legal it is also the cheapest legal completion and no
amount of repair prose outvotes it. That gate requires EVERY error to be a
prose gap. A file mismatch is not one. So attempt 1 got the weak generic
"change only what the errors name" turn, attempt 2 produced the same subject
error again, and only then -- with the file error gone -- did the mechanism
that actually works get to run.

**One misnamed file cost two attempts: its own, and the round it kept the real
repair from firing in.**

It was never the model's field. The rail pins a tag to a picture, and
`validate` is holding the correct filename in `by_tag` at the moment it rejects
the plan for not having it. This is the argument already written down for `mp`
in `_restore_rail_only` -- *spend an attempt on a rejection the rail already had
the answer to* -- and `file` is a stronger case than `mp` ever was, because the
model is not even guessing: it is renaming a real file to match the tag it was
given. `gibsonlethal.webp` becomes `hero_face.webp`. Tidy, and wrong.

`_restore_pinned_files` now puts it back before validate sees it, keyed on a
real tag match, and prints what it changed.

**Why it is not simply another `RAIL_ONLY_FIELD`.** That loop drops a field the
rail cannot supply, which is exactly right for `mp` -- an invented megapixel cap
is never wanted -- and destructive for `file`. On a brief-only write the rail is
empty and the filename the model read off the folder listing is the only one
there. The repair has to touch pinned rows and nothing else, which is the whole
difference between restoring a field and owning one.

Checked against the reported errors verbatim: three errors give the weak turn,
the same three minus the filename give the tightened one. The plan that took
three attempts should now take two, and a rail whose filenames do not resemble
their tags -- which is most rails, since photographs arrive named by the camera
or the download -- stops being a hazard at all.

The older lesson underneath: every field the node can determine and chooses to
reject instead is an attempt spent, and attempts are a budget of three.

## 36. A generator that fails quietly is worse than one that dies (2026-09-02)

A ComfyUI dependency install, run to satisfy some other pack's requirements,
uninstalled Pillow and did not finish putting it back. About 180 of the 211
files in its manifest were gone, `__init__.py` and `Image.py` among them, so
`import PIL` resolved to an empty namespace package. Four checks in the suite
died on it, which is the suite working.

What the suite did not catch is what `gen_schema.py` did next. It reads the
duration table off the node, and that import needs `comfy.model_management`,
which needs PIL. The import raised, and this was the handler:

    except Exception:                                # pragma: no cover
        durations, frames = [], {}

So a routine regeneration wrote a `SCHEMA.json` whose `duration` enum was `[]`
and whose `x-duration-frames` was `{}`. The schema that exists to constrain the
writer's duration field stopped constraining it, in a file that is committed,
published, and fed to the model as a grammar. Nothing downstream complains
about an empty enum — that is precisely what an empty enum means.

It was caught by `git status`, run for an unrelated reason. That is not a
control. Ten minutes either way and it ships.

`build()` now raises, with the cause and the instruction to fix the
environment rather than the file, and `main()` refuses to emit a schema with no
duration table at all — belt and braces, because the enum could empty for a
reason nobody has thought of yet. Verified by reproducing the original
condition rather than trusting the reasoning: a `PIL` package on `PYTHONPATH`
that raises `ImportError` on import, then both entry points checked. Both exit
1 with a readable message; the file's hash does not move.

**A correction worth recording, because the first diagnosis was wrong.** The
note written at the time blamed `--check` for rewriting the file. It does not;
it only reads and compares, and always did. The corrupt write came from
`gen_schema.py` with no arguments, run by hand in a diagnostic loop. The
distinction matters: the fault was never in the comparison path, it was in
`build()` returning a plausible-looking empty answer to a question it could not
answer. Fixing `--check` would have fixed nothing.

The general shape, and section 34's lesson one level down: a control that
displays a value it has not committed is bad, and a generator that emits a
value it could not compute is the same bug wearing a different hat. Neither
one fails; both produce something that looks like an answer.

## 37. Asking the OS who we are (2026-09-03)

1.0.1 came back Flagged from the Comfy registry with one finding, down from
1.0.0's three: the `.comfyignore` that stopped shipping `tools/` cleared the
`subprocess.run` and `importlib` hits, which is the first hard evidence that
the exclusion is honoured inside the published zip.

What is left is `python_network_operations`, matching the literal string
`aiohttp.ClientSession` four times in `llm.py`. 0.4.5 is still Active because
that file had no HTTP client at all until 1.0.0 -- the WRITE panel brought one.
So this is new code meeting an old rule, not a scanner that moved.

That finding cannot be coded away. The rule has no taint analysis and never
looks at a destination, `DEFAULT_BASE` is `127.0.0.1:1234`, `shares_this_gpu`
refuses to send an unload anywhere but this machine, and every alternative
client -- requests, httpx, urllib -- sits in the same rule family. Rewriting to
dodge a string match would be worse code in exchange for nothing.

A user's tip is what made this release worth cutting anyway: *"don't include
Claude's test workflows, or os probing."* The first half was already done. The
second half was true and I had not looked for it.

`shares_this_gpu` answered "is the writer on this machine" by asking the OS for
our own name, resolving it to every address behind it, resolving the target,
and intersecting the two sets. The intent is local and the function exists to
*prevent* reaching across a network -- it was added after someone's laptop had
its model evicted by a desktop. None of that is visible to a static scanner,
which sees a program enumerating its own host's identity, and that reading is
fair. It happened not to fire this round. It is exactly the shape that does.

It binds instead. An address binds only if this machine holds it, which is the
question, asked directly rather than by comparing two resolver results. It is
also the better implementation: no name lookup, correct for addresses a name
lookup would never have returned, and immune to a stale hosts-file entry. The
LAN case that motivated the original -- LM Studio local, typed in as
`192.168.x.x` rather than `localhost` -- still works, which a loopback-only
rewrite would have silently broken.

Two things worth keeping from how this went. The first draft removed the call
and then explained the removal in a docstring that used the function's name
twice, leaving the string in the file for a scanner that matches strings; the
count went from two to zero only after rereading. And the guard in
`check_planner.py` is a source-level assertion rather than a behavioural one,
because both implementations agree on every input a checker can name. The
difference only exists in the text, so the text is what gets asserted.
