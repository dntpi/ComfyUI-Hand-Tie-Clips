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
import json
import os
import sys
import types

# Derived from this file's position (pack/tools/ -> custom_nodes/pack ->
# ComfyUI root), so the probe works on a checkout that is not this machine's.
# Matches the root h3_ref_chain.py builds from folder_paths.get_temp_directory().
_COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_ROOT = os.path.join(_COMFY_ROOT, "temp", "h3_ref_chain_hops")


def _load_store():
    """Import the pack's own store.py without installing the pack.

    The directory has dashes, so it is not a legal module name; register a
    synthetic package pointing at it. Same trick the offline tests use.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pkg = types.ModuleType("h3pack")
    pkg.__path__ = [here]
    sys.modules["h3pack"] = pkg
    from h3pack import store  # noqa: PLC0415
    return store


def _hops(store, root):
    """Cached hops as [(hop_index, key)], ordered, skipping unreadable meta."""
    out = []
    for key, _size, _mtime in store.HopStore(root).entries():
        meta_path = os.path.join(root, key + store.META_EXT)
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError) as e:
            print(f"  ! {key[:8]}: unreadable meta ({e!r}), skipped")
            continue
        hop = meta.get("hop")
        if hop is None:
            print(f"  ! {key[:8]}: meta has no hop index, skipped")
            continue
        out.append((int(hop), key))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--overlap", type=int, default=22,
                    help="Frames pinned per join. Must match the render.")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print(f"No hop cache at {args.root}.\n"
              f"Render with cache_hops=on and probe before restarting ComfyUI "
              f"(temp/ is wiped on start).")
        return 1

    store = _load_store()
    import torch  # noqa: PLC0415  -- after store, so the venv check fails first

    hops = _hops(store, args.root)
    if len(hops) < 2:
        print(f"Found {len(hops)} cached hop(s) in {args.root}; need at least 2.")
        return 1

    print(f"{len(hops)} hops in {args.root}, overlap {args.overlap}f\n")
    st = store.HopStore(args.root)
    ov = int(args.overlap)

    prev = None
    cumulative = torch.zeros(3)
    for hop, key in hops:
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
