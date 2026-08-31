r"""Measure the texture ratchet: how detail energy drifts along a chain.

    D:\ComfyUI\venv\Scripts\python.exe tools\texture_probe.py
    D:\ComfyUI\venv\Scripts\python.exe tools\texture_probe.py --video clip.mp4

The complaint this exists to measure is "overcooked going into the hops" --
skin going blotchy, hair frizzing into noise, faces restructuring by hop 4.

**Why not mean |Laplacian|.** That is the obvious metric and it is confounded
twice over. Measured on two 44 s H3 chains whose faces are visibly destroyed by
the end, whole-frame mean |Laplacian| end-vs-start came out at 0.961 and 1.017
-- flat, one of them negative. Two reasons. It is an area average, so a face
worth 6% of a portrait frame is outvoted by wood panelling that did not change;
and it sums every spatial frequency into one number, so energy moving *between*
bands cancels. On those same clips global contrast FELL while mid-band energy
ROSE. A single scalar cannot represent that, and a fix tuned against one is
tuned against noise.

So this reports three bands, separately, in two boxes:

  fine    sigma < 1 px    grain, encoder noise, real fine detail
  mid     1 - 2.5 px      **the one that moves** -- blotch, mottle, "baked"
  coarse  2.5 - 6 px      form and shading; largely tracks content

and a subject box against a background box, because the numbers are global but
the *acceptance threshold* lives on the face. Whole-frame reporting is what let
a destroyed face read as 0.96 in the first place; this probe should not be able
to make that mistake quietly.

It also reports the within-hop slope, not just the per-hop mean. On measured
chains the climb is continuous through each hop with no step at the join -- the
hop boundary is not where the damage is injected, it is where the damaged tail
is handed forward as the next hop's context. A per-hop mean alone would hide
that, and it is the fact that decides where a correction belongs.

Two sources:

  * the hop cache (default). Each hop is stored as lossless 16-bit FFV1 BEFORE
    tone compensation runs, so this sees what the sampler produced rather than
    what survived correction and an h264 re-encode. ComfyUI wipes `temp/` on
    startup: render with `cache_hops=on` and probe before you restart.
  * `--video`, any file PyAV can open, for clips whose cache is long gone and
    for rigs that are not this pack at all.

Runs on CPU deliberately. It is meant to be usable while a render is queued.
"""
from __future__ import annotations

import argparse
import os
import sys

import hopcache

BANDS = ("fine", "mid", "coarse")
# Gaussian sigmas, in pixels, whose differences define the three bands.
SIGMAS = (1.0, 2.5, 6.0)


def _torch():
    import torch  # noqa: PLC0415  -- imported late so a venv error surfaces first
    return torch


def _gauss1d(sigma, torch):
    r = max(1, int(round(3.0 * sigma)))
    x = torch.arange(-r, r + 1, dtype=torch.float32)
    k = torch.exp(-(x * x) / (2.0 * sigma * sigma))
    return k / k.sum()


def _blur(x, sigma, torch):
    """Separable Gaussian over [N,H,W].

    Reflect-padded, so the frame edge does not manufacture a band-energy step
    of its own.
    """
    import torch.nn.functional as F  # noqa: PLC0415
    k = _gauss1d(sigma, torch)
    pad = k.numel() // 2
    y = x.unsqueeze(1)
    y = F.conv2d(F.pad(y, (pad, pad, 0, 0), mode="reflect"), k.view(1, 1, 1, -1))
    y = F.conv2d(F.pad(y, (0, 0, pad, pad), mode="reflect"), k.view(1, 1, -1, 1))
    return y.squeeze(1)


