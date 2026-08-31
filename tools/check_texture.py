r"""Offline tests for the texture probe -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_texture.py

A measuring instrument is only worth having if it has been read against a
signal whose true value is known. `tone_probe` shipped a confident wrong number
for weeks -- it differenced two unrelated renders that shared a cache directory
-- and nothing caught it because there was nothing to catch it with. So this
builds a synthetic three-hop cache with a texture ratchet of a **known**
amplitude injected into it, and asserts the probe recovers that amplitude.

The injected signal is band-limited on purpose. A ratchet is added at the mid
scale only, and the test asserts three things about it:

  * the mid band tracks it and the coarse band does not, so the bands are
    actually separating frequencies rather than all reading total contrast;
  * mean |Laplacian| -- the metric the probe exists to replace -- is markedly
    less sensitive to the same injected signal;
  * the box split localises it: a ratchet painted only inside the head box
    shows up there and not in the background box.

Covers, besides:
  * hopcache.chains  -- two renders in one directory stay two renders
  * latents.parts    -- the NestedTensor shim both the node and the probe use
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PACK)

import torch  # noqa: E402

import hopcache  # noqa: E402
import texture_probe as tp  # noqa: E402

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


# -- the fixture -----------------------------------------------------------

H, W = 128, 96
# The head box the probe would guess for a HxW frame, so the fixture and the
# probe agree without the test hard-coding the fraction twice.
HEAD, BG = tp.default_boxes(H, W)


def _base(n, seed):
    """A plausible frame: smooth shading plus fixed structure. No ratchet."""
    g = torch.Generator().manual_seed(seed)
    y = torch.linspace(0, 1, H).view(1, H, 1)
    x = torch.linspace(0, 1, W).view(1, 1, W)
    shade = 0.35 + 0.25 * y + 0.15 * x
    # Static texture so coarse and fine bands are non-zero to begin with.
    grain = torch.rand((1, H, W), generator=g) * 0.06
    t = torch.arange(n, dtype=torch.float32).view(n, 1, 1) / max(1, n)
    return (shade + grain + 0.02 * t).expand(n, H, W).clone()


# Deliberately NARROWER than the probe's mid window (tp.SIGMAS[0:2] = 1.0-2.5).
# A difference of Gaussians spanning the full window has tails that reach well
# into the coarse band, so injecting one and then asserting "coarse barely
# moved" tests the fixture's spectral hygiene rather than the probe's. Sitting
# inside the window keeps the injected signal where the test claims it is.
MID_LO, MID_HI = 1.25, 1.85


def _mid_noise(n, seed):
    """Noise concentrated inside the mid scale, matching tp.SIGMAS' window.

    Built as a difference of Gaussians of white noise, which is the same shape
    of filter the probe's `mid` measures -- so an amplitude injected here
    should come back out of the probe, and that is the whole assertion.
    """
    g = torch.Generator().manual_seed(seed)
    w = torch.randn((n, H, W), generator=g)
    return tp._blur(w, MID_LO, torch) - tp._blur(w, MID_HI, torch)


def _hop(n, hop_i, amp, seed=7, box=None):
    """One hop's frames, with `amp` of mid-band ratchet added.

    `box` confines the injection to a region, which is how the head/background
    split gets tested with a signal that is genuinely local.
    """
    img = _base(n, seed)
    inj = _mid_noise(n, seed + 100 + hop_i) * amp
    if box is None:
        img = img + inj
    else:
        y0, y1, x0, x1 = box
        img[:, y0:y1, x0:x1] += inj[:, y0:y1, x0:x1]
    img = img.clamp(0.0, 1.0)
    return img.unsqueeze(-1).expand(n, H, W, 3).contiguous()


def _write_chain(store, root, amps, box=None, n=24, tag="c"):
    """Write one synthetic render into the cache. -> [keys]."""
    st = store.HopStore(root)
    keys = []
    wav = torch.zeros((1, 2, 1024))
    for i, amp in enumerate(amps):
        key = "%s%02d%s" % (tag, i, "0" * (24 - 2 - len(tag)))
        st.put(key, _hop(n, i, amp, box=box), wav, 48000,
               meta={"hop": i + 1})
        keys.append(key)
    return keys


def _mid_of(imgs):
    per = tp.measure(tp.luma(imgs, torch), torch,
                     {"head": HEAD, "bg": BG, "whole": None})
    return per


def main():
    print("\ntexture_probe -- bands separate frequencies")
    # A pure mid-band injection must move `mid` far more than `coarse`.
    flat = _hop(16, 0, 0.0)
    baked = _hop(16, 0, 0.14)
    a, b = _mid_of(flat)["whole"], _mid_of(baked)["whole"]
    r_mid = float(b["mid"].mean()) / float(a["mid"].mean())
    r_coarse = float(b["coarse"].mean()) / float(a["coarse"].mean())
    ck("a mid-band injection raises the mid band", r_mid > 1.30,
       "x%.3f" % r_mid)
    # Relative, not absolute. These are Gaussian differences, not brickwall
    # filters, so a strong injection always leaks some energy into the
    # neighbouring band, and any fixed ceiling on `coarse` only holds at the
    # amplitude it was tuned for. What must be true at every amplitude is that
    # mid is the band that moves.
    ck("coarse moves less than half as much as mid",
       (r_coarse - 1) < 0.5 * (r_mid - 1),
       "mid %+.1f%% vs coarse %+.1f%%" % ((r_mid - 1) * 100, (r_coarse - 1) * 100))
    soft = _mid_of(_hop(16, 0, 0.05))["whole"]
    soft_r = float(soft["coarse"].mean()) / float(a["coarse"].mean())
    ck("and at a gentle amplitude the leak is small outright",
       soft_r < 1.10, "x%.3f" % soft_r)

    # The headline claim: |Laplacian| under-reports the same signal.
    lap_r = (float(tp.laplacian(tp.luma(baked, torch)).mean())
             / float(tp.laplacian(tp.luma(flat, torch)).mean()))
    ck("mean |Laplacian| is less sensitive than the mid band",
       (r_mid - 1) > 1.5 * (lap_r - 1),
       "mid x%.3f vs lap x%.3f" % (r_mid, lap_r))

    print("\ntexture_probe -- the box split localises a local signal")
    local = _hop(16, 0, 0.16, box=HEAD)
    per_f, per_l = _mid_of(flat), _mid_of(local)
    head_r = float(per_l["head"]["mid"].mean()) / float(per_f["head"]["mid"].mean())
    bg_r = float(per_l["bg"]["mid"].mean()) / float(per_f["bg"]["mid"].mean())
    ck("a head-only ratchet shows in the head box", head_r > 1.30,
       "x%.3f" % head_r)
    ck("and not in the background box", bg_r < 1.05, "x%.3f" % bg_r)
    ck("the two boxes disagree, which is the point",
       head_r > bg_r + 0.25, "head x%.3f vs bg x%.3f" % (head_r, bg_r))

    print("\ntexture_probe -- slope sees growth inside a hop")
    n = 40
    ramp = _base(n, 7) + _mid_noise(n, 11) * torch.linspace(
        0.0, 0.08, n).view(n, 1, 1)
    ramp = ramp.clamp(0, 1).unsqueeze(-1).expand(n, H, W, 3).contiguous()
    sl = tp.slope_pct(_mid_of(ramp)["whole"]["mid"], torch)
    ck("a within-hop ramp reports a positive slope", sl > 5.0,
       "%+.1f%%/100f" % sl)
    ck("a flat hop reports ~zero slope",
       abs(tp.slope_pct(_mid_of(_hop(n, 0, 0.02))["whole"]["mid"], torch)) < 3.0)

    print("\nlatents.parts -- the NestedTensor shim")
    import latents as lat
    t = torch.randn(2, 3, 4)
    ck("a plain tensor is one component", lat.parts(t) == [t])
    ck("an unrecognised object is None, not a guess",
       lat.parts(object()) is None)
    ck("a dict with no samples is None", lat.from_dict({"x": 1}) is None)
    ck("a dict with samples unwraps", len(lat.from_dict({"samples": t})) == 1)
    ck("rebuild round-trips a plain tensor",
       torch.equal(lat.rebuild(t, [t]), t))

    print("\nhopcache.chains -- two renders stay two renders")
    mk = lambda hops: [(h, "k%d" % i, {"written": float(i)})
                       for i, h in enumerate(hops)]
    ck("a single 3-hop run is one chain", len(hopcache.chains(mk([1, 2, 3]))) == 1)
    ck("two 2-hop runs are two chains",
       len(hopcache.chains(mk([1, 2, 1, 2]))) == 2)
    ck("runs of different length still split",
       [len(c) for c in hopcache.chains(mk([1, 2, 3, 1, 2]))] == [3, 2])

    print("\ntexture_probe -- end to end against a written cache")
    root = tempfile.mkdtemp(prefix="htc_texture_")
    try:
        store = hopcache.load_store()
        # Injected ratchet: hop 1 clean, then rising. Known by construction.
        amps = [0.0, 0.07, 0.14]
        _write_chain(store, root, amps, tag="a")
        st = store.HopStore(root)
        every = hopcache.hops(store, root)
        runs = hopcache.chains(every)
        ck("the written chain reads back as one run", len(runs) == 1,
           "%d run(s)" % len(runs))
        ck("with all three hops", len(runs[0]) == 3)

        mids = []
        for hop, key, _m in runs[0]:
            got = st.get(key)
            ck("hop %d decodes" % hop, got is not None)
            if got is None:
                continue
            mids.append(float(_mid_of(got[0])["whole"]["mid"].mean()))
        ck("the ratchet survives the cache as a monotone climb",
           len(mids) == 3 and mids[2] > mids[1] > mids[0],
           " -> ".join("%.5f" % v for v in mids))
        # Against the frames as written, not against a constant: the question
        # this answers is whether the 16-bit FFV1 round trip changes the
        # measurement, and a magic number would instead re-test the fixture.
        direct = [float(_mid_of(_hop(24, i, a))["whole"]["mid"].mean())
                  for i, a in enumerate(amps)]
        if len(mids) == 3:
            err = max(abs(c / d - 1.0) for c, d in zip(mids, direct))
            ck("and the round trip is lossless to within 1 percent",
               err < 0.01, "worst %.3f%% off" % (err * 100))

        # Two runs sharing a directory must not be differenced together.
        _write_chain(store, root, [0.0, 0.03], tag="b")
        runs2 = hopcache.chains(hopcache.hops(store, root))
        ck("a second render in the same directory is a second chain",
           len(runs2) == 2, "%d run(s)" % len(runs2))
        _s, picked, report = hopcache.select(root, None)
        ck("select() defaults to the newest", len(picked) == 2)
        ck("and says so out loud when there is a choice",
           "chain 1" in report and "reading" in report)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
