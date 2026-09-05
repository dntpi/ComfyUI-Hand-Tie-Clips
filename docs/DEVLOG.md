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

## 37. Two ways to hand a model a photograph and not say why (2026-09-03)

A user: *"pinning outfit to anything but first hop throws the actual image
in."* The obvious suspect was the image ordering -- something prepending the
still ahead of the continuity frame. It was not. `_attach_pin_to_qwen` builds
`[last_frame] + refs`, and `still_shift` moves the text ordinals to match. The
pictures arrive in the right order with the right numbers.

What was wrong was that nothing said what the picture was *for*.

`retention_analysis:` is the block that does that -- `<Picture 2> (denim apron
over a grey tee): the garment and its cut carry over`. It is produced by
`subject_prose`, and `subject_prose` is gated on `i == 0`. That gate is correct
for its other half: `subject_definitions:` introduces `<Subject N>`, and a
subject token first introduced on hop 4 has no antecedent in its own encode.
But retention has no such problem, and it went out with the gate. A still
scheduled onto hop 3 therefore reached the encoder as `ref_image_2` with no
retention line, no `desc`, and no subject entry. If it also had no subject
number, `id_ords` came back empty, `_identity_lock` returned `""`, and the
photograph was never named anywhere in the prompt at all.

The second defect is worse because it is confidently wrong rather than silent.
`_identity_lock` decides who is a person by `subject is not None` and has never
read `retention`. The canonical outfit reference in the README carries
`subject: 1` *and* `retention: partially_copy` -- it is a garment belonging to
person 1, not a photograph of their face. So hop 3 opened with *"`<Picture 2>`
is the only identity. That face, bone structure, and hairstyle match the
photograph exactly"*, asserted about an apron. And because `lock` was then
non-empty, the closer became *"Faces and hair follow the identity photographs.
Clothing follows whatever is already on them in the live frame"* -- which
contradicts the wardrobe plate riding that very hop. At cfg 1.0 there is no
negative branch, so both instructions are additive and the encoder gets each at
full weight.

Three changes. `retention_prose` splits out of `subject_prose` and takes an
explicit ordinal map, because on a continuation hop the live frame is `<Picture
1>` and every still shifts up -- deriving the map a second time is exactly the
drift the `hop_ords` comment warns about. The identity filter requires
`fully_preserved`, which `refs.py` already defaults subject-bearing refs to, so
ordinary face plates are untouched and only garment and setting plates stop
being called people. And the closer points clothing at the plate when one is
actually present.

Hop 1 escaped all of this because `subject_prose` runs there, which is
precisely why the report said "anything but the first hop".

The same shape turned up a third time while writing this. The author's
reference *clip* has always gone in as `<Video 1>` with nothing naming it --
`_live_cite` covers the pinned tail, which is a different video. Same failure
mode, same fix: a description field, and a note printed when a clip is wired
without one. It is gated on the field being filled and asserted as such, so no
existing workflow has its prompt quietly rewritten by upgrading.

The lesson is not about pinning. It is that an uncited reference is not
neutral. A Ref2VA model handed a photograph and no reason for it will find a
reason, and the reason it finds is "render this".

## 38. Fifteen tuples, of which the formula gets nine (2026-09-03)

The output canvas was a hand-authored table: five resolution labels by three
aspects. Adding eight more aspects would have made it fifty-five, so it became
a function -- mirroring core's `adapt_canvas`, nearest 32 per axis, under the
`768*1344` cap.

The interesting part was checking it against what it replaced. I predicted the
formula would reproduce thirteen of the fifteen old tuples and wrote that into
the plan. It reproduces nine. Two cells I had done in my head were simply wrong
-- `sqrt(200000/1.7778)` is 335.4, which rounds to ten 32-blocks and not eleven
-- and I had also forgotten that every landscape divergence has a portrait
twin.

Six diverge, all in the 16:9/9:16 column, and the pattern says why: that column
was hand-tuned for ratio fidelity rather than derived from area. 1280x736 is
1.739:1, closer to 16:9 than the 1344x736 (1.826:1) the area allows. So the old
table was not sloppy; it was optimising something else.

They are pinned rather than recomputed, because width and height are in
`chain_salt`: resolving them differently would re-render every chain a 1.0.x
user has on disk *and* change the pixels of a graph they already signed off.
`check_canvas.py` asserts both halves -- that the six stay pinned, and that the
other nine still need no pin. A compat shim nobody re-checks becomes a bug the
moment the thing it was shimming moves.

I also tried a small grid search to beat per-axis rounding on ratio fidelity.
Weighting ratio twice as heavily as area took the worst ratio error from 3.3%
to 2.1% and pushed the worst area error from 2.7% to 3.8%, and produced
identical sizes for 16:9 at every rung. Not worth diverging from core's
arithmetic for. Matching `adapt_canvas` exactly is worth more than a point of
ratio error on one cell, because the two agreeing is a property somebody can
rely on.

Separately: `media._mp_cap_size` rounded reference stills to a multiple of 16
on the stated grounds that "that is H3's canvas grid". It is not.
`CANVAS_MULTIPLE` is 32; 16 is the VAE's spatial factor, a different number
that happens to divide it. Core re-snaps every reference to 32 on the way in,
so nothing wrong ever reached the model -- the only casualty was that the size
printed in the log was not the size the encoder saw, which is the one job that
rounding has. `check_waveform.py` asserted the wrong number and had been
passing on it since it was written.

## 39. Four thousand lines with nothing in front of them (2026-09-03)

There is no Node in this environment and no build step in this pack, so `js/`
had no check of any kind between an edit and a browser. That matters more than
it sounds: the browser reports a bad import as a node with no editor, which
looks exactly like the pack failing to load, and the console line is one
`console.warn` among ComfyUI's own.

`tools/check_ui.py` is not a parser and does not try to be. It checks four
things that have gone wrong or could go wrong silently: delimiters balance per
file; every named import resolves to a real export in the file it names; every
`var(--h3-*)` the stylesheet reads is declared in it; and no rule targets a
class the JS never sets.

It found three things on its first run, before it had checked any new code.
`--h3-line` was read in two rules and declared nowhere, so the trim bar's and
the draft list's borders had been resolving to nothing. `--h3-fg` fell back to
a literal `#fff` that no longer matched the palette. And two classes had rules
but no setter -- `.h3e-drag-over`, left behind when the drop target was renamed
`.h3e-drop`, and `.h3e-card.h3e-dragging { opacity: .45 }`, which is feedback
the stylesheet was clearly written for and the drag reorder was never wired to.
The first is deleted; the second is now wired.

Its own first run also caught a bug in itself, which is the honest way to
calibrate one of these: the import scan ran against the string-stripped source,
and the module specifier of an import *is* a string literal, so it found zero
imports and reported success. The "something was actually imported" assertion
exists because of that, and it is the more valuable line -- a checker that
silently checks nothing is the failure mode every checker has.

## 40. The range that only had one end (2026-09-03)

`render_through` has stopped a chain after N hops since 0.4. There has never
been a way to start at one, so re-rendering shot 5 of an eight-shot chain meant
re-rendering shots 1 through 4 first, or accepting a chain that began at 5 with
nothing before it.

The obvious implementation is to seed `prev_imgs`, `prev_audio`, `prev_sampled`
and `prev_key` from the hop store before the loop starts. `HopStore.get`
already returns all four, so it is maybe fifteen lines.

It is the wrong fifteen lines. The cache-hit branch inside the loop already
carries exactly those four forward, and a second implementation is a second
thing to get subtly wrong -- the sampler latent especially, which is what
decides whether the next hop joins by Motion-Context or falls back to the
weaker AddGuide pin. Getting that wrong produces a chain that renders fine and
joins badly, which is the hardest class of bug to attribute.

So `render_from` truncates nothing. The leading hops run through the loop like
any other hop and are simply *required* to hit the cache; a miss names the hop
and says why it might be gone. One code path carries continuity, and it is the
one that was already carrying it.

Two things fell out of writing the guards. Every misconfiguration is refused
before the master tensor is allocated, which on an 8x15s plan is ~31 GB -- the
first draft checked `hop_store is None`, which meant the check sat after the
allocation, so a typo cost 31 GB before being told it was a typo. And the
inverted-range check has to run against the *original* `render_through`,
because `n` has already been truncated to it by then: testing `start_at > n`
first reported "past the end of the plan" for `from=5, through=2` and then
rendered the whole thing.

The cache change shipped alongside is the one that will be felt more.
`chain_salt` digested every wired reference chain-wide, so swapping the file
behind `@outfit` moved hop 1's key even when `@outfit` rides only hop 5 -- and
the key is chained, so that re-rendered everything. References are keyed per
hop now, from `base_images`, which is the right set on both paths: this hop's
scheduled stills when there is a ref plan, every wired reference when there is
not, and the pin frame it excludes is already covered by `prev_key`.

And the nine-reference ceiling turns out to be per *encode*, not per plan,
while the check counted the whole rail. Twelve references at three per shot
across four shots was refused against a limit no shot came near.

## 41. Asking the OS who we are (2026-09-03)

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

## 42. The label was never an area (2026-09-03)

A user, flatly: *"768p Tier: Uses a 768-pixel short-edge baseline (such as
1344 x 768) ... this is the stated native resolution for minimax h3 (which
people also coin as 0.98mp)."*

Section 38 in this file describes replacing a hand-authored canvas table with a
formula, measuring it against the old tuples, and running a small grid search to
justify per-axis rounding. All of that was careful work on the wrong algorithm.

