"""A music bed under the finished chain: layout, fit, duck, mix.

No ComfyUI imports, for the same reason `plan.py`, `refs.py`, `tone.py` and
`latents.py` have none: `tools/check_music.py` has to run this on the CPU with
no server and no GPU. Audio bugs are the kind you cannot see in a still, so the
only way they get caught before a render is a checker that can run at all.

**This is a mix, not a replacement.** H3 generates its own audio per hop --
dialogue and effects -- which `_xfade_audio` joins into `master_wav` at every
seam. A soundtrack sits UNDER that. Everything here is written so the dialogue
survives: the duck exists because a flat bed at any level that is audible in the
gaps is a level that buries speech, and the peak guard exists because the sum of
two things that each nearly reach full scale does not fit in full scale.

Applied once, after the last hop is joined. It touches no latent, no
conditioning and no cache key, so it cannot move a single generated frame or
sample -- it only decides what is laid on top of them.
"""
from __future__ import annotations

import torch

# The envelope and the gain curve are computed at a control rate rather than per
# sample. A one-pole attack/release filter is inherently sequential -- each
# output depends on the one before it -- so at 44.1 kHz a 40 s master is 1.8M
# Python iterations, about a minute of dead time on a node whose whole job took
# ten. At 1 kHz it is 40,000 iterations and imperceptible, and 1 ms of timing
# resolution is far finer than the attack times that matter (10-400 ms).
CONTROL_HZ = 1000.0

EPS = 1e-9


def _bcs(x):
    """Anything shaped like audio -> [B, C, S]. Samples are always last."""
    if x.dim() == 1:
        return x.view(1, 1, -1)
    if x.dim() == 2:
        return x.unsqueeze(0)
    if x.dim() == 3:
        return x
    return x.reshape(1, -1, int(x.shape[-1]))


def _equal_power(n, device, dtype):
    """(fade_out, fade_in) of length n whose squares sum to 1.

    Equal power, not equal amplitude. A linear crossfade between two
    uncorrelated signals dips ~3 dB in the middle, which on a music loop is an
    audible sag once per bar; cos/sin holds the energy flat. The same law is
    already used for the hop seam in `_xfade_audio`, and having two different
    fade shapes in one output is the kind of inconsistency nobody can hear but
    everybody can argue about.
    """
    t = torch.linspace(0.0, 1.0, n, device=device, dtype=dtype)
    return torch.cos(t * (torch.pi / 2)), torch.sin(t * (torch.pi / 2))


