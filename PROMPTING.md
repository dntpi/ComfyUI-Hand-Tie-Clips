# Writing for Hand Tie Clips

Everything in this file was confirmed against renders, not reasoned from the
code. Where a rule exists because of something the model actually did, that is
said out loud — the reasons are more useful than the rules.

If you only read one section, read [The three laws](#the-three-laws).

---

## What you are actually writing

Two JSON documents, both living in widgets on the node, both editable as cards
in the panel or as raw JSON under the **JSON** tab.

| | |
|---|---|
| **`shot_plan`** | An ordered list of shots. **One shot per hop** — the shot count *is* the hop count. |
| **`ref_plan`** | The reference register: which pictures exist, what each one is for, who they are photographs *of*, and which hops each belongs on. |

The node writes a great deal that you do not: the identity lock, the citation of
the live frame carried over from the previous hop, the join sentence, and the
per-hop `<Picture N>` numbering. Your job is the **beat** — what happens this
hop — plus the register that tells the node who is in it.

---

## The three laws

### 1. The prompt is additive

Sampling runs at **cfg 1.0 with no negative branch**. There is nothing to push
against. Every concept you name is added to the conditioning and nothing can be
removed by mentioning it.

```
BAD   No cut, no camera shake, don't change the lighting.
GOOD  One unbroken take. The camera holds its position. The light stays as it is.
```

The bad version puts `cut`, `camera shake` and `change the lighting` in front of
the encoder, which is precisely the opposite of the intent. Write what the shot
*is* doing. This is also why the directive vocabulary is phrased affirmatively
throughout, and why an unset axis emits nothing at all rather than asserting a
default — an unset axis costs zero tokens.

**Whether this bites inside quoted dialogue has not been tested.** A line like
*'I did not think you would come'* is speech, not a stage direction, and the
model may well treat it as such — but nothing here was confirmed against a
render, so the shipped templates avoid negation in dialogue rather than assume
either answer. Your own lines are your call.

### 2. Never name the thing you want to end

```
BAD   The cook stops talking and stands still.
GOOD  The cook leans back against the counter with her lips closed, and lets her
      eyes move slowly across the room. The kitchen is quiet apart from the low
      hum of the refrigerator.
```

`stops talking` keeps her talking. Describe the state you want as **a pose plus
a sound**. This follows directly from law 1 but it is the single most common
mistake, so it gets its own rule.

**Silence has to be written as a sound.** H3 generates audio for the whole hop
whether or not you asked for any. Written as an absence, you get speech — the
model fills the track with the most likely thing. Written as room tone, a
refrigerator, a distant car, you get room tone.

### 3. Put state changes at the end of the *previous* shot

Every hop after the first opens by holding the frames it was handed, and the
audio pin carries the tail of the previous hop across the join. If shot 2 ends
mid-sentence, **no wording in shot 3 will make shot 3 start quiet.**

```
shot 2:  The cook sets the knife down, turns toward the window, and finishes her
         last word as she looks out.
shot 3:  The cook leans back against the counter with her lips closed...
```

To be silent, still, or somewhere else in a shot, **arrive there before the
previous shot ends.**

### The other half of law 3: a beat must survive an over-delivered hop

Law 3 says put a state change at the end of the *previous* shot. Its mirror is
just as load-bearing: **never write a beat that assumes where the previous hop
stopped.** A hop routinely does more than you asked. Give it a walk that was
meant to be a first step and it will finish the whole walk — seven steps of
turbo sampling has no reason to stop halfway.

If the next beat then instructs an action its own live frame has already carried
out, there is exactly one way for the model to satisfy it: reset the scene and
do it again. That reads as a hard cut about a second and a half *into* the hop,
not at the seam, because `_assemble_next` holds the incoming frames for a short
beat first.

Observed on a six-hop chain. Shot 3 asked for *"takes a first slow step along
the counter"* and delivered the entire walk to the window in a tight close.
Shot 4 was then handed *"carries the bowl the length of the counter to the
window and sets it down on the sill"* — already true. It cut at 21.8 s.

Two fixes, and use both:

- **Give one hop the whole movement.** Splitting a walk across two hops is what
  creates the impossible second instruction.
- **Write the next beat so it is true from either ending.** *"She reaches the
  window and sets the bowl down"* is satisfiable whether she arrived last hop or
  is still arriving. *"She takes up the bowl again"* asserts no location for the
  bowl, so a hop that never saw it set down still has something legal to do.

The rewritten chain rendered 39 s with no cut detectable at a 0.08 scene
threshold.

### Dialogue propagates, and `tail: ongoing` compounds it

Observed on a six-hop chain: shot 1 ended on its spoken line, and the character
**went on talking through all five remaining hops**, none of which had any
dialogue written.

Two things caused it together. The audio pin carries the previous hop's tail, so
the last second of hop 1 — speech — became the opening of hop 2. And
`tail: ongoing` closes the prompt with *action is still underway*, which the
model satisfied with the action it could hear.

The fix is not to write "she is silent" in the later shots. It is to **land the
line early in the hop and leave a non-verbal action running into the seam**:

```
BAD   ...looks up from the chopping board and says, 'You are early.'
      tail: ongoing

GOOD  ...looks up from the chopping board and says, 'You are early. I have
      barely started.' She turns back to the board and goes on slicing, the
      knife tapping steadily against the wood.
      tail: ongoing
```

The knife is what is still underway when the frames stop, so that is what the
pin carries. Then **give every dialogue-free hop a sound bed of its own** —
footsteps, a refrigerator, rain on glass — or the audio has nowhere to go but
back to speech.

---

## Templates

The **Templates** button in the SCRIPT header appends ready-made patterns —
*Dialogue, held*, *Cross the room*, *Leave for an unseen space*, *Return to the
room*, *Quiet close*. They append rather than replace, so a chain is built by
stacking them.

Each one exists to demonstrate a rule on this page in a form you can edit rather
than read, and each carries a note about how to schedule references around it.
None of them contains an `@tag`: a template that named a reference you had not
created would turn one click into a run-time error.

## Writing a beat

**Shot 1 is the whole opening.** Establish the person, the place, the light, what
they are doing. **Every later shot is only the new beat** — do not re-describe
the face, the clothes, or the room. The photographs, the register and the pin
already carry all three, and re-asserting them competes with the pin rather than
reinforcing it.

```
shot 1:  @hero_face stands at the counter in @kitchen, an apron over a grey
         t-shirt. She looks up from the chopping board and says one short line,
         then turns back to the board and goes on slicing, the knife tapping
         against the wood.
shot 2:  She sets the knife down and walks the length of the counter to the
         window, looking out at the street. Her steps are soft on the tiles and
         the refrigerator hums behind her.
```

Shot 2 says nothing about her face, her apron or the kitchen. It does not need
to. It *does* name a sound, because shot 2 has no dialogue and the audio needs
somewhere to go that is not the previous hop's speech.

### Point the action line at the reference you want used

Describing a reference in the register is **not enough**. The beat is what drives
the frame. Write the tag into the action:

```
The cook stands at the counter in @kitchen, looks up from the chopping board,
and speaks one short line to someone off-frame.
```

`@kitchen` resolves to whatever `<Picture N>` that reference happens to be on
this hop, so adding, removing or rescheduling references never breaks your text.

A **person** tag behaves differently on each side of the first hop, and the node
handles the difference for you. On hop 1 `@hero_face` becomes `<Subject 1>`,
bound by the `subject_definitions:` block that only hop 1 carries. From hop 2 on
that block is gone, so an ordinal would dangle — and the node substitutes the
subject's `name` instead:

```
hop 1:  <Subject 1> stands at the counter in <Picture 2>...
hop 4:  The cook walks down a narrow hallway...
```

"The cook" binds to the identity sentence `continuity_line` puts on every
continuation hop. **So give every subject a `name`** — without one the node
falls back to `<Subject N>`, which points at nothing on that hop. Capitalisation
is handled at sentence starts.

Phrase a place as **a place that is depicted**, not as a container to be placed
inside — "in @kitchen", not "inside the image of @kitchen". The latter can
produce a literal composite of the photograph.

### Dialogue

Write the line, in quotes, inside the beat:

```
@hero_face looks up from the chopping board and says, "You are early. I have
barely started." She turns back to the board and goes on slicing.
```

Land the line in the **middle** of the hop, never at its end — see
[Dialogue propagates](#dialogue-propagates-and-tail-ongoing-compounds-it).

Keep it to roughly one line per 5–7 seconds of hop. A long speech in a short hop
gets truncated mid-word, and that truncation is then pinned into the next hop's
audio.

If you are pasting a plan into the **JSON** tab rather than typing into the
cards, single quotes around dialogue survive the round trip more reliably than
escaped double quotes, and read identically to the encoder.

### Ambience: narrowband and specific

After shot 1 the official soundscape fields are stripped and ambience is plain
prose, so the exact words carry all the weight.

| | |
|---|---|
| `faint street noise through the window` | broadband — renders as a five-second hiss |
| `the low hum of the refrigerator` | narrowband — renders as a refrigerator |
| `a single click from the refrigerator, then stillness` | a discrete event — the fallback when a continuous bed still misbehaves |

---

## Directives

Five axes, all optional. Set on a shot as `"directives": {...}`.

| axis | values | notes |
|---|---|---|
| `join` | `continuous`, `match_cut`, `hard_cut` | **Ignored on shot 1**, which has nothing to join to. |
| `camera` | `hold`, `pan_follow`, `push_in`, `pull_back`, `orbit`, `handheld` | |
| `framing` | `keep`, `wide`, `medium`, `close` | |
| `pace` | `slow`, `steady`, `brisk` | |
| `tail` | `ongoing`, `settle`, `hold` | Defaults to `ongoing`. The only axis with a default. |

They compile in that order, `join` first, because `join` describes how this hop
meets the previous one.

### `tail` is not decoration — set it on your last shot

This axis exists because of an observed failure. A beat whose action completes
before the frames run out leaves the model with nothing to render, and it settles
onto its strongest remaining conditioning — **the identity photograph**. The last
few seconds of a chain would cut to the reference still.

`ongoing` is the default for exactly that reason: it gives the tail somewhere to
go that is not the reference. But on your **final** shot, `ongoing` tells the
model action is still underway at the final frame, and it will invent something
to satisfy that — a stray gesture, or a stray line of dialogue in the closing
second. Use `settle` or `hold` there.

### Two combinations the node will warn about

**`join: continuous` + a framing change + `camera: hold`.** A framing change asks
the audience to be somewhere new. With the camera still, the only way to get
there is a cut — so the join and the framing are asking for opposite things and
the model picks one. Earn the framing on the move (`push_in`, `pull_back`,
`pan_follow`) or set `framing: keep`.

**`push_in` + `wide`, or `pull_back` + `close`.** The move points the opposite way
from the destination. Physically contradictory at any join value.

**`tail: settle` or `tail: hold`, followed by a beat that opens mid-action.**
This is the over-delivery defect of law 3, caught mechanically. `settle` brings
the subject to rest and `hold` freezes the frame — so a next beat that opens
with *"She continues…"*, *"Walking to the window…"*, *"Mid-sentence…"* is asking
the model to carry on something the hop before it was told to stop. One of the
two instructions loses, and which one is a coin flip.

This is the class of defect that produced the single hard cut in the
114-second reference chain. Both shots were individually well-formed; every
other check passed. Only the join between them was wrong. Fix it at either end:
set the previous shot's `tail` to `ongoing`, or rewrite the opening so it also
reads from a standstill.

The check only catches beats that *say* they are continuing. A beat can open
mid-action without any of those words and it will not fire — the rule in your
head still has to be the real one.

All three are warnings, not errors — they are legitimate things to want. They just
rarely read the way you meant.

> When `join: continuous` and the camera *is* moving, the framing sentence
> compiles as a **landing** ("The move settles into a close shot…") rather than
> as the shot's opening state, so it does not fight the pin that still holds the
> previous framing. You do not have to do anything to get this; it is worth
> knowing it happens.

---

## The reference register

```json
{
  "refs": [
    {"tag": "hero_face",   "file": "cook_face.jpg",    "subject": 1,
     "retention": "fully_preserved", "shots": [1, 4],
     "desc": "head-and-shoulders, even light"},
    {"tag": "hero_outfit", "file": "cook_apron.jpg",   "subject": 1,
     "retention": "partially_copy",  "shots": [1]},
    {"tag": "kitchen",     "file": "kitchen_wide.jpg",
     "retention": "reference",       "shots": [1, 2, 3, 6]}
  ],
  "subjects": {
    "1": {
      "name": "the cook",
      "locked": "the same face, the same short dark hair, the same silver stud earrings",
      "context": "the apron stays tied over the grey t-shirt"
    }
  }
}
```

### Fields

| ref field | |
|---|---|
| `tag` | **Required.** Short, stable, what you write in beats as `@tag`. Must be unique. |
| `file` | A basename in `ComfyUI/input/h3_refs`. Set for you when you drop a picture on the rail. Never a path. |
| `subject` | Integer ≥ 1. Groups pictures **of the same person**. |
| `retention` | `fully_preserved`, `partially_copy`, `reference`. Defaults to `fully_preserved` when `subject` is set, `reference` otherwise. |
| `desc` | Free text about the picture. |
| `shots` | 1-based hop numbers this picture rides on. See below — this is the important one. |

| retention | what carries over |
|---|---|
| `fully_preserved` | face, bone structure and hairstyle, exactly |
| `partially_copy` | the garment and its cut, moving naturally with the body |
| `reference` | the layout, surfaces and light, i.e. a place |

| subject field | |
|---|---|
| `name` | What to call them in prose. |
| `locked` | What must not change. **This is what survives when the photograph is off the hop.** |
| `context` | Situational state that should persist — wardrobe, what they are holding. |

At most **9 pictures on any one hop**.

### `subject` is not optional decoration

Declaring two photographs of two *different* people as the same subject makes
the model render the **average of two faces**. Group photos per person, always.

Conversely, a subject block that no ref claims is an error, not a warning: it
would ride every hop describing someone who never appears, and at cfg 1.0 that
is purely additive noise.

Give every subject a `locked`. Pictures alone put a face in front of the encoder;
`locked` is what carries the identity across a seam where the picture is absent.

> This was **not true before 2026-08-29**. `locked` and `context` reached hop 1
> only, and a hop scheduled with no references carried no identity text at all
> — which is exactly how a six-hop test lost its character on the one hop that
> had no pictures. They now ride every hop, phrased with no `<Picture N>` or
> `<Subject N>` citation so they cannot send the encoder back to the plates.

### Name a colour, or it drifts

`locked` and `context` ride every hop, but they only protect what they actually
describe. A noun with no adjective is exactly as unanchored as a `<Subject N>`
with no antecedent — each hop is an independent encode, and *"the bowl"* on
hop 4 gives that encode nothing to bind to.

Two renders of the same chain settled it. In the first, three hops said only
*"the bowl"*; it came back stainless steel on hop 4 and a different ceramic one
on hop 6. Adding the colour to every mention **and** stating it as a property in
`context` — *"the bowl she is using is white porcelain"* — held one white bowl
through all six hops of the re-run.

The second render then made the rule sharper than a designed test would have.
This `context` line ran on every hop:

> the apron stays tied over the **grey** t-shirt

The t-shirt has its colour named and is grey in all six hops. The apron is one
clause away in the same sentence, has no colour, and had turned denim blue by
hop 6. One sentence, one variable.

So:

- Put persistent properties in `context` as **properties, never locations**.
  *"the bowl is white porcelain"* is true wherever she puts it; *"the bowl stays
  in her hands"* becomes additive text fighting the beat the moment she sets it
  down.
- Repeat the adjective in the beats too. *"the white bowl"*, every time.
- This applies to anything the register does not photograph on that hop. A ref
  with `shots: [1]` stops defending its garment at hop 2; from there the only
  thing holding it is the words.

### `shots` is how you schedule references — and the default will surprise you

> **On a continuation chain, a reference with no `shots` list rides hop 1 only.**

That is deliberate for **place** plates: a room still riding a hop set
somewhere else beats the pin, because the model holds a crisp well-lit picture
of one room and a noisy carried-over frame of another, and it follows the still.
List every hop a picture should appear on, explicitly.

> **For a face plate, schedule it on every hop.** This page used to warn that a
> face plate riding a later hop beats the pin the same way. A six-hop render
> contradicted it flatly: hop 4 carried the face plate through a walking medium
> shot in a space the plate had never seen — it was photographed in a different
> kitchen — and held cleanly, while hop 5, scheduled with no refs at all, came
> back a **different person**, and hop 6 never recovered.
>
> Identity drift is permanent. The room came back the instant a place plate rode
> hop 6; nothing brought the face back, because no face plate rode anything.
> `locked` holds a face that is already right. Only a plate rebuilds one that is
> gone, so do not let it go. Putting the face on `[1,2,3,4,5,6]` fixed it and
> cost nothing visible.

The useful consequence: you can take references *away* deliberately. A hop with
no room reference is a hop the model must invent a room for, which is exactly
what you want when the character walks somewhere the register has never seen.

---

## A worked six-hop plan

This is `workflows/HandTieClips_Showcase.json`, and it is built to be hard.

| hop | beat | refs active | what it tests |
|---|---|---|---|
| 1 | kitchen, dialogue | face, outfit, kitchen | establishment |
| 2 | walks to the window | kitchen | lateral movement, held identity |
| 3 | exits through the doorway | kitchen | leaving the referenced space |
| 4 | hallway | face | **an unseen space** — no room reference exists for it |
| 5 | dialogue at the far window | **none** | **identity, wardrobe and voice on the pin + `locked` alone** |
| 6 | returns to the kitchen | kitchen | re-entry after three hops away, on a `match_cut` |

Hop 6 joins on `match_cut`, not `continuous`. Asking for one unbroken take
between two different rooms is asking the impossible, and the first run of this
plan got what it deserved: the hallway morphed into the kitchen mid-turn. A walk
through a doorway *is* a cut on a matched movement.

Hop 5 is the point of the exercise. If the cook is the same person, in the same
apron, with the same voice, **with no picture in front of the encoder at all**,
the register is doing its job. Hop 6 then asks whether the room survived three
hops of absence.

The face plate returns on hop 4 and not on 5 on purpose: hop 4 is where the
character enters a space the register cannot describe, which is where identity
drift is most likely to start, and re-asserting the face there is cheaper than
recovering from drift on 5 and 6.

---

## Duration

The label on the widget and the frames the model actually renders agree to a
tenth of a second, because every offered value satisfies the model's own frame
alignment (`n % 17 == 5` at 24 fps).

| label | frames |
|---|---|
| `5 s` | 124 |
| `7 s` | 175 |
| `8 s` | 192 |
| `10 s` | 243 |
| `15 s` | 362 |

A shot may override the chain with `"duration": "7 s"`, using the same labels.

---

## Read it before you render it

Set `dry_run=on` and queue. Every hop's prompt compiles and the node stops —
no model, no sampler, a few seconds. The text comes out on `info`, and the same
thing comes out on `contact_sheet` as one readable page.

Do this before any long chain. What you wrote in a beat is not what the encoder
receives: the directive layer, the continuation scaffolding, the identity lock
and the `<Picture N>` citations are all assembled at render time. A dry run is
the only way to read the real thing before paying for it, and it is where the
warnings above show up too — the whole plan is checked on the way through.

Two related dials:

- **`render_through=N`** stops after N hops. With `cache_hops=on`, 3 → 5 → 8
  builds a chain up in stages and only ever renders the new hops. The plan is
  not truncated; shot 4 still keys exactly as it will in the full run.
- **`quality=draft`** forces 0.30 MP and 6 steps. Enough to read blocking,
  camera and whether a join lands. Both values are in the cache key, so a draft
  never overwrites the final it stands in for.

And after the render, `contact_sheet=on` gives you one row per hop — first and
last delivered frame, beat, directives, seed, cache hit, what the tone
correction did. On an eight-hop chain it is the fastest way to find the hop
that broke.

## Re-rolling one hop

Eight hops in, hop 6 is wrong and the other seven are fine. `cache_hops` is how
you fix 6 without re-rendering 1 through 5.

Turn it **on before the first run of a plan** -- it is `off` by default, and a
hop that was never cached cannot be reused. It needs nothing installed --
the lossless FFV1 encode runs in process through PyAV, which ComfyUI already
ships.

### Editing a hop invalidates that hop and everything after it

Every hop is rendered from the previous hop's tail, so the cache key **chains**:
hop N's key mixes in hop N-1's key. Rewrite shot 5's beat and hops 5, 6, 7 and 8
re-render, while 1 to 4 come straight off disk.

That is correct rather than a limitation. Hop 6 was rendered *from* hop 5. If
hop 5 changes, the old hop 6 is a continuation of a frame that no longer exists.

The working rule: **edit the earliest shot you are unhappy with, and expect
everything after it to re-render.** Fix front to back and you pay for each hop
once. Fix back to front and you pay repeatedly.

The cache is lossless 16-bit, chosen so that a resumed chain is not a different
render from an uninterrupted one -- a cached hop's last frame becomes the next
hop's pin, so an 8-bit round trip would have let the cache change the output.

### What re-renders the whole chain

Some inputs are not per-hop. Touching any of these invalidates every cached hop
at once:

- the canvas -- `resolution`, `aspect`, `overlap`
- sampling -- `sampler_name`, `scheduler`, `shift_video`, `shift_audio`
- `ref_image_size`, `pin_to_qwen`
- **any reference picture**, the voice, the reference clip, the first-frame pin.
  These are keyed on actual pixels, so re-saving a file with a different crop
  counts even when the filename is identical
- **the model wire** -- a different LoRA stack or attention path is a different
  fingerprint, because a hop rendered under different LoRAs is not that hop

So swapping one reference photo re-renders everything. That is the right answer,
since every hop carried that photo -- but it does not look right from the
outside, and it is the most common reason the cache appears to have stopped
working.

### `locked` freezes a hop you are happy with

A shot with `"locked": true` reuses its last render **even when its inputs
changed**. The ordinary cache says *the same inputs give you the same pixels
back*; `locked` says *give me those pixels regardless*.

Use it when a hop came out better than its prompt deserves and you want to hold
that exact take while you rewrite the hops around it:

```json
{"id": "s3", "beat": "...", "locked": true, "directives": {"join": "continuous"}}
```

**Give a locked shot an `id`.** The lock is stored against that name rather than
against the content key -- that is what lets it survive the edit that would
otherwise have invalidated it. Without an `id` it falls back to `shot3`, which
moves if you reorder the plan.

Locking a hop does not stop the hops after it re-rendering. They are still
continuations of it, and if you edited them they still change.

> **Two unrelated fields are called `locked`.** `subjects.N.locked` in the
> *reference register* is identity text that rides every hop -- the face that
> must not drift. `shots[].locked` in the *shot plan* is this cache pin. Same
> word, different blocks, unrelated jobs.

## When it goes wrong

| symptom | cause | fix |
|---|---|---|
| The clip cuts to the reference photo in its last seconds | The beat finished before the frames did, and the model settled onto its strongest remaining conditioning | Set `tail`, and give the beat enough to do |
| A stray gesture or line in the closing second | `tail: ongoing` on the final shot | `settle` or `hold` |
| She keeps talking after you asked for quiet | You named the ending (`stops talking`) — law 2 | Pose plus a sound |
| Dialogue continues into hops that have none written | The hop before ended on speech, and the audio pin carried it | Land the line early; leave a non-verbal action running into the seam; give every quiet hop its own sound bed |
| A character walks between two rooms and one morphs into the other | `join: continuous` across a real location change | `match_cut` |
| The film gets steadily dimmer over a long chain, but no single join looks wrong | Each hop darkens across its own frames and hands the darker tail on; seam correction cannot see this | `tone_compensate=anchor`. Measure it first with **H3 Seam Report** — a large one-signed *sum of steps* is the signature |
| A hop ignores what you told it and carries on the previous action | The previous shot's `tail` promised a stop your beat then overrode | Over-delivery: change the `tail`, or rewrite the opening |
| A deliberately dark scene keeps getting brightened back up | `tone_compensate=anchor` cannot tell intent from drift | `"tone": "rebase"` on the first shot of the darker scene |
| Silence renders as speech | Silence written as an absence | Name room tone, a fridge, a distant car |
| Ambience is a five-second hiss | Broadband wording | Narrowband, or a single discrete event |
| Two characters' faces merge | Both declared as the same `subject` | One subject number per person |
| The face becomes a different person partway through | A hop scheduled with no face plate. `locked` holds a face that is still right; only a plate rebuilds one that is gone, and the drift never self-corrects | Put the face ref on every hop |
| A hard cut ~1.5 s into a hop, mid-scene | The previous hop over-delivered, so this beat instructs something its own live frame already did. The only way to obey is to reset the scene | Give one hop the whole movement; write the next beat so it is true from either ending |
| An animated or stylised plan renders photoreal, and hop 1 most of all | The node prepends an establishing line to hop 1 and its default asserts **live action**, so "Live-action, natural light" was added ahead of your style. Fixed in 0.3.3: the default is dropped when shot 1 names a medium | Name the medium in shot 1's **first sentence**, or clear the `establish` widget |
| The film gets steadily darker hop after hop | Luminance drifts like colour, in one direction, and nothing pushes back. Measured at 72 -> 11 mean luma across six hops of a night chain | Restate the light as a positive property in every beat: "the clearing reads bright under the full moon, pale stone, open detail in the shadows" |
| A location introduced mid-plan drifts | It has no place plate. Tightening a plate to its own shots does not mean the next location goes without one | Add a plate for it, riding the hop it arrives and every hop after. `check_place_handoff` warns when the plan ends with no plate riding |
| A prop or garment changes material or colour | It is named with no adjective, so each hop's encode is free to invent one | Repeat the adjective in every beat, and state it as a property in `context` |
| A continuous join reads as a cut | Framing change with `camera: hold` | Earn it on the move, or `framing: keep` |
| The run stops naming a reference | That row's picture is not in `h3_refs`. A named file that is not on disk is fatal -- rendering the chain without it is never what was meant | Drop the file on the row in the REFERENCES rail, or clear its picture to run without it |
| A reference has no effect on some hop | Its `shots` list leaves that hop out. This is silent, not an error -- it is a legitimate schedule | List every hop the picture should ride |
| Every hop re-renders after a tiny edit | You changed something chain-wide -- a reference picture, the sampler, the resolution, the LoRA stack. Those are not per-hop | Expected. See [Re-rolling one hop](#re-rolling-one-hop) |
| A re-roll of hop 6 also re-rendered 7 and 8 | The cache key chains; 7 and 8 were continuations of the old 6 | Expected. Edit the earliest hop you dislike and work forward |
| A hop you liked changed anyway | Its inputs moved and it was not locked | `"locked": true` on that shot, plus a stable `id` |
| A pasted plan is rejected as invalid JSON | Usually escaped double quotes mangled in transit | Use single quotes around dialogue |

---

## Having a model write the plan

The whole vocabulary, schema and rule set fits in one prompt. `prompt_pack/`
holds a copy-paste system prompt that turns a plain description of a scene into
a `shot_plan` and `ref_plan` this node accepts:

- **[`prompt_pack/AUTHORING_PROMPT.md`](prompt_pack/AUTHORING_PROMPT.md)** — paste
  into any chat model, describe your scene, paste the two JSON blocks back into
  the node.
- **[`prompt_pack/SCHEMA.json`](prompt_pack/SCHEMA.json)** — the machine-readable
  version, for anyone wiring this into their own tooling.

The node still validates everything, so a model that gets it wrong is caught
rather than obeyed.

**Treat a written plan as a strong draft, not a finished one.** It will get the
structure right -- tags, retention tiers, schedules, directives, a beat per hop
that reads as one continuous take -- and that is the part that is tedious to
write by hand and easy to get subtly wrong. What it will not reliably get right
is judgement about your particular pictures and your particular scene. Two
things to read every time before you queue:

- **The `desc` on each ref.** It is written from the photograph and it goes into
  the prompt verbatim as what to carry over, so a wrong one is actively
  harmful -- a wardrobe plate described as "a dark top" when the garment is
  white will fight the picture it came from. Vision quality varies a lot
  between models here.
- **The spoken words in each beat.** Speech runs about 2.5 words a second, so a
  10 s hop of someone talking needs roughly 25 words inside the quotes. Models
  write one short line and stop. The panel now warns with the arithmetic, and
  the fix is to write the extra sentences yourself.

Everything the panel flags is a warning rather than a rejection, because all of
it is legitimate to want on purpose. Read the warnings, fix what you meant
differently, and queue.
