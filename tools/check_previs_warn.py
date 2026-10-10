"""The hand-tie section/warning contract between run() and the panel.

    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\check_previs_warn.py

A10 put `ht_previs_warn` into the chain's own preview event, and A9 marked the
sections on the timeline. Both are easy to break silently: the payload shape
changes, and the panel simply shows nothing -- no error anywhere, because a
missing warning is indistinguishable from a run that had none. These cases pin
the field names the JS reads, and the fact that the warning is actually
computed rather than always empty.
"""
from __future__ import annotations

import os
import re
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
pkg = types.ModuleType("h3p")
pkg.__path__ = [HERE]
sys.modules["h3p"] = pkg

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-62s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def read(p):
    with open(os.path.join(HERE, p), encoding="utf-8") as fh:
        return fh.read()


py = read("h3_ref_chain.py")
js_ui = read("js/h3_ref_chain_ui.js")
js_tl = read("js/editor/timeline.js")
js_media = read("js/editor/media_strip.js")
js_model = read("js/editor/timeline_model.js")

print("\npayload field")
ck("run() puts ht_previs_warn in the final preview meta",
   '"ht_previs_warn": _warn_payload' in py,
   "not in h3_ref_chain.py")
ck("the payload is built from ht_previs_warn, not hardcoded",
   "_warn_payload" in py and "for (_sec, _seed, _cuts) in ht_previs_warn:" in py)
ck("ht_previs_warn is populated where a cut is found",
   "ht_previs_warn.append(" in py)

print("\nentry shape")
# Every key the JS reads must be produced by the Python side. These are the
# names a rename would silently break.
for key in ("section", "seed", "cuts", "first_hop", "label"):
    ck("payload carries %r" % key, '"%s":' % key in py or "'%s':" % key in py)
ck("first_hop is resolved from ht_pos, the section's own mapping",
   "_first_hop" in py and "ht_pos.items()" in py)

print("\nthe panel reads it")
ck("the timeline listens for ht_previs_warn",
   "data.ht_previs_warn" in js_tl)
ck("the handler runs BEFORE the `no timeline` guard, or the final push is "
   "dropped",
   js_tl.index("data.ht_previs_warn") < js_tl.index("if (!data?.timeline) return;"),
   "the guard is at %d, the handler at %d"
   % (js_tl.index("if (!data?.timeline) return;"),
      js_tl.index("data.ht_previs_warn")))
ck("the status bar chip is fed from the same record",
   "node._h3Warn?.show(" in js_tl)
ck("_h3Warn.show exists in the entry module",
   "_h3Warn" in js_ui and "warnChip" in js_ui)
ck("the seam row draws the warning at its frame",
   "h3e-tl-previswarn" in js_tl and "previsWarn" in js_tl)

print("\nA9 sections")
ck("tieSections is imported by the timeline",
   re.search(r"import\s*\{[^}]*\btieSections\b", js_tl))
ck("the sections are NOT recomputed in the UI (the model owns the rule)",
   len(re.findall(r"\btieSections\(", re.sub(r"//.*?$", "", js_tl, flags=re.M))) == 1)
ck("tieSections is exported from timeline_model", "export function tieSections" in js_model)
ck("the new-previs marker is drawn on the block",
   "h3e-tl-newprevis" in js_tl)
ck("and on the seam row", "h3e-tl-cutpoint" in js_tl)
ck("the section walk is gated on pin_mech=hand_tie",
   re.search(r'tieOn\s*=.*hand_tie', js_tl))

print("\ncss")
css = re.sub(r"/\*.*?\*/", " ", read("js/h3_ref_chain.css"), flags=re.S)
for cls in ("h3e-tl-newprevis", "h3e-tl-cutpoint", "h3e-tl-previswarn",
            "h3e-warnchip", "h3e-tl-sizebtn", "h3e-run-gstate"):
    ck("stylesheet has .%s" % cls, ".%s" % cls in css)

print("\nA4: the surface swap has to be able to hide RUN")
# `hidden` is only `display: none` in the UA sheet, and ANY author `display`
# on the element outranks it. `.h3e-root > .h3e-run` sets `display: flex`, so
# without an explicit `[hidden]` rule the attribute does nothing and RUN stays
# stacked under every tab -- which is the thing A4 exists to remove, failing
# silently and looking like the feature simply did not work.
ck(".h3e-run is given display:flex, so [hidden] needs its own rule",
   re.search(r"\.h3e-root\s*>\s*\.h3e-run\s*\{[^}]*display:\s*flex", css))
