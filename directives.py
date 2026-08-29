"""Directive vocabulary for H3 Ref Chain shot plans.

WHY THIS MODULE EXISTS
----------------------
MiniMax H3 output is overwhelmingly carried by the prompt text, not by the
conditioning channels. Hand-writing continuity prose per shot does not scale and
drifts: every chain re-invents its own phrasing, so nothing learned on one chain
carries to the next. Structured directives compile to vetted prose instead, and
because every string lives in VOCAB below, improving the phrasing improves every
existing shot plan at once.

PHRASING RULES -- read before editing VOCAB
-------------------------------------------
1. AFFIRMATIVE ONLY. Sampling runs through BasicGuider at cfg 1.0 with no
   negative branch, so every concept named is additive and cannot be subtracted.
   "no cut" puts `cut` in front of the encoder. Say what the shot IS doing.
2. NO PRONOUNS. The beat text owns the subject. A pronoun here can disagree with
   it -- plural "they" has been observed rendering two people from one reference.
3. NO ENUMERATED DETAIL. Naming a prop or garment to control it adds it. Keep
   directives about camera, join, framing, and pace only.
4. ONE SENTENCE PER ENTRY. These get concatenated; long entries crowd the beat.
"""

import re

# axis -> option -> prose. An option of "" (unset) emits nothing, deliberately:
# an unset axis should cost no tokens rather than assert a default.
VOCAB = {
    "join": {
        "continuous": "The camera and the action carry straight on from the pinned "
                      "frames in one unbroken take.",
        "match_cut":  "The shot changes on a matched movement, the new framing "
                      "picking up the same gesture already underway.",
        "hard_cut":   "A clean cut opens a new setup, the same place a moment later.",
    },
    "camera": {
        "hold":       "The camera holds its position throughout.",
        "pan_follow": "The camera pans smoothly to follow the movement, holding it in frame.",
        "push_in":    "The camera pushes slowly in.",
        "pull_back":  "The camera draws slowly back, opening the frame.",
        "orbit":      "The camera arcs slowly around the action.",
        "handheld":   "The camera carries a light handheld float.",
    },
    "framing": {
        "keep":   "The framing stays as it is.",
        "wide":   "A wide shot with the full room in view.",
        "medium": "A medium shot from the waist up.",
        "close":  "A close shot, head and shoulders filling the frame.",
    },
    "pace": {
        "slow":   "Everything moves at an unhurried pace.",
        "steady": "The action moves at a steady, even pace.",
        "brisk":  "The action moves briskly.",
    },
    # How the clip ENDS. This axis exists because a beat whose action
    # completes before the frames run out leaves the model with nothing to
    # render, and it settles onto its strongest remaining conditioning --
    # the identity photograph. Observed as the last ~3s of a chain cutting
    # to the reference still. `ongoing` is the default for exactly that
    # reason: it gives the tail somewhere to go that is not the ref.
    "tail": {
        "ongoing": "The action is still underway as the clip ends.",
        "settle":  "The movement eases to a rest and stays there.",
        "hold":    "The final position holds steady through the last moments.",
    },
}

# B2 (2026-08-27). When `join=continuous` and the camera is moving, the framing
# sentence has to describe where the move ENDS, not what the shot opens on.
# Asserting the destination as the opening state while the pin still holds the
# previous hop's framing is what put the jump at the 2->3 seam of chain_00028:
# the compile read "...carry straight on from the pinned frames. The camera
# pushes slowly in. A close shot, head and shoulders filling the frame."
#
# Affirmative, one sentence, no pronouns -- same rules as the rest of VOCAB.
# Set FRAMING_AS_LANDING = False to restore the pre-2026-08-27 phrasing; that
# is the A/B against chain_00038.
FRAMING_AS_LANDING = True
_FRAMING_LANDING = {
    "wide":   "The move settles into a wide shot with the full room in view.",
    "medium": "The move settles into a medium shot from the waist up.",
    "close":  "The move settles into a close shot, head and shoulders filling the frame.",
}
# "hold" is not a move, and "keep" needs no landing -- it already says the
# framing does not change.
_CAMERA_MOVES = ("pan_follow", "push_in", "pull_back", "orbit", "handheld")

# Order matters: this is the order the compiled sentences appear in the prompt.
# `join` leads because it describes how this hop meets the previous one.
AXES = ("join", "camera", "framing", "pace", "tail")

# Applied when a shot names no value for the axis. Only `tail` carries one:
# every other axis is legitimately "unspecified", but an unspecified tail is
# what lets the clip settle onto the reference image.
DEFAULTS = {"tail": "ongoing"}

