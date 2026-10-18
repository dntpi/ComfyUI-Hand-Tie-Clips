"""Hand-tie: continuity by tying every hop to one previs, never to another hop.

The Motion-Context join pins hop N+1 on hop N's finished tail, so hop N's
errors become hop N+1's starting truth: texture steps about x1.04 per join and
long chains drift or cut. Hand-tie removes the relay. One PREVIS generate covers
a whole section (a chain start and its continuations); 22-frame WINDOWS cut from
it tie the hops together: hop k is guided by window k at frame 0 and window k+1
at frame -22 (picture and sound, stock MiniMaxH3AddGuide), and no hop reads
another. The film cuts each join at the start of the shared window.

One window per hop: a section's last hop is tied at its start only and ends where
its beat takes it. (A closing window used to pin that last hop's end too. No hop
shares it, and the previs had to reach the ending to draw it: chain_00150's three
takes all cut there, to "walks toward the camera".) A run longer than one previs
holds -- PREVIS_MAX, 6 hops of 8 s -- is split into CHAINED sections, as is a shot
marked anchor=extend (what "+ extend" writes): a chained section's previs is pinned
at frame 0 on the frames the hop before it actually rendered, so the join between
sections is an ordinary hand-tie join, at the cost of one copy step there, and
the hops already rendered keep their previs and their cache.

Measured in HTC-Lab (lab name "bridge clips", 2026-10-07/08, see DEVLOG): over 4
hops the Motion-Context arm ratcheted x1.053 / x1.042 at joins 2-3 and drifted to
a new camera; the hand-tie film held one camera and one light, each hop one
copy step from its window, never compounding (2 of 2 runs).

This module is the arithmetic and the text, with no ComfyUI imports, so
tools/check_hand_tie.py runs without a server:

    sections()          a plan's hops grouped into sections (one previs each)
    chained()           whether a section continues the hop before it
    spacing()           previs frames between two windows for one hop
    layout()            window starts and previs length for a section
    window_audio()      a window's slice of the previs soundtrack, in seconds
    compile_previs()    the previs's timed description from the shots
    crossing_lints()    beats that repeat a line their head window already says
    tone_offsets()      the per-frame tone ramp (torch)
    tone_match()        a hop pulled onto its window's tone (torch)
    seam_bend()         the outgoing hop bent to meet the next one's first frame
    body_grade()        the incoming hop graded to the body a second off the cut
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
PREVIS_MAX = 362            # 15.1 s, H3's trained ceiling; = 6 hops of 8 s
REF_HOP, REF_SPACING = 192, 68   # the measured case: 8 s hop, 2.8 s of previs per hop
MIN_SPACING = 34            # windows never overlap
SPEECH_WPS = 2.5            # words a second, as planner.SPEECH_WPS
LEAD_MAX_S = 1.0            # a line starts at most this far ahead of its window
SPLIT_WPS = 3.5             # H3's speaking rate in a previs: chain_00140 said 7 words between
                            # the line's start and the end of window B, 1.9 s
PREVIS_SEED_OFFSET = 7919    # previs seed = the RUN seed at the section start + this (never a shot seed)

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

# The previs is layout: camera, place, light, blocking at the windows. It renders at
# PREVIS_RESOLUTION when that is smaller than the hop canvas (AddGuide resizes each
# window to the hop), so a take costs about a third and a re-roll is cheap. And it is
# MUTED: its sound is never used (a window pins picture only unless the plan speaks
# there), but it invented speech on every take and the windows showed mouths talking
# at the joins. A muted previs renders to frozen silence. A section with a line in a
# window keeps its sound: that window carries the previs's words. Module constants,
# not widgets: hand-tie stays plug-and-play; these are here to A/B.
PREVIS_RESOLUTION = "512p (0.44 MP)"
PREVIS_MUTE = True

# The sound first. Each hand-tie hop drafts its soundtrack at SOUND_DRAFT_RESOLUTION
# (the picture is thrown away), checks it against the line it should say, and the
# real hop renders its picture to that soundtrack, frozen. H3 lip-syncs to a fixed
# track (the music videos: the take drives the mouth), so a line can no longer come
# out of a closed mouth (chain_00164/00165 hop 1, 00165 hops 7/8/10), and invented
# words are caught on a draft costing a fraction of a hop (00165 hop 4, the Carolyn
# run's gibberish). The check needs Whisper in the local Hugging Face cache; without
# it the first draft is kept unchecked and the lock still holds the mouth.
# OFF by default since ht_seam_fix v3 (2026-10-14): the draft pass (TE reload, DiT swap,
# 448p sample, audio decode, Whisper, up to SOUND_TRIES times) cost 1-2 min a hop and
# v2 renders lost lip sync with it on. Hops now render picture and sound in one pass,
# voice ref on the hop's own conditioning, Motion-Context carrying the sound on hops 2+.
# Set True to opt back in.
SOUND_FIRST = False
SOUND_DRAFT_RESOLUTION = "448p (0.34 MP)"
SOUND_TRIES = 3              # drafts before the best one is used anyway
SOUND_RETRY_STRIDE = 15485863   # seed step between drafts: clear of hops and previs
SOUND_RECALL = 0.6           # share of the line's words a draft must say
QUIET_WORDS = 2              # words a quiet hop may "say" (Whisper hears "Thank you." in a hum)

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

def _anchor(sh):
    return str((sh or {}).get("anchor") or "")


def sections(shots, hop_frames=None):
    """[[hop index, ...], ...]: one previs each.

    Hop 0 and every anchor=restart open a section (a cut, as everywhere). So do
    anchor=extend and a hop that would push the previs past PREVIS_MAX; those
    sections are CHAINED (see `chained`) and continue the film without a cut.
    Without `hop_frames` only the shot fields split.

    A run the previs size alone splits is split EVENLY into the same number of
    sections: 10 x 8 s is 5 + 5, not 6 + 4. Same previs count, and a section of 5
    or fewer has room for its last hop's closing window (`layout`), which a full
    6 does not -- the start-only last hop is the one that snapped at f22.
    """
    out, frames, auto = [], [], []
    for i, sh in enumerate(shots or []):
        f = int(hop_frames[i]) if hop_frames is not None else None
        # layout() only adds a closing window that fits, so this is the
        # one-window-per-hop length: the closing window never splits a section.
        full = (f is not None and out
                and layout(frames + [f])["previs"] > PREVIS_MAX)
        if i == 0 or _anchor(sh) in ("restart", "extend") or full:
            out.append([])
            auto.append(bool(full) and _anchor(sh) not in ("restart", "extend"))
            frames = []
        out[-1].append(i)
        if f is not None:
            frames.append(f)
    if hop_frames is None:
        return out
    even, s = [], 0
    while s < len(out):
        e = s + 1
        while e < len(out) and auto[e]:
            e += 1
        hops = [h for sec in out[s:e] for h in sec]
        k, n = e - s, len(hops)
        sizes = [n // k + (1 if j < n % k else 0) for j in range(k)]
        cut, pos = [], 0
        for z in sizes:
            cut.append(hops[pos:pos + z])
            pos += z
        fits = all(layout([int(hop_frames[h]) for h in sec])["previs"] <= PREVIS_MAX for sec in cut)
        even.extend(cut if fits else out[s:e])
        s = e
    return even


def chained(shots, section):
    """True when a section continues the hop before it (no cut): its previs is
    pinned at frame 0 on that hop's last frames, and its first window IS them."""
    return bool(section) and section[0] > 0 and _anchor(shots[section[0]]) != "restart"


def spacing(hop_frames):
    """Previs frames between this hop's two windows: the 68-per-192 ratio, on the grid."""
    q = max(1, int(round(hop_frames * REF_SPACING / float(REF_HOP) / GRID)))
    return max(MIN_SPACING, q * GRID)


def layout(hop_frames):
    """Window starts and previs length for one section's hops.

    -> {"windows": [w0, ..., w(n-1)(, wn)], "spacings": [...], "previs": frames,
        "closing": bool}
    One window per hop: hop k is tied to window k at its start and to window k+1
    at its end. The section's last hop gets a CLOSING window when the previs has
    room for one (sections of up to 5 hops of 8 s); in a full section it is tied
    at its start only. Unguided past its head window, chain_00155 and 00156's last
    hop snapped at f22 on all three takes (a cut to another shopfront, then the
    skewer jumping to her mouth), where nine tail-guided hops ran smoothly through
    the same frame. The closing window has no moment of its own: the previs plays
    the last moment on into it, so there is no new text to cut to (the closing
    MOMENT is what chain_00150's previs cut to reach, on every take).
    """
    if not hop_frames:
        raise ValueError("hand-tie: a section needs at least one hop")
    sp = [spacing(int(f)) for f in hop_frames]
    w = [0]
    for s in sp[:-1]:
        w.append(w[-1] + s)
    closing = w[-1] + sp[-1] + WINDOW <= PREVIS_MAX
    if closing:
        w.append(w[-1] + sp[-1])
    else:
        sp = sp[:-1]
    return {"windows": w, "spacings": sp, "previs": w[-1] + WINDOW, "closing": closing}


def max_hops(hop_frames=REF_HOP):
    """How many hops of this length one previs can hold."""
    return (PREVIS_MAX - WINDOW) // spacing(hop_frames) + 1


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
    r"(?:,?\s*(?:and\s+)?)?(?:\(S\d+\)\s*)?\b(?:says?|asks?|whispers?|shouts?|replies|reply|adds?|mutters?|calls?|"
    r"continues?|answers?|sings?)\b[^<.]{0,40}?<d>.*?</d>", re.S)
_QUOTED_SPEECH = re.compile(
    r'(\b(?:says?|asks?|whispers?|shouts?|replies|reply|adds?|mutters?|calls?|'
    r'continues?|answers?|sings?)\b[^<."]{0,20}?)"([^"\n]{2,})"', re.I)
_WHO_RE = re.compile(r"@[A-Za-z0-9_]+|<Subject \d+>")


_TALKING = re.compile(
    r"(?:,\s*|\s+and\s+|\s+)(?:still\s+|keeps\s+)?(?:talking|chatting|speaking|narrating)\b[^,.;]*", re.I)


_MOUTH_OPEN_SAY = re.compile(
    r"(?:\b(?:then|and)\s+)?(?:opens?\s+(?:her|his|their|its)\s+mouth\s+to\s+"
    r"|(?:(?:her|his|their|its)\s+)?lips\s+part\s+and\s+)"
    r"(?=(?:says?|asks?|whispers?|shouts?|replies|reply|adds?|mutters?|calls?|continues?|answers?|sings?)\b)",
    re.I)
