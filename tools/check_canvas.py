r"""Offline checks for the computed output canvas -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_canvas.py

1.0.x carried the canvas as fifteen hand-authored `(w, h)` tuples. 1.1 has
eleven aspects across five resolution rungs, which is fifty-five, so the table
became a function. This file is what makes that safe:

  * every size H3 can be asked for is on the 32 px grid on BOTH axes, under the
    768*1344 area cap, and close to the area and ratio its labels promise;
  * the retired 1.0.x labels still resolve to the sizes they always did, which
    is what stops a saved workflow silently re-rendering at new dimensions;
  * the two sizes the formula does NOT reproduce are pinned, on purpose, and
    stay pinned.

The grid check is not cosmetic. Core takes the generation canvas on trust and
floor-divides it by 16 (`_empty_av_latent`), so an off-grid size does not raise:
it builds a latent for a smaller frame than the master tensor was allocated for.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


# The 1.0.x table, copied here verbatim as the thing to reproduce. This is the
# only surviving copy: h3_ref_chain.py no longer has one.
LEGACY_TABLE = {
    "0.2 MP": {"16:9 landscape": (608, 352), "9:16 portrait": (352, 608),
               "1:1 square": (448, 448)},
    "0.3 MP": {"16:9 landscape": (736, 416), "9:16 portrait": (416, 736),
               "1:1 square": (544, 544)},
    "0.5 MP": {"16:9 landscape": (960, 544), "9:16 portrait": (544, 960),
               "1:1 square": (704, 704)},
    "0.7 MP": {"16:9 landscape": (1120, 640), "9:16 portrait": (640, 1120),
               "1:1 square": (832, 832)},
    "1.0 MP": {"16:9 landscape": (1280, 736), "9:16 portrait": (736, 1280),
               "1:1 square": (992, 992)},
}


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    canvas, aspects, resolutions = H3._canvas, H3.ASPECTS, H3.RESOLUTIONS
    cap, mult = H3.CANVAS_AREA_CAP, H3.CANVAS_MULTIPLE

    def quiet(res, asp):
        """_canvas explains itself on the legacy path; not wanted per cell."""
        with contextlib.redirect_stdout(io.StringIO()):
            return canvas(res, asp)

    print("the grid, the cap, and the promise on the label")
    off_grid, over_cap = [], []
    worst_area, worst_ratio = (0.0, ""), (0.0, "")
    for res in resolutions:
        for asp in aspects:
            w, h = quiet(res, asp)
            if w % mult or h % mult:
                off_grid.append(f"{res} {asp} {w}x{h}")
            if w * h > cap:
                over_cap.append(f"{res} {asp} {w}x{h}")
            want_mp = resolutions[res]
            got_mp = w * h / 1e6
            d_area = abs(got_mp - want_mp) / want_mp
            if d_area > worst_area[0]:
                worst_area = (d_area, f"{res} {asp} {w}x{h} = {got_mp:.3f} MP")
            rw, rh = aspects[asp]
            d_ratio = abs((w / h) - (rw / rh)) / (rw / rh)
            if d_ratio > worst_ratio[0]:
                worst_ratio = (d_ratio, f"{res} {asp} {w}x{h} = {w / h:.3f}")

    n = len(resolutions) * len(aspects)
    ck("every size is on the 32 px grid, both axes",
       not off_grid, f"{n} sizes" if not off_grid else off_grid[0])
    ck("every size is under H3's 768x1344 area cap",
       not over_cap, f"cap {cap}" if not over_cap else over_cap[0])
    ck("area is within 5% of the label",
       worst_area[0] < 0.05, f"worst {worst_area[0] * 100:.1f}%  {worst_area[1]}")
    ck("ratio is within 4% of the label",
       worst_ratio[0] < 0.04, f"worst {worst_ratio[0] * 100:.1f}%  {worst_ratio[1]}")

    print("\nthe 1.0.x labels still resolve to the 1.0.x sizes")
    for res, by_asp in LEGACY_TABLE.items():
        bad = [f"{asp} {quiet(res, asp)} != {wh}"
               for asp, wh in by_asp.items() if quiet(res, asp) != wh]
        ck(f"{res} reproduces its three 1.0.x tuples", not bad,
           "" if not bad else bad[0])

    print("\npinned vs computed, and which is which")

    def formula(res, asp):
        rw, rh = H3.ASPECTS[asp]
        return H3._fit_canvas(rw / rh, float(str(res).split()[0]))

    pinned = set(H3.LEGACY_CANVAS)
    agree, disagree = [], []
    for res, by_asp in LEGACY_TABLE.items():
        for asp, wh in by_asp.items():
            (agree if formula(res, asp) == wh else disagree).append((res, asp))
    # Both halves matter. A pin that the formula has caught up with is dead
    # weight; a divergence that is NOT pinned silently changes a saved graph.
    ck("every legacy cell the formula misses is pinned",
       all(c in pinned for c in disagree),
       f"{len(disagree)} diverge, {len(pinned)} pinned")
    ck("no cell is pinned that the formula already gets right",
       not [c for c in agree if c in pinned],
       f"{len(agree)} of 15 need no pin")

    print("\nthe top rung, stated plainly")
    ck("0.98 MP 16:9 is 1312x736", quiet("0.98 MP", "16:9 landscape") == (1312, 736),
       str(quiet("0.98 MP", "16:9 landscape")))
    ck("0.98 MP 1:1 is 992x992", quiet("0.98 MP", "1:1 square") == (992, 992),
       str(quiet("0.98 MP", "1:1 square")))
    ck("portrait is its landscape transposed", all(
        quiet(res, land)[::-1] == quiet(res, port)
        for res in resolutions
        for land, port in (("16:9 landscape", "9:16 portrait"),
                           ("4:3 landscape", "3:4 portrait"),
                           ("21:9 landscape", "9:21 portrait"))))

    print("\nbad input is loud, not silently wrong")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fallback = canvas("0.98 MP", "17:4 nonsense")
    ck("an unknown aspect falls back to the default and says so",
       fallback == quiet("0.98 MP", H3.DEFAULT_ASPECT)
       and "unknown aspect" in out.getvalue(), out.getvalue().strip()[:60])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        unreadable = canvas("not a number", "16:9 landscape")
    ck("an unreadable resolution falls back and says so",
       unreadable == quiet(H3.DEFAULT_RESOLUTION, "16:9 landscape")
       and "unreadable resolution" in out.getvalue(),
       out.getvalue().strip()[:60])

    print("\nthe dials agree with the table")
    ck("DRAFT_RESOLUTION is a selectable rung",
       H3.DRAFT_RESOLUTION in resolutions, H3.DRAFT_RESOLUTION)
    ck("DEFAULT_RESOLUTION is a selectable rung",
       H3.DEFAULT_RESOLUTION in resolutions, H3.DEFAULT_RESOLUTION)
    ck("DEFAULT_ASPECT is a selectable aspect",
       H3.DEFAULT_ASPECT in aspects, H3.DEFAULT_ASPECT)
    ck("the three 1.0.x aspect labels are still offered verbatim",
       all(a in aspects for a in
           ("16:9 landscape", "9:16 portrait", "1:1 square")))
    # The editor's digest reads this rather than recomputing the size in JS.
    routes = sys.modules["htcpack.routes"]
    with contextlib.redirect_stdout(io.StringIO()):
        published = routes._payload()["canvas"]
    ck("/vocab publishes a size for every rung and aspect",
       all(list(quiet(res, asp)) == published[res][asp]
           for res in resolutions for asp in aspects),
       f"{len(published)} x {len(published[H3.DEFAULT_RESOLUTION])}")

    print()
    if FAIL:
        print(f"CANVAS CHECK: {len(FAIL)} FAILURE(S)")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("CANVAS CHECK: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
