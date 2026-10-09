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
# Same class of trap for the tab bodies: .h3e-scroll has no display, so its
# `hidden` works -- but the panes inside it are flex, which is why
# .h3e-pane[hidden] already carries a rule.
ck("the tab panes keep their own [hidden] rule",
   re.search(r"\.h3e-pane\[hidden\]\s*\{[^}]*display:\s*none", css))

print()
if FAIL:
    print("PREVIS WARN: %d FAILURE(S)\n  %s" % (len(FAIL), "\n  ".join(FAIL)))
    raise SystemExit(1)
print("PREVIS WARN: all passed")