"""Files on disk -> tensors, plus the path safety that makes that survivable.

The node used to take its pictures down nine `ref_image_N` IMAGE sockets, which
meant nine `Load Image` nodes and a 16-socket column occupying a third of the
node before the editor even started. References now live as *files* in
`<ComfyUI input>/h3_refs`, and a reference carries a filename rather than a
wire.

Two rules this module exists to enforce:

**Nothing from the browser is trusted as a path.** `resolve` normalises, then
prefix-checks against the reference directory, then checks the extension. A
POST route that writes files is only as safe as the function deciding where the
bytes land, so that function lives here, is nine lines, and is used by both the
route and the loaders.

**Pixels never travel as base64.** Only the basename is stored in a widget.
PromptMasterLD measured 1.68 MB of widget value for nine images and ComfyUI
then failed to save the workflow at all; the filename is the whole payload.

The loaders deliberately return exactly what a `Load Image` would -- float
`[N,H,W,3]` in 0..1 -- so everything downstream of the old sockets is unchanged,
including `_ref_frames`' resize and `store.tensor_digest`'s cache keying.

No hard ComfyUI import: `input_dir()` falls back to a path relative to this
file, so the loaders and the safety check are testable without a running server
(same rule as `plan.py`, `refs.py` and `tone.py`).
"""
from __future__ import annotations

import os

TAG = "HandTieClips"

# One flat folder under ComfyUI's input dir. Flat on purpose: `/view` takes a
# basename plus one subfolder, and a tree would need a second lookup for no
# benefit at the scale a chain uses (single digits of references).
REFS_SUBDIR = "h3_refs"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
ALL_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

# Anything past this is re-encoded on upload. A 12 MP phone photo is a
# reference, not an asset: it costs decode RAM on every load and is about to be
# resized to the canvas anyway.
MAX_SIDE = 2048
MAX_PIXELS = 1_500_000
JPEG_QUALITY = 88