_SPEECH_SENT = re.compile(
    r"(?:^|(?<=[.!?]\s))(?:Then,\s*)?(?:@[A-Za-z0-9_]+|<Subject \d+>|He|She|They)\s+"
    r"(?:(?:opens?\s+(?:her|his|their|its)\s+mouth\s+to\s+|(?:her|his|their|its)\s+lips\s+part\s+and\s+))?"
    r"(?:\(S\d+\)\s*)?(?:says?|asks?|whispers?|shouts?|replies|reply|adds?|mutters?|calls?|"
    r"continues?|answers?|sings?)\b[^<.\"]{0,40}?(?:<d>.*?</d>|\"[^\"\n]{2,}\")[.!?]?\s*",
    re.I | re.S,
)


def _no_speech(text):
    """A beat with its spoken lines taken out, speech verb and speaker tag included.

    A moment derived from a plain beat must not carry a dangling "(S1) says,":
    the previs would then put a voice at the window with no words to say, and
    both hops tied to it would inherit invented speech. A plain plan's lines stay
    inside their own hop (user 2026-10-09: no extra input for hand-tie)."""
    t = str(text or "")
    t = _MOUTH_OPEN_SAY.sub("", t)
    t = _SPEECH_SENT.sub("", t)
    t = _SPEECH.sub(".", t)
    t = _QUOTED_SPEECH.sub(".", t)
    t = _D.sub("", t)
    # A talking cue with no words is worse than none: H3 invents the speech in its
    # default language and, in a phone vlog, burns in captions for it (a rejected
    # take of the 9:16 vlog previs, 2026-10-10: Chinese subtitles under "talking into it").
    t = _TALKING.sub("", t)
    t = re.sub(r"\(S\d+\)\s*", "", t)
    t = re.sub(r"\.\s+then\b", ", then", t, flags=re.I)
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


def _end_sentence(text):
    """How the beat ends: its last sentence after the first that names a person
    (a pronoun opening counts, the named person put in its place), else "".

    The previs used to get only the opening, so it never finished a beat. The
    Carolyn taste beat (2026-10-13) stopped at "chews"; the swallow lived in its
    second sentence. Window 5 then caught her mid-chew and hop 5 said its line
    through it. Unlike `_last_sentence`, a sound sentence is never an ending.
    """
    parts = [p.strip() for p in _SENT.split(_no_speech(text)) if p.strip()]
    who = _WHO_RE.search(_no_speech(text))
    for p in reversed(parts[1:]):
        if _WHO_RE.search(p):
            return p
        if who and _PRONOUN.match(p):
            return _PRONOUN.sub(who.group(0), p, count=1)
    return ""


# A sound that is a voice. Under cfg 1.0 nothing is subtracted, so "the chatter of
# the crowd" or "the vendor calling out orders" is rendered as people talking, and
# with no words given H3 makes them up (the Carolyn run, 2026-10-13: gibberish over
# the walk). Hand-tie leaves these out of the previs and the hops; `line_lints` says
# so. Only voices that carry WORDS: laughter, giggles and cheers are the non-verbal
# sounds the official soundscape field is for, and "hum" is the low hum of a
# refrigerator, the sound bed this pack recommends.
_VOICE = re.compile(
    r"\b(chatter\w*|voices?|talk\w*|conversations?|murmur\w*|calls? out|calling out|"
    r"call(?:s|ing)? orders|shout\w*|yell\w*|singing|sings|"
    r"whisper\w*|chant\w*|banter\w*|announc\w*)\b", re.I)


def quiet_sound(sentence):
    """(the sound sentence without its voices, [what was taken out]).

    Items are the sentence's comma / "and" parts. Nothing is rewritten unless a
    voice is found; "" when every item was one."""
    body = (sentence or "").strip()
    items = [i.strip() for i in re.split(r",\s*(?:and\s+)?|\s+and\s+", body.rstrip(".!")) if i.strip()]
    gone = [i for i in items if _VOICE.search(i)]
    if not gone:
        return sentence, []
    keep = [i for i in items if not _VOICE.search(i)]
    if not keep:
        return "", gone
    s = keep[0] if len(keep) == 1 else ", ".join(keep[:-1]) + " and " + keep[-1]
    return s[0].upper() + s[1:] + ".", gone


def _trailing_sound(beat):
    """(text before, the beat's trailing sound sentence) or None. A sound sentence
    follows another sentence, names no one, opens on no pronoun, and holds no line."""
    b = str(beat or "").rstrip()
    cut = max(b.rfind(". "), b.rfind("! "), b.rfind("? "), b.rfind("</d> "))
    if cut < 0:
        return None
    cut += 6 if b[cut:cut + 5] == "</d> " else 2
    last = b[cut:]
    if not last or "<d>" in last or _WHO_RE.search(last) or _SUBJ.match(last) \
            or re.search(r"\(S\d+\)", last):
        return None
    return b[:cut], last


# Closed lips on a hop that speaks. Under cfg 1.0 the phrase is not timed: it pulls
# on the whole hop, and the mouth closes over the line. The Carolyn push run
# (2026-10-13) said "What's up, everybody, welcome back to the channel" with a
# moving mouth and "Today is all about street food" with lips closed, under "She
# grins with her lips closed" stamped after the line. The writing rule that asked
# for it is gone; this takes it out of plans already written that way.
_LIPS = re.compile(
    r",?\s*(?:with\s+)?(?:(?:her|his|their|its)\s+)?lips\s+(?:closed|shut|pressed\s+together)"
    r"(?:\s+in\s+a\s+smile)?", re.I)
_SAY_VERB = re.compile(
    r"\b(says?|asks?|whispers?|shouts?|replies|reply|adds?|mutters?|calls?|"
    r"continues?|answers?|sings?)(\b[^<.]{0,20}?<d>)", re.I)


def _3p_verb(v):
    low = v.lower()
    if low == "say":
        return "says"
    if low == "reply":
        return "replies"
    if not low.endswith("s"):
        return v + "s"
    return v


def _clean_dialogue_pauses(beat):
    """Replace mid-line ellipses (`...`, `…`) and dashes (`—`, `–`, `--`) inside
    `<d>...</d>` with a comma (`, `), and trailing ellipses before `</d>` with a
    period (`.`). In H3's audio branch, an ellipsis inside `<d>...</d>` leaves an
    unaligned phoneme gap that the DiT fills with gibberish syllables."""
    def _fix_span(m):
        inner = m.group(1)
        # Mid-line ellipsis or dash between words -> ", "
        inner = re.sub(r",?\s*(?:\.{2,}|…|—|–|--)\s*(?=[A-Za-z0-9'\"])", ", ", inner)
        # Trailing ellipsis or dash before <|cutoff|> -> strip
        inner = re.sub(r",?\s*(?:\.{2,}|…|—|–|--)\s*(?=<\|cutoff\|>)", "", inner)
        # Trailing ellipsis or dash at end of line -> "."
        inner = re.sub(r",?\s*(?:\.{2,}|…|—|–|--)\s*$", ".", inner)
        return f"<d>{inner}</d>"
    return re.sub(r"<d>(.*?)</d>", _fix_span, str(beat or ""), flags=re.S)


def open_lips(shots):
    """Shots with closed-lips phrases taken out of every beat that has a line
    (and untagged double-quoted speech upgraded to `<d>[English] ...</d>`,
    mid-line `...`/`—` pauses inside `<d>...</d>` normalized to `, `,
    untimed mouth-opening filler stripped before speech verbs, `(S1)` supplied
    before speech verbs on single-speaker dialogue sentences so `time_lines`,
    `_start_carries_voice`, and `refs.speaker_ids` all bind `<Audio 1>` to
    `<Subject 1> (S1)`, and continuation `framing` directives aligned to the
    section's opening `framing` so hop prompts never fight the Previs windows).
    A quiet hop keeps closed-lips phrases. Returns copies."""
    out = []
    sec_framing = None
    for idx, sh in enumerate(shots or []):
        s = dict(sh or {})
        d_raw = dict(s.get("directives") or {})
        if idx == 0 or str(s.get("anchor") or "") == "restart":
            sec_framing = d_raw.get("framing")
        elif sec_framing and d_raw.get("framing") and d_raw.get("framing") != sec_framing:
            d_raw["framing"] = sec_framing
            s["directives"] = d_raw
        b = str(s.get("beat") or "")
        if not _D.search(b) and _QUOTED_SPEECH.search(b):
            b = _QUOTED_SPEECH.sub(
                lambda m: f"{m.group(1)}<d>[English] {m.group(2).strip()}</d>", b)
        if _D.search(b):
            b = _clean_dialogue_pauses(b)
            if _MOUTH_OPEN_SAY.search(b):
                b = _MOUTH_OPEN_SAY.sub("and ", b)
                b = re.sub(r"\b(?:Then,\s*)?(@[A-Za-z0-9_]+|<Subject \d+>)\s+and\s+", r"\1 ", b)
                b = re.sub(r"\band\s+and\s+", "and ", b)
            tags = set(_WHO_RE.findall(b))
            if len(tags) == 1 and not _SID.search(b):
                b = _SAY_VERB.sub(
                    lambda m: f"(S1) {_3p_verb(m.group(1))}{m.group(2)}", b, count=1)
            if _LIPS.search(b):
                b = re.sub(r"\s+([.,!?])", r"\1", _LIPS.sub("", b))
            s["beat"] = b
        out.append(s)
    return out


