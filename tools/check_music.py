"""Pre-ship sanity for the soundtrack mix. CPU only, no ComfyUI, no GPU.

Audio defects are invisible: a wrong sample rate, a click once per loop, a bed
that buries the dialogue and a peak that wraps all look identical in a still and
in a frame count. Nothing about a render tells you they happened. So the mix
gets a checker that asserts the things an ear would catch, and asserts them on
synthetic material whose right answer is known in advance.

The one that actually matters is the LAST one: with nothing wired, the master
must come back bit-identical. That is the whole claim that this feature is
opt-in.
"""
import importlib.util
import math
import os
import sys

import torch

PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "htc_music", os.path.join(PACK, "music.py"))
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

FAIL = []
SR = 24000


def ck(label, ok, detail=""):
    print("    %s %s%s" % ("ok  " if ok else "FAIL", label,
                           ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(label)


def tone(secs, hz=220.0, sr=SR, ch=2, amp=0.5):
    t = torch.arange(int(secs * sr), dtype=torch.float32) / sr
    w = torch.sin(2 * math.pi * hz * t) * amp
    return w.view(1, 1, -1).expand(1, ch, -1).contiguous()


def speech(secs, bursts, sr=SR, ch=2, amp=0.8):
    """Silence with loud bursts in it -- `bursts` is [(start_s, end_s), ...]."""
    x = torch.zeros(1, ch, int(secs * sr))
    t = torch.arange(x.shape[-1], dtype=torch.float32) / sr
    car = torch.sin(2 * math.pi * 900.0 * t) * amp
    for a, b in bursts:
        i, j = int(a * sr), int(b * sr)
        x[..., i:j] = car[i:j]
    return x


print("\nto_layout")
r = M.to_layout(tone(1.0, sr=48000), 48000, 24000, 2)
ck("resamples 48k -> 24k", abs(int(r.shape[-1]) - 24000) <= 64,
   "%d samples" % int(r.shape[-1]))
ck("keeps 2 channels", int(r.shape[1]) == 2)
ck("mono -> stereo", int(M.to_layout(tone(0.5, ch=1), SR, SR, 2).shape[1]) == 2)
ck("stereo -> mono", int(M.to_layout(tone(0.5, ch=2), SR, SR, 1).shape[1]) == 1)
ck("no resample when rates match",
   int(M.to_layout(tone(1.0), SR, SR, 2).shape[-1]) == SR)

print("\nfit")
src = tone(0.7, hz=317.0)                      # period does not divide the length
want = int(3.1 * SR)
looped = M.fit(src, want, mode="loop", xfade_samples=SR // 20)
ck("loop hits the target length exactly", int(looped.shape[-1]) == want,
   "%d vs %d" % (int(looped.shape[-1]), want))
d_src = float(src.diff(dim=-1).abs().max())
d_out = float(looped.diff(dim=-1).abs().max())
# A butt-joined wrap is a step discontinuity, i.e. a sample-to-sample jump far
# larger than anything the source contains. The crossfade must leave none.
ck("loop wrap has no click", d_out < d_src * 3.0,
   "max step %.5f vs source %.5f" % (d_out, d_src))
once = M.fit(src, want, mode="once")
ck("once hits the target length", int(once.shape[-1]) == want)
ck("once leaves silence after the piece",
   float(once[..., int(0.75 * SR):].abs().max()) < 1e-6)
ck("longer than target is trimmed",
   int(M.fit(tone(5.0), 2 * SR, mode="loop").shape[-1]) == 2 * SR)

print("\nedge_fades")
f = M.edge_fades(tone(2.0), SR // 10)
ck("starts near silence", float(f[..., :8].abs().max()) < 0.02,
   "%.4f" % float(f[..., :8].abs().max()))
ck("ends near silence", float(f[..., -8:].abs().max()) < 0.02)
ck("middle is untouched", float(f[..., SR:SR + 100].abs().max()) > 0.4)

print("\nduck_gain")
sp = speech(3.0, [(1.0, 2.0)])
g0 = M.duck_gain(sp, SR, depth=0.0)
ck("depth 0 is exactly no-op", float(g0.min()) == 1.0 and float(g0.max()) == 1.0)
g = M.duck_gain(sp, SR, depth=0.6, attack_ms=15.0, release_ms=250.0)
mid = float(g[..., int(1.5 * SR)])
quiet = float(g[..., int(0.4 * SR)])
tail = float(g[..., int(2.9 * SR)])
ck("ducks while the burst plays", mid < 0.55, "gain %.3f" % mid)
ck("full level before it", quiet > 0.95, "gain %.3f" % quiet)
ck("recovers after it", tail > 0.9, "gain %.3f" % tail)
ck("never exceeds unity or goes negative",
   float(g.max()) <= 1.0 + 1e-6 and float(g.min()) >= 0.0)
ck("respects depth as the floor", float(g.min()) >= 0.4 - 1e-3,
   "min %.3f" % float(g.min()))
ck("curve is sample-length", int(g.shape[-1]) == int(sp.shape[-1]))
ck("silence ducks nothing",
   float(M.duck_gain(torch.zeros(1, 2, SR), SR, depth=0.9).min()) == 1.0)

print("\nmix")
out, note = M.mix(tone(1.0, amp=0.3), tone(1.0, hz=90.0, amp=0.3), -12.0)
ck("quiet mix does not trip the guard", note == "", note)
ck("quiet mix stays in range", float(out.abs().max()) <= 1.0)
out2, note2 = M.mix(tone(1.0, amp=0.95), tone(1.0, hz=90.0, amp=0.95), 0.0)
ck("loud mix trips the guard", bool(note2), note2)
ck("loud mix is brought back in range", float(out2.abs().max()) <= 1.0 + 1e-6,
   "peak %.4f" % float(out2.abs().max()))

print("\napply")
master = speech(4.0, [(1.0, 2.0), (2.6, 3.4)])
bed = tone(1.3, hz=110.0, amp=0.4)
mixed, note = M.apply(master, SR, bed, SR, gain_db=-10.0, duck=0.6,
                      fit_mode="loop", fade_s=0.25)
ck("length is the master's, exactly",
   int(mixed.shape[-1]) == int(master.shape[-1]),
   "%d vs %d" % (int(mixed.shape[-1]), int(master.shape[-1])))
ck("reports what it did", "soundtrack:" in note, note)
gap = float((mixed - master)[..., int(0.5 * SR):int(0.9 * SR)].abs().max())
spoke = float((mixed - master)[..., int(1.2 * SR):int(1.8 * SR)].abs().max())
ck("bed is audible in the gaps", gap > 0.02, "%.4f" % gap)
ck("bed is quieter under speech than in the gaps", spoke < gap,
   "under speech %.4f vs gap %.4f" % (spoke, gap))
ck("mismatched bed rate still fits",
   int(M.apply(master, SR, tone(1.3, sr=48000), 48000)[0].shape[-1])
   == int(master.shape[-1]))
a, _ = M.apply(master, SR, bed, SR, gain_db=-10.0, duck=0.6)
b, _ = M.apply(master, SR, bed, SR, gain_db=-10.0, duck=0.6)
ck("deterministic", torch.equal(a, b))

# The claim the whole feature rests on.
un, unote = M.apply(master, SR, None, SR)
ck("no soundtrack returns the master untouched", un is master and unote == "")

print()
if FAIL:
    print("%d FAILURE(S):\n  %s" % (len(FAIL), "\n  ".join(FAIL)))
    raise SystemExit(1)
print("MUSIC CHECK: all clear")
