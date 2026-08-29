# Worked example: six hops, three pictures

This is the plan inside `workflows/HandTieClips_Showcase.json`, reproduced here so
it can be shown to a model as an example of the shape and the reasoning. It is
generated from that workflow, so the two cannot drift apart.

The scene: a cook in a kitchen speaks a line, crosses the room, leaves through a
doorway into a hallway the register has no picture of, speaks again there, and
comes back.

## What each hop is for

| hop | id | references active |
|---|---|---|
| 1 | s1 | @hero_face, @hero_outfit, @kitchen |
| 2 | s2 | @hero_face, @kitchen |
| 3 | s3 | @hero_face, @kitchen |
| 4 | s4 | @hero_face |
| 5 | s5 | @hero_face |
| 6 | s6 | @hero_face, @kitchen |

Three things in that table are the whole point:

- **Hop 3** carries the kitchen while she is leaving it, and hop 4 does not.
  The hallway is a space no reference describes, so the model must invent it —
  and a kitchen plate riding hop 4 would drag her back into the kitchen.
- **Hop 4** re-asserts the face. Entering an unseen space is where identity
  drift starts, and re-asserting there is cheaper than recovering on 5 and 6.
- **Hop 5 has no references at all.** Identity, wardrobe and voice ride on the
  frame pin plus `subjects.1.locked` and `.context` alone. If she is still the
  same person in the same apron with the same voice, the register works.

Note also that the dialogue lands on hops 1 and 5 — the establishing shot and
the one with no pictures — and that both use single quotes.

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
      "beat": "She turns from the window, crosses @kitchen and pushes through the doorway into the hallway beyond, the room falling away behind her. Her footsteps carry on the tiles.",
      "directives": {
        "join": "continuous",
        "camera": "pan_follow",
        "framing": "wide",
        "pace": "brisk",
        "tail": "ongoing"
      }
    },
    {
      "id": "s4",
      "beat": "@hero_face walks down a narrow hallway hung with coats, one hand trailing along the wall, her footsteps muffled on the runner. The hallway walls stand close on either side of her and the coats brush past her shoulder.",
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
      "beat": "She reaches the window at the end of the hall and rests one hand on the frame, then half turns back over her shoulder and says, 'It is still raining. We will have to do it inside.' She looks back out at the glass with her lips closed, and the rain taps steadily on the pane.",
      "directives": {
        "join": "continuous",
        "camera": "push_in",
        "framing": "close",
        "pace": "slow",
        "tail": "settle"
      }
    },
    {
      "id": "s6",
      "beat": "She walks back along the hallway and through the doorway to the counter in @kitchen, picking the knife up again. The refrigerator hums and the knife starts on the board.",
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
