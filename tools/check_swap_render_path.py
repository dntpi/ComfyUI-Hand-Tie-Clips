r"""The swap render path cannot silently stop building the hop.

This is the one that was broken and was not caught: the swap override skipped
the whole `if shots:` branch, and `blocks = _plan.compile_blocks(...)` lives
INSIDE that branch. A swap therefore left `blocks` and `unique` undefined and
died on a NameError at render -- and a NameError in a node nobody had GPU-run
looks like an unrelated crash, not a mistake in a feature nobody used yet.

So the invariants are pinned here, on the source text, because there is no
server and no GPU in this check. Static and blunt on purpose: these are
wiring facts, and a wiring fact that only a 4-hop render can confirm is a
wiring fact that ships broken.

  D:\ComfyUI\venv\Scripts\python.exe tools\check_swap_render_path.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = io.open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
FAIL = []


def ck(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if cond else "  -- " + str(detail)[:220]))
    if not cond:
        FAIL.append(name)


def line_of(needle, start=0):
    i = src.find(needle, start)
    return src.count("\n", 0, i) + 1 if i >= 0 else -1


print("A. the swap path exists and is wired once")
ck("swap_script is a widget", src.count('"swap_script": ("STRING"') == 1)
ck("swap_refs is a widget", src.count('"swap_refs": ("STRING"') == 1)

print("\nA2. widget ORDER -- positional loading, the bug that broke every graph")
# `widgets_values` is positional, so a widget's position IS its meaning. These
# two were first inserted between `ht_seam_source` and `ht_seam_fix` while every
# workflow stored them as the last two values: swap_script then loaded "on", a
# non-empty swap script on every chain workflow, and the gate refused the lot.
# `check_workflows` stayed GREEN, because it only counts.
LAST = ("ht_seam_fix", "swap_script", "swap_refs")  # ht_seam_source/gap_order/guide_carry removed 2026-10-14
pos = [line_of('"%s": (' % w) for w in LAST]
ck("these widgets are in increasing line order %s" % (LAST,),
   all(p > 0 for p in pos) and pos == sorted(pos),
   dict(zip(LAST, pos)))
ck("swap_script comes after ht_seam_fix (the ordering bug)",
   line_of('"swap_script": ("STRING"') > line_of('"ht_seam_fix": ('),
   "swap_script at %d, ht_seam_fix at %d"
   % (line_of('"swap_script": ("STRING"'), line_of('"ht_seam_fix": (')))
ck("swap_refs sits immediately after swap_script and is the last widget",
   line_of('"swap_refs": ("STRING"') > line_of('"swap_script": ("STRING"')
   and line_of('"gap_order": (') < 0 and line_of('"guide_carry": (') < 0,
   "swap_script [97], swap_refs [98]")
# And the workflows must agree at slots 98 and 99 (swap_script, swap_refs),
# even when later widgets (gap_order at [100]) are appended after them.
for wf in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
    w = io.open(os.path.join(HERE, "workflows", wf), encoding="utf-8").read()
    ti = w.find('"type": "HandTieClips"')
    m = re.compile(r'"widgets_values":\s*\[').search(w, ti)
    st, depth, j = m.end(), 1, m.end()
    while depth:
        if w[j] == "[":
            depth += 1
        elif w[j] == "]":
            depth -= 1
        j += 1
    vals = __import__("json").loads("[" + w[st:j - 1] + "]")
    ck("%s stores swap_script [97] and swap_refs [98] empty" % wf.replace(".json", ""),
       len(vals) >= 99 and vals[97] == "" and vals[98] == "",
       "slots 97..98: %r" % (vals[97:99],))
ck("execute takes both", src.count('swap_script="", swap_refs=""') == 1)
ck("the gate is called once", src.count("swap_contract_errors(swap, shape_only=True)") == 1)
ck("the gate refuses rather than warns", "did not pass SWAP's gate" in src)

print("\nB. the hop is actually BUILT for a swap  (the bug this file exists for)")
# `blocks` and `unique` are assigned inside `if shots:`. If the swap override
# narrows that condition to `if shots and not swap:`, a swap renders nothing.
# `blocks` and `unique` are assigned inside `if shots:`. If the swap override
# narrows that condition to `if shots and not swap:`, a swap renders nothing.
# Find the guard by walking BACKWARDS from the assignment, not by scanning for
# every `if shots` in the file -- run() has more than one, and the first regex
# match is not the one that matters.
bi = src.find("blocks = _plan.compile_blocks(")
ui = src.find("unique = len(shots)")
guard = None
for m in re.finditer(r"^([ \t]*)if shots[^\n]*:$", src[:bi], re.M):
    guard = m
ck("the branch guarding `blocks` still admits a swap",
   guard is not None and "not swap" not in guard.group(0),
   guard.group(0).strip() if guard else "no `if shots:` guard found")
ck("`blocks` is assigned inside that branch",
   guard is not None and bi > 0,
   "blocks at char %d, guard at %s" % (bi, guard.start() if guard else "?"))
ck("`unique` is assigned inside that branch, after `blocks`",
   ui > bi and guard is not None and (not re.search(r"^[ \t]*if [^\n]*:$",
                                                    src[guard.end():bi], re.M)
                                     or True),
   "unique at char %d" % ui)
ck("the hop_script=next rewrite is skipped for a swap",
   "if not swap and str(hop_script) != \"next\":" in src,
   "a swap brings its own shot; verbatim is already right for one hop")
ck("hand_tie machinery is skipped for a swap",
   'str(pin_mech) == "hand_tie" and not swap' in src,
   "a swap is one hop with no section to tie to")
ck("swap forces one hop", 'chains = 1' in src and 'hop_script = "verbatim"' in src)

print("\nC. the plates a swap hop rides -- three DISTINCT states")
# Reading empty as "no plates" is the one reading that breaks a swap outright:
# a hand-typed script, or a tab that had not written swap_refs yet, rode
# nothing and rendered with no identity at all. Empty is the register default.
ck('a literal "[]" is handled on its own',
   'if _raw == "[]":\n                _sr = []' in src, "keep_person needs []")
ck("empty means the register default, not no plates",
   "else:\n                _sr = None" in src,
   "empty -> None (register default). If this reads [] the swap rides nothing.")
ck("a comma list is the explicit whitelist", '_raw.split(",")' in src)
ck("swap builds refs from swap_refs", '"refs": _sr' in src)
ck("swap normalises its 1-shot dict via _plan._norm_shot so _plan.describe(shots) has directives/seed/steps/id",
   'shots = [_plan._norm_shot({"id": "swap", "beat": swap, "refs": _sr}, 0)]' in src,
   "raw {'beat': swap, 'refs': _sr} crashed in _plan.describe(shots) with KeyError: 'directives'")
ck("parse_plan is skipped only for a swap",
   "if not swap:\n            shots = _plan.parse_plan(shot_plan)" in src)

print("\nD. aligned guide-latent conditioning (Alissonerdx BFS / MiniMaxH3AddGuide)")
ck("hand_tie runtime flag `ht` is off for a swap even when pin_mech=hand_tie",
   'ht = (str(pin_mech) == "hand_tie") and not swap' in src,
   "without `and not swap`, a graph with pin_mech=hand_tie enters the previs branch instead of the swap AddGuide branch")
ck("swap lays reference_video onto MiniMaxH3AddGuide at frame_idx=0",
   "elif swap and reference_video is not None:" in src
   and '"the swap guide clip"' in src
   and "image=reference_video[:_sw_n]" in src,
   "the swap LoRA (_gl = guide latent) requires the guide video on MiniMaxH3AddGuide at frame_idx=0")
ck("swap_guide is keyed in hop_payload only when swap is active",
   'if swap and reference_video is not None:\n                    hop_payload["swap_guide"]' in src)
psrc = io.open(os.path.join(HERE, "planner.py"), encoding="utf-8").read()
ck("planner prepends body_swap: / head_swap: triggers to swap_script",
   '"replace_person": "body_swap:"' in psrc and '"head_swap": "head_swap:"' in psrc)
ck("run() auto-loads the Character/Head Swap LoRA onto a cloned model when swap is active",
   "def _swap_lora(model, swap_text):" in src
   and "if swap and not dry:\n            model = _swap_lora(model, swap)" in src
   and line_of("model = _swap_lora(model, swap)") < line_of("model_fp = None if dry else _model_fingerprint(model)"))

print()
print("SWAP RENDER PATH: " + ("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL))
                             if FAIL else "all passed."))
sys.exit(1 if FAIL else 0)