def bands(gray, torch):
    """Per-frame band energies for [N,H,W] luma in 0..1. -> dict of [N] tensors.

    Band energy is the mean absolute difference between two Gaussians, which is
    a plain band-pass: it does not care about the local mean, so it measures
    texture rather than exposure. That independence is the point -- on the
    clips that motivated this, exposure anchoring was on and holding luma flat
    while these numbers climbed regardless.
    """
    b1 = _blur(gray, SIGMAS[0], torch)
    b2 = _blur(gray, SIGMAS[1], torch)
    b3 = _blur(gray, SIGMAS[2], torch)
    return {
        "fine": (gray - b1).abs().mean(dim=(1, 2)),
        "mid": (b1 - b2).abs().mean(dim=(1, 2)),
        "coarse": (b2 - b3).abs().mean(dim=(1, 2)),
    }


def luma(imgs, torch):
    """[N,H,W,3] in 0..1 -> [N,H,W] Rec.709 luma."""
    w = torch.tensor([0.2126, 0.7152, 0.0722], dtype=torch.float32)
    return (imgs.float() * w).sum(dim=-1)


def laplacian(gray):
    """Mean |Laplacian| per frame -- the metric this probe exists to replace.

    Reported so the comparison is on screen rather than asserted. It is the
    natural first choice and it is the one that reads 0.96 on a chain whose
    face has come apart, which is a more convincing argument against it than
    any amount of prose.
    """
    c = gray[:, 1:-1, 1:-1]
    lap = (4.0 * c - gray[:, :-2, 1:-1] - gray[:, 2:, 1:-1]
           - gray[:, 1:-1, :-2] - gray[:, 1:-1, 2:])
    return lap.abs().mean(dim=(1, 2))


def slope_pct(v, torch):
    """Least-squares slope of v against frame index, as a percentage of its mean.

    Reported per 100 sampled frames so it is comparable between hops of
    different lengths -- the within-hop growth rate, independent of how long
    the hop is.
    """
    n = int(v.numel())
    if n < 8:
        return 0.0
    x = torch.arange(n, dtype=torch.float32)
    x = x - x.mean()
    m = float(v.mean())
    if m <= 0:
        return 0.0
    per_frame = float((x * (v - v.mean())).sum() / (x * x).sum()) / m
    # x100 for "per 100 frames", x100 again for "as a percentage". Getting this
    # to one factor of 100 reports a per-frame figure under a per-100-frame
    # label, which reads as a negligible slope on a hop that doubled.
    return per_frame * 100.0 * 100.0


def parse_box(s, h, w):
    """Parse "y0,y1,x0,x1" in pixels. -> tuple, or None for the whole frame."""
    if not s:
        return None
    try:
        y0, y1, x0, x1 = (int(v) for v in str(s).split(","))
    except ValueError:
        raise SystemExit(f"box wants y0,y1,x0,x1 in pixels; got {s!r}")
    y0, x0 = max(0, y0), max(0, x0)
    y1, x1 = min(int(h), y1), min(int(w), x1)
    if y1 - y0 < 8 or x1 - x0 < 8:
        raise SystemExit(f"box {s!r} is empty or off-frame for a {w}x{h} clip")
    return y0, y1, x0, x1


def default_boxes(h, w):
    """Head box and background box for an unannotated clip.

    A guess, and the output says so. For a portrait clip the subject's head
    sits in the upper middle; the background box takes an upper corner, which
    in a talking-head setup is the part least likely to be occluded.
    """
    head = (int(h * 0.06), int(h * 0.43), int(w * 0.26), int(w * 0.84))
    bg = (0, int(h * 0.23), 0, int(w * 0.24))
    return head, bg


def measure(gray, torch, boxes):
    """-> {box name: {band: [N] tensor}}."""
    out = {}
    for name, box in boxes.items():
        g = gray if box is None else gray[:, box[0]:box[1], box[2]:box[3]]
        out[name] = bands(g, torch)
    return out


def _row(label, per_band, ref, slopes):
    bits = []
    for b in BANDS:
        v = float(per_band[b].mean())
        if ref is None:
            bits.append(f"{b} {v:.4f}        ")
        else:
            r = v / ref[b] if ref[b] else float("nan")
            bits.append(f"{b} {v:.4f} x{r:.3f}")
    tail = ""
    if slopes is not None:
        tail = "   slope/100f " + " ".join(
            f"{b[0]}{slopes[b]:+.1f}%" for b in BANDS)
    return f"    {label:<8}" + "  ".join(bits) + tail


