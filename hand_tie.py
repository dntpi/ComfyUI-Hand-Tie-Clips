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
    tone_match()        a hop pulled onto its window's tone (torch)
    seam_bend()         the outgoing hop bent to meet the next one's first frame
    deflicker()         one- and two-frame level blinks taken out of a section
    detail_settle()     the swell of detail after a cut flattened
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
SPLIT_WPS = 3.5             # H3's speaking rate in a previs: chain_00140 said 7 words between
                            # the line's start and the end of window B, 1.9 s
PREVIS_SEED_OFFSET = 7919    # previs seed = the section's first hop seed + this

# Whether a moment carries its beat's sound sentence as well as its picture.
#
# ON by default. A moment is a picture, and the previs's audio branch has no
# target if the text names none -- so it invents a voice, and every word it
# invents inside a window is pinned onto the hop that opens there. Measured on
# chain_00147's kept previs: 6 invented words, all in the first 4 s, from a plan
# that wrote no dialogue into the previs at all. `_sound_sentence` has the
# numbers and the shape test.
#
# It is a module constant and not a widget because a hand-tie plan must stay
# plain ("plug and play"): the author writes beats and gets a previs that names
# its own sounds, with nothing to switch on. It is here so the behaviour can be
# turned off to A/B the fix, not to be configured.
PREVIS_SOUND = True

# How long a trailing sentence must be to count as a sound bed rather than another
# clause. A narrowband bed is a noun phrase naming a thing or two -- "The low hum
# of the refrigerator." is 6 words, "Steady rain, distant traffic, footsteps
# splashing." is 6 -- and every sound sentence in the 5-beat movement test is 6 or
# more. A one- or two-word tail is a clause the author wrote as an action, and
# appending it tells the previs nothing: it names no sound, so the audio branch is
# left exactly as untargeted as before, and it pads a 0.92 s window for nothing.
# 4 leaves a margin on both sides of the observed 6.
MIN_SOUND_WORDS = 4


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


_SPEECH = re.compile(
    r"(?:,?\s*(?:and\s+)?)?(?:\(S\d+\)\s*)?\b(?:says|asks|whispers|shouts|replies|adds|mutters|calls|"
    r"continues|answers|sings)\b[^<.]{0,40}?<d>.*?</d>", re.S)
_WHO_RE = re.compile(r"@[A-Za-z0-9_]+|<Subject \d+>")


