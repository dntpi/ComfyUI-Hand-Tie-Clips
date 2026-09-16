r"""Offline tests for the speed preset -- no GPU, no server.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_speed_mode.py

`speed_mode` is a preset: it overwrites refine widgets with a table's values
before the hop runs. That shape has exactly three ways to be silently wrong, and
each of them costs a whole A/B rather than an error.

**A typo in the table does nothing.** `SPEED_MODES["turbo"]["refine_hed"]` is a
perfectly good dict entry that no widget ever reads, so the mode ships looking
active and rendering the regular arm. Every field in every row is checked
against the node's own declaration, and every value against that widget's legal
range, so a row can only name something real.

**`regular` stops being the identity.** The whole compatibility claim is that a
graph built before this widget existed renders byte-identically, which holds
only while the `regular` row is empty and the applier is a pass-through.

**The preset reaches the cache key twice, or not at all.** It is deliberately
absent from the hop payload and reaches the key only through the refine values
it moves -- so turbo under `hop_refine=off` must key identical to regular, and
turbo under `full` must key different. Either direction failing serves one arm's
frames to the other arm.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    pkg = types.ModuleType("htcpack")
    pkg.__path__ = [HERE]
    sys.modules["htcpack"] = pkg
    import importlib
    return importlib.import_module("htcpack.h3_ref_chain")


# The widget values a run arrives with, before any preset touches them. Read off
# the node's own declaration rather than restated, so this cannot drift from it.
def widget_defaults(H3, names):
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}
    out = {}
    for n in names:
        spec = opt.get(n)
        if not spec:
            continue
        t, cfg = spec[0], (spec[1] if len(spec) > 1 else {})
        d = cfg.get("default")
        if d is None and isinstance(t, (list, tuple)) and t:
            d = t[0]
        out[n] = d
    return out


def main():
    H3 = load_pack()
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}
    names = list(opt)
    MODES = H3.SPEED_MODES
    apply_mode = H3._apply_speed_mode

    print("the widget: appended last, shipped inert")
    ck("speed_mode is declared", "speed_mode" in opt)
    ck("it offers regular / turbo",
       (opt.get("speed_mode") or [None])[0] == ["regular", "turbo"])
    ck("it ships 'regular'",
       (opt["speed_mode"][1] or {}).get("default") == "regular")
    ck("it has a tooltip", bool((opt["speed_mode"][1] or {}).get("tooltip")))
    ck("it sits after refine_head (appended, not inserted)",
       names.index("speed_mode") > names.index("refine_head"),
       "widgets_values is positional")
    sig = inspect.signature(H3.HandTieClips.run).parameters
    ck("run() takes speed_mode='regular'",
       sig.get("speed_mode") is not None
       and sig["speed_mode"].default == "regular")
    ck("unique_id is still last", list(sig)[-1] == "unique_id",
       str(list(sig)[-3:]))

    print("\nthe table: every row names real widgets and legal values")
    ck("every declared mode is on the widget",
       set(MODES) == set(opt["speed_mode"][0]),
       "%s vs %s" % (sorted(MODES), sorted(opt["speed_mode"][0])))
    for mode, row in sorted(MODES.items()):
        for field, value in sorted(row.items()):
            ck(f"{mode}.{field} is a widget on this node", field in opt,
               "" if field in opt else "nothing reads it; the row is inert")
            if field not in opt:
                continue
            t = opt[field][0]
            if isinstance(t, (list, tuple)):
                ck(f"{mode}.{field}={value!r} is one of its choices",
                   value in t, "" if value in t else "choices %s" % (list(t),))
            else:
                want = {"INT": int, "FLOAT": float, "STRING": str}.get(t)
                ck(f"{mode}.{field}={value!r} is a {t}",
                   want is None or isinstance(value, want))
        ck(f"{mode} only touches refine fields",
           all(f.startswith("refine_") for f in row),
           "the preset is a refine preset; hop_refine stays the user's call")

    print("\nregular is the identity, and the applier reports what it moved")
    ck("the regular row is empty", MODES.get("regular") == {},
       "a graph built before this widget must render byte-identically")
    fields = sorted({f for row in MODES.values() for f in row}
                    | {"refine_denoise", "refine_steps", "refine_head",
                       "refine_audio", "refine_blend"})
    base = widget_defaults(H3, fields)
    got, moved = apply_mode("regular", dict(base))
    ck("regular changes nothing", got == base)
    ck("regular reports nothing moved", moved == [])

    got_t, moved_t = apply_mode("turbo", dict(base))
    row = MODES["turbo"]
    expect = sorted(f for f in row if f in base and base[f] != row[f])
    ck("turbo moves exactly the fields whose value differs",
       sorted(f for f, _, _ in moved_t) == expect, str(expect))
    ck("the report carries the widget value and the preset value",
       all(base[f] == was and row[f] == now for f, was, now in moved_t),
       "the log is what makes this NOT a silent correction")
    ck("turbo leaves every other field alone",
       all(got_t[k] == base[k] for k in base if k not in row))
    ck("applying turbo twice is a no-op the second time",
       apply_mode("turbo", dict(got_t))[1] == [],
       "or the log cries wolf on a re-queue")
    ck("an unknown mode falls back to the widgets",
       apply_mode("lightning", dict(base)) == (base, []),
       "a typo in a preset is not worth losing a queue over")
    ck("the applier does not mutate its input", base == widget_defaults(
        H3, fields))

    print("\nthe cache key: reached through the refine values, never twice")
    src = inspect.getsource(H3.HandTieClips.run)
    ck("speed_mode is absent from the hop payload",
       'speed_mode' not in src[src.find('hop_payload["refine"] = ['):
                               src.find("]", src.find(
                                   'hop_payload["refine"] = [') + 26)],
       "one copy of the truth; the effective refine values carry it")
    ck("the preset is applied before the ramp is parsed",
       src.find("_apply_speed_mode") < src.find("_rblend.parse(refine_blend)"),
       "a turbo row that set refine_blend would otherwise parse the widget")
    ck("the preset is applied before the refine sampler is built",
       src.find("_apply_speed_mode") < src.find("the refine sampler choice"))
    ck("the preset is applied before the hop key is assembled",
       src.find("_apply_speed_mode") < src.find('hop_payload["refine"] = ['))
    ck("it is skipped entirely when the refine is off",
       'if str(hop_refine) == "off":' in src,
       "turbo under hop_refine=off must key identical to regular")
    ck("the overrides write back onto the run's own names",
       all(f"{f} = _sv[" in src for f in sorted(MODES["turbo"])),
       "a row field never written back is a row field that does nothing")

    print("\nthe mode states its cost, once, in the log")
    notes = H3.SPEED_MODE_NOTES
    for mode in MODES:
        if mode == "regular":
            continue
        ck(f"{mode} carries a measured note", bool(notes.get(mode)),
           "no silent correction: the console says what it cost")
    ck("the note names the measured numbers",
       "8.40" in (notes.get("turbo") or "")
       and "2.07" in (notes.get("turbo") or ""),
       "junction MAE from the matched pinned-seed pair")
    tip = (opt["speed_mode"][1] or {}).get("tooltip") or ""
    ck("the tooltip makes no parity claim",
       "8.40" in tip and "climbing" in tip.lower(),
       "it says turbo is worse, with the number")
    ck("the tooltip says nothing auto-detects",
       "detect" in tip.lower() and "declared" in tip.lower(),
       "a ModelPatcher carries no name")

    print("\n%s" % ("FAILED: " + ", ".join(FAIL) if FAIL else "ALL PASS"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
