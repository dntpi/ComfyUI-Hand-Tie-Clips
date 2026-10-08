"""Hand-tie: continuity by tying every hop to one previs, never to another hop.

The Motion-Context join pins hop N+1 on hop N's finished tail, so hop N's
errors become hop N+1's starting truth: texture steps about x1.04 per join and
long chains drift or cut. Hand-tie removes the relay. One PREVIS generate covers
a whole section (a chain start and its continuations); 22-frame WINDOWS cut from
it tie the hops together: hop k is guided by window k at frame 0 and window k+1
at frame -22 (picture and sound, stock MiniMaxH3AddGuide), and no hop reads
another. The film cuts each join inside the shared window (22-frame overlap).

Measured in HTC-Lab (lab name "bridge clips", 2026-10-07/08, see DEVLOG): over 4
hops the Motion-Context arm ratcheted x1.053 / x1.042 at joins 2-3 and drifted to
a new camera; the hand-tie film held one camera and one light, each hop one
copy step from its window, never compounding (2 of 2 runs).

This module is the arithmetic and the text, with no ComfyUI imports, so
tools/check_hand_tie.py runs without a server:

    sections()          a plan's hops grouped into sections (one previs each)
    spacing()           previs frames between two windows for one hop
    layout()            window starts and previs length for a section
    window_audio()      a window's slice of the previs soundtrack, in seconds
    compile_previs()    the previs's timed description from the shots
    crossing_lints()    beats that repeat a line their head window already says
    tone_offsets()      the per-frame tone ramp (torch)
    tone_match()        a hop pulled onto its two windows' tone (torch)
    settings_errors()   what hand-tie refuses, before anything samples
    hop_key_fields()    what a hand-tie hop's cache key carries instead of prev_key
    film_frames()       a section's delivered length

What the lab settled, kept as rules here:
  * windows are 22 frames: 5-frame clips come out dark after frame 0;
  * windows sit on H3's 17-frame grid, so their latent steps line up with the hop's;
  * one previs, never independently generated windows: those disagree on the camera;
  * spacing about 68 frames per 8 s hop: 34 made every hop push in and back;
  * plain beats, no frame refs: the official keyframe-completion contract with the
    pinned frames as <Picture N> made a hop hard-cut to a separate shot and back;
  * a line that crosses a join is written once, in the previs (a shot's `moment`),
    and the next hop's beat does not repeat it.
"""

import re

FPS = 24
WINDOW = 22                 # frames per window; also the only overlap hand-tie accepts
GRID = 17                   # H3 latent cycle (1,4,4,4,4): a window starts on 17q
PREVIS_MAX = 362            # 15.1 s, H3's trained ceiling; = 5 hops of 8 s
REF_HOP, REF_SPACING = 192, 68   # the measured case: 8 s hop, 2.8 s of previs per hop
MIN_SPACING = 34            # windows never overlap
SPEECH_WPS = 2.5            # words a second, as planner.SPEECH_WPS
LEAD_MAX_S = 1.0            # a line starts at most this far ahead of its window
PREVIS_SEED_OFFSET = 7919    # previs seed = the section's first hop seed + this


# --- sections and geometry ------------------------------------------------

def sections(shots):
    """[[hop index, ...], ...]: hop 0 and every anchor=restart open a section."""
    out = []
    for i, sh in enumerate(shots or []):
        if i == 0 or str((sh or {}).get("anchor") or "") == "restart":
            out.append([])
        out[-1].append(i)
    return out


def spacing(hop_frames):
    """Previs frames between this hop's two windows: the 68-per-192 ratio, on the grid."""
    q = max(1, int(round(hop_frames * REF_SPACING / float(REF_HOP) / GRID)))
    return max(MIN_SPACING, q * GRID)


def layout(hop_frames):
    """Window starts and previs length for one section's hops.

    -> {"windows": [w0, ..., wn], "spacings": [...], "previs": frames}
    There is one window more than hops: hop k is tied to windows k and k+1.
    """
    if not hop_frames:
        raise ValueError("hand-tie: a section needs at least one hop")
    sp = [spacing(int(f)) for f in hop_frames]
    w = [0]
    for s in sp:
        w.append(w[-1] + s)
    return {"windows": w, "spacings": sp, "previs": w[-1] + WINDOW}


def max_hops(hop_frames=REF_HOP):
    """How many hops of this length one previs can hold."""
    return (PREVIS_MAX - WINDOW) // spacing(hop_frames)


def window_audio(start, fps=FPS):
    """(start_s, duration_s) of a window's slice of the previs soundtrack."""
    return (start / float(fps), WINDOW / float(fps))


def film_frames(hop_frames):
    """Delivered frames for one section: hop k+1's first WINDOW frames are cut."""
    return sum(int(f) for f in hop_frames) - WINDOW * (len(hop_frames) - 1)


# --- the previs text --------------------------------------------------------

_D = re.compile(r"<d>(.*?)</d>", re.S)
_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")
_SENT = re.compile(r"(?<=[.!?])\s+")


def _spoken_s(text):
    words = sum(len(_TAG.sub("", m).split()) for m in _D.findall(text or ""))
    return words / SPEECH_WPS