# Opening line for hop 1, which has no previous hop to join to.
#
# 2026-08-29. This is the DEFAULT, not a constant. It used to be prepended to
# every hop-1 prompt unconditionally, which is correct for the live-action
# plans the pack shipped with and actively wrong for anything else: at cfg 1.0
# with no negative branch "Live-action" and "natural light" are ADDED, ahead of
# whatever style the beat declares. A stop-motion puppet plan compiled to
#
#     Live-action, natural light, one continuous take.
#     ...
#     Hand-drawn stop-motion puppet animation in felt and painted wood ...
#
# and the two fought. It is also the reason hop 1 of the 8x15 s anime chain
# rendered as bright naturalistic daylight (mean luma 72) against a night plan
# and a night place plate, then dropped to 46 on hop 2 the moment this line
# stopped riding.
#
# Two fixes, because either alone leaves a hole. The node exposes an
# `establish` widget so a human can set or clear it; and `establish_for` below
# drops it automatically when the beat names a medium of its own, because a
# model-authored plan never touches a widget.
ESTABLISH = "Live-action, natural light, one continuous take."

# Media that contradict ESTABLISH's "Live-action". Matched against the opening
# of shot 1 only -- a beat that says "a photograph of a painting" later on is
# describing a prop, not declaring its own medium.
_STYLE_WORDS = (
    "anime", "animation", "animated", "cartoon", "cel ", "cel-",
    "stop-motion", "stop motion", "claymation", "clay-mation", "puppet",
    "watercolour", "watercolor", "gouache", "oil-painted", "oil painted",
    "hand-drawn", "hand drawn", "illustrated", "illustration", "storyboard",
    "pixel art", "cgi", "3d render", "3d-render", "rendered in", "rotoscope",
    "comic", "manga", "woodcut", "linocut", "papercraft", "paper-craft",
)
# How far into the beat to look. Rule 12 asks for the style in the first
# sentence; 220 characters covers a generous one without reaching the action.
_STYLE_WINDOW = 220


def declares_own_medium(beat):
    """True when shot 1 opens by naming a medium that is not live action.

    Deliberately narrow. It reads only the opening of the beat, and it defers
    to an explicit "live-action" there, so a plan that means live action keeps
    the establishing line even when it also mentions, say, an animated sign in
    the background.
    """
    head = (beat or "")[:_STYLE_WINDOW].lower()
    if "live-action" in head or "live action" in head:
        return False
    return any(w in head for w in _STYLE_WORDS)


def establish_for(beat, establish=None):
    """The establishing line to prepend to hop 1, or "" for none.

    `establish=None` means "the node did not say", which is every caller that
    predates the widget -- those keep the historical default.
    """
    line = ESTABLISH if establish is None else str(establish or "").strip()
    if line and declares_own_medium(beat):
        return ""
    return line


def validate(axis, value, where=""):
    """Raise ValueError naming the offending shot and the valid options."""
    v = str(value or "").strip()
    if not v:
        return ""
    if v not in VOCAB[axis]:
        valid = ", ".join(sorted(VOCAB[axis]))
        raise ValueError(
            f"HandTieClips: {where}directive {axis}='{v}' is not valid. Use one of: {valid}"
        )
    return v


def directive_prose(directives, hop_index, axes=None):
    """Compile a shot's directives into prose. Hop 1 has nothing to join to."""
    d = directives or {}
    out = []
    for axis in (axes or AXES):
        if axis == "join" and hop_index == 0:
            continue
        value = str(d.get(axis) or "").strip() or DEFAULTS.get(axis, "")
        line = VOCAB.get(axis, {}).get(value)
        if (axis == "framing" and FRAMING_AS_LANDING and hop_index > 0
                and str(d.get("join") or "").strip() == "continuous"
                and str(d.get("camera") or "").strip() in _CAMERA_MOVES
                and value in _FRAMING_LANDING):
            line = _FRAMING_LANDING[value]
        if line:
            out.append(line)
    return " ".join(out)


_OFFICIAL_FIELDS = (
    "subject_definitions:",
    "summary:",
    "retention_analysis:",
    "detailed_description:",
    "integrated_multimodal_description:",
    "overall_soundscape:",
    "non_diegetic_music:",
)

