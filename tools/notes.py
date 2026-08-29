"""The instruction board that ships on the Starter workflow's canvas.

One source for six `MarkdownNote` cards laid out to the left of the loaders.
`build_notes.py` writes them into `workflows/HandTieClips_Starter.json`.

Why on the canvas at all: the craft lives in `PROMPTING.md`, `prompt_pack/` and
the editor's Templates panel, and all three require leaving the graph. The rules
that decide whether a first render works are needed at the moment beats are being
written, which is on the canvas.

This is a condensation, not a copy. `PROMPTING.md` stays the long-form authority
and every card says so; what is here is the part needed to get a first render
right. Keep the cards short enough to read at a glance -- a wall of text on the
canvas is the same as no text on the canvas.

Layout: two columns of three at x=-960 and x=-480, 440 wide, so the board ends at
x=-40 and the loaders (which start at x=0) are untouched. Reading order is down
column A, then down column B.
"""

YELLOW = ("#432", "#653")     # what the existing Note uses
GREEN = ("#232", "#353")      # ComfyUI's green -- card 1 only, so the entry
                              # point is unmistakable in a wall of yellow

GROUP = {
    "id": 1,
    "title": "READ ME -- writing for this node",
    "bounding": [-990, -465, 980, 2560],
    "color": "#3f789e",
    "font_size": 24,
}

# `extra.ds` is restored on load rather than fitted, so without this the board
# sits off-screen to the left and is never found. Screen = (world + offset) *
# scale, so an offset of 500 puts world x=-500 at the left edge: column B, the
# loaders and the left of the chain node all in view at once.
DS = {"scale": 0.8264462809917354, "offset": [500, -281.1386834420914]}


START_HERE = """\
## Start here

Two hops of 5 s, joined into one 10 s clip.

### Requires

The MODEL wire is a turbo stack, not a bare loader:

```
UNETLoader
  -> LoRA Loader Stack          (turbo LoRA)
  -> H3 AdaLN LoRA Fix
  -> MiniMax H3 Low VRAM Attention
  -> H3 SLA Attention
  -> Model Preview Override
  -> this node
```

| pack | nodes |
|---|---|
| **ComfyUI-PlagueKind-Nodes** | LoRA Loader Stack, AdaLN Fix, SLA Attention |
| **ComfyUI-KJNodes** | Low VRAM Attention (experimental), Model Preview Override |

Also on disk: the **turbo LoRA** named in the loader, and `taeh3.safetensors`
for the live preview (or set `tiny_vae` to `none`).

**CLIP goes to this node from the LoRA loader, not from the encoder.** That is
what makes the text half of every LoRA land. Do not rewire it back.

`steps` is **7**, which only works with the turbo LoRA. Missing a pack? Its
nodes load as red boxes -- delete them, wire the loader straight into `model`
and the encoder into `clip`, and raise `steps` to 20 or so.

1. Point the four **loaders** at your H3 files.
2. Queue.
3. Read the **SHOTS** cards on the node.

**Shot 1 is the whole opening. Every later shot is only the new beat.** The node
writes the identity lock, the live-frame citation and the join itself -- so
re-describing the face, the clothes or the room after shot 1 competes with the
frame pin instead of reinforcing it.

This plan ships with **no references on purpose**, so it runs before you have
supplied any pictures.

### Adding pictures

- Open **REFERENCES**, add a row, drop a photo on its thumbnail. Files land in
  `ComfyUI/input/h3_refs`. There is no Load Image node to wire.
- Give the row a `@tag`, and group photos of the same person under one subject.
- Write the tag into the beat: `@hero_face stands at the counter in @kitchen`.

The long-form guide is `PROMPTING.md` in the pack folder.\
"""