ck("and it has one", re.search(r"\.h3e-root\s*>\s*\.h3e-run\[hidden\]\s*\{[^}]*display:\s*none", css))
ck("the tab panes keep their own [hidden] rule",
   re.search(r"\.h3e-pane\[hidden\]\s*\{[^}]*display:\s*none", css))

# The scroller itself. This check was WRONG before D1c and said so in a comment:
# it claimed ".h3e-scroll has no display, so its hidden works". The panes are
# what get `hidden` -- the SCROLLER keeps them -- and a flex item with a
# min-height does not collapse just because its children are hidden, so the
# attribute on `.h3e-scroll` did nothing at all. It took a screenshot to find,
# because every JS-level assertion passes: the attribute IS set.
#
# The general rule, now stated as a check rather than as a comment: any element
# the JS toggles `.hidden` on, where the CSS gives that element a layout `display`
# (or a flex/min-height that keeps it in flow), needs its own [hidden] rule.
# `hidden` is `display: none` in the UA sheet and ANY author rule outranks it.
ck("the tab SCROLLER keeps its own [hidden] rule (D1c)",
   re.search(r"\.h3e-scroll\[hidden\]\s*\{[^}]*display:\s*none", css),
   "setSurface() hides tabs.bodies -- that is .h3e-scroll -- and without this "
   "the attribute is inert, so RUN lays out UNDER the tab bodies")

print("\nD1d: one item per row, and a label that is never clipped")
# Both of these were found by the user in a screenshot, not by a check, and both
# had the same cause: a grid track narrower than the longest label in the pack.
ck("the MEDIA strip is one slot per row, not an auto-fill grid",
   re.search(r"\.h3e-media-grid\s*\{[^}]*display:\s*flex", css)
   and not re.search(r"\.h3e-media-grid\s*\{[^}]*auto-fill", css),
   "auto-fill put 3-4 slots on a row and every long label wrapped")
ck("the MEDIA label column is wide enough for the longest slot name",
   re.search(r"\.h3e-media-cell\s*\{[^}]*grid-template-columns:\s*1[2-9]\d px", css)
   or re.search(r"\.h3e-media-cell\s*\{[^}]*grid-template-columns:\s*1[2-9]\dpx", css)
   or re.search(r"\.h3e-media-cell\s*\{[^}]*grid-template-columns:\s*[2-9]\d\dpx", css),
   "a 132px label column clipped REFERENCE CLIP 2")
ck("the MEDIA label is not nowrap+ellipsis",
   not re.search(r"\.h3e-media-label\s*\{[^}]*text-overflow:\s*ellipsis", css),
   "nowrap + ellipsis silently truncated the long labels")
ck("the RUN dial grid tracks fit the longest label (control_after_generate)",
   re.search(r"\.h3e-grid\s*\{[^}]*minmax\(1[3-9]\dpx", css),
   "104px wrapped CONTROL AFTER GENERATE onto two lines and pushed its field down")

print("\nD1g: a scrubber lives in its own slot's row")
# The trim window, the description and the size were appended to a single
# `.h3e-trims` tray UNDER the whole grid, so a VOICE 3 scrubber sat below MASTER
# AUDIO and had to label itself "VOICE 3" -- detached from the file it trims.
# These are structural, so they are pinned structurally.
ck("there is no tray container for trim rows any more",
   "h3e-trims" not in js_media,
   "a scrubber must live inside the slot it belongs to")
ck("the media strip appends its trim rows to the slot body",
   len(re.findall(r"body\.appendChild\(row\)", js_media)) >= 1
   and "trims.appendChild(row)" not in js_media,
   "rows in a shared tray are detached from their slot")
ck("the trim row is a flex child of the body, not a stacked block",
   re.search(r"\.h3e-media-body\s*>[^{]*\.h3e-trimrow\s*\{[^}]*flex:", css),
   "otherwise the scrubber stacks under the filename instead of beside it")
ck("the native media transport is hidden (it duplicated our waveform)",
   re.search(r"\.h3e-trim-media\s*\{[^}]*display:\s*none", css),
   "the browser's own play/clock/volume bar is the ugliest thing on the tab")

print()
if FAIL:
    print("PREVIS WARN: %d FAILURE(S)\n  %s" % (len(FAIL), "\n  ".join(FAIL)))
    raise SystemExit(1)
print("PREVIS WARN: all passed")