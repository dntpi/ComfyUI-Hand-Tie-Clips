"""Lock one continuous recording into every hop of a chain.

No ComfyUI imports. The window function is the whole reason this file
exists as its own module: an off-by-one here costs 42 ms per hop and
compounds, and it is the kind of arithmetic the offline checkers can
prove completely. Encode/splice live in h3_ref_chain.py because they
need the audio VAE.

Decisions (taken instead of asking, because the reference diff does not
exist and the source is ElevenLabs TTS -- clean, known rate, no room
tone, regenerable):

1. Hop index is 0-based. Hop 1 (index 0) starts at t = 0 of the take.
   "Hop N starts at N x stride" with N 1-based would put hop 1 at 7.08 s
   and every line would be the next hop's words. Failure if wrong: lips
   lead the take by one hop, obvious at hop 2.
2. noise_mask polarity follows PromptMasterLD song_lock.py: 1 = denoise
   (video), 0 = freeze (audio). Inverted is "lip-sync died and she
   stopped moving" -- catastrophic and easy to ship. Asserted, not
   trusted.
3. Pad with zeros at the VAE sample rate if the file runs out. Not
   "real silence from the file" -- ElevenLabs TTS has no room tone to
   steal, and a silent tail is louder than a guessed pad. Failure:
   mute last hop, not a wrong voice.
4. Splice AFTER the Motion-Context / AddGuide pin, BEFORE the sampler.
   The pin may rewrite the hop latent; locking first would be overwritten.
   Failure if wrong: hop 2+ audio window is the previous hop's tail.
5. The freeze covers the whole hop, overlap included. The overlap is
   still this hop's generate; unlocking it would mix generated voice
   into a locked hop.
6. No auto-injection of a word-level transcript into the beat. The
   beat already carries `<d>[English] ...</d>`. A TSV cutter is a
   second clock that can desync from the take. Failure if the beat
   does not match: wooden mouth, correct sound -- recoverable.
7. Mono is duplicated to stereo. ElevenLabs is usually stereo already;
   duplicating mono is the song_lock behaviour and cannot invent a
   second channel of content.
"""
from __future__ import annotations

import hashlib
import math
import os

FPS = 24.0
AUDIO_HZ = 40.0
VAE_SR = 32000

# Hand-computed. stride = hop_frames - overlap_frames.
# 8 s = 192 f, 0.9 s overlap = 22 f, stride = 170 f.
# 15 s = 362 f, same overlap, stride = 340 f.
WINDOW_TABLE = {
    ("8s", 0): (0.0, 8.0),
    ("8s", 1): (170.0 / 24.0, 170.0 / 24.0 + 8.0),
    ("8s", 2): (340.0 / 24.0, 340.0 / 24.0 + 8.0),
    ("8s", 3): (510.0 / 24.0, 510.0 / 24.0 + 8.0),
    ("8s", 4): (680.0 / 24.0, 680.0 / 24.0 + 8.0),
    ("8s", 5): (850.0 / 24.0, 850.0 / 24.0 + 8.0),
    ("8s", 6): (1020.0 / 24.0, 1020.0 / 24.0 + 8.0),
    ("8s", 7): (1190.0 / 24.0, 1190.0 / 24.0 + 8.0),
    ("8s", 8): (1360.0 / 24.0, 1360.0 / 24.0 + 8.0),
    ("15s", 0): (0.0, 362.0 / 24.0),
    ("15s", 1): (340.0 / 24.0, 340.0 / 24.0 + 362.0 / 24.0),
    ("15s", 2): (680.0 / 24.0, 680.0 / 24.0 + 362.0 / 24.0),
}


