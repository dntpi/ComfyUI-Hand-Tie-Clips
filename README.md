# Hand Tie Clips

One node. Write a shot plan, drop in your reference stills, queue.

> **Renamed 2026-08-29.** This pack was `ComfyUI-H3-Ref-Chain`. The nodes now
> live under the **Hand Tie Clips** category and register as `HandTieClips`,
> `HTCChainPreview`, `HTCToneCompensate` and `HTCContinuityState`. The old ids
> are still registered as deprecated aliases, so **every workflow saved before
> the rename keeps loading** — they are just hidden from node search. Nothing
> needs migrating.

**Writing for it:** [PROMPTING.md](PROMPTING.md) is the authoring guide — the rules that come from what this model actually does, not from taste. [prompt_pack/](prompt_pack/) has a copy-paste prompt that gets a language model to write plans for you.

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
- `ffmpeg` on PATH, if you turn `cache_hops` on
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

Keep later-hop beats on **what happens next**. Do not re-describe the face; the photos, the register and the pin already carry it.

Two modes:

- **Simple** — one prompt box and a hop count, the way it always worked. Later hops advance the same action rather than replaying the opening.
- **Shots** — one card per hop with directives. Switching from Simple carries your prompt over and splits it on `---`.

Only one of them is on screen at a time, so there is never a text box quietly doing nothing.

Under the script sits **RUN**, collapsed, holding everything that is not per-shot: output size and length, sampling, the join and pin controls, and the hop cache. Its title line summarises the run — `1.0 MP 16:9 · 10s ×3 · 14 steps res_multistep · cache off` — so you can read the setup without opening it. In Shots mode `chains` and `hop_script` are not offered there, because the shot list already decides both.

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
- change resolution, sampler, a reference still, a LoRA, or an attention setting → the whole chain re-renders.

That last one is worth knowing about. The node cannot read the settings on your LoRA and attention nodes, so instead it fingerprints what they *did* to the model — which weight keys were patched, at what strengths, and the attention overrides. Change a LoRA strength and the cache correctly invalidates. Two different LoRAs touching exactly the same keys at exactly the same strengths would look identical to it; that is the one gap.

Set `locked: true` on a shot to pin it to its last render regardless. Needs `ffmpeg` on PATH.

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
| resolution | 1.0 MP (1280×736 landscape) |
| duration | 10 s (243 frames) |
| overlap | 0.9 s (22 frames) |
| steps | 8, with a 4-step turbo LoRA — the regime this node targets |
| sampler / scheduler | `res_multistep` / `beta` |
| seed per hop | on |
| sigma shift | 12 / 3 |
| cache budget | 20 GB |

Three shots at 10 s with a 0.9 s overlap is about 28 s of master after the overlap is dropped.

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

**H3 Ref2VA Chain** — `images`, `audio`, `info` out. Wire `CreateVideo` + `SaveVideo` as in the example workflow, and `info` to a Preview Text node.

**H3 Chain Preview** — a passthrough panel for the IMAGE (and optionally AUDIO) wire, placed **between the chain and `CreateVideo`**. Images and audio come out unchanged, so adding or removing it changes no pixels. It shows the live sample, the seam (the previous hop's last frame beside this hop's first), a chain-wide progress bar, cache hit / seed / steps per hop, **which pin mechanism each hop actually used** — a latent Motion-Context pin or the AddGuide pixel fallback — and end-of-run A/V drift. Drag the grip to resize the stats panel; double-click it to reset.

**H3 Tone Compensate** — `images` out. Corrects a generated segment's tone against the previous one, estimated on the overlap they share. **For hand-built chains only.** It cannot fix `H3 Ref2VA Chain`'s output: that node joins its hops internally and drops each hop's first `overlap` frames at the seam, so the regenerated copies this needs are already gone by the time images leave it. Use the chain node's `tone_compensate` widget instead. Estimator ported from [rkfg/ComfyUI-MiniMaxH3-ToneCompensate](https://github.com/rkfg/ComfyUI-MiniMaxH3-ToneCompensate) (MIT).

**H3 Continuity State** — `continuity_state` (STRING) out. **Setting only**: `setting_locked` / `setting_context` / `setting_mutable`. Characters belong in `ref_plan`.
