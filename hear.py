"""What a hand-tie sound draft says: Whisper on the CPU, from local files only.

The sound-first check (`hand_tie.SOUND_FIRST`) compares a draft's words with the
line the hop should say. This is the listening half. It never downloads: a model
is used only if it is already in the Hugging Face cache, and without one the
check is skipped and says so once. It runs on the CPU so the DiT keeps the card.

Loaded lazily, on the first draft that needs it, and kept for the session.
"""

MODELS = ("openai/whisper-small", "openai/whisper-base", "openai/whisper-tiny",
          "openai/whisper-medium", "openai/whisper-large-v3-turbo")
_SR = 16000
_state = {"tried": False, "pipe": None, "name": None, "path": None}


def available():
    """The cached model this machine would use, or None. Reads the cache only."""
    if _state["path"] is None and not _state["tried"]:
        try:
            from huggingface_hub import snapshot_download  # noqa: PLC0415
        except Exception:
            _state["tried"] = True
            return None
        for repo in MODELS:
            try:
                _state["path"] = snapshot_download(repo, local_files_only=True)
                _state["name"] = repo
                break
            except Exception:
                continue
        if _state["path"] is None:
            _state["tried"] = True
    return _state["name"]


def _device():
    """GPU 0 when CUDA is up (Whisper-small is ~0.5 GB, the draft check was the
    slow step on the CPU), else the CPU. Seam-fix batch, 2026-10-14."""
    try:
        import torch  # noqa: PLC0415
        return 0 if torch.cuda.is_available() else -1
    except Exception:
        return -1


def _pipe():
    if _state["pipe"] is None and available() and not _state["tried"]:
        _state["tried"] = True
        try:
            from transformers import pipeline  # noqa: PLC0415
            _state["pipe"] = pipeline("automatic-speech-recognition",
                                      model=_state["path"], device=_device())
            print("[HandTieClips] sound check: %s on %s" % (
                _state["name"], "GPU" if _device() >= 0 else "CPU"), flush=True)
        except Exception as e:
            print("[HandTieClips] sound check: %s did not load (%r); drafts go "
                  "unchecked" % (_state["name"], e), flush=True)
    return _state["pipe"]


def transcribe(waveform, sample_rate, language="en"):
    """The words in a [B, C, T] or [C, T] waveform, or None when no model is
    available to listen."""
    pipe = _pipe()
    if pipe is None:
        return None
    from .music import resample  # noqa: PLC0415
    w = waveform.detach().float().cpu()
    while w.dim() > 2:
        w = w[0]
    mono = w.mean(0, keepdim=True) if w.dim() == 2 else w.unsqueeze(0)
    mono = resample(mono, int(sample_rate), _SR)[0].numpy()
    kw = {"generate_kwargs": {"task": "transcribe", **({"language": language} if language else {})}}
    out = pipe({"raw": mono, "sampling_rate": _SR}, **kw)
    return str((out or {}).get("text") or "").strip()


def transcribe_words(waveform, sample_rate, language="en"):
    """(text, [(word, start_s, end_s), ...]) with Whisper's word timestamps, or
    None when no model listens. `transcribe` is unchanged; this is the
    ht_seam_fix timing check's ear."""
    pipe = _pipe()
    if pipe is None:
        return None
    from .music import resample  # noqa: PLC0415
    w = waveform.detach().float().cpu()
    while w.dim() > 2:
        w = w[0]
    mono = w.mean(0, keepdim=True) if w.dim() == 2 else w.unsqueeze(0)
    mono = resample(mono, int(sample_rate), _SR)[0].numpy()
    kw = {"return_timestamps": "word",
          "generate_kwargs": {"task": "transcribe", **({"language": language} if language else {})}}
    try:
        out = pipe({"raw": mono, "sampling_rate": _SR}, **kw) or {}
    except Exception as e:
        print("[HandTieClips] sound check: word timestamps failed (%r); words only" % (e,),
              flush=True)
        out = pipe({"raw": mono, "sampling_rate": _SR},
                   generate_kwargs=kw["generate_kwargs"]) or {}
    words = []
    for ch in out.get("chunks") or []:
        ts = ch.get("timestamp") or (None, None)
        t0 = ts[0] if ts and ts[0] is not None else None
        t1 = ts[1] if ts and len(ts) > 1 and ts[1] is not None else t0
        if t0 is not None:
            words.append((str(ch.get("text") or "").strip(), float(t0), float(t1)))
    return str(out.get("text") or "").strip(), words