def _first_sentence(text):
    t = _D.sub("", text or "").strip()
    return _SENT.split(t, 1)[0].strip() if t else ""


def _last_sentence(text):
    t = _D.sub("", text or "").strip()
    parts = [p for p in _SENT.split(t) if p.strip()]
    return parts[-1].strip() if parts else ""


def _stamp(seconds):
    s = max(0.0, seconds)
    return "%02d:%06.3f" % (int(s // 60), s - 60 * int(s // 60))


def moments(shots):
    """One moment per window: shot k's `moment` (else the first sentence of its beat)
    for window k, and the last shot's `moment_end` (else its beat's last sentence)
    for the closing window."""
    out = []
    for sh in shots:
        m = str((sh or {}).get("moment") or "").strip()
        out.append(m or _first_sentence((sh or {}).get("beat")))
    last = shots[-1] or {}
    out.append(str(last.get("moment_end") or "").strip() or _last_sentence(last.get("beat")))
    return out


def _lead_s(spoken_s):
    """How far ahead of its window a crossing line starts."""
    return min(LEAD_MAX_S, spoken_s / 2.0)


def compile_previs(shots, hop_frames, fps=FPS):
    """The previs's timed description: `At MM:SS.mmm <moment>` per window.

    A moment that carries a spoken line starts ahead of its window, by half the
    line (at most LEAD_MAX_S), so the window lands inside the line rather than at
    its first word: both hops tied to that window then share the middle of the
    sentence, and each says its own end of it. A line in the closing moment
    instead ENDS where the previs ends, so the film's last hop finishes it.
    """
    lay = layout(hop_frames)
    rows = []
    ms = moments(shots)
    for k, (w, m) in enumerate(zip(lay["windows"], ms)):
        if not m:
            continue
        t = w / float(fps)
        sp = _spoken_s(m)
        if sp and k == len(ms) - 1:
            t = max(0.0, (w + WINDOW) / float(fps) - sp)
        elif sp:
            t = max(0.0, t - _lead_s(sp))
        rows.append("At %s %s" % (_stamp(t), m))
    return " ".join(rows), lay


_SID = re.compile(r"\(S\d+\)")
_WHO = re.compile(r"@[A-Za-z0-9_]+|<Subject \d+>")
CUTOFF = "<|cutoff|>"       # core's tokenizer special token (comfy/text_encoders/minimax.py)


def _line_parts(moment):
    """(text before the line, language tag, words) of a moment's first spoken line."""
    m = _D.search(moment or "")
    if not m:
        return None
    inner = m.group(1).strip()
    tag = _TAG.match(inner)
    lang = tag.group(0).strip() if tag else ""
    words = _TAG.sub("", inner).split()
    return moment[:m.start()].strip(), lang, words


def split_crossing(shots, fps=FPS):
    """Shots with every crossing line written into the two hops that share it.

    A spoken line in shot k's `moment` (k > 0) straddles window k, which both
    hop k-1 (its last 22 frames) and hop k (its first 22) are tied to. Neither
    beat contains those words, so without this each hop invents its half: the
    node's first run garbled the line on both sides (chain_00134). Hop k-1 now
    ends on the line's opening words up to the end of the window, cut by core's
    `<|cutoff|>` token ("speech truncated by the end of the video"); hop k picks
    the same sentence up from the start of the window. The film keeps hop k-1's
    copy of the window, so the sentence plays once. A line in the last shot's
    `moment_end` is appended whole to the last hop, which the previs ends on.

    Returns copies; the input shots are not modified. Shot 0's `moment` is the
    film's first frame and is left to the beat.
    """
    out = [dict(s or {}) for s in shots or []]
    per_s = 1.0 / SPEECH_WPS
    n_win = max(1, int(round(WINDOW / float(fps) / per_s)))
    for k in range(1, len(out)):
        parts = _line_parts(out[k].get("moment"))
        if not parts:
            continue
        prefix, lang, words = parts
        if not words:
            continue
        n_before = int(round(_lead_s(len(words) * per_s) / per_s))
        head = " ".join(words[:min(len(words), n_before + n_win)])
        rest = " ".join(words[min(n_before, len(words) - 1):])
        sid = _SID.search(prefix)
        who = _WHO.search(prefix)
        speaker = ("%s %s" % (who.group(0) if who else "", sid.group(0) if sid else "")).strip()
        lead = (prefix + " " if prefix else "")
        out[k - 1]["beat"] = ("%s %s<d>%s %s%s</d>" % (
            str(out[k - 1].get("beat") or "").rstrip(), lead, lang, head, CUTOFF)).strip()
        out[k]["beat"] = ("%s continues the same sentence, <d>%s %s</d> %s" % (
            speaker or "The speaker", lang, rest, str(out[k].get("beat") or "").lstrip())).strip()
    if out:
        parts = _line_parts(out[-1].get("moment_end"))
        if parts and parts[2]:
            prefix, lang, words = parts
            out[-1]["beat"] = ("%s %s<d>%s %s</d>" % (
                str(out[-1].get("beat") or "").rstrip(), (prefix + " ") if prefix else "",
                lang, " ".join(words))).strip()
    return out


def crossing_lints(shots):
    """Warnings for a beat that repeats the line its head window already speaks.

    Window k carries shot k's `moment`. If that moment speaks and shot k's own
    beat speaks the same words, the hop says them twice: once from the pin, once
    from the beat (the lab's join-1 repeat). Write the line in the moment only.
    """
    warn = []
    for k, sh in enumerate(shots or []):
        m = " ".join(_D.findall(str((sh or {}).get("moment") or ""))).lower()
        b = " ".join(_D.findall(str((sh or {}).get("beat") or ""))).lower()
        mw = [w.strip(".,!?") for w in _TAG.sub("", m).split()]
        if not mw or not b:
            continue
        hits = [w for w in mw if len(w) > 3 and w in b]
        if len(hits) >= max(2, len(mw) // 3):
            warn.append("hand-tie: shot %d's beat repeats the line in its moment (%s). "
                        "The window already says it; describe only what follows."
                        % (k + 1, ", ".join(sorted(set(hits))[:4])))
    return warn


# --- tone --------------------------------------------------------------------

def tone_offsets(n, head_off, tail_off, frames=WINDOW):
    """[n, C] offsets: head_off up to the head span's centre, tail_off from the tail
    span's centre, linear in between."""
    import torch  # noqa: PLC0415
    c0 = (frames - 1) / 2.0
    c1 = (n - 1) - (frames - 1) / 2.0
    i = torch.arange(n, dtype=torch.float32)
    t = ((i - c0) / max(c1 - c0, 1e-6)).clamp(0.0, 1.0).unsqueeze(1)
    return head_off.unsqueeze(0) * (1.0 - t) + tail_off.unsqueeze(0) * t


def tone_match(images, head, tail=None, frames=WINDOW):
    """Pull a hop's frames onto the tone of the windows that guided it.

    images [N,H,W,3] in 0..1; head/tail [F,H,W,3]. Each guided span is compared with
    the window it was guided by (same content, so the mean difference is the hop's
    tone offset), and the offset is ramped across the hop. Additive per channel:
    a level moved, not a contrast. -> (images, (head_off, tail_off)) in 0..1 units.
    Lab: the cut seam's colour step 1.7 -> 0.6 /255 at join 1.
    """
    n, f = int(images.shape[0]), int(frames)
    if n < 2 * f:
        raise ValueError("hand-tie tone: a %d-frame hop cannot hold two %d-frame spans" % (n, f))
    for name, w in (("head", head), ("tail", tail)):
        if w is not None and tuple(w.shape[1:]) != tuple(images.shape[1:]):
            raise ValueError("hand-tie tone: the %s window is %s, the hop is %s"
                             % (name, tuple(w.shape[1:]), tuple(images.shape[1:])))
    mean = lambda x: x.float().mean(dim=(0, 1, 2))  # noqa: E731
    head_off = mean(head[:f]) - mean(images[:f])
    tail_off = (mean(tail[-f:]) - mean(images[-f:])) if tail is not None else head_off
    off = tone_offsets(n, head_off, tail_off, f).to(images.device, images.dtype)
    return (images + off.view(n, 1, 1, -1)).clamp(0.0, 1.0), (head_off, tail_off)


# --- what hand-tie refuses, and what it keys ------------------------------------

def settings_errors(overlap_frames, last_frame_guide="off", shots=None, hop_frames=None):
    """Messages for settings hand-tie cannot honour. Empty list = fine."""
    err = []
    if int(overlap_frames) != WINDOW:
        err.append("pin_mech=hand_tie needs overlap 0.9 s (22 frames): its windows are 22 frames "
                   "(5-frame windows came out dark after their first frame). Got %d." % int(overlap_frames))
    if str(last_frame_guide or "off") != "off":
        err.append("pin_mech=hand_tie ties every hop's last 22 frames to its next window; "
                   "last_frame_guide=%s would put a second guide on the same frames. "
                   "Set it to off." % last_frame_guide)
    if shots is not None and hop_frames is not None:
        for sec in sections(shots):
            frames = [hop_frames[i] for i in sec]
            lay = layout(frames)
            if lay["previs"] > PREVIS_MAX:
                err.append("hand-tie: shots %d-%d make a %d-frame previs; one previs holds at most "
                           "%d frames (%d hops of 8 s). Add anchor=restart to split the section."
                           % (sec[0] + 1, sec[-1] + 1, lay["previs"], PREVIS_MAX, max_hops()))
    return err


def hop_key_fields(head_digest, tail_digest, head_audio_digest=None, tail_audio_digest=None):
    """A hand-tie hop's key carries its two windows instead of prev_key.

    The hop reads nothing from the hop before it, so editing an earlier shot's beat
    leaves it cached; editing a moment changes the previs, hence both digests.
    """
    out = {"pin": "hand_tie", "head": str(head_digest), "tail": str(tail_digest)}
    if head_audio_digest is not None or tail_audio_digest is not None:
        out["head_audio"] = str(head_audio_digest)
        out["tail_audio"] = str(tail_audio_digest)
    return out
