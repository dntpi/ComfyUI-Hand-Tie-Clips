r"""Measure the brightness step at each seam of a finished master.

This is the *after* instrument. `tone_probe.py` reads the hop cache, which
stores raw pre-correction hops, so it always reports the same raw drift whether
or not compensation is on -- by design (the mode stays out of the hop key).
The correction is only visible in the delivered video, which is what this reads.

A single file's seam step is contaminated by real content change across the cut:
the frames either side are ~0.9 s apart in scene time. Pass two masters rendered
from the same seed and cache and that contamination is identical in both, so the
DIFFERENCE between them is a clean read on what the correction did.

    D:\ComfyUI\venv\Scripts\python.exe tools\seam_probe.py --hops 3 before.mp4 after.mp4

Seam positions are derived, not guessed: hop 1 contributes its whole length and
every later hop contributes length - overlap, so
    hop_len = (total_frames + (hops - 1) * overlap) / hops
"""
from __future__ import annotations

import argparse
import os

import av
import numpy as np


def frame_means(path):
    """Per-frame mean RGB. -> [N,3] float64."""
    out = []
    with av.open(path) as container:
        for frame in container.decode(video=0):
            out.append(frame.to_ndarray(format="rgb24").mean(axis=(0, 1)))
    return np.asarray(out, dtype=np.float64) / 255.0


def seams(total, hops, overlap):
    """Frame index of the first frame belonging to each later hop."""
    if hops < 2:
        return []
    hop_len = (total + (hops - 1) * overlap) / hops
    return [int(round(hop_len + k * (hop_len - overlap))) for k in range(hops - 1)]


def report(path, hops, overlap, window):
    means = frame_means(path)
    n = len(means)
    cuts = seams(n, hops, overlap)
    print(f"{os.path.basename(path)}: {n}f, seams at {cuts}")
    steps = []
    for k, c in enumerate(cuts, 1):
        a = means[max(0, c - window):c]
        b = means[c:c + window]
        if not len(a) or not len(b):
            print(f"  seam {k} @ {c}: too close to an edge, skipped")
            continue
        step = b.mean(axis=0) - a.mean(axis=0)
        luma = float(step.mean())
        steps.append(luma)
        print(f"  seam {k} @ {c}: "
              + " ".join(f"{ch}{v:+.5f}" for ch, v in zip("rgb", step))
              + f"   luma {luma:+.5f} ({luma * 255:+.2f}/255)")
    if steps:
        tot = sum(steps)
        print(f"  sum of steps: {tot:+.5f} ({tot * 255:+.2f}/255)   "
              f"mean |step| {np.mean(np.abs(steps)) * 255:.2f}/255")
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="master mp4(s), oldest first")
    ap.add_argument("--hops", type=int, required=True)
    ap.add_argument("--overlap", type=int, default=22)
    ap.add_argument("--window", type=int, default=6,
                    help="Frames averaged either side of the cut.")
    args = ap.parse_args()

    all_steps = []
    for p in args.files:
        if not os.path.isfile(p):
            print(f"{p}: not found")
            return 1
        all_steps.append(report(p, args.hops, args.overlap, args.window))
        print()

    if len(all_steps) == 2 and all_steps[0] and all_steps[1]:
        print("A/B (same seed + cache, so content change cancels):")
        for k, (a, b) in enumerate(zip(*all_steps), 1):
            print(f"  seam {k}: {a * 255:+.2f}/255 -> {b * 255:+.2f}/255   "
                  f"({(abs(b) - abs(a)) * 255:+.2f}/255 magnitude)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