Core's `adapt_canvas` has a one-line docstring: *"768-short-edge canvas with
768*1344 area cap, per-axis round to 32."* It pins the short edge and derives
the long edge from the ratio. It does not work from an area budget. What v1.1
shipped derived BOTH axes from a megapixel figure, and the two agree at no
aspect ratio at all -- 16:9 came out 1312x768's worth of pixels shaped 1312x736
against core's 1344x768, and 4:3 came out 1152x864 against 1024x768. The comment
above the function claimed it mirrored core. It mirrored the rounding.

The error was upstream of the code, in reading "0.98 MP" as 980,000 pixels.
1344 x 768 = 1,032,192, and 1,032,192 / 1,048,576 = 0.984. The number everyone
quotes is a MEBIpixel count of a short-edge tier -- 1024*1024, not 10^6. So the
label was describing the thing I had decided it could not describe, and a
decision was put to the user ("pure maths, accept 1312x736") on a premise that
was simply false. They accepted it. It was still wrong.

The replacement is core's function with the short edge as an argument, so the
draft tiers run the identical path rather than a second implementation that
agrees at one rung. And `check_canvas.py` now imports `adapt_canvas` and
compares against it directly, instead of against numbers copied out of it. The
old assertion -- "area is within 5% of the label" -- passed the entire time the
sizes were wrong, because it was checking the formula against its own premise.
That is the whole lesson: an assertion derived from the implementation cannot
falsify the implementation.

All fifteen 1.0.x cells are pinned now rather than nine, since no short-edge
formula reproduces an area table by accident. The five v1.1 pre-release labels
alias forward onto the tier they were trying to name, rather than to the sizes
their arithmetic gave them -- they named the right thing badly, and a graph
saved mid-development should land where its author meant.

## 43. Three times in one day (2026-09-03)

1.0.2 shipped to clear a scanner finding. It doubled it.

The change removed a hostname lookup from `shares_this_gpu` and replaced it with
a bind test -- better code by every measure except the one being optimised. The
scan came back with two findings where 1.0.1 had one. The bind matched three
patterns at once, and the second finding was in `docs/DEVLOG.md`, on the line of
section 41 where the aiohttp client class was quoted while explaining that the
aiohttp client class was the finding.

That is the same mistake three times in one day. The first draft of the new
docstring used the removed function's name twice while explaining its removal;
that one was caught by rereading, and the count went two to zero. The devlog one
was not caught, because prose does not feel like code. The third was the
replacement docstring quoting the two patterns that had just flagged it.

Reading is not a control. `tools/check_publish.py` reads the set `comfy node
publish` actually uploads -- git-tracked, minus `.comfyignore` -- and fails on
any token this pack has been Flagged for, wherever it appears. Its first run
found eight occurrences, six of which predated the day's work, including one in
`.comfyignore` itself: the file explaining which trigger words were being
excluded had quoted them, and it ships.

Two consequences beyond the fix. `CLAUDE.md` and `docs/DEVLOG.md` stop shipping
-- they are developer material, they are on GitHub, and they are where a
discussion of scan patterns naturally lives. And `shares_this_gpu` is loopback
only. The LAN-address case genuinely regresses: a writer on this machine
addressed as 192.168.x.x no longer gets its model evicted automatically. The
message says so and names the fix, which is to type localhost. A courtesy
feature is not worth every user who reads the scan seeing a socket call.

The aiohttp finding itself remains, and no release will clear it. The rule has
no taint analysis, never looks at a destination, and every alternative client is
in the same family. It needs a reviewer.

## 44. Decoding a plate to throw most of it away (2026-09-03)

A user, relaying another: *"for reference clip, someone was mentioning the
ability to do a resize on the input -- to save on memory and speed. LD
promptmaster has this feature as well."*

Section 40's closing note said reference-video resizing was deferred because
"core already scales ref video down, so the cost is disk and load time, not
quality." The first half is true. The second half looks at the wrong place.

Core does resize -- but at the far end, in `MiniMaxH3ReferenceToVideo.execute`,
after `load_video` has decoded every frame at source resolution, stacked them,
and converted the stack to float32. For a 10 s 4K plate that is about 36 GB of
system RAM spent to deliver about 4.5 GB of pixels that core then discards.
Nothing about the output changes; the memory is simply spent before anyone
looks at whether it was needed. That is not a missing feature, it is a defect,
and it was sitting behind a sentence I had written to explain why there was
nothing to do here.

So there are two changes, and only one of them is the requested feature.

`load_video` now computes the decode size before the loop and hands it to
`frame.reformat`, so libswscale scales inside the decoder and a full-resolution
RGB array is never built. The default, "match H3", asks core's own
`adapt_canvas` what it would have resized to and never scales up -- a clip
already below the tier is left exactly alone, which is what core does too. On
the default path the model therefore sees the same pixels through a different
resampling kernel, and nothing else moves.

The requested part is the rungs below that: 640p through 384p, trading
reference fidelity for memory. Measured on a 226-frame 960x544 plate, which is
already under the tier and so gets nothing from the defect fix: 1.42 GB and
6.1 s at source, 0.70 GB and 2.0 s at 384p. Three times faster is more than the
pixel count explains -- the RGB conversion and the float32 cast cost more than
the decode does.

Third, free: `load_video` has accepted `max_frames` since it was written and
nobody passed it. Core truncates a reference clip to the hop's frame count, so
a 60 s plate on a 10 s hop decoded 1440 frames to use 243. It is passed now,
bounded by the LONGEST hop -- bounding by the shortest would cut frames core
might still have wanted.

The lesson is the one from section 42 wearing different clothes. Both times a
conclusion was reached by reasoning about what the code must be doing rather
than reading where the cost actually lands, and both times the reasoning was
written down confidently enough that it stopped anyone looking again. A user
asking "possible?" was what reopened it.

## 45. A mean cannot see a colour (2026-09-05)

An outside tester ran ten controlled 9-hop chains -- 65 s each, one variable per
run, same model, LoRA, references, locked audio and seed -- and measured them
with her own instruments rather than ours. The result that matters here is not
which lever won. It is her section 5, which explains in our own code why none of
them could have.

The relay chain loses about a fifth of the skin's colourfulness over nine hops
(chroma 33.6 in the reference still, 30 at hop 1, 23.8 by hop 9) and darkens it
(L* 52.3 -> 45.0), while the HUE ANGLE DOES NOT MOVE. It is not a cast. It is a
loss of chroma and a loss of level, arriving together.

`tone_compensate=anchor` was built to hold exactly this and did not, for two
reasons. The first is a plumbing problem -- under the latent join the corrected
frames never become the next hop's pin -- and it is the pin anchor's to fix.
The second is this one: `anchor_pull` matched a per-channel RGB MEAN. Three channel means
cannot distinguish "less colourful" from "differently coloured", because a hop
that has greyed out can have all three of its means sitting exactly on the
reference. The correction had nothing to correct with.

So the anchor now measures and corrects in CIE Lab, and pulls four numbers
rather than three: mean L*, mean a*, mean b*, and the SPREAD of L*. a* and b*
are the colour on its own, which is the thing that was being lost. The spread is
the contrast, which is what hardens when a face bakes -- her section 3.2
measured mid-scale contrast rising x1.07 to x1.37 across the ten runs while fine
detail fell to 0.76-0.96 of the still, the combination that reads as waxy. A
per-hop cap and the existing ramp are unchanged in spirit; the spread has its
own cap (`ANCHOR_MAX_GAIN`) because it is a ratio, and a ratio applied to a hop
whose contrast genuinely collapsed -- a fade, a cut to a flat wall -- would
stretch it hard.

The cost is real and worth naming. Lab is a non-linear round trip over every
pixel of every frame, and the first working version cost 32 s on a 362-frame
1344x768 hop. Three changes took it to 15 s: the statistics are read from 24
evenly spaced frames rather than all 362 (they agree to 0.005 Lab units, and it
is four moments over twenty million pixels either way), frames past the ramp
skip the blend they would multiply by 1.0, and every linear stage -- both
colour matrices and the f()->Lab assembly -- is a matmul instead of three
expressions and a `torch.stack`. That last one alone was 9x on the stage it
touches: 0.032 s to 0.003 s on a full frame, measured, because BLAS does in one
call what the stack form does in nine multiplies, six adds and a copy.

A 3D LUT was tried first and rejected on its own numbers: 33^3 lattice with
trilinear interpolation came out SLOWER than the direct conversion (0.301 s
against 0.256 s on the same chunk) and carried 2.1/255 of error. The intuition
that a lookup must beat a transcendental is a CPU-era one; on a vectorised
backend the eight gathers cost more than the two pow calls they replace.

The seam guarantee is unchanged and is still checked: frame 0 comes back
bit-identical, because the ramp blends against the untouched source in RGB
rather than in Lab. Through a non-linear round trip that distinction is the
whole guarantee -- a Lab-side blend of zero strength still returns the pixel
that survived a forward and inverse transform, not the pixel that went in.

Two things this does not fix, both deliberate. It still corrects the DELIVERED
frames, so under the Motion-Context latent join it still never reaches the next
hop's pin. And it still measures the whole frame at once, so a
chain where the skin greys while the background gains edges -- which is what
hers did -- gets one compromise correction for two opposite drifts.
## 46. Anchoring on the first casualty (2026-09-05)

`tone_compensate=anchor` holds the chain on hop 1's look, and the reasoning was that hop 1
is "the only tone in the chain nobody drifted into". The ten-run study says that is wrong
in a way that matters: hop 1 is already not the photograph. Before any relay has happened,
measured on the same instrument, the reference still reads chroma 33.6 and hop 1 reads 30;
b* 26.6 against 22; fine detail 0.72 to 0.99 of the still. The chain was being held on a
target that had already fallen short, and holding it perfectly still meant converging on
the first casualty rather than on the reference.

