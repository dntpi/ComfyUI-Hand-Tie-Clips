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
