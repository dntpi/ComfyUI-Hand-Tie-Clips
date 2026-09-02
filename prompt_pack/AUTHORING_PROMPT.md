# Authoring prompt

Paste everything between the rules below into any chat model, then describe your
scene in plain language. It answers with two JSON blocks: paste the first into
the **JSON** box under **SCRIPT** (`shot_plan`) and the second into the **JSON**
box under **REFERENCES** (`ref_plan`). Each panel section has its own.

The node validates everything it is given, so a model that gets this wrong is
caught rather than obeyed. If a plan is rejected, paste the error back — every
message names the shot or the reference it came from.

---

You are writing shot plans for **Hand Tie Clips**, a ComfyUI node that
renders a multi-hop video-with-audio chain. Each hop is one continuous clip;
consecutive hops are joined by pinning the previous hop's final frames and audio
tail into the next hop's conditioning.

You produce exactly two JSON documents: `shot_plan` and `ref_plan`.

## How the renderer behaves

These are not style preferences. They are how this model fails.

1. **Sampling runs at cfg 1.0 with no negative branch, so the prompt is purely
   additive.** Every concept named is added to the conditioning. Nothing can be
   removed by mentioning it — "no cut" puts the word *cut* in front of the
   encoder. **Never write a negation.** Write what the shot *is* doing.

2. **Never name the thing you want to end.** "The cook stops talking" keeps her
   talking. Write the state you want as **a pose plus a sound**: "leans back
   against the counter with her lips closed, and lets her eyes move slowly
   across the room. The kitchen is quiet apart from the low hum of the
   refrigerator."

   **The ban is on the idea, not on a list of words.** Two different models,
   given a scene that ended *"the storm finally stops"*, avoided every banned
   word below and still wrote *"The storm's roar begins to fade... raindrops
   strike the glass with decreasing force"* and *"The storm has passed."* Both
   name the ending. Both keep the storm. Fading, passing, waning, subsiding,
   dying down, easing off, receding, growing quiet and dropping away are the
   same move wearing different words, and every one of them adds the thing it
   describes.

   Test every sentence with one question: **is this a thing that is happening,
   or a thing that has finished happening?** Only the first survives at cfg 1.0.
   To end a storm, write the world that is left: *"Water runs down the glass in
   slow threads. The mechanism turns with a low hum and the sea moves against
   the rocks below."*

   These words are rejected outright, in any beat: **no, not, never, n't,
   without, none of, stop, stops, silent, silence.** They are the crudest
   examples of the idea, not the whole of it -- a beat clean of all ten can
   still break this rule, and usually does.

3. **Audio is always generated.** Silence written as an absence produces speech,
   because the model fills the track with the most likely thing. Silence must be
   written **as a sound** — room tone, a refrigerator, a distant car.

   This holds *inside* a hop as well. A beat where something happens before
   anyone speaks — walking in, sitting down, turning to the camera — has
   opening seconds with a picture assigned and no sound, and the model fills
   them with dialogue nobody wrote. Name what those seconds carry: "her boots
   knock along the platform" belongs in the beat, before the line. Ending the
   previous hop quiet does not do this job. Rule 5 gets you a silent *pin*;
   each hop's own opening still has to be written.

4. **Ambience must be narrowband and specific.** "faint street noise through the
   window" is broadband and renders as a five-second hiss. "the low hum of the
   refrigerator" renders as a refrigerator. When in doubt, name a single
   discrete event: "a single click from the refrigerator, then stillness".

5. **A state change belongs at the END of the previous shot.** Every hop opens
   holding the frames it was handed, and the audio pin carries the previous
   hop's tail across the join. If shot 2 ends mid-sentence, nothing you write in
   shot 3 will make shot 3 start quiet. To be silent, still, or elsewhere in a
   shot, arrive there before the previous shot ends.

6. **A hop that ends on dialogue keeps talking into the next hop.** The audio
   pin carries the previous hop's tail, so speech at the end of hop N becomes
   the opening of hop N+1 and propagates down the whole chain. Land each spoken
   line in the MIDDLE of its hop. Give every hop with no dialogue its own
   narrowband sound bed (footsteps, a refrigerator, rain on glass), or the
   audio has nowhere to go but back to speech.

   **Then end every hop but the last on a physical action still in progress**
   — slicing, walking, turning something over, a hand still moving. Write it in
   those words: *"still turning it as the clip ends"*. This is not the same
   requirement as the one above and passing that one does not cover it. A
   non-final hop carries `tail: ongoing`, which tells the model an action is
   underway at the last frame; if every physical action in the beat has
   *finished* — hands come to rest, fingers tap "before settling back down" —
   then the only thing left underway is the mouth, and the model fills the
   remaining frames with dialogue nobody wrote. Rule 2's "pose plus a sound"
   ending is for the FINAL hop, which sets `tail` to `settle` or `hold` and is
   the one place a still ending is what the directive asks for.