RULES = """\
## The rules that decide whether it works

Not style preferences. This is how this model fails.

### 1. The prompt is additive

Sampling runs at **cfg 1.0 with no negative branch**. Every concept you name is
added, and nothing can be removed by mentioning it -- `no cut` puts the word
*cut* in front of the encoder. **Never write a negation.**

### 2. Never name the thing you want to end

"The cook stops talking" keeps her talking. Write the state you want as **a pose
plus a sound**:

> leans back against the counter with her lips closed, and lets her eyes move
> slowly across the room. The kitchen is quiet apart from the low hum of the
> refrigerator.

Audio is always generated. Silence written as an absence comes back as speech,
so **silence has to be written as a sound** -- room tone, a fridge, a single
click. Keep it narrowband: "faint street noise" renders as a five-second hiss.

**The ban is on the idea, not on a word list.** *Fades, passes, wanes, subsides,
dies down, eases off* all name an ending as surely as *stops* does, and all of
them add the thing they describe. Ask of each sentence: is this happening, or
has it finished happening?

### 3. A state change belongs at the END of the previous shot

Every hop opens holding the frames it was handed, and the audio pin carries the
previous hop's tail across the join. Nothing you write in shot 3 can make shot 3
start quiet. **Arrive there before the previous shot ends.**

### 4. A hop that ends on dialogue keeps talking

Speech at the end of hop N opens hop N+1 and propagates down the whole chain.
Land each line **mid-hop** and leave a non-verbal action running into the seam --
slicing, walking, a hand on a doorframe. Give every hop with no dialogue a sound
bed of its own.

### 5. A walk between two rooms is `match_cut`

`join: continuous` across a real location change makes the model morph one room
into the other mid-movement.

### 6. Set `tail` on your last shot

Left at `ongoing`, the model is told action is still underway at the final frame
and will invent something to satisfy it. Use `settle` or `hold`.\
"""


REFERENCES = """\
## References and @tags

A picture in the register does not make the model use it. **The beat is what
drives the frame** -- write the tag into the action line.

Phrase a place as a place that is *depicted*, not as a container to be placed
inside: "at the counter in `@kitchen`", not "steps into `@kitchen`".

### Fields

| field | what it does |
|---|---|
| `tag` | what you write in beats as `@tag` |
| `file` | a bare filename in `input/h3_refs` |
| `subject` | groups pictures **of the same person** |
| `retention` | `fully_preserved` / `partially_copy` / `reference` |
| `shots` | the 1-based hops this picture rides |

**One subject number per person.** Two different people under one number makes
the model render the average of their faces.

### `shots` is the part that decides continuity

**A reference with no `shots` list rides hop 1 only.** Right for a **place**
plate: a room still riding a hop set somewhere else beats the frame pin, and the
model follows the still. **List every hop a picture belongs on.**

**A face plate is the opposite — put it on every hop.** A hop scheduled with no
face reference came back a different person, and no later hop recovered.
Identity drift does not self-correct.

- Every **face** ref gets every hop: `"shots": [1, 2, 3, 4, 5, 6]`.
- Keep a **place** ref on the hops set in that place, and off the rest.

### `locked` and `context`

Per-subject prose that rides **every hop**, with no picture citation, so it
carries identity across a hop where the photograph is absent. Give every subject
a `locked`, and a `name` -- from hop 2 on, `@tag` for a person resolves to that
name.

**Name a colour or it drifts.** A noun with no adjective is unanchored: each hop
is an independent encode, so "the bowl" on hop 4 came back stainless steel. Put
properties in `context` -- "the bowl is white porcelain" -- never locations, and
repeat the adjective in every beat.\
"""


DIRECTIVES = """\
## Directives

Five axes, all optional, set per shot. An unset axis emits nothing at all and
costs no tokens.

| axis | values |
|---|---|
| `join` | `continuous`, `match_cut`, `hard_cut` -- **omit on shot 1** |
| `camera` | `hold`, `pan_follow`, `push_in`, `pull_back`, `orbit`, `handheld` |
| `framing` | `keep`, `wide`, `medium`, `close` |
| `pace` | `slow`, `steady`, `brisk` |
| `tail` | `ongoing` (default), `settle`, `hold` |

They compile in that order -- `join` first, because it describes how this hop
meets the previous one.

### Two combinations the node warns about

**`join: continuous` + a framing change + `camera: hold`.** A framing change asks
the audience to be somewhere new; with the camera still, the only way there is a
cut. Earn it on the move (`push_in`, `pull_back`, `pan_follow`) or use
`framing: keep`.

**`push_in` + `wide`, or `pull_back` + `close`.** The move points the opposite way
from the destination.

Both are warnings, not errors. They are legitimate things to want -- they just
rarely read the way you meant.

### Duration

Per-shot `duration` overrides the chain, using exactly these labels:
`"5 s"`, `"7 s"`, `"8 s"`, `"10 s"`, `"15 s"`.\
"""


