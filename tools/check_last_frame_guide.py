r"""Offline tests for last_frame_guide -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_last_frame_guide.py

The opt-in last-frame AddGuide is the conservative half of keyframe chaining:
pin start_image at the last PIXEL frame of every hop, default off. Two ways
it ships silently wrong:

  * frame_idx is latent T-1. AddGuide's index is pixel frames. FRAME_PER_TOKEN
    is (1, 4, 4, 4, 4); on an 8 s hop that pins ~2.3 s in, not the end.
  * the cache key carries "off", so flipping the widget from the default
    does not look like a change -- or the reverse, every existing hop cache
    misses because a new None field arrived.

Neither is visible without a render. The helpers below are the part a
checker can actually call.
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


def raises(fn, needle):
    try:
        fn()
    except ValueError as e:
        return needle.lower() in str(e).lower()
    except Exception:
        return False
    return False


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


def main():
    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    S = sys.modules["htcpack.store"]

    print("widget: appended, default off")
    names = widget_names(H3)
    ck("last_frame_guide is declared", "last_frame_guide" in names, str(names[-5:]))
    if "last_frame_guide" in names and "master_audio_file" in names:
        ck("last_frame_guide is after master_audio_file (appended, not inserted)",
           names.index("last_frame_guide") > names.index("master_audio_file"))
    else:
        ck("last_frame_guide is after master_audio_file (appended, not inserted)",
           False, "widget missing")
    opt = (H3.HandTieClips.INPUT_TYPES().get("optional") or {})
    spec = opt.get("last_frame_guide") or [None, {}]
    ck("combo is off / before_restart / still",
       spec[0] == ["off", "before_restart", "still"], str(spec[0]))
    ck("default is off", (spec[1] if len(spec) > 1 else {}).get("default") == "off")

    print("queue-fail: still without a start image")
    V = getattr(H3, "_validate_last_frame_guide", None)
    if V is None:
        ck("off with no image is fine", False, "helper missing")
        ck("still with an image is fine", False, "helper missing")
        ck("still without an image is refused", False, "helper missing")
        ck("whitespace is not a start image", False, "helper missing")
    else:
        ck("off with no image is fine", V("off", "") is None)
        ck("still with an image is fine", V("still", "img.png") is None)
        ck("still without an image is refused",
           raises(lambda: V("still", ""), "start image"))
        ck("whitespace is not a start image",
           raises(lambda: V("still", "   "), "start image"))

    print("cache key: omitted when off, present when still")
    key_field = getattr(H3, "_last_frame_guide_key_field", None)
    if key_field is None:
        ck("off -> None (field omitted)", False, "helper missing")
        ck("empty -> None", False, "helper missing")
        ck("still -> still", False, "helper missing")
        ck("off does not move the hop key", False, "helper missing")
        ck("still moves the hop key", False, "helper missing")
    else:
        # Signature gained (hop_index, shots) when before_restart landed:
        # the field now describes what the HOP gets, not what the widget
        # says, because most hops in a before_restart chain are unguided
        # and must keep the key they had before the feature existed.
        _plan = [{}, {}]
        ck("off -> None (field omitted)", key_field("off", 0, _plan) is None)
        ck("empty -> None", key_field("", 0, _plan) is None)
        ck("still -> still", key_field("still", 0, _plan) == "still")
        base = {"chain": 1, "voice_on": True}
        k_off = S.hop_key(None, dict(base))
        payload = dict(base)
        field = key_field("off", 0, _plan)
        if field is not None:
            payload["last_frame_guide"] = field
        ck("off does not move the hop key", S.hop_key(None, payload) == k_off)
        payload_on = dict(base)
        field_on = key_field("still", 0, _plan)
        if field_on is not None:
            payload_on["last_frame_guide"] = field_on
        ck("still moves the hop key", S.hop_key(None, payload_on) != k_off)

    print("frame_idx is the last PIXEL frame, not latent T-1")
    idx = getattr(H3, "_last_pixel_guide_idx", None)
    if idx is None:
        ck("_last_pixel_guide_idx is -1 (Core counts from the end)",
           False, "helper missing")
        ck("the helper names FRAME_PER_TOKEN so the latent-T trap stays visible",
           False, "helper missing")
    else:
        ck("_last_pixel_guide_idx is -1 (Core counts from the end)", idx() == -1)
        # The 8 s numbers in the helper docstring: 192 px, T=57, T-1=56 ~= 2.3 s.
        # If someone "fixes" the helper to return latent_T-1 they have to pick a
        # hop length; the constant -1 is the only value that is last-frame for
        # every duration.
        src = inspect.getsource(idx)
        ck("the helper names FRAME_PER_TOKEN so the latent-T trap stays visible",
           "FRAME_PER_TOKEN" in src)
    run_src = inspect.getsource(H3.HandTieClips.run)
    ck("run() uses the helper, not a raw latent shape",
       "_last_pixel_guide_idx(" in run_src)
    ck("run() does not index video.shape[2] as a pixel frame",
       "shape[2] - 1" not in run_src and "shape[2]-1" not in run_src)
    ck("the last-frame AddGuide is behind the gating helper",
       "_guides_last_frame(last_frame_guide" in run_src,
       "not a raw string compare, so before_restart cannot be forgotten here")

    # The measured reason `before_restart` exists. `still` guides every hop,
    # which turns a restart into a match cut AND overrides an authored framing
    # directive at every other hop ending: a shot set framing=close plays close
    # for six seconds, snaps to the still's wider framing in ~0.6 s, and the
    # next hop pushes back in. Watched, that reads as the camera cutting in and
    # out. `before_restart` keeps the match cut and drops the pumping.
    print("which hops actually get the guide")
    plan = [{}, {}, {"anchor": "restart"}, {}]
    g = H3._guides_last_frame
    ck("off guides nothing",
       [g("off", i, plan) for i in range(4)] == [False] * 4)
    ck("still guides every hop",
       [g("still", i, plan) for i in range(4)] == [True] * 4)
    ck("before_restart guides ONLY the hop before the restart",
       [g("before_restart", i, plan) for i in range(4)]
       == [False, True, False, False],
       "restart is shot 3, so hop 2 is the one that has to arrive on the still")
    ck("the last hop is never guided by before_restart",
       g("before_restart", 3, plan) is False,
       "nothing follows it, so there is no cut to match")
    ck("a plan with no restart guides nothing under before_restart",
       not any(g("before_restart", i, [{}, {}, {}]) for i in range(3)))
    ck("an unknown mode guides nothing rather than raising",
       g("banana", 1, plan) is False)

    # The cache key must follow what the hop GETS, not what the widget says, or
    # every unguided hop in a before_restart chain moves its key for nothing.
    k = H3._last_frame_guide_key_field
    ck("unguided hops keep their old cache key",
       [k("before_restart", i, plan) for i in range(4)]
       == [None, "before_restart", None, None],
       "None on a hop the guide does not reach")
    ck("off is absent from every key",
       [k("off", i, plan) for i in range(4)] == [None] * 4,
       "the default-off byte-identical claim")

    ck("before_restart is offered in the combo",
       "before_restart" in H3.HandTieClips.INPUT_TYPES()
       ["optional"]["last_frame_guide"][0])

    print("frontend and shipped workflows")
    js = io.open(os.path.join(HERE, "js", "editor", "run_panel.js"),
                 encoding="utf-8").read()
    ck("run_panel.js claims last_frame_guide", "last_frame_guide" in js)
    for fn in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
        wf = json.load(io.open(os.path.join(HERE, "workflows", fn),
                               encoding="utf-8"))
        chain = next(n for n in wf["nodes"] if n["type"] == "HandTieClips")
        # seed occupies two widgets_values slots (control_after_generate).
        expect = []
        it = H3.HandTieClips.INPUT_TYPES()
        for section in ("required", "optional"):
            for name, spec in (it.get(section) or {}).items():
                t = spec[0]
                cfg = spec[1] if len(spec) > 1 else {}
                if isinstance(t, str) and t in ("MODEL", "CLIP", "VAE", "IMAGE",
                                                "AUDIO", "LATENT",
                                                "CONDITIONING", "VIDEO"):
                    continue
                if cfg.get("forceInput"):
                    continue
                expect.append(name)
                if name == "seed":
                    expect.append("control_after_generate")
        wv = dict(zip(expect, chain["widgets_values"]))
        ck(f"{fn} last_frame_guide=off", wv.get("last_frame_guide") == "off")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