`tone_anchor_ref` chooses: `hop1` is the original behaviour and stays the default, `still`
takes the statistics from `start_image` instead. Two things follow from the second that the
first cannot do. Hop 1 itself gets pulled, which is the only way that 33.6-against-30 gap
is ever closed. And the target stops moving with the chain -- a photograph does not drift.

Ramp handling had to change with it. The ramp exists to keep a JOIN exact: frame 0 of a hop
must equal the previous hop's last frame, so the correction fades in over 48 frames. Hop 1
has no previous frame, and neither does a restart hop, which opens on the photograph by
construction. Both now take the correction whole from frame 0 (`ramp=0`), because ramping
them spends two seconds fading into the look at the exact moment a viewer decides what the
shot looks like.

While wiring the key for it, a cache gap that predates this: the tone settings were in no
key at all, on the documented grounds that the cache holds RAW hops and tone is applied
after, so switching modes need not invalidate them. That is true of the hop being corrected
and FALSE of the next one -- the corrected frames are what `prev_imgs` becomes, and
`prev_imgs` is the pin. Hop 2 rendered under one tone mode was being served to a run using
another, with a byte-identical key. Now keyed from hop 2 only, which keeps the half of the
original claim that was always true: hop 1 has no pin, so it still survives every tone
switch.

## 47. Choosing the pin, and refusing to pretend (2026-09-04)

The node has always known which mechanism pinned each hop.
`_motion_context_cls()` resolves the upstream class and rejects forks whose
`apply()` differs, `run()` logs the path each hop took, and the preview
shows it per hop. The only thing missing was the ability to choose, which
made a whole class of question unanswerable: does the AddGuide pixel path
ratchet less than Motion-Context? Its VAE round trip is itself a projection
and might scrub the off-manifold latent structure that accumulates -- at the
cost of the join quality Motion-Context was picked for. Today the only way
to ask was to uninstall a pack.

`pin_mech` is `auto` / `motion_context` / `addguide`, defaulting to `auto`,
which is exactly what shipped.

The one way to get this wrong is well documented, by the code itself.
`_pin_mech_for` predicts the mechanism so it can go in the per-hop cache key
before the pin runs, and its docstring says "Every condition here mirrors
_pin_continue". If the predicted mechanism and the executed one disagree,
the key stops describing what is on disk -- which is the failure that made
`cache_hops=on` measurably worse than off before the latent sidecar landed.
So the override is applied in both, and both carry a comment saying not to
change one without the other.

Forcing does not add a fifth condition to the predictor. It removes the
four, which is the point. A forced setting that silently degrades to the
other mechanism answers a question nobody asked, and worse, poisons the A/B
it was turned on to run. So `motion_context` refuses rather than falls back.
The two chain-wide preconditions -- pack installed, and an overlap with a
matching `context_length` -- are checked before any sampling, next to the
duration validation, so a bad combination fails on the queue rather than
three hops in and names the accepted frame counts. The per-hop precondition,
a previous hop that came from a cache entry written before latents were
stored, cannot be known up front and raises at the hop it affects, with the
two ways out. And if Motion-Context itself raises at call time -- the one
thing the predictor was always unable to foresee -- a forced setting
re-raises instead of quietly downgrading.

The cache work was already done, years of it in one comment. The mechanism
is in the per-hop key and deliberately NOT in `chain_salt`, so switching
re-renders hops 2+ and hop 1 hits cache. That is the ideal A/B and it fell
out of the existing design with nothing added. Adding `pin_mech` to
`chain_salt` would have thrown away a byte-identical hop 1 on every
comparison, which is the mistake the comment two lines above it records
about `overlap`.

Two things this cost that are worth writing down. The widget is APPENDED,
never inserted: `widgets_values` is a bare ordered array indexed against the
schema, so a widget added anywhere but the end silently reassigns every
later value in every saved workflow. Section 27 said it in one line --
adding options to a combo is safe, adding widgets is not. And
`check_workflows.py` caught the other half immediately, 48 widgets against
49, because both shipped workflows carry their own `widgets_values` array
and neither knew about the new dial. That check existing is the only reason
this was a thirty-second fix rather than a bug report about a Starter that
loads with its dials one position out.

Last, a message. Forcing `addguide` sets the Motion-Context class to None
internally, which walked straight into the existing "Motion-Context not
available; install it for a latent join" line -- false, and precisely the
line a user would read while wondering why forcing the setting appeared to
do nothing. Forced now says it was forced.

## 48. The restart was still being told it was a continuation (2026-09-05)

`anchor: "restart"` shipped in 1.2.0-beta1 with a clear contract, written into the branch
that implements it: *"A restart hop is a chain start. It gets the photograph as its frame-0
anchor exactly as hop 1 does, and NOTHING from the previous hop reaches it -- no sampler
latent, no decoded tail."*

The first half was true. The second was not, and the code said so two hundred lines apart
without anyone noticing, because `hop_restart` was computed **beside the sampler**, and
almost everything that makes a hop a start rather than a continuation is decided long
before that:

- `_attach_pin_to_qwen` ran on `i > 0`, so a restart hop was handed the previous hop's last
  frame as Qwen `<Picture 1>` and its last 22 frames as a live video reference.
- `_assemble_next` ran on `i > 0`, so the compiled prompt opened with *"The clip opens
  already in progress from the pinned frames"* -- on a hop whose frame 0 is a studio
  portrait and which has no pinned frames at all.
- Under `hop_script=next` the identity stills were dropped as "unscheduled stills stay off
  this continue", on the one hop that is not a continue and most needs them.
- The voice reference was dropped for the same reason.
- And `dry_run` never printed the ANCHOR RESTART line, because that print sat past the
  point a dry run returns. The one tool for reading a plan before paying for it did not
  mention the feature.

An outside reviewer found the consequence in the tester's own package, and her instruments
had already recorded it. Background edge density at the restart hop is **0.0032** and
**0.0031** in the two restart runs, against 4.7 to 7.2 at every other hop of those same
runs -- a featureless backdrop. Verified here from `background_metrics.json` rather than
taken on report. For several seconds the model renders the reference photograph instead of
the room.

The asymmetry is the part that convinced me. Both runs restart at hops 4 AND 7, and only
hop 4 collapses; hop 7 reads 6.77 and 4.68, an ordinary room. So this is not "restart is
broken".

**Correction, from the reviewer's second pass, before anyone builds on the paragraph above.**
My first reading was that the hop was handed a portrait at frame 0 and rendered it. That is
wrong, and the frames say so. Hop 4 OPENS on the couch -- 21.0 to 22.5 s is the room, at
162 to 167 KB a frame -- ghosts at 23.0, sits on the identity still from 23.5 to 26.0 at 61
to 72 KB, and is **back on the couch at 26.5 s, inside the same hop**. The next hop does not
rescue it; it leaves and returns on its own. So the restart's frame-0 anchor worked. What
failed is the middle of a pin-less hop, where the eight identity photographs are an
attractor with no video pin holding the room.

Two consequences. First, `background_metrics.json` samples MID-hop frames, so
`edge_density = 0.003` is a reading of the wander, not of the whole hop -- still a true
reading, and still one nobody interpreted. Second, hop index and seed are confounded in
this package: hop 4 is seed ...458 and hop 7 is ...461 in both runs, so anything that
happens only on hop 4 here also happens only on that seed. The asymmetry cannot be
attributed yet.

It is also not restart's invention. Run 02 -- a plain relay, continuous join, Motion-Context
pin -- does the same thing at t = 9 to 10 s and comes back by t = 11. Same attractor,
shorter, because it had a pin.

What the fix below is, then: the code contradicted its own contract in five places, all of
them real, none of them proven to be THIS. The continuation wrapper is the one candidate
the reviewer could not rule out, because a contact sheet shows the authored beat and not
the block the encoder receives. Whether it was the cause is a render away.

The fix is small and entirely in the ordering: `hop_restart` is computed at the top of the
hop loop, `hop_is_start = i == 0 or hop_restart` replaces the six `i == 0` / `i > 0` tests
that were standing in for it, and the restart hop's cache key drops the pin levers and the
tone settings because its pixels genuinely cannot depend on them.

Three lessons. First: a contract written in a
comment is not a contract. The words "NOTHING from the previous hop reaches it" sat six
lines from code that passed the previous hop's tail, and reading either one alone made
sense. Second: **the instruments caught it and the prose did not.** `edge_density = 0.00`
is not a subtle reading. It was in the table, in the shipped package, and the write-up
described that hop as "the restart resets every axis at the cut" -- because the number that
mattered was the one nobody expected to look at. Someone reviewing measurements they did
not take is worth more than one more run.

Third, and it is the one I paid for: **having the right defect does not license the first
story that explains it.** The five contract violations were real and I found them by
reading; the causal claim I hung on them lasted until somebody opened the frames and
checked when the hop actually breaks. The fix stands. The explanation did not.

## 49. Four features that already shipped (2026-09-04)

A review pass went looking for missing capability and kept finding the
capability already there. Four things users have asked for are implemented,
and they fail in four different ways -- which matters, because each one
needs a different fix and only one of them is a documentation gap.

`README:133` said "Voice stays as a reference every hop." The code says
otherwise: `hop_voice` is true only at `i == 0` or under
`hop_script=verbatim`, so on any chain with a shot plan -- which forces
`next` -- the voice is a hop 1 reference and nothing else. That is
deliberate, and the comment directly above the line records why: in
chain_00038 a second `<Audio 1>` with no line to attach to put a 1.35 s male
take into the final second while the written line still followed the woman's
face. The console has been printing `voice ref stays off this continue` the
whole time. So the node was right, the log was right, and the sentence was
wrong -- which is the worst of the four, because a user who reads the doc
and believes it files a bug against working code. One had already done so.