TROUBLE = """\
## When it goes wrong

| symptom | cause | fix |
|---|---|---|
| The clip cuts to the reference photo in its last seconds | The beat finished before the frames did | Set `tail`, give the beat enough to do |
| A stray gesture or line in the closing second | `tail: ongoing` on the final shot | `settle` or `hold` |
| She keeps talking after you asked for quiet | You named the ending | Pose plus a sound |
| Dialogue continues into hops that have none written | The hop before ended on speech | Land the line early; non-verbal action into the seam; a sound bed on every quiet hop |
| A character walks between two rooms and one morphs into the other | `continuous` across a location change | `match_cut` |
| Silence renders as speech | Silence written as an absence | Name room tone, a fridge, a distant car |
| Ambience is a five-second hiss | Broadband wording | Narrowband, or one discrete event |
| Two characters' faces merge | Both declared as the same `subject` | One subject number per person |
| The face becomes a different person partway through | A hop scheduled with no face plate. `locked` holds a face that is still right; only a plate rebuilds one that is gone, and the drift never self-corrects | Put the face ref on **every** hop |
| A stylised plan renders photoreal | The node's hop-1 establishing line asserts live action | Name the medium in shot 1's first sentence, or clear the `establish` widget |
| The film gets darker every hop | Luminance drifts one way and nothing pushes back | Restate the light as a positive property in every beat |
| A location introduced mid-plan drifts | It has no place plate of its own | Plate it, on the hop it arrives and every hop after |
| A prop or garment changes colour or material | Named with no adjective, so each hop's encode is free to invent one | Repeat the adjective in every beat, and state it as a property in `context` |
| A hard cut ~1.5 s into a hop, mid-scene | The previous hop over-delivered, so this beat instructs what its own live frame already did | One movement per hop; write the next beat so it is true from either ending |
| A continuous join reads as a cut | Framing change with `camera: hold` | Earn it on the move, or `framing: keep` |
| The run stops, naming a reference | That row's picture is not in `h3_refs` | Drop the file on the row, or clear its picture to run without it |
| A reference has no effect on some hop | Its `shots` list leaves that hop out | List every hop the picture should ride |
| A pasted plan is rejected as invalid JSON | Escaped double quotes mangled in transit | Single quotes around dialogue |

Every error message names the shot or the reference it came from. Nothing
guesses.\
"""


AUTHOR = """\
## Let a model write your plan

`prompt_pack/` in the pack folder turns any chat model into a plan writer. In
LM Studio, or anything with a system-prompt box:

1. Load a model with **context 8192 or more**. The prompt is ~2,600 tokens and
   the reply another 1,000-2,000; a 4k window truncates the rules and you get
   invented directive names.
2. Paste **`prompt_pack/SYSTEM_PROMPT.md`** into the **System Prompt** box.
   Nothing else goes in that box.
3. **Temperature 0.3-0.5.** Higher and the JSON grows trailing commas and smart
   quotes.
4. Describe the scene, and say how many hops and what pictures you have:

   > Six hops. A cook in a kitchen; she says one line, walks out into a hallway,
   > waits by a window, then comes back. I have a face photo, a photo of her
   > apron, and a photo of the kitchen.

5. Each panel section has its own **JSON** disclosure at the bottom. The first
   ```json``` block goes in the one under **SCRIPT** (`shot_plan`), the second
   in the one under **REFERENCES** (`ref_plan`). Bad JSON keeps the last good
   version on screen and says so, rather than discarding your paste.
6. **If the node rejects it, paste the error straight back into the chat.** One
   round trip usually fixes it.

Want it to match a shape? Paste `prompt_pack/EXAMPLE_6_HOP.md` first.

Small models (7B-8B) hold the JSON schema but drift on the prose rules -- they
write negations. Skim the beats before queueing.\
"""


