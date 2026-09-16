r"""Offline tests for the refine blend ramp -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_refine_blend.py

The ramp decides which frames of every hop ship the raw sample and which ship
the refined one. Get it wrong by a couple of latent steps and the seam is
refined after all -- which is the exact failure the blend exists to avoid, and
it is invisible in the log, in the console and in a still frame. It only shows
up as a geometry step at the join, three hops later, mixed in with everything
else being tested that day.

So the arithmetic is asserted against hand-computed values here, where it costs
a second. The one that matters most is the `duration` bug in the node this is
ported from: his ramp position is read off a widget, and this file's job is to
prove ours is read off the latent.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

import torch  # noqa: E402

import refine_blend as rb  # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def near(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


def main():
    print("step_for_frame -- the (1,4,4,4,4) token cycle")
    # Step 0 carries frame 0 alone; steps 1-4 carry four each, so the first
    # five steps cover 17 frames. Everything after is fours.
    ck("frame 0 is step 0", near(rb.step_for_frame(0), 0.0))
    ck("frame 1 closes step 0", near(rb.step_for_frame(1), 1.0))
    ck("frame 17 closes step 4", near(rb.step_for_frame(17), 5.0))
    ck("frame 22 closes step 6, i.e. 7 steps cover it",
       near(rb.step_for_frame(22), 7.0), "7.00")
    ck("frame 44 is a quarter into step 13",
       near(rb.step_for_frame(44), 13.25), "13.25")
    # The published ramp lands on steps 7-13, not on 22/192 of the way along.
    uniform = 22.0 * 48.0 / 192.0
    ck("and it is NOT the uniform ratio", not near(rb.step_for_frame(22),
                                                   uniform, 0.2),
       f"uniform would say {uniform:.2f}")
    ck("monotone across the cycle",
       all(rb.step_for_frame(i) <= rb.step_for_frame(i + 1)
           for i in range(0, 200)))

    print("\nwhole_steps_for_frames -- the head freeze needs whole tokens")
    # The mapping the ported node documents: 5/22/39/56 -> 2/7/12/17.
    for f, want in ((5, 2), (22, 7), (39, 12), (56, 17)):
        ck(f"{f} frames is {want} steps",
           rb.whole_steps_for_frames(f) == want,
           str(rb.whole_steps_for_frames(f)))
    ck("every shipped overlap is on the grid",
       all(rb.whole_steps_for_frames(f) for f in (5, 22, 39)))
    ck("off the grid is None, not a rounded guess",
       rb.whole_steps_for_frames(23) is None)
    ck("no frames is None", rb.whole_steps_for_frames(0) is None)

    print("\nparse")
    ck("the default ramp", rb.parse(rb.DEFAULT_RAMP)
       == [(0.0, 0.0), (22.0, 0.0), (44.0, 1.0)])
    ck("empty is no blend, not zero blend", rb.parse("   ") == [])
    ck("newline separated", len(rb.parse("0:0\n22:0\n44:1")) == 3)
    ck("out of order is sorted", rb.parse("44:1, 0:0")[0][0] == 0.0)
    for bad, why in (("0:0, 22", "no colon"),
                     ("0:2", "weight above 1"),
                     ("-4:0", "frame before the hop"),
                     ("0:0, 22:refined", "weight is not a number")):
        try:
            rb.parse(bad)
            ck(f"rejects {why}", False, f"{bad!r} was accepted")
        except ValueError as e:
            ck(f"rejects {why}", "refine_blend" in str(e))

    print("\nstep_weights")
    keys = rb.parse(rb.DEFAULT_RAMP)
    w = rb.step_weights(48, keys, "linear")
    ck("one weight per latent step", len(w) == 48)
    ck("the pinned head ships raw", all(x == 0.0 for x in w[:7]),
       f"steps 0-6 = {w[0]:.2f}")
    ck("the crossover is partial", 0.0 < w[9] < 1.0, f"step 9 = {w[9]:.3f}")
    ck("past the last key it is fully refined",
       all(x == 1.0 for x in w[14:]), f"step 13 = {w[13]:.2f}")
    ck("never leaves 0..1", all(0.0 <= x <= 1.0 for x in w))
    ck("monotone for a monotone ramp",
       all(w[i] <= w[i + 1] for i in range(len(w) - 1)))

    ws = rb.step_weights(48, keys, "smooth")
    ck("smooth has the same endpoints",
       ws[0] == 0.0 and ws[-1] == 1.0 and all(0.0 <= x <= 1.0 for x in ws))
    ck("smooth is not linear", any(not near(a, b, 1e-3)
                                   for a, b in zip(w, ws)))
    wst = rb.step_weights(48, keys, "step")
    ck("step holds the previous key",
       all(x in (0.0, 1.0) for x in wst))
    ck("no keys means the refine is taken whole",
       rb.step_weights(6, [], "linear") == [1.0] * 6)
    try:
        rb.step_weights(48, keys, "cosine")
        ck("unknown interpolation is refused", False)
    except ValueError:
        ck("unknown interpolation is refused", True)

    print("\nthe duration bug this port exists to fix")
    # xyzdist's node reads the ramp position off a `duration` widget. The same
    # ramp on the same latent must not move when a duration is mis-set, so the
    # only thing that may change these weights is the latent itself.
    a = rb.step_weights(48, keys, "linear")
    b = rb.step_weights(48, rb.parse(rb.DEFAULT_RAMP), "linear")
    ck("the ramp depends only on the keys and the step count", a == b)
    short = rb.step_weights(31, keys, "linear")   # a 5 s hop, 124 f
    ck("a shorter hop keeps the same crossover steps",
       short[:12] == a[:12], "head and ramp are frame-addressed")
    ck("and still ends fully refined", short[-1] == 1.0)

    print("\nblend_video")
    raw = torch.zeros((1, 16, 48, 8, 8))
    ref = torch.ones((1, 16, 48, 8, 8))
    out = rb.blend_video(raw, ref, w)
    ck("shape survives", tuple(out.shape) == tuple(raw.shape))
    ck("weight 0 is exactly the raw sample",
       float(out[:, :, 0].abs().max()) == 0.0)
    ck("weight 1 is exactly the refined sample",
       float((out[:, :, 47] - 1.0).abs().max()) == 0.0)
    ck("the crossover is a real mix",
       0.0 < float(out[:, :, 9].mean()) < 1.0)
    ck("every step matches its weight",
       all(near(float(out[:, :, j].mean()), w[j], 1e-6)
           for j in range(48)))
    try:
        rb.blend_video(raw, torch.ones((1, 16, 47, 8, 8)), w)
        ck("a shape mismatch is refused", False)
    except ValueError as e:
        ck("a shape mismatch is refused", "refine_blend" in str(e))
    try:
        rb.blend_video(raw, ref, w[:-1])
        ck("a weight-count mismatch is refused", False)
    except ValueError as e:
        ck("a weight-count mismatch is refused", "refine_blend" in str(e))

    print("\ndescribe")
    line = rb.describe(w, keys, "linear")
    ck("names the raw and refined counts", "raw" in line and "refined" in line,
       line)
    ck("names where the keys landed on the grid", "13.25" in line, line)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