def _no_speech(text):
    """A beat with its spoken lines taken out, speech verb and speaker tag included.

    A moment derived from a plain beat must not carry a dangling "(S1) says,":
    the previs would then put a voice at the window with no words to say, and
    both hops tied to it would inherit invented speech. A plain plan's lines stay
    inside their own hop (user 2026-10-09: no extra input for hand-tie)."""
    t = _SPEECH.sub(".", text or "")
    t = _D.sub("", t)
    t = re.sub(r"\(S\d+\)\s*", "", t)
    t = re.sub(r"\s*\.\s*\.", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _first_sentence(text):
    t = _no_speech(text)
    return _SENT.split(t, 1)[0].strip() if t else ""


_PRONOUN = re.compile(r"^(He|She|They)\b")


def _last_sentence(text):
    """The beat's last sentence that names a person, else its last sentence: a
    beat usually ends on its sound ("the low hum of the refrigerator"), which is
    not a frame. A sentence opening on a pronoun counts, with the beat's first
    named person put in its place: "He walks toward the camera" is the ending a
    writer types, and skipping it handed the closing window the beat's opening."""
    parts = [p.strip() for p in _SENT.split(_no_speech(text)) if p.strip()]
    who = _WHO_RE.search(_no_speech(text))
    for p in reversed(parts):
        if _WHO_RE.search(p):
            return p
        if who and _PRONOUN.match(p):
            return _PRONOUN.sub(who.group(0), p, count=1)
    return parts[-1] if parts else ""


_SUBJ = re.compile(r"^(He|She|They|His|Hers|Their)\b")


def _sound_sentence(text):
    """The beat's trailing sound sentence, or "".

    A moment is a picture. The previs is the one generate every hop follows, so a
    text that describes only pictures leaves its audio branch with no target at
    all, and H3 fills it with the most likely thing there is -- a voice. Measured
    on chain_00147's kept previs (seed 219378, six hops' worth of windows):
    **6 invented words, all inside the first 4 s**, "Looks to be I'm hard."
    (window 0) and "Yeah." (window 1), against a plan that put 20 words of
    dialogue into the hops and none into the previs. Those words are pinned onto
    whichever hop opens at that window.

    `audio_cut` only saved that run by luck: it needs a point inside the window
    where both hops are under AUDIO_QUIET_DB, and the same previs's window 3 --
    the club bass, mean -10.1 dB -- has **zero** such points in 88 probes. On one
    window in six the invention would have reached the film.

    The discriminator is the sentence's SHAPE, not a keyword list. A beat's last
    sentence is its sound when the author wrote one ("A sizzling grill, crowd
    chatter, footsteps on wet pavement.") and is another action when they did not
    ("She turns to the window."). So: the last sentence, provided it neither
    names a person nor opens on a pronoun. `planner._SOUND` was tried first and
    is not it -- its narrowband vocabulary is the right test for a wordless HOP's
    sound bed, and it misses "A thumping bass beat fills the street", which is
    the loudest thing in the plan.
    """
    ns = _no_speech(text)
    parts = [p.strip() for p in _SENT.split(ns) if p.strip()]
    if len(parts) < 2:
        return ""                      # one sentence: nothing but the moment
    cand = parts[-1]
    if _WHO_RE.search(cand) or _SUBJ.match(cand):
        return ""                      # the beat ends on an action, not a sound
    if len(cand.split()) < MIN_SOUND_WORDS:
        return ""                      # too short to be a sound bed (see above)
    return cand


def moments(shots, sound=PREVIS_SOUND):
    """One moment per window: shot k's `moment` (else the first sentence of its beat)
    for window k, and the last shot's `moment_end` (else its beat's last sentence)
    for the closing window.

    `sound` appends each beat's trailing sound sentence to its own moment, so the
    previs's audio branch has a named non-voice target. The CLOSING moment is
    left alone deliberately: it is the film's last frame, and it is read as a
    picture, where a sound sentence would be a second thing at the end of the
    film.
    """
    out = []
    for sh in shots:
        beat = (sh or {}).get("beat")
        m = str((sh or {}).get("moment") or "").strip() or _first_sentence(beat)
        if m and sound:
            snd = _sound_sentence(beat)
            if snd and snd not in m:
                m = m.rstrip() + " " + snd
        out.append(m)
    last = shots[-1] or {}
    out.append(str(last.get("moment_end") or "").strip()
               or _last_sentence(last.get("beat")))
    return out


def _stamp(seconds):
    s = max(0.0, seconds)
    return "%02d:%06.3f" % (int(s // 60), s - 60 * int(s // 60))


def _lead_s(spoken_s):
    """How far ahead of its window a crossing line starts."""
    return min(LEAD_MAX_S, spoken_s / 2.0)


def compile_previs(shots, hop_frames, fps=FPS, sound=None):
    """The previs's timed description: `At MM:SS.mmm <moment>` per window.

    A moment that carries a spoken line starts ahead of its window, by half the
    line (at most LEAD_MAX_S), so the window lands inside the line rather than at
    its first word: both hops tied to that window then share the middle of the
    sentence, and each says its own end of it. A line in the closing moment
    instead ENDS where the previs ends, so the film's last hop finishes it.

    `sound` defaults to PREVIS_SOUND and appends each beat's sound sentence to
    its own moment. It does not disturb the lead-in arithmetic: `_spoken_s`
    counts words only inside `<d>`, and a sound sentence has none.
    """
    lay = layout(hop_frames)
    rows = []
    ms = moments(shots, PREVIS_SOUND if sound is None else sound)
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
    n_win = max(1, int(round(WINDOW / float(fps) * SPLIT_WPS)))
    for k in range(1, len(out)):
        parts = _line_parts(out[k].get("moment"))
        if not parts:
            continue
        prefix, lang, words = parts
        if not words:
            continue
        # The film cuts at the END of the shared window: hop k-1 is kept through
        # it, hop k only after it. So each word goes to the hop that is on screen
        # when it is spoken. Starting hop k at the window's START repeated the
        # window's words after the cut ("will stop" twice, chain_00140).
        lead = _lead_s(len(words) / SPEECH_WPS)
        n_cut = min(len(words) - 1, int(round(lead * SPLIT_WPS)) + n_win)
        head = " ".join(words[:max(1, n_cut)])
        rest = " ".join(words[max(1, n_cut):])
        sid = _SID.search(prefix)
        who = _WHO.search(prefix)
        speaker = ("%s %s" % (who.group(0) if who else "", sid.group(0) if sid else "")).strip()
        lead = (prefix + " " if prefix else "")
        out[k - 1]["beat"] = ("%s %s<d>%s %s%s</d>" % (
            str(out[k - 1].get("beat") or "").rstrip(), lead, lang, head, CUTOFF)).strip()
        out[k]["beat"] = ("%s finishes the same sentence, <d>%s %s</d> %s" % (
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


SEAM_STILL = 4.0 / 255   # a pixel is "still" if it moves less than this on both sides
SEAM_MIN_STILL = 0.05    # below this share of still pixels the step is not trusted
DEFLICKER = 7            # frames in the running median that defines the film's level
DETAIL_SIGMA = 2.5       # px: below this scale is "detail" (the fine and mid bands)
DETAIL_PRE = 12          # frames before a cut whose detail is the level to start from
DETAIL_SPAN = 30         # frames after a cut that are held to the line (the swell)
DETAIL_POST = 12         # frames after the span whose detail is the level to end on
DETAIL_FLOOR = 0.8       # the most detail is ever taken out of a frame


def seam_step(prev_tail, images, start, still=SEAM_STILL):
    """The level step across a cut, measured on pixels that are still on both sides.

    A whole-frame mean confuses motion with tone: on chain_00141 it read 2.4 /255 at
    a join whose background did not move at all, and 0.3 at one whose background
    stepped 2 /255. The median left-minus-right difference over pixels that hold
    still across the two frames before AND the two after the cut is the level shift
    alone. -> (step [C], share of still pixels), step None when too few are still.
    """
    import torch  # noqa: PLC0415
    a2, a1 = prev_tail[-2].float(), prev_tail[-1].float()
    b0, b1 = images[start].float(), images[start + 1].float()
    mask = ((a1 - a2).abs().amax(-1) < still) & ((b1 - b0).abs().amax(-1) < still)
    share = float(mask.float().mean())
    if share < SEAM_MIN_STILL:
        return None, share
    return (a1 - b0)[mask].median(dim=0).values, share


def seam_bend(master, start, cut, images):
    """Bend the outgoing hop, in place, so its last kept frame meets the next hop's first.

    Each hop is pulled onto its HEAD window only. Its tail span is not delivered (the
    film cuts at the window start), and it is the wrong thing to measure: guided at
    -22, it renders about 10 /255 brighter than the window and than the hop's own
    frames before it (chain_00144: 109.7 against 96.4 and 99.0). Ramping a hop toward
    that span darkened the frames the film keeps, and fixing the step that left at the
    cut by fading the next hop showed as a brightening over the second after each join.
    What is left between two head-matched hops is 1-2 /255; it is spread from 0 at
    the outgoing hop's first frame to the full step at its last, which nobody sees.

    master [N,H,W,C]; the outgoing hop occupies master[start:cut]; images is the
    incoming hop, first frame delivered at the cut. -> step per channel, or None.
    """
    import torch  # noqa: PLC0415
    n = int(cut) - int(start)
    if n < 2:
        return None
    step, _share = seam_step(master[cut - 2:cut], images, 0)
    if step is None:
        return None
    w = torch.linspace(0.0, 1.0, n, device=step.device).view(-1, 1, 1, 1)
    master[start:cut] = (master[start:cut].float() - w * step.view(1, 1, 1, -1)
                         ).clamp(0.0, 1.0).to(master.dtype)
    return step


def deflicker(master, frames=DEFLICKER, still=SEAM_STILL):
    """Remove level blinks of a frame or two, in place, over a finished section.

    The film's level, per channel, is the running sum of each frame's still-pixel
    step; its running median over `frames` is the level the film should have, and
    the difference is the blink. That takes out the one-frame dip every guided head
    has where its 17-frame VAE cycle meets the guide's end (frame 17: -3 /255, back
    by 19), and leaves any change that lasts longer than half the window. Additive,
    so a level moves, not a contrast. -> the largest correction in /1 units.
    """
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415
    n = int(master.shape[0])
    if n < frames:
        return 0.0
    d = torch.zeros(n, int(master.shape[-1]))
    for t in range(1, n):
        a, b = master[t - 1].float(), master[t].float()
        lo = master[t - 2].float() if t >= 2 else a
        hi = master[t + 1].float() if t + 1 < n else b
        mask = ((a - lo).abs().amax(-1) < still) & ((hi - b).abs().amax(-1) < still)
        if float(mask.float().mean()) >= SEAM_MIN_STILL:
            d[t] = (b - a)[mask].median(dim=0).values.cpu()
    level = d.cumsum(0)
    r = frames // 2
    padded = F.pad(level.T[None], (r, r), mode="replicate")[0]
    corr = padded.unfold(1, frames, 1).median(-1).values.T - level
    for t in range(n):
        if float(corr[t].abs().max()) > 0.0:
            master[t] = (master[t].float() + corr[t].to(master.device).view(1, 1, -1)
                         ).clamp(0.0, 1.0).to(master.dtype)
    return float(corr.abs().max())


CUT_RATIO = 6.0          # a cut changes the frame this many times more than its neighbours do
CUT_COMP_RATIO = 12.0    # ... and the coarse composition this many times more
CUT_FLOOR = 20.0 / 255   # ... and by at least this much: a jump in a still shot is no cut
CUT_SPAN = 12            # frames either side that set "its neighbours"
PREVIS_TRIES = 3         # previs renders before the least-cut one is used anyway
PREVIS_RETRY_STRIDE = 104729   # seed step between tries: clear of every hop's own seed


def previs_cuts(frames):
    """Frames where the previs hard-cuts to another shot: [index of the first new frame].

    The previs is the single point of failure: every hop follows it, and a cut in it
    becomes a cut in the film (chain_00135 split two kitchens; chain_00146 jumped
    from a medium to a wide at 13.08 s). Nobody runs a previs-only dry run first, so
    the node checks its own. A cut is a frame whose change is CUT_RATIO times the
    median change of the CUT_SPAN frames either side, whose coarse composition jumps
    CUT_COMP_RATIO times, and by at least CUT_FLOOR. Calibrated on films: real cuts
    9-87x / 25-164x / 52-74 per 255; clean hand-tie films (rain, dancing, walking
    included) at most 4.2x / 6.6x.
    """
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415
    x = frames.float()
    if int(x.shape[0]) < 2 * CUT_SPAN:
        return []
    x = F.avg_pool2d(x.permute(0, 3, 1, 2), 4)                 # quarter size is plenty
    m = (x[1:] - x[:-1]).abs().mean(dim=(1, 2, 3))
    small = F.avg_pool2d(x.mean(1, keepdim=True), 8)[:, 0]
    c = (small[1:] - small[:-1]).abs().mean(dim=(1, 2))
    n, out = int(m.shape[0]), []
    for t in range(n):
        idx = [k for k in range(max(0, t - CUT_SPAN), min(n, t + CUT_SPAN + 1)) if abs(k - t) > 1]
        lm, lc = float(m[idx].median()), float(c[idx].median())
        if (float(m[t]) >= CUT_FLOOR and float(m[t]) > CUT_RATIO * max(lm, 1e-4)
                and float(c[t]) > CUT_COMP_RATIO * max(lc, 1e-4)):
            out.append(t + 1)
    return out


AUDIO_QUIET_DB = -35.0   # dBFS: the outgoing hop is quiet enough to leave here
AUDIO_PROBE_S = 0.04     # seconds of sound judged at each candidate point (the xfade)
AUDIO_STEP_S = 0.01


def audio_cut(out_tail, in_head, sr):
    """Samples past the picture cut at which the sound should switch hops.

    Both hops render the window, so the sound can switch anywhere inside it and
    stay in sync. chain_00147 showed two words broken at the picture cut:
    the outgoing hop was still finishing "tonight" (0.11 s past the cut), and the
    incoming hop opened on a fragment of a word the PREVIS had invented ("air",
    0.35 s): its window sound is pinned to the incoming hop's frame 0, so that hop
    repeats it. The sound leaves at the first point where BOTH hops are quiet
    (below AUDIO_QUIET_DB); failing that, where the outgoing one is; failing that
    (one continuous sound, rain or music, across the window) at the picture cut.

    out_tail / in_head: [..., S] the outgoing hop's window-span sound and the
    incoming hop's, same length. -> samples (0 .. S - probe).
    """
    import torch  # noqa: PLC0415
    a = out_tail.float().reshape(-1, out_tail.shape[-1]).mean(0)
    b = in_head.float().reshape(-1, in_head.shape[-1]).mean(0)
    s = min(int(a.shape[-1]), int(b.shape[-1]))
    w, step = max(1, int(AUDIO_PROBE_S * sr)), max(1, int(AUDIO_STEP_S * sr))
    db = lambda x: 20.0 * float(torch.log10(x.pow(2).mean().sqrt() + 1e-9))  # noqa: E731
    out_quiet = None
    for p in range(0, s - w + 1, step):
        la, lb = db(a[p:p + w]), db(b[p:p + w])
        if la < AUDIO_QUIET_DB:
            if lb < AUDIO_QUIET_DB:
                return p
            if out_quiet is None:
                out_quiet = p
    return out_quiet or 0


def _detail_split(frame, sigma=DETAIL_SIGMA):
    """(base, detail energy) of one [H,W,C] frame: a separable gaussian blur and the
    mean absolute difference from it."""
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415
    x = frame.float()
    r = int(3 * sigma + 0.5)
    k = torch.exp(-torch.arange(-r, r + 1, dtype=torch.float32) ** 2 / (2 * sigma * sigma))
    k = (k / k.sum()).to(x.device)
    y = x.permute(2, 0, 1)[:, None]
    y = F.conv2d(F.pad(y, (r, r, 0, 0), mode="reflect"), k.view(1, 1, 1, -1))
    y = F.conv2d(F.pad(y, (0, 0, r, r), mode="reflect"), k.view(1, 1, -1, 1))
    base = y[:, 0].permute(1, 2, 0)
    return base, float((x - base).abs().mean())


def detail_settle(master, cut):
    """Flatten the swell of detail after a cut, in place. -> the gains used, or None.

    The incoming hop opens on its head window and sharpens as it leaves it: on
    chain_00144 fine detail rose +12% (mid +7%) over the half second after join 1
    and settled back. Brightness cannot show this and a grade cannot fix it. For the
    DETAIL_SPAN frames after the cut, each frame's detail (frame minus its blur) is
    scaled so its energy follows a straight line from the level before the cut to
    the level after the span. It only ever softens (gain <= 1, floor DETAIL_FLOOR);
    nothing outside the span moves. Replay on 00144: join 1 +12% -> +5%.
    """
    import torch  # noqa: PLC0415
    n = int(master.shape[0])
    a, b = cut + DETAIL_SPAN, min(n, cut + DETAIL_SPAN + DETAIL_POST)
    if cut - DETAIL_PRE < 0 or b - a < 4:
        return None
    pre = sorted(_detail_split(master[t])[1] for t in range(cut - DETAIL_PRE, cut))
    post = sorted(_detail_split(master[t])[1] for t in range(a, b))
    e0, e1 = pre[len(pre) // 2], post[len(post) // 2]
    gains = []
    for j in range(DETAIL_SPAN):
        t = cut + j
        base, e = _detail_split(master[t])
        g = max(DETAIL_FLOOR, min(1.0, (e0 + (e1 - e0) * j / float(DETAIL_SPAN)) / max(e, 1e-6)))
        if g < 1.0:
            f = master[t].float()
            master[t] = (base + g * (f - base)).clamp(0.0, 1.0).to(master.dtype)
        gains.append(g)
    return gains


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
