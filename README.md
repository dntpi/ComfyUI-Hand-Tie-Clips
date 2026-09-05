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

**Multi-hop MiniMax H3 video from one node.** Write a shot plan, drop in your
reference stills, queue. The pack's whole job is the *join* — the place where
one generation ends and the next begins.

![Six consecutive frames across a join](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/seam-frames.png)

*Six consecutive frames spanning the join between hop 1 and hop 2. One of these
is the last frame the first generation produced and the next is the first frame
of a second, separate generation. The pack exists so that you cannot tell which.*

Each hop is native **MiniMax H3 Reference-to-Video**. Hops after the first are
guided by the previous hop's sampler AV latent through
[ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)
when it is installed; stock `MiniMaxH3AddGuide` is the fallback, and the console
says which one each hop took.

---

## What it gives you

| | |
|---|---|
| **One node, five tabs** | SCRIPT, REFS, MEDIA, WRITE and SWAP on the node body. No `Load Image` chains, no JSON in a textarea. |
| **A shot plan** | One card per hop — the shot count *is* the hop count. Beat, duration, seed, and five directive axes compiled to vetted prose. |
| **A reference register** | Stable `@tags` for your stills, grouped per subject, each with a retention rule and its own pixel budget. Pulling one out of the middle no longer renumbers `<Picture N>`. |
| **WRITE** | A plan writer on the node. Point it at any OpenAI-compatible server, describe the scene in a sentence, and it fills the script *and* the reference rows — reading the pictures you already dropped in. |
| **SWAP** | One-hop identity swap from a reference clip. Four named modes, so what *stays* with the clip is stated rather than left to omission. |
| **A lossless hop cache** | Re-roll shot 5 of 8 and only 5–8 re-render. Resume after a crash. Hold about one hop in RAM instead of the whole film. |
| **A lip-sync lock** | `master_audio_file` — one continuous take every hop locks to, delivered as a passthrough. |
| **Instruments** | Dry run, contact sheet, seam report and a live preview panel, so you can find the hop that broke without scrubbing the file. |

---

## Install

From your `ComfyUI/custom_nodes/` folder:

```
git clone https://github.com/dntpi/ComfyUI-Hand-Tie-Clips.git
```

Or from the HuggingFace mirror, which is the same repository:

```
git clone https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips
```

Cloning puts the folder at the right depth, which is the mistake the zip route
invites — one level too deep
(`custom_nodes/ComfyUI-Hand-Tie-Clips/ComfyUI-Hand-Tie-Clips/`) and the pack
simply will not appear. Updating later is `git pull` and a restart.

**No dependencies to install.** Everything it imports — `torch`, `numpy`,
`PIL`, `av`, `aiohttp` — already ships with ComfyUI, which is why
`dependencies` in `pyproject.toml` is empty.

Then **restart ComfyUI** (Python changed) and **hard-refresh the browser**
(Ctrl+Shift+R). The editor is served from
`/extensions/ComfyUI-Hand-Tie-Clips/`, and a stale cache is the single most
common reason the node mounts with no UI on a fresh install.

It is installed correctly when all three are true:

- the startup log carries a line beginning `[HandTieClips]`
- the browser console says `[HandTieClips] editor ui v2.0.0 loaded`
- node search shows a **Hand Tie Clips** category with five nodes, each once

Workflows saved before the 2026-08-29 rename keep loading — the old ids are
registered as deprecated aliases. Nothing needs migrating.

## Needs

- ComfyUI new enough to include **Add Guide for MiniMax H3** (`MiniMaxH3AddGuide`)
- A **ref2va** (or hybrid ref2va) checkpoint — fl2va has no reference rows
- Video VAE + audio VAE + MiniMax text encoder
- **Recommended:** [ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context)
  for the latent join. Without it the node falls back to `MiniMaxH3AddGuide`,
  which still works and still chains — it is a different join, so a seam you
  are comparing against someone else's render may not be the same code path.

The two shipped workflows wire the **turbo stack** this node is actually run
with, because an example without it is not the graph anyone uses:

```
UNETLoader -> LoRA Loader Stack -> H3 AdaLN LoRA Fix -> MiniMax H3 Low VRAM
           -> H3 SLA Attention -> Model Preview Override -> Hand Tie Clips
```

