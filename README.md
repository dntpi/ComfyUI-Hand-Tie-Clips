---
license: mit
tags:
  - comfyui
  - comfyui-nodes
  - custom-nodes
  - video
  - video-generation
  - text-to-video
  - image-to-video
  - minimax-h3
---

# Hand Tie Clips

One node. Write a shot plan, drop in your reference stills, queue.

![Six consecutive frames across a join](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/seam-frames.png)

*Six consecutive frames spanning the join between hop 1 and hop 2. One of these
is the last frame the first generation produced and the next is the first frame
of a second, separate generation. The pack exists so that you cannot tell which.*

> **Renamed 2026-08-29.** This pack was `ComfyUI-H3-Ref-Chain`. The nodes now
> live under the **Hand Tie Clips** category and register as `HandTieClips`,
> `HTCChainPreview`, `HTCToneCompensate` and `HTCContinuityState`. The old ids
> are still registered as deprecated aliases, so **every workflow saved before
> the rename keeps loading** — they are just hidden from node search. Nothing
> needs migrating.

> **0.4.1 — 2026-08-30.** The 0.4.0 feature set was built without a browser or a
> GPU and verified offline only. It has now been run in ComfyUI, and two things
> were broken: the five new dials were never added to the run panel's widget list
> (they worked, but rendered as raw dials on the node body), and a dry run
> returned a 1×1 placeholder image that **libx264 cannot encode** — so every dry
> run wired to `SaveVideo`, which is what the Starter ships, died in
> `avcodec_open2`. Both fixed. The Starter now also ships the seam report wired.
> Measurements are in [`docs/DEVLOG.md`](docs/DEVLOG.md) section 21.

> **0.4.4 — 2026-08-30.** Two local models were given `prompt_pack/SYSTEM_PROMPT.md`
> and the same scene, and both made the same two mistakes — so both were the
> prompt's fault, not theirs. The register example had no place tag in it, so
> each model invented its own convention (`kitchen_plate`) while leaving
> `@kitchen` in the beat, and the run stopped on an unknown reference. One model
> also wrote `"name": "@cook_face"` into `subjects`, which parsed, rendered, and
> quietly put a literal at-sign in front of the text encoder on every
> continuation hop. The prompt now shows a place tag on both sides of the round
> trip and says the two spellings are one string; `refs.py` now rejects an
> `@tag` in `name`, `locked` or `context` instead of encoding it. The RUN panel
> also has its tooltips back — it read them from `widget.options`, where this
> frontend does not keep them. Section 22 of the devlog has the A/B.

> **0.4.5 — 2026-08-30.** The hop cache no longer needs `ffmpeg` on PATH. It
> used to shell out to an ffmpeg binary to write its lossless FFV1, so the
> feature that makes a tone A/B cost 14 seconds instead of 164 raised a
> `RuntimeError` for anyone who did not happen to have ffmpeg installed —
> which ComfyUI itself never requires. It now encodes in process through PyAV,
> which ComfyUI already depends on. **The format is unchanged** (ffv1 /
> `rgb48le` / level 3), verified bit-exact in both directions, so caches
> written by the old path still read and a resumed chain still matches an
> uninterrupted one. This also clears the Comfy registry's security scan,
> which flags every `subprocess` call in a custom node regardless of how it is
> invoked. Section 23 of the devlog.

> **1.0.0 — 2026-09-02.** First full release. The headline is **WRITE**: a plan
> writer on the node itself, pointed at any OpenAI-compatible server, that fills
> the script and the reference rows together and sends your reference pictures
> with the request. Around it, the parts that make a written plan survive
> contact with the model — the schema the server is handed now *requires* both
> documents, so a reply carrying only half of one stopped being a silent
> failure; the reference rail owns each picture's pixel budget, which a plan
> used to reset to full and quietly triple the load against a fixed canvas; and
> the writer is told the hop length, so beats are sized to the clip instead of
> to a guess. Three plan lints were measured against real renders and found to
> be warning about correct work — a restated framing read as a cut, a
> single-room chain read as an abandoned location — and were narrowed. Rule 3
> of the prompt pack grew the case that cost the most renders: a hop that
> speaks *later* has opening seconds with a picture and no sound assigned, and
> the model fills them with dialogue nobody wrote. Ending the previous hop
> quiet buys a quiet pin, not a quiet opening. Sections 31 and 32 of the devlog
> have the measurements.

