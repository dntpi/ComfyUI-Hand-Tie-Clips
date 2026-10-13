"""Hand-tie: each hop ends on its own clean window, and the next hop opens on it.

W_k is the 22-frame clip rendered for hop k. Its last frame is the still. The
join between hop k and hop k+1 is W_k's sampler latent, rendered from the refs
and a still, never from hop k: hop k is guided onto those frames and then
overwritten with them, and hop k+1 is pinned to them and then overwritten with
them. Neither hop reads the other's finished latent, so texture does not
ratchet, and both sides of the join decode one latent, so the seam stays.

A continuation's window takes the previous window's still as a weak_reference
picture; a restart's takes still 1. With still 1 alone, consecutive windows sat
in different places and the hop between them slid or cut to get there. Only a
picture passes from window to window, never a latent. W_k is muted; the pin is
video only, and each hop keeps its own audio.

This module is the text and the arithmetic, with no ComfyUI imports, so
tools/check_hand_tie.py runs without a server.
"""

import re

import torch

FPS = 24
OVERLAP = 22                # frames each hop shares with the one before (17 + 5: one token group + 2)
MIN_HOP_FRAMES = 56         # 17*3+5: shortest hop whose 22-frame head and tail do not share frames
STILL_FRAMES = 22           # the clip a window is rendered at; its last frame is the still
STILL_SEED_OFFSET = 7919    # a still's seed = the hop's seed + this
STILL_CONTEXT = ("the frame the shot before ends on: weak_reference - the place, the light "
                 "and the surroundings carry over; the shot does not open on it")
HOP_RETRY_STRIDE = 104729   # seed step for the one retake of a hop that cuts


