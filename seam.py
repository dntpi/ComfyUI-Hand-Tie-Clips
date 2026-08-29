"""Seam report: measure the joins this pack exists to hide.

The whole claim of Hand Tie Clips is that you cannot see where one generation
ends and the next begins. Until now the only instrument for that was
`tools/seam_probe.py`, a command-line script that decodes an mp4 -- so the
person best placed to check the claim, the one who just rendered a chain, had
no way to.

This is that measurement as a node. Wire the chain's `images` straight into it.

WHAT IT MEASURES, and what it does not: a seam's brightness step is read as the
mean of `window` frames either side of the cut. Those frames are ~0.9 s apart in
scene time, so a real change in the shot -- someone crossing the light, a hand
entering frame -- lands in the number too. A single reading is therefore an
UPPER BOUND on the seam step, not a clean measurement of it. To isolate the
seam, render the same chain twice with the same seed and cache, changing only
tone_compensate, and compare: the content change is identical in both, so the
difference between them is the correction.

Seam positions are derived, not guessed: hop 1 contributes its whole length and
every later hop contributes `length - overlap`, so
    hop_len = (total_frames + (hops - 1) * overlap) / hops
Per-shot `duration` overrides break that assumption -- the report says so when
the arithmetic does not land on whole frames.
"""
from __future__ import annotations

import torch

from . import sheet as _sheet

TAG = "HTCSeam"

# Judgement thresholds, in 1/255 units of mean luma. These are read off the
# chains this pack has actually measured, not from a standard: the 8x15s
# reference chain sat under 1.0 on 7 of 7 seams and nobody could find a join by
# eye. Treat them as a triage, and remember the content-change caveat above --
# a "visible" reading on a shot that genuinely changes brightness is honest.
T_INVISIBLE = 1.0
T_MARGINAL = 2.5


def frame_means(images):
    """Per-frame mean RGB from an IMAGE tensor. -> [N,3] float32 on CPU."""
    return images.detach().float().mean(dim=(1, 2)).cpu()


def seam_positions(total, hops, overlap):
    """Index of the first frame belonging to each hop after the first."""
    if hops < 2:
        return [], None
    hop_len = (float(total) + (hops - 1) * float(overlap)) / float(hops)
    cuts = [int(round(hop_len + k * (hop_len - overlap))) for k in range(hops - 1)]
    return cuts, hop_len


def measure(images, hops, overlap, window):
    """-> (rows, hop_len, note). rows: dicts with seam, at, rgb, luma, verdict."""
    means = frame_means(images)
    total = int(means.shape[0])
    cuts, hop_len = seam_positions(total, int(hops), int(overlap))
    note = ""
    if hop_len is not None and abs(hop_len - round(hop_len)) > 0.01:
        note = (f"hop length works out to {hop_len:.2f} frames, which is not a "
                f"whole number -- the hops are probably not all the same "
                f"duration, so the seam positions below are approximate.")
    rows = []
    for k, c in enumerate(cuts, 1):
        a = means[max(0, c - int(window)):c]
        b = means[c:c + int(window)]
        if not len(a) or not len(b):
            rows.append({"seam": k, "at": c, "rgb": None, "luma": None,
                         "verdict": "too close to an edge, skipped"})
            continue
        step = (b.mean(dim=0) - a.mean(dim=0))
        luma = float(step.mean()) * 255.0
        mag = abs(luma)
        verdict = ("invisible" if mag < T_INVISIBLE
                   else "marginal" if mag < T_MARGINAL else "VISIBLE")
        rows.append({"seam": k, "at": int(c),
                     "rgb": [float(v) * 255.0 for v in step.tolist()],
                     "luma": luma, "verdict": verdict})
    return rows, hop_len, note


def format_report(rows, total, hops, overlap, window, hop_len, note):
    L = []
    L.append(f"Seam report: {total} frames, {hops} hops, overlap {overlap}, "
             f"window {window}")
    if hop_len:
        L.append(f"  derived hop length {hop_len:.1f} frames "
                 f"({hop_len / 24.0:.2f} s at 24 fps)")
    if note:
        L.append(f"  NOTE {note}")
    L.append("")
    scored = [r for r in rows if r["luma"] is not None]
    for r in rows:
        if r["luma"] is None:
            L.append(f"  seam {r['seam']} @ f{r['at']}: {r['verdict']}")
            continue
        rgb = " ".join(f"{c}{v:+.2f}" for c, v in zip("rgb", r["rgb"]))
        L.append(f"  seam {r['seam']} @ f{r['at']}: {rgb}   "
                 f"luma {r['luma']:+.2f}/255   {r['verdict']}")
    if scored:
        mags = [abs(r["luma"]) for r in scored]
        drift = sum(r["luma"] for r in scored)
        worst = max(scored, key=lambda r: abs(r["luma"]))
        L.append("")
        L.append(f"  mean |step| {sum(mags) / len(mags):.2f}/255   "
                 f"worst seam {worst['seam']} at {worst['luma']:+.2f}/255")
        L.append(f"  sum of steps {drift:+.2f}/255 "
                 f"-- this is the chain's cumulative brightness drift; a large "
                 f"one-signed total is what tone_compensate=anchor is for.")
        n_vis = sum(1 for r in scored if r["verdict"] == "VISIBLE")
        L.append(f"  {len(scored) - n_vis} of {len(scored)} seams under "
                 f"{T_MARGINAL}/255.")
    L.append("")
    L.append("  A single reading includes whatever the scene did across the "
             "cut. To isolate the seam itself, A/B two renders from the same "
             "seed and cache.")
    return chr(10).join(L)


