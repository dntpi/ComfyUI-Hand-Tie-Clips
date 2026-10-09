"""Pictures and playback of cached hops, for the retake timeline.

The timeline shows what the hop cache holds: a scrub sheet per hop, the hop
itself as a small playable clip, and a join -- the tail of one hop and the head
of the next, trimmed the way the master is -- as a clip. All three are built
from `HopStore.get()` output and nothing else, so they show exactly what the
next run would replay.

**No ComfyUI import.** Same rule as `waveform.py`, `plan.py` and `refs.py`: the
math runs on a CPU with no server, which is what `tools/check_timeline.py`
does. PyAV is already a ComfyUI dependency and encodes H.264 + AAC in process,
so nothing shells out.

Two things these are not, stated because they are easy to mistake:

**Not the master.** The cache holds hops *before* tone correction (the chain
corrects after `put` so the mode stays out of the key), so with
`tone_compensate` on, a seam can play a little brighter or darker here than in
the delivered video. The 40 ms audio crossfade at each join is not reproduced
either.

**Not evidence about drift.** They are small H.264 previews. Judge texture and
tone on the master with `texture_probe` / `tone_probe`, never on these.
"""
from __future__ import annotations

import io
import os

import av
import numpy as np
import torch

FPS = 24
SPRITE_N = 24          # frames in a scrub sheet
SPRITE_H = 144         # scrub cell height, px
CLIP_H = 360           # preview clip height, px
SEAM_SECONDS = 2.0     # each side of a join
AUDIO_RATE = 48000     # AAC-friendly; the cache's own rate is resampled


