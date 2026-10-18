r"""Offline regression tests for the 2026-08-30 feature set (docs/DEVLOG.md section 20).

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


def encodable(t):
    """An IMAGE a video encoder can actually open: 4-D, one or more frames,
    both dimensions even and >= 2. Checked instead of an exact 1x1 shape
    because the exact shape is what shipped broken."""
    sh = tuple(t.shape)
    return (len(sh) == 4 and sh[0] >= 1 and sh[3] == 3
            and sh[1] >= 2 and sh[2] >= 2
            and sh[1] % 2 == 0 and sh[2] % 2 == 0)


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
    SH = sys.modules["htcpack.sheet"]

    torch.manual_seed(0)
    N, OV, HW = 120, 22, 32

    # ---------------------------------------------------------------- tone
    print("\ntone.anchor_pull")
    ck("anchor is a mode", "anchor" in T.MODES, str(T.MODES))

    # sRGB <-> Lab has to be an identity before anything built on it means
    # anything: the pull measures in Lab and delivers in sRGB, so a lossy round
    # trip would show up as drift the anchor itself introduced.
    probe = torch.rand(3, 8, 8, 3)
    ck("sRGB -> Lab -> sRGB is identity",
       float((T._lab_to_srgb(T._srgb_to_lab(probe)) - probe).abs().max()) < 1e-4)
    grey_lab = T._srgb_to_lab(torch.full((1, 2, 2, 3), 0.5))
    ck("neutral grey has no chroma",
       float(grey_lab[..., 1:].abs().max()) < 0.02, "a*/b* on 0.5 grey")

    def lab_of(x):
        return T.anchor_stats(x)

    hop = (torch.rand(N, HW, HW, 3) * 0.1 + 0.30
           + torch.linspace(0.0, -0.05, N).view(-1, 1, 1, 1)).clamp(0, 1)
    out, _ = T.anchor_pull(hop, lab_of(torch.full((1, HW, HW, 3), 0.5)))
    ck("frame 0 is untouched (the seam stays exact)",
       float((out[0] - hop[0]).abs().max()) < 1e-6)
    ck("the tail is corrected", float((out[-1] - hop[-1]).mean()) > 0.01)

    flat = (torch.rand(N, HW, HW, 3) * 0.1 + 0.05).clamp(0, 1)
    o2, _ = T.anchor_pull(flat, lab_of(torch.full((1, HW, HW, 3), 0.9)),
                          strength=1.0)
    # The cap is quoted in 0..1 RGB units and spent in Lab units, so it is
    # checked where it is spent. The slack covers the L* spread gain, which is
    # capped separately and also lifts the mean of a dark hop.
    moved = float(lab_of(o2)[0] - lab_of(flat)[0])
    ck("per-hop cap is honoured", moved <= T.ANCHOR_MAX_SHIFT * 100.0 + 1.5,
       "moved %.1f L*, cap %.1f L*" % (moved, T.ANCHOR_MAX_SHIFT * 100.0))

    same = (torch.rand(N, HW, HW, 3) * 0.1 + 0.5).clamp(0, 1)
    o3, n3 = T.anchor_pull(same, T.anchor_stats(same))
    ck("no-op when already on the anchor", torch.equal(o3, same) and not n3)

    # The finding this mode was rebuilt for. A per-channel RGB mean cannot pull
    # a greyed-out hop back toward a colourful reference: it can match all three
    # means and leave the chroma exactly where it was. Lab can, because a* and
    # b* are the colour and nothing else is.
    warm = (torch.rand(N, HW, HW, 3) * 0.1
            + torch.tensor([0.62, 0.42, 0.30])).clamp(0, 1)
    greyed = warm.mean(dim=-1, keepdim=True).expand_as(warm).contiguous()
    o4, _ = T.anchor_pull(greyed, lab_of(warm), strength=1.0)
    c_before = float(lab_of(greyed)[1:3].pow(2).sum().sqrt())
    c_after = float(lab_of(o4)[1:3].pow(2).sum().sqrt())
    ck("chroma is restored, not just the level", c_after > c_before + 1.0,
       "chroma %.1f -> %.1f (reference %.1f)"
       % (c_before, c_after, float(lab_of(warm)[1:3].pow(2).sum().sqrt())))

    # A hop whose contrast has collapsed gets it stretched back, but capped.
    dull = (torch.rand(N, HW, HW, 3) * 0.02 + 0.5).clamp(0, 1)
    lively = (torch.rand(N, HW, HW, 3) * 0.40 + 0.3).clamp(0, 1)
    o5, _ = T.anchor_pull(dull, lab_of(lively), strength=1.0)
    grew = float(lab_of(o5)[3]) / max(1e-6, float(lab_of(dull)[3]))
    ck("L* spread is pulled up but capped",
       1.0 < grew <= 1.0 + T.ANCHOR_MAX_GAIN + 0.02,
       "spread x%.3f, cap x%.2f" % (grew, 1.0 + T.ANCHOR_MAX_GAIN))

    # ramp=0: a chain's first hop and a restart hop open on nothing, so they
    # take the correction from frame 0 rather than fading into it.
    o6, _ = T.anchor_pull(hop, lab_of(torch.full((1, HW, HW, 3), 0.5)), ramp=0)
    ck("ramp=0 corrects frame 0 too",
       float((o6[0] - hop[0]).abs().max()) > 1e-4)
    ck("ramp=0 is flat, not a fade",
       abs(float(lab_of(o6[:1])[0] - lab_of(o6[-1:])[0])
           - float(lab_of(hop[:1])[0] - lab_of(hop[-1:])[0])) < 0.5)

    a, _ = T.compensate(same, flat, "anchor", OV)
    b, _ = T.compensate(same, flat, "frame_shift", OV)
    ck("compensate(anchor) == compensate(frame_shift)", torch.allclose(a, b),
       "the chain-wide half lives in anchor_pull")

    # The reference still: warmer and more colourful than anything the chain
    # renders, which is the situation the `still` mode exists for. A measured
    # 9-hop study read the photograph at chroma 33.6 and HOP 1 at 30, before
    # any relay had happened, so a chain anchored on hop 1 holds a target that
    # already fell short.
    still = (torch.rand(1, HW, HW, 3) * 0.05
             + torch.tensor([0.60, 0.44, 0.33])).clamp(0, 1)
    still_ref = T.anchor_stats(still)

    def chain(mode):
        # Same seed for every mode, so the runs differ ONLY by the mode.
        # Without this the comparison is against different noise and the seam
        # numbers wander by more than the effect being measured.
        torch.manual_seed(7)
        prev = None
        ref = still_ref if mode == "still" else None
        means, seams, out = [], [], None
        for i in range(8):
            start = float(prev[-1].mean()) if prev is not None else 0.55
            imgs = (torch.rand(N, HW, HW, 3) * 0.05 + start
                    + torch.linspace(0.0, -0.045, N).view(-1, 1, 1, 1)).clamp(0, 1)
            # Each hop drifts a little toward grey, the way a relay chain does.
            imgs = imgs * 0.94 + imgs.mean(dim=-1, keepdim=True) * 0.06
            if mode != "off" and prev is not None:
                imgs, _ = T.compensate(prev, imgs, "frame_shift", OV)
            if mode in ("anchor", "still") and ref is not None:
                imgs, _ = T.anchor_pull(imgs, ref,
                                        ramp=(0 if i == 0 else T.ANCHOR_RAMP))
            if i == 0 and mode == "anchor":
                ref = T.anchor_stats(imgs)
            if prev is not None:
                seams.append(abs(float(imgs[0].mean() - prev[-1].mean())))
            means.append(float(imgs.mean()))
            prev = imgs[-OV:].clone()
            out = imgs
        return ((means[0] - means[-1]) * 255.0, max(seams) * 255.0,
                T.anchor_stats(out))

    slide_fs, seam_fs, end_fs = chain("frame_shift")
    slide_an, seam_an, end_an = chain("anchor")
    slide_st, seam_st, end_st = chain("still")
    ck("anchor cuts the 8-hop slide by more than half",
       slide_an < slide_fs * 0.5,
       "frame_shift %+.1f/255 -> anchor %+.1f/255" % (slide_fs, slide_an))
    ck("anchor does not regress the seam", seam_an <= seam_fs + 0.02,
       "anchor %.3f vs frame_shift %.3f /255" % (seam_an, seam_fs))

    # The point of tone_anchor_ref=still: the chain ends nearer the photograph
    # than an identically-run chain anchored on its own first hop.
    def chroma_gap(s):
        return float(((s[1:3] - still_ref[1:3]) ** 2).sum().sqrt())

    ck("anchoring on the still ends nearer the still",
       chroma_gap(end_st) < chroma_gap(end_an),
       "chroma gap: still %.1f, hop1 %.1f, uncorrected %.1f"
       % (chroma_gap(end_st), chroma_gap(end_an), chroma_gap(end_fs)))
    ck("the still anchor does not regress the seam either",
       seam_st <= seam_fs + 0.02,
       "still %.3f vs frame_shift %.3f /255" % (seam_st, seam_fs))

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
    ck("empty -> placeholder", encodable(SH.build([])))
    ck("hostile row -> placeholder, no raise",
       encodable(SH.build([{"hop": 1, "first": "nope", "beat": "x"}])))
    ck("placeholder floors at 2x2, never 1x1",
       encodable(SH.placeholder(1, 1)) and encodable(SH.placeholder(0, 0)))
    ck("placeholder rounds odd dimensions down to even",
       tuple(SH.placeholder(737, 415).shape) == (1, 414, 736, 3))
    ck("small() shrinks a full frame",
       tuple(SH.small(torch.rand(736, 1280, 3)).shape)[0] == SH.THUMB_H)

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
    # Was `== (1, 1, 1, 3)`. A 1x1 frame is inert to SaveImage and fatal to
    # every video encoder -- libx264 cannot open a yuv420p context on an odd
    # dimension -- so the Starter's own SaveVideo died on a dry run. The
    # contract is now "encodable, at the geometry the plan resolved to".
    ck("dry-run images is one encodable frame", encodable(out[0]))
    # `base` asks for "1.0 MP", which 1.1 retired from the dropdown. Getting
    # 1280x736 out of it is the legacy pin working end to end: the formula would
    # give 1344x736, and a saved 1.0.x workflow must not quietly change size.
    ck("dry-run images carries the planned geometry",
       tuple(out[0].shape) == (1, 736, 1280, 3), str(tuple(out[0].shape)))
    o2, _ = run(resolution="768p (0.98 MP)")
    ck("the native tier resolves to 1344x768",
       tuple(o2[0].shape) == (1, 768, 1344, 3), str(tuple(o2[0].shape)))
    # The v1.1 pre-release label aliases forward rather than resolving to the
    # size its own arithmetic gave it, which was 1312x736.
    o3, _ = run(resolution="0.98 MP")
    ck("a v1.1 pre-release label aliases onto the tier it meant",
       tuple(o3[0].shape) == (1, 768, 1344, 3), str(tuple(o3[0].shape)))
    ck("audio is silent, not None", out[1]["waveform"].abs().max() == 0)
    ck("info carries every compiled prompt", out[2].count("===== hop") == 8)
    ck("contact sheet is built on a dry run", out[3].shape[1] > 100)
    # pin_mech=hand_tie: every hop compiled as a start, sections logged, and the
    # refusals that have to fire on the queue before anything samples.
    ht_plan = {"shots": [
        {"beat": "She chops herbs. She sets the knife down.", "duration": "8 s"},
        {"beat": "She leans on the sill.", "duration": "8 s",
         "moment": "She turns to the window and (S1) says, <d>[English] The snow again.</d>"},
        {"beat": "She sips from the mug.", "duration": "8 s"},
        {"beat": "She chops again.", "duration": "8 s", "moment_end": "She chops, smiling."}]}
    oh, log_h = run(shot_plan=json.dumps(ht_plan), pin_mech="hand_tie", duration="8 s")
    ck("hand_tie: dry run compiles every hop", oh[2].count("===== hop") == 4)
    # (A restart opens a second section; the geometry of that is pinned in
    # check_hand_tie.py. Here a restart would also need a start image or refs.)
    ck("hand_tie: the section is logged with its previs geometry",
       "hand-tie section 1: hops 1-4, previs 294f" in log_h
       and "windows at [0, 68, 136, 204, 272]" in log_h,
       log_h[-600:])
    oh7, log7 = run(shot_plan=json.dumps({"shots": [{"beat": "x %d" % k, "duration": "8 s"}
                                                     for k in range(7)]}),
                    pin_mech="hand_tie", duration="8 s")
    ck("hand_tie: 7 x 8 s chain into two even sections (4 + 3), the second continuing hop 4",
       "hand-tie section 2: hops 5-7, previs 226f" in log7
       and "windows at [0, 68, 136, 204], continuing hop 4's last frames" in log7,
       log7[-600:])
    ck("hand_tie: no hop is assembled as a continuation (no next-beat airlock)",
       "next-beat assembled" not in log_h)
    for label, kw, needle in (
            ("needs overlap 0.9 s", {"overlap": "0.2 s"}, "needs overlap 0.9 s"),
            ("refuses last_frame_guide", {"last_frame_guide": "before_restart"}, "last_frame_guide"),
            ("refuses master_audio_file", {"master_audio_file": "x.wav"}, "master_audio_file"),
            ("refuses anchor=extend on shot 1",
             {"shot_plan": json.dumps({"shots": [{"beat": "x", "duration": "8 s", "anchor": "extend"},
                                                 {"beat": "y", "duration": "8 s"}]})}, "anchor=extend")):
        try:
            run(**dict({"shot_plan": json.dumps(ht_plan), "pin_mech": "hand_tie",
                        "duration": "8 s"}, **kw))
            ck("hand_tie: " + label, False, "accepted")
        except ValueError as e:
            ck("hand_tie: " + label, needle in str(e), str(e)[:90])
    _pm_opts = (H3.HandTieClips.INPUT_TYPES()["required"]["pin_mech"][0]
                if "pin_mech" in H3.HandTieClips.INPUT_TYPES()["required"]
                else H3.HandTieClips.INPUT_TYPES()["optional"]["pin_mech"][0])
    ck("hand_tie and hand_tie_mc are the last pin_mech options (appended, never inserted)",
       _pm_opts[-2:] == ["hand_tie", "hand_tie_mc"], _pm_opts)
    oh_mc, _ = run(shot_plan=json.dumps(ht_plan), pin_mech="hand_tie_mc", duration="8 s")
    ck("hand_tie_mc: dry run compiles every hop identically to hand_tie",
       oh_mc[2] == oh[2])
    # Functional test of _ht_mc_inpaint_head: Previs window unpacked into 7 per-step
    # Motion-Context keyframes + Hop k end-aligned audio_kf + preserved tail window.
    import comfy.nested_tensor as _cnt
    torch.manual_seed(42)
    _pv_v = torch.randn(1, 16, 57, 4, 4) * 1.3 + 0.25
    _pv_a = torch.randn(1, 16, 2, 320)
    _nx_v = torch.zeros(1, 16, 57, 4, 4)
    _nx_a = torch.zeros(1, 16, 2, 320)
    _prev_s = {"samples": _cnt.NestedTensor([_pv_v, _pv_a])}
    _next_l = {"samples": _cnt.NestedTensor([_nx_v, _nx_a])}
    _w_head = torch.randn(1, 16, 7, 4, 4) * 0.9 - 0.15
    _w_tail = torch.randn(1, 16, 7, 4, 4)
    _spk_a = torch.randn(1, 16, 16)
    _cond_in = [[torch.zeros(1, 4, 8), {
        "minimax_keyframes": [
            {"frame_index": 0, "resolved_frame_index": 0, "latent": _w_head, "strength": 1.0},
            {"frame_index": -22, "resolved_frame_index": 170, "latent": _w_tail, "strength": 1.0},
            {"frame_index": 0, "resolved_frame_index": 0, "audio_latent": _spk_a, "strength": 1.0},
        ],
    }]]
    _, _cond_mc, _hold_mc, _ = H3._ht_mc_inpaint_head(
        _next_l, _cond_in, _prev_s, 22, vae=None, audio_ctx=24, head_speaks=False)
    _kfs_mc = _cond_mc[0][1]["minimax_keyframes"]
    _vkfs_mc = [k for k in _kfs_mc if "latent" in k]
    _akfs_mc = [k for k in _kfs_mc if "audio_latent" in k]
    ck("hand_tie_mc: unpacks 7 per-step head keyframes [0,1,5,9,13,17,18] + keeps tail at 170",
       _hold_mc == 7
       and [k["resolved_frame_index"] for k in _vkfs_mc] == [170, 0, 1, 5, 9, 13, 17, 18]
       and torch.equal(_vkfs_mc[0]["latent"], _w_tail),
       [k["resolved_frame_index"] for k in _vkfs_mc])
    # Verify spatial picture of unpacked keyframes comes 100% from _w_head (not _pv_v)
    _step0_out = _vkfs_mc[1]["latent"].float()
    _step0_ref = _w_head[:, :, 0:1].float()
    _s0_n = (_step0_out - _step0_out.mean(dim=(2, 3, 4), keepdim=True)) / _step0_out.std(dim=(2, 3, 4), keepdim=True)
    _r0_n = (_step0_ref - _step0_ref.mean(dim=(2, 3, 4), keepdim=True)) / _step0_ref.std(dim=(2, 3, 4), keepdim=True)
    ck("hand_tie_mc: unpacked keyframes keep 100% Previs spatial structure (calibrated to Hop k)",
       float((_s0_n - _r0_n).abs().max()) < 1e-4
       and float((_step0_out.mean(dim=(2, 3, 4)) - _pv_v[:, :, -12:-11].mean(dim=(2, 3, 4))).abs().max()) < 1e-4)
    ck("hand_tie_mc: strips audio_kf when head_speaks=False so closed-mouth head window never speaks or swooshes",
       len(_akfs_mc) == 0,
       f"len={len(_akfs_mc)}")
    _, _cond_spk, _, _ = H3._ht_mc_inpaint_head(
        _next_l, _cond_in, _prev_s, 22, vae=None, audio_ctx=24, head_speaks=True)
    _akfs_spk = [k for k in _cond_spk[0][1]["minimax_keyframes"] if "audio_latent" in k]
    ck("hand_tie_mc: preserves Previs spoken audio keyframe when head_speaks=True",
       len(_akfs_spk) == 1
       and _akfs_spk[0]["resolved_frame_index"] == 0
       and torch.equal(_akfs_spk[0]["audio_latent"], _spk_a))
    try:
        run(dry_run="previs")
        ck("dry_run=previs is refused without pin_mech=hand_tie", False, "accepted")
    except ValueError as e:
        ck("dry_run=previs is refused without pin_mech=hand_tie", "pin_mech=hand_tie" in str(e), str(e)[:70])
    oc, _ = run(shot_plan=json.dumps({"shots": [
        {"beat": "She chops.", "duration": "8 s"},
        {"beat": "She leans on the sill.", "duration": "8 s",
         "moment": "She turns and (S1) says, <d>[English] I keep thinking the snow will stop by noon.</d>"}]}),
        pin_mech="hand_tie", duration="8 s")
    _h1 = oc[2].split("===== hop 1 prompt =====")[1].split("===== hop 2 prompt =====")[0]
    _h2 = oc[2].split("===== hop 2 prompt =====")[1]
    ck("hand_tie: hop 1's prompt ends the crossing line with <|cutoff|>",
       "<d>[English] I keep thinking the snow will stop<|cutoff|></d>" in _h1, _h1[-120:])
    ck("hand_tie: hop 2's prompt finishes the same sentence, from after the window",
       "finishes the same sentence, <d>[English] by noon.</d>" in _h2, _h2[:200])
    ck("plan keeps moment / moment_end only when written",
       "moment" in P.parse_plan(json.dumps(ht_plan))[1]
       and "moment" not in P.parse_plan(json.dumps(ht_plan))[0]
       and "moment_end" in P.parse_plan(json.dumps(ht_plan))[3])

    ou, log_u = run(upsample="on", upsample_megapixels=2.0)
    ck("upsample does nothing on a dry run",
       ou[2].count("===== hop") == 8 and tuple(ou[0].shape) == (1, 736, 1280, 3)
       and "upsample on" not in log_u)

    for rt, want in ((0, 8), (3, 3), (8, 8), (12, 8)):
        o, _ = run(render_through=rt)
        ck("render_through=%d compiles %d hop(s)" % (rt, want),
           o[2].count("===== hop") == want)

    # render_from. The replay itself needs a populated cache and a real render,
    # so what is asserted here is the half that decides whether a user loses an
    # hour: every misconfiguration is refused BEFORE the master tensor is
    # allocated (~31 GB on this plan) and before a single step is sampled.
    def raises(**kw):
        try:
            run(**kw)
        except ValueError as exc:
            return str(exc)
        return ""

    ck("render_from past render_through is refused",
       "range is empty" in raises(render_from=5, render_through=2))
    ck("render_from on a dry run is refused",
       "dry run never touches it" in raises(render_from=3))
    ck("render_from without the hop cache is refused",
       "cache_hops must be on" in raises(render_from=3, dry_run="off",
                                         cache_hops="off"))
    o, log = run(render_from=99)
    ck("render_from past the end falls back to hop 1",
       "past the end" in log and o[2].count("===== hop") == 8)
    o, log = run(render_from=1)
    ck("render_from=1 is not a replay at all",
       "come from the cache" not in log and o[2].count("===== hop") == 8)
    o, _ = run(render_from=0)
    ck("render_from=0 leaves the chain alone", o[2].count("===== hop") == 8)

    # ------------------------------------------------------- anchor=restart
    # A restart is a CHAIN START. It was being assembled as a continuation and
    # only then handed the photograph, so hop 4 of a restart chain was told it
    # "opens already in progress from the pinned frames" while its frame 0 was
    # a studio portrait and nothing was pinned. An outside 9-hop study caught
    # the result: background edge density 0.003 at the restart hop against
    # 4.7-7.2 at every other hop of the same run -- a featureless backdrop,
    # i.e. the model rendered the reference photograph instead of the room.
    print("\nanchor=restart is a chain start")
    restart_plan = {"shots": [
        dict({"beat": "Shot %d happens." % (i + 1)},
             **({"directives": {"join": "continuous"}} if i else {}))
        for i in range(5)]}
    restart_plan["shots"][2] = {"beat": "Shot 3 happens.", "anchor": "restart",
                                "directives": {"join": "hard_cut"}}

    def hops_of(info):
        parts = info.split("===== hop ")
        return {int(p.split()[0]): p for p in parts[1:] if p.split()}

    o, log = run(chains="5", shot_plan=json.dumps(restart_plan),
                 start_image_file="anything.png", hop_script="next")
    hops = hops_of(o[2])
    ck("the restart hop is announced", "ANCHOR RESTART" in log)
    OPENS = "opens already in progress"
    ck("an ordinary continuation still says it continues", OPENS in hops[2],
       "hop 2")
    ck("the restart hop is NOT told it continues", OPENS not in hops[3],
       "hop 3 -- it has no pinned frames to continue from")
    ck("the hop after the restart continues again", OPENS in hops[4], "hop 4")
    ck("no live frame is pinned as a picture on the restart",
       "<Picture 1>" not in hops[3] or "pinned" not in hops[3].lower())
    ck("the restart is not counted as a continue for the stills",
       "stills stay off this continue" not in
       log.split("hop 3/5")[-1].split("hop 4/5")[0])

    _, log = run(quality="draft", render_through=2)
    ck("draft drops the canvas", "800x448" in log and "448p" in log)
    ck("draft drops the steps", "6 steps" in log)
    _, log = run(quality="final", render_through=2)
    ck("final leaves the canvas alone", "1280x736" in log)

    # The reference clip's description. The gate matters as much as the feature:
    # an empty field has to leave every existing prompt byte-identical, or 1.1
    # silently rewrites what every 1.0.x workflow sends to the encoder.
    cite = H3._refvid_cite
    ck("no description means no line", cite("") == "" and cite(None) == "")
    ck("whitespace is not a description", cite("   ") == "")
    ck("a description cites Video 1",
       cite("a slow dolly along the counter")
       == "<Video 1> is a reference clip: a slow dolly along the counter.",
       cite("a slow dolly along the counter"))
    ck("the author's full stop is not doubled",
       cite("she turns and looks back.").endswith("looks back."),
       cite("she turns and looks back."))
    base_block = H3._assemble_next("She crosses the room.", live_picture=1,
                                   n_stills=1, identity_ordinals=[2],
                                   n_subjects=1)
    with_vid = H3._assemble_next("She crosses the room.", live_picture=1,
                                 n_stills=1, identity_ordinals=[2], n_subjects=1,
                                 refvid=cite("a slow dolly"))
    ck("an empty description changes the continuation prompt not at all",
       H3._assemble_next("She crosses the room.", live_picture=1, n_stills=1,
                         identity_ordinals=[2], n_subjects=1, refvid="")
       == base_block)
    ck("a description reaches the continuation prompt",
       "reference clip: a slow dolly" in with_vid and with_vid != base_block)

    # The reference ceiling is per ENCODE, not per plan. This counted the whole
    # rail until 1.1, so a plan that spread its references across shots was
    # refused against a limit no shot came near.
    R = sys.modules["htcpack.refs"]

    def rail(*specs):
        """specs are (tag, shots) pairs; shots=None means 'rides every shot'."""
        return json.dumps({"refs": [
            {"tag": t, "file": f"{t}.jpg", "shots": s} for t, s in specs]})

    def ref_err(*specs):
        try:
            R.parse_ref_plan(rail(*specs))
        except Exception as exc:
            return str(exc)
        return ""

    spread = [(f"r{i}", [1 + i // 3]) for i in range(12)]
    ck("12 references at 3 per shot is allowed", not ref_err(*spread))
    ck("10 references all riding every shot is refused",
       "would carry 10" in ref_err(*[(f"r{i}", None) for i in range(10)]))
    ck("10 references landing on ONE shot is refused",
       "shot 2 would carry 10" in ref_err(*[(f"r{i}", [2]) for i in range(10)]))
    # The mix is the case the old plan-wide count could not see either way:
    # 6 unscheduled + 4 on shot 3 is 10 on that shot, and 6 everywhere else.
    ck("unscheduled references count on every shot",
       "shot 3 would carry 10" in ref_err(
           *([(f"a{i}", None) for i in range(6)]
             + [(f"b{i}", [3]) for i in range(4)])))
    ck("the refusal explains where the unscheduled ones went",
       "counted on every shot" in ref_err(
           *([(f"a{i}", None) for i in range(6)]
             + [(f"b{i}", [3]) for i in range(4)])))

    # End-to-end mocked non-dry-run test of pin_mech=hand_tie_mc (catches any
    # UnboundLocalError or shape mismatch inside _ht_previs / _hop_ref2va /
    # _ht_mc_inpaint_head / _calibrate_ht_latent / _decode_av).
    def _steps_for_f(nf):
        return 2 + ((int(nf) - 5) // 17) * 5

    class _MockRef2VA:
        @staticmethod
        def execute(clip, vae, audio_vae, prompt, width, height, length,
                    ref_image_size="match", ref_images=None, ref_videos=None,
                    ref_video_audios=None, ref_audios=None):
            vt = _steps_for_f(length)
            lh, lw = 4, 4
            at = int(round(int(length) * 5.0 / 3.0))
            v = torch.randn(1, 16, vt, lh, lw) * 0.2 + 0.1
            a = torch.randn(1, 16, 2, at) * 0.1
            cond = [[torch.zeros(1, 4, 8), {"minimax_keyframes": [], "_len": int(length)}]]
            lat = {"samples": _cnt.NestedTensor([v, a]), "_len": int(length)}
            return (cond, lat)

    class _MockAddGuide:
        @staticmethod
        def execute(positive, latent, frame_idx, vae, audio_vae, image, audio,
                    strength=1.0):
            v = H3._stream_5d(H3._latents.from_dict(latent)[0])
            lh, lw = int(v.shape[3]), int(v.shape[4])
            tot_f = int(latent.get("_len", 192))
            r_idx = int(frame_idx) if int(frame_idx) >= 0 else tot_f + int(frame_idx)
            kfs = list(positive[0][1].get("minimax_keyframes") or ())
            kfs.append({
                "frame_index": int(frame_idx),
                "resolved_frame_index": r_idx,
                "latent": torch.randn(1, 16, 7, lh, lw) * 0.2 + 0.1,
                "strength": float(strength),
            })
            if audio is not None:
                kfs.append({
                    "frame_index": int(frame_idx),
                    "resolved_frame_index": r_idx,
                    "audio_latent": torch.randn(1, 16, 2, 37) * 0.1,
                    "strength": float(strength),
                })
            out_c = [[positive[0][0], dict(positive[0][1], minimax_keyframes=kfs)]]
            return (out_c,)

    class _MockPass:
        @staticmethod
        def execute(**kw):
            if "latent_image" in kw:
                return (kw["latent_image"], kw["latent_image"])
            if "steps" in kw:
                return (torch.linspace(1.0, 0.0, int(kw["steps"]) + 1),)
            return (object(),)

    class _MockVAE:
        def encode(self, imgs):
            nf = int(imgs.shape[0])
            vt = _steps_for_f(nf)
            return torch.randn(1, 16, vt, 4, 4) * 0.2 + 0.1

    def _mock_decode_av(video_vae, audio_vae, latent, skip_audio=False):
        v = H3._stream_5d(H3._latents.from_dict(latent)[0])
        vt = int(v.shape[2])
        fpt = (1, 4, 4, 4, 4)
        nf = sum(fpt[k % 5] for k in range(vt))
        imgs = torch.full((nf, 64, 64, 3), 0.45)
        sr = 44100
        ns = max(1, int(round(nf / 24.0 * sr)))
        wav = torch.zeros((1, 2, ns)) if skip_audio else torch.randn((1, 2, ns)) * 0.01
        return imgs, {"waveform": wav, "sample_rate": sr}

    H3.MiniMaxH3ReferenceToVideo = _MockRef2VA
    H3.MiniMaxH3AddGuide = _MockAddGuide
    for _mname in ("MiniMaxH3SigmaShift", "BasicScheduler", "KSamplerSelect",
                   "BasicGuider", "RandomNoise", "SamplerCustomAdvanced"):
        setattr(H3, _mname, _MockPass)
    _orig_decode_av = H3._decode_av
    _orig_offload = H3._offload_text_encoder
    _orig_qwen_pv = H3._ht_qwen_previs_moments
    _orig_fp = H3._model_fingerprint
    _orig_enc_lock = H3._encode_locked_slice
    _orig_canvas = H3._canvas
    _orig_pv_canvas = H3._ht.previs_canvas
    H3._decode_av = _mock_decode_av
    H3._offload_text_encoder = lambda *a, **k: None
    H3._ht_qwen_previs_moments = lambda s, *a, **k: s
    H3._model_fingerprint = lambda *a, **k: "mock_fp"
    H3._encode_locked_slice = lambda av, w, s0, s1, n_tok: torch.zeros(1, 16, 2, int(n_tok))
    H3._canvas = lambda *a, **k: (64, 64)
    H3._ht.previs_canvas = lambda *a, **k: (64, 64)
    try:
        _mc_plan = {"shots": [
            {"beat": "She walks down the street.", "duration": "8 s"},
            {"beat": "She passes a stall.", "duration": "8 s"},
        ]}
        _mc_out, _mc_log = run(
            vae=_MockVAE(), shot_plan=json.dumps(_mc_plan),
            pin_mech="hand_tie_mc", duration="8 s", resolution="512p (0.44 MP)",
            aspect="9:16 portrait", dry_run="off", cache_hops="off",
            hop_refine="off", ht_seam_fix="off", contact_sheet="off")
        ck("hand_tie_mc live render executes _ht_previs + _hop_ref2va + _ht_mc_inpaint_head end-to-end",
           int(_mc_out[0].shape[0]) == 362
           and "via Previs-driven Motion-Context (7 per-step keyframes + ambient audio tail)" in _mc_log,
           f"frames={int(_mc_out[0].shape[0])}")
        _mc7_plan = {"shots": [{"beat": f"Beat {k + 1} walks.", "duration": "8 s"} for k in range(7)]}
        _mc7_out, _mc7_log = run(
            vae=_MockVAE(), shot_plan=json.dumps(_mc7_plan),
            pin_mech="hand_tie_mc", duration="8 s", resolution="512p (0.44 MP)",
            aspect="9:16 portrait", dry_run="off", cache_hops="off",
            hop_refine="off", ht_seam_fix="off", contact_sheet="off")
        ck("hand_tie_mc 7-hop 2-section chained live render executes Previs 1 -> Previs 2 spine end-to-end",
           int(_mc7_out[0].shape[0]) == 1212
           and "hand-tie previs 2: rendering" in _mc7_log,
           f"frames={int(_mc7_out[0].shape[0])}")
    finally:
        H3._decode_av = _orig_decode_av
        H3._offload_text_encoder = _orig_offload
        H3._ht_qwen_previs_moments = _orig_qwen_pv
        H3._model_fingerprint = _orig_fp
        H3._encode_locked_slice = _orig_enc_lock
        H3._canvas = _orig_canvas
        H3._ht.previs_canvas = _orig_pv_canvas

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
