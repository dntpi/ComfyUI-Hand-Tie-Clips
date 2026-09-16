# Worked example: six hops, four pictures

This is the plan inside `workflows/HandTieClips_Showcase.json`, reproduced here
so it can be shown to a model as an example of the shape and the reasoning. It
is generated from that workflow, so the two cannot drift apart.

The scene: a cook in a kitchen speaks a line, crosses the room, leaves through a
doorway into a hallway, speaks again there, and comes back.

## What each hop is for

| hop | id | references active |
|---|---|---|
| 1 | s1 | @hero_face, @hero_outfit, @kitchen |
| 2 | s2 | @hero_face, @kitchen |
| 3 | s3 | @hero_face, @kitchen, @hallway |
| 4 | s4 | @hero_face, @hallway |
| 5 | s5 | @hero_face, @hallway |
| 6 | s6 | @hero_face, @kitchen, @hallway |

Four things in that table are the whole point:

- **The face plate rides every hop.** It is the one reference that is never
  tightened. Identity drift does not self-correct, and only a plate rebuilds a
  face that has already gone.
- **The hallway has its own plate.** Narrowing the kitchen plate to the hops set
  in the kitchen does not mean the second location goes without one. Leaving it
  unplated is the most common mistake on this rule, and it would have left three
  of six hops held by beat text alone.
- **Hops 3 and 6 carry both place plates**, because those are the two hops that
  travel: each opens in one room and ends in the other, so each is plated for
  both. Every other hop carries exactly the room it is in.
- **The outfit plate rides hop 1 only.** From hop 2 on, the wardrobe is held by
  `subjects.1.context` alone -- which is why that field names the colours rather
  than pointing back at the picture.

Note also that every hop before the last carries `tail: ongoing` and ends on
something still underway, that the two location changes join on `match_cut`
rather than `continuous`, and that the dialogue on hops 1 and 5 uses single
quotes inside the JSON string.

## shot_plan

```json
{
  "shots": [
    {
      "id": "s1",
      "beat": "@hero_face stands at the counter in @kitchen, a grey apron over a grey t-shirt. She looks up from the chopping board and says, 'You are early. I have barely started.' She turns back to the board and goes on slicing, the knife tapping steadily against the wood.",
      "directives": {
        "camera": "hold",
        "framing": "medium",
        "pace": "steady",
        "tail": "ongoing"
      }
    },
    {
      "id": "s2",
      "beat": "She sets the knife down and walks the length of the counter to the window of @kitchen, looking out at the street. Her steps are soft on the tiles and the refrigerator hums behind her.",
      "directives": {
        "join": "continuous",
        "camera": "pan_follow",
        "framing": "medium",
        "pace": "steady",
        "tail": "ongoing"
      }
    },
    {
      "id": "s3",
      "beat": "She turns from the window, crosses @kitchen and pushes through the doorway into the hallway of @hallway beyond, the room falling away behind her. Her footsteps carry off the tiles and onto the soft runner as she keeps walking.",
      "directives": {
        "join": "match_cut",
        "camera": "pan_follow",
        "framing": "wide",
        "pace": "brisk",
        "tail": "ongoing"
      }
    },
    {
      "id": "s4",
      "beat": "@hero_face walks down the narrow hallway of @hallway, hung with coats, one hand trailing along the wall, her footsteps muffled on the runner. The hallway walls stand close on either side of her and the coats brush past her shoulder.",
      "directives": {
        "join": "continuous",
        "camera": "handheld",
        "framing": "medium",
        "pace": "steady",
        "tail": "ongoing"
      }
    },
    {
      "id": "s5",
      "beat": "She reaches the window at the end of @hallway and rests one hand on the frame, then half turns back over her shoulder and says, 'It is still raining. We will have to do it inside.' She pushes off the frame and starts back down the hall.",
      "directives": {
        "join": "continuous",
        "camera": "push_in",
        "framing": "medium",
        "pace": "slow",
        "tail": "ongoing"
      }
    },
    {
      "id": "s6",
      "beat": "She walks back along @hallway and through the doorway to the counter in @kitchen, picking the knife up again and settling back into the rhythm of it. The refrigerator hums and the knife starts on the board.",
      "directives": {
        "join": "match_cut",
        "camera": "pull_back",
        "framing": "wide",
        "pace": "steady",
        "tail": "hold"
      }
    }
  ]
}
```

## ref_plan

```json
{
  "refs": [
    {
      "tag": "hero_face",
      "file": "ref_face.jpg",
      "subject": 1,
      "retention": "fully_preserved",
      "shots": [
        1,
        2,
        3,
        4,
        5,
        6
      ],
      "desc": "head-and-shoulders photograph of the cook, even light"
    },
    {
      "tag": "hero_outfit",
      "file": "ref_outfit.jpg",
      "subject": 1,
      "retention": "partially_copy",
      "shots": [
        1
      ],
      "desc": "full-length photograph of the same cook, grey apron over a grey t-shirt"
    },
    {
      "tag": "kitchen",
      "file": "ref_room.jpg",
      "retention": "reference",
      "shots": [
        1,
        2,
        3,
        6
      ],
      "desc": "the kitchen: counter, window, and the light coming through it"
    },
    {
      "tag": "hallway",
      "file": "ref_hall.jpg",
      "retention": "reference",
      "shots": [
        3,
        4,
        5,
        6
      ],
      "desc": "the hallway: coats along one wall, a runner underfoot, a window at the far end"
    }
  ],
  "subjects": {
    "1": {
      "name": "the cook",
      "locked": "the same face, the same short dark hair, the same silver stud earrings",
      "context": "the grey apron stays tied over the grey t-shirt"
    }
  }
}
```