def _frames_u8(imgs):
    """[N,H,W,3] -> uint8 numpy. uint8 passes through (`HopStore.frames`);
    float 0..1, torch or numpy, is quantised."""
    if torch.is_tensor(imgs):
        imgs = imgs.detach().cpu()
        if imgs.dtype == torch.uint8:
            return imgs.numpy()[..., :3]
        imgs = imgs.float().numpy()
    imgs = np.asarray(imgs)
    if imgs.dtype == np.uint8:
        return imgs[..., :3]
    return (np.clip(imgs[..., :3], 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def _even(n):
    return max(2, int(n) // 2 * 2)


def _fit(h, w, target_h):
    """(th, tw) for a frame scaled to `target_h`, never up, both even."""
    th = min(int(h), int(target_h))
    tw = int(round(w * th / float(h)))
    return _even(th), _even(tw)


def _resize(frame_u8, th, tw):
    from PIL import Image

    if frame_u8.shape[0] == th and frame_u8.shape[1] == tw:
        return frame_u8
    return np.asarray(Image.fromarray(frame_u8).resize((tw, th), Image.BILINEAR))


def sprite(imgs, n=SPRITE_N, height=SPRITE_H, quality=80):
    """A scrub sheet: `n` frames evenly across the hop, side by side, as JPEG.

    Returns `(jpeg_bytes, n_cells)`. The first and last cells are the hop's
    first and last frames -- the two that meet the neighbours at a join.
    """
    from PIL import Image

    total = int(imgs.shape[0])
    if total < 1:
        raise ValueError("a hop with no frames has no sheet")
    n = max(1, min(int(n), total))
    idx = np.linspace(0, total - 1, n).round().astype(int)
    th, tw = _fit(imgs.shape[1], imgs.shape[2], height)
    cells = [_resize(_frames_u8(imgs[int(i):int(i) + 1])[0], th, tw) for i in idx]
    sheet = np.concatenate(cells, axis=1)
    buf = io.BytesIO()
    Image.fromarray(sheet).save(buf, format="JPEG", quality=int(quality))
    return buf.getvalue(), n


def _mono_or_stereo(wav):
    """Any of [S], [C,S], [B,C,S] -> float32 numpy [C,S] with C in (1, 2)."""
    if torch.is_tensor(wav):
        wav = wav.detach().cpu().float().numpy()
    wav = np.asarray(wav, dtype=np.float32)
    while wav.ndim > 2:
        wav = wav[0]
    if wav.ndim == 1:
        wav = wav[None, :]
    return np.ascontiguousarray(wav[:2])


def seam(prev_tail, prev_wav, next_head, next_wav, sr, trim_frames, fps=FPS, tail_frames=0):
    """The join as the master delivers it: `(frames uint8, wav [C,S])`.

    `prev_tail` is the last frames of the previous hop; `next_head` is the
    next hop's frames starting AFTER its first `trim_frames` -- the overlap a
    continuation regenerates and the master throws away (0 for a restart).
    The audio is cut to match: the previous hop's last `len(prev_tail)`
    frames of sound, then the next hop's from `trim_frames` on. Hand-tie cuts
    the other side: `tail_frames` of the previous hop's sound are dropped
    from its end and `trim_frames` is 0. Pass the
    hops' whole waveforms; the windows are taken here, so picture and sound
    come from the same arithmetic.
    """
    a = _frames_u8(prev_tail)
    b = _frames_u8(next_head)
    if a.shape[0] < 1 or b.shape[0] < 1:
        raise ValueError("a join needs frames on both sides")
    per = sr / float(fps)
    a_wav = _mono_or_stereo(prev_wav)
    b_wav = _mono_or_stereo(next_wav)
    a_n = int(round(a.shape[0] * per))
    b_skip = int(round(max(0, int(trim_frames)) * per))
    b_n = int(round(b.shape[0] * per))
    a_end = a_wav.shape[1] - int(round(max(0, int(tail_frames)) * per))
    a_part = a_wav[:, max(0, a_end - a_n):a_end]
    b_part = b_wav[:, b_skip:b_skip + b_n]
    ch = max(a_part.shape[0], b_part.shape[0])
    if a_part.shape[0] < ch:
        a_part = np.repeat(a_part, ch, axis=0)
    if b_part.shape[0] < ch:
        b_part = np.repeat(b_part, ch, axis=0)
    return np.concatenate([a, b], axis=0), np.concatenate([a_part, b_part], axis=1)


def seam_indices(prev_frames, next_frames, trim_frames, seconds=SEAM_SECONDS, fps=FPS,
                 tail_frames=0):
    """Which frames of each hop a join clip shows: `(prev_idx, next_idx)`.

    `tail_frames` is hand-tie's cut: the previous hop's last frames are dropped
    instead of the next hop's first."""
    k = max(1, int(round(seconds * fps)))
    trim = max(0, int(trim_frames))
    end = int(prev_frames) - max(0, int(tail_frames))
    prev_idx = list(range(max(0, end - k), end))
    next_idx = list(range(trim, min(int(next_frames), trim + k)))
    return prev_idx, next_idx


def encode_clip(imgs, wav, sr, path, fps=FPS, height=CLIP_H):
    """Write a small H.264 + AAC MP4 to `path`. Returns `path`.

    To a file, not to memory: the route serves it with a file response, which
    answers the byte-range requests a browser `<video>` makes to seek.
    Written to `.part` and renamed, so a half-encoded preview is never served.
    """
    n = int(imgs.shape[0])
    if n < 1:
        raise ValueError("nothing to encode")
    th, tw = _fit(imgs.shape[1], imgs.shape[2], height)
    tmp = path + ".part"
    container = av.open(tmp, mode="w", format="mp4")
    try:
        vs = container.add_stream("libx264", rate=int(fps))
        vs.width, vs.height = tw, th
        vs.pix_fmt = "yuv420p"
        vs.options = {"crf": "26", "preset": "veryfast"}
        audio = _mono_or_stereo(wav) if wav is not None else None
        a_s = None
        if audio is not None and audio.shape[-1] > 0:
            layout = "stereo" if audio.shape[0] == 2 else "mono"
            a_s = container.add_stream("aac", rate=AUDIO_RATE)
            a_s.layout = layout
            resampler = av.AudioResampler(format="fltp", layout=layout,
                                          rate=AUDIO_RATE)
        for i in range(n):
            frame = _resize(_frames_u8(imgs[i:i + 1])[0], th, tw)
            vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="rgb24")
            for packet in vs.encode(vf):
                container.mux(packet)
        for packet in vs.encode():
            container.mux(packet)
        if a_s is not None:
            # Packed `flt` is interleaved L R L R, so [C,S] goes in transposed.
            af = av.AudioFrame.from_ndarray(
                np.ascontiguousarray(audio.T.reshape(1, -1)), format="flt",
                layout=layout)
            af.sample_rate = int(sr)
            for out in resampler.resample(af) + resampler.resample(None):
                for packet in a_s.encode(out):
                    container.mux(packet)
            for packet in a_s.encode(None):
                container.mux(packet)
    finally:
        container.close()
    os.replace(tmp, path)
    return path