| pack | nodes |
|---|---|
| [ComfyUI-PlagueKind-Nodes](https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes) | LoRA Loader Stack, H3 AdaLN LoRA Fix, H3 SLA Attention |
| [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | MiniMax H3 Low VRAM Attention (experimental), Model Preview Override |

**CLIP reaches the node from the LoRA loader, not from the encoder** — that is
what makes the text half of every LoRA land. Do not rewire it back.

None of it is required by the node itself. Missing a pack, its nodes load as
red boxes: delete them, wire the loader straight into `model` and the encoder
into `clip`, and raise `steps` from 7 to around 20.

Both shipped workflows are saved pointing at the exact quantised files they
were rendered with. These are **not** the only builds that work — any ref2va or
hybrid ref2va set will do. Repoint the loaders at what you have; a loader
showing an empty or red filename is naming a file you do not have, not a broken
workflow.

<details>
<summary>The exact files the examples name</summary>

| loader | file |
|---|---|
| `UNETLoader` | `minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors` |
| `CLIPLoader` | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` |
| `VAELoader` (video) | `minimax_h3_video_vae_int8_convrot.safetensors` |
| `VAELoader` (audio) | `minimax_h3_audio_vae_fp32.safetensors` |
| LoRA Loader Stack | `minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors` |
| `tiny_vae` | `taeh3.safetensors` (or `none`) |

</details>

---

## Start here

1. Load **`workflows/HandTieClips_Starter.json`** — two hops, no references,
   runs as soon as the loaders point at your files. It carries a **READ ME**
   board on its own canvas, to the left of the loaders: the laws, the directive
   table, the reference rules and the failure table, where you need them rather
   than in another file.
2. Point the loaders at your ref2va DiT, encoder, and both VAEs.
3. In **REFS**, add a row per identity still and **drop a picture onto its
   thumbnail**. Give it a `@tag`; group photos of the same person under one
   subject number. Files land in `ComfyUI/input/h3_refs` — there is nothing to
   wire.
4. In **SCRIPT**, write one card per hop. Shot 1 is the whole opening; every
   later card is **only the new beat**.
5. Optional: a first frame, up to three reference clips, up to three voices, a
   music bed, a master audio take — all in **MEDIA**.
6. Queue. Wire **`info`** to a Preview Text node to read the fully assembled
   prompt for every hop.

`workflows/HandTieClips_Showcase.json` is the six-hop version and needs three
pictures of your own. Both carry the turbo stack above.

![The editor's tabs, with SCRIPT open on a ten-shot plan](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/editor-tabs.png)

*Each tab carries its count, and RUN stays pinned below them. SCRIPT is one
card per hop: the beat, its duration, and its own join, camera, framing, pace
and tail, with `lock` and the range button on the card header.*

Under the script sits **RUN**, collapsed, holding everything that is not
per-shot: output size and length, sampling, the join and pin controls
(`pin_mech`, `last_frame_guide`, tone), and the hop cache. Its title line
summarises the run — `1344x768 · 10s ×3 · 14 steps res_multistep · cache off` —
so you can read the setup without opening it.

Two script modes: **Simple** (one prompt box and a hop count, the way it always
worked) and **Shots** (one card per hop with directives; switching from Simple
carries your prompt over and splits it on `---`). Only one is on screen at a
time, so no text box is ever quietly doing nothing.

Keep later-hop beats on **what happens next**. Do not re-describe the face; the
photos, the register and the pin already carry it.

---

## The one law

Sampling runs at **cfg 1.0 with no negative branch**, so the prompt is purely
additive: **anything you name is added, and nothing can be removed by
mentioning it.** Almost every rule in this pack is a consequence, and each of
the rules below was confirmed against renders rather than reasoned from code.

**Never name the thing you want to end.** `The cook stops talking` keeps her
talking. Write the state you want as a pose plus a sound:

> The cook leans back against the counter with her lips closed, and lets her
> eyes move slowly across the room. The kitchen is quiet apart from the hum of
> the refrigerator.

H3 generates audio for the whole hop no matter what, so silence has to be
written as a sound. Written as an absence, you get speech. The ban is on the
idea, not on a word list: *fades, subsides, dies down* all name an ending as
surely as *stops* does.

**Point the action line at the reference you want used.** Describing a
reference in the register is not enough — the beat is what drives the frame.
Write the tag into the action: `The cook stands at the counter in @kitchen,
looks up…`. Phrase a place as depicted, not as a container to be placed inside,
or you can get a literal composite of the photograph.

**Put a state change at the end of the previous shot.** Every hop after the
first opens by holding the frames it was handed, and the audio pin carries the
previous hop's tail across the join. If shot 2 ends mid-sentence, no wording in
shot 3 will make it start quiet — arrive there before the previous shot ends.

**Pick ambience that is narrowband and specific.** "faint street noise through
the window" is broadband and renders as a five-second hiss; "the low hum of the
refrigerator" does not.

**Set `tail` on your last shot.** Left on `ongoing`, the model is told action is
still underway at the final frame and will invent something to satisfy it — a
stray gesture, or a stray line of dialogue in the closing second.

> The full authoring guide is **[PROMPTING.md](PROMPTING.md)**. Its rules come
> from what this model actually does, not from taste. `prompt_pack/` is the
> same writer as a copy-paste prompt, for when you would rather work in a chat
> window.

---

## Let a model write the plan

Open **WRITE**, point it at any OpenAI-compatible server, say what you want in
one sentence, and press **Write plan**. It fills the SCRIPT cards and the
REFERENCES rows together — and the pictures already on those rows go with the
request, so the model describes what it is actually looking at rather than
guessing from a filename.

![The WRITE bar](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/write-panel.png)

- **Context 32768.** The system prompt alone is ~6,000 tokens, the reply
  another 1,000–2,000, and every reference picture costs ~260 on top.
- **Reasoning off.** Thinking tokens come out of the same budget; a reply that
  stops before the JSON closes is the tell.
- **Temperature 0.3.** Higher and the JSON grows trailing commas and smart quotes.

Server settings are saved on this machine only — a shared `.json` never points
at your server.

**Treat what comes back as a strong draft, not a finished plan.** It gets the
structure right — hop count, join types, which reference rides which hop, a
sound bed on every quiet hop — which is the part that is tedious by hand. Read
two things every time: each reference's `desc`, which can be confidently wrong
about its own photograph, and the spoken words in every beat. The node lints
both and prints what it finds before you render.

---

## SWAP

A **one-hop identity swap** from a reference clip: the clip supplies the motion
and the scene, a still from your rail supplies the person. Trim the clip in
MEDIA, pick an identity, press **Write**, then **Accept** — which writes exactly
one shot and the clip's description, and never touches `ref_plan`. Your
register is not rewritten.

Four modes, because at cfg 1.0 **a mode that merely omits the swap line does
not keep the clip's person** — the identity still is in front of the encoder
either way and governs the subject anyway. Each mode states positively what
stays:

| mode | what the photograph contributes |
|---|---|
| `replace_person` | Face, build, hairstyle **and** wardrobe. |
| `head_swap` | Face, hair and skin tone. The body stays with the clip: build, posture, hands and every garment. |
| `face_only` | Facial features only. Hair, ears, expression, build and clothes stay with the clip. |
| `keep_person` | Nobody is swapped; the clip is a scene and motion plate. The identity picker greys out. |

Alongside them: **background** (from the clip / from a picture `@tag` / free)
and an optional **wardrobe plate** — a `@tag` whose garment is *worn*, draping
on the body in frame and creasing where it bends, not pasted.

**Two things to get right, both of which cost renders to find out:**

- **Do not run MEDIA's describe on the clip before a swap.** That caption
  reaches the encoder as what `<Video 1>` *is*, and — being additive — a
  caption naming a person asks for the person you are about to replace. Four
  consecutive "head swap doesn't work" reports came down to that, a missing
  frame sequence and a weak citation; none of them was broken code. SWAP now
  warns when a caption names somebody.
- **Drop the clip to ~0.3 MP.** A reference clip's decode area is its token
  count, and its token count is its influence. A full-size plate out-argues a
  single photograph.

---

## Tested in public

Before 2.0 shipped, a tester ran **ten controlled nine-hop chains** — 65 s each,
one variable per run, same model, LoRA, references, locked audio and seed — and
measured them end to end with her own instruments rather than by eye. The
results below are hers, used with permission.

![Scorecard across the ten runs](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/tester-scorecard.png)

Her headline: **`anchor: "restart"` won.** It is the only run type whose last
ten seconds is still on the reference still's side of its own hop 1 — colour,
texture, background and framing all hold. The cost is that a restart is a hard
cut. Second best was a small `pin_noise`, one run each way. **Nothing else moved
the needle** — not `pin_renorm=band`, not reference protection, not a different
DiT, not `ref_image_size=match`, not a fresh seed — and the plain control was
the worst of the ten.

![Background detail across hops](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/tester-background-drift.png)

Her diagnosis is sharper than ours was: **relay convergence with no content
anchor.** Each hop inherits its predecessor's end state and nothing pulls it
back toward the reference. It also explains why the scale knobs did nothing —
the pin's *statistics* never drifted (sigma stayed within ±6% across nine hops)
while the picture lost a fifth of its chroma and doubled its background edges.
The drift is in the latent's **content**, not its scale, and `pin_renorm` and
`pin_noise` only rescale.

Three things in 2.0 come straight from that study:

- **Restart hops write their full length.** They used to drop 0.9 s of new
  content as though they were continuations.
- **`last_frame_guide=before_restart`**, so both sides of a restart cut meet on
  the same photograph and it reads as a match cut rather than a jump.
- **`tone_anchor_ref=still`.** The anchor used to hold hop 1, on the reasoning
  that hop 1 is the one tone in the chain nothing has drifted into yet. Her
  measurements say that is false — hop 1 is the *first casualty*, already short
  of the still before any relay has happened:

| | reference still | hop 1 |
|---|---|---|
| chroma | 33.6 | 30 |
| b\* (warmth) | 26.6 | 22 |
| fine detail | 1.00 | 0.72–0.99 |

A chain anchored on hop 1 converges on a target that already fell short.

**What her study could not fix, this release does not claim to fix.** Texture
still ratchets on long chains, and 3–5 hops is still the honest limit.

---

## Holding a chain together

### The join

`pin_mech` chooses it. `auto` (default) is Motion-Context when the pack is
installed, the overlap has a matching `context_length`, and the previous hop
left a sampler latent; AddGuide pixels otherwise. Forcing `motion_context` or
`addguide` does **not** fall back — it fails with the reason, because a lever
that silently becomes the other setting cannot be compared against it.
`motion_context` is a latent join with no decode/re-encode; `addguide`
re-encodes decoded pixels, which is itself a VAE round trip. Switching
re-renders hops 2+ and leaves hop 1 on disk. A restart hop pins nothing.

<details>
<summary>Three more pin dials, all defaulting to their pre-existing behaviour</summary>

| widget | default | what it does |
|---|---|---|
| `audio_pin_frames` | `24` | Audio context handed to the pin, in frames; 24 is one second on the model's 40 Hz grid. Longer costs conditioning rows but **no delivered frames**, so it is the cheap lever on speech that breaks across a join — try `96` (4 s) for continuous dialogue. |
| `pin_renorm` | `off` | Rescales each pinned latent back toward the first pinned hop's, against the texture ratchet — measured at +4.2% mid-band per join, flat inside each hop. Both modes are scalar rescales, so neither moves structure or can blur detail, and video and audio are corrected separately. **`band`** matches the high-band fraction, which is the statistic the ratchet actually moves: a 12.74% drift went to −0.04%. **`sigma`** is the original lever, kept for old workflows and measurably the wrong statistic — total sigma *falls* across a chain whose picture is baking, so it corrects the wrong way. Use `band` for 3+ hops. |
| `pin_noise` | `0.0` | Mixes seeded noise into the pin — the other half of the same fix. Small values only; measured gains reverse above `0.10`, which is where the range stops. |

`pin_to_qwen` separately shows the incoming state to the *text* encoder: `off`,
`last frame` (default — the previous hop's last frame becomes `<Picture 1>`, and
identity stills shift to Picture 2+), `pin clip` (overlap frames as an extra
`<Video>` at ~2 fps, no soundtrack), or `both`. `@tags` in beats resolve per
hop, so stills shifting never breaks prose; a literal `<Picture N>` in a hop 2+
beat would.

</details>

### Brightness drift — two different problems

**The step at a join** is the denoiser's tone bias on a fresh generation.
`tone_compensate=frame_shift` measures it on the overlap the hop regenerated and
cancels it, which is why the seams in a corrected chain read as invisible.
Measured on a 3-hop render: chain drift 5.6/255 without it, 0.3/255 with it.
All three modes remove the drift equally well, but `gain_bias` and `lut` pair
pixels between a frame and its *regeneration*, fitting a slope that is not
really there; `frame_shift` uses frame averages only, so it can shift but never
distort.

**The slide across a whole chain** is different. Each hop also darkens across
its *own* frames, hands that darker tail to the next hop, and the next hop
starts from there. Seam correction cannot see this — every individual join is
exact while the film gets steadily dimmer. An 8×15 s chain slid from luma 46 to
11 across hops 2–6 with every seam already corrected.

`tone_compensate=anchor` is frame_shift **plus** a pull back toward a target,
matched in **Lab** — L\*, a\*, b\* and L\* spread — because chroma loss is the
largest measured drift and a per-channel RGB mean cannot restore it.
`tone_anchor_ref` picks the target: `hop1` (default) or **`still`**, which holds
the photograph and is the only setting that ever closes the 33.6-against-30 gap
above. `still` needs `start_image_file`, and under the Motion-Context join the
correction still only reaches the delivered frames, not the next hop's pin —
`pin_mech=addguide` is what closes that loop.

Two things keep the pull from causing the problem it is fixing: it **ramps in**
across the first two seconds of each hop, so frame 0 still matches the previous
hop's last frame exactly and the seam stays as clean as frame_shift left it;
and it is **capped** per hop (`tone_anchor`, default 0.35 ≈ a third of the gap),
so a long slide is corrected over several hops instead of one hop snapping back
and pumping.

![Seam report chart](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/seam-report.png)

| `tone_anchor` | drift across the chain | worst seam |
|---|---|---|
| off | 13.5/255 | 2.1/255 |
| 0.15 | 7.4 | 1.3 |
| 0.35 | 5.1 | 1.9 |
| 0.60 | 2.9 | 2.6 |

Both columns are **H3 Seam Report's own numbers**, so what you measure matches
what this table says. Drift falls evenly — 45%, 62%, 78% of the uncorrected
slide. The seam does not: `0.15` pulls it *tighter* than the uncorrected chain
and it grows from there. Hop 1 is byte-identical in all four. **0.35 is the
default and stays** — it halves the drift while every seam still reads as
marginal or better. Set `tone_anchor` to 0 for plain frame_shift.

A scene that is *meant* to get darker looks exactly like drift from the inside,
so a shot can opt out: `"tone": "free"` skips the pull for that hop,
`"tone": "rebase"` also moves the anchor onto it — which is how a scene that is
genuinely darker from here on stops being fought for the rest of the film.

### Restarts, and the last-frame guide

`anchor: "restart"` on a shot makes that hop a chain start — the start image is
frame 0 and nothing is relayed. It is **a cut**, so it belongs where a cut is
motivated: a pause, a change of thought, a new beat, not on a hop interval.
Pair it with `join: hard_cut` or `match_cut`; `continuous` is refused. Name the
room in the beat, because a pin-less hop has nothing else telling it where it
is. Never on shot 1, and it needs a start image.

`last_frame_guide` plants `start_image` at a hop's last **pixel** frame
(AddGuide `frame_idx=-1`, not latent T-1). It does not become the next hop's
frame 0. Needs a start image. Ships `off`.

**Reach for `before_restart`.** It guides only a hop whose *next* shot is a
restart, so both sides of the cut meet on one image — hop 3 used to end tight
and smiling, hop 4 open wide and neutral; with the guide, hop 3 *arrives* at the
still's framing. Measured: the four hop endings of a 4-hop chain converge to
3.8/255 of each other against 39.1/255 unguided, while mid-hop frames stay as
varied as ever (65.8 against 61.1). Two people watched it in motion and could
not see the convergence.

**`still` is the "I know what I am doing" setting.** It plants the photograph on
every hop, unconditionally, which overrides an authored `framing` at every hop
ending — a shot set `framing: close` plays close for six seconds and then snaps
to the still's wider framing in about 0.6 s, and the next hop pushes back in. A
user watching this described it, unprompted, as *"the camera kept cutting in and
out."* Frame-by-frame from that run, hop 3, `framing: close`:

    1.58s close   3.67s close   4.92s close   5.96s close-ish   6.58s WIDE   7.04s WIDE

Safe when no shot authors a framing; visibly wrong when they do.

---

## Reference

### Shot plan

The cards write this; you rarely see it. It is under **JSON** on the node if you
want to copy a plan between workflows.

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

Shot 1 is the whole opening. Every later shot is **only the new beat** — the
node supplies the identity lock, the live-frame citation and the join itself.
Fields, all optional except `beat`:

| | |
|---|---|
| `beat` | What happens this hop. |
| `directives` | The five axes below. |
| `prose` | Free text appended verbatim, for anything the vocabulary lacks. |
| `seed`, `steps`, `duration` | Per-shot overrides. `duration` takes the widget's labels (`"8 s"`). |
| `refs` | Which register stills ride this hop, as tags. Omit for the register default; `[]` is none; a list is those tags only, in that order. Unknown tags fail on the queue. |
| `anchor` | `"restart"` makes this hop a chain start. See above. |
| `tone` | `"free"` skips the chain-wide tone pull once; `"rebase"` also moves the anchor. |
| `locked` | Reuse this shot's cached render even when its inputs changed. Needs `cache_hops=on` and a stable `id`. Not to be confused with `subjects.N.locked`, which is identity text. |
| `id` | Stable name, used as the cache pointer. Generated if absent. |

**Hops can differ in length.** `duration` is per shot and everything downstream
sizes itself around it. Labels are the widget's — `5 s`, `7 s`, `8 s`, `10 s`,
`15 s` — and that set is fixed, not arbitrary: every value has to land on H3's
frame grid (`n % 17 == 5` at 24 fps), so there is no `6.5 s`. Editing one shot's
length invalidates that hop and the hops after it, and nothing before it.

**Short hops cut, long hops flow.** Overlap is chain-wide — 0.9 s by default —
so a 5 s hop asking for `join: continuous` spends a fifth of itself on the
airlock, and the node prints a note saying so.

### Directives

| axis | options |
|---|---|
| `join` | `continuous`, `match_cut`, `hard_cut` — ignored on shot 1, which has nothing to join to |
| `camera` | `hold`, `pan_follow`, `push_in`, `pull_back`, `orbit`, `handheld` |
| `framing` | `keep`, `wide`, `medium`, `close` |
| `pace` | `slow`, `steady`, `brisk` |
| `tail` | `ongoing` (default), `settle`, `hold` |

An unset axis emits nothing rather than asserting a default, so it costs no
tokens. Everything is phrased affirmatively, for the reason in
[The one law](#the-one-law).

`join=continuous` with a framing change and a held camera warns: with the camera
still, the only way to reach a new framing is a cut. Earn it on the move, or use
`framing: keep`. A camera move pointing the opposite way from the framing
(`push_in` with `wide`, `pull_back` with `close`) warns too. When `continuous`
and the camera *is* moving, the framing sentence compiles as a **landing** —
"The move settles into a close shot…" — so it does not fight the pin that still
holds the previous framing.

### Reference register

```json
{
  "refs": [
    {"tag": "hero_face",   "file": "cook_face.jpg",   "subject": 1, "retention": "fully_preserved"},
    {"tag": "hero_outfit", "file": "cook_apron.jpg",  "subject": 1, "retention": "partially_copy"},
    {"tag": "kitchen",     "file": "kitchen_wide.jpg", "retention": "reference", "mp": 0.3}
  ],
  "subjects": {
    "1": {"name": "the cook", "locked": "the same face, the same short dark hair"}
  }
}
```

`file` is a picture in `ComfyUI/input/h3_refs`, set by the rail. `tag` is what
you write in beats, and the node resolves it to the right `<Picture N>` **per
hop**, so pulling a still out of the middle no longer breaks every later
reference.

`subject` groups pictures per person. **This matters:** declaring every picture
as a photo of `<Subject 1>` makes the model render the *average* of two
different people.

`retention` says how much of a picture carries over — `fully_preserved` (face
and bone structure exactly), `partially_copy` (the garment and its cut),
`reference` (layout, surfaces and light, i.e. a place). Refs with a subject
default to `fully_preserved`; everything else defaults to `reference`.

`mp` caps one picture's pixel budget in megapixels. It is a **token dial, not a
quality one**: H3 turns every reference into pixel area ÷ 256 entries and
attends over all of them on every step of every hop, so a location plate costing
what a face costs is waste. A 0.3 MP place plate is ~1,170 tokens; a 2 MP
portrait is ~7,800.

> **The dial is inert at the default.** On `ref_image_size=match` every
> reference is first scaled down to the output's pixel area, and `mp` only ever
> caps *further* — so at 768p (~1.03 MP) the 1.5 and 2.0 settings change
> nothing. The real per-reference resolution control is `ref_image_size=max`
> **plus** `mp`, never `mp` on its own.

Add `"shots": [1, 2]` to a ref to keep it out of the hops it does not belong in.
On a continuation chain, omitting `shots` means **hop 1 only** — right for a
place plate, which beats the pin if it rides a hop set somewhere else. **Put
face plates on every hop:** a hop with no face reference comes back a different
person and no later hop recovers. A shot's own `refs` overrides all of this for
that one hop.

### Reference media: three clips, three voices

H3 takes 9 reference pictures, 3 reference videos and 3 standalone reference
audios. Pictures go through REFS; the clips and voices are in **MEDIA**.

| slot | cited as | notes |
|---|---|---|
| `reference clip` 1–3 | `<Video 1..3>` | motion/look plates the whole chain reads |
| `voice` 1–3 | `<Audio 1..3>` | timbre references for hop 1 |

**Numbering is dense.** Fill slots 1 and 3 and you get `<Video 1>` and
`<Video 2>` — there is no gap, so **clearing a slot renumbers the ones after
it**. Refer to media by what it is, not by its number.

Reference clips carry their own sound: each clip's audio track is decoded and
handed to the model alongside its picture. A clip with no usable audio track
passes silent and says so in the log.

**Trim them.** Every reference audio is attended on every step of every hop, and
H3 encodes the whole file, so a three-minute take is a large invisible tax on a
clip that only needed four seconds. Slots 2 and 3 share slot 1's `reference
video size`, which is a decode budget rather than a creative setting — and, as
[SWAP](#swap) found the hard way, an influence dial.

![The MEDIA tab with a reference clip loaded](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/reference-clip.png)

*A reference clip with its in/out scrubber, the description that tells the
encoder what the clip is **for**, and `video input size`.*

`master_audio_file` is the odd one out: one continuous take every hop lip-syncs
to, delivered as a passthrough with no VAE round trip. Empty is off and leaves
generated voice as before. The beat still needs the words in `<d>[English]
…</d>` — the lock supplies the timing, not the script — and a take shorter than
the chain is refused on the queue rather than discovered as a mute final hop.

An end of `0` on any scrubber means *to the end of the file*, so a longer
replacement still plays out rather than being cropped to the old one.

### Hop cache

`cache_hops=on` writes every rendered hop to ComfyUI's temp dir as lossless FFV1
video plus a float32 `.npy` waveform, evicting least-recently-used above
`cache_budget_gb`. The key **chains** — each hop's key includes the previous
hop's — because hops are causally dependent. So:

- edit shot 3 and re-queue → shots 1 and 2 load from cache, only 3 renders;
- edit shot 1 → all three re-render, which is correct, not a bug;
- change resolution, sampler, the checkpoint, a LoRA, or an attention setting →
  the whole chain re-renders;
- change `pin_to_qwen` or `overlap` → only hops 2+ re-render, because neither
  can reach hop 1. A restart hop is a start, so overlap does not reach it either;
- set `last_frame_guide=still` or `master_audio_file` → every hop re-renders
  (both reach hop 1). `before_restart` only moves the hop that gets the guide;
- change a reference picture → only the hops that picture rides re-render.

That last one is worth knowing about. The node cannot read the settings on your
LoRA and attention nodes, so it fingerprints what they *did* to the model —
which weight keys were patched, at what strengths, and the attention overrides —
plus the base model's class, dtype and parameter count. Two remaining gaps: two
different LoRAs touching exactly the same keys at exactly the same strengths,
and two different builds of the same architecture at the same dtype and
parameter count.

Set `locked: true` on a shot to pin it to its last render regardless.

### Reading a plan before you render it

`dry_run=on` compiles every hop's prompt and stops. No model, no sampler,
seconds instead of minutes. The compiled text comes out on `info`, and as a
readable page on `contact_sheet`. This is the only way to see what the text
encoder will actually receive — the directive layer, the continuation
scaffolding, the identity lock and the `<Picture N>` citations are all assembled
at render time.

![`lock` and the range button on the shot cards](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/shot-lock-range.png)

`render_through=N` stops after N hops; with `cache_hops=on`, 3 → 5 → 8 builds a
chain up in stages and only ever renders the new hops. The plan is not
truncated: shot 4 still knows it is shot 4 and keys the same way it will in the
full run.

`quality=draft` forces the 448p tier and 6 steps. Treat it as a **fidelity**
lever rather than a speed one — measured at ~42 s/hop against ~45 s/hop at 7
steps, so if you already render at 448p and 6–8 steps it saves almost nothing
and `dry_run` is the fast button. Draft earns its place when your final is
genuinely heavier, 768p at 14 steps.

`contact_sheet=on` adds an image on the fourth output: one row per hop, that
hop's first and last **delivered** frame side by side, its beat, its directives,
and what actually happened to it. On a chain of any length this is the fastest
way to find the hop that broke.

![Contact sheet](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/contact-sheet-vlog.png)

### Defaults

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
| tone_anchor_ref | `hop1`. `still` holds the photograph; needs a start image |
| pin_mech | `auto` |
| last_frame_guide | `off`. Recommended `before_restart` when the plan has a restart |
| master_audio_file | empty (generated voice) |
| quality | final |

Three shots at 10 s with a 0.9 s overlap is about 28 s of master.

### Nodes

**H3 Ref2VA Chain** — `images`, `audio`, `info`, `contact_sheet` out. Wire
`CreateVideo` + `SaveVideo` as in the example workflow, `info` to a Preview Text
node, and `contact_sheet` to a Save Image.

**H3 Chain Preview** — a passthrough panel for the IMAGE (and optionally AUDIO)
wire, placed between the chain and `CreateVideo`. Images and audio come out
unchanged, so adding or removing it changes no pixels. It shows the live sample,
the seam, a chain-wide progress bar, cache hit / seed / steps per hop, **which
pin mechanism each hop actually used**, and end-of-run A/V drift.

**H3 Tone Compensate** — `images` out. **For hand-built chains only.** It cannot
fix `H3 Ref2VA Chain`'s output: that node joins its hops internally and drops
each hop's first `overlap` frames at the seam, so the regenerated copies this
needs are already gone. Use the chain node's `tone_compensate` widget instead.

**H3 Seam Report** — `report` (STRING) + `chart` (IMAGE). **Ships wired on the
Starter canvas.** Wire the chain's `info` into it as well as `images`: with a
restart in the chain the hop lengths are no longer uniform, and the `hops` /
`overlap` widgets cannot describe that. It measures the brightness step at every
join, says whether each is invisible / marginal / visible, and totals the
chain's cumulative drift. A single reading includes whatever the scene did
across the cut, so treat one number as an upper bound.

**H3 Continuity State** — `continuity_state` (STRING) out. **Setting only**:
`setting_locked` / `setting_context` / `setting_mutable`. Characters belong in
`ref_plan`.

### Limits

- **Texture still ratchets on long chains.** Stay around **3–5 hops** until that
  is handled. Nine-hop chains have been measured; they converge.
- A workflow saved before 2026-08-28 loses its reference pictures. The old
  `ref_image_N` sockets carried tensors, so there is no filename to recover.
  The rail names each affected ref and asks you to pick its picture.
- The reference `desc` and subject `locked` text go to the encoder verbatim,
  every hop. A detail that is not in the photograph is **asked for**, not
  ignored. Describe what you actually wired.
- `HTCContinuityState` is setting only; the `characters_*` fields were removed
  because filling in both injected identity text twice.
- Each join hard-cuts video but crossfades audio ~40 ms, so A/V drifts ~40 ms
  per hop.
- A 22-frame pin clip is ~2 Qwen frames at 2 fps. It is a live-state hint, not a
  full previous-clip watch.
- A longer overlap does not fix continuity — it can pin whatever content happens
  to be in that longer tail.
- A 5 s hop drops the airlock on a continuous join; validate seams at 8 s or 15 s.

---

## Credits

From the **Sulphur** Discord:

- **@urlilgoddess** — the ten-run degradation study this release is built on,
  run and measured with her own instruments, and the sample frames in
  [Tested in public](#tested-in-public). Three of 2.0's behaviours exist because
  she measured that the reasoning behind the old ones was wrong.
- **@frankyi** — contributed the SWAP tab, which ships in 2.0 rebuilt from its
  own design rather than merged as-is.
- **@Sean3884** — author of **PromptMasterLD**, which this pack has borrowed
  from twice: its `song_lock` shaped `audio_lock.py`, and its edit laws shaped
  SWAP's four modes. Technique, not code — the distinctions and the discipline
  of stating an exclusion affirmatively, with the prose written fresh here.

Also: the tone estimator is ported from
[rkfg/ComfyUI-MiniMaxH3-ToneCompensate](https://github.com/rkfg/ComfyUI-MiniMaxH3-ToneCompensate)
(MIT, as is this pack), and the latent join comes from
[ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context).
The turbo stack in the shipped workflows is
[PlagueKind](https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes) and
[KJNodes](https://github.com/kijai/ComfyUI-KJNodes).

## Docs

[`CHANGELOG.md`](CHANGELOG.md) is what 2.0.0 contains, written for users.
[`PROMPTING.md`](PROMPTING.md) is the authoring guide. [`CLAUDE.md`](CLAUDE.md)
is the current map of the pack if you are changing it, and `docs/DEVLOG.md` is
the engineering log. `docs/HANDOVER_*.md`, root `HANDOVER.md` and `BETA_NOTES.md`
are historical session notes — do not take them as the state of this release.

---

## Changelog

Full notes in [`CHANGELOG.md`](CHANGELOG.md).

**2.0.0** — 2026-09-05. Full release. `master_audio_file`, one continuous take
every hop lip-syncs to. `last_frame_guide` (`before_restart` recommended).
`anchor: "restart"` as a real chain start, now writing its full length. `refs`
on a shot. The **SWAP** tab. Three reference-clip and three voice slots. A Lab
tone anchor with `tone_anchor_ref=still`. `pin_mech`. A hop cache that no longer
pickles, and an fp16 master buffer that halves the largest allocation in the
pack. New widgets were appended, so saved 1.1 graphs keep their values — but a
1.1 *hop cache* is fully invalidated on purpose, because the model fingerprint
now identifies the base checkpoint.

**1.1.x** — 2026-09-03/05. Tabbed editor with RUN pinned at the bottom.
`render_from` / `render_through` as a range. `retention_analysis` on every hop a
still rides, which fixes a reference pinned to any hop but the first being
rendered *as* the shot. Computed canvas — eleven aspect ratios on H3's 32 px
grid, so 16:9 at the top rung is 1344×768. Per-hop reference keys, so changing
one picture re-renders only the hops it rides. 1.1.1 passes every Core argument
by name, fixing `got multiple values for argument 'ref_image_size'` on builds
that order MiniMax H3's parameters differently.

**1.0.x** — 2026-09-02. First full release: the **WRITE** panel, the required
two-document schema, the reference rail owning each picture's pixel budget, and
the writer being told the hop length. Three plan lints were measured against
real renders, found to be warning about correct work, and narrowed. Patch
releases fixed the "no model is selected" bug on a fresh install and cleared
registry-scanner findings.

**0.4.x** — 2026-08-30. The five new dials reached the run panel; a dry run
stopped returning a 1×1 placeholder that libx264 cannot encode. The prompt pack
learned to show a place tag on both sides of its round trip. The hop cache
stopped shelling out to `ffmpeg` and encodes FFV1 in process through PyAV —
same format, bit-exact, and no external command for the registry scanner to flag.

**Renamed 2026-08-29** from `ComfyUI-H3-Ref-Chain`. The old node ids are still
registered as deprecated aliases, so every workflow saved before the rename
keeps loading. Nothing needs migrating.