`pin_renorm` was worse in a quieter way. The row documented the sigma story
and recommended it for 3+ hops. But sigma was measured to be the wrong
statistic -- total spread *falls* across a chain whose picture is baking, so
matching it corrects the wrong way -- and `band`, which matches the
statistic the ratchet actually moves, took a 12.74% drift to -0.04%. Section
27 measured all of this on 2026-09-01 and the tooltip has carried it since.
The README went on pointing at the inert mode and never named the working
one. Documentation that disagrees with the tooltip beside it is not a gap,
it is a contradiction, and the tooltip won.

`mp` was simply absent. The Reference register section documented five
sibling fields and omitted the sixth, whose only prior appearance was one
clause in the 1.0.0 release note -- despite `refs.py` carrying it, `:1916`
consuming it as `cap_mp=`, and the rail drawing it on every row. It is also
the dial people ask for when they ask for a per-reference resolution slider.
Now documented, with the trap that makes it look broken: on the default
`ref_image_size=match` every reference is scaled to the output area first
and `mp` only caps further, so at 768p the 1.5 and 2.0 settings do nothing
at all. The real control is `max` plus `mp`, and that pairing was written
down nowhere.

Per-hop duration was the different one. It shipped, it is cache-correct, and
it was documented -- in a fields table, as one clause, where nobody found
it. It kept being requested as a new feature. The fix for that is not
another sentence; it is an example, so there is one now, and the editorial
rule that goes with it is in PROMPTING.md: overlap is chain-wide, so short
hops cut and long hops flow.

The pattern is worth keeping. Three of these four cost nothing to build
because they were already built, and the support cost of the first two was
being paid in bug reports against correct code. When a capability is
requested, check whether it exists and is merely unfindable before designing
it.

## 50. What the fingerprint could not see (2026-09-04)

Three holes in hop-cache invalidation, none of them live while `cache_hops`
ships off, all of them the same shape: something that changes the rendered
frames does not change the key that identifies them. That is the failure
mode the fingerprint exists to prevent, and its own docstring names the
stakes -- "silently wrong output, which is worse than no cache at all."

The first is the base checkpoint, and it is the one a second person makes
likely. `_model_fingerprint` hashed the LoRA patch keys, the per-key
strength scalars and the scalar half of `transformer_options`. Every one of
those describes something patched ONTO the model. Nothing described the
model. So an int8 build and a bf16 build of the same architecture, under the
same LoRA stack at the same strengths and the same attention settings,
produced byte-identical hop keys. ComfyUI re-executes the node because the
loader's output changed, `run()` recomputes the same keys, and the cache
serves frames rendered under the other checkpoint. It now hashes the inner
model's class, its dtype through ModelPatcher's own `model_dtype()`, and its
parameter count. Two different int8 builds of the same architecture still
match; separating those needs digests of fixed weight keys and a state-dict
walk per run, which is a worse trade.

The second is the more interesting one, because it is the SLA bug wearing a
different coat. That fix -- which lives only in the `_closure_scalars`
docstring, never having been written up here -- taught `_scalars` to dig
settings out of a callable's closure, because H3-SLA-Attention installs its
config that way and changing sparsity 0.90 -> 0.50 had been leaving the key
unmoved. But a node can also configure itself with a plain object --
`set_model_patch_replace(cache, "dit", "block_loop", 0)` with a configured
instance -- and closure-digging cannot reach that. Worse, such an instance
is usually callable, so it went down the closure path and came back as the
constant `"fn()"`: an instance inherits neither `__qualname__` nor
`__name__` from its class, and it has no `__closure__` at all. Every setting
on it hashed to the same four characters as every other. Presence was
detected, because installing the node adds a key to `patches_replace`;
configuration was not.

That is not hypothetical. It is the shape of an approximate step cache that
is actively recommended alongside this model, whose whole behaviour is three
numbers -- a reuse threshold, a window, a step cap -- carried on the
instance.

`_object_scalars` reads an object's public scalar attributes before falling
back to its type name. Review then found two more bindings that hide an
object behind a callable and lose it just as completely. A BOUND METHOD:
`vars()` on one proxies to the underlying function's `__dict__`, which is
empty, not to the instance, so a node registering `self.forward` instead of
`self` is the same bug one attribute away. And a `functools.partial`: no
name, no cells, no attributes of its own, everything it carries sitting in
`func`, `args` and `keywords` -- the SLA bug one binding away. Both
collapsed to the same four characters as a bare instance did.
`_callable_scalars` now unwraps all four shapes.

The tradeoff is worth writing down plainly, because the first draft of this
entry got it wrong and review caught that too. A public scalar the node
mutates during a run IS hashed. Only mutable *containers* are skipped, and a
step counter is normally a plain int on the instance rather than a dict, so
it is hashed like any other setting. A node that counts on itself between
queues therefore moves the fingerprint between runs and the cache stops
hitting for as long as it is installed. That is wasteful rather than wrong,
and it is the direction this pack should err in -- but it is a real cost,
and it used to be an invisible one. The run now prints the fingerprint
beside the hop-cache line, because a cache that has quietly stopped hitting
is otherwise indistinguishable from one that is merely cold.

The third is not a hole but a cost. `pin_to_qwen` sat in `chain_salt`, which
mixes into every hop -- while `_attach_pin_to_qwen` is called only under `if
i > 0`. So the setting could not reach hop 1's pixels, and keying it
chain-wide threw away a byte-identical cached hop 1 on every pin_to_qwen
A/B. That is exactly the mistake the comment two lines above it was written
to record about `overlap`, repeated one field over. It has moved to the
per-hop key, from hop 2, next to `overlap` and `pin_cond` which already live
there for the same reason. Read the comment block in `chain_salt` as a
checklist: any lever whose effect starts at hop 2 does not belong in it.

None of this can be caught by reading a diff, which is why it now has a
checker. `tools/check_cache_keys.py` builds two graphs differing in exactly
one thing and requires the digests to disagree -- and requires two identical
graphs to agree, because a fingerprint that always moves is a cache that
never hits. It carries a regression guard for the closure path as well, so
the SLA case cannot come back quietly. Before the fix it fails on the dtype
case and the object case and passes the rest, which is the only real
evidence that a cache key fix did anything at all.

## 51. The sidecar was a pickle, and did not need to be (2026-09-04)

`store.py` wrote the hop's sampler latent with `torch.save` and read it back
with `weights_only=False`. The comment beside the read was honest about why
-- this is our own dict, written by our own `put()`, into ComfyUI's temp
directory, not a downloaded checkpoint -- and it was true. It was also the
next thing a registry scanner reaches for after the subprocess work in 0.4.1
through 0.4.3, and being right about a finding is not the same as not having
one.

Nothing about the payload needed a pickle. The awkward member is `samples`,
which is a `comfy.nested_tensor.NestedTensor`, and `latents.parts()` has
decomposed that into plain tensors since the pin levers needed exactly that
decomposition. A flat dict of tensors plus a small JSON header is a
safetensors file wearing a different name, and safetensors is already a hard
dependency of ComfyUI. The sidecar is now `.latent.safetensors`: components
as `samples.N`, tensor members of the dict as `extra.<key>`, scalar members
in the header.

Three things this had to get right, and they are asserted in
`tools/check_latent_sidecar.py` rather than argued for here.

Bit-identical, not close. A cached hop's latent becomes the next hop's
Motion-Context pin, so a chain resumed from cache that differs at all from
an uninterrupted one would mean the cache changes the output -- the single
thing it must never do. The test round-trips through a real file on disk and
requires `torch.equal`, not a tolerance.

Every member survives, including ones this code has never seen. A latent
dict that came back missing a key would be quietly not the latent that was
stored, so an unrepresentable member refuses the whole write rather than
dropping it. That refusal costs nothing, because the caller already had a
correct answer for it: cache the frames, skip the latent, fall back to the
pixel pin. That path was built when latents were first stored and it is
exercised here rather than added.

Old `.pt` entries are not read. The extension changed, so they are simply
invisible, and `_get_latent`'s docstring already promised what happens then
-- entries written by an older build have no sidecar and keep working. Not
reading them is the point: keeping a compatibility path would keep
`weights_only=False` in the file, which is the thing being removed.

The interesting part was the first version being wrong in a way only the
repository's own tooling caught. It recorded the container's module and
qualname in the header and rebuilt it with `importlib.import_module`, which
is more general and reads to a registry scanner as
`python_bytecode_manipulation`. `check_publish.py` failed on it immediately.
Trading the pickle finding for a dynamic-import finding would have been a
lateral move dressed up as a fix, and the generality bought nothing:
`parts()` recognises exactly two shapes, a bare tensor and a nested one. The
header now names which, and the rebuild is a static import. If core ever
moves `NestedTensor`, that import raises, the existing handler logs it and
the hop falls back to the pixel pin -- loud, and already handled.

Worth stating plainly, because the analysis this came from got it backwards
at first: the incremental-writer machinery from silveroxides'
unifiedefficientloader is not what this wanted. Its cleverness is a reserved
header that lets you stream thousands of tensors whose shapes you do not
know yet. This is a handful of tensors of known shape written at once. Plain
`save_file` is the whole job. The place that machinery would earn its keep
is the master frame buffer, and that is the opposite direction -- one
tensor, known shape, contiguous ranges, which wants a memmap instead.

## 52. Thirty-one gigabytes doing nothing (2026-09-04)

The observation came from outside, from silveroxides, without seeing the
code: anything held in memory but completely inactive can be written away
while inference runs. The master frame buffer is exactly that shape, and the
comment above it has named the number since it was written -- at 8 x 15 s
and 1280x736 it is 2742 full float frames, about 31 GB. It is allocated once
at full chain length, slice-written as each hop lands, and then not touched
again until the final preview frame and the return. Through every sampling
pass of every hop it is resident, untouched, and competing with the DiT, the
VAE decode buffers, `imgs` and `prev_imgs`.

