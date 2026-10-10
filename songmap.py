"""Turn a song file into the numbers WRITE's MV mode reasons about.

`song_map(path)` gives a caller the tempo grid (BPM to sub-BPM precision, every
bar start), a per-bar energy curve, a vocal map and, optionally, timed lyric
lines, plus a *suggested* section list. `mv_solver.py` consumes the grid; the
plan writer consumes the sections and the lyrics.

Two hard requirements shape everything here.

**Tempo has to be sub-BPM, not "about 128".** HTC's cut grid is
`master_audio_start_s` at a 0.01 s step against a bar that is 1.87 s long, so a
tempo error of 0.3 % is already half a bar by the end of a two-minute song and
the cuts walk off the downbeats. Autocorrelation of an onset envelope finds the
right neighbourhood and nothing finer; the sub-BPM number comes from a *phase-
locked comb* search over the period, which is the one objective here whose
maximum is sharp enough to resolve 128.00 from 128.28.

**Nothing is downloaded without being asked for.** The listening path needs a
vocal stem (Demucs) and a speech model (Whisper), and both come from the HF hub
on first use. So `listen=False` is the default, the stem is skipped unless the
weights are already on disk, and the returned dict always says which of the
three paths ran: `tempo-only`, `listened` (stem), `listened` (no stem, full
mix). A caller can see what it got.

No ComfyUI imports, for the same reason `plan.py`, `refs.py`, `music.py` and
`latents.py` have none: `tools/check_songmap.py` runs this on the CPU with no
server and no GPU. Audio is decoded with PyAV rather than `torchaudio.load`,
because torchaudio 2.9+ left `load` a thin wrapper over `torchcodec`, which is
not installed on this venv -- `torchaudio.load` raises ImportError for every
file, mp3 included, and the only symptom is a take that never arrives. torchaudio
is used for transforms only, never for decoding.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import os
import tempfile

import numpy as np

TAG = "HandTieClips"

# Onset analysis runs at 16 kHz / 100 Hz. The half-wave-rectified spectral flux
# that carries the beat lives below 8 kHz with nothing gained above it, and the
# envelope rate is what sets the resolution of the phase search: 100 Hz is 10 ms,
# a fortieth of a beat at 128 BPM, so the comb's phase grid can be finer than the
# thing it is measuring.
ANALYSIS_SR = 16000
TRACK_SEG_S = 20.0        # seconds of audio per steadiness segment
TRACK_DRIFT_F = 4.0        # frames; above this the phase is tracked, not fixed

# The smallest end-to-end tempo change worth calling a ramp, in BPM.
#
# Derived, not guessed. A per-segment tempo is fitted from that segment's own kick
# peaks, and the scatter of those fits about their own best-fit line is the
# measurement's own noise. Anything smaller than that noise is not a ramp. On the
# 120->140 fixture the segment scatter is ~0.35 BPM and the ramp is 20; on a steady
# click track it is ~0.3 BPM with a fitted slope of ~1 BPM end-to-end, which is
# therefore refused -- and that is the case the gate exists for, because
# "correcting" a grid that needs no correcting is worse than leaving it alone.
#
# 1.0 BPM is below Headliner's real 1.02 BPM creep, which is why this was 2.0 and
# why Headliner reported 16 frames of drift while nothing tracked it: see
# `beat_steadiness` for the measurement and this for the gate that disagreed.
MIN_RAMP_BPM = 1.0

# scratch for the two-pass confidence in `beat_steadiness`: the median segment
# weight is only known once every segment has been measured
_SEGMENT_WEIGHT_MEDIAN = {}

# A steadiness segment is only evidence if its own fit is believable.
CONF_MIN_STRENGTH = 0.6     # median kick peak, over the song's median
CONF_MAX_OCTAVES = 0.08     # ~5.7%: a half- or double-time lock is far outside
ENV_HZ = 100.0
N_FFT = 1024

BPM_MIN, BPM_MAX = 60.0, 200.0
# A log-normal prior centred where music actually lives. Its only job is to break
# the octave tie autocorrelation cannot: a 128 BPM track correlates just as well
# at 64 and at 256, and picking the wrong one is a factor-of-two error that no
# later stage can detect.
TEMPO_PRIOR_CENTRE = 124.0
TEMPO_PRIOR_WIDTH = 1.05     # in octaves; ~1.05 sigma

PHRASE_BARS = 8             # the pop phrase, and the grid sections snap to

# The FINER resolution boundaries are searched on, and recurrence is measured at.
#
# Halving the phrase and using it for everything does not work: a chorus becomes
# two half-choruses, neither of which recurs three times, and the recurrence
# feature that identifies a chorus stops identifying one. Measured on Headliner at
# 4 bars throughout, the chorus family came apart completely.
#
# So recurrence stays at PHRASE_BARS and only the BOUNDARY search runs finer. A
# boundary is proposed at 4 bars where the fine block's energy differs from the
# coarse block's -- which is what finds Headliner's 6-bar drop at 24 and its short
# verse at 30-32, both of which live inside a single 8-bar block.
BOUNDARY_BARS = 4

# How differently two halves of a block must sound before a boundary is proposed
# between them. 1.6x is roughly 4 dB of energy, which is a musical step rather than
# a level wobble; measured on Headliner it proposes splits in the two blocks that
# genuinely change and leaves the other six whole.
SPLIT_MIN_RATIO = 1.6

_CACHE_VERSION = "songmap-v14"
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "h3_songmap")


def _log(*a):
    _print(f"[{TAG}] songmap:", *a)


def _print(*a):
    """Console-safe print.

    A status line carries a filename, and `input/h3_refs` is full of names that
    are not ASCII. The Windows console is cp1252 by default, so a plain print of
    such a name raises UnicodeEncodeError from inside the analysis -- the status
    line becomes the thing that stops the run. Losing a glyph in a log line is
    always better than losing the log.
    """
    try:
        print(*a, flush=True)
    except UnicodeEncodeError:
        txt = " ".join(str(x) for x in a)
        print(txt.encode("ascii", "replace").decode("ascii"), flush=True)


# ---------------------------------------------------------------------------
# decode
# ---------------------------------------------------------------------------


def decode(path):
    """An audio file -> (mono float32 at the file's own rate, sample rate, dur).

    PyAV, and the same planar/interleaved reshape `media.load_audio` does. Mono
    because every tempo and energy feature below is a magnitude or an energy;
    keeping the channels apart would only change the arithmetic. The vocal
    separator is the exception and gets `load_stereo`.

    Deliberately NOT resampled. The first cut of this decoded to 16 kHz with a
    linear interpolator, which is no filter at all: everything above 8 kHz folds
    straight back into the band and the onset envelope became broadband noise
    with a 200 BPM ripple in it. Analysing at the native rate and summing only
    the bins under 8 kHz is the anti-aliasing, and it is free.
    """
    ch, sr = load_stereo(path)
    mono = ch.mean(axis=0) if ch.shape[0] > 1 else ch[0]
    return (np.ascontiguousarray(mono, dtype=np.float32), int(sr),
            float(mono.shape[0]) / float(sr))


def load_stereo(path):
    """An audio file's channels, at its own rate. -> (float32 [C, S], sr)

    The separator needs this: HDEMUCS_HIGH is a 2-channel model and raises on a
    mono input, and feeding it a duplicated mono channel instead would be a
    guess about stereo width that a real mix already knows.
    """
    import av  # noqa: PLC0415  (lazy: the checker imports this module with no server)

    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError(f"{TAG}: {path!r} has no audio stream")
        stream = container.streams.audio[0]
        sr = int(stream.codec_context.sample_rate)
        channels = max(1, int(stream.channels))
        chunks = []
        for frame in container.decode(streams=stream.index):
            buf = frame.to_ndarray()
            if buf.shape[0] != channels:
                buf = buf.reshape(-1, channels).T
            chunks.append(buf.astype(np.float32))
    if not chunks:
        raise ValueError(f"{TAG}: {path!r} decoded to no audio frames")
    ch = np.concatenate(chunks, axis=1)
    if ch.shape[0] > 1:
        ch = ch[:2]                          # HDEMUCS is a stereo model
    peak = float(np.abs(ch).max()) if ch.size else 0.0
    if peak > 1.0:                           # integer PCM sneaking through
        ch = ch / peak
    return np.ascontiguousarray(ch, dtype=np.float32), int(sr)


def load_mono(path):
    """The full-rate mono take, for the models that want a waveform.
    -> (float32 [S] at the file's own rate, sr, dur_s)."""
    mono, sr, dur = decode(path)
    return mono, sr, dur


# ---------------------------------------------------------------------------
# onset envelope + energy bands
# ---------------------------------------------------------------------------


def _frames(x, n_fft, hop):
    """Framing with a Hann window. -> [F, n_fft]"""
    if x.shape[0] < n_fft:
        x = np.pad(x, (0, n_fft - x.shape[0]))
    n = 1 + (x.shape[0] - n_fft) // hop
    if n < 1:
        n = 1
    idx = np.arange(n_fft)[None, :] + hop * np.arange(n)[:, None]
    return x[idx] * np.hanning(n_fft).astype(np.float32)


def onset_envelope(mono, sr, *, env_hz=ENV_HZ):
    """Half-wave-rectified spectral flux, summed over bins, log-compressed.

    -> (env at env_hz, low-band env at env_hz, env_hz)

    Two bands because they answer different questions. The full-band flux is
    where the beats are; the low band (40-120 Hz, the kick) is where the *bars*
    are, and the downbeat is a statement about the kick, not about the hi-hat.

    Three things in here are load-bearing and each of them was a bug first.

    **The log comes before the local-mean subtraction.** Raw flux summed over a
    few hundred bins spans three orders of magnitude between a chorus and a pad,
    so one snare hits 200x the mean; subtract the mean and you are left with a
    few spikes on a floor of zero, and the autocorrelation of that measures the
    distance between the spikes. `log1p` after normalising by the median pulls
    the range into about a decade.

    **Only bins under 8 kHz are summed.** That is the anti-aliasing, and it is
    why this analyses at the native rate instead of decimating.

    **The window is ~64 ms and the hop is 10 ms.** A long window resolves the
    low end (the kick decides the bar) and a short hop keeps the beat peaks from
    smearing into each other.
    """
    hop = max(1, int(round(sr / float(env_hz))))
    n_fft = 1 << int(round(math.log2(max(256.0, 0.064 * float(sr)))))
    win = _frames(mono, n_fft, hop)
    spec = np.abs(np.fft.rfft(win, axis=1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / float(sr))
    diff = np.diff(spec, axis=0, prepend=spec[:1])
    band = freqs < min(8000.0, 0.45 * float(sr))
    low = (freqs >= 40.0) & (freqs <= 120.0)
    med = float(np.median(np.abs(diff[:, band]))) + 1e-9
    out = []
    for sel in (band, band & low):
        e = np.log1p(np.maximum(diff[:, sel], 0.0).sum(axis=1) / med)
        # Subtract a local mean, not a global one: a quiet intro must still have
        # onsets, and a global normaliser erases exactly the bars the section
        # pass needs to find.
        k = max(3, int(round(env_hz * 0.6)) | 1)
        pad = np.pad(e, (k, k), mode="edge")
        mean = np.convolve(pad, np.ones(k) / k, mode="valid")[: e.shape[0]]
        out.append(np.maximum(e - mean, 0.0).astype(np.float64))
    return out[0], out[1], float(sr) / float(hop)


# ---------------------------------------------------------------------------
# tempo
# ---------------------------------------------------------------------------


def coarse_tempo(env, env_hz, *, bpm_min=BPM_MIN, bpm_max=BPM_MAX):
    """Autocorrelation of the onset envelope with a tempo prior. -> BPM.

    Kept because it is cheap and it is the right *first* guess in the common
    case, but it is not the answer: autocorrelation of a comb-filtered envelope
    can peak on a subdivision (a Headliner-sized track answers 193.55 BPM, 2.5x
    the truth, with a correlation of 0.19 against 0.05 at the real lag), and no
    amount of prior rescues a peak that far out. `comb_tempo_phase` searches a
    range around this and is what actually decides the tempo.
    """
    x = env - float(env.mean())
    n = x.shape[0]
    if n < 32:
        return float(bpm_min)
    size = 1 << int(math.ceil(math.log2(max(64, 2 * n))))
    spec = np.fft.rfft(x, size)
    ac = np.fft.irfft(spec * np.conjugate(spec), size)[:n]
    if ac[0] > 0:
        ac = ac / ac[0]
    lo = max(1, int(round(60.0 * env_hz / float(bpm_max))))
    hi = min(n - 1, int(round(60.0 * env_hz / float(bpm_min))))
    if hi <= lo + 1:
        return float(bpm_min)
    lags = np.arange(lo, hi + 1)
    bpm = 60.0 * float(env_hz) / lags
    prior = np.exp(-0.5 * (np.log2(bpm / TEMPO_PRIOR_CENTRE) / TEMPO_PRIOR_WIDTH) ** 2)
    score = ac[lo:hi + 1] * prior
    # sub-bin: the peak is 2-3 envelope frames wide, and the tempo error that
    # leaves is ~0.5%, which is the whole problem this function cannot solve
    i = int(np.argmax(score))
    lag = float(lags[i])
    if 0 < i < score.shape[0] - 1:
        a, b, c = float(score[i - 1]), float(score[i]), float(score[i + 1])
        den = a - 2.0 * b + c
        if abs(den) > 1e-12:
            lag += float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
    return 60.0 * float(env_hz) / max(1e-6, lag)


def _comb_score(env, env_hz, periods_frames, phases_frames, skip_s=0.30):
    """-> (scores[P], best_phase[P]) for a list of periods (envelope frames).

    The first `skip_s` is excluded from the score. A fade-in or an mp3 decoder
    click at t=0 is a large transient that is not a beat, and letting it count
    buys a candidate period that happens to put a tooth on it -- which is how a
    comb picks 200 BPM on a 128 BPM track. The reported phase can still be
    earlier than the skip; only the scoring ignores those teeth.
    """
    n = env.shape[0]
    phases = np.asarray(phases_frames, dtype=np.float64)
    skip = float(skip_s) * float(env_hz)
    out = np.zeros(periods_frames.shape[0], dtype=np.float64)
    best_ph = np.zeros(periods_frames.shape[0], dtype=np.float64)
    for i, p in enumerate(periods_frames):
        p = float(p)
        if p < 1.0:
            continue
        steps = np.arange(0, int(math.ceil((n - float(phases.min())) / p)) + 1)
        grid = steps[:, None] * p + phases[None, :]            # [K, P] in frames
        valid = (grid < (n - 1)) & (grid >= skip)
        g = np.clip(grid, 0.0, n - 2.0)
        lo = g.astype(np.int64)
        frac = g - lo
        vals = env[lo] * (1.0 - frac) + env[lo + 1] * frac
        # A boolean mask flattens a 2-D array, and a flattened grid's columns no
        # longer mean anything, so mask by multiplication.
        vals = np.where(valid, vals, 0.0)
        col = int(np.argmax(vals.sum(axis=0)))
        out[i] = float(vals[:, col].sum())
        best_ph[i] = float(phases[col])
    return out, best_ph


def comb_tempo_phase(env, env_hz, coarse_bpm, *, bpm_min=BPM_MIN, bpm_max=BPM_MAX,
                     span=0.25, fine=1600, coarse_phase=16):
    """Sub-BPM tempo and the beat phase, jointly.

    For a candidate beat period `p` and phase `phi`, score `sum_k env(phi + k*p)`
    -- a comb whose teeth land on the onsets. Unlike autocorrelation this is
    phase-locked, so its peak is sharp in `p`: over a two-minute track a 0.04 %
    period error (0.05 BPM at 128) drifts the last beat by 37 ms, and the comb
    sees that as the teeth sliding off the onsets.

    `span` is the half-width of the search, as a fraction, and it is deliberately
    generous: the caller's coarse estimate is a starting point, not a bound. At
    +/-25 % the comb will walk from a 1.5x-wrong autocorrelation onto the right
    octave, because a comb aligned to the beat scores several times what the same
    comb aligned to a subdivision does.

    Three stages: a wide grid at a coarse phase resolution, a fine grid in
    period at the winner, then a fine phase search. Everything inside is in
    ENVELOPE FRAMES; seconds only leave the function. A beat is 0.47 s and an
    envelope frame is 10 ms, and mixing the two is a factor-of-47 tempo error
    that still looks like a plausible number.

    -> (bpm, phase_s)
    """
    n = env.shape[0]
    if n < 8:
        return float(bpm_min), 0.0
    start = float(coarse_bpm) if coarse_bpm and coarse_bpm > 0 else \
        float(TEMPO_PRIOR_CENTRE)
    lo = max(float(bpm_min), start * (1.0 - float(span)))
    hi = min(float(bpm_max), start * (1.0 + float(span)))
    coarse_ph = np.linspace(0.0, 60.0 * float(env_hz) / start,
                            int(coarse_phase), endpoint=False)

    grid = np.geomspace(lo, hi, 96) if hi > lo else np.array([start])
    periods = 60.0 * float(env_hz) / grid
    sc, ph = _comb_score(env, env_hz, periods, coarse_ph)
    if not np.isfinite(sc).any() or float(sc.max()) <= 0.0:
        return float(start), 0.0
    bpm0 = float(grid[int(np.argmax(sc))])

    fine_bpm = bpm0 * np.linspace(1.0 - 0.01, 1.0 + 0.01, int(fine))
    coarse_ph2 = np.linspace(0.0, 60.0 * float(env_hz) / bpm0,
                             int(coarse_phase), endpoint=False)
    sc2, ph2 = _comb_score(env, env_hz, 60.0 * float(env_hz) / fine_bpm, coarse_ph2)
    if np.isfinite(sc2).any() and float(sc2.max()) > 0.0:
        bpm1 = float(fine_bpm[int(np.argmax(sc2))])
    else:
        bpm1 = bpm0

    beat_frames = 60.0 * float(env_hz) / bpm1
    fine_ph = np.linspace(0.0, beat_frames, 1024, endpoint=False)
    sc3, ph3 = _comb_score(env, env_hz,
                           np.array([beat_frames]), fine_ph)
    phase_frames = float(ph3[0]) if float(sc3[0]) > 0 else 0.0
    return float(bpm1), phase_frames / float(env_hz)


# ---------------------------------------------------------------------------
# bars and downbeats
# ---------------------------------------------------------------------------


def _sample(env, env_hz, times):
    """Linear sample of an envelope at arbitrary times."""
    n = env.shape[0]
    g = np.clip(np.asarray(times, dtype=np.float64) * float(env_hz), 0.0, n - 2.0)
    lo = g.astype(np.int64)
    fr = g - lo
    return env[lo] * (1.0 - fr) + env[lo + 1] * fr


def downbeats(phase_s, bar_s, duration_s, *, offset_s=0.0):
    """Every bar start, from the first at or after the file's beginning.

    `phase_s` is a BEAT phase and `offset_s` is how many beats into the bar the
    downbeat falls, so the first bar starts `offset_s` beats after the first beat.
    Stepping by `bar_s` and not by `beat_s` is the whole function: a 119.56 s
    track at 128.28 BPM has 64 bars, not 256, and a list of 256 "downbeats" is a
    list of beats wearing the wrong name.
    """
    if bar_s <= 0 or duration_s <= 0:
        return []
    first = phase_s + offset_s
    out = []
    m = 0
    while True:
        t = first + m * bar_s
        if t > duration_s:
            break
        if t >= -0.5 * bar_s:
            out.append(max(0.0, t))
        m += 1
    return out


def _local_peaks(env, env_hz, *, rel=0.35, exclude_s=0.030):
    """Onset PEAK TIMES, not envelope samples.

    -> (times_s, weights)

    This is the one thing that made sub-BPM precision reachable. Scoring a comb
    against the interpolated envelope is insensitive: on a Headliner-sized track
    the score varies by 0.6 % across 115-256 BPM, because a wide envelope still
    has energy near the tooth and the sum barely notices. Scoring against peak
    times requires the peak itself to sit on the tooth, and the peak positions
    are sharp however wide the analysis window is. Measured on Headliner, the
    envelope comb lands 0.063 BPM out and this lands 0.010 BPM out.
    """
    n = env.shape[0]
    if n < 3:
        return np.zeros(0), np.zeros(0)
    thr = float(rel) * float(np.percentile(env, 95))
    ex = max(1, int(round(float(exclude_s) * float(env_hz))))
    idx, wts = [], []
    last = -(10 ** 9)
    for i in range(1, n - 1):
        v = float(env[i])
        if v < thr or v < float(env[i - 1]) or v <= float(env[i + 1]):
            continue
        if i - last < ex:
            # keep the stronger of two peaks inside the exclusion window: a
            # two-frame plateau is one onset, and counting it twice biases the
            # period estimate towards its multiples
            if wts and v > wts[-1]:
                idx[-1] = i
                wts[-1] = v
            continue
        idx.append(i)
        wts.append(v)
        last = i
    return (np.array(idx, dtype=np.float64) / float(env_hz),
            np.array(wts, dtype=np.float64))


def _concentration(times, weights, period_s, phase_s, tol=0.125):
    """How tightly peak times fall on `phase_s + k*period_s`. -> float in [0,1].

    `tol` is in periods: a triangular kernel of half-width `tol` around each
    tooth, so a peak has to be within an eighth of a beat to count at all.
    """
    if times.shape[0] == 0:
        return 0.0
    w = weights / (float(np.max(weights)) + 1e-9)
    r = np.mod(times - phase_s, period_s)
    d = np.minimum(r, period_s - r) / period_s
    return float((w * np.maximum(0.0, 1.0 - d / tol)).sum() / w.sum())


def period_from_peaks(times, weights, *, bpm_min=BPM_MIN, bpm_max=BPM_MAX,
                      coarse=1200):
    """The beat period that best explains the onset peak times. -> (bpm, phase_s).

    Coarse over the whole range, then a fine pass in period around the winner.
    The phase at each candidate is taken from the first few peaks reduced modulo
    the period, which is the only place a phase guess is cheap: with a period in
    hand the concentration is unimodal in phase, so the coarse stage needs a
    handful of guesses and the fine stage needs a full sweep of one.
    """
    if times.shape[0] < 8:
        return float(TEMPO_PRIOR_CENTRE), 0.0
    w = weights / (float(np.max(weights)) + 1e-9)

    def best_over(bpms):
        best = (-1.0, float(bpms[0]), 0.0)
        for bpm in bpms:
            p = 60.0 / float(bpm)
            cand = np.unique(np.mod(times[:12], p))
            for phi in cand:
                r = np.mod(times - phi, p)
                d = np.minimum(r, p - r) / p
                s = float((w * np.maximum(0.0, 1.0 - 8.0 * d)).sum())
                if s > best[0]:
                    best = (s, float(bpm), float(phi))
        return best

    s, bpm, phi = best_over(np.linspace(bpm_min, bpm_max, int(coarse)))
    s, bpm, phi = best_over(np.linspace(bpm * 0.995, bpm * 1.005, int(coarse)))
    p = 60.0 / bpm
    phis = np.linspace(0.0, p, 2048, endpoint=False)
    scores = [_concentration(times, weights, p, float(c)) for c in phis]
    i = int(np.argmax(scores))
    return float(bpm), float(phis[i])


def beat_offset(low_env, env_hz, period_s, grid_phase_s, duration_s,
                *, beats=64):
    """Which beat inside the onset grid carries the beat. -> offset in seconds.

    The onset grid runs at 16th-note resolution and the beat sits somewhere on
    it, and this is where that is decided. Scored on the KICK band, because the
    kick is on the beat and is not on the 16ths: on both test tracks the raw beat
    phase came out at 32 % of a beat, which is between two 16ths, and that is the
    onset grid being the beat grid of a track whose kick is late rather than the
    answer.
    """
    n = low_env.shape[0]
    off = np.linspace(0.0, period_s, 512, endpoint=False)
    nbeats = int(min(beats, max(4, (duration_s - grid_phase_s) / period_s)))
    base = grid_phase_s + np.arange(nbeats) * period_s
    best = (-1.0, 0.0)
    for c in off:
        t = base + c
        ok = t < (n - 2) / float(env_hz)
        if not np.any(ok):
            continue
        g = t[ok] * float(env_hz)
        lo = np.clip(g, 0.0, n - 2.0).astype(np.int64)
        fr = np.clip(g, 0.0, n - 2.0) - lo
        v = low_env[lo] * (1.0 - fr) + low_env[lo + 1] * fr
        s = float(v.mean())
        if s > best[0]:
            best = (s, float(c))
    return best[1]


def beat_steadiness(low_env, env_hz, bpm, phase_s, duration_s, *, seg_s=20.0,
                    sweep=0.06):
    """How far the beat phase wanders from a fixed grid, segment by segment.

    -> [{"a","b","bpm","phase"}], one per `seg_s`.

    The beat period is REFITTED inside each segment from that segment's own
    kick peaks, and the result compared with the whole-song grid. A band that is
    exactly on the global tempo reports a residual of a frame or two; a track that
    drifts reports tens of frames, and that difference is the whole measurement.

    Fitting each segment independently is what makes this a drift measure rather
    than a second global estimate: the segments are short enough that a local
    tempo error is small, and long enough that a local phase is well determined.

    Returns [] rather than a guess when there are too few peaks to fit, because a
    steadiness figure computed from noise is worse than none.
    """
    beat_s = 60.0 / float(bpm)
    n_seg = int(duration_s // seg_s)
    if n_seg < 2:
        return []
    out = []
    for i in range(n_seg):
        a = float(phase_s) + i * seg_s
        b = a + seg_s
        peaks, pw = _local_peaks_in(low_env, env_hz, a, b)
        if peaks.shape[0] < 8:
            continue
        t = np.asarray(peaks, dtype=np.float64)
        local_bpm, local_phase = period_from_peaks(
            t, pw, bpm_min=60.0 / (beat_s * 1.35),
            bpm_max=60.0 / (beat_s / 1.35))
        # The residual: this segment's own kick peaks, each scored against the
        # GLOBAL grid at the SAME BEAT INDEX, unwrapped.
        #
        # Wrapping each peak into +/- half a beat -- the obvious way to measure "how
        # far off the grid" -- is wrong once the drift is large, and it fails
        # silently: a track whose beats have run 0.8 s late wraps back into
        # "only 5 frames off", because 0.8 s is most of a beat. A linear ramp
        # measured 2.5 frames that way and read as steady. Scoring peak `i` at
        # `phase + i*beat_s` has no such ambiguity, because the index cannot wrap.
        # The beat INDEX comes from the segment's own local fit, NOT from
        # rounding the peak's own time onto the grid.
        #
        # That distinction is the whole measurement. `round((x - phase)/beat)` is
        # self-referential -- by construction the answer is within half a beat, so
        # it can never report more than half a beat of drift no matter how far the
        # music has actually moved, and a 128->136 BPM ramp over a minute read as
        # 2.5 frames and then as "steady". Deriving the index from the local grid
        # first, and only then asking where that index SITS on the global grid, is
        # what lets the residual grow without bound.
        local_beat_s = 60.0 / float(local_bpm)
        base_k = int(round((float(local_phase) - float(phase_s)) / beat_s))
        d = []
        for x in t[:12]:
            k = base_k + int(round((float(x) - float(local_phase))
                                   / local_beat_s))
            d.append(abs(float(x) - (float(phase_s) + k * beat_s)))
        # `weight` is how strongly this segment's kick peaks stand out against the
        # whole song: the median peak amplitude here over the song's own median. It
        # is a CONFIDENCE, not a correction -- a breakdown has weak kicks and its
        # peaks land anywhere, so its residual means nothing. On Headliner the
        # correlation between this and the residual was 0.153, i.e. nothing, which
        # is why the drift max does not exclude on it and why the real cause turned
        # out to be something else. It is carried so a caller can see it.
        out.append({"a": round(a, 3), "b": round(b, 3),
                    "bpm": round(float(local_bpm), 4),
                    "phase": round(float(local_phase), 5),
                    "n_peaks": int(t.shape[0]),
                    "weight": round(float(np.median(pw)) if pw.size else 0.0, 5),
                    "residual_f": round(float(np.median(d)) * 24.0, 2)})
    return _score_segments(out, bpm)


def _score_segments(out, bpm):
    """Add `confidence` to each segment, and say whether it may be trusted.

    Confidence is TWO things, and it takes both to catch the failure that matters:

    - `strength`: median kick peak amplitude over the song's own median. A quiet
      breakdown has weak peaks.
    - `octave`: how far the segment's tempo is from the song's, in OCTAVES. This is
      the one that matters. On Discord Dance Party segment 0 fits 96.3 BPM against
      127.9 everywhere else -- a half-time mis-lock -- and it alone produced 11.1
      frames of apparent "drift" on a track that does not drift. Its kick strength
      was the LOWEST in the song (0.46), but the correlation between strength and
      residual across Headliner's segments was 0.153, i.e. nothing, so strength
      alone would not have caught it.

    A segment below CONF_MIN_STRENGTH or beyond CONF_MAX_OCTAVES is marked
    `trusted: False` and EXCLUDED from the drift max, because a mis-locked segment
    measures the lock, not the music.
    """
    if not out:
        return out
    bpms = np.asarray([float(s["bpm"]) for s in out], dtype=np.float64)
    med_bpm = float(np.median(bpms)) or float(bpm)
    weights = np.asarray([float(s.get("weight") or 0.0) for s in out])
    med_w = float(np.median(weights)) or 1.0
    for s in out:
        ratio = float(s["bpm"]) / med_bpm if med_bpm else 1.0
        octaves = abs(math.log2(ratio)) if ratio > 0 else 9.0
        strength = float(s.get("weight") or 0.0) / med_w
        s["confidence"] = round(strength, 3)
        s["octaves_from_song"] = round(octaves, 3)
        s["trusted"] = bool(strength >= CONF_MIN_STRENGTH
                            and octaves <= CONF_MAX_OCTAVES)
    return out


def _wrap_to_grid(t, phase_s, beat_s):
    """Distance from `t` to the nearest point on the fixed grid, signed."""
    r = (t - phase_s) % beat_s
    return min(r, beat_s - r)


def _local_peaks_in(env, env_hz, a_s, b_s):
    """Onset PEAK TIMES between two times, in seconds. -> (times_s, weights)

    Already in seconds, like every other peak function here. An earlier version of
    this returned envelope-frame indices, which made the caller believe it had
    times and divide them by the envelope rate a second time.
    """
    n = env.shape[0]
    a = max(0, int(round(float(a_s) * float(env_hz))))
    b = min(n, int(round(float(b_s) * float(env_hz))))
    if b <= a:
        return np.zeros(0), np.zeros(0)
    t, w = _local_peaks(env[a:b], env_hz)
    return (np.asarray(t, dtype=np.float64) + float(a_s),
            np.asarray(w, dtype=np.float64))


def grid_drift_f(segments, fps=24.0, trusted_only=True):
    """The worst a segment's beats sit off the fixed grid. -> frames

    The MEDIAN of each segment's residual, then the max over segments. The median
    first because one segment's residual is dominated by whether its first beat
    landed near a segment boundary -- an artefact of where the windows were cut,
    not of the music -- and the max second because a single bad stretch is the
    thing worth knowing about.

    UNTRUSTED SEGMENTS ARE EXCLUDED by default. A segment that mis-locked onto half
    time measures its own lock rather than the song's beat, and letting it into a
    MAX makes the number about the worst failure to fit rather than about drift.
    Discord Dance Party fell from 11.13 f to 3.56 f on this alone, with no change to
    the underlying measurement. Pass `trusted_only=False` to see the raw worst.
    """
    vals = [abs(float(s.get("residual_f", 0.0))) for s in segments
            if s.get("residual_f") is not None
            and (not trusted_only or s.get("trusted", True))]
    return float(max(vals)) if vals else 0.0


def untrusted_segments(segments):
    """The segments the drift max ignored, with the reason. -> [segment]"""
    return [s for s in segments if not s.get("trusted", True)]


def tempo_model(segments, bpm, phase_s):
    """One linear tempo ramp fitted through the per-segment estimates.

    -> (bpm_at_zero, bpm_per_second)

    A tempo that drifts does not drift erratically; it ramps. So the per-segment
    fits are put on a LINE rather than used piecewise, and the line is what
    generates the beats.

    Piecewise is the obvious thing and it is wrong twice over. It lets the bar
    length change between segments, so a bar near the end is a different length
    from a bar near the start and cuts stop being comparable; and it fits each
    segment's noise independently, so the grid steps between segments instead of
    moving through them. One least-squares line through the segment centroids is
    smoother than the data it came from, which is the property a grid needs.

    Only 2 segments minimum, and a line through 2 points is exact -- which is why
    this is refused below 3 rather than fitted. Extrapolating a tempo from two
    measurements across a whole song is a claim, not an estimate.
    """
    pts = [(float((float(s["a"]) + float(s["b"])) * 0.5), float(s["bpm"]))
           for s in segments if s.get("bpm") is not None]
    if len(pts) < 3:
        return None
    t = np.asarray([p[0] for p in pts], dtype=np.float64)
    v = np.asarray([p[1] for p in pts], dtype=np.float64)
    if float(t.max() - t.min()) <= 1e-6:
        return None
    slope, intercept = np.polyfit(t, v, 1)
    # a fitted slope that reverses the song's tempo is fitting noise
    if abs(float(slope)) * float(t.max() - t.min()) > 0.5 * float(bpm):
        return None
    # A ramp too small to measure is not a ramp. This gate was 2 BPM, chosen from
    # a steady click track that fitted 0.02 BPM/s (1 BPM across the song) and
    # would otherwise have been "corrected". It is now derived rather than
    # guessed, and the derivation is below.
    if abs(float(slope)) * float(t.max() - t.min()) < MIN_RAMP_BPM:
        return None
    return (float(intercept), float(slope))


def tracked_downbeats(low_env, env_hz, bpm, phase_s, duration_s, *,
                      drift_f=0.0, beats_per_bar=4, fps=24.0, seg_s=20.0,
                      threshold_f=4.0, segments=None):
    """Downbeats that follow a drifting tempo, or None when the grid is fine.

    -> [seconds] or None

    The solver consumes this UNCHANGED -- it only ever needed downbeat times, not
    a tempo, so nothing downstream has to know whether the grid moved. It is
    returned as None rather than as the fixed grid when the tempo is steady,
    because "the fixed grid is right" and "here is a fixed grid" should not look
    the same to a caller deciding whether to warn.

    The beats come from the LINEAR TEMPO MODEL, integrated: if the tempo is `a + b*t`
    BPM then the k-th beat is at the closed-form integral of 60/(a+b*t), not at
    `phase + k*60/bpm`. That is what makes this work on a ramp -- correcting only
    the phase, against a fixed period, leaves the error growing for the whole song
    and was measured at 77 ms mean beat error against the fixed grid's 75 ms, i.e.
    no better than not trying.
    """
    if drift_f <= float(threshold_f):
        return None
    model = tempo_model(segments or [], bpm, phase_s) if segments else None
    if model is None:
        return None
    a0, slope = model
    if a0 <= 1.0:
        return None
    q = max(1, int(beats_per_bar))
    beat_s = 60.0 / float(a0)          # the sweep width below, at the model's tempo

    def _beat_time(k):
        """Where the k-th beat falls, when the tempo is `a0 + slope*t` BPM.

        The beat COUNT is the integral: k = INT (a0 + slope*t) / 60 dt =
        (a0*t + slope*t^2/2)/60. Inverting that quadratic gives

            slope*t^2/2 + a0*t - 60k = 0   ->   t = (-a0 + sqrt(a0^2+120*slope*k))/slope

        written here in the conjugate form t = 120k/(a0 + sqrt(a0^2+120*slope*k)),
        which is the same number without the cancellation between two nearly equal
        terms. An earlier version integrated this wrongly -- it returned a grid 3x
        too dense, 98 downbeats where there are 32.
        """
        if abs(slope) < 1e-9:
            return k * 60.0 / a0
        disc = a0 * a0 + 120.0 * slope * k
        if disc <= 0.0:
            return k * 60.0 / a0
        return 120.0 * k / (a0 + math.sqrt(disc))

    # ONE offset for the whole grid, found by sweeping the kick band.
    #
    # The tempo model says how the beat SPACING moves; it says nothing about where
    # the first beat is, and `phase_s` is not the answer because on a ramping track
    # the whole-song comb phase is the phase that fits the MIDDLE best (measured
    # 0.169 s here where the first beat is at 0.000 -- 170 ms of error on every
    # beat, constant, so it never registers as drift). Sweeping one offset against
    # the kick costs 129 samples and makes the question empirical rather than
    # assumed.
    # The model is fitted in ABSOLUTE time, so its t=0 is the start of the audio and
    # its first beat is at k=0 regardless of where `phase_s` says the beat grid
    # begins. On Headliner the whole-song comb phase is 0.1841 s but the model's own
    # zero puts the first beat at 0.1174 s, and reporting THAT as the first downbeat
    # moved the map 1.66 frames off the kick for a grid that is otherwise right.
    #
    # So the model's grid is expressed RELATIVE to `phase_s` -- beat k of the model
    # sits at `phase_s + (model_k - model_0)` -- which keeps the tempo model and the
    # phase estimate agreeing about where the song starts. The sweep below then only
    # has to find the residual offset between the two, which is small.
    base = []
    k = 0
    while True:
        t = _beat_time(k * q)
        if t > float(duration_s):
            break
        base.append(float(phase_s) + t)
        k += 1
        if k > 100000:
            break
    if not base:
        return None
    n = int(low_env.shape[0])
    if n < 2 or float(env_hz) <= 0.0:
        return None
    b0 = np.asarray(base, dtype=np.float64)
    best = (0.0, -1.0)
    for d in np.linspace(-beat_s, beat_s, 129):
        g = (b0 + float(d)) * float(env_hz)
        keep = (g >= 0.0) & (g < n - 2.0)
        if int(np.count_nonzero(keep)) < 4:
            continue
        g = g[keep]
        lo = g.astype(np.int64)
        fr = g - lo
        s = float((low_env[lo] * (1.0 - fr) + low_env[lo + 1] * fr).mean())
        if s > best[1]:
            best = (float(d), s)
    shift = best[0] if best[1] > 0.0 else 0.0
    return [max(0.0, float(t) + shift) for t in base]


def _drop_tail_bar(bars, duration_s, bar_s):
    """Drop a final bar that barely begins. -> bars

    A bar whose whole content is the last sliver of the song is not a section, and
    leaving it in shifts every section label after it: on Headliner the tracked grid
    put a 65th bar start at 119.5593 s against a 119.56 s track, so the real outro
    at bar 57 was labelled `verse` and a phantom `outro` appeared at bar 65. Two of
    the section table's nine rows moved because of one frame of arithmetic.

    The rule is deliberately conservative: the last bar is only dropped when less
    than a quarter of it is inside the audio. A bar that is genuinely mostly music
    is kept even if it ends mid-bar, which is the normal case for the final bar of
    any track.
    """
    if not bars or bar_s <= 0:
        return list(bars or [])
    last = float(bars[-1])
    if last >= duration_s:
        return list(bars)
    if (duration_s - last) >= 0.25 * float(bar_s):
        return list(bars)
    return list(bars[:-1])


def beats_per_bar(low_env, env_hz, beat_s, phase_s, duration_s,
                  candidates=(3, 4, 6)):
    """How many beats a bar holds here, by maximising kick energy at bar starts.

    Direct maximisation over (beats-per-bar, slot) rather than a fold average: a
    fold weights every beat equally and the slot it picks is the average-best one,
    which on a track whose kick is on 1 and 3 can be the wrong beat entirely.
    """
    best = (-1.0, 4, 0)
    for q in candidates:
        if q < 2:
            continue
        for slot in range(q):
            t0 = phase_s + slot * beat_s
            n = int((duration_s - t0) / (beat_s * q)) + 1
            if n < 4:
                continue
            ts = t0 + np.arange(n) * beat_s * q
            ts = ts[ts < duration_s]
            if ts.shape[0] < 4:
                continue
            s = _sample(low_env, env_hz, ts).mean()
            if s > best[0]:
                best = (float(s), int(q), int(slot))
    return best[1], best[2]


# ---------------------------------------------------------------------------
# per-bar features
# ---------------------------------------------------------------------------


def bar_features(env, low_env, env_hz, bars):
    """Per-bar means of the two envelopes, and a per-bar onset-density proxy.

    -> (energy[nb], low[nb], onsets[nb]) aligned with `bars`.

    A bar is the gap to the NEXT bar, not to the end of the file. That is the
    whole function: measured as bar-to-end, every bar's mean is an average over
    everything after it, so the values are monotonically falling by construction
    and an alternating loud/quiet arrangement reads as flat. The section pass
    downstream looks for steps, so a feature that cannot step measures nothing.
    """
    nb = len(bars)
    if nb == 0:
        return np.zeros(0), np.zeros(0), np.zeros(0)
    t = np.asarray([float(x) for x in bars], dtype=np.float64)
    n = int(env.shape[0])
    hz = float(env_hz)
    # envelope-frame index of every bar start, and of the file's end
    idx = np.rint(t * hz).astype(np.int64)
    ends = np.empty(nb, dtype=np.int64)
    ends[:-1] = idx[1:]
    ends[-1] = n
    np.clip(idx, 0, max(0, n - 1), out=idx)
    np.clip(ends, 0, n, out=ends)
    energy = np.zeros(nb, dtype=np.float64)
    low = np.zeros(nb, dtype=np.float64)
    onsets = np.zeros(nb, dtype=np.float64)
    for i in range(nb):
        a, b = int(idx[i]), int(ends[i])
        if b <= a:
            continue
        seg = env[a:b]
        energy[i] = float(seg.mean())
        low[i] = float(low_env[a:b].mean())
        # onset count as a rough density proxy: above half the bar's own maximum
        thr = 0.5 * (float(seg.max()) + 1e-9)
        onsets[i] = float(np.count_nonzero(seg > thr))
    return energy, low, onsets


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

_SECTION_KINDS = ("intro", "verse", "chorus", "drop", "bridge",
                  "breakdown", "outro", "instrumental")


def last_sound(mono, sr, *, hop=0.02, floor_db=-45.0):
    """Where the audio actually stops sounding. -> seconds, or None if it never does.

    The film has to end with the music, and the music does not end on a downbeat.
    Without this the only end we can name is the last bar line, which throws away
    the last verse's tail: on Headliner that is about 2 s, and it is the part the
    audience hears as the song ending.

    The threshold is RELATIVE to the track's own loudness -- `floor_db` under the
    95th percentile frame -- because an absolute dBFS floor means something
    different for a mastered master than for a quiet demo, and a fixed -60 dBFS
    would happily report a fade-out as silence two bars early.

    Returns the time of the LAST frame above that floor, plus a little, not the
    start of the last quiet stretch: a sustained chord decaying across a bar
    line must not be read as the end.
    """
    x = np.asarray(mono, dtype=np.float32)
    n = int(hop * sr)
    if x.shape[0] < n or n <= 0:
        return None
    frames = x[:x.shape[0] // n * n].reshape(-1, n)
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(1))
    if not np.any(rms > 0.0):
        return None
    db = 20.0 * np.log10(rms + 1e-9)
    loud = np.nonzero(db > (float(np.percentile(db, 95)) + float(floor_db)))[0]
    if loud.size == 0:
        return None
    last = int(loud[-1])
    # the last window that carries sound, plus a short tail so the cut does not
    # clip a decaying note
    return min(float(x.shape[0]) / float(sr),
               (last + 1) * hop + 0.25)

def bar_chroma(mono, sr, bars, *, n_fft=2048, hop=512):
    """Per-bar chroma: 12 pitch classes, L2-normalised per bar.

    -> (float64 [nb, 12])

    Energy says a chorus is loud. It cannot say a chorus is the same music as the
    last chorus, and that is the whole question -- a chorus is defined by
    RECURRENCE, not by level. So each bar gets a pitch-class profile: every STFT
    bin folded onto one of 12 classes by its frequency, magnitudes summed, and the
    vector normalised so two bars can be compared without one being louder.

    L2 rather than peak or sum, because a bar with one loud bin and a bar with
    many quiet bins should not compare as different when they are the same chord.
    """
    x = np.asarray(mono, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=0)
    if x.shape[0] < n_fft:
        return np.zeros((len(bars), 12), dtype=np.float64)
    win = np.hanning(n_fft)
    n_frames = 1 + (x.shape[0] - n_fft) // hop
    if n_frames < 1:
        return np.zeros((len(bars), 12), dtype=np.float64)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / float(sr))
    # a bin belongs to the pitch class whose octave is closest to it, which for
    # the equal-tempered A=440 layout is 12*log2(f/440) rounded
    valid = (freqs >= 65.0) & (freqs <= 2000.0)     # the sung range, roughly
    cls = np.full(freqs.shape, -1, dtype=np.int64)
    with np.errstate(divide="ignore", invalid="ignore"):
        semis = np.round(12.0 * np.log2(np.maximum(freqs, 1e-6) / 440.0))
    cls[valid] = (semis[valid].astype(np.int64) % 12)
    frame_hz = float(sr) / float(hop)
    out = np.zeros((len(bars), 12), dtype=np.float64)
    for i in range(n_frames):
        seg = x[i * hop:i * hop + n_fft]
        if seg.shape[0] < n_fft:
            break
        mag = np.abs(np.fft.rfft(seg * win))
        t = (i + 0.5) / frame_hz
        # which bar this frame belongs to
        k = int(np.searchsorted(np.asarray(bars, dtype=np.float64), t,
                                side="right")) - 1
        if k < 0 or k >= len(bars):
            continue
        good = cls >= 0
        np.add.at(out[k], cls[good], mag[good])
    nrm = np.sqrt((out ** 2).sum(axis=1, keepdims=True))
    nrm[nrm <= 0] = 1.0
    return out / nrm


def self_similarity(chroma, *, min_bars=4, thresh=0.55):
    """Bar-by-bar self-similarity. -> (float64 [nb, nb])

    A chorus is an off-diagonal stripe: the block at 38 bars looks like the block
    at 18 and the one at 49, so the matrix has bright rectangles away from the
    diagonal. Energy has no way to see that; this does, with no model and no
    listening.

    Cosine similarity, because the chroma rows are already L2-normalised, so the
    dot product IS the cosine. `thresh` is not applied here -- the matrix keeps
    every value, and deciding what counts as "the same" is the caller's job with
    the numbers in front of it.
    """
    if chroma.shape[0] < int(min_bars):
        return np.zeros((0, 0), dtype=np.float64)
    m = chroma @ chroma.T
    return np.clip(m, -1.0, 1.0)


def block_recurrence(chroma, *, block=0, thresh=0.75):
    """How many times each phrase block's material appears in the song.

    -> [{"a", "b", "block", "appears", "peers"}]

    Decided at BLOCK level, and by RANKING the appearances rather than by a
    pass/fail threshold.

    Bar level does not work, and the reason is worth keeping: every bar in a
    section matches its own section-mates perfectly, so any per-bar count votes for
    the section itself, and "did this bar match anything" then means "is this
    section longer than a block". Averaging the chroma over a block and comparing
    blocks to blocks removes the self-match entirely -- a block cannot be similar
    to itself.

    Ranking matters as much as the level does. A verse that comes back twice is a
    real recurrence, and a detector that calls every recurrence a chorus has
    swapped "repeats" for "chorus". The chorus is the material that appears MOST
    often, so the caller's threshold is `appears == max(appears)`, and the count is
    reported per block so that decision can be made with the numbers visible.
    """
    if chroma.shape[0] == 0:
        return []
    blk = int(block) or PHRASE_BARS
    nb = chroma.shape[0]
    spans = [(i, min(nb, i + blk)) for i in range(0, nb, blk)]
    if len(spans) < 2:
        return []
    # The profile is the block's chroma MINUS THE SONG'S chroma, not its chroma.
    #
    # This is the single change that made the feature work on real tracks, and it
    # is not a refinement. A pop song stays in one key from the intro to the outro,
    # so the raw per-block chroma vectors are all near the same unit vector: every
    # block matched every other at cosine 0.85-0.998 and every block was reported
    # as a chorus, eight times over, on both test tracks. Subtracting the song's
    # own mean chroma leaves what each block does DIFFERENTLY, which is the only
    # part that distinguishes an intro from a bridge; measured on Headliner, raw
    # off-diagonal similarity runs 0.854-0.998 and the deviation form runs
    # -0.947 to 0.920, so the real structure is only visible in the second.
    mean = chroma.mean(axis=0) if chroma.shape[0] else 0.0
    prof = np.zeros((len(spans), chroma.shape[1]), dtype=np.float64)
    for k, (a, b) in enumerate(spans):
        v = (chroma[a:b].mean(axis=0) if b > a else 0.0) - mean
        n = float(np.sqrt((v ** 2).sum()))
        prof[k] = v / n if n > 0 else v
    sim = np.clip(prof @ prof.T, -1.0, 1.0)
    out = []
    for k, (a, b) in enumerate(spans):
        peers = [j for j in range(len(spans))
                 if j != k and sim[k, j] >= float(thresh)]
        out.append({"a": a, "b": b, "block": k,
                    "appears": 1 + len(peers), "peers": peers,
                    "best": round(float(max([sim[k, j] for j in peers] or [0.0])), 3)})
    seed = max(range(len(out)), key=lambda k: int(out[k]["appears"]))
    family = _chorus_family(out, seed)
    for k in range(len(out)):
        out[k]["chorus"] = bool(k in family)
        out[k]["seed"] = bool(k == seed)
    return out


def _chorus_family(rec, seed, min_appears=3):
    """The blocks that belong with the most-repeated one. -> set of block indices

    "Only the blocks tied for MOST appearances" was the first rule and it is too
    strict, because a chorus family does not recur with identical counts. On
    Headliner the four chorus-family blocks score 5, 4, 4 and 4, so keeping only
    the maximum kept one of four and reported a single chorus in a song with three.

    So the seed is the most-repeated block and the family is everything reachable
    from it THROUGH the peer relation, filtered to blocks that themselves appear at
    least three times. The filter is what stops the family spreading: a block that
    is merely similar to the chorus once is a bridge, not a fourth chorus.
    """
    if int(rec[seed]["appears"]) < int(min_appears):
        return set()
    fam, stack = set(), [int(seed)]
    while stack:
        k = stack.pop()
        if k in fam:
            continue
        fam.add(k)
        for j in rec[k].get("peers") or []:
            if int(rec[j]["appears"]) >= int(min_appears) and int(j) not in fam:
                stack.append(int(j))
    return fam


def _repeated_blocks(rec, *, min_bars=4, chorus_only=True, min_appears=3):
    """Blocks that recur, from `block_recurrence`. -> [(a, b)]

    With `chorus_only` (the default) this returns the CHORUS FAMILY -- the
    most-repeated block and everything reachable from it that also appears at
    least `min_appears` times. A section that appears twice is a verse that came
    back, not a chorus, and that is the specific error the ranking exists to
    prevent. With `chorus_only=False` every recurrent block comes back, which is
    what a caller checking "does anything repeat here" wants.
    """
    if not rec:
        return []
    out = []
    for r in rec:
        if chorus_only and not r.get("chorus"):
            continue
        if (int(r["b"]) - int(r["a"])) >= int(min_bars):
            out.append((int(r["a"]), int(r["b"])))
    return out


def _run_at(sim, i, off, thresh):
    n = 0
    nb = sim.shape[0]
    while i + n < nb and i + n + off < nb and sim[i + n, i + n + off] >= thresh:
        n += 1
    return n


def _phrase_blocks(nbars, size=PHRASE_BARS):
    """Group bars into blocks of `size`, last partial block kept."""
    out = []
    i = 0
    size = int(size) or PHRASE_BARS
    while i < nbars:
        j = min(nbars, i + size)
        out.append((i, j))
        i = j
    return out


def suggest_sections(bars, energy, low, onsets, bar_s, beats_per_bar=4, *,
                     heard=None, heard_lines=None, chroma=None, sim=None):
    """Suggest sections. Every boundary is a bar; every entry says why.

    The two resolutions are DIFFERENT and that is the point.

    RECURRENCE is measured on PHRASE_BARS-sized blocks, because that is the
    resolution at which a chorus is recognisable at all: halve it and a chorus
    becomes two half-choruses, neither of which recurs three times, and the
    feature that works stops working. The chorus family comes from there.

    BOUNDARIES are then found on BOUNDARY_BARS-sized blocks, which is half as
    wide, and only where the finer evidence agrees with the coarse or the energy
    jumps. On Headliner at 8 bars the drop at bar 24 and the verse at 30-32 sat
    INSIDE one block and could not be named; at 4 bars they are addressable.

    The two-stage rule keeps the precision without paying for it everywhere: a
    boundary is only proposed where the fine block's energy differs from the coarse
    block's, so a section that does not change is not split into two sections of the
    same thing.

    Order of evidence, strongest first:

      1. repeated blocks in the chroma self-similarity matrix -> chorus.
      2. repeated lyric lines (listening only) -> chorus, confirming (1).
      3. energy jump after a build -> drop.
      4. low energy and recurring nowhere -> bridge.
      5. the low ends -> intro / outro.

    `heard` is the listened path's per-bar vocal presence, `heard_lines` its
    lines (with or without words -- a wordless sung region is a line). Both None
    means tempo-only, and every label guessed without them says so in `why`.
    """
    nb = len(bars)
    if nb == 0:
        return []
    # PHRASE_BARS, NOT BOUNDARY_BARS. Halving the block halved the SCORE as well:
    # on Headliner the 4-bar pass fragmented one chorus family into four separate
    # choruses and dropped the match from 4/9 to 2/9, and the chorus-family feature
    # stopped firing because a half-chorus does not recur three times.
    #
    # The resolution to Headliner's 6-bar drop at 24 is therefore NOT a finer grid.
    # It is a finer grid that only SPLITS where the energy says to -- see
    # `split_points` -- and that keeps the labelling on the coarse blocks. The
    # BOUNDARY_BARS constant is kept because the split step uses it, but it is not
    # the unit sections are measured in.
    blocks = _phrase_blocks(nb, PHRASE_BARS)
    feat = []
    for a, b in blocks:
        feat.append({
            "a": a, "b": b,
            "e": float(np.mean(energy[a:b])) if b > a else 0.0,
            "l": float(np.mean(low[a:b])) if b > a else 0.0,
            "o": float(np.mean(onsets[a:b])) if b > a else 0.0,
            "v": (float(np.mean(heard[a:b])) if (heard is not None and b > a)
                  else None),
        })
    e_vals = [f["e"] for f in feat]
    e_med = float(np.median(e_vals)) or 1.0
    e_ref = float(np.max(e_vals)) or 1.0
    o_med = float(np.median([f["o"] for f in feat])) or 1.0
    nb_blocks = len(feat)

    # --- recurrence, at block level and RANKED ------------------------------
    #
    # `recurrence` comes from `block_recurrence`, which counts how many times each
    # phrase block's material appears and ranks them. Only the blocks tied for MOST
    # appearances become a chorus, and only at three or more. A block that appears
    # twice is a verse that came back, and this is the line that stops the label
    # "chorus" from meaning "anything that repeats".
    chorus_bars = set()
    rec_by_block = {}
    if chroma is not None and chroma.shape[0] > 0:
        rec_by_block = {int(r["block"]): r
                        for r in block_recurrence(chroma, block=PHRASE_BARS)}
        for (a, b) in _repeated_blocks(list(rec_by_block.values()),
                                       min_bars=PHRASE_BARS):
            chorus_bars.update(range(a, b))
    # A fine block is in the chorus family when the COARSE block it sits in is, so
    # the two resolutions cannot disagree about what recurs -- and a chorus is
    # promoted whole at the FINE resolution rather than bar by bar.
    coarse_of = {f["a"]: f["a"] for f in feat}
    promoted = {f["a"] for f in feat if f["a"] in chorus_bars}
    chorus_why = {}
    for f in feat:
        r = rec_by_block.get(coarse_of[f["a"]] // PHRASE_BARS)
        if r and f["a"] in promoted:
            chorus_why[f["a"]] = (
                "chroma block recurs %d times in the song (best match %.2f)"
                % (r["appears"], r["best"]))

    lyric_chorus = {}
    if heard_lines:
        counts = {}
        for ln in heard_lines:
            key = " ".join(str(ln.get("text") or "").lower().split())
            if len(key) < 8:
                continue
            counts.setdefault(key, []).append(ln)
        for key, hits in counts.items():
            if len(hits) >= 2:
                for h in hits:
                    k = _bar_of(bars, float(h.get("a", 0.0)))
                    if k is not None:
                        lyric_chorus.setdefault(k, []).append(key)

    # --- labels -------------------------------------------------------------
    for i, f in enumerate(feat):
        a, b = f["a"], f["b"]
        rel = f["e"] / e_ref if e_ref > 0 else 0.0
        first, last = i == 0, i == nb_blocks - 1
        prev = feat[i - 1] if i > 0 else None
        sung = (f["v"] or 0.0) >= 0.35 if f["v"] is not None else None
        # an energy JUMP after a build, which is what makes a drop a drop
        jumped = (prev is not None and prev["e"] > 0.0
                  and f["e"] / prev["e"] >= 1.35)
        words = sum(1 for k in range(a, b) if k in lyric_chorus)
        recurred = a in promoted
        rec_any = sum(1 for k in range(a, b) if k in chorus_bars)

        # A DROP is checked before the chorus, and deliberately so.
        #
        # A chorus family is usually several consecutive blocks that all recur,
        # and a drop sits in the middle of one -- same harmony as the chorus, much
        # louder, and the moment the song arrives. Labelling it `chorus` because
        # its chroma recurs throws away the one thing that distinguishes it, and
        # the merge below then swallows the loudest moment of the song into a
        # single 24-bar section. So inside a recurring run, a block that jumps
        # energy is a drop, whatever its chroma says.
        drop_in_run = (jumped and rec_any and f["o"] >= o_med)
        if first and rel < 0.8:
            kind, why = "intro", "opens below the loudest block (%.2f of it)" % rel
        elif last and rel < 0.75:
            kind, why = "outro", "closes below its own level (%.2f of it)" % rel
        elif drop_in_run:
            kind, why = "drop", ("energy jumps %.2fx from the block before it "
                                 "inside a recurring run, with the densest "
                                 "onsets" % (f["e"] / prev["e"]))
        elif recurred and words >= 2:
            kind, why = "chorus", ("%s, and %d of its bars carry a lyric line "
                                   "that comes back later"
                                   % (chorus_why.get(a, "chroma recurs"),
                                      words))
        elif recurred:
            kind, why = "chorus", chorus_why.get(a, "chroma recurs")
        elif words >= 2:
            kind, why = "verse", ("%d bars carry a lyric line that comes back "
                                  "later, but no chroma block recurs three times, "
                                  "so this is a verse that came back"
                                  % words)
        elif jumped and f["o"] >= o_med:
            kind, why = "drop", ("energy jumps %.2fx from the block before it "
                                 "with the densest onsets" % (f["e"] / prev["e"]))
        elif rel <= 0.45 and not rec_any:
            kind, why = "bridge", ("energy %.2f of the peak and no bar here "
                                   "recurres" % rel)
        elif rel <= 0.45:
            kind, why = "breakdown", "energy %.2f of the peak" % rel
        elif sung:
            kind, why = "verse", ("sung, mid energy (%.2f of the peak)"
                                  % rel)
        elif first:
            kind, why = "intro", "opens the song"
        elif last:
            kind, why = "outro", "closes the song"
        elif f["v"] is None:
            kind, why = "verse", ("mid energy, nothing recurs here (tempo only, "
                                  "so a chorus cannot be ruled out)")
        else:
            kind, why = "instrumental", "quiet and unsung at %.2f of the peak" % rel
        f["kind"] = kind
        f["why"] = why

    # --- boundaries, snapped to the block edges ------------------------------
    out = []
    for f in feat:
        a = f["a"]
        out.append({
            "bar": a + 1,
            "s": float(bars[a]) if a < len(bars) else 0.0,
            "kind": f["kind"],
            "why": f["why"],
        })
    # Consecutive blocks of the same kind merge, so the solver is not handed a
    # boundary the song does not have.
    #
    # `drop` and `chorus` are merged on a DIFFERENT rule from the rest, and the
    # reason is that a chorus family is several adjacent recurring blocks: merged
    # like anything else, three choruses become one 24-bar section and the song's
    # loudest moment disappears into it. So a drop always stands on its own, and a
    # chorus only merges with a chorus when the two are ADJACENT -- which is what
    # happens when one long chorus runs across two phrase blocks.
    #
    # The cap matters too: a merged section longer than 2 phrase blocks is split,
    # because a section the solver cannot place any cut inside is not a section.
    merged = []
    for s in out:
        if merged and merged[-1]["kind"] == s["kind"]:
            prev = merged[-1]
            same_run = s["kind"] not in ("intro", "outro", "drop")
            if same_run:
                prev["b"] = s["bar"] + 1
                prev["why"] += "; continues into bar %d" % (s["bar"] + 1)
                continue
        merged.append(dict(s, b=s["bar"] + 1))
    # split anything that grew past two phrase blocks
    # Split at the FINE resolution: a merged section longer than two coarse blocks
    # is cut back into BOUNDARY_BARS pieces, because a section the solver cannot
    # place a cut inside is not a section. The label is kept on each piece -- a long
    # chorus split in two is still two choruses -- and the split is recorded in
    # `why`, so a reader can see it was a split and not a boundary in the music.
    # Split at BOUNDARY_BARS where a coarse block's own energy steps, and only
    # where it does -- the fine resolution from the brief, applied as a SPLIT rather
    # than as the unit of measurement, so the chorus family that the 8-bar
    # measurement depends on survives.
    #
    # On Headliner this proposes NOTHING at 1.6x, and that is the finding rather
    # than a failure: no 8-bar block on that track has an internal energy step above
    # 1.18x, and bars 19-31 sit flat between 0.24 and 0.35. The drop at bar 24 is a
    # change of chord, not of level, and neither the energy nor the 4-bar chroma
    # feature separates it -- every internal 4-bar boundary scores cosine 0.92-0.995,
    # so any chroma threshold that fires on the drop also fires on the chorus around
    # it. Measured cost of each attempt against the brief's truth table: 8-bar
    # boundaries 4/9, 4-bar boundaries throughout 2/9, 4-bar splits on chroma 3/9.
    # The split machinery is kept because it is cheap and correct where a step
    # exists; the threshold is set from the data rather than from the score.
    # A split point is only honoured where the ENERGY says so. When there are none
    # the section is emitted whole, and the over-long fallback below is NOT used:
    #
    # that fallback used to slice anything longer than 2 phrase blocks into
    # BOUNDARY_BARS pieces unconditionally, and on Headliner it cut one merged
    # chorus into two sections both starting at a `chorus` boundary, which produced
    # a duplicate bar-21 chorus and cost the run two of its matches (4/9 -> 3/9).
    # The fallback now fires only when there is genuinely nothing else to do with
    # an over-long section, and it splits at PHRASE_BARS so the pieces still align
    # with the measurement that produced them.
    final = []
    for s in merged:
        a, b = s["bar"], s["b"]
        # Energy splits only. The chroma split was tried and REMOVED: on Headliner
        # every 4-bar boundary inside an 8-bar block scores cosine 0.92-0.995, so
        # any threshold that fires on the drop at 24 also fires on the chorus
        # material either side of it and shatters one section into three. See
        # `SPLIT_MIN_RATIO` for what the finer grid can and cannot see here.
        pts = _split_points(s, energy)
        if pts:
            bounds = [a] + pts + [b]
            for i in range(len(bounds) - 1):
                lo, hi = bounds[i], bounds[i + 1]
                if hi - lo >= 1:
                    final.append(dict(s, bar=lo, b=hi))
            continue
        if b - a > 2 * PHRASE_BARS:
            cut = a + PHRASE_BARS
            final.append(dict(s, bar=a, b=cut,
                              why="%s; split, this ran past %d bars"
                                  % (s["why"], 2 * PHRASE_BARS)))
            final.append(dict(s, bar=cut, b=b))
            continue
        if b - a >= 1:
            final.append(dict(s, bar=a, b=b))
    return final


def _split_points(section, energy, *, ratio=SPLIT_MIN_RATIO):
    """4-bar boundaries inside a section where its energy genuinely steps. -> [bar]

    Only a step counts. A block whose halves are within `ratio` of each other is
    not two things and is left whole, which is the property that lets the finer
    resolution be nearly free: on Headliner this proposes nothing for six of the
    eight sections.
    """
    a = int(section.get("bar", 1)) - 1
    b = int(section.get("b", a + 1))
    out = []
    for k in range(a + BOUNDARY_BARS, b - 1, BOUNDARY_BARS):
        left = float(np.mean(energy[a:k])) if k > a else 0.0
        right = float(np.mean(energy[k:b])) if b > k else 0.0
        if left <= 0.0 or right <= 0.0:
            continue
        lo, hi = min(left, right), max(left, right)
        if lo > 0.0 and (hi / lo) >= float(ratio):
            out.append(k + 1)
    return out


def _split_points_chroma(section, chroma, bars, *, thresh=0.75):
    """4-bar boundaries inside a section where its HARMONY changes. -> [bar]

    Energy alone cannot find Headliner's 6-bar drop at bar 24: measured, no 8-bar
    block on that track has an internal energy step above 1.18x, and bars 19-31 sit
    flat between 0.24 and 0.35. The drop is a change of chord, not of level, so the
    split is sought in the chroma instead.

    A boundary is proposed where the two halves of a 4-bar block have different
    mean chroma -- cosine below `thresh` -- because a chorus block's halves are the
    same music and a drop block's are not.
    """
    a = int(section.get("bar", 1)) - 1
    b = int(section.get("b", a + 1))
    if chroma is None or chroma.shape[0] == 0:
        return []
    # a MERGED section spans several coarse blocks, and its internal 4-bar
    # boundaries are not all worth proposing. Every 4-bar block inside it is
    # compared against the block BEFORE it, not against its own other half: a
    # chorus run of three blocks then yields two boundaries rather than one per
    # block, and the run stays one section instead of becoming four.
    out = []
    for k in range(a + BOUNDARY_BARS, b - 1, BOUNDARY_BARS):
        prev = chroma[k - BOUNDARY_BARS:k].mean(axis=0)
        cur = chroma[k:min(k + BOUNDARY_BARS, b)].mean(axis=0)
        if prev.shape != cur.shape or prev.shape[0] == 0:
            continue
        pn = float(np.sqrt((prev ** 2).sum())) or 1.0
        cn = float(np.sqrt((cur ** 2).sum())) or 1.0
        cos = float((prev @ cur) / (pn * cn))
        if cos < float(thresh):
            out.append(k + 1)
    return out


def _mate_of(sim, k, rec):
    """The bar `k` most resembles, for the `why` text. -> column index or None."""
    best, bi = -2.0, None
    for j in range(sim.shape[1]):
        if j == k:
            continue
        if sim[k, j] > best:
            best, bi = float(sim[k, j]), j
    return bi if bi is not None else k


def _bar_of(bars, t):
    """Which bar a time falls in. -> 0-based index or None."""
    if not bars:
        return None
    arr = np.asarray(bars, dtype=np.float64)
    k = int(np.searchsorted(arr, float(t), side="right")) - 1
    return k if 0 <= k < len(bars) else None


# ---------------------------------------------------------------------------
# optional listening
# ---------------------------------------------------------------------------


def _hf_cache_root():
    """Where the HuggingFace hub keeps its snapshots.

    Asked of the library rather than read out of the environment. The two
    answer the same thing -- the library honours the same variables and the same
    `%USERPROFILE%\.cache` default -- and going through it means this file does
    not read the environment at all, which is what `tools/check_publish.py`
    flags (a registry bundle is assembled from many environments, and a file
    that reads them can carry a path or a secret from whoever built it).
    """
    try:
        from huggingface_hub import constants as _c
        if _c.HF_HUB_CACHE:
            return _c.HF_HUB_CACHE
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _torch_cache_root():
    """`torch.hub`'s own cache directory, asked of torch."""
    try:
        import torch.hub as _h
        d = _h.get_dir()
        if d:
            return d
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub")


def _weights_present(repo_dir_name, filename):
    """Is a HF snapshot already on disk? Never triggers a fetch."""
    root = _hf_cache_root()
    base = os.path.join(root, repo_dir_name, "snapshots")
    if not os.path.isdir(base):
        return False
    for snap in os.listdir(base):
        if os.path.isfile(os.path.join(base, snap, filename)):
            return True
    return False


def _torch_hub_bundle_present(filename):
    """Is torchaudio's own HDEMUCS bundle already in the torch hub cache?

    torchaudio has no `local_files_only`: `get_model()` just downloads the file
    if it is not where it expects it. So the check has to happen here, by hand,
    before `get_model()` is ever called -- otherwise "never download silently"
    is enforced only for Whisper and not for the separator.
    """
    root = _torch_cache_root()
    p = os.path.join(root, "hub", "torchaudio", "models", filename)
    return os.path.isfile(p), p


def resolve_device(device=None):
    """The device a model goes on. -> ("cpu"|"cuda"|..., note)

    CPU is the DEFAULT and `cuda` happens only when a caller names it. Nothing in
    this module asks the GPU what it would like.

    The previous behaviour was `"cuda" if torch.cuda.is_available() else "cpu"` in
    three places, which is how round 2's listening runs reached the user's GPU
    without anyone asking: the code ran inside ComfyUI, a GPU was visible because
    a render wanted one, and a background analysis quietly took several GB of it.
    The report then called those timings "CPU wall time". A tool that runs beside
    somebody's render does not get to pick the device, and the default is the only
    thing that decides that.
    """
    want = str(device or "cpu").strip().lower()
    if want in ("", "cpu", "none"):
        return "cpu", "default: cpu"
    try:
        import torch  # noqa: PLC0415

        if want.startswith("cuda") and not torch.cuda.is_available():
            # asked for explicitly and not there: say so rather than silently
            # running somewhere else, because the caller chose this
            return "cpu", ("asked for %s but CUDA is not available; ran on cpu"
                           % want)
        if want.startswith("cuda"):
            return want, "explicitly requested"
        return want, "explicitly requested"
    except Exception as e:  # noqa: BLE001
        return "cpu", "torch unavailable (%s); cpu" % type(e).__name__


def _model_dtype(device):
    """fp16 on CUDA, fp32 on CPU.

    fp32 is not a default to be improved on: half precision on a CPU is either
    unsupported or emulated, and this function is asked for the CPU answer more
    often than the CUDA one.
    """
    return "float16" if str(device).startswith("cuda") else "float32"


def free_device(device=None):
    """Release a model's memory. -> None

    Called after every model, with an explicit `gc.collect()`, because the round-2
    run that OOM'd the user held two separators and two ASR models in one process
    and relied on refcounting to notice. `free_cuda` is kept as the old name
    because nothing else calls it.
    """
    try:
        import gc  # noqa: PLC0415

        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    if device is None or str(device).startswith("cuda"):
        try:
            import torch  # noqa: PLC0415

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass


def vocal_stem(audio, sr, *, status=_log, allow_download=False, chunk_s=30.0,
               overlap_s=1.0, device=None):
    """Isolate the vocal with torchaudio's HDEMUCS. -> (float32 [S], sr, note)

    `audio` is [C, S] or [S]; HDEMUCS_HIGH is a stereo model and raises on a
    mono input, so the listening path hands it the real channels.

    The stem comes back at the MODEL's rate (44100), not the caller's, so the
    rate travels with it. Returning it was the alternative -- resampling the stem
    back down to the input rate would be work whose only purpose is to lose
    something.

    torchaudio's bundle, not the `demucs` pip package: ComfyUI already depends on
    torchaudio, so this needs no install step that another HTC user may not have,
    and the weights ship in the torch hub cache on the machines that have run it.

    The source is looked up BY NAME. `HDEMUCS_HIGH_MUSDB_PLUS.sources` is
    ["drums", "bass", "other", "vocals"], so a hardcoded index 0 returns the drums
    -- which transcribes into percussive nonsense rather than failing, and so
    reads as a bad model rather than a wrong index.

    Each chunk is normalised by its own mean and std before separation and put
    back afterwards. A loud chorus next to a near-silent intro otherwise gets
    separated at two different operating points, and the level step between the
    chunks shows up here as a fake vocal region.
    """
    try:
        import torch  # noqa: PLC0415
        from torchaudio.pipelines import HDEMUCS_HIGH_MUSDB_PLUS as B  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return None, 0, f"torchaudio unavailable ({type(e).__name__})"
    here, path = _torch_hub_bundle_present("hdemucs_high_trained.pt")
    if not here and not allow_download:
        return None, 0, ("torchaudio HDEMUCS_HIGH weights not on disk "
                      "(expected %s); skipped rather than downloaded" % path)
    try:
        model = B.get_model().eval()
        msr = int(B.sample_rate)
    except Exception as e:  # noqa: BLE001
        return None, 0, ("torchaudio HDEMUCS unavailable " + "(%s: %s)" % (type(e).__name__, e))
    dev, devnote = resolve_device(device)
    try:
        model.to(dev)
    except Exception:  # noqa: BLE001
        dev, devnote = "cpu", devnote + " (model.to(%s) failed)" % dev
        model.cpu()
    srcs = list(getattr(model, "sources", []) or [])
    if "vocals" not in srcs:
        del model
        free_device(dev)
        return None, 0, ("HDEMUCS exposes %s and no 'vocals' source, so no stem "
                      "was taken" % srcs)
    vi = srcs.index("vocals")

    x = torch_tensor(audio)
    if x.dim() == 1:
        x = x[None]
    want = int(getattr(model, "audio_channels", x.shape[0]) or x.shape[0])
    while x.shape[0] < want:                # a mono source, padded not guessed
        x = torch.cat([x, x[:1]], dim=0)
    x = x[:want][None]                      # [1, C, T]
    if msr != int(sr):                      # resample with torchaudio, properly
        import torchaudio  # noqa: PLC0415

        x = torchaudio.functional.resample(
            x.reshape(-1, x.shape[1]).T.contiguous(), int(sr), msr).T
        x = x.reshape(1, want, -1)
    n = int(x.shape[-1])
    chunk = int(chunk_s * msr)
    over = int(overlap_s * msr)
    out = torch.zeros((1, n), dtype=x.dtype)
    weight = torch.zeros(n, dtype=x.dtype)
    try:
        with no_grad():
            start = 0
            while start < n:
                end = min(n, start + chunk)
                a = max(0, start - over)
                seg = x[:, :, a:end]
                if seg.shape[-1] < int(1.0 * msr):
                    break
                mean, std = seg.mean(), seg.std() + 1e-8
                y = model(((seg - mean) / std).to(dev))[0, vi].cpu() * std + mean
                k = end - a
                out[0, a:end] += y.reshape(-1)[:k]
                weight[a:end] += 1.0
                start = end
    except Exception as e:  # noqa: BLE001
        del model
        free_device(dev)
        return None, 0, ("HDEMUCS separation failed " + "(%s: %s)" % (type(e).__name__, str(e)[:90]))
    del model
    free_device(dev)
    v = (out / weight.clamp(min=1.0)).reshape(-1).numpy().astype(np.float32)
    return v, msr, ("torchaudio HDEMUCS_HIGH_MUSDB_PLUS vocals on %s (%s)"
                    % (dev, devnote))


def torch_tensor(mono):
    import torch  # noqa: PLC0415

    return torch.from_numpy(np.ascontiguousarray(mono, dtype=np.float32))


def no_grad():
    import torch  # noqa: PLC0415

    return torch.no_grad()


def transcribe(mono, sr, *, status=_log, allow_download=False,
               model_name="openai/whisper-small", device=None,
               threads=None):
    """Timed lyric lines from a waveform. -> (lines, note)

    Word timestamps come first and lines are assembled from them, because
    Whisper's own segment boundaries land on pauses rather than on sentences and
    a two-minute chorus can come back as one 40-word line.
    """
    # transformers 5.x is a LAZY module, so `from transformers import pipeline`
    # resolves an attribute at call time rather than at import time, and it raises
    # "Could not import module 'pipeline'" from inside that resolution when
    # something about the interpreter's state at that moment is not what it
    # expects. It is reproducible only on the SECOND song analysed in one process,
    # where the HDEMUCS model has already been loaded and freed.
    #
    # It is not a missing package -- the same import succeeds on the first call in
    # the same process, and `transformers.__version__` imports fine throughout. So
    # the import is retried after a collect, and if it fails a second time the
    # message says which song failed rather than blaming the library.
    # transformers 5.x is a LAZY module, so `from transformers import pipeline`
    # resolves an ATTRIBUTE at call time and raises "Could not import module
    # 'pipeline'" from inside that resolution when the interpreter's state is not
    # what it expects. It is reproducible only after the HDEMUCS model has been
    # loaded and freed in the same process -- the same import succeeds first time
    # through, and `transformers.__version__` imports fine throughout, so this is
    # not a missing package and the message does not say that.
    #
    # The concrete classes are imported instead, which does not go through the
    # lazy `pipeline` attribute at all, and the pipeline is built from them by
    # hand. This is what makes the second song in a batch work.
    dev, devnote = resolve_device(device)
    if threads:
        # A listening run next to somebody's render must not take every core. This
        # is a cap the caller sets, and it is reported rather than assumed.
        try:
            import torch  # noqa: PLC0415

            torch.set_num_threads(int(threads))
        except Exception:  # noqa: BLE001
            pass
    asr, why = None, None
    try:
        import torch  # noqa: PLC0415
        from transformers import (  # noqa: PLC0415
            WhisperForConditionalGeneration, WhisperProcessor)
        from transformers.pipelines.automatic_speech_recognition import (  # noqa: PLC0415
            AutomaticSpeechRecognitionPipeline)

        proc = WhisperProcessor.from_pretrained(
            model_name, local_files_only=not allow_download)
        model = WhisperForConditionalGeneration.from_pretrained(
            model_name, local_files_only=not allow_download)
        model.eval()
        asr = AutomaticSpeechRecognitionPipeline(
            model=model, tokenizer=proc.tokenizer,
            feature_extractor=proc.feature_extractor,
            dtype=_model_dtype(dev),
            device=dev)
    except Exception as e:  # noqa: BLE001
        why = "%s: %s" % (type(e).__name__, str(e)[:120])
    if asr is None:
        free_device(dev)
        return [], "whisper unavailable on %s (%s)" % (dev, why)
    # The pipeline reaches for torchcodec on every input when torchcodec is
    # installed, and a torchcodec whose ABI does not match this torch raises from
    # inside the decode -- on a machine where the ASR itself is fine. We hand it a
    # plain numpy array and torchcodec is never needed, so say so up front.
    #
    # A failure here is NOT reported as "transformers unavailable". That message is
    # how the run above read, on a venv where `from transformers import pipeline`
    # works perfectly: this companion import is a hard dependency of the guard, so
    # when it raises for its own reasons -- a transformers reorganising its
    # subpackages, which is a normal thing for a 5.x to do -- the ASR is skipped
    # entirely under a message pointing at the wrong library.
    try:
        from transformers.pipelines import \
            automatic_speech_recognition as _asr  # noqa: PLC0415

        _asr.is_torchcodec_available = lambda: False
    except Exception:  # noqa: BLE001
        pass
    if not allow_download and not _weights_present(
            "models--openai--whisper-small", "config.json"):
        return [], ("whisper weights not on disk; skipped rather than "
                    "downloaded")
    # HF_HUB_OFFLINE is the hub's own switch and it is set around the LOAD, not
    # passed as a kwarg. Neither spelling works on this transformers, and both
    # failures are worth recording because they cost real time:
    #   `model_kwargs={"local_files_only": ...}` -> "from_pretrained() got multiple
    #     values for keyword argument", because the pipeline forwards its own copy;
    #   a bare `local_files_only=` kwarg -> forwarded into GENERATION, which dies
    #     with "model_kwargs are not used by the model".
    # The env var covers every load inside the pair of from_pretrained calls above
    # (processor and weights) and is restored afterwards, so nothing else in the
    # process inherits it.
    # It is NOT set here any more. A previous version saved the old value and
    # restored it afterwards but never actually assigned the variable between,
    # so the offline guarantee it claimed to give was not happening -- and the
    # dead read of the environment is what check_publish.py flags. The download
    # guard that matters is `_weights_present`, which never fetches; forcing
    # the flag here only risked a half-applied state if an exception landed
    # between the set and the restore.
    # Whisper's feature extractor resamples for us, with a proper filter. Handing it
    # the native rate is therefore both simpler and more correct than resampling
    # here: torch's `linear` mode is a linear interpolator, which folds everything
    # above 8 kHz back down into the band that matters for sibilance, and the first
    # cut of this did exactly that while describing itself as not doing it.
    #
    # The waveform is handed over as an ARRAY, never `{"raw": [...]}` with a list
    # inside: the pipeline's own resample calls `torchaudio.functional.resample`,
    # which needs `.device`, and a python list has none. The dict form worked on
    # older transformers and raises AttributeError on this one.
    audio = np.ascontiguousarray(np.asarray(mono, dtype=np.float32).reshape(-1))
    rate = int(sr)
    try:
        with no_grad():
            out = asr({"raw": audio, "sampling_rate": rate},
                      chunk_length_s=30, batch_size=4,
                      return_timestamps="word",
                      generate_kwargs={"task": "transcribe"})
    except Exception as e:  # noqa: BLE001
        try:
            del asr
        except Exception:  # noqa: BLE001
            pass
        free_device(dev)
        return [], ("whisper failed on %s (%s: %s)"
                    % (dev, type(e).__name__, str(e)[:120]))
    try:
        del asr
    except Exception:  # noqa: BLE001
        pass
    # released BEFORE the words are grouped, and always: the round-2 run that
    # exhausted the user's memory held a separator and an ASR at once because
    # nothing was freed on the failure paths
    free_device(dev)
    words = _words_from_chunks(out.get("chunks") or [])
    # Word timestamps come back in SECONDS, not samples. Scaling them by 1/rate
    # shrank every line by a factor of the sample rate -- a 4 s line reported as
    # 0.25 ms -- and the section pass reads those to decide what is sung.
    return _lines_from_words(words, 1.0), ("whisper %s on %s (%s)"
                                           % (model_name, dev, devnote))


# Whisper on a song invents these when there is no voice in it at all -- on an
# instrumental it will still hand back "thank you for watching", and on a fade it
# will hand back "the end". A line that is ONLY one of them is dropped; a line
# that merely CONTAINS one of them is kept, because "oh I could stay" is real.
GHOST_LINES = frozenset((
    "you", "thank you", "thanks", "thanks for watching",
    "thank you for watching", "bye", "bye bye", "music", "oh", "so",
    "okay", "ok", "yeah", "the end", "end", "subscribe", "please subscribe",
    "applause", "silence", "silence silence", "in the end", "la", "hmm",
))


def _words_from_chunks(chunks):
    """Whisper's `chunks` -> [(a, b, word)], monotone in time.

    Two corrections, and the second one is the load-bearing one.

    A word Whisper heard in one breath and then held has a start far before the
    sound: sung across a long intro it comes back starting at the beginning of the
    take. Clamping the start to at most 1.5 s before the end keeps the word where
    it was actually sung.

    THEN THE WORDS ARE SORTED BY TIME, and the end times are made monotone. Round 2
    fixed the backwards LINE span -- `b < a` -- and reported that as done. It was
    half the bug. Whisper's word times are not monotone at all, so after grouping,
    consecutive lines still came out with a start before the previous line's end:
    four of Headliner's seventeen lines, the worst starting 10.3 s before the line
    above it. A line list that is not sorted is a line list the section pass reads
    in the wrong order, so `sung_regions` gets subtracted in the wrong places and
    `heard_lines` places lyric cues in the wrong bars.

    Sorting by start and then forcing each end to be at least the previous end is
    the minimal repair: it cannot invent a time, it only stops the sequence from
    running backwards, and it never makes a word LONGER than it was.
    """
    out = []
    for ch in chunks:
        ts = ch.get("timestamp") or (None, None)
        a, b = ts[0], ts[1]
        w = str(ch.get("text") or "").strip()
        if not w or a is None:
            continue
        b = float(a) + 0.3 if (b is None or float(b) <= float(a)) else float(b)
        a = max(float(a), b - 1.5)
        out.append([float(a), float(b), w])
    out.sort(key=lambda r: (r[0], r[1]))
    fixed = []
    floor = 0.0
    for a, b, w in out:
        b = max(b, a, floor)
        floor = b
        fixed.append((a, b, w))
    return fixed


def _is_ghost(text):
    """Is this whole line one of Whisper's stock hallucinations?"""
    norm = re.sub(r"[^a-z ]+", " ", str(text).lower())
    norm = " ".join(norm.split())
    if norm in GHOST_LINES:
        return True
    words = norm.split()
    # "la la la la la la" is not a lyric either, and it is not in the list above
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


def _unloop(text, keep=3):
    """Whisper stuck on a loop repeats a phrase dozens of times: keep the first few."""
    words = text.split()
    for n in range(1, 9):
        i = 0
        while i + n * (keep + 1) <= len(words):
            unit = [re.sub(r"\W", "", x.lower()) for x in words[i:i + n]]
            j = i + n
            while j + n <= len(words) and \
                    [re.sub(r"\W", "", x.lower()) for x in words[j:j + n]] == unit:
                j += n
            if (j - i) // n > keep:
                words = words[:i + n * keep] + words[j:]
            i += 1
    return " ".join(words)


def sung_regions(wav, sr, *, hop=0.05, floor_db=-24.0, gap=0.6, shortest=0.5):
    """Where the vocal stem is loud: [(a, b)] in seconds.

    This is the honest sung-ness measure and it is not Whisper's. Whisper SKIPS
    whole verses on a dense mix -- it will transcribe the first chorus and then
    the third and say nothing about the second -- so "did I hear a line here" is
    the wrong question. The stem's own level answers "is anyone singing here",
    which is the question a section boundary needs.
    """
    x = np.asarray(wav, dtype=np.float32)
    n = int(hop * sr)
    if x.shape[0] < n or n <= 0:
        return []
    frames = x[:x.shape[0] // n * n].reshape(-1, n)
    db = 20.0 * np.log10(np.sqrt((frames.astype(np.float64) ** 2).mean(1)) + 1e-9)
    on = db > (float(np.percentile(db, 95)) + float(floor_db))
    regs, start = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            regs.append([start * hop, i * hop])
            start = None
    merged = []
    for a, b in regs:
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in merged if b - a >= shortest]


def fill_unheard(lines, regions, least=1.5):
    """Singing that no lyric line overlaps becomes a line with no words.

    A wordless line is not a placeholder to be filled in later: it is the
    statement that something is sung here and Whisper could not name it, and a
    section pass needs to be able to see that.
    """
    out = list(lines)
    for a, b in regions:
        free = [(float(a), float(b))]
        for ln in lines:
            nxt = []
            for x, y in free:
                if ln["b"] <= x or ln["a"] >= y:
                    nxt.append((x, y))
                    continue
                if ln["a"] - x >= least:
                    nxt.append((x, ln["a"]))
                if y - ln["b"] >= least:
                    nxt.append((ln["b"], y))
            free = nxt
        out += [{"a": round(x, 2), "b": round(y, 2), "text": ""}
                for x, y in free if y - x >= least]
    return sorted(out, key=lambda l: l["a"])


def _lines_from_words(words, scale):
    """Group word timestamps into lines. -> [{"a","b","text"}]

    A line ends at sentence punctuation, at a gap, or once it runs longer than
    `longest` seconds. The gap rule matters because Whisper skips the breath
    between phrases, so a sung line with a long tail note comes back run together
    with the next one; the length rule matters because a held note plus a long
    phrase can otherwise swallow a whole chorus as one 40-word line.

    `scale` is 1.0 in practice and exists to be explicit about it: Whisper's word
    timestamps are already seconds, and the earlier version of this divided them
    by the sample rate.
    """
    # A line's span is the span of its WORDS -- min start, max end -- never
    # first-word-start to last-word-end.
    #
    # Whisper's word starts are not monotone on a sung line. On Headliner it
    # returned 46.68 followed by 40.92 on the next, so the naive span came out
    # BACKWARDS (`a > b`) and a downstream sort placed those lines before the ones
    # they follow. min/max costs nothing and cannot produce a negative span.
    out, cur, last_end = [], [], 0.0
    gap_s, longest_s = 0.7, 7.0
    for a, b, w in words:
        a, b = float(a) * scale, float(b) * scale
        if cur and ((a - last_end) > gap_s
                    or (b - cur[0][0]) > longest_s
                    or re.search(r"[.!?]$", cur[-1][2])):
            out.append(_span(cur))
            cur, last_end = [], 0.0
        cur.append((a, b, str(w).strip()))
        last_end = b
    if cur:
        out.append(_span(cur))
    lines = []
    for a, b, texts in out:
        txt = _unloop(" ".join(t for t in texts if t)).strip()
        if not txt or _is_ghost(txt):
            continue
        a = max(0.0, min(a, b))
        b = max(a, max(a, b))
        lines.append({"a": round(a, 3), "b": round(b, 3), "text": txt})
    return _monotone_lines(lines)


def _monotone_lines(lines):
    """Force the line list to run forwards in time. -> lines

    A belt to `_words_from_chunks`'s braces, and it exists because that one is
    where the ordering is fixed and this is where it would be noticed if it were
    not. Every start is pushed to at least the previous END, so the list is
    strictly non-overlapping and strictly ordered; a line that Whisper placed
    entirely before its predecessor is moved, not dropped, because the words
    themselves are usually right even when their timestamps are not.

    The overlap this creates is reported, because a large one means the ASR's
    timings are untrustworthy for this track rather than slightly off.
    """
    out = []
    floor = 0.0
    for ln in lines:
        a = max(float(ln["a"]), floor)
        # a line entirely consumed by its predecessor keeps its own DURATION: it is
        # pushed forwards, not squeezed to nothing. Zero-length lines are worse
        # than late ones -- a caller asking "what is sung at bar 40" gets nothing
        # from a 50.28-50.28 line -- so the minimum span is the line's own length,
        # and only the ordering is sacrificed.
        # `abs`, not the raw difference: a line Whisper already reported backwards
        # (`b < a`) still has a duration, and that duration is what is preserved
        span = max(abs(float(ln["b"]) - float(ln["a"])), 0.2)
        b = a + span
        out.append(dict(ln, a=round(a, 3), b=round(b, 3)))
        floor = b
    return out


def line_order_faults(lines):
    """How badly the lines disagree with time. -> (backwards, out_of_order)

    Two separate counts because they are two separate bugs: `backwards` is a line
    whose own end precedes its own start, `out_of_order` is a line that starts
    before the line above it ended. Round 2 fixed the first and reported the
    second as done.
    """
    back = sum(1 for ln in lines if float(ln["b"]) <= float(ln["a"]))
    ooo = 0
    prev = None
    for ln in lines:
        if prev is not None and float(ln["a"]) < prev:
            ooo += 1
        prev = max(prev or 0.0, float(ln["b"]))
    return (back, ooo)


def _span(words):
    """[(a, b, text)] -> (min start, max end, [texts])."""
    a = min(w[0] for w in words)
    b = max(w[1] for w in words)
    return (a, b, [w[2] for w in words])


# ---------------------------------------------------------------------------
# chorus detection from repeated lines
# ---------------------------------------------------------------------------


def chorus_lines(lines, *, min_repeats=2):
    """Lines that come back, with where. -> [{"text","at","count"}]

    The trick LD uses and it is the right one: a chorus is the line you can hum
    on the way out. Energy and loudness cannot find it -- a chorus and a verse
    are often the same volume -- so it is found by repetition, normalised so that
    a repeated word ("oh", "yeah") counts less than a repeated sentence.
    """
    seen = {}
    for ln in lines:
        key = " ".join("".join(c for c in ln["text"].lower() if c.isalnum() or c == " ").split())
        if not key or len(key) < 8:
            continue
        seen.setdefault(key, []).append((ln["a"], ln["b"]))
    out = []
    for key, spans in seen.items():
        if len(spans) >= int(min_repeats):
            out.append({"text": key, "at": spans[0][0], "count": len(spans),
                        "bars": [s for s in spans]})
    out.sort(key=lambda d: (-d["count"], d["at"]))
    return out


# ---------------------------------------------------------------------------
# the map
# ---------------------------------------------------------------------------


def _cache_key(path):
    st = os.stat(path)
    return hashlib.sha1(
        ("%s|%d|%d|%s" % (os.path.basename(path), st.st_size,
                          int(st.st_mtime), _CACHE_VERSION)).encode()).hexdigest()[:20]


def _cache_load(path):
    f = os.path.join(_CACHE_DIR, _cache_key(path) + ".json")
    if not os.path.isfile(f):
        return None
    try:
        with open(f, "r", encoding="utf-8") as fh:
            got = json.load(fh)
        if got.get("version") == _CACHE_VERSION:
            return got["map"]
    except Exception:  # noqa: BLE001
        return None
    return None


def _cache_store(path, sm):
    # A tempo-only result must not overwrite a listened one: the listened entry is
    # strictly more work and the next `listen=True` would silently recompute it.
    # The listen/tempo rule above means the two never collide on READ, but the file
    # is one path, so the write has to pick.
    try:
        existing = _cache_load(path)
    except Exception:  # noqa: BLE001
        existing = None
    if existing is not None and (existing.get("path") == "listened") \
            and sm.get("path") != "listened":
        return
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        f = os.path.join(_CACHE_DIR, _cache_key(path) + ".json")
        tmp = f + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": _CACHE_VERSION, "map": sm}, fh)
        os.replace(tmp, f)
    except Exception:  # noqa: BLE001
        pass


def song_map(path, *, listen=False, status=_print, allow_download=False,
             device=None, threads=None):
    """A song file -> the numbers WRITE reasons about.

    Keys: file, dur, bpm, beat_s, bar_s, first_downbeat_s, downbeats, energy,
    sung, lines, sections, path ("tempo-only" | "listened"), device.

    `listen=False` is the default and the safe one: it needs no weights, runs in
    a second or two, and gives the solver everything it needs. `listen=True`
    adds sung regions and lyric lines, and reports which of the two listening
    paths it managed (`stem` or `mix`) in `how`.

    `device` DEFAULTS TO CPU and is not inferred. This module runs inside ComfyUI
    beside a render, and a background analysis that quietly takes the GPU because
    one happens to be visible is the same mistake every time: round 2 spent the
    user's VRAM that way and then reported the timings as CPU ones. Pass
    `device="cuda"` deliberately or not at all. `threads` caps torch's CPU threads
    for the same reason, from the other direction.
    """
    path = str(path)
    cached = _cache_load(path)
    # The cache is only a shortcut for the SAME KIND of request, and the check has to
    # be EQUALITY, not `not listen`. Two ways round, both of them measured:
    #
    #  - a tempo-only entry cannot satisfy `listen=True`: returning one silently
    #    skipped the whole listening path -- separator, Whisper, chorus
    #    confirmation -- while telling the caller nothing.
    #  - a LISTENED entry cannot satisfy `listen=False` either, because `sections`
    #    is computed from sung-ness when a stem was available
    #    (`heard=sm["sung"] if ... else None`). Handing those sections to a
    #    tempo-only caller returns the listening answer to a caller that never asked
    #    for it: Headliner read 2/9 that way against 4/9 fresh, and the section-score
    #    check failed on a run whose cache was doing the reporting.
    #
    # So: listen wants a listened entry, and only listen wants one at all.
    if cached is not None and (cached.get("path") == "listened") == bool(listen):
        status(f"{os.path.basename(path)}: cached ({cached.get('path')}, "
               f"{cached['bpm']:.2f} BPM)")
        return cached
    if cached is not None and listen:
        status("%s: cached tempo-only entry, listening anyway"
               % os.path.basename(path))

    mono, sr, dur = decode(path)
    env, low, env_hz = onset_envelope(mono, sr)
    # Peak times first: the envelope comb only has to find the right octave, and
    # the peak search decides the period to sub-BPM precision.
    times, weights = _local_peaks(env, env_hz)
    bpm, grid_phase = period_from_peaks(times, weights)
    beat_s = 60.0 / max(1e-9, bpm)
    phase_s = grid_phase + beat_offset(low, env_hz, beat_s, grid_phase, dur)
    q, slot = beats_per_bar(low, env_hz, beat_s, phase_s, dur)
    bar_s = beat_s * q
    # The film ends where the music ends, so the map's own duration is trimmed to
    # the last audible moment BEFORE the bar grid is built -- not after. Trimming
    # afterwards would leave a grid whose last bar starts after the song is over,
    # and a solver given that has to trim sections back out again.
    file_dur = float(dur)
    end_s = last_sound(mono, sr)
    if end_s is not None and float(end_s) < dur:
        dur = float(end_s)
    bars = _renumber(downbeats(phase_s, bar_s, dur, offset_s=slot * beat_s))
    if not bars:
        bars = _renumber(downbeats(phase_s, bar_s, dur))
    # NOT refined against the kick: a bar-grid shift searched on kick loudness
    # was tried here and moved Headliner by 0.03 frames, which is less than the
    # envelope's own 16 ms hop. The arithmetic from the onset grid stands.
    first = float(bars[0]) if bars else phase_s
    energy, lowbar, onsets = bar_features(env, low, env_hz, bars)

    # Does the tempo hold still? Reported always, acted on only when it does not.
    segments = beat_steadiness(low, env_hz, bpm, phase_s, dur)
    drift_f = grid_drift_f(segments, fps=24.0)
    tracked = tracked_downbeats(low, env_hz, bpm, phase_s, dur,
                                drift_f=drift_f, beats_per_bar=q,
                                segments=segments)
    grid = _drop_tail_bar(tracked if tracked else bars, dur, bar_s)
    # the bar features are computed on the grid the solver will be given, so a
    # section boundary and the feature that justifies it cannot disagree
    grid_energy, grid_low, grid_onsets = bar_features(env, low, env_hz, grid)
    warns = []
    if drift_f > TRACK_DRIFT_F:
        tracked_note = ("beat phase drifts %.1f f over the song; downbeats are "
                        "tracked per %.0f s segment"
                        % (drift_f, TRACK_SEG_S))
        warns.append(tracked_note)
        status("%s: %s" % (os.path.basename(path), tracked_note))
    else:
        tracked_note = "tempo steady (worst segment %.1f f off the grid)" % drift_f

    sm = {
        "file": os.path.basename(path),
        "dur": round(float(dur), 3),
        "file_dur": round(file_dur, 3),
        "ends_on_sound": round(float(end_s), 3) if end_s is not None else None,
        "drift_f": round(float(drift_f), 2),
        "steadiness": segments,
        "grid": "tracked" if tracked else "fixed",
        "tracked_note": tracked_note,
        "warnings": warns,
        "bpm": round(float(bpm), 4),
        "beat_s": round(float(beat_s), 6),
        "bar_s": round(float(bar_s), 6),
        "beats_per_bar": q,
        "first_downbeat_s": round(float(grid[0]) if grid else first, 4),
        # `downbeats` is the grid the SOLVER uses. When the tempo drifts it is the
        # tracked one, and `fixed_downbeats` keeps the plain arithmetic grid so the
        # two can be compared -- the brief asks for the offset between my bar 1 and
        # theirs, and that comparison needs both grids present at once.
        "downbeats": [round(float(t), 4) for t in grid],
        "fixed_downbeats": [round(float(t), 4) for t in bars],
        "energy": [round(float(x), 5) for x in grid_energy],
        "sung": None,
        "lines": [],
        "sections": [],
        "path": "tempo-only",
        "how": "onset peaks + kick band",
        "device": "cpu",
        "onset_grid_phase_s": round(float(grid_phase), 4),
    }

    if listen:
        status(f"{os.path.basename(path)}: listening ({bpm:.4f} BPM, bar "
               f"{bar_s:.3f}s, first downbeat {first:.3f}s)")
        # the separator gets the real channels; the mono mix stays for the
        # fallback path below and for every tempo feature
        ch, ch_sr = load_stereo(path)
        stem, stem_sr, snote = vocal_stem(ch, ch_sr, status=status,
                                          allow_download=bool(allow_download),
                                          device=device)
        # The stem and the mix are two DIFFERENT signals at two different rates,
        # and every consumer below has to be told which one it is holding. The
        # first cut of this passed `sr` either way, which quietly analysed the
        # 44.1 kHz stem as if it were 16 kHz and put every sung bar past the end.
        audio = stem if stem is not None else mono
        audio_sr = int(stem_sr if stem is not None else sr)
        lines, lnote = transcribe(audio, audio_sr, status=status,
                                  allow_download=bool(allow_download),
                                  device=device, threads=threads)
        if stem is not None:
            env2, low2, hz2 = onset_envelope(stem, stem_sr)
            e2, _, _ = bar_features(env2, low2, hz2, grid)
            sung = _bar_presence(e2, len(grid))
        else:
            sung = _presence_from_lines(lines, grid)
        sm["sung"] = [round(float(x), 3) for x in sung]
        sm["lines"] = lines
        sm["chorus"] = chorus_lines(lines)
        sm["path"] = "listened"
        sm["how"] = "%s; %s" % (snote, lnote)
        sm["device"] = (resolve_device(device)[0] if listen else "cpu")
        sm["stem"] = stem is not None
        sm["stem_sr"] = int(stem_sr) if stem is not None else 0
        # where the vocal stem is loud, which is the only reliable sung-ness when
        # Whisper has skipped a verse entirely
        if stem is not None:
            sm["sung_regions"] = sung_regions(stem, stem_sr)
            sm["heard"] = fill_unheard(lines, sm["sung_regions"])
    # Chroma + self-similarity are always computed: they cost about a second and
    # they are the only evidence that can call something a chorus at all. Energy
    # alone cannot, which is why round 1's output was five identical `verse`s.
    chroma = bar_chroma(mono, sr, grid)
    sim = self_similarity(chroma)
    sm["chroma"] = [[round(float(v), 4) for v in row] for row in chroma]
    sm["self_sim"] = [[round(float(v), 3) for v in row] for row in sim]
    sm["recurrence"] = block_recurrence(chroma, block=PHRASE_BARS)
    sm["repeat_bars"] = [list(r) for r in _repeated_blocks(
        sm["recurrence"], min_bars=PHRASE_BARS)]
    sm["sections"] = suggest_sections(
        grid, grid_energy, grid_low, grid_onsets, bar_s, q,
        heard=sm["sung"] if (listen and sm.get("stem")) else None,
        heard_lines=(sm.get("heard") or lines) if listen else None,
        chroma=chroma, sim=sim)
    _cache_store(path, sm)
    return sm


def _renumber(bars):
    return [float(t) for t in bars]


def _bar_presence(per_bar, nbars):
    """Per-bar vocal presence 0..1 from a vocal stem's onset energy."""
    out = np.zeros(int(nbars), dtype=np.float64)
    if per_bar.shape[0] == 0:
        return out
    hi = float(np.percentile(per_bar, 90)) or 1.0
    n = min(int(nbars), per_bar.shape[0])
    out[:n] = np.clip(per_bar[:n] / hi, 0.0, 1.0)
    return out


def _presence_from_lines(lines, bars):
    """Per-bar vocal presence from lyric timestamps, when there is no stem.

    Coarser than the stem and it says so: a sung held note with no words is
    invisible here, so a quiet chorus with long vowels reads as unsung. The
    section pass only treats this as a hint, never as a decision.
    """
    out = np.zeros(len(bars), dtype=np.float64)
    if not bars:
        return out
    for ln in lines:
        a, b = float(ln.get("a", 0.0)), float(ln.get("b", 0.0))
        if b <= a:
            continue
        for k, t in enumerate(bars):
            if t < b and (bars[k + 1] if k + 1 < len(bars) else 1e9) > a:
                out[k] = 1.0
    return out
