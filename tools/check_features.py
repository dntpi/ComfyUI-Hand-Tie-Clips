r"""Offline regression tests for the 2026-08-30 feature set (CLAUDE.md section 20).

None of these features could be tested in a browser or against a GPU when they
were written, so this is the only thing standing between them and a silent
regression. It runs without a server, without a model and without CUDA.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_features.py

Covers:
  * tone.anchor_pull   -- seam exactness, the cap, the no-op, and the 8-hop
                          behaviour that justifies the mode existing
  * dry_run            -- compiles every prompt WITHOUT reaching the sampler,
                          asserted by booby-trapping every sampler entry point
  * render_through     -- truncates the render, not the plan
  * quality=draft      -- forces 0.3 MP and 6 steps
  * contact sheet      -- builds, and degrades to a placeholder rather than
                          raising
  * seam report        -- recovers a planted step at the right frame index
  * over-delivery lint -- fires on the real pattern, stays quiet on the traps
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-52s %s" % ("ok" if cond else "FAIL", name, detail))
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


def main():
    import torch

    pack = load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]
    T = sys.modules["htcpack.tone"]
    P = sys.modules["htcpack.plan"]
    S = sys.modules["htcpack.seam"]
    SH = sys.modules["htcpack.sheet"]

    torch.manual_seed(0)
    N, OV, HW = 120, 22, 32

    # ---------------------------------------------------------------- tone
    print("\ntone.anchor_pull")
    ck("anchor is a mode", "anchor" in T.MODES, str(T.MODES))

    hop = (torch.rand(N, HW, HW, 3) * 0.1 + 0.30
           + torch.linspace(0.0, -0.05, N).view(-1, 1, 1, 1)).clamp(0, 1)
    out, _ = T.anchor_pull(hop, torch.tensor([0.5, 0.5, 0.5]))
    ck("frame 0 is untouched (the seam stays exact)",
       float((out[0] - hop[0]).abs().max()) < 1e-6)
    ck("the tail is corrected", float((out[-1] - hop[-1]).mean()) > 0.01)

    flat = (torch.rand(N, HW, HW, 3) * 0.1 + 0.05).clamp(0, 1)
    o2, _ = T.anchor_pull(flat, torch.tensor([0.9, 0.9, 0.9]), strength=1.0)
    ck("per-hop cap is honoured",
       float((o2[-1] - flat[-1]).mean()) <= T.ANCHOR_MAX_SHIFT + 0.02,
       "cap %.2f" % T.ANCHOR_MAX_SHIFT)

    same = (torch.rand(N, HW, HW, 3) * 0.1 + 0.5).clamp(0, 1)
    o3, n3 = T.anchor_pull(same, T.anchor_stats(same))
    ck("no-op when already on the anchor", torch.equal(o3, same) and not n3)

    a, _ = T.compensate(same, flat, "anchor", OV)
    b, _ = T.compensate(same, flat, "frame_shift", OV)
    ck("compensate(anchor) == compensate(frame_shift)", torch.allclose(a, b),
       "the chain-wide half lives in anchor_pull")

    def chain(mode):
        # Same seed for every mode, so the two runs differ ONLY by the mode.
        # Without this the comparison is against different noise and the seam
        # numbers wander by more than the effect being measured.
        torch.manual_seed(7)
        prev = ref = None
        means, seams = [], []
        for i in range(8):
            start = float(prev[-1].mean()) if prev is not None else 0.55
            imgs = (torch.rand(N, HW, HW, 3) * 0.05 + start
                    + torch.linspace(0.0, -0.045, N).view(-1, 1, 1, 1)).clamp(0, 1)
            if mode != "off" and prev is not None:
                imgs, _ = T.compensate(prev, imgs, "frame_shift", OV)
            if mode == "anchor" and ref is not None:
                imgs, _ = T.anchor_pull(imgs, ref)
            if i == 0:
                ref = T.anchor_stats(imgs)
            if prev is not None:
                seams.append(abs(float(imgs[0].mean() - prev[-1].mean())))
            means.append(float(imgs.mean()))
            prev = imgs[-OV:].clone()
        return (means[0] - means[-1]) * 255.0, max(seams) * 255.0

    slide_fs, seam_fs = chain("frame_shift")
    slide_an, seam_an = chain("anchor")
    ck("anchor cuts the 8-hop slide by more than half",
       slide_an < slide_fs * 0.5,
       "frame_shift %+.1f/255 -> anchor %+.1f/255" % (slide_fs, slide_an))
    ck("anchor does not regress the seam", seam_an <= seam_fs + 0.02,
       "anchor %.3f vs frame_shift %.3f /255" % (seam_an, seam_fs))

    # ------------------------------------------------------- over-delivery
    print("\nplan.check_over_delivery")

    def plan_of(*shots):
        return P.parse_plan(json.dumps({"shots": list(shots)}))

    fires = [
        ("settle + 'continues'", plan_of(
            {"beat": "She sits.", "directives": {"tail": "settle"}},
            {"beat": "She continues the story."})),
        ("hold + gerund opening", plan_of(
            {"beat": "Quiet.", "directives": {"tail": "hold"}},
            {"beat": "Walking to the window, she looks out."})),
    ]
    for name, sh in fires:
        ck("fires: " + name, len(P.check_over_delivery(sh)) == 1)

    quiet = [
        ("default tail", plan_of({"beat": "She sits."},
                                 {"beat": "She continues."})),
        ("settle + standstill opening", plan_of(
            {"beat": "She sits.", "directives": {"tail": "settle"}},
            {"beat": "After a moment she looks up."})),
        ("stillness / keepsake / morning are not hits", plan_of(
            {"beat": "She sits.", "directives": {"tail": "settle"}},
            {"beat": "Morning light fills the stillness; a keepsake sits there."})),
    ]
    for name, sh in quiet:
        ck("quiet: " + name, not P.check_over_delivery(sh))

    for fn in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
        wf = json.load(io.open(os.path.join(HERE, "workflows", fn), encoding="utf-8"))
        node = next(n for n in wf["nodes"] if n["type"] == "HandTieClips")
        sh = P.parse_plan(node["widgets_values"][17])
        ck("shipped plan stays quiet: " + fn, not P.check_over_delivery(sh))

    ck("tone field accepts free/rebase",
       [x["tone"] for x in plan_of({"beat": "a", "tone": "free"},
                                   {"beat": "b", "tone": "rebase"})]
       == ["free", "rebase"])
    try:
        plan_of({"beat": "a", "tone": "nonsense"})
        ck("tone field rejects garbage", False)
    except ValueError:
        ck("tone field rejects garbage", True)

    # ---------------------------------------------------------------- sheet
    print("\nsheet")
    rows = [{"hop": 1, "first": torch.rand(64, 114, 3), "last": torch.rand(64, 114, 3),
             "beat": "A beat.", "directives": {"join": "continuous"},
             "meta": ["362f"], "note": "tone: anchor"}]
    ck("builds with frames", SH.build(rows, "t").shape[1] > 50)
    ck("builds text-only (dry run shape)",
       SH.build([{"hop": 1, "first": None, "last": None, "beat": "x",
                  "directives": {}}], "t").shape[1] > 20)
    ck("empty -> placeholder", tuple(SH.build([]).shape) == (1, 1, 1, 3))
    ck("hostile row -> placeholder, no raise",
       tuple(SH.build([{"hop": 1, "first": "nope", "beat": "x"}]).shape) == (1, 1, 1, 3))
    ck("small() shrinks a full frame",
       tuple(SH.small(torch.rand(736, 1280, 3)).shape)[0] == SH.THUMB_H)

    # ----------------------------------------------------------- seam node
    print("\nseam report")
    total = 362 + 340 * 2
    v = torch.full((total, 16, 16, 3), 0.50)
    v[702:] -= 0.02
    rows_, hop_len, _ = S.measure(v, 3, 22, 6)
    ck("derives whole-frame hop length", abs(hop_len - 362.0) < 0.01, "%.2f" % hop_len)
    ck("finds the seams at the right frames",
       [r["at"] for r in rows_] == [362, 702], str([r["at"] for r in rows_]))
    ck("reads the planted -5.1/255 step",
       abs(rows_[1]["luma"] + 5.1) < 0.2 and rows_[1]["verdict"] == "VISIBLE",
       "%+.2f/255 %s" % (rows_[1]["luma"], rows_[1]["verdict"]))
    ck("calls the clean seam invisible", rows_[0]["verdict"] == "invisible")
    ck("chart renders", S.chart(rows_).shape[1] > 50)

    # ------------------------------------------------------------- dry run
    print("\ndry_run / render_through / quality")

    class Trap:
        def __getattr__(self, k):
            raise AssertionError("dry run reached the sampler (%s)" % k)

    for name in ("MiniMaxH3ReferenceToVideo", "SamplerCustomAdvanced",
                 "MiniMaxH3SigmaShift", "BasicScheduler", "KSamplerSelect"):
        setattr(H3, name, Trap())
    H3._model_fingerprint = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("dry run fingerprinted the model"))
    H3._push_preview = lambda *a, **k: None

    plan8 = {"shots": [{"beat": "Shot %d happens." % (i + 1),
                        "directives": {"join": "continuous"} if i else {}}
                       for i in range(8)]}

    def run(**kw):
        base = dict(model=object(), clip=object(), vae=object(), audio_vae=object(),
                    prompt="unused", chains="8", resolution="1.0 MP",
                    aspect="16:9 landscape", duration="15 s", overlap="0.9 s",
                    seed=41, seed_per_shot=True, steps=14,
                    sampler_name="res_multistep", scheduler="beta",
                    shift_video=12.0, shift_audio=3.0, ref_image_size="match",
                    shot_plan=json.dumps(plan8), dry_run="on", unique_id="t")
        base.update(kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = H3.HandTieClips().run(**base)
        return out, buf.getvalue()

    out, _ = run(tone_compensate="anchor", cache_hops="on")
    ck("dry run returns four values", len(out) == 4)
    ck("dry run never reached the sampler", True, "asserted by the traps above")
    ck("images is the 1x1 placeholder", tuple(out[0].shape) == (1, 1, 1, 3))
    ck("audio is silent, not None", out[1]["waveform"].abs().max() == 0)
    ck("info carries every compiled prompt", out[2].count("===== hop") == 8)
    ck("contact sheet is built on a dry run", out[3].shape[1] > 100)

    for rt, want in ((0, 8), (3, 3), (8, 8), (12, 8)):
        o, _ = run(render_through=rt)
        ck("render_through=%d compiles %d hop(s)" % (rt, want),
           o[2].count("===== hop") == want)

    _, log = run(quality="draft", render_through=2)
    ck("draft drops the canvas", "736x416" in log and "0.3 MP" in log)
    ck("draft drops the steps", "6 steps" in log)
    _, log = run(quality="final", render_through=2)
    ck("final leaves the canvas alone", "1280x736" in log)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