Above 2 GiB it is now a `np.memmap` in ComfyUI's temp directory, wrapped by
`torch.from_numpy`. Every slice-write is unchanged, the trim still works,
and SaveVideo walking frames in order is ideal page locality on the way back
out -- the encode may read better off the mapping than out of a cold RAM
buffer.

**What moves is the peak, not the total.** ComfyUI's IMAGE type is a dense
tensor, so the whole master still has to exist to be returned. The peak goes
from `master + inference` to `max(master, inference)`.

That sounded like a hedge until it was measured. A 3-hop 8 s chain at
640x1152 was instrumented end to end: peak RSS 49.4 GB, of which the master
is 4.4 GB. The useful number is the other one -- **non-master residency came
out at 41.8 GB and does not grow with chain length.** Model weights, the
pinned-memory pool and the decode buffers are a fixed cost. The master is
the only term that scales, linearly, with frames x area:

```
  3 x  8 s @ 640x1152    532 frames   master  4.4 GB   peak ~46.2 GB
  8 x 15 s @ 1280x736   2742 frames   master 28.9 GB   peak ~70.6 GB  <-- over 64 GB
 10 x 15 s @ 1280x736   3422 frames   master 36.0 GB   peak ~77.8 GB  <-- over 64 GB
  spilled, any of the above                            peak ~41.8 GB
```

So the honest claim is scale-dependent, and stating it as a flat percentage
was wrong. On a small chain the master is a tenth of the peak and spilling
it is a nicety. At the settings the original report came from -- long chains
at working resolution -- it is 40% of the peak and it is the term that takes
a 64 GB machine past its limit. There the change is not an improvement, it
is the difference between a run that completes and one that cannot.

The honest weakness is that the OS decides when pages leave RAM. Under no
memory pressure they simply stay and nothing has been bought; under heavy
pressure a large dirty flush can stall at an awkward moment. That is not
knowable from here and it is why the run prints which path it took -- a
silent memory path is the one thing nobody could diagnose from a bug report.

Why a memmap rather than the incremental safetensors writer from section
48's neighbourhood: that machinery exists to stream thousands of tensors
whose shapes you do not know in advance, which is why it reserves a header.
The master is one tensor, of perfectly known shape, written in contiguous
ranges, in order. The right primitive for that is a mapping, and it is
nearly a drop-in. The writer earns its place on the latent sidecar, which is
the opposite shape -- and that is where it went.

Cleanup is the part that needed thought, and the first answer was wrong in a
way that only a real render exposed. The returned IMAGE is backed by its
mapping, so the file cannot be removed while `run()` still has to hand it
back. The original plan was therefore to sweep stale files on the NEXT run,
relying on Windows refusing to unlink a live mapping as the guard.

**It never reclaimed anything.** ComfyUI keeps the previous run's output in
its execution cache, so the mapping is still open when the next run starts;
`os.remove` raised, and the handler passed on `OSError` without a word. Two
renders left two 9 GB files on disk. The docstring asserted "there is never
more than one" and the checker only tested a stale file that nothing held,
so both the code and its test agreed with each other and neither agreed with
reality. Ten renders would have been 92 GB of silent disk.

Delete-on-close removes the problem rather than policing it. Windows has it
natively as `O_TEMPORARY`; POSIX gets the same by unlinking the name while
the descriptor stays open. The bytes then live exactly as long as something
is using them, and a hard kill cleans up too, because the kernel closes the
handles. The sweep stays as a safety net for files written by the previous
build, and it now REPORTS what it could not remove instead of swallowing it
-- silence is what let this go unnoticed.

Two things the analysis this came from had wrong, both caught by writing it.
`numpy` was not already imported in `h3_ref_chain.py`; it is now. And the
master is not "never read again until the return" -- `master_imgs[-1]` feeds
the final preview frame. That does not disturb the fp16 argument, because
`prev_imgs` is cloned from `imgs` and nothing in the conditioning path reads
the master back, but the statement was wrong and an fp16 change would have
been justified with it.

fp16 is deliberately not in this change. The reasoning holds -- the master
is delivery-only, `tone_compensate` runs before the write so correction
arithmetic stays in fp32, and fp16's ten mantissa bits space the top octave
about eight times finer than the 8-bit encode downstream. It is a separate
decision from where the buffer lives and it should be made on its own. It is
emphatically not true of the hop cache, where a resumed chain must not
diverge from an uninterrupted one.

What is verified here is narrow and deliberate: that the spilled buffer
behaves exactly like the RAM one. `tools/check_master_spill.py` drives the
real access pattern -- the hop-0 write, the later windowed writes, the
short-chain trim, the `[-1]` preview read -- and requires the delivered
frames to be bit-identical, because the alternative is a chain whose pixels
depend on how much RAM the machine had. The win itself is not testable
offline and stays unproven until a long chain runs with peak RSS
instrumented.
## 53. The texture ladder, run (2026-09-04)

Four levers, measured, on one chain. Three of the results contradict
something that was written down as guidance, which is the point of running
it rather than reasoning about it.

The rig: 3 hops x 8 s at 640x1152, 8 steps `lcm`/`beta_57` under a
checkpoint with the turbo baked in, seed 12345 fixed, one location, one
subject, a 560x690 face plate on every hop. `cache_hops=on`, so hop 1 was
rendered once and every later run reused it -- which is also the first live
confirmation that moving `pin_to_qwen` out of `chain_salt` works: run A took
772 s, runs B, C and E about 330 s each because only hops 2 and 3
re-rendered.

### The measurement was wrong twice before it was right

First attempt used the probe's default head box. It reported the face's mid
band climbing x1.455 across the chain -- a textbook ratchet. Then the box
was drawn on an actual frame and it turned out to be sitting half on a
yellow sign behind her. Tightened onto skin, the same data reported x0.792.
**The same chain, measured two ways, gave opposite answers.**

Both were wrong, for the reason section 05 already documented and then
walked into anyway: the subject moves. `camera: hold` and `framing: keep` do
not hold the frame, and a fixed pixel box covers mouth-and-nose in hop 1 and
smooth forehead in hop 3. Any within-run drift number off this rig is
measuring anatomy, not texture.

The fix came from the user watching the videos rather than the numbers: the
pose at the END of hop 3 returns close to the pose at the START of hop 1.
That pair is matched. Measuring hop 1's first 24 frames against hop 3's last
24 gives a common baseline -- and because hop 1 came off cache in every run,
it is literally the same baseline for all four.

### What the levers did

Fraction of the reference's face mid-band energy still present at the end of
hop 3, and the head/background ratio which cancels frame-wide effects:

```
  A  baseline                 0.838   ratio 0.609
  B  pin_to_qwen=off          0.781   ratio 0.592      WORSE than doing nothing
  C  pin_renorm=band          0.852   ratio 0.606      within noise of A
  E  pin_mech=addguide        0.954   ratio 0.741      clearly best
  D  ref_image_size=max       untestable
```

**`pin_to_qwen=off` made it worse.** That was the ladder's first rung, the
cheapest lever, the one recommended on the reasoning that hops 2+ receive
the model's own degraded output presented as a reference picture. Removing
it cost texture rather than saving it.

**`pin_renorm=band` did exactly what it claims and it did not matter.** The
latent high-band fraction it targets drifted -1.90% in A and -0.85% in C, so
the lever more than halved the drift in its own statistic -- and moved the
pixels by 1.4%, which is noise. The prediction that "band restores the
energy ratio without restoring the structure" is now measured rather than
argued.

**`pin_mech=addguide` won, and it was never in the ladder.** Section 06
justified the switch as the thing that makes the experiment *possible* -- a
way to ask whether the AddGuide VAE round trip scrubs the accumulating
latent structure. It does: the pin's sigma climbs 1.0441 -> 1.0612 under
Motion-Context and sits flat at 1.0429 -> 1.0409 under AddGuide, and the
face keeps 95% of its texture instead of 84%. The lever that was scaffolding
turned out to be the only one that worked. This is one chain and it costs
whatever join quality Motion-Context was chosen for, which was not measured
here.

**`ref_image_size=max` could not be tested at all.** H3 only ever scales a
reference DOWN. The face plate is 560x690, which is below both the `match`
target (0.74 MP) and the `max` target (2048 short edge), so both modes hand
the model the identical image and the lever is a no-op. Lever 3 needs a
genuine high-resolution photograph; a crop from a video frame has nothing to
give. Note what this means for the mp dial as well -- its whole range is
inert on source material this size.

### A second finding nobody was looking for

The background box gains mid-band energy in every run (x1.29 to x1.41) while
the face loses it. Detail moves from the subject to the surroundings. That
is why a whole-frame metric reads flat on a chain whose face is visibly
coming apart -- it averages a rising background against a falling face,
which is the exact confound `texture_probe` was built to replace, caught in
the act.

### The seams, which the user spotted by eye

Watching the four masters, the user asked whether there were flashes at the
joins. There were, and they are the first seam steps this pack has measured
across correction modes rather than just asserting.

Mean absolute luma step at the two joins, on byte-identical hops -- every
mode after the first is the SAME cached frames re-joined, so nothing but the
correction differs:

```
  off           2.36/255      seams +1.48 and +3.25, both brightening
  frame_shift   1.35/255      -1.80 and -0.90
  gain_bias     0.76/255      -1.50 and +0.02
  lut           0.77/255      -1.49 and +0.04
  anchor        0.68/255      -1.13 and -0.24      best
```

Three things fall out of that table.

`anchor` is the mode to use, and `frame_shift` -- the one the tooltip
recommended for regenerated content -- is the weakest of the four
corrections, roughly half as effective.

