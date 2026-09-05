"""Did the chain leave the shot? A per-second read of a delivered clip.

`anchor: "restart"` opens a hop on `start_image` with no video pin, and an
outside 9-hop study caught what that can cost: for about two and a half
seconds the model rendered the reference PHOTOGRAPH instead of the room. Her
instruments had already recorded it and nobody read them -- background edge
density at that hop was **0.003** against 4.7 to 7.2 at every other hop of the
same run, and the distinct-colour count halved. The write-up called it "a hard
cut" because the number nobody expected to look at was the one that mattered.

So this asks the same question of our own output, and asks it per SECOND rather
than per hop. Two reasons that is the better shape:

  * It needs no hop arithmetic. Restart hops write their full length and
    continuations are trimmed by the overlap, so hop boundaries move with the
    plan; a misaligned window would read the wrong frames and say so
    confidently. Seconds cannot be misaligned.
  * The failure it is looking for is a WANDER, not a bad hop. In her package
    the restart hop opened correctly on the room, drifted to the portrait
    around 2.5 s in, and came back before the hop ended. A per-hop mean would
    have averaged that away.

Two measurements, both cheap and both collapsing hard on a studio portrait:

  edges   -- Canny density over the whole frame, as a percentage of pixels. A
             room full of shelves, a lamp and a microphone has plenty; a
             seamless backdrop behind a face has almost none. This is the
             separation her study measured at roughly 2000x.
  colours -- distinct 5-bit RGB triples. A lit room spans thousands; a portrait
             on grey spans hundreds. Independent of the edge reading, so two
             instruments have to agree before this reports anything.

Deliberately whole-frame: no person segmentation, no YOLO, no new dependency.
Masking the subject out would be more precise and would make this a different
tool that needs models. The signal being looked for is a set change, and a set
change does not need a mask to see.

    python tools/shot_probe.py output/chain_00091_.mp4
    python tools/shot_probe.py clip.mp4 --stride 0.5 --csv out.csv

Exit code is 1 when a suspect window is found, so it can gate a test run.
"""
from __future__ import annotations

import argparse
import sys

import av
import cv2
import numpy as np


def read_frames(path, stride_s):
    """Decode `path`, yielding (t_seconds, RGB uint8 frame) every `stride_s`."""
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        fps = float(stream.average_rate or 24)
        step = max(1, int(round(fps * float(stride_s))))
        for i, frame in enumerate(container.decode(stream)):
            if i % step:
                continue
            yield i / fps, frame.to_ndarray(format="rgb24")


def measure(rgb):
    """-> (edge density %, distinct 5-bit colours) for one frame."""
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Fixed thresholds, not Otsu: the comparison is BETWEEN frames of one clip,
    # and an adaptive threshold would re-normalise away the very collapse this
    # is looking for.
    edges = cv2.Canny(grey, 60, 160)
    density = 100.0 * float((edges > 0).mean())
    quant = (rgb.astype(np.uint16) >> 3)
    packed = (quant[..., 0] << 10) | (quant[..., 1] << 5) | quant[..., 2]
    return density, int(np.unique(packed).size)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("video")
    ap.add_argument("--stride", type=float, default=0.5,
                    help="seconds between samples (default 0.5)")
    # Calibrated against a known positive rather than guessed, and at two
    # resolutions rather than one.
    #
    #                       portrait window        room floor
    #   edges   1.03 MP     x0.21-0.40             x0.78
    #   edges   0.70 MP     x0.25-0.46             x0.81
    #   colours 1.03 MP     x0.53-0.74             x0.85
    #   colours 0.70 MP     x0.54-0.75             x0.86
    #
    # The first threshold written here was a single 0.45 on BOTH measures, and
    # it MISSED the event: edges collapsed to 0.21, colours only to 0.53. They
    # do not collapse equally, so they do not share a threshold.
    #
    # Everything is relative to the clip's OWN median, which is why dropping
    # from 1.03 MP to 0.70 MP barely moves the ratios. What does move them is
    # CONTENT: a sparse set -- a plain desk against a plain wall -- has a lower
    # room median to begin with, so the gap to a blank portrait narrows. Read
    # the table, not only the verdict. A dip that does not trip the thresholds
    # is still a dip.
    #
    # Edges is the discriminator (0.46 against 0.81); colours is a veto with
    # only ten points of separation, and is there so that one measure alone
    # cannot fire. That AND is what keeps the tight colour margin harmless.
    ap.add_argument("--drop-edges", type=float, default=0.50,
                    help="flag below this fraction of the clip's median edge "
                         "density (default 0.50; room floor measured at 0.88)")
    ap.add_argument("--drop-colours", type=float, default=0.80,
                    help="and below this fraction of median colours "
                         "(default 0.80; portrait ceiling 0.75, room floor 0.86)")
    ap.add_argument("--csv", default=None, help="also write the series here")
    a = ap.parse_args(argv)

    rows = [(t, *measure(f)) for t, f in read_frames(a.video, a.stride)]
    if not rows:
        print("no frames decoded", file=sys.stderr)
        return 2

    ed = np.array([r[1] for r in rows])
    co = np.array([r[2] for r in rows], dtype=float)
    med_e, med_c = float(np.median(ed)), float(np.median(co))

    print(f"{a.video}: {len(rows)} samples every {a.stride:g}s")
    print(f"median edges {med_e:.2f}%  median colours {med_c:.0f}")
    print()
    print("     t      edges   colours")
    # Both measures have to collapse. Either alone has an innocent reading -- a
    # tight close-up loses edges, a dim shot loses colours -- and requiring both
    # is what separates "a different shot" from "a quiet moment".
    suspect = []
    for t, e, c in rows:
        bad = e < med_e * a.drop_edges and c < med_c * a.drop_colours
        if bad:
            suspect.append(t)
        print(f"  {t:6.2f}   {e:6.2f}   {c:7d}   {'<-- OFF THE SET' if bad else ''}")

    if a.csv:
        with open(a.csv, "w", encoding="utf-8") as fh:
            fh.write("t,edges_pct,colours_5bit\n")
            for t, e, c in rows:
                fh.write(f"{t:.3f},{e:.4f},{c}\n")
        print(f"\nseries written to {a.csv}")

    print()
    if not suspect:
        print("No window looks like a different shot. Every sample holds at "
              f"least {a.drop_edges:.0%} of the clip's median edges or "
              f"{a.drop_colours:.0%} of its median colours.")
        return 0

    # Contiguous runs, because one flagged sample is a frame and a run is an event.
    runs, start, prev = [], suspect[0], suspect[0]
    for t in suspect[1:]:
        if t - prev > a.stride * 1.5:
            runs.append((start, prev))
            start = t
        prev = t
    runs.append((start, prev))

    print("SUSPECT WINDOWS -- both edges and colours collapsed:")
    for s, e in runs:
        print(f"  {s:.2f}s to {e + a.stride:.2f}s  ({e + a.stride - s:.2f}s)")
    print()
    print("That is the signature of the chain rendering a reference photograph "
          "instead of the scene. Check those seconds by eye before believing "
          "it -- a genuine cut to a flat wall reads the same way.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