SHOWCASE_NOTE = """\
Hand Tie Clips -- SHOWCASE (6 hops x 7 s = 39.2 s)

A continuity stress test, and the honest demonstration of what the reference
register buys you.

REQUIRES a turbo stack on the MODEL wire, which is how this node is actually
run here:

    UNETLoader -> LoRA Loader Stack (turbo LoRA) -> H3 AdaLN LoRA Fix
               -> MiniMax H3 Low VRAM Attention -> H3 SLA Attention
               -> Model Preview Override -> this node

    ComfyUI-PlagueKind-Nodes  ->  LoRA Loader Stack, AdaLN Fix, SLA Attention
    ComfyUI-KJNodes           ->  Low VRAM Attention (experimental),
                                  Model Preview Override

On disk as well: the turbo LoRA named in the loader, and taeh3.safetensors for
the live preview (or set tiny_vae to none).

CLIP reaches this node FROM THE LORA LOADER, not from the encoder. That is what
makes the text half of every LoRA land. Do not rewire it back.

`steps` is 7, which only works with the turbo LoRA. Missing a pack? Its nodes
load as red boxes -- delete them, wire the loader straight into `model` and the
encoder into `clip`, and raise `steps` to 20 or so.

BEFORE YOU RUN IT, supply three pictures. Open REFERENCES on the node and drop
your own onto each thumbnail, or put files in ComfyUI/input/h3_refs named:

    ref_face.jpg     head-and-shoulders, even light   -> @hero_face
    ref_outfit.jpg   full length, same person         -> @hero_outfit
    ref_room.jpg     the room, wide                   -> @kitchen

Without them the run stops and names the reference it could not find. Nothing
guesses.

What each hop is testing:

  1  kitchen, dialogue           all three references active
  2  lateral move to the window  kitchen only
  3  exits through the doorway   kitchen only
  4  hallway - UNSEEN space      face re-asserted; no room reference exists
  5  dialogue, ZERO references   identity, wardrobe and voice ride on the frame
                                 pin plus subjects.1.locked/context alone
  6  returns to the kitchen      on a MATCH CUT, not a continuous join -- one
                                 unbroken take across two rooms makes the model
                                 morph one into the other mid-movement

Hop 5 is the point of the whole thing. If the cook is still the same person in
the same apron with the same voice, with no picture in front of the encoder,
the register is doing its job.

`shots` on each reference is what schedules this. On a continuation chain,
omitting `shots` means HOP 1 ONLY. Right for a place plate, wrong for a face:
put every face reference on every hop, or the identity drifts and stays
drifted. List every hop a still should appear on.

Settings that are deliberate, not defaults:
  control_after_generate = fixed   or no two runs are comparable
  tone_compensate = frame_shift    counters the drift that accumulates over six
                                   hops. Set it to `off` and re-queue for a
                                   same-seed A/B: tone mode is not in the hop
                                   key, so every hop replays from cache in
                                   seconds instead of re-rendering.
  cache_hops = on                  edit shot 4 and only shots 4-6 re-render
"""


CARDS = [
    # key, title, pos, size, colour
    ("start", "START HERE", [-960, -400], [440, 940], GREEN, START_HERE),
    ("rules", "The rules", [-960, 600], [440, 760], YELLOW, RULES),
    ("refs", "References and @tags", [-960, 1420], [440, 640], YELLOW, REFERENCES),
    ("directives", "Directives", [-480, -400], [440, 640], YELLOW, DIRECTIVES),
    ("trouble", "When it goes wrong", [-480, 280], [440, 760], YELLOW, TROUBLE),
    ("author", "Let a model write it", [-480, 1080], [440, 620], YELLOW, AUTHOR),
]
