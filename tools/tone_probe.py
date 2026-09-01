"""Measure the real per-hop tone drift, from the hop cache.

Why the cache and not the rendered master: the estimate has to pair hop N's last
`overlap` frames with hop N+1's *first* `overlap` frames, which are the model's
regeneration of the same content. The join drops that second copy
(`h3_ref_chain.py`, `master_imgs[...] = imgs[overlap_n:]`), so it does not exist
in the output video. It does exist in the hop cache, which stores each hop's
full pre-trim frames as lossless 16-bit FFV1 -- which makes the cache the only
honest instrument for this measurement.

Comparing frames either side of the seam in a finished master is NOT the same
measurement: those frames are ~0.9 s apart in scene time, so the number includes
whatever the scene did in between.

Usage, from the pack directory:

    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\tone_probe.py [--overlap 22] [--root PATH]

ComfyUI wipes `temp/` on startup, so this only sees a cache written by the
currently running session: render with `cache_hops=on` and probe before you
restart.

Read-only apart from one side effect worth knowing: `HopStore.get()` touches
each entry's mtime for LRU, so probing marks every hop as recently used.
"""
from __future__ import annotations

import argparse

# The cache reader lives in hopcache.py because texture_probe.py needs the
# same chain segmentation, and a second copy of it is how the
# two-renders-differenced-as-one bug comes back.
import hopcache
from hopcache import DEFAULT_ROOT


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--overlap", type=int, default=22,
                    help="Frames pinned per join. Must match the render.")
    ap.add_argument("--chain", type=int, default=None,
                    help="Which cached render to read when the directory holds "
                         "more than one (1 = oldest). Default: the newest.")
    args = ap.parse_args()

    store, hops, report = hopcache.select(args.root, args.chain)
    import torch  # noqa: PLC0415  -- after store, so the venv check fails first

    if report:
        # Never silently pick one. The numbers below are only meaningful for
        # a single render, and which one was read changes the answer.
        print(report)

    if len(hops) < 2:
        print(f"Found {len(hops)} cached hop(s) in the selected render; "
              f"need at least 2.")
        return 1

    print(f"{len(hops)} hops in {args.root}, overlap {args.overlap}f\n")
    st = store.HopStore(args.root)
    ov = int(args.overlap)

    prev = None
    cumulative = torch.zeros(3)
    for hop, key, _meta in hops:
        got = st.get(key)
        if got is None:
            print(f"hop {hop}: entry {key[:8]} incomplete, skipped")
            continue
        imgs = got[0]
        if prev is None:
            print(f"hop {hop} ({key[:8]}): {imgs.shape[0]}f  "
                  f"mean {float(imgs.mean()):.4f}  [reference]")
            prev = imgs
            continue

        n = min(ov, prev.shape[0], imgs.shape[0])
        src = prev[-n:]           # what hop N ended on
        tgt = imgs[:n]            # hop N+1's regeneration of it
        # Per channel over the matched overlap: this is exactly what
        # tone.compensate's frame_shift mode fits.
        drift = tgt.mean(dim=(0, 1, 2)) - src.mean(dim=(0, 1, 2))
        cumulative = cumulative + drift
        luma = float(drift.mean())
        print(f"hop {hop} ({key[:8]}): {imgs.shape[0]}f  "
              f"mean {float(imgs.mean()):.4f}")
        print(f"    drift vs hop {hop - 1} over {n}f: "
              + " ".join(f"{c}{v:+.5f}" for c, v in zip("rgb", drift.tolist()))
              + f"   luma {luma:+.5f} ({luma * 255:+.2f}/255)")
        print(f"    cumulative from hop 1:            "
              + " ".join(f"{c}{v:+.5f}" for c, v in zip("rgb", cumulative.tolist()))
              + f"   luma {float(cumulative.mean()):+.5f} "
                f"({float(cumulative.mean()) * 255:+.2f}/255)")
        prev = imgs

    total = float(cumulative.mean())
    print(f"\nTotal drift across the chain: {total:+.5f} ({total * 255:+.2f}/255)")
    print("Positive = later hops are brighter. A step below ~1/255 is not worth "
          "correcting; the correction itself costs a clamp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