> **1.1.0 — 2026-09-03.** The panel is a 4:3 box with tabs instead of one long
> scrolling column, and RUN stays pinned at the bottom. The **WRITE** draft is
> readable — it used to clip every beat at 110 characters and throw away the
> duration, seed, directives and per-shot references before rendering, so what
> you were asked to Accept was a row of sentences ending in an ellipsis — and it
> now survives leaving the tab or reloading the page. Shot cards carry `⏵` and
> `lock` buttons driving `render_from` / `render_through`, the new one being the
> other end of a range `render_through` has had since 0.4: everything before the
> start is replayed from the hop cache rather than re-rendered.
>
> The bug worth upgrading for: **a reference pinned to any hop but the first
> tended to be rendered as the shot.** `retention_analysis:` — the text that
> tells the encoder what a picture is *for* — was emitted on hop 1 only, so a
> still scheduled onto hop 3 arrived uncited; and the identity lock never read
> `retention`, so a wardrobe plate carrying a subject was announced as *"the
> only identity … that face, bone structure, and hairstyle match the photograph
> exactly"* about a photograph of an apron, while the closer simultaneously said
> clothing follows the live frame. Both fixed. The reference *clip* had the same
> gap — it went in as `<Video 1>` with nothing naming it — and now has a
> description field.
>
> Output size is computed rather than tabulated: eleven aspect ratios from 21:9
> to 9:21 across five short-edge tiers, every one on H3's 32 px grid and under
> its 768×1344 cap. H3 is a 768-short-edge model, so **the top rung is 768p and
> 16:9 there is 1344×768** — the size MiniMax states as native, and the same
> number core's own `adapt_canvas` produces. The hop cache invalidates once
> because of it. Saved 1.0.x workflows keep the
> exact pixels they were built with. And changing a reference picture now
> re-renders only the hops that picture rides, instead of the whole chain.

> **1.0.1 — 2026-09-02.** Three fixes, all found by the first people to use
> 1.0.0. The **WRITE** panel never saved your model on a fresh install: with
> nothing stored, no entry in the dropdown was ever *selected* and the browser
> simply displayed the first one, so the panel showed a model the backend had
> never been told about. That one empty string produced every symptom — WRITE
> answering *no model is selected*, Just-In-Time loading never firing because
> nothing was ever asked of the server, and Free VRAM working anyway, which
> made it stranger still. Underneath it, a failed write of `htc_llm.json` was
> swallowed and reported as success, so a pack folder the ComfyUI account
> cannot write to — a system-wide install, a container — gave the same
> complaint from a different cause with the evidence in a console nobody
> reads; it now says so. And writing a plan no longer spends attempts on
> filenames: your photographs are named by the camera, a model that has just
> looked at one renames it after the tag you gave it, and the node rejected the
> whole plan over a value the rail was already holding. Worse, a filename
> complaint is not a prose gap, so it suppressed the repair that actually works
> for a full round. Sections 34 and 35 of the devlog.

**Writing for it:** [PROMPTING.md](PROMPTING.md) is the authoring guide — the rules that come from what this model actually does, not from taste. The node's **WRITE** bar hands the whole job to a local model: describe the scene in a sentence and it fills the script and the reference rows for you. [prompt_pack/](prompt_pack/) is the same writer as a copy-paste prompt, for when you would rather work in a chat window.

Each hop is native **MiniMax H3 Reference-to-Video**. Hops after the first are guided by the **previous hop's sampler AV latent** via `ComfyUI-H3-Motion-Context` when that pack is installed (22 picture frames + 24-frame end-aligned audio). Stock `MiniMaxH3AddGuide` is the fallback when Motion-Context is missing or the previous hop was a pixel cache hit. Voice stays as a reference every hop. Identity stills ride hop 1; later hops use the pin for wardrobe and room unless a ref lists those hops in `shots`. A 5 s hop drops the airlock on a continuous join — validate seams at 8 s or 15 s.

This is not the seamless-chain pack. No airlock script, no Motion-Context, no interior patch.

## What v1.0 changed

The prompt is no longer a wall of `---`-delimited text, and the MODEL wire is no longer four extra nodes.