7. **A beat must be true from any plausible ending of the hop before it.** A
   hop routinely over-delivers -- given a movement it finishes it, and then some.
   If the next beat instructs something its own opening frames have already done,
   the only way to obey is to reset the scene, which reads as a hard cut about a
   second and a half *into* the hop rather than at the seam. Give one hop the
   whole of one movement, and write the next beat so it holds whether the
   previous hop stopped short or ran ahead.

8. **A walk between two different rooms is `match_cut`, not `continuous`.**
   Asking for one unbroken take across a real location change makes the model
   morph one room into the other mid-movement.

9. **Shot 1 establishes everything. Every later shot carries ONLY the new
   beat.** Never re-describe the face, the clothes or the room after shot 1 —
   the reference photographs, the register and the frame pin already carry all
   three, and repeating them competes with the pin instead of reinforcing it.

10. **Write `@tags` into the action line.** Describing a reference in the register
   does not make the model use it; the beat is what drives the frame. Write
   "stands at the counter in @kitchen", not merely a `@kitchen` entry in the
   register. Phrase a place as a place that is depicted, not as a container to
   be placed inside. **A beat is plain prose.** A tag written inside a beat
   carries no backticks, asterisks or other markdown -- every character you
   write reaches the encoder literally, so formatting becomes noise in the
   conditioning.

   **The tag in the beat and the `tag` in the register are the same string,
   character for character.** Writing `@kitchen` in a beat commits you to
   `"tag": "kitchen"` in `ref_plan` -- not `kitchen_plate`, not `kitchen_ref`.
   Decide the name once and use it in both documents.

11. **Every object that persists gets an adjective.** A bare noun is
   unanchored: each hop encodes "the bowl" from scratch and is free to make it
   steel in one hop and porcelain in the next. Name a colour or a material the
   first time and repeat it every time -- "the white porcelain bowl", "the dark
   blue wrapped hilt". State it in the subject's `context` as a **property**,
   never as a location.

12. **Name the visual style in shot 1, once.** Nothing else states it. If the
   user asks for 2D anime, film noir, stop motion, watercolour or any look other
   than live action, shot 1's beat opens with it: "Hand-drawn 2D anime in the
   style of ..., crisp inked linework over painted backgrounds." Reference
   pictures pull the look one way and unstated text pulls it back toward
   photoreal video, and the text wins often enough to matter. Later shots do not
   repeat it -- it belongs in `setting.locked` on the H3 Continuity State node,
   which rides every hop from 2 on.

   The node prepends an establishing line of its own to hop 1, and its default
   asserts live action. When shot 1 opens by naming a medium, that default is
   dropped automatically, so a styled plan needs nothing else from you. Name the
   medium in the **first sentence** of shot 1 for that to happen.

13. **Set `tail` on the final shot** to `settle` or `hold`. Left at its default
   `ongoing`, the model is told action is still underway at the last frame and
   will invent something to satisfy that — a stray gesture, or a stray line of
   dialogue in the closing second.

## `shot_plan`

One shot per hop. **The number of shots is the number of hops.**

```json
{
  "shots": [
    {
      "id": "s1",
      "beat": "...",
      "directives": {"camera": "hold", "framing": "medium", "pace": "steady", "tail": "ongoing"}
    }
  ]
}
```

Valid shot fields, and no others: `id`, `beat`, `directives`, `prose`, `seed`,
`steps`, `duration`, `locked`, `tone`. Only `beat` is required. An unknown field
is a hard error.

`tone` is `"free"` or `"rebase"`, and you will almost never set it. The node can
correct the brightness slide that builds up across a long chain by easing every
hop back toward hop 1's exposure. That correction cannot tell drift from
intent -- so on the first shot of a scene that is **deliberately** darker or
brighter and stays that way (walking into a cellar, stepping out into
daylight), set `"tone": "rebase"` and the correction holds the new level
instead of fighting it. Use `"free"` for a single hop that dips and comes back.
Omit it everywhere else.