def append_section_line(text, name, line):
    """Append a line to the end of an official field. No-op when absent.

    Carries a hop-1 `tail` directive into a complete six-field block: `tail`
    describes how the clip *ends*, so it belongs at the end of
    detailed_description rather than wrapped around the outside, where it
    would fight the official fields.
    """
    t = text or ""
    line = str(line or "").strip()
    if not line or line in t:
        return t
    marker = name if name.endswith(":") else name + ":"
    start = t.find(marker)
    if start < 0:
        marker = "integrated_multimodal_description:"
        start = t.find(marker)
        if start < 0:
            return t
    end = len(t)
    for other in _OFFICIAL_FIELDS:
        if other == marker:
            continue
        i = t.find(other, start + len(marker))
        if i != -1:
            end = min(end, i)
    return t[:end].rstrip() + "\n" + line + "\n\n" + t[end:].lstrip()


def is_full_h3_prompt(text):
    t = text or ""
    return "subject_definitions:" in t or "integrated_multimodal_description:" in t


def field_body(text, name):
    """Inner text of one official field, or ''."""
    t = text or ""
    marker = name if str(name).endswith(":") else str(name) + ":"
    start = t.find(marker)
    if start < 0:
        return ""
    end = len(t)
    for other in _OFFICIAL_FIELDS:
        if other == marker:
            continue
        i = t.find(other, start + len(marker))
        if i != -1:
            end = min(end, i)
    return t[start + len(marker):end].strip()


def flatten_official_continue(body):
    """Hop 2+ cannot be a complete Ref2VA generate (chain_00030..00032).

    Keep the action + soundscape. Drop subject_definitions / summary /
    retention_analysis and a leading [Shot 1] — those start a new scene.
    """
    if not is_full_h3_prompt(body):
        return body
    dd = (field_body(body, "detailed_description")
          or field_body(body, "integrated_multimodal_description")
          or "")
    dd = re.sub(r"^\[Shot 1\]\s*", "", dd.strip())
    for prefix in (
        "The clip opens already in progress from the pinned frames, "
        "continuing the scene they leave off. ",
        "The clip opens already in progress from the pinned frames. ",
        "The clip opens on the action already in progress from the pinned frames. ",
    ):
        if dd.startswith(prefix):
            dd = dd[len(prefix):].lstrip()
            break
    parts = []
    if dd:
        parts.append(dd)
    sound = field_body(body, "overall_soundscape")
    if sound:
        parts.append("overall_soundscape:\n" + sound)
    music = field_body(body, "non_diegetic_music")
    if music:
        parts.append("non_diegetic_music:\n" + music)
    return "\n\n".join(parts)


def compile_shot(shot, hop_index, establish=None):
    """Build the body text for one hop.

    Returns the *beat* portion only. For hop 2+ the caller wraps this with the
    identity lock and live-frame citation via _assemble_next, so this must not
    duplicate either of those.

    Hop 1 may be a complete official H3 block and is returned intact. Hop 2+
    of a complete block is flattened to a continuation beat — a second full
    Ref2VA generate is a new scene (chain_00030..00032).
    """
    beat = str((shot or {}).get("beat") or "").strip()
    prose = str((shot or {}).get("prose") or "").strip()
    body = "\n\n".join(p for p in (beat, prose) if p)
    dirs = (shot or {}).get("directives")
    if is_full_h3_prompt(body):
        if hop_index == 0:
            # A complete six-field block is one generate and is returned
            # intact -- lead directives would fight its own field text. `tail`
            # is the exception: one affirmative sentence about how the clip
            # ends, which is what stops the last seconds settling onto a
            # reference still, so it is appended inside detailed_description
            # rather than dropped. The rest are reported, because silently
            # ignoring dropdowns the editor still renders is how this went
            # unnoticed.
            body = append_section_line(
                body, "detailed_description",
                directive_prose(dirs, hop_index, axes=("tail",)))
            ignored = sorted(
                a for a in ("join", "camera", "framing", "pace")
                if str((dirs or {}).get(a) or "").strip())
            if ignored:
                print("[HandTieClips] shot 1 is a complete H3 block; its own fields "
                      "win, so these directives are not applied: " + ", ".join(ignored),
                      flush=True)
            return body
        beat = flatten_official_continue(body)
        prose = ""
    lead = directive_prose(dirs, hop_index, axes=("join", "camera", "framing", "pace"))
    tail = directive_prose(dirs, hop_index, axes=("tail",))

    parts = []
    if hop_index == 0:
        # `beat` is shot 1's own text, so a plan that declares its own medium
        # suppresses the live-action default rather than fighting it.
        opening = establish_for(beat, establish)
        if opening:
            parts.append(opening)
    if lead:
        parts.append(lead)
    if beat:
        parts.append(beat)
    if prose:
        parts.append(prose)
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)