| | |
|---|---|
| **`shot_plan`** | The script, as JSON. One shot per hop — **the shot count is the hop count**, so `chains` can no longer disagree with it. |
| **`directives`** | `join` / `camera` / `framing` / `pace` / `tail` per shot, compiled to vetted prose. Improving a phrase improves every plan you have ever written. |
| **`ref_plan`** | Stable `@tags` for reference stills, grouped into subjects, each naming a picture file. Removing one can no longer silently renumber `<Picture N>`. |
| **`cache_hops`** | Lossless per-hop cache. Re-roll one shot, resume after a crash, and hold roughly one hop in RAM instead of the whole clip. Edit hop 5 of 8 and only 5-8 re-render. [How to use it](PROMPTING.md#re-rolling-one-hop). |
| **`WRITE`** | A plan writer on the node. Point it at any OpenAI-compatible server — LM Studio, llama-server — describe the scene in plain language, and it fills the script and the reference rows, reading the pictures you have already dropped in. Server settings stay on your machine, never in the workflow. |
| **the editor** | Cards on the node, not JSON in a textarea. Hover any directive to read the exact sentence it puts in the prompt, or hit **Templates** for a ready-made pattern. |

Simple mode keeps the old one-prompt workflow; the speed stack (LoRAs, AdaLN fix, low-VRAM, SLA) stays as four ordinary nodes on the MODEL wire, where you can see it.

## Install

### Clone it (recommended)

From your `ComfyUI/custom_nodes/` folder:

```
cd ComfyUI/custom_nodes
git clone https://github.com/dntpi/ComfyUI-Hand-Tie-Clips.git
```

Or from HuggingFace, if that is where you found it -- the two are mirrors of the same repository and either is fine:

```
git clone https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips
```

Cloning creates the folder at the right depth for you, which is the mistake the zip route invites. **Updating later is then one command** from inside the pack folder:

```
git pull
```

followed by a restart of ComfyUI. No re-downloading, and you can see exactly what changed with `git log`.

### Or unzip it

Unzip the folder into `ComfyUI/custom_nodes/`, so that it lands as:

```
ComfyUI/custom_nodes/ComfyUI-Hand-Tie-Clips/
    __init__.py
    h3_ref_chain.py
    js/
    workflows/
```

One folder level too deep (`custom_nodes/ComfyUI-Hand-Tie-Clips/ComfyUI-Hand-Tie-Clips/`) is the usual mistake, and the pack simply will not appear.

### Either way

No dependencies to install. Everything it imports -- `torch`, `numpy`, `PIL`, `av`, `aiohttp` -- already ships with ComfyUI, which is why `dependencies` in `pyproject.toml` is empty.

Then:

1. **Restart ComfyUI.** Python changed.
2. **Hard-refresh the browser** (Ctrl+Shift+R / Cmd+Shift+R). The editor is served from `/extensions/ComfyUI-Hand-Tie-Clips/`, and a stale cache is the single most common reason the node mounts with no UI on a fresh install.

It is installed correctly when all three are true:

- the startup log carries a line beginning `[HandTieClips]`
- the browser console says `[HandTieClips] editor ui v1.5.0 loaded`
- node search shows a **Hand Tie Clips** category with four nodes, each listed once

Workflows saved before the 2026-08-29 rename keep loading: the old ids are still registered as deprecated aliases. Nothing needs migrating.

The two example graphs are in `workflows/` inside the pack folder -- open them with the **Workflow > Open** menu, or copy them into `ComfyUI/user/default/workflows/` to have them in the sidebar. Start with `HandTieClips_Starter.json`; it ships with no references on purpose, so it runs before you have supplied any pictures.

## Needs

- ComfyUI new enough to include **Add Guide for MiniMax H3** (`MiniMaxH3AddGuide`)
- A **ref2va** (or hybrid ref2va) checkpoint — fl2va has no reference rows
- Video VAE + audio VAE + MiniMax text encoder
- **Optional but recommended:** [ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context). Hops after the first are guided by the previous hop's sampler AV latent through it. Without it the node falls back to stock `MiniMaxH3AddGuide`, which still works and still chains -- it is a different join, so a seam you are comparing against someone else's render may not be the same code path. The node detects it at runtime and prints which one it took.

The two shipped workflows wire the **turbo stack** this node is actually run with, because an example without it is not the graph anyone uses:

```
UNETLoader -> LoRA Loader Stack -> H3 AdaLN LoRA Fix -> MiniMax H3 Low VRAM
           -> H3 SLA Attention -> Model Preview Override -> Hand Tie Clips
```

| pack | nodes |
|---|---|
| [ComfyUI-PlagueKind-Nodes](https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes) | LoRA Loader Stack, H3 AdaLN LoRA Fix, H3 SLA Attention |
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | MiniMax H3 Low VRAM Attention (experimental), Model Preview Override |

On disk as well: the **turbo LoRA** named in the loader stack, and `taeh3.safetensors` for the live preview (or set `tiny_vae` to `none`).

Both shipped workflows are saved pointing at the exact files they were rendered with. These are quantised builds and are **not** the only ones that work -- any ref2va or hybrid ref2va set will do. Repoint the loaders at what you have; a loader showing an empty or red filename is naming a file you do not have, not a broken workflow.

| loader | file the example names |
|---|---|
| `UNETLoader` | `minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors` |
| `CLIPLoader` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` |
| `VAELoader` (video) | `minimax_h3_video_vae_int8_convrot.safetensors` |
| `VAELoader` (audio) | `minimax_h3_audio_vae_fp32.safetensors` |
| LoRA Loader Stack | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` |
| `tiny_vae` | `taeh3.safetensors` (or `none`) |

**CLIP reaches the node from the LoRA loader, not from the encoder** — that is what makes the text half of every LoRA land. Do not rewire it back.

None of it is required by the node itself. Missing a pack, its nodes load as red boxes: delete them, wire the loader straight into `model` and the encoder into `clip`, and raise `steps` from 7 to around 20.

## Use

1. Restart ComfyUI and load **`workflows/HandTieClips_Starter.json`** — two hops, no references, runs as soon as the loaders are pointed at your files. It carries a six-card **READ ME** board on its own canvas, to the left of the loaders: the three laws, the directive table, the reference rules and the failure table, where you need them rather than in another file. `workflows/HandTieClips_Showcase.json` is the six-hop version and needs three pictures of your own. Both carry the turbo stack listed above.
2. Point the loaders at your ref2va DiT, encoder, and both VAEs
3. Add a row per identity still in the **REFERENCES** rail, then **drop a picture onto its thumbnail** (or click to browse, or pick one already uploaded). Give it a `@tag` and group photos of the same person under one subject number. There are no `Load Image` nodes to wire — files land in `ComfyUI/input/h3_refs`.
4. In **SHOTS**, write one card per hop. Shot 1 is the whole opening; every later card is only the new beat.
5. Optional: a `voice_file`, a `start_image_file`, a look `reference_video_file` — all picked in the **MEDIA** strip, all files under `input/h3_refs`
6. Queue, and wire the **`info`** output to a Preview Text node — it prints the fully assembled prompt for every hop

![The editor's four tabs, with SCRIPT open on a ten-shot plan](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/editor-tabs.png)

*Four tabs -- SCRIPT, REFS, MEDIA, WRITE -- each with its count, and RUN pinned below them. SCRIPT is one card per hop: the beat, its duration, and its own join, camera, framing, pace and tail, with `lock` and the range button on the card header.*

Keep later-hop beats on **what happens next**. Do not re-describe the face; the photos, the register and the pin already carry it.

Two modes:

- **Simple** — one prompt box and a hop count, the way it always worked. Later hops advance the same action rather than replaying the opening.
- **Shots** — one card per hop with directives. Switching from Simple carries your prompt over and splits it on `---`.

Only one of them is on screen at a time, so there is never a text box quietly doing nothing.

Under the script sits **RUN**, collapsed, holding everything that is not per-shot: output size and length, sampling, the join and pin controls, and the hop cache. Its title line summarises the run — `1344x768 · 10s ×3 · 14 steps res_multistep · cache off` — so you can read the setup without opening it. In Shots mode `chains` and `hop_script` are not offered there, because the shot list already decides both.

**`tone_compensate`** lives in that panel's *join & pin* group. The H3 denoiser biases each hop's tone, so a chain gets steadily brighter; this measures the bias on the overlap each hop regenerated and undoes it, correcting each hop against the previous **corrected** one so the whole chain lands on hop 1's tone. `frame_shift` is the mode to reach for: all three modes remove the drift equally well (within 0.4/255 of each other), but `gain_bias` and `lut` pair pixels between a frame and its *regeneration*, which fits a slope that is not really there and flattens contrast a little more with every hop. `frame_shift` uses frame averages only, so it can shift but never distort. **Measured on a 3-hop render: chain drift 5.6/255 without it, 0.3/255 with it.** Worth turning on for anything past two hops. It ships off because enabling it also clamps the master to 0..1, and because the correction grows with hop count — by hop 10 it is subtracting ~23/255 and will start crushing blacks. Switching modes never invalidates the hop cache, so it is free to A/B. Do **not** judge it by whether the seams flatten to zero: real scene brightness changes across a cut should survive, and they do.

## Shot plan

The cards write this; you rarely see it. It is under **JSON** on the node if you want to copy a plan between workflows.

```json
{
  "shots": [
    {
      "beat": "The cook stands at the counter, looks up, and speaks one short line.",
      "directives": {"camera": "hold", "framing": "medium", "pace": "steady", "tail": "ongoing"}
    },
    {
      "beat": "The cook sets the knife down and turns toward the window, still talking.",
      "directives": {"join": "continuous", "camera": "push_in", "framing": "close"}
    }
  ]
}
```

Shot 1 is the whole opening. Every later shot is **only the new beat** — the node supplies the identity lock, the live-frame citation and the join itself.

Fields, all optional except `beat`:

| | |
|---|---|
| `beat` | What happens this hop. |
| `directives` | The five axes below. |
| `prose` | Free text appended verbatim, for anything the vocabulary lacks. |
| `seed`, `steps`, `duration` | Per-shot overrides. `duration` takes the same labels as the widget (`"8 s"`). |
| `locked` | Reuse this shot's cached render even when its inputs changed -- freeze a take you like while you rewrite the hops around it. Needs `cache_hops=on`, and give the shot an `id`. Not to be confused with `subjects.N.locked`, which is identity text. |
| `id` | Stable name, used as the cache pointer. Generated if absent. |

### Directives

| axis | options |
|---|---|
| `join` | `continuous`, `match_cut`, `hard_cut` — ignored on shot 1, which has nothing to join to |
| `camera` | `hold`, `pan_follow`, `push_in`, `pull_back`, `orbit`, `handheld` |
| `framing` | `keep`, `wide`, `medium`, `close` |
| `pace` | `slow`, `steady`, `brisk` |
| `tail` | `ongoing` (default), `settle`, `hold` |

An unset axis emits nothing rather than asserting a default, so it costs no tokens.

**Everything is phrased affirmatively, on purpose.** Sampling runs at cfg 1.0 with no negative branch, so every concept named is additive — "no cut" puts the word `cut` in front of the encoder. If you add prose of your own, say what the shot *is* doing.

`join=continuous` together with a framing change and a held camera will warn: with the camera still, the only way to reach a new framing is a cut, so the two are asking for opposite things. Use `push_in` / `pull_back` / `pan_follow` to earn the framing on the move, or `framing: keep`.

A camera move that points the opposite way from the framing (`push_in` with `wide`, `pull_back` with `close`) warns too -- those are physically contradictory whatever the join.

When `join=continuous` and the camera *is* moving, the framing sentence is compiled as a **landing** ("The move settles into a close shot...") rather than as the shot's opening state, so it does not fight the pin that still holds the previous framing.

## Writing beats

> The full authoring guide, including how to have a model write plans for you, is **[PROMPTING.md](PROMPTING.md)**. What follows is the short version.

Sampling runs at cfg 1.0 with no negative branch, so **the prompt is additive:
anything you name is added, and nothing can be removed by mentioning it.** Three
rules follow, each confirmed against renders rather than reasoned from the code.

**Point the action line at the reference you want used.** Describing a reference
in the register is not enough -- the beat is what drives the frame. Write the tag
into the action:

```
The cook stands at the counter in @kitchen, looks up from the chopping board,
and speaks one short line to someone off-frame.
```

`@kitchen` resolves to the right `<Picture N>` on every hop it is active, so
rewiring references never breaks the text. Phrase it as a place that is depicted,
not as a container to be placed inside -- otherwise you can get a literal
composite of the photograph.

**Never name the thing you want to end.** `The cook stops talking` keeps her
talking. Describe the state you want as a pose plus a sound:

```
The cook leans back against the counter with her lips closed, and lets her eyes
move slowly across the room. The kitchen is quiet apart from the hum of the
refrigerator and faint street noise through the window.
```

H3 generates audio for the whole hop no matter what, so silence must be written
as a sound -- room tone, a fridge, traffic. Written as an absence, you get speech.

**Put a state change at the end of the previous shot.** Every hop after the first
opens by holding the frames it was handed, and the audio pin carries the tail of
the previous hop across the join. If shot 2 ends mid-sentence, no wording in shot
3 will make it start quiet. Finish the line where it actually finishes:

```
shot 2: The cook sets the knife down, turns toward the window, and finishes her
        last word as she looks out.
shot 3: The cook leans back against the counter with her lips closed...
```

To be silent, still, or somewhere else in a shot, arrive there before the
previous shot ends.

**Pick ambience that is narrowband and specific.** After the first shot, official
soundscape fields are stripped and ambience is plain prose, so the exact words
matter. "faint street noise through the window" is broadband and renders as a
five-second hiss; "the low hum of the refrigerator" does not. If a continuous bed
still misbehaves, name a single discrete event instead -- "a single click from the
refrigerator, then stillness".

**Set `tail` on your last shot.** `settle` and `hold` change the final sentence of
the prompt, which governs the terminal state of the clip. Left on `ongoing`, the
model is told action is still underway at the final frame and will invent
something to satisfy it -- on a last shot that means a stray gesture or a stray
line of dialogue in the closing second.

## Let a model write the plan

Open **WRITE** on the node, point it at any OpenAI-compatible server, say what
you want in one sentence, and press **Write plan**. It fills the SCRIPT cards
and the REFERENCES rows together — and the pictures already on those rows go
with the request, so the model describes what it is actually looking at rather
than guessing from a filename.

![The WRITE bar](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/write-panel.png)

- **Context 32768.** The system prompt alone is ~6,000 tokens, the reply another
  1,000–2,000, and every reference picture costs ~260 on top.
- **Reasoning off.** Thinking tokens come out of the same budget; a reply that
  stops before the JSON closes is the tell.
- **Temperature 0.3.** Higher and the JSON grows trailing commas and smart quotes.

Server settings are saved on this machine only — they are not part of the
workflow, so a shared `.json` never points at your server.

**Treat what comes back as a strong draft, not a finished plan.** It gets the
structure right — hop count, join types, which reference rides which hop, a
sound bed on every quiet hop — and that is the part that is tedious by hand.
Two things are worth reading every time: each reference's `desc`, which can be
confidently wrong about its own photograph, and the spoken words in every beat.
The node lints both and prints what it finds before you render.

`prompt_pack/` is the same writer as a copy-paste prompt for a chat window.

## Reference register

```json
{
  "refs": [
    {"tag": "hero_face",   "file": "cook_face.jpg",   "subject": 1, "retention": "fully_preserved"},
    {"tag": "hero_outfit", "file": "cook_apron.jpg",  "subject": 1, "retention": "partially_copy"},
    {"tag": "kitchen",     "file": "kitchen_wide.jpg", "retention": "reference"}
  ],
  "subjects": {
    "1": {"name": "the cook", "locked": "the same face, the same short dark hair"}
  }
}
```

`file` is a picture in `ComfyUI/input/h3_refs`, set by the rail. `tag` is what you write in your beats — `@hero_face` — and the node resolves it to the right `<Picture N>` **per hop**, so pulling a still out of the middle no longer breaks every later reference.

`subject` groups pictures per person. This matters: declaring every picture as a photo of `<Subject 1>` makes the model render the *average* of two different people.

`retention` says how much of a picture carries over — `fully_preserved` (face and bone structure exactly), `partially_copy` (the garment and its cut), `reference` (layout, surfaces and light, i.e. a place). Refs with a subject default to `fully_preserved`; everything else defaults to `reference`.

Add `"shots": [1, 2]` to a ref to keep it out of the hops it does not belong in. On a continuation chain (`hop_script=next` / a shot plan), omitting `shots` means **hop 1 only** — right for a place plate, which beats the pin if it rides a hop set somewhere else. **Put face plates on every hop:** a hop with no face reference comes back a different person and no later hop recovers.

## Hop cache

`cache_hops=on` writes every rendered hop to ComfyUI's temp dir as lossless FFV1 video plus a float32 `.npy` waveform, evicting least-recently-used above `cache_budget_gb`. The key is chained and includes the model fingerprint and, per hop, which pin mechanism was used -- a hop pinned by the AddGuide fallback is not the same render as one pinned by Motion-Context.

The key **chains** — each hop's key includes the previous hop's — because hops are causally dependent. So:

- edit shot 3 and re-queue → shots 1 and 2 load from cache, only 3 renders;
- edit shot 1 → all three re-render, which is correct, not a bug;
- change resolution, sampler, a LoRA, or an attention setting → the whole chain re-renders;
- change a reference picture → only the hops that picture rides re-render. Swapping the file behind `@outfit` when it rides hop 5 leaves hops 1-4 on cache. Before 1.1 this invalidated everything.

That last one is worth knowing about. The node cannot read the settings on your LoRA and attention nodes, so instead it fingerprints what they *did* to the model — which weight keys were patched, at what strengths, and the attention overrides. Change a LoRA strength and the cache correctly invalidates. Two different LoRAs touching exactly the same keys at exactly the same strengths would look identical to it; that is the one gap.

Set `locked: true` on a shot to pin it to its last render regardless.

## Reading a plan before you render it

`dry_run=on` compiles every hop's prompt and stops. No model, no sampler,
seconds instead of minutes. The compiled text comes out on `info`; the same
thing comes out on `contact_sheet` as a page you can read at a glance.

This is the only way to see what the text encoder will actually receive. The
directive layer, the continuation scaffolding, the identity lock and the
`<Picture N>` citations are all assembled at render time, so until now the first
sight of the real prompt was in the log of a render you had already paid for.

![`lock` and the range button on the shot cards](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/shot-lock-range.png)

*`lock` freezes a take you like; the `⏵` beside it sets the render range to that shot alone.*

`render_through=N` stops after N hops. With `cache_hops=on` the hops you already
rendered stay on disk, so 3 → 5 → 8 builds a chain up in stages and only ever
renders the new hops. The plan is not truncated: shot 4 still knows it is shot
4, keeps its own seed, and keys the same way it will in the full run.

`quality=draft` forces the 448p tier and 6 steps — enough to read blocking, camera and
whether a join lands. Resolution and steps are both in the cache key, so a draft
never overwrites the final it stands in for; the two simply cost two entries.

Treat it as a **fidelity** lever rather than a speed one. Measured at ~42 s/hop
against ~45 s/hop at 7 steps: if you already render at 448p and 6–8 steps —
the regime this pack targets — draft saves almost nothing, and `dry_run` is the
fast button. Draft earns its place when your final is genuinely heavier, 768p
at 14 steps.

## Contact sheet

`contact_sheet=on` adds an image on the fourth output: one row per hop, that
hop's first and last **delivered** frame side by side, its beat, its directives,
and what actually happened to it — seed, steps, whether it came from cache, what
the tone correction did. Wire it to a Save Image.

![Contact sheet](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/contact-sheet-vlog.png)

On a chain of any length this is the fastest way to find the hop that broke. The
one hard cut in the 114-second reference chain sat in a plan that passed every
automated check; it is obvious in one row of a contact sheet and invisible in a
progress bar.

## Brightness drift, and the two things that fix it

These are different problems and they need different settings.

**The step at a join** is the denoiser's tone bias on a fresh generation.
`tone_compensate=frame_shift` measures it on the overlap the hop regenerated and
cancels it, which is why the seams in a corrected chain read as invisible.

**The slide across a whole chain** is different. Each hop also darkens across
its *own* frames, hands that darker tail to the next hop, and the next hop
starts from there. Seam correction cannot see this — every individual join is
exact while the film gets steadily dimmer. The 8×15 s reference chain slid from
luma 46 to 11 across hops 2–6 with every seam already corrected.

`tone_compensate=anchor` is frame_shift plus a pull back toward **hop 1's**
exposure — the one tone in the chain nothing drifted into. Two things keep it
from causing the problem it is fixing:

- the pull **ramps in** across the first two seconds of each hop, so frame 0
  still matches the previous hop's last frame exactly and the seam stays as
  clean as frame_shift left it;
- it is **capped** per hop (`tone_anchor`, default 0.35 = about a third of the
  gap), so a long slide is corrected over several hops instead of one hop
  snapping back and pumping.

A scene that is *meant* to get darker looks exactly like drift from the
inside, so a shot can opt out:

```json
{ "beat": "She steps down into the cellar.", "tone": "rebase" }
```

`"tone": "free"` skips the pull for that hop only. `"tone": "rebase"` also moves
the anchor onto that hop, which is how a scene that is genuinely darker from
here on stops being fought for the rest of the film.

Set `tone_anchor` to 0 to get plain frame_shift back.

**Measured** on three hops from one seed and one cache — the hop store writes
before the tone stage runs, so flipping the mode re-grades the same renders and
only the correction differs:

| `tone_anchor` | drift across the chain | worst seam |
|---|---|---|
| off | 13.5/255 | 2.1/255 |
| 0.15 | 7.4 | 1.3 |
| 0.35 | 5.1 | 1.9 |
| 0.60 | 2.9 | 2.6 |

Both columns are **H3 Seam Report's own numbers**, so what you measure matches
what this table says.

![Seam report chart](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/seam-report.png)

Drift falls evenly — 45%, 62%, 78% of the uncorrected slide. The seam does not:
`0.15` pulls it *tighter* than the uncorrected chain, and it grows from there at
roughly 0.6/255 per step of strength. Hop 1 is byte-identical in all four.
**0.35 is the default and stays** — it halves the drift while every seam still
reads as marginal or better.

One caveat the numbers cannot capture: a scene that brightens *for a reason* —
walking toward a window — is indistinguishable from drift from the inside, and
anchor will flatten it. That is what the per-shot `tone` field is for.

## The MODEL wire

Four ordinary nodes, in this order:

    UNETLoader → LoRA stack (mode=minimax) → H3 AdaLN Fix → MiniMax Low VRAM Attention → H3 SLA Attention → H3 Ref2VA Chain

**Take CLIP from the LoRA loader's second output**, not straight from the encoder, or the text-side keys H3 LoRAs carry are silently dropped.

They briefly lived inside this node as a `model_patches` JSON widget. That made the graph shorter and the node harder to read, so it was reverted — these belong where you can see them.

## Pinning the previous hop

Three widgets tune the pin, all defaulting to their pre-existing behaviour:

| widget | default | what it does |
|---|---|---|
| `audio_pin_frames` | `24` | Audio context handed to the pin, in frames. 24 is one second on the model's 40 Hz grid. Longer audio context costs conditioning rows but **no delivered frames**, so it is the cheap lever on speech that breaks across a join — try `96` (4 s) for continuous dialogue. |
| `pin_renorm` | `off` | Rescales each pinned latent so its spread matches the one hop 2 established. The pin's own sigma climbs hop over hop and that inflated pin conditions the next one, so texture ratchets along a long chain. A scalar rescale moves no structure, so it cannot blur detail. Video and audio are corrected separately — their sigmas drift by different amounts. Worth turning on for 3+ hops. |
| `pin_noise` | `0.0` | Mixes seeded noise into the pin — the other half of the same fix. Small values only; measured gains reverse above `0.10`, which is why the range stops there. |

The DiT pin is the previous hop’s **sampler latent** through Motion-Context when present (no decode/re-encode; audio window ends at the join). AddGuide on decoded frames is the fallback. `pin_to_qwen` still shows the incoming state to the text encoder:

| | |
|---|---|
| `off` | Pin stays DiT-only. |
| `last frame` (default) | Previous hop's last frame is `<Picture 1>`. Identity stills that still ride this hop shift to Picture 2+. |
| `pin clip` | Overlap frames become an extra `<Video>` at ~2 fps. No soundtrack on it, so `voice` stays `<Audio 1>`. |
| `both` | Last frame + pin clip. |

`@tags` in beats resolve per hop, so stills shifting to Picture 2+ does not break prose. Literal `<Picture N>` in hop 2+ beats would.

## Defaults

| | |
|---|---|
| resolution | 768p (1344×768 landscape) |
| duration | 10 s (243 frames) |
| overlap | 0.9 s (22 frames) |
| steps | 8, with a 4-step turbo LoRA — the regime this node targets |
| sampler / scheduler | `res_multistep` / `beta` |
| seed per hop | on |
| sigma shift | 12 / 3 |
| cache budget | 20 GB |
| tone_compensate | off (both shipped workflows set `frame_shift`) |
| tone_anchor | 0.35, used only by `tone_compensate=anchor` |
| quality | final |

Three shots at 10 s with a 0.9 s overlap is about 28 s of master after the overlap is dropped.

## Voice, music and trims

The **MEDIA** strip takes a first frame, a look reference clip, a voice
reference and a music bed — all files under `input/h3_refs`, all picked in the
panel, none of them a `Load Image` node you have to wire.

![The MEDIA tab with a reference clip loaded](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/reference-clip.png)

*A reference clip with its in/out scrubber, the description that tells the encoder what the clip is **for**, and `video input size` -- which decodes the plate straight to the size you pick instead of loading it at source resolution first.*

Each audio slot has a scrubber with an in/out window, and it is worth using.
H3 encodes the **whole** voice file into the conditioning with no cap, and every
latent frame of it is attended over on every step of every hop — so a
three-minute take is a large invisible tax on a clip that only needed four
seconds. The soundtrack window is cut from the track first, then `music_fit`
loops or trims that to the chain, which is what stops a mastered track always
starting the chain on its intro.

An end of `0` always means *to the end of the file*, so a longer replacement
file still plays out rather than being silently cropped to the old one.

## Limits

- **A workflow saved before 2026-08-28 loses its reference pictures.** The old `ref_image_N` sockets carried tensors, so there is no filename to recover. The rail names each affected ref and asks you to pick its picture; nothing else about the plan is lost.
- There is no shot-level `refs` field. Activation lives on the reference: give a ref a `shots` list, or use the per-hop chips on its row in the rail.
- The reference `desc` and subject `locked` text go to the encoder verbatim, every hop. At cfg 1.0 there is no negative branch, so a detail that is not in the photograph is **asked for**, not ignored. Describe what you actually wired.
- `HTCContinuityState` is **setting only**. Characters live in the reference register; the node's `characters_*` fields were removed because filling in both injected identity text twice.
- Each join hard-cuts video but crossfades audio ~40 ms, so A/V drifts ~40 ms per hop.
- Texture still ratchets on long chains. Stay around 3–5 hops until that is handled.
- A 22-frame pin clip is ~2 Qwen frames at 2 fps. It is a live-state hint, not a full previous-clip watch.
- A longer overlap does not fix continuity — it can pin whatever content happens to be in that longer tail.

## Nodes

**H3 Ref2VA Chain** — `images`, `audio`, `info`, `contact_sheet` out. Wire `CreateVideo` + `SaveVideo` as in the example workflow, `info` to a Preview Text node, and `contact_sheet` to a Save Image.

**H3 Chain Preview** — a passthrough panel for the IMAGE (and optionally AUDIO) wire, placed **between the chain and `CreateVideo`**. Images and audio come out unchanged, so adding or removing it changes no pixels. It shows the live sample, the seam (the previous hop's last frame beside this hop's first), a chain-wide progress bar, cache hit / seed / steps per hop, **which pin mechanism each hop actually used** — a latent Motion-Context pin or the AddGuide pixel fallback — and end-of-run A/V drift. Drag the grip to resize the stats panel; double-click it to reset.

**H3 Tone Compensate** — `images` out. Corrects a generated segment's tone against the previous one, estimated on the overlap they share. **For hand-built chains only.** It cannot fix `H3 Ref2VA Chain`'s output: that node joins its hops internally and drops each hop's first `overlap` frames at the seam, so the regenerated copies this needs are already gone by the time images leave it. Use the chain node's `tone_compensate` widget instead. Estimator ported from [rkfg/ComfyUI-MiniMaxH3-ToneCompensate](https://github.com/rkfg/ComfyUI-MiniMaxH3-ToneCompensate) (MIT).

**H3 Seam Report** — `report` (STRING) + `chart` (IMAGE). **Ships wired on the Starter canvas.** Set `hops` to your shot count: it derives hop length from frames, hops and overlap, so a wrong `hops` does not error — it returns a plausible length and puts every seam where no join exists. Wire the chain's `images` into it and it measures the brightness step at every join, says whether each is invisible / marginal / visible, and totals the chain's cumulative drift. A single reading includes whatever the scene did across the cut — the frames either side are ~0.9 s apart in scene time — so treat one number as an upper bound; to isolate the seam itself, render twice from the same seed and cache changing only `tone_compensate`, and compare.

**H3 Continuity State** — `continuity_state` (STRING) out. **Setting only**: `setting_locked` / `setting_context` / `setting_mutable`. Characters belong in `ref_plan`.