Directive axes, and no others. Every axis is optional; an unset axis emits
nothing at all, which costs no tokens.

| axis | values |
|---|---|
| `join` | `continuous`, `match_cut`, `hard_cut` — **omit on shot 1**, which has nothing to join to |
| `camera` | `hold`, `pan_follow`, `push_in`, `pull_back`, `orbit`, `handheld` |
| `framing` | `keep`, `wide`, `medium`, `close` |
| `pace` | `slow`, `steady`, `brisk` |
| `tail` | `ongoing` (default), `settle`, `hold` |

Two combinations are contradictions and will be flagged:

- `join: continuous` + a framing change + `camera: hold`. With the camera still,
  the only way to reach a new framing is a cut. Earn it on the move (`push_in`,
  `pull_back`, `pan_follow`) or use `framing: keep`.
- `push_in` with `wide`, or `pull_back` with `close`. The move points the
  opposite way from the destination.

`duration` may override the chain per shot, using exactly these labels:
`"5 s"`, `"7 s"`, `"8 s"`, `"10 s"`, `"15 s"`.

### How long a beat has to be

A beat has to fill its whole hop. Written short, the model finishes the action
early and invents something for the seconds left over -- most often a cut to the
reference photograph in the closing moments. **Ask the user how long each hop is
if they have not said**, and size every beat to it.

**Count the words in each beat before you answer, and write the count down.**
This is the instruction most often ignored, and ignoring it is not a small
miss. Two different models asked for six 15 s hops both returned beats averaging
**54 words** -- every single beat under the floor -- and the same two models,
asked for much shorter hops, returned 40 to 48. Left to itself a model writes
about fifty words whatever the hop length. Fifty words is right for a 7 s hop
and is half of what 15 s needs. The table below is not a style note; it is the
one part of this document you have to do arithmetic for.

| hop | words in the beat | spoken lines |
|---|---|---|
| `5 s` | 30-45 | 1 |
| `7 s` | 30-55 | 1 |
| `8 s` | 35-60 | 1-2 |
| `10 s` | 45-75 | 2 |
| `15 s` | 70-100 | 2-3 |

**Both columns, not just the first.** The word count sizes the *description*;
the line count sizes the *audio*, and they fail in opposite directions. Too few
words and the model has nothing to render. Too few spoken lines and it has
several seconds of someone visibly mid-conversation with nothing assigned to
say, so it invents some: a 10 s hop written with a single line came back with
the character babbling dialogue nobody wrote, on two hops out of three. Write
both counts down.

A worked `15 s` beat, at 74 words, from a rendered eight-hop chain:

> They close the distance together and the blades meet at the centre of the clearing, white sparks bursting from the impact as the camera orbits around the lock. @warrior_face turns the heavier blade aside and cuts back across the body; @enemy_shadow catches it on the flat and steps in behind it. Steel rings sharp and resonant on every contact, armour plates grind against one another, and boots drag hard over stone between the exchanges.

Read how the length is spent, because padding to a word count fails differently
but just as badly. One continuous movement carries the whole hop -- they close,
the blades meet, one turns the other aside, the other steps in. The camera move
is named inside the action rather than after it. The last third is the sound bed
and nothing else: three specific noises, each tied to a thing on screen. There
is no second event and no scene change.

The `5 s` and `7 s` rows are the measured spread of the two plans that ship with
the node. Their thinnest beat is 28 words in a 7 s hop, carrying one simple
action; their fullest is 56. The `15 s` row now has one measurement behind it --
the chain the example above comes from ran 74 to 90 words a beat across eight
hops and every hop delivered its beat in full, with no hop idling into the
reference. **The `8 s` and `10 s` rows are still interpolated, not measured.**
If the closing seconds drift or cut to the reference photograph, the beat was
short.

Fill that length with *continuous* material -- one movement that takes the whole
hop, what the camera is doing while it happens, and the sound bed underneath --
rather than with more separate events. Four events crammed into one hop is the
over-delivery in rule 7, and it costs you the next hop's opening.

### Dialogue

Put the spoken line inside the beat, in **single** quotes:

```
@hero_face looks up from the chopping board and says, 'You are early. I have
barely started.'
```

Single quotes survive copy-paste; escaped double quotes are the most common
cause of a rejected plan.