def overlap_rows(frames=OVERLAP):
    """Video latent steps that cover `frames` pixel frames at the start of a hop.

    H3 packs frames in token groups of (1, 4, 4, 4, 4) = 17, so a 17k+5 span is
    k groups plus a 1-frame and a 4-frame token (core's video_latent_t). 22
    frames = 7 steps, and a hop of 17k+5 frames ends on whole groups too, so
    the last 7 steps of the previous hop are exactly the first 7 of this one.
    """
    f = int(frames)
    return 2 if f <= 5 else ((f - 5) // 17) * 5 + 2


def write_rows(video, src, rows, tail=False):
    """Copy `rows` latent steps from `src` onto `video`. [B, C, T, H, W].

    Head (`tail` false): video's first `rows` become src's last `rows`.
    Tail: video's last `rows` become src's first `rows`. A window is exactly
    `rows` long, so either slice is the whole window. Returns `video` itself
    when the splice would be the wrong shape, and never writes that tensor
    in place.
    """
    rows = int(rows)
    if rows <= 0 or int(src.shape[2]) < rows or int(video.shape[2]) <= rows:
        return video
    if tuple(src.shape[:2]) != tuple(video.shape[:2]) \
            or tuple(src.shape[3:]) != tuple(video.shape[3:]):
        return video
    out = video.clone()
    taken = src[:, :, :rows] if tail else src[:, :, -rows:]
    taken = taken.to(device=out.device, dtype=out.dtype)
    if tail:
        out[:, :, -rows:] = taken
    else:
        out[:, :, :rows] = taken
    return out

# Lab joins. `rows` is today's path. The other three hold the windows in the
# latent instead of calling Motion-Context and AddGuide. The node property
# picks one; absent is `rows`.
LAB_JOINS = ("rows", "inplace", "inplace_cont", "inplace_cont_audio")

# A local copy of directives._OFFICIAL_FIELDS. This module does not import it.
_OFFICIAL_FIELDS = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "integrated_multimodal_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)


def head_audio_rows(frames=OVERLAP, fps=FPS, hz=40):
    """Audio latent rows that cover `frames` at `hz` rows per second.

    22 frames at 24 fps is 37 rows.
    """
    return int(round(float(frames) / float(fps) * float(hz)))


def hold_masks(video_shape, audio_shape, rows, head, tail, audio_head_rows=0):
    """Ones everywhere, zeros on the rows a hop holds. 0 = hold, 1 = generate.

    `video_shape` is (B, C, T, H, W). `audio_shape` is (B, C, 2, Ta).
    Returns `(vmask [1,1,T,H,W], amask [1,1,2,Ta])`, float32.
    """
    if len(video_shape) < 3 or len(audio_shape) < 3:
        raise ValueError(
            "hold_masks needs a video shape (B,C,T,H,W) and an audio shape "
            "(B,C,2,Ta)")
    rows = int(rows)
    t = int(video_shape[2])
    if head and tail and 2 * rows > t:
        raise ValueError(
            "held head and tail overlap: %d + %d rows in %d steps" % (rows, rows, t))
    vmask = torch.ones((1, 1) + tuple(int(d) for d in video_shape[2:]),
                       dtype=torch.float32)
    if head and rows > 0:
        vmask[:, :, :rows] = 0
    if tail and rows > 0:
        vmask[:, :, -rows:] = 0
    amask = torch.ones((1, 1) + tuple(int(d) for d in audio_shape[2:]),
                       dtype=torch.float32)
    n = int(audio_head_rows)
    if n > 0:
        amask[..., :n] = 0
    return vmask, amask


def _field_name(line):
    s = line.lstrip()
    for name in _OFFICIAL_FIELDS:
        if s.startswith(name):
            return name
    return None


def _split_field(block, field):
    """Split off one official field.

    The field ends at the next official marker, or at the first blank line
    followed by text that is not a field. Returns (before, value, after).
    """
    lines = str(block).split("\n")
    start = None
    for i, line in enumerate(lines):
        if _field_name(line) == field:
            start = i
            break
    if start is None:
        return None
    inline = lines[start].lstrip()[len(field):].strip()
    end = start + 1
    i = start + 1
    while i < len(lines):
        if _field_name(lines[i]):
            end = i
            break
        if lines[i].strip() == "":
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j >= len(lines) or _field_name(lines[j]) is None:
                end = i
                break
            end = j
            break
        i += 1
    else:
        end = len(lines)
    value_lines = ([inline] if inline else []) + lines[start + 1:end]
    while value_lines and value_lines[-1].strip() == "":
        value_lines.pop()
    before = "\n".join(lines[:start]).rstrip("\n")
    after = "\n".join(lines[end:]).lstrip("\n")
    return before, "\n".join(value_lines).strip(), after


def insert_continuation(block, video_n, audio_n):
    """Tell this hop it continues one video and its audio.

    Adds one `summary:` when the block has none. A block that already has
    `summary:` gets the task prefix on that field and the sentence at its
    end, never a second field. A second call returns the same text.
    """
    block = "" if block is None else str(block)
    video_n = int(video_n)
    audio_n = int(audio_n)
    prefix = "[reference generation + video continuation] "
    cite = "<Video %d> and <Audio %d>" % (video_n, audio_n)
    summary_value = prefix + "The target video is continuation of %s." % cite
    sentence = "The shot seamlessly continues %s." % cite
    if prefix in block and sentence in block:
        return block
    if any(_field_name(line) == "summary:" for line in block.split("\n")):
        before, value, after = _split_field(block, "summary:")
        if not value.startswith(prefix):
            value = prefix + value
        if sentence not in value:
            value = (value.rstrip() + "\n" + sentence) if value else sentence
        parts = [p for p in (before, "summary:\n" + value, after) if p]
        return "\n\n".join(parts)
    field = "summary:\n" + summary_value
    if any(_field_name(line) == "subject_definitions:" for line in block.split("\n")):
        before, value, after = _split_field(block, "subject_definitions:")
        head = "subject_definitions:" + ("\n" + value if value else "")
        if before:
            head = before + "\n\n" + head
        parts = [head, field + "\n\n" + sentence]
        if after:
            parts.append(after)
        return "\n\n".join(parts)
    if block.strip():
        return field + "\n\n" + sentence + "\n\n" + block
    return field + "\n\n" + sentence


def write_back(video, prev_video, rows):
    """`video` with its first `rows` latent steps replaced by `prev_video`'s last
    `rows`. The chain's head splice; a hand-tie join uses `write_rows`."""
    return write_rows(video, prev_video, rows, tail=False)


def video_only_keyframes(keyframes):
    """Drop audio-only keyframes. A keyframe that also carries video stays.

    Motion-Context has no video-only switch: a wired latent always appends an
    audio keyframe, and a muted window's audio is silence. `None` and `[]`
    pass through.
    """
    if not keyframes:
        return keyframes
    return [kf for kf in keyframes
            if kf.get("latent") is not None or kf.get("audio_latent") is None]


def hop_fit_errors(lengths):
    """Hops too short for a 22-frame head pin and a 22-frame tail window.

    Motion-Context drops any anchor that lands inside the pinned head, so a
    hop under 44 frames would lose the end of its own window. 56 is the
    shortest 17n+5 length past that.
    """
    err = []
    for i, n in enumerate(lengths or []):
        n = int(n)
        if n < MIN_HOP_FRAMES:
            err.append(
                "pin_mech=hand_tie hop %d is %d frames; the 22-frame opening "
                "pin and the 22-frame end window need at least %d so they do "
                "not share frames." % (i + 1, n, MIN_HOP_FRAMES))
    return err


# --- the text ---------------------------------------------------------------------

_D = re.compile(r"<d>(.*?)</d>", re.S)


_TAG = re.compile(r"^\s*\[[^\]]*\]\s*")


_SENT = re.compile(r"(?<=[.!?])\s+")


_SPEECH = re.compile(
    r"(?:,?\s*(?:and\s+)?)?(?:\(S\d+\)\s*)?\b(?:says|asks|whispers|shouts|replies|adds|mutters|calls|"
    r"continues|answers|sings)\b[^<.]{0,40}?<d>.*?</d>", re.S)


_WHO_RE = re.compile(r"@[A-Za-z0-9_]+|<Subject \d+>")


_TALKING = re.compile(
    r"(?:,\s*|\s+and\s+|\s+)(?:still\s+|keeps\s+)?(?:talking|chatting|speaking|narrating)\b[^,.;]*", re.I)


def _no_speech(text):
    """A beat with its spoken lines taken out, speech verb and speaker tag included.

    A still's text must not carry a dangling "(S1) says,": it would draw an open
    mouth with no words into the frame a hop has to land on."""
    t = _SPEECH.sub(".", text or "")
    t = _D.sub("", t)
    # A talking cue with no words is worse than none: H3 invents the speech in its
    # default language and, in a phone vlog, burns in captions for it (a rejected
    # take of a 9:16 vlog clip, 2026-10-10: Chinese subtitles under "talking into it").
    t = _TALKING.sub("", t)
    t = re.sub(r"\(S\d+\)\s*", "", t)
    t = re.sub(r"\s*\.\s*\.", ".", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def _first_sentence(text):
    t = _no_speech(text)
    return _SENT.split(t, 1)[0].strip() if t else ""


_PRONOUN = re.compile(r"^(He|She|They)\b")


def _end_sentence(text):
    """How the beat ends: its last sentence after the first that names a person
    (a pronoun opening counts, the named person put in its place), else "".

    A hop's still shows how its beat ends (`still_text`). The Carolyn taste beat
    (2026-10-13) ends on its second sentence, the swallow; given only the opening,
    the frame caught her mid-chew and the next hop said its line through it. A
    sound sentence is never an ending.
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
# the walk). Hand-tie leaves these out of every hop; `line_lints` says
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


def open_lips(shots):
    """Shots with closed-lips phrases taken out of every beat that has a line.
    A quiet hop keeps them (chewing with lips closed is what it should show).
    Returns copies."""
    out = []
    for sh in shots or []:
        s = dict(sh or {})
        b = str(s.get("beat") or "")
        if _D.search(b) and _LIPS.search(b):
            s["beat"] = re.sub(r"\s+([.,!?])", r"\1", _LIPS.sub("", b))
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


def _stamp(seconds):
    s = max(0.0, seconds)
    return "%02d:%06.3f" % (int(s // 60), s - 60 * int(s // 60))


CUTOFF = "<|cutoff|>"       # core's tokenizer special token (comfy/text_encoders/minimax.py)


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


# The overlap (0.917 s) is dropped from the film, and its picture is the muted window,
# so nothing said there can be kept or lip-synced. The stamp does not reliably set the
# onset: 1.5 s was tried (Storyboard4 run 2, 2026-10-14) and hop 3 lost more of its
# line than at 1.0 s, so it is back, with the word it cost.
LINE_START_S = 1.0


# H3's speaking rate is not one number: chain_00153 spoke 14-17 words at about 2.1 a
# second, chain_00155 spoke 9-10 at about 3, starting 1.2-1.7 s in. A 12-word cap off
# the slow rate left 2.3-3.6 s of a vlogger's face with nothing said, and the model
# filled it with mouthing. The cap sits between the two; the close is timed off the
# fast one, so the closed-mouth sentence lands where a short line actually ends.
LINE_WPS = 2.4              # words a second the cap assumes: 14 words at 8 s


LINE_WPS_FAST = 3.0         # chain_00155: when the line is predicted to end


CLOSE_MARGIN_S = 0.5        # the close lands at least this far before the hop's last second


_TIMED = re.compile(r"\bAt \d\d:\d\d(?:\.\d+)?\b")


def line_budget(hop_frames, fps=FPS):
    """Spoken words that fit in a hop: after the overlap it opens on (dropped from the
    film) and before the last second, where it settles onto its still."""
    usable = int(hop_frames) / float(fps) - LINE_START_S - OVERLAP / float(fps)
    return max(0, int(usable * LINE_WPS))


def _close_s(beat, hop_frames, fps=FPS, start_s=LINE_START_S):
    """When the sentence after a hop's last line should start: the predicted end of
    the line at LINE_WPS_FAST, never later than CLOSE_MARGIN_S before the tail."""
    words = sum(len(_TAG.sub("", m).split()) for m in _D.findall(beat))
    last = (int(hop_frames) - OVERLAP) / float(fps) - CLOSE_MARGIN_S
    return round(max(start_s + 0.5, min(start_s + words / LINE_WPS_FAST, last)), 1)


def time_lines(shots, start_s=LINE_START_S, hop_frames=None, fps=FPS):
    """Shots whose first spoken line is placed after the overlap the hop opens on.

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
                s["beat"] = (b[:at].rstrip() + " At %s " % _stamp(_close_s(beat, hop_frames[k], fps, start_s))
                             + b[at:])
        out.append(s)
    return out


# A mouth that eats cannot say a line: a hop with both played the line over the
# eating (chain_00153, 00160) or through the chewing (00154) -- three films of six,
# the same beat each time. Every other dialogue hop passed.
_MOUTH = re.compile(r"\b(eat|eats|eating|bite|bites|biting|chew|chews|chewing|sip|sips|sipping|"
                    r"drink|drinks|drinking|swallow|swallows|swallowing|lick|licks|licking|"
                    r"taste|tastes|tasting)\b", re.I)


def line_lints(shots, hop_frames, fps=FPS):
    """Warnings for a hop whose lines will not fit, or that
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
            warn.append("hand-tie: shot %d speaks %d words; about %d fit in it "
                        "(%.1f s at %.1f words a second). The rest runs into the hop's last second, "
                        "where it settles onto its still. Cut the line or give the shot a "
                        "longer duration. "
                        "After the line, give the shot an action and a sound: a hop that "
                        "runs out of words fills the rest with mouthing."
                        % (k + 1, words, room, room / LINE_WPS, LINE_WPS))
    return warn


CUT_JUMP = 1.4           # a cut's one-frame change beats CUT_SPAN frames of motion either side


CUT_FLOOR = 15.0 / 255   # ... and is at least this big: a jump in a still shot is no cut.


                         # 15, not 20: chain_00156's snap at f22 moved 18/255 at 3.5x
                         # its neighbours; clean frames over CUT_JUMP move 14 at most
CUT_SPAN = 3             # the motion yardstick: frames either side


CUT_CELLS = 12           # coarse cells across the frame's short side: layout, not texture.


def hard_cuts(frames):
    """Frames where a clip hard-cuts to another shot: [index of the first new frame].

    A hop that cuts to reach its still puts the cut in the film; the node checks
    each hop and renders a cutting one once more (chain_00135 split two kitchens;
    chain_00146 jumped from a medium to a wide at 13.08 s, both under the old
    previs).

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
                      for i in range(0, n, 32)])           # chunked: a native hop is ~3 GB as float
    d = lambda a, b: float((luma[a] - luma[b]).abs().mean())  # noqa: E731
    out = []
    for t in range(CUT_SPAN + 1, n - CUT_SPAN):
        j = d(t - 1, t)
        if j >= CUT_FLOOR and j >= CUT_JUMP * max(d(t - 1 - CUT_SPAN, t - 1), d(t, t + CUT_SPAN),
                                                  0.5 / 255):
            out.append(t)
    return out