def kind_of(name):
    """'image' | 'video' | 'audio' | None, from the extension alone."""
    ext = os.path.splitext(str(name or ""))[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


def input_dir():
    """ComfyUI's input directory, or a sibling `input/` when it is absent."""
    try:
        import folder_paths  # noqa: PLC0415
        return folder_paths.get_input_directory()
    except Exception:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(os.path.dirname(here), "input")


def refs_dir(create=False):
    d = os.path.join(input_dir(), REFS_SUBDIR)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def resolve(name, kinds=None):
    """Absolute path for a reference basename, or None.

    Refuses anything that escapes the reference directory, carries an unknown
    extension, or does not exist. `name` is treated as a bare filename -- any
    directory component is a caller error, not a feature, so `..` cannot even be
    expressed as a traversal.

    `kinds` optionally restricts to {'image','video','audio'}.
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    kind = kind_of(raw)
    if kind is None:
        return None
    if kinds and kind not in kinds:
        return None
    base = os.path.normpath(refs_dir())
    full = os.path.normpath(os.path.join(base, raw.replace("/", os.sep)))
    # The prefix check is the load-bearing line. normpath has already collapsed
    # any `..`, so a path that still starts with the reference directory cannot
    # be pointing outside it.
    if full != base and not full.startswith(base + os.sep):
        return None
    return full if os.path.isfile(full) else None


def listing(kinds=None):
    """Reference filenames on disk, newest first, for the editor's dropdown."""
    d = refs_dir()
    if not os.path.isdir(d):
        return []
    rows = []
    for name in os.listdir(d):
        kind = kind_of(name)
        if kind is None or (kinds and kind not in kinds):
            continue
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        try:
            rows.append((os.path.getmtime(path), name, kind))
        except OSError:
            continue
    rows.sort(reverse=True)
    return [{"name": n, "kind": k} for _m, n, k in rows]


def stamp(names):
    """`name:mtime` for each file, for IS_CHANGED.

    ComfyUI caches a node's output on its inputs, and a filename is a stable
    input even when the bytes behind it change. Without this, replacing a
    reference in place would serve the previous render.
    """
    bits = []
    for name in names:
        if not name:
            continue
        path = resolve(name)
        try:
            bits.append(f"{name}:{os.path.getmtime(path)}" if path else f"{name}:missing")
        except OSError:
            bits.append(f"{name}:missing")
    return "|".join(bits)


# -- loaders ---------------------------------------------------------------

def _to_tensor(pil):
    import numpy as np  # noqa: PLC0415
    import torch  # noqa: PLC0415

    arr = np.asarray(pil.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def load_image(name):
    """One still as an IMAGE tensor `[1,H,W,3]`, or None.

    Returns what a `Load Image` returns, so `_ref_frames` resizes it and
    `tensor_digest` keys it exactly as before.
    """
    path = resolve(name, kinds={"image"})
    if path is None:
        return None
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
        with Image.open(path) as im:
            # EXIF orientation: a phone portrait otherwise loads on its side,
            # and the model would be handed a rotated face.
            return _to_tensor(ImageOps.exif_transpose(im))
    except Exception as exc:
        print(f"[{TAG}] could not read reference {name!r}: {exc!r}", flush=True)
        return None


def load_video(name, max_frames=None):
    """A clip as an IMAGE batch `[N,H,W,3]`, or None."""
    path = resolve(name, kinds={"video"})
    if path is None:
        return None
    try:
        import av  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415

        frames = []
        with av.open(path) as container:
            for frame in container.decode(video=0):
                frames.append(frame.to_ndarray(format="rgb24"))
                if max_frames and len(frames) >= int(max_frames):
                    break
        if not frames:
            return None
        arr = np.stack(frames).astype(np.float32) / 255.0
        return torch.from_numpy(arr)
    except Exception as exc:
        print(f"[{TAG}] could not read video {name!r}: {exc!r}", flush=True)
        return None


def load_audio(name):
    """A take as ComfyUI's AUDIO dict, or None.

    Shape is `[batch, channels, samples]`, which is what every AUDIO consumer
    in the graph expects.

    Decoded with PyAV rather than `torchaudio.load`. torchaudio 2.9 removed its
    own decoding backends and left `load` a thin wrapper over `torchcodec`, so
    on an install without that package -- including this one -- it raises
    ImportError for every file, wav and mp3 alike, and the only symptom is a
    reference that silently does not arrive. PyAV is already a hard ComfyUI
    dependency and is what core's own Load Audio decodes with, so a file picked
    in the panel now takes exactly the same path as one arriving down a wire.

    `torchaudio` is still used for `functional.resample` in music.py; it is only
    the *decoding* half of that library that is gone.
    """
    path = resolve(name, kinds={"audio"})
    if path is None:
        return None
    try:
        import av  # noqa: PLC0415
        import torch  # noqa: PLC0415

        with av.open(path) as container:
            if not container.streams.audio:
                print(f"[{TAG}] {name!r} has no audio stream", flush=True)
                return None
            stream = container.streams.audio[0]
            sr = int(stream.codec_context.sample_rate)
            channels = int(stream.channels)
            chunks = []
            for frame in container.decode(streams=stream.index):
                buf = torch.from_numpy(frame.to_ndarray())
                # Planar formats decode to [channels, samples]; packed ones to
                # [1, samples*channels] interleaved. Same reshape ComfyUI uses.
                if buf.shape[0] != channels:
                    buf = buf.view(-1, channels).t()
                chunks.append(buf)
        if not chunks:
            print(f"[{TAG}] {name!r} decoded to no audio frames", flush=True)
            return None
        wav = torch.cat(chunks, dim=1)
        # Integer PCM is scaled by its own full range, not normalised by peak:
        # a quiet take must stay quiet, and dividing by max would silently
        # apply a wildly different gain per file.
        if not wav.dtype.is_floating_point:
            if wav.dtype == torch.int16:
                wav = wav.float() / (2 ** 15)
            elif wav.dtype == torch.int32:
                wav = wav.float() / (2 ** 31)
            else:
                wav = wav.float()
        return {"waveform": wav.float().unsqueeze(0), "sample_rate": sr}
    except Exception as exc:
        print(f"[{TAG}] could not read audio {name!r}: {exc!r}", flush=True)
        return None


# -- writing (used by the upload route) ------------------------------------

def unique_path(directory, filename):
    """A free path in `directory`, suffixing `_1`, `_2`... on collision."""
    name = os.path.basename(str(filename or "").strip()) or "upload"
    stem, ext = os.path.splitext(name)
    path, i = os.path.join(directory, name), 1
    while os.path.exists(path):
        path = os.path.join(directory, f"{stem}_{i}{ext}")
        i += 1
    return path


def shrink_image(path):
    """Re-encode an oversized still in place. -> (final_path, w, h).

    A 12 MP drop costs decode RAM on every single load and is about to be
    resized to a 0.3 MP canvas regardless. Anything within budget is left
    untouched, so a PNG the author cared about stays a PNG.
    """
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            if w * h <= MAX_PIXELS and max(w, h) <= MAX_SIDE:
                return path, w, h
            scale = min(MAX_SIDE / max(w, h), (MAX_PIXELS / float(w * h)) ** 0.5, 1.0)
            new = (max(1, int(w * scale)), max(1, int(h * scale)))
            im = im.convert("RGB").resize(new, Image.Resampling.LANCZOS)
            out = os.path.splitext(path)[0] + ".jpg"
            out = out if out == path else unique_path(os.path.dirname(path),
                                                      os.path.basename(out))
            im.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=False)
        if out != path:
            try:
                os.remove(path)
            except OSError:
                pass
        return out, new[0], new[1]
    except Exception as exc:
        print(f"[{TAG}] could not shrink {os.path.basename(path)}: {exc!r}",
              flush=True)
        return path, 0, 0
