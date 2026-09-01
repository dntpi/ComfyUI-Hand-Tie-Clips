"""Peaks and trim windows, on the CPU, with no server and no GPU.

Same shape as check_music.py: `waveform.py` and the window arithmetic in
`media.py` are importable without ComfyUI, so the parts that are easy to get
subtly wrong -- an off-by-one bucket, a reversed window, a silent file dividing
by zero -- are checked here rather than discovered in a render.

The window cases are the ones that matter most. A bad window does not raise; it
quietly slices the wrong audio, and the only symptom is a render that came out
different for no visible reason.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch  # noqa: E402

import media  # noqa: E402
import waveform  # noqa: E402

FAILED = []


def ck(what, cond, note=""):
    print(f"    {'ok ' if cond else 'FAIL'}  {what}{'   ' + note if note else ''}")
    if not cond:
        FAILED.append(what)


print("peaks")

sr = 16000
# A quiet bed with one loud tick a quarter of the way in. The tick is the whole
# point: it is the landmark a person trims against, and a mean-bucketed
# waveform would smear it into the noise floor.
sig = torch.full((sr * 8,), 0.05)
sig[sr * 2:sr * 2 + 200] = 0.9
p = waveform.reduce_peaks(sig, 240)

ck("returns exactly n buckets", len(p) == 240, f"{len(p)}")
ck("normalised to 1.0", max(p) == 1.0)
ck("nothing above 1.0", all(v <= 1.0 for v in p))
ck("nothing below 0", all(v >= 0.0 for v in p))
# 2s of 8s = bucket 60. Max-bucketing keeps it at full height; a mean would
# divide the tick by the bucket width and bury it under the bed.
peak_at = p.index(1.0)
ck("the transient survives bucketing", 55 <= peak_at <= 65, f"bucket {peak_at}")
ck("the quiet bed is not normalised up to the tick",
   p[10] < 0.2, f"{p[10]:.3f}")

ck("n is clamped low", len(waveform.reduce_peaks(sig, 1)) == waveform.MIN_N)
ck("n is clamped high", len(waveform.reduce_peaks(sig, 99999)) == waveform.MAX_N)
ck("n defaults", len(waveform.reduce_peaks(sig, None)) == waveform.DEFAULT_N)

# Silence must not divide by zero. This is the empty-file case and it reaches
# the route on any brand-new upload that is still being written.
z = waveform.reduce_peaks(torch.zeros(sr), 240)
ck("silence is a flat line, not a crash", len(z) == 240 and max(z) == 0.0)
ck("an empty signal is a flat line", waveform.reduce_peaks(torch.zeros(0), 240) == [0.0] * 240)

# The remainder fold: a transient in the final fraction of a second is audible,
# so it has to be visible.
tail = torch.zeros(sr * 8 + 137)
tail[-50:] = 1.0
ck("a transient in the ragged tail is kept",
   waveform.reduce_peaks(tail, 240)[-1] == 1.0)

print("\npeaks() on an AUDIO dict")

aud = {"waveform": sig.reshape(1, 1, -1), "sample_rate": sr}
pk, secs = waveform.peaks(aud, 240)
ck("seconds comes from the samples", abs(secs - 8.0) < 1e-6, f"{secs:.3f}")
ck("n buckets from a dict", len(pk) == 240)

stereo = {"waveform": sig.reshape(1, 1, -1).repeat(1, 2, 1), "sample_rate": sr}
ck("a stereo copy of a mono file draws identically",
   waveform.peaks(stereo, 240)[0] == pk)

ck("no audio is a flat line", waveform.peaks(None, 240)[1] == 0.0)
ck("a dict with no waveform is a flat line",
   waveform.peaks({"sample_rate": sr}, 240)[1] == 0.0)

print("\nclip_window")

W = media.clip_window
ck("0/0 is the whole file", W(0, 0, 100.0) == (0.0, 100.0))
ck("end 0 means to the end", W(3, 0, 100.0) == (3.0, 100.0))
ck("a real window is kept", W(3, 9, 100.0) == (3.0, 9.0))
ck("a reversed window falls back to the whole file", W(9, 3, 100.0) == (0.0, 100.0))
ck("a start past the end falls back", W(200, 0, 100.0) == (0.0, 100.0))
ck("a window shorter than the minimum falls back",
   W(5, 5.0 + media.MIN_WINDOW_S / 2, 100.0) == (0.0, 100.0))
ck("an end past the file is clamped to it", W(3, 500, 100.0) == (3.0, 100.0))
ck("a negative start is clamped to 0", W(-5, 9, 100.0) == (0.0, 9.0))
ck("a zero-length file yields nothing", W(0, 0, 0.0) == (0.0, 0.0))
ck("junk does not raise", W("x", None, 100.0) == (0.0, 100.0))
# The load-bearing one. A window is only ever a window; it can never hand the
# encoder an empty tensor, whatever the widgets say.
ck("no input produces an empty window",
   all(W(a, b, 100.0)[1] > W(a, b, 100.0)[0]
       for a in (-1, 0, 3, 99.99, 100, 500) for b in (-1, 0, 0.01, 3, 100, 500)))

print("\nmegapixel cap")

C = media._mp_cap_size
ck("no cap is a no-op", C(1920, 1080, 0) == (1920, 1080))
ck("a cap above the image is a no-op", C(1920, 1080, 8.0) == (1920, 1080))
w, h = C(1920, 1080, 0.5)
ck("a cap below the image scales it down", w * h <= 0.5e6, f"{w}x{h}")
ck("the cap keeps the aspect", abs((w / h) - (1920 / 1080)) < 0.05, f"{w / h:.3f}")
ck("edges land on H3's 16 px grid", w % 16 == 0 and h % 16 == 0, f"{w}x{h}")
ck("a portrait image stays portrait", C(1080, 1920, 0.3)[0] < C(1080, 1920, 0.3)[1])
ck("never smaller than one grid cell", min(C(64, 64, 0.3)) >= 16)

print()
if FAILED:
    print(f"WAVEFORM CHECK: {len(FAILED)} FAILURE(S)")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("WAVEFORM CHECK: all clear")