SOUND_RECALL = 0.6           # share of the line's words a draft must say


QUIET_WORDS = 2              # words a quiet hop may "say" (Whisper hears "Thank you." in a hum)


# --- the storyboard --------------------------------------------------------------

def still_text(shot):
    """The text a hop's still is rendered from: the beat's opening (who, where)
    and how it ends, with no line, no talking cue and no sound sentence.

    The still is the frame the hop ends on, so it shows the ending; the opening
    stays because it carries the place. A beat that is one sentence is used as
    it is. Spoken words are left out: a still has no sound, and a line in its
    prompt draws an open mouth into the frame the hop must land on.
    """
    beat = str((shot or {}).get("beat") or "")
    first = _first_sentence(beat)
    end = _end_sentence(beat)
    return (first + " " + end).strip() if end and end != first else first


def still_seed(hop_seed):
    return (int(hop_seed) + STILL_SEED_OFFSET) & 0x7FFFFFFF


def settings_errors(overlap_n, last_frame_guide, master_audio=""):
    """What hand-tie refuses before anything samples."""
    err = []
    if int(overlap_n) != OVERLAP:
        err.append("pin_mech=hand_tie needs overlap 0.9 s (22 frames): the join "
                   "window is 22 frames, %d were asked for." % int(overlap_n))
    if str(last_frame_guide or "off") != "off":
        err.append("pin_mech=hand_tie ends every hop on its own storyboard still; "
                   "last_frame_guide=%s would put a second end guide on the same frame. "
                   "Set it to off." % last_frame_guide)
    if str(master_audio or "").strip():
        err.append("pin_mech=hand_tie does not take master_audio_file yet. Clear "
                   "master_audio_file, or use pin_mech=auto.")
    return err
