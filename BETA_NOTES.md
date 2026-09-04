# 1.2.0-beta1 — `anchor: "restart"`

Beta. Two things over 1.1.0: a crash fix everyone needs, and one new feature
that wants testing on a real chain.

## The crash fix (this is why 1.1.1 exists)

If your ComfyUI ever gave you this on hop 1, before anything rendered:

    MiniMaxH3ReferenceToVideo.execute() got multiple values for argument
    'ref_image_size'

that is fixed. The pack was handing Core its first seven arguments by
position, which only works if your ComfyUI orders them the same way the pack
was written against. Some builds don't. Everything is passed by name now, so
the order upstream no longer matters. If a future ComfyUI renames something,
you'll get a message naming both signatures instead of a traceback.

## The new thing: `anchor: "restart"`

**What it's for.** On a long chain the picture goes flat — skin plastic, light
dead, everything a bit stiller than it should be. Measured on a 6-hop chain
where every hop was given the *identical* beat, so nothing below is the script:

    the subject's movement fell 39%
    the background's brightness swing fell 64%
    lighting-vs-head-position went from +0.63 to -0.49 -- it INVERTED

Every hop is conditioned on the previous hop's **last frames**, and the end of
a clip is its most settled moment. So each hop starts from a slightly calmer,
flatter state than the one before, and it compounds. That is why fiddling with
`pin_to_qwen`, `pin_renorm` and `pin_mech` doesn't fix it: they all change how
the previous hop is *presented*, not the fact that it is inherited.

**What it does.** Put `"anchor": "restart"` on a shot and that hop stops
relaying. It takes your start image as its frame 0, exactly as hop 1 does, and
nothing from the previous hop reaches it. The decay is bounded to the distance
between restarts instead of running the whole chain.

**How to use it.** In the SCRIPT tab, expand `ADVANCED` on any shot after the
first — there's an `anchor` dropdown. Or by hand:

    {"beat": "She picks up the next point.",
     "anchor": "restart",
     "directives": {"join": "hard_cut", "camera": "hold"}}

It needs a **start image** set in MEDIA — that's what it restarts onto.

**It is a CUT.** The hop opens on the start image's pose, not where the last
hop ended, so `join` must be `hard_cut` or `match_cut`. It refuses
`continuous`, on the queue, rather than making a bad seam. Write the beat as a
fresh start.

## What we measured, honestly

One chain, 6 hops, 640x1152, restart on hop 4. Against the same chain with no
restart:

    at the restart hop:  +80% lighting swing, +33% movement, better response
    two hops later:      about half the damage of the control, but decaying again

So it works, and **one restart every six hops is not enough**. The decay
resumes straight away. If you're running long chains, try a restart every 3
hops or so. `pin_noise` around 0.06 slows the decay in between — measured
better than both 0 and the 0.10 maximum, which injects flicker that isn't
coupled to the subject.

This is a single measurement on one subject. That's the main reason it's a
beta.

## Known rough edges

- A restart hop still drops the 22-frame overlap even though it has nothing to
  overlap with, so you lose ~0.9 s of fresh material at the cut. Harmless, but
  unintended.
- The `anchor` dropdown is new. Before this build, editing any shot card
  silently discarded `anchor` **and `tone`** — `tone` has had that bug since
  1.1, so if you ever set `"tone": "rebase"` by hand and it seemed to do
  nothing, that's why.
- `resync` — re-anchoring on a *pose-matched* clean frame so the join can stay
  continuous, for rolling scenes where a cut is wrong — is designed but not
  built.

## What would help most

Whether a restart every 2-3 hops actually holds a long chain up, on your
content. That's the number we couldn't get from one 6-hop test.