**`lut` does not overfit.** It tracks `gain_bias` to within 0.02/255 at both
seams. That warning has been in the module docstring and two tooltips since
the modes were written, and it appears never to have been measured.
Corrected in all three places, and stated as unproven rather than reversed:
one chain is not enough to say lut is *good*, only that the specific claim
has no evidence behind it.

**Every mode overshoots, and none of them fixes the first join.** An
uncorrected seam brightens; a corrected one darkens. All four land between
-1.13 and -1.80 at seam 1 where doing nothing gives +1.48. Seam 2 is
correctable to near zero by three of them. A systematic bias that survives
four different estimators is not noise -- it is something about the first
join the estimate cannot see, and the obvious suspect is that hop 2 is the
first hop that has a pin at all. Not chased further here.

### The objection to AddGuide, tested

The reason Motion-Context is the default is join quality, so `addguide`
winning on texture is only interesting if the join survives. Measured on the
delivered masters, with no new renders: mean absolute frame-to-frame
difference AT the join, against the median of the twelve frame pairs either
side of it. A ratio of 1.0 means the cut looks like ordinary motion.

```
                        seam 1   seam 2
  A  motion_context      1.25x    0.95x
  E  addguide            1.16x    0.90x
  B  pin_to_qwen=off     1.26x    1.03x
  C  pin_renorm=band     1.21x    0.94x
```

No run shows a discontinuity, and **AddGuide is the lowest of the four at
both seams**. Frames either side of both joins were also inspected directly
and neither mode shows a visible break. The expected cost did not appear.

**The limitation is the scene, and it is a real one.** This is a talking
head under `camera: hold` with very little motion. Motion-Context exists for
motion continuity, so a chain with actual camera movement or fast action is
precisely where AddGuide would be expected to fail -- and there is barely
any motion here for a join to break. The honest reading is not "AddGuide
costs nothing" but "on low-motion content the cost does not show, and the
texture gain does." Anyone reaching for it on a moving shot should measure
again.

Worth noting the asymmetry that appears here too: seam 1 bumps ~1.2x in
every run while seam 2 sits at or below baseline. That is the same
first-join asymmetry the brightness measurement found, from an unrelated
instrument.

### Read section 51 before acting on any of this

Everything above measures TEXTURE, and texture turned out not to be where
the degradation lives. A later pass on the same rig found the one quantity
that decays monotonically across a chain, and it is not a band energy: it is
how much the LIGHTING responds to the subject. Section 51 has it. Three
consequences for the numbers above.

**The face and background band figures are partly pose, not texture.** They
are sampled from each hop's last frames, and the lighting in this model
tracks head height -- correlation +0.70 in hop 1. The within-hop brightness
swing (0.36) is LARGER than the across-chain drift (0.29), so which pose the
final frames happen to catch moves the reading more than the chain does. The
matched-pose method fixes the anatomy problem, not this one.

**A background result reported here was withdrawn.** An earlier draft had
the background gaining 59% mid-band energy while the face lost it, and read
that as detail migrating off the subject. Normalising each box by its own
brightness removes the effect entirely: the background is not gaining
texture, it is getting 57% BRIGHTER. That was caught only because the user
said the lighting kept changing.

**`pin_to_qwen=off` was already the shipped state for the reporting user.**
An independent audit of her runs found that with nine identity photographs
scheduled on every hop she hits `MAX_REF_IMAGES`, so the pack skips the
last-frame Qwen pin anyway -- 36 log lines saying so. The lever measured
WORSE here is the one her chains had on the whole time, which makes the
result more useful, not less.

One thing above got stronger rather than weaker. `pin_mech=addguide` winning
was doubted because her isolated long-chain test of the same idea
(`force_pixel_pin`) still degraded. The audit found that test had no matched
control -- it was read against runs differing in hop count, audio file,
reference schedule, encoder-side pin and code version. The A/B here changed
one widget against a shared cached hop 1, so it is the better-controlled of
the two.

### What this is not

One chain, one subject, three hops, 640p, one checkpoint. Every number above
is a single measurement. What it does establish is narrower and more useful
than a ranking: the levers can now be measured at all, the method that makes
them measurable is a matched-pose pair rather than a fixed box, and three
pieces of standing guidance were wrong in ways that only showed when someone
rendered.
## 54. The chain forgets how to light her (2026-09-05)

Four levers were measured against the texture ratchet and three did nothing.
That is section 50. This is why: the thing that degrades is not texture, and
none of those levers could have touched it.

The user watched the four renders and said the lighting kept changing --
darker when she raised her head, brighter when she looked down. That is a
real behaviour and it is measurable: in hop 1, the correlation between
background brightness and head height is **+0.70**. The model is doing a
light-source thing, and it is doing it well.

By hop 6 that correlation is **-0.49**. It does not fade, it INVERTS.

### The measurement, with the script removed

The obvious objection is that she simply moves less in later hops, and a
weaker signal gives a weaker correlation. So the chain was re-run with the
SAME BEAT on all six hops -- "She talks to the camera, glancing down at her
notes and back up again", chosen because it contains the vertical head
movement the lighting tracks. Any decay across hops 2-6 is then the chain's,
not the writing's.

```
  hop   corr(bg luma, head height)   bg swing   how much she MOVED
   1              +0.581               0.278           23.97
   2              +0.631               0.229           22.75
   3              +0.334               0.172           14.73
   4              +0.338               0.133           13.47
   5              -0.417               0.132           11.10
   6              -0.494               0.099           14.52
```

Three things decay monotonically on identical beats. The lighting stops
moving (swing -64%). The subject stops moving (-39%). And the coupling
between them inverts.

The inversion is the part no confound explains. Less movement drives a
correlation toward ZERO; it cannot carry it through zero to -0.49. That is a
different behaviour, not a weaker one. The movement decay does mean the
swing and correlation MAGNITUDES are partly downstream of the subject moving
less -- which is itself a finding, since the beat asked for the same
movement every time.

### It is not exposure drift

The chain also brightens: background luma 0.63 -> 0.77.
`tone_compensate=anchor` was run over byte-identical cached hops, so nothing
but the correction differed. It pulled the level down (0.767 -> 0.705) and
left the responsiveness untouched -- the per-hop swings match the
uncorrected run to three decimal places, and the correlation still ends
negative. Exposure drift is a symptom sitting on top of this, not the cause.

### Why every presentation lever failed

Each hop is conditioned on the previous hop's LAST frames, and the end of a
clip is its most settled moment. So each hop starts from a slightly calmer,
flatter state than the one before, and it compounds. `pin_to_qwen`,
`pin_renorm` and `pin_mech` all change how that state is PRESENTED. None of
them changes the fact that it is inherited. That is why three of four did
nothing, and why the pin's own statistics show no ratchet at all: sigma
wanders 1.000 / 1.010 / 1.015 / 1.021 / 1.010 across six hops with no
per-join step, and the high-band fraction ends ABOVE its anchor.

`pin_noise` is the exception, and the exception proves the reading: it works
by mixing energy back INTO the pin, which is the only shipped lever that
opposes the inheritance rather than re-presenting it. Measured against a
matched control it held movement (18.10 vs 14.52 at hop 6), roughly doubled
the surviving lighting swing (0.194 vs 0.099), and prevented the inversion
-- +0.228 where the control reached -0.494. At the widget's 0.10 maximum it
gets WORSE in the way that matters: more swing, less correlation, i.e.
flicker that is not coupled to the subject. The tooltip's "gains reverse
above 0.10" is now measured rather than asserted. 0.06 is the number.

### anchor=restart, and what it did

If the inheritance is the mechanism, severing it should reset the decay. It
does. A 6-hop chain with `"anchor": "restart"` on hop 4, against the same
chain without it:

```
                   control            restart at 4
  hop 4     corr .338  swing .133   corr .431  swing .239  (+80% swing, +33% move)
  hop 5     corr -.417              corr -.137
  hop 6     corr -.494              corr -.258
```

The latent agreed independently: the pin's sigma against the hop-2 anchor
went x1.0126 in the control and x0.9938 after the restart -- below the
anchor, i.e. the accumulation reset rather than merely paused.

**And one restart in six hops is not enough.** Hop 5, the first hop relaying
from the restart, is already at -0.137 where hop 2 relaying from hop 1 was
+0.427. By hop 6 it is negative again. The restart buys one clean hop and
about half the end-state damage. The useful interval looks like two or three
hops, not five, and that is untested.

### What this is worth

One subject, one location, 640x1152, six hops, single runs. The hop-to-hop
wobble is real -- hop 3's correlation reads .334 in one chain and .559 in
another on identical beats -- so no single row above should be defended.
What is solid is the direction: three quantities decaying monotonically on
identical beats, an inversion no confound explains, a mechanism that
predicts which levers can and cannot work, and a lever built on that
prediction that moved all three in the right direction at the hop it was
applied.

The honest description of the failure is not "texture degrades". It is that
the chain converges on a static, evenly-lit, motionless picture -- which is
what "plastic" looks like, and which explains why chasing skin detail found
nothing.
## 55. Two channels at a third of capacity (2026-09-05)

A user asked why the MEDIA tab offers one reference clip and one voice. The
answer was that nobody had checked what the model takes. Core declares it
outright, in the node's own input template: 9 reference images, 3 reference
videos, 3 standalone reference audios, 3 per-video soundtracks. The pack
matched the 9 and hardcoded one of each of the rest, two lines apart.

So `ref_videos` and `ref_audios` ran at a third of capacity, and
`ref_video_audios` ran at zero -- a reference clip reached the model SILENT
even when the file had sound. Twelve widgets and a pairing loop later, all
three channels are full.