def to_layout(wav, sr, target_sr, target_ch):
    """Resample and re-channel a bed to sit alongside the master. -> tensor.

    A rate mismatch is the failure that does not announce itself: 48 kHz music
    dropped into a 44.1 kHz master plays 9% fast and a semitone sharp, which
    reads as "the model generated bad music", not as a bug in the node. Convert
    explicitly and early.
    """
    x = _bcs(wav).float()
    x = x[:1]                                  # a bed is one item, never a batch
    sr, target_sr = int(sr), int(target_sr)
    if sr != target_sr and sr > 0 and target_sr > 0:
        import torchaudio  # noqa: PLC0415
        x = torchaudio.functional.resample(x, sr, target_sr)
    c = int(x.shape[1])
    target_ch = max(1, int(target_ch))
    if c != target_ch:
        if c == 1:
            x = x.expand(-1, target_ch, -1).contiguous()
        elif target_ch == 1:
            x = x.mean(dim=1, keepdim=True)
        elif c > target_ch:
            x = x[:, :target_ch]
        else:
            reps = -(-target_ch // c)          # ceil
            x = x.repeat(1, reps, 1)[:, :target_ch]
    return x.contiguous()


def fit(bed, target_samples, *, mode="loop", xfade_samples=None):
    """Make `bed` exactly `target_samples` long. -> tensor.

    `loop` tiles it with a crossfade at every wrap; `once` plays it through and
    leaves silence after. Butt-joining a loop puts a step discontinuity at the
    wrap, which is a click -- and a click that recurs on a fixed period is the
    most audible artifact available, far worse than the seam it came from.
    """
    x = _bcs(bed).float()
    n = int(x.shape[-1])
    target = int(target_samples)
    if target <= 0 or n == 0:
        return x[..., :0]
    if n >= target:
        return x[..., :target].contiguous()
    if mode != "loop":
        pad = torch.zeros(x.shape[0], x.shape[1], target - n,
                          device=x.device, dtype=x.dtype)
        return torch.cat([x, pad], dim=-1)

    # Overlap-add rather than repeated concatenation. Growing the buffer one
    # copy at a time is quadratic, and a 2 s sting under a 5 minute master is
    # exactly the case where that stops being theoretical.
    xf = int(xfade_samples if xfade_samples is not None else 0)
    xf = max(0, min(xf, n // 4))
    advance = max(1, n - xf)
    reps = -(-(target - n) // advance) + 1
    out = torch.zeros(x.shape[0], x.shape[1], n + (reps - 1) * advance,
                      device=x.device, dtype=x.dtype)
    if xf > 0:
        fade_out, fade_in = _equal_power(xf, x.device, x.dtype)
    for i in range(reps):
        piece = x.clone()
        if xf > 0:
            if i > 0:                       # ramp in over the previous tail
                piece[..., :xf] = piece[..., :xf] * fade_in
            if i < reps - 1:                # ramp out under the next head
                piece[..., -xf:] = piece[..., -xf:] * fade_out
        a = i * advance
        out[..., a:a + n] += piece
    return out[..., :target].contiguous()


def edge_fades(x, fade_samples):
    """Fade the bed in at the top and out at the tail. -> tensor.

    Music that starts at full level on frame 0 sounds like a cut, and music
    still playing when the picture ends sounds like a dropout. Applied to the
    fitted bed, so it is the piece's own start and end, not a loop's.
    """
    n = int(x.shape[-1])
    f = int(min(max(0, fade_samples), n // 2))
    if f <= 0:
        return x
    x = x.clone()
    ramp = torch.linspace(0.0, 1.0, f, device=x.device, dtype=x.dtype)
    x[..., :f] = x[..., :f] * ramp
    x[..., -f:] = x[..., -f:] * ramp.flip(0)
    return x


def duck_gain(speech, sr, *, depth=0.0, attack_ms=15.0, release_ms=350.0):
    """A 0..1 gain curve for the bed, driven by the generated audio. -> tensor.

    Fast attack, slow release: get out of the way the instant someone starts
    talking, come back gently once they stop. The reverse -- slow attack -- lets
    the first syllable of every line collide with the music, which is precisely
    the word a listener needs to hear to follow a sentence.

    The reference level is the 95th percentile of the envelope, not its peak. A
    single shouted word would otherwise set the scale and leave ordinary
    dialogue barely ducking at all, which is the common case in a podcast clip
    and exactly the material this was asked for.
    """
    x = _bcs(speech).float()
    depth = float(max(0.0, min(1.0, depth)))
    total = int(x.shape[-1])
    ones = torch.ones(1, 1, total, device=x.device, dtype=x.dtype)
    if depth <= 0.0 or total == 0:
        return ones

    block = max(1, int(round(float(sr) / CONTROL_HZ)))
    mono = x[:1].abs().amax(dim=1, keepdim=True)          # [1,1,S], peak of chans
    pad = (-total) % block
    if pad:
        mono = torch.nn.functional.pad(mono, (0, pad))
    env = mono.reshape(-1, block).amax(dim=-1)            # control-rate peaks
    if int(env.numel()) == 0:
        return ones

    ref = torch.quantile(env, 0.95)
    if float(ref) <= EPS:
        return ones                                       # silence: nothing to duck

    # One-pole, per control sample. Coefficients are the usual exp(-1/(t*fs)).
    ctrl_hz = float(sr) / float(block)
    a_att = float(torch.exp(torch.tensor(-1.0 / max(1e-6, (attack_ms / 1000.0) * ctrl_hz))))
    a_rel = float(torch.exp(torch.tensor(-1.0 / max(1e-6, (release_ms / 1000.0) * ctrl_hz))))
    src = (env / ref).clamp(0.0, 1.0).tolist()
    y, sm = 0.0, []
    for v in src:
        a = a_att if v > y else a_rel
        y = a * y + (1.0 - a) * v
        sm.append(y)

    g = 1.0 - depth * torch.tensor(sm, device=x.device, dtype=x.dtype)
    g = g.clamp(0.0, 1.0).view(1, 1, -1)
    # Back to sample rate. Linear, so the curve has no steps of its own to click
    # on; the control rate is already far above anything audible as modulation.
    g = torch.nn.functional.interpolate(g, size=int(g.shape[-1]) * block,
                                        mode="linear", align_corners=False)
    return g[..., :total].contiguous()


def mix(master, bed, gain_db=-14.0):
    """Sum the bed under the master, guarding the peak. -> (tensor, note).

    The guard scales the WHOLE mix, dialogue included, rather than only the bed.
    Ducking the bed further to fit would change the balance the user set,
    silently and by an amount they cannot predict; a single overall trim is one
    number, is reported, and preserves the relationship between the two. If the
    master was already at full scale on its own, no arrangement of the bed
    helps, and the note is the honest answer.
    """
    m = _bcs(master).float()
    b = _bcs(bed).float()
    n = min(int(m.shape[-1]), int(b.shape[-1]))
    out = m.clone()
    g = float(10.0 ** (float(gain_db) / 20.0))
    out[..., :n] = out[..., :n] + b[..., :n] * g
    peak = float(out.abs().max()) if out.numel() else 0.0
    note = ""
    if peak > 1.0:
        out = out / peak
        note = f"peak guard: mix trimmed {20.0 * torch.log10(torch.tensor(peak)):.1f} dB"
    return out.contiguous(), note


def apply(master, sr, music, music_sr, *, gain_db=-14.0, duck=0.0,
          fit_mode="loop", fade_s=1.0):
    """The one call the node makes. -> (waveform, note).

    Returns the master untouched, and an empty note, whenever there is nothing
    to do -- so the caller needs no branch and an unwired socket costs nothing.
    """
    if music is None:
        return master, ""
    m = _bcs(master).float()
    total = int(m.shape[-1])
    sr = int(sr)
    if total == 0 or sr <= 0:
        return master, ""

    bed = to_layout(music, music_sr, sr, int(m.shape[1]))
    if int(bed.shape[-1]) == 0:
        return master, "soundtrack ignored: empty"

    fade_n = int(max(0.0, float(fade_s)) * sr)
    src_n = int(bed.shape[-1])
    bed = fit(bed, total, mode=fit_mode, xfade_samples=max(fade_n, sr // 4))
    bed = edge_fades(bed, fade_n)

    g = duck_gain(m, sr, depth=duck)
    bed = bed * g

    out, guard = mix(m, bed, gain_db)
    parts = [f"soundtrack: {src_n / sr:.1f}s {fit_mode} -> {total / sr:.1f}s",
             f"{gain_db:+.1f} dB"]
    if duck > 0:
        parts.append(f"duck {duck:.2f} (min gain {float(g.min()):.2f})")
    if guard:
        parts.append(guard)
    return out, ", ".join(parts)
