r"""Offline tests for the refine widgets and their cache key -- no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_refine_keys.py

Two silent failures, both of which cost a whole A/B rather than an error.

**A refine field missing from the hop key.** The refine runs after EVERY
sampler, hop 1 included: under `full` it rewrites hop 1's delivered pixels and
under `pin_only` it rewrites the latent hop 1 hands forward. So a hop cached by
an `off` run must never be served to a refine run, and flipping any refine
widget must re-render. A missing field means the comparison run is served the
other arm's frames, which is the whole experiment, silently void.

**The reverse, and it is just as expensive.** `hop_refine=off` must key
byte-identical to a build that had no refine at all, or every cached hop on
disk misses the first time this version loads and the default costs a full
re-render for a feature nobody turned on.

`widgets_values` is positional, so the third thing asserted is that these
eleven arrived at the END of the declaration and nothing above them moved.
"""
from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []

# name -> (expected default, a different legal value)
REFINE = {
    "hop_refine": ("off", "full"),
    "refine_denoise": (0.50, 0.35),
    "refine_steps": (2, 3),
    "refine_sampler": ("same", "euler"),
    "refine_scheduler": ("simple", "beta"),
    # denoise, NOT hop_tail: an undistilled trunk is accurate at every sigma,
    # so the wider stride costs nothing there, and a graph saved before this
    # widget existed must reload on the schedule it rendered under. hop_tail is
    # reached through speed_mode=turbo, where the base makes it load-bearing.
    "refine_align": ("denoise", "hop_tail"),
    "refine_cond": ("base", "hop"),
    "refine_audio": ("freeze", "refine"),
    # 'auto', NOT the literal: the 22 in "0:0, 22:0, 44:1" is `overlap`, and
    # the two only coincide at the default 0.9 s. auto resolves to a literal
    # before the hop key, so it keys identically there and correctly elsewhere.
    "refine_blend": ("auto", "0:0, 22:0, 66:1"),
    "refine_blend_interp": ("linear", "smooth"),
    "refine_head": ("refine", "freeze"),
}


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
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


def widget_names(H3):
    it = H3.HandTieClips.INPUT_TYPES()
    names = []
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in ("MODEL", "CLIP", "VAE", "IMAGE",
                                            "AUDIO", "LATENT", "CONDITIONING",
                                            "VIDEO"):
                continue
            if cfg.get("forceInput"):
                continue
            names.append(name)
    return names