### The ordinals are the trap

`<Video N>` and `<Audio N>` are positional: core numbers reference blocks by
the order it iterates them, and the prompt cites those numbers. So the
numbering has to be DENSE. Fill slots 1 and 3 and the model must see `<Video
1>` and `<Video 2>`, not 1 and 3 -- and the consequence, which is now in the
tooltip and the README, is that clearing a slot RENUMBERS the ones after it.
A beat naming "the second clip" then cites something else, and nothing would
report it. That is the same class of defect `refs.py` fixed for pictures and
paid for once already.

### What the cache needed

`chain_salt` digested slot 1 of each. Left alone, a chain rendered with a
second voice would be served to a run that dropped it -- the
silently-wrong-frames class the key exists to prevent. It digests all three
of each now, plus which clips carried sound. `IS_CHANGED` grew the new
filenames too, so overwriting `clip2.mp4` in place re-runs rather than
serving the old render.

### The guard that did not exist

`tools/check_media_slots.py` asserts the dense numbering including the gap
case, and then something broader: that every `*_file` widget the node
declares is claimed by a slot in `media_strip.js`. A Python file widget with
no slot falls through to a native dial where the user types a filename by
hand -- the 0.4.0 failure recorded at `run_panel.js:53`, which has recurred
since and recurred again during this very session on the shot editor.
Nothing checked for it. Now something does.

Not rendered. The plumbing is checked and the ordinals are tested; whether
three voices actually behave -- and what three sets of reference blocks cost
per step -- needs a GPU.

## 56. One take, every hop (2026-09-05)

`master_audio_file`: one continuous recording every hop lip-syncs to, so a
chain can carry a scripted voiceover instead of generating a new voice per
hop and relaying it. Empty string is off and is the default. The reference
diff that first shipped this on a tester's tree is not in the pack; this
was rebuilt from the prose, from `PromptMasterLD/song_lock.py` as the
mask/encode reference, and from seven decisions taken in place of asking.

The window function is 0-based, pure, and checked against a hand-computed
table for hops 0-8 at 8 s and hops 0-2 at 15 s (`tools/check_audio_lock.py`).
A 1-based version fails every row of that table. The recording's digest is
in `chain_salt` only when the file is set -- adding a None field while off
would have moved every existing cache key. Delivered audio is a passthrough
of the take trimmed to the rendered duration; the log line names the window.

Untested, because this machine has no GPU window:

- Whether `SamplerCustomAdvanced` honours a NestedTensor `noise_mask` on
  the audio stream. The polarity is asserted (1 on video, 0 on audio) but
  an ignored mask would still generate a voice that we then throw away at
  passthrough -- lips would follow the generate, sound would be the take.
  GPU test 1 in `GROK_V2_GPU_TESTS.md` is the proof.
- Whether this ComfyUI's audio VAE `encode` matches song_lock's
  `[B, T, C]` call. A mismatch raises with both signatures.
- Beat/`<d>` alignment with the take. We do not auto-cut a transcript into
  the beat; unmatched words can pull the mouth off. Visible, recoverable.

## 57. Last-frame guide, opt-in (2026-09-05)

`last_frame_guide`: combo `off` / `still`, default `off`. `still` AddGuide-pins
`start_image` at the last PIXEL frame of every hop (`frame_idx=-1`, which
Core counts from the end). Conservative half only: that pin does **not**
become the next hop's frame 0. Keyframe chaining is a v3 conversation.

Default off is byte-identical. The setting reaches hop 1, so it lives in
the per-hop key for every hop, and only when not off -- a new `"off"` field
would have moved every existing cache key. Queue-fails if `still` is set
with no `start_image_file`.

The index is the thing that was easy to get wrong. AddGuide's `frame_idx`
is pixel frames. H3's `FRAME_PER_TOKEN` is `(1, 4, 4, 4, 4)`. An 8 s hop
is 192 pixel frames at latent T=57; `latent_T-1` is pixel 56, about 2.3 s
in, not the end. `_last_pixel_guide_idx` returns `-1` so that trap has one
place to live. `tools/check_last_frame_guide.py` asserts it.

Untested, because this machine has no GPU window:

- Whether the DiT actually treats a last-pixel AddGuide as a bound. The
  hop-4 wander this is medicine for was diagnosed on pin-less restarts;
  a last-frame pin can still lose if identity stills in the rail outvote
  it mid-hop. GPU test 5 in `GROK_V2_GPU_TESTS.md`.
- Whether `-1` after Core's `resolved_frame_index = frame_count + frame_idx`
  is the last *decoded* frame, not one token-group early. Core's own
  tooltip says negative values count from the end; we did not decode a
  hop to confirm the last displayed frame matches.
- Chroma pulse at the still/render gap, named in the tooltip. Off by
  default, so a bad result costs nothing shipped.

## 58. Restart hops write their full length (2026-09-05)

A restart is a chain start. It overlaps with nothing. The master-write
path still dropped the leading `overlap_n` frames of every hop after
index 0, and the preallocation `sum(lengths) - overlap * (n - 1)` sized
the buffer to match. Two restarts on a 9 x 8 s / 0.9 s chain threw away
44 frames (1.83 s) -- either silence at the end or an overrun raise,
depending on which side you looked at.

The write now keys on `hop_is_start`, not `i == 0`. The preallocation
counts only the hops that actually trim. The audio-lock window moves
with the master head: a restart hop's take slice starts at the current
write position, not at `hop_index * stride`. Leaving the uniform stride
in place would have locked lips 0.9 s earlier than the picture, once
per restart, compounding.

`overlap` leaves the per-hop key on a restart the same way `pin_cond`
does -- the lever cannot reach a start hop's pixels. `tools/check_restart_trim.py`
has the 1552 / 1596 table and the 510/24 vs 532/24 window split.

Untested: the 40 ms audio xfade at a restart cut (same helper as a
continuation join). A hard concat might click; we did not hear it.
GPU test 4 in `GROK_V2_GPU_TESTS.md` is the length check -- the log
line names the frame count.

## 59. Shot-level refs, a choice not a drop (2026-09-05)

Identity stills on a pin-less hop can win the middle of it -- restart hop
4 of the tester chain opened on the couch and visited the portrait
anyway. Silently dropping those stills on restart hops was the old
accident, and it is not obviously right: a restart that needs the face
still needs the face.

`shot.refs` is the choice. Omitted keeps the register default
(unscheduled stills ride chain starts, stay off continuations under
`hop_script=next`). `[]` is explicit none. A filled list is those tags
only, in that order -- putting the room first is how you stop a portrait
becoming Picture 1. Unknown tags fail on the queue. The editor
round-trips both a filled list and the empty list; destroying the field
on save is how it died the first time it existed.

Default is current behaviour on purpose. Last-frame guide (DEVLOG
"Last-frame guide, opt-in") is the other medicine, and it is also
opt-in. Neither ships as a silent drop.

Untested: whether `refs: []` on hop 4 actually keeps the couch. The
diagnosis is from pictures, not from this code path. GPU test 3.

## 60. 2.0.0 (2026-09-05)

Version bump. `pyproject.toml` 2.0.0, editor `VERSION` v2.0.0, README and
`CHANGELOG.md` written against the code. Shot-field table, MEDIA strip,
hop-cache notes and the "no shot-level refs" line were stale; `join: cut`
in a README example is not a join value. `docs/HANDOVER_*.md` left as
history; `BETA_NOTES.md` marked superseded. No behaviour change.

## 61. Locked hop-1 audio was 2-D, hop-2 trim was 3-D (2026-09-05)

GPU test 1: hop 1 locked `[0.00s-8.00s]` of the 26 s take, then died on hop 2
at `_xfade_audio` with `Tensors must have same number of dimensions: got 2
and 3`. The lock path squeezed the take to `[C, T]` for `wav` and
unsqueezed a copy into the AUDIO dict; hop 1 wrote `wav` onto the master
and hop 2 xfade'd that against `audio["waveform"]` which is `[B, C, T]`.

`_xfade_audio` now batches both sides. The lock path keeps `wav` as
`[B, C, T]` so it matches a decode. `tools/check_audio_lock.py` cats a
2-D take against a 3-D hop. Needs a ComfyUI restart (Python).

## 62. GPU tests 1 and 2 (2026-09-05)

2 hops × 8 s × 8 steps, 26 s ElevenLabs take, after the xfade fix and a
ComfyUI restart.

**Test 1 (lock on).** Hop 1 `audio locked [0.00s-8.00s]`, hop 2
`[7.08s-15.08s]`, join wrote, `final audio: passthrough of
master_audio_file [0.00s-15.08s]`, drift +0 ms. Window and polarity are
not Fail A/B.

**Test 2 (lock off).** Empty MEDIA slot: no `master_audio_file: loaded`,
no `audio locked`, no passthrough line. Delivered audio is generated
voice (heard). Drift -40 ms (the generated-voice xfade). Empty is off.

Still untested on GPU: last-frame guide, restart hop 4, restart length
arithmetic on a real master, `refs: []` on a pin-less hop. The latent
sidecar still logs `not representable without pickling` on this NestedTensor
shape -- hops render; a later cache hit will fall back to the pixel pin.


## 63. The fixture did not match the code (2026-09-05)

`master_audio_file` shipped with the hop cache silently disabled underneath it.

Every locked hop of the first GPU test logged `latent not cached (ValueError('latent is
not representable without pickling'))`. The hops rendered, so it read as noise. It is not
noise. Without a stored sampler latent a later cache hit leaves `prev_sampled` empty, the
hop after it predicts the AddGuide pixel fallback instead of Motion-Context, and that
prediction is part of its key -- so the key stops matching what is on disk. `store.py`
already carries a paragraph about this exact chain of consequences, written when the
sidecar was built, ending "making `cache_hops=on` actively worse than off". A feature added
after it walked straight back into it.