def quiet_sounds(shots):
    """Shots with the voices taken out of each beat's trailing sound sentence.
    Returns copies; a beat without one, or without a voice in it, is unchanged."""
    out = []
    for sh in shots or []:
        s = dict(sh or {})
        tr = _trailing_sound(s.get("beat"))
        if tr:
            q, gone = quiet_sound(tr[1])
            if gone:
                s["beat"] = (tr[0] + q).rstrip()
        out.append(s)
    return out


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
    for window k. There is no closing moment: the last hop ends where its beat goes.

    `sound` appends a trailing sound sentence, so the previs's audio branch has a
    named non-voice target. Window k is the first 0.9 s of hop k, which is the moment
    hop k-1 ENDS, so it takes the latest closing sound before hop k (ambience carries
    until a beat names another); window 0 takes its own beat's.
    A beat's last sentence is how it ends: chain_00154's "The crunch of her chewing"
    on the bite hop put chewing at that hop's opening window, the previs ate early,
    hop 3 opened with the skewer already in her mouth and hop 4 spoke its line while
    chewing.
    """
    out = []
    quiet = lambda b: quiet_sound(_sound_sentence(b))[0]  # noqa: E731
    for k, sh in enumerate(shots):
        beat = (sh or {}).get("beat")
        m = str((sh or {}).get("moment") or "").strip()
        if not m:
            # The beat played to its end, in the same moment: an ending with a
            # stamp of its own was a cut on every take (the Carolyn eeebcad run
            # cut at 4.83 / 7.54 / 10.50 s against endings stamped 4.71 / 7.54 /
            # 10.38 s). Every `At` is a shot boundary to H3.
            m = _first_sentence(beat)
            end = _end_sentence(beat)
            if m and end:
                m = m + " " + end
        if m and sound:
            snd = quiet(beat) if not k else next(
                (s for s in (quiet((shots[j] or {}).get("beat")) for j in range(k - 1, -1, -1))
                 if s), "")
            if snd and snd not in m:
                m = m.rstrip() + " " + snd
        out.append(m)
    return out


def window_speaks(shots, n_windows):
    """[bool] per window: does the plan put a spoken line there?

    Only those windows pin the previs's SOUND as well as its picture. Everywhere
    else the previs has no line to say, renders a person anyway, and invents
    speech: every take of chain_00159's previs talked ("I'm going to go to the
    rescue team..."), and the 0.9 s window inside it opened hop 2 on "Right,".
    00153's "Tenya" came the same way. A window with a planned line is a crossing
    line (a `moment` with `<d>`): there the previs speaks the plan's own words. The
    closing window never has a moment, so it never carries sound.
    """
    ms = moments(shots, sound=False)
    return [k < len(ms) and _spoken_s(ms[k]) > 0 for k in range(int(n_windows))]


def _stamp(seconds):
    s = max(0.0, seconds)
    return "%02d:%06.3f" % (int(s // 60), s - 60 * int(s // 60))


def _lead_s(spoken_s):
    """How far ahead of its window a crossing line starts."""
    return min(LEAD_MAX_S, spoken_s / 2.0)


# --- previs cut lint (2026-10-14) -------------------------------------------------
# Every take of the Storyboard4 previs (17:26 run) cut 6-13 frames BEFORE a window
# stamp: seed 13069 at f62 / f126 / f191 against stamps f68 / f136 / f204, seed 117798
# at f195 (f204). H3 cuts where the text starts a new shot, and the moment behind a
# stamp read as one: a place reached or re-established (window 1 "slows to a stop
# beside a street food stall"; window 2 "stands at the street food stall on the same
# street"), the camera distance or the background changed ("steps back from the
# stall", "the stall behind her"). This lint reads the moments before the previs
# renders, flags each such trigger per window, and rewrites the ones it can turn
# into continuous action without changing what happens. Pure text, CPU only.
# Set False by Grok bot, 2026-10-14 18:35, in the muted-window voice-fix edit.
# User attested that attribution on 2026-10-15. Reason recorded then: Storyboard4
# run 2, wording moved the cut and did not close it. Not a UI switch.
PREVIS_CUT_LINT = False

_ARRIVE = re.compile(
    r"\b(?P<verb>slows to a stop|comes to a stop|stops|arrives|pulls up)"
    r" (?P<prep>beside|at|in front of|next to|by) (?P<det>a|an|the) (?P<place>[^,.;]+?)"
    r"(?=[,.;]| and\b|$)", re.I)
_REESTABLISH = re.compile(
    r"(?P<who>@[A-Za-z0-9_]+|<Subject \d+>|She|He|They) (?P<verb>stands|is standing|waits|sits)"
    r" (?P<prep>at|beside|by|in front of|next to) the (?P<place>[^,.;]+?)"
    r"(?P<same> on the same (?:street|road|block|corner))?(?= and\b|[,.;])", re.I)
_STEP_AWAY = re.compile(
    r"\b(?P<verb>steps back|steps away|walks away|backs away|moves away) from the (?P<place>[^,.;]+?)"
    r" and\b", re.I)
_BEHIND = re.compile(r",? the (?P<place>[^,.;]+?) behind (?:her|him|them)\b", re.I)
_CAMERA = re.compile(
    r"\b(wider|wide shot|wide angle|close-up|closeup|close up on|cut to|cuts to|new shot|"
    r"another angle|from above|from behind|overhead|zooms?|pans? to|reveals?|reveal)\b", re.I)
_TIME = re.compile(
    r"\b(later|the next day|next morning|that night|that evening|hours|minutes later|"
    r"meanwhile|afterwards|after a while|by now|soon after)\b", re.I)
_PLACE_NOUN = re.compile(
    r"\b(?:at|to|into|inside|beside|by|in front of|next to|outside|toward|towards)"
    r" (?:a|an|the) ((?:[a-z]+ ){0,3}(?:stall|shop|store|market|cafe|restaurant|bar|kitchen|"
    r"room|street|corner|park|station|counter|door|doorway|window|stand|truck|cart|booth|"
    r"square|alley|bridge|beach|car|bus|train|office|hall|house|building|stage))\b", re.I)
_HOLD = re.compile(r"\b(?:holding|holds|raises|lifts|waves|eats|bites|takes a (?:big )?bite of)"
                   r" (?:the|her|his|their|a|an) ((?:(?!(?:from|toward|towards|to|with|and|at|of|beside|in|into|on|for)\b)[a-z]+ ?){1,2})", re.I)
_GET = re.compile(r"\b(?:takes|picks up|grabs|is handed|receives|pulls out|accepts)"
                  r" (?:the|her|his|their|a|an) ((?:(?!(?:from|toward|towards|to|with|and|at|of|beside|in|into|on|for)\b)[a-z]+ ?){1,2})", re.I)


def _head(np):
    return np.lower().split()[-1] if np.strip() else ""


def cut_triggers(moments_):
    """[[(kind, phrase, note)]] per window: text likely to make the previs cut at that
    window's stamp. kinds: location, framing, time, state. Window 0 has no stamp
    before it, so it is checked for framing / time only."""
    out, seen_places, seen_things = [], set(), set()
    for k, m in enumerate(moments_):
        m = m or ""
        fl = []
        if k:
            for a in _ARRIVE.finditer(m):
                fl.append(("location", a.group(0), "reaches a new place inside the moment"))
            for a in _REESTABLISH.finditer(m):
                fl.append(("location", a.group(0), "re-establishes the place at the stamp"))
            for p in _PLACE_NOUN.findall(m):
                if _head(p) not in seen_places and not any(p.lower() in f[1].lower() for f in fl):
                    fl.append(("location", p, "a place no earlier window shows"))
            for a in _STEP_AWAY.finditer(m):
                fl.append(("framing", a.group(0)[:-4], "changes the distance to the camera at the stamp"))
            for a in _BEHIND.finditer(m):
                fl.append(("framing", a.group(0).lstrip(", "), "asks for the background to be re-framed"))
            for a in _HOLD.finditer(m):
                h = _head(a.group(1))
                got_here = any(_head(g) == h for g in _GET.findall(m))
                if h not in seen_things and not got_here:
                    fl.append(("state", a.group(0), "holds something no earlier window gave her"))
        for a in _CAMERA.finditer(m):
            fl.append(("framing", a.group(0), "a camera instruction inside the take"))
        for a in _TIME.finditer(m):
            fl.append(("time", a.group(0), "a time jump"))
        seen_places |= {_head(p) for p in _PLACE_NOUN.findall(m)}
        seen_places |= {_head(a.group("place")) for a in _ARRIVE.finditer(m)}
        seen_things |= {_head(g) for g in _GET.findall(m)} | {_head(g) for g in _HOLD.findall(m)}
        out.append(fl)
    return out


def smooth_moments(moments_):
    """(moments, [(window, before, after)]): the rewrites that keep the action and make
    it continuous. Anything not listed here is only warned about (cut_triggers)."""
    out, changes, prev = [], [], ""
    for k, m in enumerate(moments_):
        new = m or ""
        if k:
            # "slows to a stop beside a stall" -> "walks up to a stall and stops beside
            # it": the approach is spoken first, so the stall arrives by walking.
            def _arr(a):
                return "walks up to %s %s, stops %s it," % (a.group("det"), a.group("place"),
                                                            a.group("prep"))
            new = _ARRIVE.sub(_arr, new, count=1)
            # "stands at the stall on the same street and" -> "is still at the stall and",
            # when the previous window already reached that place.
            def _re(a):
                if _head(a.group("place")) in (prev or "").lower():
                    return "%s is still %s the %s" % (a.group("who"), a.group("prep"), a.group("place"))
                return a.group(0)
            new = _REESTABLISH.sub(_re, new)
            # "steps back from the stall and" -> "leans back a little, still at the stall,
            # and": the body stays where the last window left it.
            new = _STEP_AWAY.sub(lambda a: "leans back a little, still at the %s, and" % a.group("place"),
                                 new)
        if new != (m or ""):
            changes.append((k, m, new))
        out.append(new)
        prev = (prev + " " + new)
    return out, changes


def cut_lint_report(shots, sound=None):
    """Lines for the log: per window, what cut_triggers flags in the plan's moments,
    what smooth_moments rewrote, and what is still flagged after the rewrite."""
    if not PREVIS_CUT_LINT:
        return []
    ms = moments(shots, PREVIS_SOUND if sound is None else sound)
    before = cut_triggers(ms)
    after_ms, changes = smooth_moments(ms)
    after = cut_triggers(after_ms)
    done = {k for k, _, _ in changes}
    lines = []
    for k, fl in enumerate(before):
        for kind, phrase, note in fl:
            fixed = not any(p == phrase for _, p, _ in after[k])
            lines.append("previs window %d: %s cut trigger %r (%s)%s"
                         % (k + 1, kind, phrase, note,
                            " -- rewritten" if (fixed and k in done) else " -- check the previs here"))
    for k, b, a in changes:
        lines.append("previs window %d rewritten for one take: %r -> %r" % (k + 1, b, a))
    return lines


def compile_previs(shots, hop_frames, fps=FPS, sound=None):
    """The previs's timed description: ONE shot written as back-to-back motion clocks.

    Previs law (2026-10-14). In H3's prompt format a bare `At MM:SS.mmm` is a camera
    CUT ("that time is the edit"); a range `At MM:SS.mmm-MM:SS.mmm,` clocks motion
    inside the current shot. The old one-stamp-per-window text cut 2-9 frames before
    every window on every seed (f66/f131/f195 against stamps f68/f136/f204), so it
    is written as ranges now: contiguous, from 00:00.000 to the previs's last frame,
    no gap, no overlap, one shot, no camera cut.

    Each window is the HAND-OFF between two hops, so it must show the previous beat
    still carrying on, never the next beat starting (window 2 used to be "takes a
    skewer", so hop 2 always ended holding one). So beat k's range starts right
    AFTER window k (window start + WINDOW) and runs until window k+1, whose own
    range is beat k carrying on (`carry_text`, built from the beat's subject, no new
    action, and no settle/hold: "settles ... and holds it" froze her into a blank
    stare at hop 1's tail, user 22:05); the closing window carries the last beat on.
    Beat 0 starts at frame 0.

    A beat whose moment carries a spoken line is the one exception: the line
    crosses its window (split_crossing gives both hops their half of it), so that
    beat starts up to LEAD_MAX_S (half the line) BEFORE its window, the previous
    beat's range ends there, and that window has no carry range.

    `sound` defaults to PREVIS_SOUND and appends each beat's sound sentence to its
    own moment. Beat text is used as written (`moments`); nothing is reworded.
    """
    lay = layout(hop_frames)
    ms = moments(shots, PREVIS_SOUND if sound is None else sound)
    if PREVIS_CUT_LINT:
        # The moments rewritten for one take where that is safe (smooth_moments);
        # what is left is logged by run() through cut_lint_report.
        ms = smooth_moments(ms)[0]
    win = lay["windows"]
    end = int(lay["previs"])
    segs = []          # (start frame, text), in order
    last = None        # the latest beat written
    for k, (w, m) in enumerate(zip(win, ms)):
        if not m:
            continue
        sp = _spoken_s(m)
        if not segs:
            start = 0
        elif sp:
            # The line crosses window k: start it ahead of the window by half the line.
            start = max(segs[-1][0] + 1, int(w) - int(round(_lead_s(sp) * fps)))
        elif int(w) + WINDOW >= end:
            # A full section's last window is the previs's last WINDOW frames: no
            # room after it, so the last beat is written over its own window.
            start = int(w)
        else:
            segs.append((int(w), carry_text(last)))
            start = int(w) + WINDOW
        segs.append((start, m))
        last = m
    if last is not None and len(win) > len(ms) and int(win[len(ms)]) > segs[-1][0]:
        # The closing window: the last beat carries on through it.
        segs.append((int(win[len(ms)]), carry_text(last)))
    rows = []
    for n, (a, txt) in enumerate(segs):
        b = segs[n + 1][0] if n + 1 < len(segs) else end
        if b <= a:
            continue
        rows.append("At %s-%s, %s" % (_stamp(a / float(fps)), _stamp(b / float(fps)), txt))
    return " ".join(rows), lay


def carry_text(moment):
    """A window's range: the beat before it carrying on, alive, with no new action of
    its own. Never settle/hold/still/pause/stop/stare: those froze the hop's tail
    into a blank stare at camera (2026-10-14, user 22:05)."""
    who = _WHO.search(moment or "")
    return ("%s carries on naturally with that motion, moving and relaxed, "
            "in the same continuous shot." % (who.group(0) if who else "The subject"))


def chain_previs_text(text, shots, lay, fps=FPS):
    """Clock a chained section's opening 22-frame window (`00:00.000-00:00.917`) with `carry_text`.

    In a chained previs (`section > 0`), Window 0 (`f0..f21`) is pinned to the
    previous section's closing window (`W_end`). Starting Beat 0 at `00:00.000`
    asked H3 to begin a new beat inside the pinned 22-frame handoff window and
    hard-cut at `f22` (`00:00.917`) the moment the head pin ended (`chain_00127_`).
    Holding `carry_text` across `00:00.000-00:00.917` and starting Beat 0 at
    `00:00.917` (`f22`) treats Window 0 identically to Windows 1..N.
    """
    s = str(text or "")
    if not s or not lay:
        return s
    ms = moments(shots, sound=False)
    if ms and _spoken_s(ms[0]) > 0:
        return s
    w0_end = _stamp(WINDOW / float(fps))
    m = re.match(r"^At\s+00:00\.000-(?P<end>\d\d:\d\d\.\d{3}),\s*(?P<rest>.*)$", s, re.S)
    if not m or m.group("end") <= w0_end:
        return s
    carry = carry_text(ms[0] if ms else s)
    return ("Continuing the same unbroken shot in the same location and lighting: "
            "At 00:00.000-%s, %s At %s-%s, %s" % (
                w0_end, carry, w0_end, m.group("end"), m.group("rest")
            ))


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


# --- the sound first -----------------------------------------------------------

_WORD = re.compile(r"[a-z0-9']+")


def _words(text):
    return [w.strip("'") for w in _WORD.findall(str(text or "").lower()) if w.strip("'")]


def hop_words(block):
    """The words a hop's assembled block asks it to say, in order. A line cut by
    `<|cutoff|>` counts up to the cut."""
    out = []
    for m in _D.findall(str(block or "")):
        out += _words(_TAG.sub("", m.replace(CUTOFF, " ")))
    return out


def hop_language(block):
    """Whisper's language code for the block's first line: "en" for `[English]`
    or an untagged line, None (detect) for any other tag."""
    m = _D.search(str(block or ""))
    tag = _TAG.match(m.group(1)) if m else None
    if not tag:
        return "en"
    return "en" if "english" in tag.group(0).lower() else None


def judge_sound(planned, heard):
    """(ok, score, note) for a draft's transcript against the hop's line.

    A hop with a line must say at least SOUND_RECALL of its words and invent few
    others; a quiet hop may say QUIET_WORDS. Order-free on purpose: Whisper splits
    and joins words its own way, and a wrong word order is not what fails a film.
    """
    want, got = list(planned or []), _words(heard)
    if not want:
        ok = len(got) <= QUIET_WORDS
        return ok, -float(len(got)), ("quiet" if not got else "quiet hop says %d word(s)" % len(got))
    ws = set(want)
    recall = len(ws & set(got)) / float(len(ws))
    extra = sum(1 for w in got if w not in ws)
    ok = recall >= SOUND_RECALL and extra <= max(3, len(want) // 2)
    return ok, recall - 0.05 * extra, "%d%% of the line, %d other word(s)" % (round(100 * recall), extra)


# --- where a hop's own line sits ----------------------------------------------
#
# A hand-tie hop opens on its head window (the previs's sound, 0.92 s) and the
# film drops its last WINDOW frames (the next hop's head replaces them). Its own
# line has the middle only: on chain_00153 (12 x 8 s, native 9:16) every line was
# spoken whole by its hop, and every error sat at a join -- endings in the dropped
# tail ("good vibes" -> "goodbye", "the train" -> "the cactus"), openings under the
# window's sound. Measured rate: 14-17 words in 7.0-7.8 s, about 2.1 a second.

LINE_START_S = 1.0          # the head window is 0.917 s; the line starts after it

# --- ht_seam_fix (recover, 2026-10-14) ------------------------------------------
# One RUN switch, ht_seam_fix=on, turns all of these on together (see CLAUDE.md).
SEAMFIX_ANCHOR_STRENGTH = 0.75  # share of a hop's per-(channel, 5-latent position) mean
                                # offset from hop 1 that is taken out before decode
SEAMFIX_EARLY_SLACK_S = 0.05    # a new line's first word may start this far inside the overlap
SEAMFIX_EARLY_PENALTY = 0.5     # score taken off an early draft, so a late one wins if all fail
# --- ht_seam_fix v2 (recover, 2026-10-14, render seamAB_prev_hop_00003) ----------
# A hop's last WINDOW frames always render brighter (~10 /255) and griddy. Under seam
# fix every hop that has a next hop renders SEAMFIX_TAIL_EXTRA frames more and drops
# them (latent, picture and sound), so what is delivered and handed to the next hop
# ends on clean frames. 24 = 6 latent steps: >= WINDOW and keeps length 1 + 4k.
SEAMFIX_TAIL_EXTRA = 34  # two whole 17-frame/5-step blocks; any other count shifts the latent phase
# v3: back to LINE_START_S (1.0). 1.3 only served the v2 draft lock; v1 synced at 1.0.
SEAMFIX_LINE_START_S = LINE_START_S
# v3: v2's picture-only Motion-Context (audio keyframe stripped) and frozen draft head
# are off: they took the carried sound cue away from hops 2+ (no lip sync on hop 2).
SEAMFIX_PICTURE_ONLY = False
SEAMFIX_DRAFT_LOCK = False
# Sound carry (2026-10-14): on a prev_hop seam, hop k+1 takes ONLY hop k's sound
# guide from Motion-Context (its picture keyframes are stripped) and opens on its own
# previs window, picture only. Speech carries across the join; no rendered picture
# does, here or in a chained section's window 0 or the sound draft's opening.
SEAM_SOUND_ONLY = True
# End tie (2026-10-14, v6): every prev_hop seam hop that has a next window ties its
# tail to window k+1 at -(WINDOW + SEAMFIX_TAIL_EXTRA), as hop 1 does. ee2b299 dropped
# it (background melt on the stall / awning); without it hops 2-3 ended free and the
# next hop's head window snapped the pose at joins 2/3 (seam bends -10..-13 /255 vs
# -1.4 at the tied join 1). False restores the head-only behaviour.
SEAMFIX_END_TIE = True
# Gap order (GAP_ORDER, gap_render_order, gap_role) was REMOVED 2026-10-14 (user).
# Save each hop's guide windows, exactly as AddGuide encodes them (17k+5 crop,
# lanczos to the hop canvas), as PNGs under output/HANDTIECLIPS_windows/<run>/.
# Off by default so testing does not accumulate multi-GB PNG folders on disk.
SAVE_WINDOWS = False
# v4 (2026-10-14): the per-hop corrections are off. With the previs and the
# Motion-Context carry the hop already opens on the right level; v3 measured the
# head tone match growing -2 -> -6 -> -10 /255 hop over hop (a constant darken on
# a brighter, contrastier raw hop = the wet sheen), and the flash is a rise over
# the incoming hop's first 2-4 frames, which a whole-hop offset cannot remove.
SEAMFIX_TONE_MATCH = False   # prev-hop head tone match on prev_hop seam hops
SEAMFIX_ANCHOR = False       # latent mean anchor to hop 1
SEAMFIX_ANCHOR_STRENGTH_V2 = 0.3  # was 0.75; and never on the carried overlap latents
SEAMFIX_ANCHOR_SKIP = 6         # latent steps of the 22-frame overlap (frames 0-20)
SEAMFIX_ANCHOR_RAMP = 2         # latent steps over which the anchor fades in after it


def early_words(planned, words, before_s, slack_s=SEAMFIX_EARLY_SLACK_S):
    """The heard words of the NEW line that start before `before_s` (the overlap's
    end). Words not in the planned line are treated as carried-over speech from
    the previous hop and are allowed there. `words` is hear.transcribe_words'
    [(word, start_s, end_s)]. -> [(word, start_s)]"""
    want = set(planned or [])
    out = []
    for w, t0, _t1 in words or []:
        for ww in _words(w):
            if ww in want and float(t0) < float(before_s) - float(slack_s):
                out.append((ww, float(t0)))
    return out


def invented_words(words, after_s):
    """ht_seam_fix: heard words that start at or after `after_s` (the carried
    overlap's end). For a hop with no line any of them is invented speech
    (seamAB_prev_hop_00003 hop 4: "Thanks for watching!"). -> [(word, start_s)]"""
    out = []
    for w, t0, _t1 in words or []:
        if float(t0) >= float(after_s) and _words(w):
            out.append((str(w).strip(), float(t0)))
    return out
# H3's speaking rate is not one number: chain_00153 spoke 14-17 words at about 2.1 a
# second, chain_00155 spoke 9-10 at about 3, starting 1.2-1.7 s in. A 12-word cap off
# the slow rate left 2.3-3.6 s of a vlogger's face with nothing said, and the model
# filled it with mouthing. The cap sits between the two; the close is timed off the
# fast one, so the closed-mouth sentence lands where a short line actually ends.
LINE_WPS = 2.4              # words a second the cap assumes: 14 words at 8 s
LINE_WPS_FAST = 3.0         # chain_00155: when the line is predicted to end
CLOSE_MARGIN_S = 0.5        # the close lands at least this far before the dropped tail
_TIMED = re.compile(r"\bAt \d\d:\d\d(?:\.\d+)?\b")


def line_budget(hop_frames, fps=FPS):
    """Spoken words that fit between a hop's head window and its dropped tail."""
    usable = int(hop_frames) / float(fps) - LINE_START_S - WINDOW / float(fps)
    return max(0, int(usable * LINE_WPS))


def _close_s(beat, hop_frames, fps=FPS, start_s=LINE_START_S):
    """When the sentence after a hop's last line should start: the predicted end of
    the line at LINE_WPS_FAST for short phrases (<= 5 words) or 2.0 wps for full
    sentences, never later than CLOSE_MARGIN_S before the tail."""
    words = sum(len(_TAG.sub("", m).split()) for m in _D.findall(beat))
    last = (int(hop_frames) - WINDOW) / float(fps) - CLOSE_MARGIN_S
    wps = LINE_WPS_FAST if words <= 5 else 2.0
    return round(max(start_s + 0.5, min(start_s + words / wps, last)), 1)


def time_lines(shots, start_s=LINE_START_S, hop_frames=None, fps=FPS):
    """Shots whose first spoken line is placed after the head window.

    Official H3 syntax, `At MM:SS.mmm` inside the description: the sentence that
    carries the beat's first `<d>` gets `At 00:01.000`, and the beat's opening
    gets `At 00:00.000` so the stamps read in order. With `hop_frames`, the
    sentence after the last line is stamped too (`_close_s`): a closed-mouth
    action with no time had nothing to hold it, and chain_00155 mouthed through
    the 2-3.5 s after every line. A beat that already writes its own times, a
    full H3 block, or a beat with no line is left as it is. Returns copies.
    """
    out = []
    for k, sh in enumerate(shots or []):
        s = dict(sh or {})
        beat = str(s.get("beat") or "")
        at = beat.find("<d>")
        if at < 0 or _TIMED.search(beat) or "detailed_description:" in beat \
                or "subject_definitions:" in beat:
            out.append(s)
            continue
        head = beat[:at]
        cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
        stamp = "At %s " % _stamp(start_s)
        if cut < 0:
            s["beat"] = stamp + beat.lstrip()
        else:
            s["beat"] = ("At %s %s %s%s" % (_stamp(0.0), beat[:cut + 1].strip(), stamp,
                                            beat[cut + 2:].lstrip()))
        if hop_frames is not None and k < len(hop_frames):
            b = s["beat"]
            end = b.rfind("</d>")
            tail = b[end + 4:] if end >= 0 else ""
            lead = len(tail) - len(tail.lstrip(" .,!?"))
            if end >= 0 and tail[lead:].strip():
                at = end + 4 + lead
                rest = b[at:]
                if rest and rest[0].islower():
                    rest = rest[0].upper() + rest[1:]
                s["beat"] = (b[:at].rstrip() + " At %s " % _stamp(_close_s(beat, hop_frames[k], fps, start_s))
                             + rest)
        out.append(s)
    return out


# A mouth that eats cannot say a line. The previs squeezes a beat into ~3 s, so it
# reaches the bite early, and the hop follows its picture: the line then plays over
# the eating (chain_00153, 00160) or through the chewing (00154) -- three films of
# six, the same beat each time. Every other dialogue hop passed.
_MOUTH = re.compile(r"\b(eat|eats|eating|bite|bites|biting|chew|chews|chewing|sip|sips|sipping|"
                    r"drink|drinks|drinking|swallow|swallows|swallowing|lick|licks|licking|"
                    r"taste|tastes|tasting)\b", re.I)


def line_lints(shots, hop_frames, fps=FPS):
    """Warnings for a hop whose lines will not fit between its windows, or that
    asks one mouth to speak and to eat."""
    warn = []
    for k, sh in enumerate(shots or []):
        beat = str((sh or {}).get("beat") or "")
        tr = _trailing_sound(beat)
        gone = quiet_sound(tr[1])[1] if tr else []
        if gone:
            warn.append("hand-tie: shot %d's sound names a voice (%s). With no words to say, "
                        "H3 makes them up, so the node leaves it out. Name sounds that are "
                        "not voices: footsteps, traffic, a sizzling grill."
                        % (k + 1, "; ".join(gone)))
        if _D.search(beat):
            m = _MOUTH.search(_D.sub(" ", beat))
            if m:
                warn.append("hand-tie: shot %d speaks a line and %s in the same hop. The "
                            "picture reaches the %s early and the line plays over it. Give "
                            "the %s its own hop (closed lips, its sound named), and say the "
                            "line in the hop before." % (k + 1, m.group(0).lower(),
                                                         m.group(0).lower(), m.group(0).lower()))
        words = sum(len(_TAG.sub("", m).split()) for m in _D.findall(beat))
        room = line_budget(hop_frames[k], fps)
        if words > room:
            warn.append("hand-tie: shot %d speaks %d words; about %d fit between its windows "
                        "(%.1f s at %.1f words a second). The rest runs into the join, where "
                        "the film drops it. Cut the line or give the shot a longer duration. "
                        "After the line, give the shot an action and a sound: a hop that "
                        "runs out of words fills the rest with mouthing."
                        % (k + 1, words, room, room / LINE_WPS, LINE_WPS))
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
    # Channels only: the means are over every pixel, so a window cut from a smaller
    # previs (PREVIS_RESOLUTION) is compared with the hop as it is.
    for name, w in (("head", head), ("tail", tail)):
        if w is not None and int(w.shape[-1]) != int(images.shape[-1]):
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


BODY_GRADE_S = 1.0       # sample the two bodies this far off the cut, not the contact frame


def still_color(a, b, still=SEAM_STILL):
    """Median color of pixels that hold still between two [H,W,C] frames.

    None when too few hold still for the median to be a grade rather than motion.
    """
    mask = ((a.float() - b.float()).abs().amax(-1) < still)
    if float(mask.float().mean()) < SEAM_MIN_STILL:
        return None
    return a.float()[mask].median(dim=0).values


def _span_color(frames, still=SEAM_STILL):
    """Median of the per-pair still colors across a span. None when none qualify."""
    import torch  # noqa: PLC0415
    cols = []
    for t in range(int(frames.shape[0]) - 1):
        c = still_color(frames[t], frames[t + 1], still)
        if c is not None:
            cols.append(c)
    if not cols:
        return None
    return torch.stack(cols, dim=0).median(dim=0).values


def body_grade(prev, incoming, overlap, fps=FPS):
    """Grade the incoming hop onto the previous hop's body, a second off the cut.

    Not called from run(). A full-frame shift of this size recoloured every hop
    on seamAB_prev_hop_00029 (previs only): the body mean climbed about
    R119, R137, R182, R189 across four hops, because the sample a second
    before the cut sits on the hot end and that colour was added to the next
    scene. Kept here so the measurement can be rerun. Do not wire it back as a
    whole-hop add.


    prev is the previous hop as it sits in the master, tail window included: the
    last `overlap` frames are the shared window and are about to be overwritten.
    A second before the cut is therefore the previous hop's own body, not that
    window. A second after the cut is the incoming hop's body, just past its head
    window. The cut frame and the previs window are not the samples.

    The whole incoming hop takes the still-pixel difference of those two bodies,
    one constant per channel. The head window then takes whatever extra its own
    still-pixel color still needs, so the span between the two samples stops
    carrying the previs window's grade. The picture is kept; only the grade moves.
    prev is not modified, and seam_bend must not run on this join: it would drag
    the second-before sample toward the contact frame.

    -> (graded, hop_shift, head_shift). graded is incoming unchanged, and the
    shifts are None, when a still-pixel sample cannot be trusted.
    """
    gap = int(round(float(fps) * BODY_GRADE_S))
    overlap = int(overlap)
    cut = int(prev.shape[0]) - overlap
    i_prev = cut - gap
    if i_prev < 1 or gap + 1 >= int(incoming.shape[0]) or overlap < 2:
        return incoming, None, None
    prev_c = still_color(prev[i_prev], prev[i_prev + 1])
    next_c = still_color(incoming[gap], incoming[gap + 1])
    if prev_c is None or next_c is None:
        return incoming, None, None
    hop_shift = (prev_c - next_c).to(device=incoming.device, dtype=incoming.dtype)
    out = (incoming.float() + hop_shift.float().view(1, 1, 1, -1)).clamp(0.0, 1.0)
    head_c = _span_color(out[:overlap])
    head_shift = None
    if head_c is not None:
        head_shift = (prev_c.to(head_c.device) - head_c).to(dtype=incoming.dtype)
        out[:overlap] = (out[:overlap] + head_shift.float().view(1, 1, 1, -1)).clamp(0.0, 1.0)
    return out.to(dtype=incoming.dtype), hop_shift, head_shift


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
        mask = (((a - lo).abs().amax(-1) < still)
                & ((hi - b).abs().amax(-1) < still)
                & ((b - a).abs().amax(-1) < still * 1.5))
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


CUT_JUMP = 1.4           # a cut's one-frame change beats CUT_SPAN frames of motion either side
CUT_FLOOR = 15.0 / 255   # ... and is at least this big: a jump in a still shot is no cut.
                         # 15, not 20: chain_00156's snap at f22 moved 18/255 at 3.5x
                         # its neighbours; clean frames over CUT_JUMP move 14 at most
CUT_SPAN = 3             # the motion yardstick: frames either side
CUT_CELLS = 12           # coarse cells across the frame's short side: layout, not texture.
                         # A count, not a pixel size: 32 px cells caught 00156's snap at
                         # 512p and missed 00164's hop-7 cut at native (1.34x; 1.49x here)
# PREVIS_TRIES removed 2026-10-14: the previs is one take, never re-rolled.
PREVIS_RETRY_STRIDE = 104729   # the HOP retake's seed step (h3_ref_chain hop cut check); not the previs


# same_cut / CUT_SAME_FRAMES removed 2026-10-14 with the previs retake.


# There is no "the text's cut" any more (moment_cuts, removed 2026-10-13). Every
# one of 7 takes across chain_00159, 00160 and the 10-hop run cut within 10 frames
# of a window start, so those cuts were kept as asked for by the text. They were:
# the previs was written as a shot list (see compile_previs). A cut at a window
# leaves the hop before it pinned to two different shots, and that hop cuts or
# morphs to reach the second (Carolyn's hop 1 at f171 on both seeds, the shifting
# background). The previs is now one take, so such a cut re-rolls like any other;
# Since 2026-10-14 the previs is one take: cuts are only logged.


def window_cuts(cuts, windows):
    """The cuts that land INSIDE a window: the worse kind, used to rank takes.

    Inside a window a cut is pinned into two hops' guides. Between two windows a hop
    carries it as a camera move about half the time (00150's hop 5, 00154's hop 3)
    and copies it the other half (00154's hop 1, from the previs's f66), so every cut
    earns a new take; this only picks the least bad take when all of them cut.
    `cuts` are first-new-frame indices, so a cut AT a window's first frame leaves
    that window whole.
    """
    return [c for c in cuts if any(w < c < w + WINDOW for w in windows)]


def previs_cuts(frames):
    """Frames where the previs hard-cuts to another shot: [index of the first new frame].

    The previs is the single point of failure: every hop follows it, and a cut in it
    becomes a cut in the film (chain_00135 split two kitchens; chain_00146 jumped
    from a medium to a wide at 13.08 s). Nobody runs a previs-only dry run first, so
    the node checks its own.

    A cut is a ONE-frame jump in the coarse layout bigger than CUT_SPAN frames of
    change on either side of it, by CUT_JUMP, and at least CUT_FLOOR. Motion, however
    fast, accumulates: three frames of a walk move the layout more than one does. A
    cut does not. The old test (a frame's change against the median of its
    neighbours') read a handheld walk's every frame as a big change and missed
    chain_00154's previs cut at f66, road to shopfronts, at 3.6x against its 6x;
    hop 1 copied it into the film. Calibrated on 23 clips: cuts 1.81-5.22 at
    29-45/255 (00146 f314, 00154 previs f66 / f192, its hop 1 f123), and 00156's
    snap 3.5x at 18/255; clean films (00141-00153: walking, rain, dancing, 12
    native hops) at most 1.64, and every clean frame over 1.0 moves 14/255 or less.
    """
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415
    n = int(frames.shape[0])
    if n < 2 * CUT_SPAN + 2:
        return []
    h, w = int(frames.shape[1]), int(frames.shape[2])
    cell = max(1, min(h, w) // CUT_CELLS)
    grid = (max(1, h // cell), max(1, w // cell))
    luma = torch.cat([F.adaptive_avg_pool2d(frames[i:i + 32].float().mean(-1)[:, None], grid)[:, 0]
                      for i in range(0, n, 32)])           # chunked: a native previs is 4 GB
    d = lambda a, b: float((luma[a] - luma[b]).abs().mean())  # noqa: E731
    out = []
    for t in range(CUT_SPAN + 1, n - CUT_SPAN):
        j = d(t - 1, t)
        if j >= CUT_FLOOR and j >= CUT_JUMP * max(d(t - 1 - CUT_SPAN, t - 1), d(t, t + CUT_SPAN),
                                                  0.5 / 255):
            out.append(t)
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


def calm_hop_audio(wav, sr, has_line=True, head_speaks=False, tail_speaks=False,
                   overlap_n=WINDOW, fps=FPS):
    """Suppress guide-window swoosh SFX, closed-mouth head speech leaks, and
    hallucinated speech gibberish on non-speaking hand-tie hops.

    1. Extracts a vocal-formant-filtered (~1000 Hz low-passed) ambient floor bed
       from the hop's quietest non-zero span (`target_rms` in `[0.008, 0.015]`).
    2. On a non-speaking hop (`not has_line and not head_speaks and not tail_speaks`),
       replaces any speech-level bursts (`> 1.5x target_rms`, such as H3's invented
       background chatter on silent shots) or dead-zero dropouts with the clean
       ambient bed so silent hops match the background floor of speaking hops.
    3. On a speaking hop (`has_line=True`), holds the muted 22-frame head window
       (`0.0s..0.80s`, where the mouth is pinned closed before `At 00:01.000`) and
       any spiky/dropout muted tail window (`-0.85s..end`) to the clean ambient bed
       via a 60 ms cosine crossfade, eliminating seam swoosh/zoom SFX while leaving
       the spoken body (`0.80s .. -0.85s`) 100% untouched.
    """
    import torch  # noqa: PLC0415
    import torch.nn.functional as F  # noqa: PLC0415
    if wav is None or not isinstance(wav, torch.Tensor) or int(wav.shape[-1]) < int(sr):
        return wav
    orig_shape = wav.shape
    w2 = wav.float().reshape(-1, orig_shape[-1]).clone()
    _c, total_s = w2.shape
    win_n = max(64, int(round(0.050 * sr)))
    hop_n = max(32, win_n // 2)
    if total_s < win_n * 6:
        return wav
    n_wins = (total_s - win_n) // hop_n + 1
    rms_t = torch.tensor(
        [float(w2[:, i * hop_n:i * hop_n + win_n].pow(2).mean().sqrt().item())
         for i in range(n_wins)],
        dtype=torch.float32, device=w2.device,
    )
    quiet_mask = (rms_t > 0.002) & (rms_t < 0.040)
    if int(quiet_mask.sum().item()) >= 3:
        target_rms = float(torch.median(rms_t[quiet_mask]).item())
    else:
        nz = rms_t[rms_t > 0.001]
        target_rms = float(torch.quantile(nz, 0.20).item()) if int(nz.numel()) >= 3 else 0.010
    target_rms = max(0.008, min(0.015, target_rms))

    span_w = min(max(4, int(round(0.40 / 0.025))), n_wins)
    best_i, best_r = 0, 1e9
    for i in range(0, max(1, n_wins - span_w + 1)):
        r = float(rms_t[i:i + span_w].mean().item())
        if 0.001 < r < best_r:
            best_r = r
            best_i = i
    amb_start = best_i * hop_n
    amb_len = min(total_s - amb_start, max(win_n * 4, int(round(0.50 * sr))))
    amb_tile = w2[:, amb_start:amb_start + amb_len].clone()
    k_lp = max(3, int(round(sr / 1100.0)) | 1)
    pad_lp = k_lp // 2
    amb_lp = F.avg_pool1d(
        F.pad(amb_tile.unsqueeze(1), (pad_lp, pad_lp), mode="reflect"),
        kernel_size=k_lp, stride=1,
    ).squeeze(1)
    amb_rms = float(amb_lp.pow(2).mean().sqrt().item())
    if amb_rms > 1e-4:
        amb_lp = amb_lp * (target_rms / amb_rms)
    else:
        g = torch.Generator(device=w2.device)
        g.manual_seed(1337)
        noise = torch.randn(amb_tile.shape, generator=g, device=w2.device, dtype=w2.dtype)
        amb_lp = F.avg_pool1d(
            F.pad(noise.unsqueeze(1), (pad_lp, pad_lp), mode="reflect"),
            kernel_size=k_lp, stride=1,
        ).squeeze(1)
        n_rms = float(amb_lp.pow(2).mean().sqrt().item())
        amb_lp = amb_lp * (target_rms / max(n_rms, 1e-6))

    bed = torch.zeros_like(w2)
    xf = min(win_n, max(16, amb_len // 4))
    step_len = max(16, amb_len - xf)
    pos = 0
    while pos < total_s:
        take = min(amb_len, total_s - pos)
        chunk = amb_lp[:, :take].clone()
        if pos > 0 and xf > 0 and take > xf:
            t = torch.linspace(0.0, 1.0, xf, device=w2.device).unsqueeze(0)
            bed[:, pos:pos + xf] = (
                bed[:, pos:pos + xf] * torch.cos(t * 1.5707963)
                + chunk[:, :xf] * torch.sin(t * 1.5707963)
            )
            bed[:, pos + xf:pos + take] = chunk[:, xf:take]
        else:
            bed[:, pos:pos + take] = chunk
        pos += step_len

    if not has_line and not head_speaks and not tail_speaks:
        env = torch.ones((total_s,), dtype=torch.float32, device=w2.device)
        win_edge = min(total_s // 4, int(round((overlap_n / float(fps)) * sr)))
        if win_edge > 0:
            env[:win_edge] = 0.0
            env[total_s - win_edge:] = 0.0
        for i in range(n_wins):
            r = float(rms_t[i].item())
            if r > target_rms * 1.5 or r < 0.004:
                env[i * hop_n:min(total_s, i * hop_n + win_n)] = 0.0
        sm = max(3, int(round(0.060 * sr)) | 1)
        env = F.avg_pool1d(
            F.pad(env.view(1, 1, -1), (sm // 2, sm // 2), mode="replicate"),
            kernel_size=sm, stride=1,
        ).view(1, -1)
        w2_soft = F.avg_pool1d(
            F.pad(w2.unsqueeze(1), (pad_lp, pad_lp), mode="reflect"),
            kernel_size=k_lp, stride=1,
        ).squeeze(1)
        w2 = w2_soft * env + bed * (1.0 - env)
        return w2.reshape(orig_shape).to(dtype=wav.dtype)

    fade_n = max(16, int(round(0.060 * sr)))
    if not head_speaks:
        head_n = min(total_s // 4, int(round((overlap_n / float(fps)) * 0.86 * sr)))
        if head_n > fade_n:
            t = torch.linspace(0.0, 1.0, fade_n, device=w2.device).unsqueeze(0)
            w2[:, :head_n - fade_n] = bed[:, :head_n - fade_n]
            w2[:, head_n - fade_n:head_n] = (
                bed[:, head_n - fade_n:head_n] * torch.cos(t * 1.5707963)
                + w2[:, head_n - fade_n:head_n] * torch.sin(t * 1.5707963)
            )
    if not tail_speaks:
        fade_tail = max(16, int(round(0.100 * sr)))
        tail_n = min(total_s // 3, int(round((overlap_n / float(fps) + 0.30) * sr)))
        if tail_n > fade_tail:
            t = torch.linspace(0.0, 1.0, fade_tail, device=w2.device).unsqueeze(0)
            w2[:, total_s - tail_n:total_s - tail_n + fade_tail] = (
                w2[:, total_s - tail_n:total_s - tail_n + fade_tail] * torch.cos(t * 1.5707963)
                + bed[:, total_s - tail_n:total_s - tail_n + fade_tail] * torch.sin(t * 1.5707963)
            )
            w2[:, total_s - tail_n + fade_tail:] = bed[:, total_s - tail_n + fade_tail:]
    return w2.reshape(orig_shape).to(dtype=wav.dtype)


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
    if shots and _anchor(shots[0]) == "extend":
        err.append("hand-tie: shot 1 has anchor=extend, but there is no hop before it to "
                   "continue. Remove the anchor from shot 1.")
    return err


PREVIS_MIN_SHORT = 320   # px: below this the previs is too small to hold a face's layout


def previs_canvas(width, height, hop_frames, previs_frames, cap=None):
    """(w, h) the previs renders at: never more pixels x frames than one hop.

    A previs is up to PREVIS_MAX frames (15 s); at the hop canvas that is more
    than most cards can render (user 2026-10-13: most of the community can barely
    do 10 s). So its canvas is the hop's, shrunk until canvas x previs frames fits
    in canvas x the section's longest hop -- whoever can render a hop can render
    its previs -- and never above `cap` (the PREVIS_RESOLUTION tier). Same aspect,
    on the 32 px grid, no short edge under PREVIS_MIN_SHORT.
    """
    w, h = int(width), int(height)
    budget = w * h * max(int(f) for f in hop_frames) / float(max(1, int(previs_frames)))
    if cap is not None and int(cap[0]) * int(cap[1]) < w * h:
        budget = min(budget, float(int(cap[0]) * int(cap[1])))
    if w * h <= budget:
        return w, h
    s = (budget / float(w * h)) ** 0.5
    nw, nh = max(32, int(w * s) // 32 * 32), max(32, int(h * s) // 32 * 32)
    short = min(nw, nh)
    if short < PREVIS_MIN_SHORT:
        f = PREVIS_MIN_SHORT / float(short)
        nw, nh = -(-int(nw * f) // 32) * 32, -(-int(nh * f) // 32) * 32
    return min(nw, w), min(nh, h)


def free_head(k, chained, start_image=False):
    """Whether hop k of a section opens untied: the first hop of a section that
    starts fresh (hop 1, a restart) with no start image.

    Such a hop has nothing before it to join, so its head window tied it to the
    previs's cold first frames for no continuity at all. Hop 1 is where the
    closed-mouth line kept landing (chain_00164, 00165, the Carolyn run) and
    where it cut at f171 on both seeds to reach window 2. It is tied at its end
    only. A chained section's first hop continues the film and keeps its head;
    a start image is the head window's picture, so that one does too.
    """
    return int(k) == 0 and not chained and not start_image


def hop_key_fields(head_digest, tail_digest, head_audio_digest=None, tail_audio_digest=None):
    """A hand-tie hop's key carries its two windows instead of prev_key.

    The hop reads nothing from the hop before it, so editing an earlier shot's beat
    leaves it cached; editing a moment changes the previs, hence both digests.
    """
    out = {"pin": "hand_tie", "head": str(head_digest), "tail": str(tail_digest)}
    # tail None: a section's last hop, tied at its start only.
    if head_audio_digest is not None or tail_audio_digest is not None:
        out["head_audio"] = str(head_audio_digest)
        out["tail_audio"] = str(tail_audio_digest)
    return out


# --- Qwen3-VL Previs Compiler & Seam Feather (2026-10-16) -----------------------

PREVIS_LAWS_SYSTEM = (
    "You compile N shot beats into N continuous single-take previs bridge moments "
    "for a MiniMax H3 video generator.\n"
    "STRICT LAWS:\n"
    "1. CONTINUOUS BRIDGE POSTURE ONLY (NO EARLY PAYOFFS OR NEW PROPS): Each previs "
    "moment is only a ~2.5s blocking/camera bridge. Keep ONLY where the subject is "
    "and how their body and camera move continuously. Remove ALL discrete mid-shot "
    "payoff actions (eating, biting, chewing, licking, drinking, picking up or "
    "grabbing objects, raising food/drinks) AND remove any newly acquired hand props "
    "(skewers, cones, cups, food) that were not held in Beat 1, so props are never "
    "spawned early in the previs.\n"
    "2. CONTINUOUS FORWARD FLOW (NO STATIONARY TELEPORTS OR BACKGROUND CLUTTER): "
    "Never write camera cuts ('cut to', 'wide shot'), time jumps ('later'), or "
    "abruptly switch from walking to 'stands at' / 'waits at' a new landmark (write "
    "'walks up to' or 'walks past' instead). Drop secondary background people "
    "('people gathering behind her', 'so he is behind her', 'vendor') so the "
    "background never warps or locks a crowd into a 22-frame handoff window.\n"
    "3. SILENT MOUTHS & CLEAN PUNCTUATION: Remove all spoken dialogue (<d>...</d>), "
    "speech verbs ('says', 'talking', 'chatting'), and crowd voices ('chatter', "
    "'calling out'). Never use ellipses ('...' or '…') or dashes ('—' / '--'); use "
    "only commas (',') and periods ('.').\n"
    "4. EXACT FORMAT: Output ONLY numbered lines `1: <one short sentence>`, "
    "`2: <one short sentence>`, ... up to N. Keep every `@tag` or `<Subject N>` "
    "token exactly as written in the input."
)

_COMPOUND_TAIL = re.compile(
    r"(?:,\s*then\b|\s+then\b|,\s*before\b|\s+before\b|"
    r",?\s+and\s+(?:takes?|bites?|chews?|eats?|licks?|sips?|drinks?|swallows?|grabs?|"
    r"picks?\s+up|opens?|closes?|pulls?\s+out|raises?|lifts?|waves?)\b).*$",
    re.I,
)

# Clauses attaching a newly acquired mid-chain prop (", holding the skewer...",
# "with the mango ice cream cone held up...", "with the cone in her other hand").
_TRANSIENT_PROP_CLAUSE = re.compile(
    r"(?:,\s*|\s+)(?:holding|with)\s+(?:the|a|an|her|his|their)\s+"
    r"(?P<prop>(?:(?!\b(?:with|holding|and|in|on|beside|close|out|up|toward|to|from)\b)[a-z]+[\s-]+){0,2}?"
    r"(?:skewer|cone|ice\s+cream|cup|mug|glass|bowl|plate|fork|spoon|chopsticks|"
    r"sandwich|burger|slice|bottle|can|bag|box|ticket|napkin|towel|umbrella|hat|"
    r"flower|book|pen|key|coin|card|phone|camera|lantern|torch))\b"
    r"(?:(?!\s+(?:with|and\s+holding)\s+(?:the|a|an|her|his|their)\b)[^,.;])*",
    re.I,
)

# Background crowd / secondary-person framing clauses that pin extra people into
# 22-frame handoff windows ("people gathering at the curb behind her", "so he is behind her").
_BG_CROWD_CLAUSE = re.compile(
    r"(?:,\s*|\s+)(?:people\s+(?:gathering|standing|walking|waiting|crowding|passing)[^,.;]*|"
    r"so\s+(?:he|she|they|it)\s+(?:is|are|stays?)\s+behind\s+(?:her|him|them)|"
    r"playing\s+(?:guitar|music|drums|violin|sax|accordion)(?:\s+on\s+the\s+corner)?)\b",
    re.I,
)

_STATIC_LANDMARK = re.compile(
    r"(?P<who>@[A-Za-z0-9_]+|<Subject \d+>|She|He|They)\s+"
    r"(?P<verb>stands?|is\s+standing|waits?|is\s+waiting)\s+"
    r"(?P<prep>at|beside|by|in\s+front\s+of|next\s+to)\s+"
    r"(?P<det>the|a|an)\s+"
    r"(?P<place>[a-z]+(?:\s+(?!on\b|with\b|holding\b|and\b|smiling\b|smiles\b)[a-z]+){0,3})"
    r"(?:\s+on\s+the\s+same\s+(?:street|road|block|corner))?",
    re.I,
)

_STEP_BACK_ANY = re.compile(
    r"\b(?:steps?\s+back|steps?\s+away|walks?\s+away|backs?\s+away|moves?\s+away)\s+from\s+the\s+(?P<place>[^,.;]+?)(?=[,.;]|$|\s+and\b)",
    re.I,
)


def _strip_transient_props(text, beat0, prev_text="", smooth_motion=True):
    """Remove held-prop clauses for props that were not present in Shot 1 (`beat0`),
    drop secondary crowd framing clutter, and smooth static new-place teleports into
    continuous walking approach.

    Keeps persistent Shot-1 props (e.g. `phone` in a walking vlog) while stripping
    mid-chain props (`skewer`, `ice cream cone`, `cup`) from the Previs so the
    single-generate Previs never spawns them hops early or holds two props at once.
    """
    b0_low = str(beat0 or "").lower()
    prev_low = str(prev_text or "").lower()

    def _keep_or_drop(m):
        prop_words = [w for w in re.findall(r"[a-z]+", m.group("prop").lower()) if len(w) > 2]
        if prop_words and any(w in b0_low for w in prop_words):
            return m.group(0)
        return ""

    out = _TRANSIENT_PROP_CLAUSE.sub(_keep_or_drop, str(text or ""))
    out = _BG_CROWD_CLAUSE.sub("", out)

    if smooth_motion:
        def _re_place(a):
            pl = a.group("place").strip()
            pl_low = pl.lower()
            if pl_low and pl_low in prev_low:
                return "%s continues %s %s %s" % (a.group("who"), a.group("prep"), a.group("det"), pl)
            if any(w in pl_low for w in ("musician", "vendor", "performer", "busker", "singer", "player")):
                return "%s walks past %s %s" % (a.group("who"), a.group("det"), pl)
            return "%s walks up to %s %s" % (a.group("who"), a.group("det"), pl)

        out = _STATIC_LANDMARK.sub(_re_place, out)
        out = _STEP_BACK_ANY.sub(lambda a: "leans back a little, still at the %s" % a.group("place").strip(), out)
    out = re.sub(r"\s+([,.;!?])", r"\1", out)
    out = re.sub(r",\s*\.", ".", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def lead_clause(beat, beat0=None, prev_text="", smooth_motion=False):
    """Extract only the opening posture/motion lead clause of a manual beat.

    Used both as the deterministic fallback when no Qwen3-VL 4B/8B text encoder
    is installed and as the safety fallback per shot. Drops spoken dialogue,
    `_end_sentence`, and compound mid-hop payoff clauses (`, then ...`,
    `and takes a big bite...`) so the previs guides camera + blocking without
    rushing discrete prop actions into the 1.9 s gap.
    """
    first = _first_sentence(beat)
    if not first:
        return ""
    trimmed = _COMPOUND_TAIL.sub("", first).rstrip(" ,;.")
    if beat0 is not None:
        trimmed = _strip_transient_props(
            trimmed, beat0, prev_text=prev_text, smooth_motion=smooth_motion
        ).rstrip(" ,;.")
    if not trimmed:
        trimmed = first.rstrip(" ,;.")
    return trimmed + "."


def format_previs_llm_prompt(shots):
    """Build the numbered user prompt for the Qwen3-VL previs compiler."""
    lines = []
    beat0 = _no_speech(str(((shots or [None])[0] or {}).get("beat") or ""))
    prev_acc = beat0
    for k, sh in enumerate(shots or [], 1):
        raw_beat = str((sh or {}).get("beat") or "")
        clean = lead_clause(
            raw_beat, beat0=beat0 if k > 1 else None,
            prev_text=prev_acc if k > 1 else "", smooth_motion=(k > 1),
        )
        line_txt = clean or _no_speech(raw_beat)
        lines.append(f"{k}: {line_txt}")
        if k > 1:
            prev_acc = prev_acc + " " + line_txt
    return (
        f"Rewrite these {len(lines)} shot beats into {len(lines)} continuous "
        "single-take previs bridge moments following all 4 laws:\n"
        + "\n".join(lines)
    )


def parse_previs_llm_moments(reply, shots):
    """Parse Qwen3-VL's numbered bridge lines and return copies of `shots` with
    `moment` populated for any shot that did not already author one.

    Validates each line: preserves the beat's primary `@tag` / `<Subject N>`,
    strips any stray `<d>`, speech cues, mid-hop payoff tails, and transient
    props not present in Shot 1, and falls back to `lead_clause` if a line is
    missing or malformed.
    """
    parsed = {}
    for raw_ln in str(reply or "").splitlines():
        m = re.match(r"^\s*(\d+)\s*[:.)\-]\s*(.+?)\s*$", raw_ln)
        if m:
            idx = int(m.group(1)) - 1
            txt = _no_speech(m.group(2)).strip()
            if txt and not txt.endswith((".", "!")):
                txt += "."
            if txt:
                parsed[idx] = txt
    out = []
    beat0 = _no_speech(str(((shots or [None])[0] or {}).get("beat") or ""))
    prev_acc = ""
    for k, sh in enumerate(shots or []):
        s = dict(sh or {})
        if str(s.get("moment") or "").strip():
            out.append(s)
            prev_acc = prev_acc + " " + str(s.get("moment") or "")
            continue
        beat = str(s.get("beat") or "")
        who = _WHO_RE.search(_no_speech(beat))
        cand = parsed.get(k, "")
        if cand:
            parts = [p.strip().rstrip(".!?") for p in _SENT.split(_no_speech(cand)) if p.strip()]
            cleaned_parts = []
            for idx, p in enumerate(parts):
                p_clean = _COMPOUND_TAIL.sub("", p).rstrip(" ,;.")
                if k > 0:
                    p_clean = _strip_transient_props(
                        p_clean, beat0, prev_text=prev_acc, smooth_motion=True
                    ).rstrip(" ,;.")
                if p_clean:
                    if idx > 0 and p_clean[0].isupper() and not p_clean.startswith(("@", "<")):
                        p_clean = p_clean[0].lower() + p_clean[1:]
                    cleaned_parts.append(p_clean)
            if cleaned_parts:
                cand = " — ".join(cleaned_parts) + "."
            else:
                cand = ""
            if who and who.group(0) not in cand:
                cand = _PRONOUN.sub(who.group(0), cand, count=1)
                if who.group(0) not in cand:
                    cand = ""
        if not cand:
            cand = lead_clause(beat, beat0=beat0 if k > 0 else None, prev_text=prev_acc, smooth_motion=False)
        if cand:
            s["moment"] = cand
            prev_acc = prev_acc + " " + cand
        out.append(s)
    return out


SEAM_FEATHER_PRE = 16    # outgoing frames before the cut flattened onto the bridge
SEAM_FEATHER_POST = 26   # incoming frames after the cut flattened onto the bridge


def seam_feather(master, cut, prev_window=None, span=SEAM_FEATHER_PRE,
                 blend=SEAM_FEATHER_POST, still=SEAM_STILL):
    """Eliminate the dark-dip / bright-overshoot flash across a hand-tie cut (`master[cut]`).

    Measured on `seamAB_prev_hop_00040` / `00043`:
    - The last ~8 frames of the outgoing hop dip `-6 / 255` approaching the `-22`
      tail guide, and Frame `0..1` of the incoming hop (H3's isolated 1-frame VAE
      token) dips before overshooting across the 22-frame head guide (`0..21`).
    - Bridging per-frame channel levels (`mean + std`) smoothly from a 4-frame
      pre-dip anchor at `cut - pre` to a 4-frame post-guide anchor at `cut + post`
      with a smoothstep target curve and cosine bell weight removes both the
      Frame-0 dip and the 22-frame head-guide highlight overshoot while leaving
      the hop bodies outside `[cut - pre, cut + post]` 100% untouched.
    """
    import torch  # noqa: PLC0415
    n = int(master.shape[0])
    cut = int(cut)
    pre = min(max(2, int(span)), cut)
    post = min(max(2, int(blend)), n - cut - 1)
    if cut < 2 or cut + 2 > n or pre < 2 or post < 2:
        return None
    step, _share = seam_step(master[cut - 2:cut], master[cut:cut + 2], 0, still=still)
    l_start = max(0, cut - pre - 3)
    l_slice = master[l_start:cut - pre + 1].float()
    r_end = min(n, cut + post + 4)
    r_slice = master[cut + post:r_end].float()
    l_mu = l_slice.mean(dim=(0, 1, 2), keepdim=True)[0]
    r_mu = r_slice.mean(dim=(0, 1, 2), keepdim=True)[0]
    l_sd = l_slice.std(dim=(0, 1, 2), keepdim=True, unbiased=False)[0].clamp(min=1e-4)
    r_sd = r_slice.std(dim=(0, 1, 2), keepdim=True, unbiased=False)[0].clamp(min=1e-4)
    total = pre + post
    for k in range(1, total):
        idx = cut - pre + k
        t = (k + 0.5) / float(total)
        ts = t * t * (3.0 - 2.0 * t)
        t_mu = l_mu * (1.0 - ts) + r_mu * ts
        t_sd = l_sd * (1.0 - ts) + r_sd * ts
        f_idx = master[idx].float()
        c_mu = f_idx.mean(dim=(0, 1), keepdim=True)
        c_sd = f_idx.std(dim=(0, 1), keepdim=True, unbiased=False).clamp(min=1e-4)
        dist = abs(k - pre) / float(pre if k < pre else post)
        w = float(0.5 * (1.0 + torch.cos(torch.tensor(3.14159265 * min(1.0, dist))).item()))
        gain = (t_sd / c_sd).clamp(0.85, 1.15)
        matched = (f_idx - c_mu) * gain + t_mu
        master[idx] = (f_idx * (1.0 - w) + matched * w).clamp(0.0, 1.0).to(master.dtype)
    return step if step is not None else (l_mu - r_mu).reshape(-1).cpu()


def tail_feather(master, end_pos, window=WINDOW):
    """Flatten the `+14..+16 / 255` closing `-22` tail-guide flash on a section's
    last hop (`master[end_pos - window : end_pos]`), in place.

    Earlier hops have their `-22` tail span overwritten by the next hop; the last
    hop of a section has no next hop, so its closing `-22` tail guide sits at the
    end of the film (`seamAB_prev_hop_00040` `f680..701`: `92.2 -> 111.4`). Matching
    both per-channel mean and std (black level + contrast) of the tail span to the
    4 clean frames just before it removes both the brightness flare and the milky
    shadow lift at the end of the film.
    """
    import torch  # noqa: PLC0415
    end_pos = int(end_pos)
    w = int(window)
    if end_pos < w + 4 or end_pos > int(master.shape[0]):
        return None
    start = end_pos - w - 1
    ref_slice = master[start - 4:start].float()
    t_mu = ref_slice.mean(dim=(0, 1, 2), keepdim=True)[0]
    t_sd = ref_slice.std(dim=(0, 1, 2), keepdim=True, unbiased=False)[0].clamp(min=1e-4)
    for k in range(end_pos - start):
        idx = start + k
        wt = min(1.0, (k + 1) / 4.0)
        f_idx = master[idx].float()
        c_mu = f_idx.mean(dim=(0, 1), keepdim=True)
        c_sd = f_idx.std(dim=(0, 1), keepdim=True, unbiased=False).clamp(min=1e-4)
        matched = (f_idx - c_mu) * (t_sd / c_sd) + t_mu
        master[idx] = (f_idx * (1.0 - wt) + matched * wt).clamp(0.0, 1.0).to(master.dtype)
    return t_mu.reshape(-1)
