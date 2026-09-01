"""Peaks for the trim bar, computed here rather than in the browser.

The obvious way to draw a waveform in a panel is to fetch the file and run it
through `AudioContext.decodeAudioData`. That was the first design and it is
wrong at this scale: a 173 s stereo file decodes to roughly 66 MB of Float32 in
the tab, per control, for a picture 240 pixels wide. PromptMasterLD reaches the
same conclusion from the other direction -- it has four separate trim controls
and not one `decodeAudioData` call between them.

So the server sends 240 numbers. The browser holds 240 numbers. The decode
happens once, on a thread, behind an mtime-keyed cache.

Three details are load-bearing:

**Bucket by max, not by mean.** A mean flattens transients into a smooth
sausage, and transients are the only landmarks you can trim against -- the
whole point of looking at the picture is to find the downbeat or the start of
the sentence.

**`seconds` comes from the decoded sample count.** MP3 Xing/LAME headers
routinely report double the real duration, and a duration that lies makes every
position on the bar lie with it. We already decode, so the honest number is
free.

**No ComfyUI import.** Same rule as `music.py`, `plan.py` and `refs.py`: the DSP
is testable on a CPU with no server, which is what `tools/check_waveform.py`
does.
"""
from __future__ import annotations

TAG = "HandTieClips"

# 240 is PromptMasterLD's default and it is a good one: a trim bar is a few
# hundred CSS pixels wide, so more buckets than this buys nothing you can see
# and costs JSON on every panel open.
DEFAULT_N = 240
MIN_N = 24
MAX_N = 600


def reduce_peaks(mono, n=DEFAULT_N):
    """A 1-D waveform -> `n` bucket maxima in 0..1.

    `mono` is any 1-D sequence of floats (a torch tensor, a numpy array, a
    list). Returns a plain list so the caller can hand it straight to
    `json_response` without a tensor library in the loop.
    """
    n = max(MIN_N, min(MAX_N, int(n or DEFAULT_N)))
    total = len(mono)
    if total <= 0:
        return [0.0] * n

    import torch  # noqa: PLC0415

    x = mono if isinstance(mono, torch.Tensor) else torch.as_tensor(mono)
    x = x.detach().to(torch.float32).abs().flatten()

    # Pad up to a whole number of buckets rather than looping in Python. A
    # 173 s file is 8.3M samples and a per-bucket slice in the interpreter is
    # measurable; one reshape and one max over a dim is not.
    per = max(1, total // n)
    keep = per * n
    if keep < total:
        # Fold the remainder into the last bucket instead of dropping it, or a
        # transient in the final fraction of a second would vanish from the
        # picture while still being audible in the file.
        head = x[:keep].reshape(n, per).amax(dim=1)
        head[-1] = torch.maximum(head[-1], x[keep:].amax())
        buckets = head
    else:
        buckets = x[:keep].reshape(n, per).amax(dim=1)

    top = float(buckets.max())
    if top > 0:
        buckets = buckets / top
    # 3 dp is well past what a 240 px canvas can render, and it keeps the JSON
    # payload around 1.5 KB instead of 5 KB.
    return [round(float(v), 3) for v in buckets]


def peaks(audio, n=DEFAULT_N):
    """A ComfyUI AUDIO dict -> `(peaks, seconds)`.

    `audio` is what `media.load_audio` returns: `{"waveform": [B,C,S],
    "sample_rate": int}`. A missing or empty take returns a flat line rather
    than raising, so a panel drawing a control for a file that has gone missing
    shows an empty bar instead of an error.
    """
    if not audio:
        return [0.0] * max(MIN_N, min(MAX_N, int(n or DEFAULT_N))), 0.0

    import torch  # noqa: PLC0415

    wav = audio.get("waveform")
    sr = int(audio.get("sample_rate") or 0)
    if wav is None or sr <= 0:
        return [0.0] * max(MIN_N, min(MAX_N, int(n or DEFAULT_N))), 0.0

    x = wav if isinstance(wav, torch.Tensor) else torch.as_tensor(wav)
    while x.dim() > 1:
        # Mean across batch and channels, not sum: summing two correlated
        # channels doubles the amplitude and the normalise below would hide it,
        # but a mono file and its own stereo copy should draw identically.
        x = x.mean(dim=0)
    samples = int(x.shape[-1])
    return reduce_peaks(x, n), samples / float(sr)