def boxes_for(h, w, box_s, bgbox_s):
    hb, bb = default_boxes(h, w)
    return {"head": parse_box(box_s, h, w) or hb,
            "bg": parse_box(bgbox_s, h, w) or bb,
            "whole": None}


# -- sources ---------------------------------------------------------------

def from_video(path, torch, stride):
    import av  # noqa: PLC0415
    if not os.path.isfile(path):
        raise SystemExit(f"No such video: {path}")
    c = av.open(path)
    s = c.streams.video[0]
    s.thread_type = "AUTO"
    frames = []
    for i, f in enumerate(c.decode(s)):
        if i % stride:
            continue
        frames.append(torch.from_numpy(
            f.to_ndarray(format="rgb24").astype("float32") / 255.0))
    c.close()
    if len(frames) < 8:
        raise SystemExit(f"{path}: only {len(frames)} frame(s) after stride "
                         f"{stride}; nothing to fit.")
    return torch.stack(frames)


# -- latent side -----------------------------------------------------------

def latent_stats(latent, torch):
    """Per-component sigma, plus a high-band sigma for the video component.

    This is the measurement that decides whether a latent-space lever can work
    at all. `pin_renorm` today matches one scalar sigma per component; if the
    drift is a redistribution BETWEEN bands, sigma can sit still -- or move the
    other way -- while the picture bakes. Printed next to the pixel bands so
    the two can be compared rather than assumed to agree.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    import latents as _lat  # noqa: PLC0415
    got = _lat.from_dict(latent) if isinstance(latent, dict) else None
    if got is None:
        return None
    rows = []
    for t in got:
        t = t.float()
        row = {"sigma": float(t.std()), "hi": None}
        # Only a component with real spatial extent has bands to speak of; the
        # audio stream is a different object and a blur across it means nothing.
        if t.dim() >= 3 and t.shape[-1] >= 16 and t.shape[-2] >= 16:
            flat = t.reshape(-1, t.shape[-2], t.shape[-1])
            if flat.shape[0] > 64:
                flat = flat[:64]
            row["hi"] = float((flat - _blur(flat, 2.0, torch)).std())
        rows.append(row)
    return rows


# -- the two runners -------------------------------------------------------

def run_video(args, torch, stride):
    imgs = from_video(args.video, torch, stride)
    n, h, w = int(imgs.shape[0]), int(imgs.shape[1]), int(imgs.shape[2])
    boxes = boxes_for(h, w, args.box, args.bgbox)
    print(f"{args.video}: {n} sampled frames (stride {stride}) of {w}x{h}\n")
    for k in ("head", "bg"):
        given = args.box if k == "head" else args.bgbox
        b = boxes[k]
        print(f"  {k:<5} box y{b[0]}-{b[1]} x{b[2]}-{b[3]}"
              f"  ({'given' if given else 'DEFAULT GUESS'})")
    if not (args.box and args.bgbox):
        print("  A default box is a guess about framing. If the subject is not "
              "inside it,\n  pass --box y0,y1,x0,x1 -- these numbers are only "
              "about what is in the box.")
    print()

    per = measure(luma(imgs, torch), torch, boxes)
    segs = max(2, int(args.segments))
    edges = [round(i * n / segs) for i in range(segs + 1)]

    for name in ("head", "bg", "whole"):
        vals = per[name]
        ref = {b: float(vals[b][edges[0]:edges[1]].mean()) for b in BANDS}
        print(f"  [{name}]")
        for i in range(segs):
            a, z = edges[i], edges[i + 1]
            if z - a < 2:
                continue
            print(_row(f"seg {i + 1}", {b: vals[b][a:z] for b in BANDS},
                       None if i == 0 else ref, None))
        print()

    def ends(v):
        return float(v[edges[-2]:].mean()) / float(v[edges[0]:edges[1]].mean())

    print(f"  mid-band end/start:  head x{ends(per['head']['mid']):.3f}"
          f"   bg x{ends(per['bg']['mid']):.3f}"
          f"   whole x{ends(per['whole']['mid']):.3f}")
    lap = laplacian(luma(imgs, torch))
    print(f"  whole-frame mean |Laplacian| end/start: x{ends(lap):.3f}"
          f"   <- the confounded metric, for comparison")
    return 0


def run_cache(args, torch, stride):
    store, hops, report = hopcache.select(args.root, args.chain)
    if report:
        print(report)
    if len(hops) < 2:
        print(f"Found {len(hops)} cached hop(s) in the selected render; "
              f"need at least 2.")
        return 1
    print(f"{len(hops)} hops in {args.root} (stride {stride})\n")
    print("Ratios are against hop 1 -- the only texture in the chain nobody "
          "drifted into,\nthe same anchor tone.py uses for exposure.\n")

    st = store.HopStore(args.root)
    ref = None
    first = hops[0][0]
    for hop, key, _meta in hops:
        got = st.get(key)
        if got is None:
            print(f"hop {hop}: entry {key[:8]} incomplete, skipped")
            continue
        imgs, _wav, _sr, latent = got
        imgs = imgs[::stride]
        h, w = int(imgs.shape[1]), int(imgs.shape[2])
        boxes = boxes_for(h, w, args.box, args.bgbox)
        g = luma(imgs, torch)
        per = measure(g, torch, boxes)
        if ref is None:
            ref = {k: {b: float(per[k][b].mean()) for b in BANDS} for k in per}
        print(f"hop {hop} ({key[:8]}): {imgs.shape[0]} sampled frames of "
              f"{w}x{h}   luma {float(g.mean()):.4f}")
        for name in ("head", "bg", "whole"):
            slopes = {b: slope_pct(per[name][b], torch) for b in BANDS}
            print(_row(name, per[name],
                       None if hop == first else ref[name], slopes))
        rows = latent_stats(latent, torch)
        if rows is None:
            print("    latent  (none cached for this hop)")
        else:
            bits = []
            for i, r in enumerate(rows):
                hi = f" hi {r['hi']:.4f}" if r["hi"] is not None else ""
                bits.append(f"[{i}] sigma {r['sigma']:.4f}{hi}")
            print("    latent  " + "  ".join(bits))
        print()

    print("Read `mid`, in the head box, first. If mid climbs while coarse and "
          "luma sit\nstill, that is the ratchet and not the scene changing. A "
          "positive slope inside\nevery hop means the growth is within-hop: "
          "the join carries it forward rather\nthan creating it.\n")
    print("On the latent row: if sigma sits still while the pixel mid band "
          "climbs, then\nmatching sigma (`pin_renorm=on`) cannot fix this and "
          "a band-aware lever is\nthe one worth building.")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=hopcache.DEFAULT_ROOT)
    ap.add_argument("--chain", type=int, default=None,
                    help="Which cached render to read (1 = oldest). "
                         "Default: the newest.")
    ap.add_argument("--video", default=None,
                    help="Measure a video file instead of the hop cache.")
    ap.add_argument("--stride", type=int, default=4,
                    help="Measure every Nth frame. 1 is exact and slow.")
    ap.add_argument("--box", default=None, help="Subject box, y0,y1,x0,x1.")
    ap.add_argument("--bg-box", dest="bgbox", default=None,
                    help="Background control box, y0,y1,x0,x1.")
    ap.add_argument("--segments", type=int, default=8,
                    help="--video only: split the clip into N equal parts.")
    args = ap.parse_args()

    torch = _torch()
    torch.set_grad_enabled(False)
    stride = max(1, int(args.stride))
    if args.video:
        return run_video(args, torch, stride)
    return run_cache(args, torch, stride)


if __name__ == "__main__":
    raise SystemExit(main())
