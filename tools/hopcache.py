r"""Reading the hop cache: locating it, loading it, and splitting it into runs.

Shared by `tone_probe.py` and `texture_probe.py`. It lives in its own file
because of what `chains()` does. The cache is keyed by content, not by run, so
one directory routinely holds several renders at once, and an instrument that
differences straight down a hop-sorted list pairs hop 2 of one render against
hop 1 of another and reports the gap between two unrelated scenes as drift --
a large, plausible, entirely fictional number. That bug shipped once. Two
probes must not each carry their own copy of the code that prevents it.

ComfyUI wipes `temp/` on startup, so a cache only ever describes the currently
running session: render with `cache_hops=on` and probe before you restart.
"""
from __future__ import annotations

import json
import os
import sys
import types

# Derived from this file's position (pack/tools/ -> custom_nodes/pack ->
# ComfyUI root), so the probes work on a checkout that is not this machine's.
# Matches the root h3_ref_chain.py builds from folder_paths.get_temp_directory().
_COMFY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_ROOT = os.path.join(_COMFY_ROOT, "temp", "h3_ref_chain_hops")


def load_store():
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


def hops(store, root):
    """Cached hops as [(hop_index, key, meta)], oldest first."""
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
        out.append((int(hop), key, meta))
    # `written` is the render order. Sorting by hop index instead would
    # interleave two runs that share the directory -- see chains().
    return sorted(out, key=lambda t: (float(t[2].get("written") or 0.0), t[0]))


def chains(all_hops):
    """Split cached hops into separate renders. -> [[(hop, key, meta), ...]].

    Hops of one render are written in order, so a hop index that does not
    advance means a new run started. That separates two runs of the SAME shape
    (a settings A/B) as well as two of different lengths, which grouping on
    frames/resolution alone would not.
    """
    out, cur, last = [], [], None
    for hop, key, meta in all_hops:
        if last is not None and hop <= last:
            out.append(cur)
            cur = []
        cur.append((hop, key, meta))
        last = hop
    if cur:
        out.append(cur)
    return out


def describe(chain):
    m = chain[0][2]
    return (f"{len(chain)} hop(s), {m.get('frames', '?')}f "
            f"@ {m.get('width', '?')}x{m.get('height', '?')}")


def select(root, which=None):
    """Pick one render out of the cache. -> (store_module, [hops], report).

    `report` is text the caller should print before its numbers: when the
    directory holds more than one render it lists them all and marks the one
    being read. Never silently picks -- which chain was read changes every
    number that follows, so it has to be on screen next to them.

    Raises SystemExit with a readable message rather than returning a sentinel,
    because every caller's response to "no usable cache" is to stop.
    """
    if not os.path.isdir(root):
        raise SystemExit(
            f"No hop cache at {root}.\n"
            f"Render with cache_hops=on and probe before restarting ComfyUI "
            f"(temp/ is wiped on start).")
    store = load_store()
    every = hops(store, root)
    runs = chains(every)
    if which is not None:
        if not 1 <= which <= len(runs):
            raise SystemExit(f"--chain {which} out of range; "
                             f"{len(runs)} chain(s) cached.")
        picked = runs[which - 1]
    else:
        picked = runs[-1] if runs else []

    report = ""
    if len(runs) > 1:
        lines = [f"{len(every)} cached hops in {root} span "
                 f"{len(runs)} separate renders:"]
        for i, c in enumerate(runs, 1):
            mark = " <- reading" if c is picked else ""
            lines.append(f"  chain {i}: {describe(c)}{mark}")
        lines.append("  Pick another with --chain N.\n")
        report = "\n".join(lines)
    return store, picked, report