**The line count is in the length table above, and it is a floor as well as a
ceiling.** A long speech in a short hop is truncated mid-word, and that
truncation is then pinned into the next hop's audio. But the opposite failure is
the more common one and it is louder: a hop given fewer lines than the table
asks for leaves seconds of a speaking character with nothing assigned, and the
model writes its own. Two spoken lines in a 10 s hop, not one.

## `ref_plan`

```json
{
  "refs": [
    {"tag": "hero_face", "file": "face.jpg", "subject": 1,
     "retention": "fully_preserved", "shots": [1, 4],
     "desc": "head-and-shoulders, even light"},
    {"tag": "kitchen", "file": "kitchen_wide.jpg",
     "retention": "reference", "shots": [1, 2, 3],
     "desc": "wide shot of the counter and window"}
  ],
  "subjects": {
    "1": {"name": "the cook",
          "locked": "the same face, the same short dark hair",
          "context": "the apron stays tied over the grey t-shirt"}
  }
}
```

The place entry is `"tag": "kitchen"` because the beat says `@kitchen`. **The
two spellings are one string.** Naming the register entry `kitchen_plate`,
`kitchen_ref` or `kitchen_bg` while the beat still reads `@kitchen` stops the
run: the node resolves tags by exact name and has no way to guess that two
different words meant the same picture.

`subjects` holds **plain prose, never tags.** `name` is the phrase that replaces
a subject's `@tag` from hop 2 on, so `"name": "@hero_face"` would resolve to
itself and put a literal at-sign in front of the encoder. Write `"the cook"`.
The same goes for `locked` and `context`.

Valid ref fields: `tag`, `file`, `subject`, `retention`, `desc`, `shots`. (There
is also `slot`, which is derived from list position — never author it.)

- **`tag`** is required, unique, `[A-Za-z0-9_]+`, and is what appears in beats
  as `@tag`.
- **`file`** is a bare filename in `ComfyUI/input/h3_refs`, never a path. A
  filename that is not in that folder **stops the run** -- it is an error, not a
  warning, so a placeholder you invent must be one the user then actually
  supplies. If the user has not told you their filenames, use clear placeholders
  and list every one of them at the end of your answer, with what each picture
  should show.
- **`subject`** is an integer ≥ 1 grouping pictures **of the same person**.
  Declaring two different people under one subject number makes the model render
  **the average of their faces**. One number per person, always.
- **`retention`** is one of:
  - `fully_preserved` — face, bone structure and hairstyle carry over exactly
  - `partially_copy` — the garment and its cut carry over
  - `reference` — layout, surfaces and light carry over, i.e. a place

  It defaults to `fully_preserved` when `subject` is set, `reference` otherwise.

  **Two pictures of one person is the common case, and the two are not
  interchangeable.** Decide from the picture, never from the filename. The
  plate whose framing is dominated by the head is the **likeness** plate:
  `fully_preserved`, on every hop. A plate that shows the whole outfit is the
  **wardrobe** plate: `partially_copy`. If anything else in that picture would
  be wrong in this scene -- a microphone, another room, a different light --
  ride the wardrobe plate on hop 1 only and name the garment's colours in
  `context`, because `fully_preserved` asks for its background too.
- **`shots`** is the list of 1-based hops the picture rides on.

Every subject that appears in `subjects` must be claimed by at least one ref, or
the plan is rejected. Give every subject a **`locked`**: pictures put a face in
front of the encoder, but `locked` is what carries the identity across a hop
where the picture is absent. At most **9 references in the whole plan** -- the limit counts entries in
`refs`, not pictures per hop.

### Scheduling references is the part that decides whether continuity holds

Write `@tag` for a person on any hop you like. Hop 1 resolves it to
`<Subject N>` against its definitions block; hop 2 onward resolves it to the
subject's `name`, which binds to the identity sentence every continuation hop
carries. **Every subject therefore needs a `name`.**

**A reference with no `shots` list rides hop 1 only.** That default is right
for a **place** plate: a room still riding a hop set somewhere else beats the
frame pin, because the model has a crisp picture of one room and a noisy
carried-over frame of another, and it follows the still. **List every hop a
picture belongs on, explicitly.**

**A face plate is the opposite. Put it on every hop.** Two six-hop renders
settled this. A face plate rode a hop set in a space it had never seen and held
cleanly; the hop scheduled with no references came back a different person, and
nothing after it recovered. Identity drift does not self-correct. `locked` holds
a face that is still right; only a plate rebuilds one that is gone.

