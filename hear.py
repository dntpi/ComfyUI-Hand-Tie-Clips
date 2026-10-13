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


def _pipe():
    if _state["pipe"] is None and available() and not _state["tried"]:
        _state["tried"] = True
        try:
            from transformers import pipeline  # noqa: PLC0415
            _state["pipe"] = pipeline("automatic-speech-recognition",
                                      model=_state["path"], device=-1)
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