The cause is one line of type-checking. `_latent_to_flat` handles `samples` through
`latents.parts()` because it can be a `NestedTensor`, and then handles every OTHER member
of the latent dict as either a plain tensor or a scalar. But this is a joint AV latent, so
anything shaped like it carries one tensor per stream -- and the audio lock's whole
mechanism is `out["noise_mask"] = NestedTensor((ones, zeros))`. A `NestedTensor` is not a
`torch.Tensor`; the module docstring in `latents.py` says so in its second paragraph. It
fell to the `else`, which refuses, which is correct-by-design behaviour applied to a case
nobody meant to refuse.

`tools/check_latent_sidecar.py` covered "other members of the latent dict" and passed,
because its fixture builds the mask as `torch.rand(1, 1, 8, 8)` -- a plain tensor. The code
under test never makes one of those. **A fixture that does not match what the code produces
is not a test**, and this one was green through the whole of v2's development while the
behaviour it guards was broken in every run that used the feature it was written beside.

Fixed both ends. Nested members are now decomposed the way `samples` is, stored as
`nested.<key>.<i>`, and rebuilt through the same static import and the same class-name
check; a member whose container `parts()` does not recognise is still refused rather than
dropped, because restoring a latent minus its noise_mask would denoise the audio it was
supposed to freeze. The checker now builds the mask the shape the code actually sets, and
fails without the fix.

The lesson is not "write more checks". There were twenty-two, all green, and one of them
was pointed directly at this. It is that a check is only as good as the resemblance between
its fixture and production, and that resemblance decays silently every time a feature lands
next to an older test. The tell was in the log the whole time, once per hop, in a line that
said the word `ValueError` and was still easy to read as routine.

## 64. fp16 for the master, and what it actually costs (2026-09-05)

Section 52 deferred this: "fp16 is deliberately not in this change... it is a separate
decision and should be made on its own." The decision was never made, and the master
shipped fp32 through the whole of v2. Taking it now.

The master is DELIVERY-ONLY. `prev_imgs` is cloned from `imgs`, never read back out of the
master, so nothing in the conditioning path depends on its precision. That halves both the
footprint and the disk I/O of the largest allocation in the pack: an 8 x 15 s 1280x736
chain goes from ~29 GB to **14.4 GB**.

fp16 and not bf16. The buffer is clamped to 0..1, so exponent range buys nothing and
mantissa bits are the whole question. fp16's ten space the top octave at about 1/2048;
bf16's seven space it at 1/256 -- exactly 8-bit output precision with nothing in reserve,
and the highlights would band.

**It is not free, and the checker now says so in the right units.** The first assertion
written here was "fp16 delivers the same 8-bit pixels as fp32", and it FAILED. The encode
truncates rather than rounds -- core's `Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))`
-- so a value fp16 nudges down across an integer boundary loses one 255th. Measured over
two million samples: **2.06% of pixels move, every one of them by exactly 1, none by more**,
and rounding instead of truncating barely changes it (2.06%). That is well under the h264
encode's own error and is not visible, but "eight times finer than the output" was an
argument about spacing that quietly implied bit-identity, and bit-identity is not what
happens. The check asserts the measured bound instead of the comfortable claim.

Two paths were verified not to care: `_frame_to_jpeg_b64` and `sheet.small` both call
`.detach().float()` before touching pixels, so the preview and the contact sheet are
unaffected. The one thing offline work cannot settle is a downstream node that assumes an
IMAGE is fp32 -- core's own save path handles it, third-party nodes are not all core. That
is a two-hop render away and belongs in the next GPU window.

Also recorded here because it was missing everywhere except this log: the master-spill work
began with silveroxides putting the problem in exactly these terms and proposing
`unifiedefficientloader`'s streaming writer. The diagnosis was right. The writer was the
wrong shape -- it exists to stream many tensors of unknown offset, and this is one tensor of
known size written in order -- so `np.memmap` won and no code travelled. No licence
obligation follows from that, which is precisely why the credit is worth writing down: this
pack already names rkfg in `tone.py` and PromptMasterLD in `audio_lock.py` for techniques
rather than code, and the same rule applies to a reading that turned out to be correct.

## 65. What the GPU window settled (2026-09-05)

Four things were verified on hardware, and one of them corrected a reading of my own.

**anchor=restart stays in the shot.** The tester's 9-hop package had hop 4 of both restart
runs leaving the couch for the reference portrait for about two and a half seconds. On the
v2 build, 4 hops at 0.70 MP with a fresh seed on the restart hop, it does not happen: hop 4
opens at the desk, holds the mic mid-hop, and ends at the desk. `tools/shot_probe.py` reads
edges 3.2-4.4% and 1979-2483 colours across the whole hop with no collapse anywhere.

That is one run, and hop index and seed were confounded in her package, so this is "did not
reproduce with the contract violations fixed and a different seed" rather than "fixed". The
five contract violations in section 48 remain unproven as the cause. Recorded as such.

**Restart hops write their full length.** `4 hops x 192f overlap 22 -> 724 frames`, and the
log prints `old formula would have been 702f` beside it. The arithmetic in `master_frame_count`
holds on hardware.

**fp16 survives delivery.** `master buffer: 3.0 GB spilled` at 0.70 MP and `4.2 GB` at
native, both exactly half what fp32 would have asked for, and SaveVideo accepted the fp16
IMAGE without complaint. That was the one claim in section 64 that offline work could not
settle.

**The last-frame guide fixes the restart cut, and I misread what it costs.**

Guided, hop 3 arrives at the still's framing instead of snapping to it, so the restart reads
as a match cut rather than the obvious jump the user watched on the unguided run. Two frames
either side of the join make it plain: unguided, hop 3 ends tight and smiling and hop 4 opens
wide and neutral -- a framing jump, a pose jump and a colour shift at once.

Then the cost, measured: the four hop ENDINGS converge to 3.8/255 of each other, against
39.1/255 unguided, while mid-hop frames stay as varied as ever (65.8 against 61.1). I put
those four endings in a grid, saw one picture four times, and called for making the setting
per-shot before release.

**That was the wrong call and the user said so.** A hop's last frame is passed through in a
twenty-fourth of a second and the next hop continues straight out of it. Four stills in a
grid is precisely the presentation that makes convergence obvious and motion invisible. Two
people watched the clip and saw nothing; I had looked at 4 frames of 724 and chosen the
arrangement that flattered my worry. The number is right, the inference from it was not.

What the number does support is narrower and is now a test rather than a claim. That run had
CAMERA, FRAMING and PACE all unset, so nothing competed with the photograph. The guide plants
it at `frame_idx=-1` on every hop, so a shot authored `framing: close` ought to be overridden
at its own ending -- and if it is, that is a real constraint on a feature we are shipping.
Running now with `close` on two hops against a start image that is a wide.

The lesson is the one from section 48 wearing different clothes, six hours later. There the
defect was real and the story I hung on it was not. Here the measurement is real and the
conclusion I drew from it was not. Both times the error was reaching past what the evidence
covered, and both times somebody looking at the actual output caught it.

## 66. The seam report was measuring the middle of hops (2026-09-05)

The user's complaint was ergonomic: the seam report's `hops` and `overlap` are typed in by
hand and have to be kept in step with the chain node, which is a chore and goes stale. The
bug underneath it is not ergonomic.

`seam_positions(total, hops, overlap)` does not know where the joins are. It solves for
them, assuming every hop is the same length and every hop past the first is trimmed by the
overlap. Section 58 broke the second assumption: a restart hop is a chain start, overlaps
nothing, and writes its full length. So on the 4 x 192 chain rendered tonight with a
restart on hop 4 -- 724 frames, overlap 22 -- it solves for a hop length of **197.5** and
puts the seams at **198, 373, 548**. The joins are at **192, 362, 532**.

It does not merely misplace them. Planting a real step at each true join and measuring the
same clip both ways:

    unwired   seam 1 @ f198  +0.00/255  invisible
              seam 2 @ f373  +0.00/255  invisible
              seam 3 @ f548  +0.00/255  invisible

    wired     seam 1 @ f192 +15.30/255  VISIBLE
              seam 2 @ f362 -12.75/255  VISIBLE
              seam 3 @ f532 +10.20/255  VISIBLE

Six, eleven and sixteen frames off is enough to land in the flat middle of a hop, where
there is nothing to measure, so the report gives a **clean bill of health to a chain with
three visible seams**. Anyone A/B-ing restart against relay -- which is the experiment this
whole line of work exists to run -- would have read "0 of 3 visible" and believed it.

The fix is to stop deriving something that is known. `run()` records the write position of
every hop past the first as it lays it into the master, and publishes them on `info`:

    4 hops x 192f overlap 22 -> 724 frames (30.2s) 768x1344
    seams: 192, 362, 532

`HTCSeamReport` takes `info` as an OPTIONAL socket -- `forceInput: True`, so it is not a
widget and adds no entry to `widgets_values`, and every workflow saved before today keeps
reading its three numbers out of the right slots. Wired, the widgets are ignored and the
measured positions are exact. Unwired, nothing changes. An unparseable string falls back
rather than raising, because a bad line on an input should not break a report that worked
without one.

Two things worth keeping. The uniform solve also printed `NOTE hop length works out to
197.50 frames, which is not a whole number -- the hops are probably not all the same
duration`, which was **correct and was the tell**, and which nobody read as anything but
noise. And the ergonomic complaint was the symptom that surfaced it: a number a user has to
keep in sync by hand is a number that will eventually disagree with the render, and the
question "why do I have to type this twice" is a reasonable way to find out that the second
copy was never reliable in the first place.