def chart(rows, width=880):
    """A bar per seam, signed, with the two thresholds drawn. -> IMAGE tensor."""
    try:
        from PIL import Image, ImageDraw
        import numpy as np

        scored = [r for r in rows if r["luma"] is not None]
        if not scored:
            return _sheet.placeholder()

        pad, bar_gap, h = 44, 10, 300
        mid = h // 2
        span = max(T_MARGINAL * 1.6, max(abs(r["luma"]) for r in scored) * 1.25)
        img = Image.new("RGB", (width, h), _sheet.BG)
        d = ImageDraw.Draw(img)
        f = _sheet._font(12)
        fb = _sheet._font(14, bold=True)

        def y_of(v):
            return int(mid - (v / span) * (mid - pad))

        # thresholds
        for t, col in ((T_INVISIBLE, (70, 110, 70)), (T_MARGINAL, (120, 100, 50))):
            for sgn in (1, -1):
                yy = y_of(t * sgn)
                d.line([pad, yy, width - pad, yy], fill=col)
            d.text((width - pad + 4, y_of(t) - 7), f"{t:g}", font=f, fill=col)
        d.line([pad, mid, width - pad, mid], fill=(90, 90, 90))
        d.text((8, mid - 7), "0", font=f, fill=_sheet.DIM)

        n = len(scored)
        avail = width - pad * 2
        bw = max(6, int(avail / max(1, n)) - bar_gap)
        for i, r in enumerate(scored):
            x = pad + int(i * (avail / max(1, n))) + bar_gap // 2
            yy = y_of(r["luma"])
            col = ((110, 200, 120) if r["verdict"] == "invisible"
                   else (230, 200, 90) if r["verdict"] == "marginal"
                   else (240, 110, 80))
            top, bot = (yy, mid) if r["luma"] >= 0 else (mid, yy)
            d.rectangle([x, top, x + bw, bot], fill=col)
            d.text((x, h - 30), f"s{r['seam']}", font=f, fill=_sheet.DIM)
            d.text((x, h - 16), f"{r['luma']:+.1f}", font=f, fill=_sheet.DIM)
        d.text((pad, 10), "seam step, mean luma /255", font=fb, fill=_sheet.ACCENT)

        a = np.asarray(img, dtype=np.float32) / 255.0
        return torch.from_numpy(a).unsqueeze(0)
    except Exception as e:
        print("[%s] chart skipped (%s: %s)" % (TAG, type(e).__name__, e), flush=True)
        return _sheet.placeholder()


class HTCSeamReport:
    """Measure the brightness step at every join of a finished chain."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "The chain's `images` output -- the joined master."}),
                "hops": ("INT", {
                    "default": 3, "min": 2, "max": 64,
                    "tooltip": "How many hops were joined. Must match the render or the seam positions are wrong.",
                }),
                "overlap": ("INT", {
                    "default": 22, "min": 1, "max": 4096,
                    "tooltip": "Frames trimmed at each join. 22 = the 0.9 s default; 5 = 0.2 s; 39 = 1.6 s.",
                }),
                "window": ("INT", {
                    "default": 6, "min": 1, "max": 120,
                    "tooltip": "Frames averaged either side of the cut. Wider is steadier but folds in more real scene change.",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "IMAGE")
    RETURN_NAMES = ("report", "chart")
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = (
        "Measures the brightness step at each seam of a joined chain and says "
        "whether it is invisible, marginal or visible. Wire `images` from "
        "HandTieClips. A single reading includes real scene change across the "
        "cut, so treat it as an upper bound; A/B two renders to isolate the seam."
    )

    def run(self, images, hops, overlap, window):
        total = int(images.shape[0])
        rows, hop_len, note = measure(images, hops, overlap, window)
        report = format_report(rows, total, int(hops), int(overlap),
                               int(window), hop_len, note)
        print("[%s]%s%s" % (TAG, chr(10), report), flush=True)
        return (report, chart(rows))


NODE_CLASS_MAPPINGS = {"HTCSeamReport": HTCSeamReport}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCSeamReport": "H3 Seam Report"}