def refine_field(S, **over):
    """The `refine` key field exactly as run() builds it."""
    v = {k: d for k, (d, _) in REFINE.items()}
    v.update(over)
    field = [str(v["hop_refine"]), round(float(v["refine_denoise"]), 4),
            int(v["refine_steps"]), str(v["refine_sampler"]),
            str(v["refine_scheduler"]), str(v["refine_cond"]),
            str(v["refine_head"]), str(v["refine_audio"]),
            str(v["refine_blend"]).strip(), str(v["refine_blend_interp"]),
            over.get("refine_model_fp")]
    # Mirrors run(): appended only for hop_tail, so pinning back to `denoise`
    # reproduces a pre-refine_align key exactly.
    if str(v["refine_align"]) != "denoise":
        field.append(str(v["refine_align"]))
    return field


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    S = sys.modules["htcpack.store"]
    opt = H3.HandTieClips.INPUT_TYPES().get("optional") or {}

    print("widgets: appended last, shipped inert")
    names = widget_names(H3)
    for name, (default, _) in REFINE.items():
        spec = opt.get(name)
        got = (spec[1] if spec and len(spec) > 1 else {}).get("default")
        ck(f"{name} defaults to {default!r}", got == default, repr(got))
    ck("all eleven are after voice_every_hop (appended, not inserted)",
       all(name in names and names.index(name) > names.index("voice_every_hop")
           for name in REFINE),
       "widgets_values is positional; an insert renumbers every saved workflow")
    ck("refine_model is a socket, not a widget",
       "refine_model" in (opt or {}) and "refine_model" not in names,
       "a MODEL input takes no widgets_values slot")
    ck("hop_refine offers off / full / pin_only / split (split appended last)",
       (opt.get("hop_refine") or [None])[0] == ["off", "full", "pin_only", "split"])
    ck("every refine widget has a tooltip",
       all((opt[n][1] if len(opt[n]) > 1 else {}).get("tooltip")
           for n in REFINE))

    print("\nrun(): the signature accepts them, defaults matching")
    sig = inspect.signature(H3.HandTieClips.run).parameters
    for name, (default, _) in REFINE.items():
        ck(f"run() takes {name}={default!r}",
           name in sig and sig[name].default == default,
           repr(sig[name].default) if name in sig else "missing")
    ck("run() takes refine_model, defaulting to unwired",
       "refine_model" in sig and sig["refine_model"].default is None)
    ck("hidden inputs close the signature (unique_id, extra_pnginfo)",
       list(sig)[-2:] == ["unique_id", "extra_pnginfo"], str(list(sig)[-3:]))

    print("\nthe hop key: absent when off, moving when on")
    base = {"chain": "salt", "block": "a beat", "len": 192, "voice_on": True}
    k_off = S.hop_key(None, dict(base))
    on = dict(base)
    on["refine"] = refine_field(S, hop_refine="full")
    k_on = S.hop_key(None, on)
    ck("hop_refine=off adds no field at all",
       S.hop_key(None, dict(base)) == k_off,
       "a build with no refine keys byte-identical")
    ck("turning it on moves the key", k_on != k_off)
    ck("the same settings twice agree",
       S.hop_key(None, dict(on)) == k_on,
       "a key that always moves is a cache that never hits")

    for name, (_, other) in REFINE.items():
        if name == "hop_refine":
            continue
        moved = dict(base)
        moved["refine"] = refine_field(S, hop_refine="full", **{name: other})
        ck(f"{name} moves the key", S.hop_key(None, moved) != k_on,
           f"-> {other!r}")
    pin = dict(base)
    pin["refine"] = refine_field(S, hop_refine="pin_only")
    ck("full and pin_only are different hops",
       S.hop_key(None, pin) != k_on,
       "one rewrites the delivered pixels, the other the teacher")
    fp = dict(base)
    fp["refine"] = refine_field(S, hop_refine="full",
                                refine_model_fp="deadbeef")
    ck("wiring refine_model moves the key", S.hop_key(None, fp) != k_on,
       "a hop refined under a different model is a different hop")

    print("\nrun() actually builds that field, and gates it")
    src = inspect.getsource(H3.HandTieClips.run)
    ck("the field is added only when the refine is on",
       'if str(hop_refine) != "off":\n                    hop_payload["refine"]'
       in src, "adding 'off' would move every existing cache key")
    mark = 'hop_payload["refine"] = ['
    start = src.find(mark)
    # From AFTER the opening bracket: `hop_payload["refine"]` closes a bracket
    # of its own, so searching from `start` finds that one and reads an empty
    # block -- which passes every "is this name present" test by accident.
    # Runs to the hop_key() call, not to the list's closing bracket: a widget
    # may be appended conditionally after the literal (refine_align is, so that
    # `denoise` reproduces a pre-refine_align key), and that is still "in the
    # key field".
    block = src[start:src.find("hop_key = ", start)] if start >= 0 else ""
    missing = [n for n in REFINE if n not in block and n != "hop_refine"]
    ck("every refine widget is in the key field", not missing, str(missing))
    ck("...including the refine_model fingerprint", "refine_model_fp" in block)
    ck("the fingerprint is computed from the wired model",
       "_model_fingerprint(refine_model)" in src)
    ck("the ramp is parsed before anything samples",
       src.index("_rblend.parse(refine_blend)") < src.index("_refine_sampled("),
       "a malformed ramp fails on the queue, not two minutes into hop 1")
    ck("pin_only is skipped on the last hop",
       'str(hop_refine) == "pin_only" and i >= n - 1' in src,
       "nothing downstream would ever read it")
    ck("the sigma cache key cannot collide with a hop schedule",
       '("refine", int(steps)' in inspect.getsource(H3._refine_sampled),
       "a bare int is the hop's; a collision re-renders the hop from noise")
    ck("the refine noise seed is independent of the hop's",
       "0x5EF1" in inspect.getsource(H3._refine_sampled),
       "re-noising on the hop's own seed reads as 'the lever did nothing'")

    print("\nshipped workflows carry the defaults")
    expect = []
    it = H3.HandTieClips.INPUT_TYPES()
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in ("MODEL", "CLIP", "VAE", "IMAGE",
                                            "AUDIO", "LATENT", "CONDITIONING",
                                            "VIDEO"):
                continue
            if cfg.get("forceInput"):
                continue
            expect.append(name)
            if name == "seed":
                expect.append("control_after_generate")
    for fn in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
        wf = json.load(io.open(os.path.join(HERE, "workflows", fn),
                               encoding="utf-8"))
        chain = next(n for n in wf["nodes"] if n["type"] == "HandTieClips")
        wv = dict(zip(expect, chain["widgets_values"]))
        ck(f"{fn} has a slot for every widget",
           len(chain["widgets_values"]) == len(expect),
           f"{len(chain['widgets_values'])} vs {len(expect)}")
        ck(f"{fn} ships hop_refine=off", wv.get("hop_refine") == "off")
        # Not "is the published string" but "RESOLVES to it at the overlap
        # this workflow ships", which is the property that actually matters:
        # the delivered frames and the hop key both stay what 2.1 produced.
        RB = sys.modules["htcpack.refine_blend"]
        ov = H3.OVERLAP_FRAMES.get(str(wv.get("overlap")), 22)
        ck(f"{fn} resolves to the published ramp at its own overlap",
           RB.resolve(wv.get("refine_blend"), ov) == RB.DEFAULT_RAMP,
           f"{wv.get('refine_blend')!r} @ overlap={wv.get('overlap')!r} "
           f"({ov} f) -> {RB.resolve(wv.get('refine_blend'), ov)!r}")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