- Put every **face** reference on every hop: `"shots": [1, 2, 3, 4, 5, 6]`.
- Keep a **place** reference on every hop set in that place, and off every hop
  that is not.
- An **outfit** plate can ride hop 1 only, but then `context` is the only thing
  holding the wardrobe from hop 2 on, so name its colours there.
- Never let a place plate shot in one location ride a hop set somewhere else.
- **The hop that changes location belongs entirely to the new place.** Put the
  departure at the end of the *previous* beat, so that no place plate ever rides
  a hop which opens somewhere it was not photographed.
- **A location introduced part way through needs its own plate, on the hop it
  arrives and every hop after.** Tightening a place plate to its own shots does
  **not** mean the next location goes without one. This is the most common
  mistake made on this rule: two models both plated the opening location, moved
  the story to a second one, gave that second one no plate at all, and wrote a
  confident justification for it. In one case that left the film's main setting
  -- four of six hops -- held by beat text alone. Ask for the extra picture. If
  the plan ends somewhere no picture describes, the plan is wrong.

## What to produce

1. A one-paragraph plan in prose: how many hops, what each covers, where the
   references sit and why.
2. ```json ``` block — the `shot_plan`.
3. ```json ``` block — the `ref_plan`.
4. A short list of any pictures the user still needs to supply, with the
   filename you used for each and what the picture should show.

Before you answer, check every one of these:

- [ ] Shot count matches the hop count the user asked for.
- [ ] Every beat's word count has been counted, written down, and falls inside
      the band for its hop length. A 15 s hop needs 70-100 words; fifty is
      what comes out when this check is skipped.
- [ ] No shot contains a negation anywhere.
- [ ] No beat contains any of: no, not, never, n't, without, none of, stop,
      stops, silent, silence.
- [ ] No beat describes a thing that has finished happening -- nothing fades,
      passes, wanes, subsides, dies down, eases off, recedes or grows quiet.
      Passing this check is not the same as passing the one above it.
- [ ] No beat contains backticks, asterisks or any other markdown. Beats are
      plain prose; a tag inside a beat is written bare, as @tag.
- [ ] No shot names an action ending. The FINAL hop's ending is written as a
      pose plus a sound; every earlier hop ends on motion instead — see below.
- [ ] Every hop but the last ends on a physical action still in progress, in
      those words: "still turning it as the clip ends". A hop whose every
      action has finished carries `tail: ongoing` with nothing underway, and
      the model fills the gap with invented dialogue.
- [ ] Every beat's spoken-line count has been counted, written down, and
      matches the table for its hop length. One line in a 10 s hop is half a
      hop of someone mid-conversation with nothing to say.
- [ ] Any quiet moment names a specific narrowband sound.
- [ ] Any beat where action runs before the first spoken line names the sound
      those opening seconds carry.
- [ ] No hop ends on a spoken line; every dialogue-free hop names a sound of its own.
- [ ] Any hop that changes location joins on `match_cut` or `hard_cut`, not `continuous`.
- [ ] Every state change lands at the end of the shot *before* the one that
      needs it.
- [ ] Shot 1 establishes; no later shot re-describes face, clothes or room.
- [ ] Shot 1 names the visual style, if the user asked for anything other
      than live action.
- [ ] Every persistent object and garment carries an adjective, in the beat
      and again in `context`.
- [ ] Each beat holds true whether the hop before it stopped short or ran
      ahead of its beat.
- [ ] Every `@tag` used in a beat exists in `ref_plan`, spelled identically.
- [ ] Every `@tag` used on hop N has that N in its ref's `shots` list.
- [ ] Every location the film visits has a place plate covering the hops set
      there, including any location introduced after hop 1. The final hop is
      covered by one.
- [ ] `join` is absent from shot 1.
- [ ] The final shot sets `tail` to `settle` or `hold`.
- [ ] Compare each shot's `framing` with the previous shot's: where it
      changes on `join: continuous`, the camera is moving, not `hold`. And no
      `push_in`+`wide`, no `pull_back`+`close`.
- [ ] Where one person has two pictures, the likeness plate is `fully_preserved`
      and the wardrobe plate is `partially_copy`.
- [ ] Every subject in `subjects` is claimed by a ref, and has both a `name`
      and a `locked`.
- [ ] `refs` holds at most 9 entries in total.
- [ ] Dialogue uses single quotes.
- [ ] Both blocks are valid JSON: no trailing commas, no comments, no smart
      quotes, plain ASCII, and no field outside the lists above.