def hop_audio_window_s(hop_index, hop_frames, overlap_frames, fps=FPS):
    """Seconds ``[t0, t1)`` of the master recording for this hop.

    `hop_index` is 0-based. Hop 0 starts at 0. Each later hop starts
    `hop_frames - overlap_frames` into the take, so the overlap region
    of hop N is the same slice of the recording as the tail of hop N-1.
    """
    hop_index = int(hop_index)
    hop_frames = int(hop_frames)
    overlap_frames = int(overlap_frames)
    fps = float(fps)
    if hop_index < 0:
        raise ValueError("hop_audio_window_s: hop_index must be >= 0, "
                         f"got {hop_index}")
    if hop_frames <= 0 or fps <= 0:
        raise ValueError("hop_audio_window_s: hop_frames and fps must be > 0")
    if overlap_frames < 0 or overlap_frames >= hop_frames:
        raise ValueError(
            "hop_audio_window_s: overlap_frames must be in "
            f"[0, hop_frames), got {overlap_frames} vs {hop_frames}")
    stride = hop_frames - overlap_frames
    t0 = hop_index * stride / fps
    t1 = t0 + hop_frames / fps
    return t0, t1


def sample_range(t0, t1, sr):
    """Half-open sample indices ``[start, end)`` at `sr`."""
    sr = int(sr)
    start = int(round(float(t0) * sr))
    end = int(round(float(t1) * sr))
    if end < start:
        raise ValueError(f"sample_range: end {end} < start {start}")
    return start, end


def grid_samples(audio_latent_length, sr=VAE_SR, audio_hz=AUDIO_HZ):
    """Raw samples the 40 Hz audio-latent grid needs for this hop."""
    return int(math.ceil(int(audio_latent_length) / float(audio_hz) * int(sr)))


def recording_digest(path):
    """Identity of the take for the hop-cache key.

    Empty / missing -> None, so `master_audio_file=""` does not move a
    key. Size + mtime + basename, not a full-file hash: the take is
    minutes long and this runs on every queue.
    """
    path = str(path or "").strip()
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        # The file will fail to load later with a readable error. A missing
        # file must still change the key, or a render against a now-gone
        # take could be served from cache.
        h = hashlib.sha256()
        h.update(b"missing:")
        h.update(path.encode("utf-8", "replace"))
        return h.hexdigest()[:16]
    h = hashlib.sha256()
    h.update(os.path.basename(path).encode("utf-8", "replace"))
    h.update(b":")
    h.update(str(int(st.st_size)).encode())
    h.update(b":")
    h.update(str(int(st.st_mtime_ns if hasattr(st, "st_mtime_ns")
                     else st.st_mtime)).encode())
    return h.hexdigest()[:16]


def force_stereo(wav):
    """wav is [C, T] or [B, C, T]. Mono is duplicated. Returns [C, T] C=2."""
    import torch
    t = wav
    if t.dim() == 3:
        t = t[0]
    if t.dim() != 2:
        raise ValueError(f"force_stereo: expected [C, T], got {tuple(t.shape)}")
    if int(t.shape[0]) == 1:
        t = t.repeat(2, 1)
    elif int(t.shape[0]) != 2:
        raise ValueError(f"force_stereo: channels must be 1 or 2, got {int(t.shape[0])}")
    return t


def fit_samples(wav, want):
    """Crop or zero-pad the last dim to `want` samples. wav is [C, T]."""
    import torch
    want = int(want)
    have = int(wav.shape[-1])
    if have == want:
        return wav
    if have > want:
        return wav[..., :want]
    return torch.nn.functional.pad(wav, (0, want - have))


def passthrough_n_samples(n_frames, sr, fps=FPS):
    """How many samples of the take the delivered clip should be."""
    return int(round(int(n_frames) / float(fps) * int(sr)))


def assert_mask_polarity(vmask, amask):
    """1 on video (denoise), 0 on audio (freeze). Raises with both numbers."""
    v = float(vmask.float().mean())
    a = float(amask.float().mean())
    if v < 0.999:
        raise RuntimeError(
            f"audio_lock: video noise_mask mean is {v:.4f}, expected 1 "
            "(1 = denoise). An inverted mask freezes the picture and "
            "generates a new voice -- that is the failure that reads as "
            "'lip-sync died and she stopped moving'.")
    if a > 0.001:
        raise RuntimeError(
            f"audio_lock: audio noise_mask mean is {a:.4f}, expected 0 "
            "(0 = freeze). The take would be denoised instead of locked.")
    return True
