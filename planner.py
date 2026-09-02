"""Have a language model write a plan, then make it fix its own mistakes.

`prompt_pack/README.md` has always told people to close this loop by hand:

    If the node rejects the plan, paste the error straight back into the chat --
    every message names the shot or reference it came from, and one round trip
    usually fixes it.

That instruction is the whole feature. The node already carries validators whose
messages were written to be read, and the A/B run that chose the shipped prompt
proved the need: two unrelated model families made the same `@tag` mismatch, and
one round trip fixed both. This module does that round trip automatically.

The important design property is that `validate()` is **pure and synchronous**
and `write_plan()` takes its completion function as an argument. Together those
mean the repair loop can be tested with a scripted fake model and no server --
which is the only way it gets tested at all, in the tradition of
`tools/check_features.py`. A loop that has never demonstrably repaired anything
is a loop nobody should trust.

`validate()` deliberately re-runs the *real* checkers rather than describing the
rules a second time. A second copy of the rules is a second thing to get wrong,
and the whole point is that what passes here is what the node accepts.
"""

import json
import re

from . import directives as _d
from . import plan as _plan
from . import refs as _refs

TAG = "HandTieClips"

MAX_ATTEMPTS = 3
SYSTEM_PROMPT = "prompt_pack/SYSTEM_PROMPT.md"
SCHEMA_FILE = "prompt_pack/SCHEMA.json"


def _pack_file(rel):
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, rel.replace("/", os.sep))


def system_prompt():
    """The shipped prompt, read from disk rather than copied into this file.

    `SYSTEM_PROMPT.md` is generated from `AUTHORING_PROMPT.md` and is the exact
    text the docs tell people to paste into LM Studio. Reading it means the
    button and the manual route can never drift apart, and improving the prompt
    improves both at once.
    """
    with open(_pack_file(SYSTEM_PROMPT), encoding="utf-8") as fh:
        return fh.read()


# | `8 s` | 35-60 | 1-2 |   -- the length table in SYSTEM_PROMPT.md
_BEAT_ROW = re.compile(
    r"^\|\s*`(\d+\s*s)`\s*\|\s*(\d+)\s*-\s*(\d+)\s*\|\s*(\d+)(?:\s*-\s*(\d+))?\s*\|",
    re.M)


def beat_table():
    """{"8 s": (min_words, max_words, min_lines, max_lines)}, read from the prompt.

    Parsed rather than restated, for the reason `schema()` reads SCHEMA.json and
    `system_prompt()` reads the markdown: the numbers the model is told and the
    numbers anything here checks have to be one source or they drift, and this
    table is the one part of that document with arithmetic in it.
    """
    out = {}
    try:
        text = system_prompt()
    except Exception:
        return out
    for label, w0, w1, l0, l1 in _BEAT_ROW.findall(text):
        out[re.sub(r"\s+", " ", label)] = (int(w0), int(w1),
                                           int(l0), int(l1 or l0))
    return out


# Words per second of ordinary speech. ~150 wpm is the conversational figure
# and it matches what this model renders: chain_00059 hop 1 carried six spoken
# words and measured 1.8 s of voiced audio.
SPEECH_WPS = 2.5

# Speech rate for scripts written in syllable blocks rather than space-delimited
# words -- Hangul, Han, Kana. Counting their whitespace tokens with SPEECH_WPS
# undercounts badly: a 27-syllable Korean line is 8 tokens, which the word rate
# calls 3.2 s against a real 4-5 s, and the "write roughly N words" target it
# derives would ask for about 85 syllables in a 10 s hop. ~5.5 syllables a
# second is the conversational figure across all three scripts; it is a
# reference figure, not something measured on this model.
SPEECH_SPS = 5.5

# Built from codepoints rather than written as an escape class so the source
# stays pure ASCII. Hangul jamo and syllables, kana, CJK ideographs, halfwidth
# kana.
_CJK_RANGES = ((0x1100, 0x11ff), (0x3040, 0x30ff), (0x3130, 0x318f),
               (0x3400, 0x4dbf), (0x4e00, 0x9fff), (0xa960, 0xa97f),
               (0xac00, 0xd7ff), (0xff66, 0xff9f))
_CJK = re.compile("[" + "".join(chr(a) + "-" + chr(b) for a, b in _CJK_RANGES) + "]")


def speech_seconds(text):
    """Seconds of speech in `text`, counting each script in its own unit.

    A plan may be written in any language -- the beats stay English prose, the
    lines inside the quotes need not. Mixed text is counted both ways and
    added, so an English beat with one Korean line is estimated correctly
    rather than in whichever unit happens to dominate.
    """
    t = str(text or "")
    cjk = len(_CJK.findall(t))
    # Only tokens carrying a letter or digit. Stripping the CJK out of a
    # Korean line leaves its punctuation stranded as free-standing tokens,
    # and counting "!" and "." as two spoken words bought a 10 s hop most of
    # a second it does not have.
    rest = sum(1 for w in _CJK.sub(" ", t).split() if any(c.isalnum() for c in w))
    return cjk / SPEECH_SPS + rest / SPEECH_WPS


def spoken_text(beat):
    """Just the words inside the spoken spans, joined."""
    t = str(beat or "")
    return " ".join(t[a + 1:b] for a, b in spoken_spans(t))


# How much of a hop a speaking character should actually be speaking for. Below
# this there are seconds of someone visibly mid-sentence with nothing assigned,
# and the model fills them itself -- as fragments (chain_00059: 17.8% voiced,
# longest run 0.5 s) or as invented dialogue (chain_00048). Half is deliberately
# lenient: a beat is allowed to be action as well as talk, and the point is to
# catch a hop that cannot possibly be filled, not to demand wall-to-wall speech.
SPEECH_MIN_SHARE = 0.5

# Non-speech sounds a beat can hand its silent seconds to. The vocabulary is
# the one SYSTEM_PROMPT rules 4 and 6 already teach -- narrowband, tied to a
# thing on screen -- so the lint accepts exactly what the prompt asks for.
_SOUND = re.compile(
    r"\b(sound|tone|hum\w*|whir\w*|rattl\w*|clatter\w*|clink\w*|footsteps?|"
    r"boots?|rain|traffic|buzz\w*|click\w*|creak\w*|rustl\w*|patter\w*|"
    r"drone|murmur\w*|chatter\w*|announcement\w*|horn|engine|wind|birdsong|"
    r"tick\w*|thud\w*|scuff\w*|squeak\w*|jingl\w*|shuffl\w*|breath\w*|"
    r"sigh\w*|ring\w*|grind\w*|whistl\w*|siren|bell|chime\w*|static|noise|"
    r"music|rumbl\w*|hiss\w*|splash\w*|crunch\w*|tap\w*|knock\w*|hoot\w*|"
    r"clang\w*|scrap\w*|whoosh\w*|purr\w*|bark\w*|applause|laughter)\b",
    re.I)

# How much action may precede the first spoken line before those seconds are
# worth a sound of their own. Eight words is roughly three seconds of screen
# action at the rate the length table assumes -- long enough that the model has
# frames to fill and no instruction for them. Measured against the walk-then-
# talk hop 2 of 2026-09-02, which opened on invented dialogue over a silent
# audio pin: hop 1 ended quiet, which rule 5 asks for, and the yap was hop 2's
# OWN unassigned opening, which nothing in the pack covered.
LEAD_IN_MAX_WORDS = 8


def count_beat(beat):
    """(words, spoken_lines, spoken_words) for one beat.

    A spoken line is a single-quoted span. The delimiter test is a quote that is
    NOT between two word characters, because the beats are full of apostrophes
    that are: "Today's class was absolutely exhausting" is one line, not two.

    `spoken_words` is what is inside those spans. Lines are the wrong unit on
    their own -- a line runs from six words to twenty, and six words is 2.4 s
    of a 10 s hop.
    """
    text = str(beat or "")
    words = len(text.split())
    spans = spoken_spans(text)
    spoken = 0
    for a, b in spans:
        spoken += len(text[a + 1:b].split())
    return words, len(spans), spoken


def spoken_spans(beat):
    """Character spans of the single-quoted spoken lines, in order.

    Split out of `count_beat` because where the first line STARTS is its own
    question: a beat whose opening seconds are action carries frames with a
    picture assigned and no sound, and rule 3 applies inside a hop as well as
    across one. See the lead-in lint in `validate`.
    """
    text = str(beat or "")
    marks = [m.start() for m in re.finditer(r"'", text)]
    delims = [i for i in marks
              if not (i > 0 and text[i - 1].isalnum()
                      and i + 1 < len(text) and text[i + 1].isalnum())]
    return list(zip(delims[0::2], delims[1::2]))


def schema():
    """The generated JSON Schema, for servers that support structured output.

    `tools/gen_schema.py` builds this from the node itself and asserts against
    `plan._SHOT_KEYS`, `refs.REF_FIELDS` and `refs.SUBJECT_FIELDS`, so it cannot
    describe a vocabulary that does not exist. Handing it to the server as
    `response_format` deletes the malformed-JSON failure class outright.
    """
    try:
        with open(_pack_file(SCHEMA_FILE), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print(f"[{TAG}] SCHEMA.json unreadable, falling back to free-form "
              f"JSON: {exc}", flush=True)
        return None


# -- pulling the two blocks out of a reply ---------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def split_reply(raw):
    """Return (shot_plan_text, ref_plan_text) from a model's reply.

    Three shapes have to work, because all three occur in practice:
      * structured output -- one object with `shot_plan` and `ref_plan` keys;
      * two ```json``` fences, which is what the prompt asks for in prose;
      * one bare object, when a model ignores the fences entirely.
    """
    raw = (raw or "").strip()
    if not raw:
        return "", ""

    # Structured output: a single object carrying both.
    try:
        whole = json.loads(raw)
    except ValueError:
        whole = None
    if isinstance(whole, dict) and ("shot_plan" in whole or "ref_plan" in whole):
        sp = whole.get("shot_plan")
        rp = whole.get("ref_plan")
        dump = lambda v: "" if v is None else (
            v if isinstance(v, str) else json.dumps(v, indent=2))
        return dump(sp), dump(rp)

    blocks = [b.strip() for b in _FENCE.findall(raw) if b.strip()]
    if not blocks and whole is not None:
        blocks = [raw]

    shot_txt = ref_txt = ""
    for b in blocks:
        try:
            obj = json.loads(b)
        except ValueError:
            continue
        # Identify by shape, not by order: a model that emits the register
        # first should not have its plans swapped silently.
        if isinstance(obj, dict) and ("refs" in obj or "subjects" in obj):
            ref_txt = ref_txt or b
        elif isinstance(obj, list) or (isinstance(obj, dict) and "shots" in obj):
            shot_txt = shot_txt or b
    if not shot_txt and blocks:
        # A subjects-only repair is a register patch, not a script. Taking
        # blocks[0] here is how `{"subjects": {...}}` became
        # `shot_plan object needs a "shots" array`.
        try:
            first = json.loads(blocks[0])
        except ValueError:
            first = None
        if not (isinstance(first, dict)
                and ("refs" in first or "subjects" in first)
                and "shots" not in first):
            shot_txt = blocks[0]
    return shot_txt, ref_txt


# -- validation ------------------------------------------------------------

def validate(shot_text, ref_text, *, hops=None, known_files=None, pinned=None,
             duration=None):
    """Run the node's own checkers. Returns (errors, warnings).

    `errors` are what the node would REJECT -- they go back to the model.
    `warnings` are the lints the node prints and keeps going on; they are shown
    to the person but never retried, because a model asked to fix a lint it
    disagrees with tends to rewrite the parts that were fine.
    """
    from .h3_ref_chain import DURATION_FRAMES, FPS

    errors, warnings = [], []

    try:
        shots = _plan.parse_plan(shot_text)
    except Exception as exc:
        return [_clean(exc)], warnings
    if not shots:
        return ["shot_plan is empty -- it needs at least one shot."], warnings

    if hops and len(shots) != int(hops):
        errors.append(f"the plan has {len(shots)} shot(s) but {hops} hop(s) "
                      f"were asked for. Write exactly {hops}.")

    # An absent register is its own fault and has to say so. Reported only as
    # undeclared beat tags it reads as a spelling problem, and a repair turn
    # spends itself rewriting beats that were already right: gemma4-26b went
    # @woman_face -> @ref_1 on attempt 2 and still shipped no `ref_plan`, three
    # attempts, no convergence. The schema now requires both documents; this is
    # the same guard for `use_schema=False` and for servers that ignore it.
    if not str(ref_text or "").strip():
        errors.append(
            "the reply had no `ref_plan` document. Answer with BOTH JSON "
            "documents: `shot_plan` and `ref_plan`. Every @tag a beat uses "
            "must be declared as a `tag` in ref_plan.refs.")
        return errors, warnings

    try:
        ref_plan = _refs.parse_ref_plan(ref_text)
    except Exception as exc:
        return [_clean(exc)], warnings

    refs = ref_plan.get("refs") or []
    subs = ref_plan.get("subjects") or {}
    known = {r["tag"] for r in refs}

    # A duration label has to be exact -- "10s" is not "10 s". `parse_plan` does
    # not check this (it takes any string), so the failure surfaces later, at
    # h3_ref_chain.py:1399. Catching it here turns a rejected queue into a retry.
    for i, sh in enumerate(shots):
        dur = (sh or {}).get("duration")
        if dur and str(dur) not in DURATION_FRAMES:
            errors.append(
                f"shot {i + 1}: duration {dur!r} is not a valid label. "
                f"Use one of: {', '.join(DURATION_FRAMES)}")

    # Beat length against the length table, now that the hop length is known.
    # A warning, not an error: the node renders a short beat perfectly happily,
    # and `errors` means "what the node would REJECT". It is the loudest lint
    # in the pack all the same -- a hop given fewer spoken lines than its row
    # asks for leaves seconds of a visibly speaking character with nothing
    # assigned, and the model writes its own. Every 8 s beat measured on
    # 2026-09-02 carried one line where the row allows two, and chain_00052
    # came back 26.7% voiced.
    _table = beat_table()
    for i, sh in enumerate(shots):
        row = _table.get(str((sh or {}).get("duration")
                             or duration or "").strip())
        if not row:
            continue
        w0, w1, l0, l1 = row
        words, spoken, spoken_words = count_beat((sh or {}).get("beat"))
        label = str((sh or {}).get("duration") or duration).strip()
        if words < w0 or words > w1:
            warnings.append(
                f"shot {i + 1}: the beat is {words} words; the {label} row "
                f"wants {w0}-{w1}. "
                + ("Written short, the model finishes the action early and "
                   "invents something for the seconds left over."
                   if words < w0 else
                   "Written long, the action is truncated mid-way."))
        if spoken < l0:
            warnings.append(
                f"shot {i + 1}: {spoken} spoken line(s); the {label} row wants "
                f"{l0}" + (f"-{l1}" if l1 > l0 else "") + ". The line count is "
                "a floor as well as a ceiling -- speaking seconds with nothing "
                "assigned come back as invented dialogue.")
        # Lines are the wrong unit on their own: one runs from six words to
        # twenty. This is the arithmetic, and it is worth stating outright
        # because the number the author needs is not the number of lines.
        secs = DURATION_FRAMES.get(label, 0) / float(FPS or 24)
        said = spoken_text((sh or {}).get("beat"))
        if spoken_words and secs:
            speech = speech_seconds(said)
            # Name the target in the unit the line is actually written in,
            # because "roughly 25 words" of Korean is about 85 syllables and
            # three times the hop.
            if len(_CJK.findall(said)) > len(_CJK.sub(" ", said).split()):
                target = f"{int(secs * SPEECH_SPS)} syllables"
            else:
                target = f"{int(secs * SPEECH_WPS)} words"
            if speech < secs * SPEECH_MIN_SHARE:
                warnings.append(
                    f"shot {i + 1}: the spoken lines run about {speech:.1f}s "
                    f"in a {secs:.0f}s hop. A character written as talking "
                    f"throughout needs roughly {target}. The seconds left over "
                    f"are a person visibly mid-sentence with nothing assigned, "
                    f"and the model fills them itself. Give those seconds more "
                    f"to say, or name the sound they carry.")
        # Rule 3 inside a hop. A beat that opens on action and speaks later has
        # frames with a picture and no sound, and the model fills them with
        # dialogue nobody wrote -- the same failure as a silent hop, one
        # granularity down. Ending the PREVIOUS hop quiet does not cover it.
        beat_text = str((sh or {}).get("beat") or "")
        spans = spoken_spans(beat_text)
        if spans:
            lead = beat_text[:spans[0][0]]
            if (len(lead.split()) > LEAD_IN_MAX_WORDS
                    and not _SOUND.search(beat_text)):
                warnings.append(
                    f"shot {i + 1}: {len(lead.split())} words of action run "
                    f"before the first spoken line and the beat names no "
                    f"sound. Those opening seconds have a picture and no "
                    f"audio assigned, and the model fills them with dialogue "
                    f"nobody wrote. Name what they carry -- footsteps, room "
                    f"tone, a passing train.")

    # A person with pictures but no continuity text still *renders*, which is
    # why `refs.check` only warns. The writer is the one place that can fill
    # the box, so missing name/locked is an error here and gets another
    # attempt -- otherwise Accept writes an empty subject card.
    for num in sorted({r["subject"] for r in refs if r.get("subject") is not None}):
        info = subs.get(num) or {}
        missing = [k for k in ("name", "locked", "context")
                   if not (info.get(k) or "").strip()]
        if missing:
            errors.append(
                f"subject {num} is missing {', '.join(missing)}. Fill from "
                f"the photograph -- prose, never an @tag. `context` is "
                f"wardrobe and appearance, never a pose and never a "
                f"place: it is injected on EVERY hop, so a posture here "
                f"argues with every beat that has them doing something "
                f"else. The beat owns what they are doing.")

    pinned_rows = [p for p in (pinned or []) if isinstance(p, dict)]
    if pinned_rows:
        want = {str(p.get("tag") or "").lstrip("@") for p in pinned_rows}
        by_tag = {str(p.get("tag") or "").lstrip("@"): str(p.get("file") or "")
                  for p in pinned_rows}
        have_tags = {r["tag"] for r in refs}
        missing = sorted(want - have_tags)
        extra = sorted(have_tags - want)
        if missing:
            errors.append(
                "the rail already has "
                + ", ".join("@" + t for t in missing)
                + ". Keep those tags; do not replace them.")
        if extra:
            errors.append(
                "do not add references that are not on the rail: "
                + ", ".join("@" + t for t in extra) + ".")
        for r in refs:
            want_file = by_tag.get(r["tag"])
            got = (r.get("file") or "").strip()
            if want_file and got != want_file:
                errors.append(
                    f"@{r['tag']} must keep file '{want_file}', not '{got}'.")
            if not (r.get("desc") or "").strip():
                errors.append(
                    f"@{r['tag']} needs `desc`: one sentence of what the "
                    f"photograph shows.")

    # A named picture that is not on disk stops the queue, so an invented
    # filename is a model error worth retrying rather than a user problem.
    if known_files is not None:
        have = {str(f) for f in known_files}
        # The full list is already in the system turn. Repeating thirty-odd
        # filenames inside every error buries the one sentence that says what
        # to do, and on a folder this size it is most of the retry turn.
        shown = sorted(have)[:12]
        catalogue = (", ".join(shown)
                     + (f", and {len(have) - len(shown)} more (see the list "
                        f"above)" if len(have) > len(shown) else ""))
        for r in refs:
            fname = (r.get("file") or "").strip()
            if fname and fname not in have:
                errors.append(
                    f"@{r['tag']} names '{fname}', which is not in the "
                    f"reference folder. Do not invent filenames -- use one of "
                    f"the exact names given, or omit `file` for this ref. "
                    + (f"Available: {catalogue}." if have
                       else "The folder is empty, so omit `file` entirely."))

    # Which refs count as loaded. The node derives this from files it actually
    # decoded (`slot_images` at h3_ref_chain.py:1447); here the file list stands
    # in for the disk. A ref whose picture is not wired is not ACTIVE on any
    # hop, and `resolve_tags` then rejects a tag that is otherwise perfectly
    # declared -- so getting this wrong makes every good plan look broken.
    if known_files is None:
        wired = {r["slot"] for r in refs}
    else:
        have = {str(f) for f in known_files}
        wired = {r["slot"] for r in refs
                 if not (r.get("file") or "").strip()
                 or (r.get("file") or "").strip() in have}

    # The tag round trip. This is the fault the A/B actually produced: a beat
    # says @kitchen and the register never declares it, so resolve_tags raises
    # and the queue stops. Nothing before this point catches it.
    for i, sh in enumerate(shots):
        beat = (sh or {}).get("beat") or ""
        if not beat:
            continue
        used = set(re.findall(r"@([A-Za-z0-9_]+)", beat))
        unknown = sorted(used - known)
        if unknown:
            errors.append(
                f"shot {i + 1}: the beat uses "
                + ", ".join(f"@{t}" for t in unknown)
                + " but the reference register does not declare "
                + ("them" if len(unknown) > 1 else "it")
                + ". The tag in the beat and the `tag` in the register must be "
                  "the same string, character for character."
                + (f" Declared: {', '.join('@' + t for t in sorted(known))}."
                   if known else ""))
            continue
        if not refs:
            continue
        active = _refs.active_refs(refs, i, wired)
        ords = _refs.ordinals(active)
        shift = 1 if i > 0 else 0            # the live frame takes ordinal 1
        hop_ords = {t: n + shift for t, n in ords.items()}
        try:
            resolved = _refs.resolve_tags(
                beat, hop_ords, _refs.subjects(refs), where=f"shot {i + 1}",
                declared=known,
                subject_names=({k: (v or {}).get("name")
                                for k, v in subs.items()} if i > 0 else None))
        except Exception as exc:
            errors.append(_clean(exc))
            continue
        if i > 0 and _d.is_full_h3_prompt(resolved):
            warnings.append(
                f"shot {i + 1}: this reads as a complete H3 block rather than "
                f"a continuation beat; it will be flattened.")

    # The lints. These never stop a queue, so they are reported, not retried.
    try:
        warnings.extend(_plan.check_coherence(shots) or [])
    except Exception:
        pass
    for fn, args in ((_plan.check_place_handoff, (shots, ref_plan)),
                     (_plan.check_over_delivery, (shots,))):
        try:
            warnings.extend(fn(*args) or [])
        except Exception:
            pass
    try:
        # `wired`, not an empty set. Passing set() makes check() report every
        # ref as unreadable -- four false "this ref is inactive" warnings on a
        # plan whose pictures are all present, which is worse than no warning
        # because it teaches the reader to ignore the tier.
        warnings.extend(_refs.check(ref_plan, wired) or [])
    except Exception:
        pass

    # Two plates of one person are not interchangeable, and the schema default
    # (`fully_preserved` when `subject` is set) pushes both toward "face".
    # Observed live: a full-body plate with a microphone and a different room
    # in it, marked fully_preserved on every hop -- which asks the encoder to
    # copy that room too. The prompt already distinguishes a likeness plate
    # from a wardrobe plate; nothing checked that the plan did.
    #
    # Decidable from the register alone, so it is a lint rather than a guess:
    # two "copy this exactly" plates of one person riding the same hop are
    # contradictory instructions whichever picture is which.
    try:
        for num in sorted({r["subject"] for r in refs
                           if r.get("subject") is not None}):
            mine = [r for r in refs if r.get("subject") == num
                    and (r.get("retention") or "fully_preserved")
                    == "fully_preserved"]
            if len(mine) < 2:
                continue
            shared = sorted(set.intersection(*[
                set(r.get("shots") or [1]) for r in mine]))
            if not shared:
                continue
            warnings.append(
                f"subject {num} has {len(mine)} `fully_preserved` plates "
                + ", ".join("@" + r["tag"] for r in mine)
                + f" riding hop(s) {', '.join(str(h) for h in shared)} "
                f"together. One picture is the likeness; a picture that shows "
                f"the whole outfit is the wardrobe plate and wants "
                f"`partially_copy`, or its background rides into the scene "
                f"with the garment.")
    except Exception:
        pass

    # A wardrobe plate on a continuation hop brings its own room with it. The
    # garment is what `partially_copy` is for, and hop 1 is where it lands --
    # after that the pin carries it, which is what the `next`-mode drop in the
    # renderer exists to arrange. Scheduled onto later hops the photograph
    # competes with the pin as a Picture, and the moment the beat asks for
    # something the pin cannot supply the model reaches for it instead.
    #
    # chain_00034 was this on hop 2 (commercial kitchen, apron gone). The 3x10 s
    # portrait chain on 2026-09-02 was this on hop 3: `podcast_host.jpg` is her
    # sitting in a studio, it rode all three hops, and a beat reading "she turns
    # the camera toward her face" cut to that room mid-hop and back. The tone
    # anchor measured the excursion at +14.0/255 against hop 2's +5.6.
    #
    # SYSTEM_PROMPT.md:317 already states the rule; nothing checked it.
    try:
        if len(shots) > 1:
            for r in refs:
                if (r.get("retention") or "") != "partially_copy":
                    continue
                late = sorted(h for h in (r.get("shots") or [1]) if h > 1)
                if not late:
                    continue
                warnings.append(
                    f"@{r['tag']} is a wardrobe plate (`partially_copy`) riding "
                    f"hop(s) {', '.join(str(h) for h in late)}. Whatever else is "
                    f"in that photograph -- its room, its light, a microphone -- "
                    f"rides with the garment and competes with the pin. Give it "
                    f"`shots: [1]` and name the garment in the subject's "
                    f"`context`; the pin carries it after that.")
    except Exception:
        pass

    # `context` is injected VERBATIM on every continuation hop by
    # `refs.continuity_line`, so a posture written into it is asserted on hops
    # whose beats may have the subject doing something else entirely. The pack's
    # own example is wardrobe alone -- "the apron stays tied over the grey
    # t-shirt" -- but nothing said so and nothing checked.
    #
    # Live on 2026-09-02: a wardrobe plate showing her seated at a desk was
    # correctly held to `shots: [1]`, and its POSE rode every hop anyway as
    # "a white ribbed crop top, sitting at a wooden table" while the beats had
    # her walking a train platform. Hop 3 obeyed the context and sat her down.
    # The plate was excluded from the picture channel and leaked through the
    # prose one.
    #
    # Presence is not the fault -- chain_00052 carried "sitting on a wooden
    # counter" against beats that had her sitting on a counter, and it was
    # right. The CONTRADICTION is the fault, so both halves have to be read.
    try:
        _POSTURE = {
            "seated": r"\b(sits?|sitting|seated|perched|kneel\w*|crouch\w*)\b",
            "afoot": r"\b(walk\w*|strolls?|strides?|stands?|standing|runs?|"
                     r"running|paces?|steps? away|moves? past)\b",
        }
        beats_low = " ".join(str((s or {}).get("beat") or "") for s in shots).lower()
        for num, info in sorted((subs or {}).items()):
            ctx = str((info or {}).get("context") or "").lower()
            if not ctx:
                continue
            in_ctx = {k for k, rx in _POSTURE.items() if re.search(rx, ctx)}
            in_beats = {k for k, rx in _POSTURE.items() if re.search(rx, beats_low)}
            clash = in_ctx and in_beats and not (in_ctx & in_beats)
            if clash:
                warnings.append(
                    f"subjects.{num}.context says the subject is "
                    f"{'/'.join(sorted(in_ctx))} while the beats have her "
                    f"{'/'.join(sorted(in_beats))}. `context` is injected on "
                    f"EVERY hop, so a posture in it argues with every beat that "
                    f"disagrees -- and a pose copied from a wardrobe plate is "
                    f"how that plate's scene rides hops it was kept off. Keep "
                    f"`context` to wardrobe and appearance; the beat owns the "
                    f"pose.")
    except Exception:
        pass

    # The mirror hazard, and the one that does not self-correct. Two six-hop
    # renders settled it: the hop scheduled with no face plate came back a
    # different person and nothing after it recovered. `locked` holds a face
    # that is still right; only a plate rebuilds one that is gone.
    try:
        if len(shots) > 1:
            for num in sorted({r["subject"] for r in refs
                               if r.get("subject") is not None}):
                ride = set()
                for r in refs:
                    if r.get("subject") == num:
                        ride |= set(r.get("shots") or [1])
                bare = [h for h in range(1, len(shots) + 1) if h not in ride]
                if bare:
                    warnings.append(
                        f"subject {num} has no picture on hop(s) "
                        + ", ".join(str(h) for h in bare)
                        + ". Identity drift does not self-correct: put the "
                          "likeness plate on every hop.")
    except Exception:
        pass

    # SYSTEM_PROMPT.md rule 13, and its own closing checklist: the final shot
    # sets `tail` to `settle` or `hold`. Left at the default `ongoing`, the
    # chain ends mid-gesture -- the clip stops rather than finishes.
    #
    # A warning and not an error, even though it is mechanical enough to be
    # one. The node does not reject it, and this file's whole doctrine is that
    # `errors` means "the node would refuse this": promoting a prose rule into
    # the retry loop asks the model to rewrite a plan that would have rendered,
    # which is how the parts that were already right get lost. Shown, not
    # retried. `tools/grade_plan.py` is the instrument that grades this tier
    # properly, and it caught this gap.
    try:
        last = (shots[-1].get("directives") or {}).get("tail")
        if last not in ("settle", "hold"):
            warnings.append(
                f"shot {len(shots)} is the last one and leaves `tail` as "
                f"{last or 'the default (ongoing)'}. SYSTEM_PROMPT rule 13 asks "
                f"the final shot for `settle` or `hold`, so the chain closes "
                f"instead of stopping mid-gesture.")
    except Exception:
        pass

    return errors, [str(w) for w in warnings]


def _clean(exc):
    """Validator messages are prefixed for the ComfyUI log. The model does not
    need the prefix, and leaving it in invites it to echo the tag back."""
    msg = str(exc).strip()
    for prefix in (f"[{TAG}] ", f"{TAG}: "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
    return msg


# -- the loop --------------------------------------------------------------

def build_user_turn(brief, hops, files, pinned=None, duration=None):
    """The one user message. Names the files that actually exist, so the model
    schedules pictures it has rather than inventing plausible filenames.

    `pinned` is the REFERENCES rail: those tags and files are locked. The
    folder listing is the fallback for a brief-only write with an empty rail.

    `duration` is the node's hop length. Without it the model gets a five-row
    table and no way to know which row it is writing to -- the prompt tells it
    to ASK, which a button cannot answer, so it guesses and defaults to about
    fifty words whatever the hop length. Naming the band here is the whole
    difference between a table and an instruction.
    """
    lines = [(brief or "").strip() or "A short scene of your choosing.",
             "",
             f"Write exactly {int(hops)} hop(s)."]
    band = beat_table().get(str(duration or "").strip())
    if band:
        w0, w1, l0, l1 = band
        lines.append(
            f"Every hop is {duration}. That is the row of the length table you "
            f"are writing to: {w0}-{w1} words in each beat, and "
            + (f"{l0}-{l1} spoken lines" if l1 > l0 else
               f"{l0} spoken line" + ("" if l0 == 1 else "s"))
            + ". Count both in every beat before you answer.")
        # Lines alone do not size the audio: one runs from six words to twenty,
        # and six words is 2.4 s of a 10 s hop. Live, a 3x10 s vlog written
        # with one six-word line per hop rendered 17.8% voiced, in fragments.
        try:
            from .h3_ref_chain import DURATION_FRAMES, FPS  # noqa: PLC0415
            secs = DURATION_FRAMES.get(str(duration).strip(), 0) / float(FPS or 24)
        except Exception:
            secs = 0
        if secs:
            lines.append(
                f"Speech runs about {SPEECH_WPS:g} words a second, so a "
                f"character who talks for most of a {secs:.0f}s hop needs "
                f"roughly {int(secs * SPEECH_WPS)} words INSIDE the quotes -- "
                f"several sentences, not one. Count the spoken words too. "
                f"A hop where nobody speaks is fine, but say so: name the "
                f"sound the room makes instead. If the lines are in a script "
                f"written in syllable blocks rather than spaced words -- "
                f"Korean, Japanese, Chinese -- count syllables instead and "
                f"aim for about "
                f"{int(secs * SPEECH_SPS)}.")
            lines.append(
                "If anything happens before the first spoken line -- walking "
                "in, sitting down, turning to the camera -- name the sound "
                "those opening seconds carry as well. An opening with a "
                "picture and no sound assigned comes back as invented "
                "dialogue over the action, even when the hop before it ended "
                "silent.")
    pinned = [p for p in (pinned or []) if isinstance(p, dict)]
    if pinned:
        lines += ["",
                  "These pictures are already on the rail. Keep these tags "
                  "and filenames. Do not add other files. Do not rename tags. "
                  "Look at each still: a person gets subject N + "
                  "retention fully_preserved and a subjects.N block; a place "
                  "gets retention reference and no subject.",
                  "",
                  # The rule is in the prompt already; both writers still put
                  # the wardrobe plate on every hop, and its room came with it.
                  "A `partially_copy` ref is a wardrobe plate. Give it "
                  "\"shots\": [1] unless the room in that photograph IS this "
                  "scene -- everything else in it rides along with the garment, "
                  "and after hop 1 the frame pin carries the outfit. Name the "
                  "garment's colours in the subject's `context` instead.",
                  "",
                  "If any ref has \"subject\": N, subjects MUST contain that "
                  "N with `name`, `locked`, and `context` in prose from the "
                  "photograph. Every ref needs `desc` (what the photograph "
                  "shows). An empty \"subjects\": {} is a rejected plan. "
                  "Example:",
                  '  "desc": "a young woman in a green top, facing the camera"',
                  '  "subjects": {"1": {"name": "the young woman", '
                  '"locked": "the same face, the same long dark hair", '
                  '"context": "a green blouse, standing at the counter"}}']
        for p in pinned:
            bit = f"  - @{p.get('tag')}  file={p.get('file')}"
            if p.get("subject") is not None:
                bit += f"  subject={p['subject']}"
            lines.append(bit)
        lines += ["",
                  "Stills of those rows are attached below, labelled with "
                  "their @tag. Look at them."]
    elif files:
        lines += ["",
                  "These reference files are on disk. Use these exact "
                  "filenames, and only these:"]
        lines += [f"  - {f}" for f in files]
    else:
        lines += ["",
                  "There are no reference files on disk. Write the plan "
                  "without a `file` on any reference."]
    return "\n".join(lines)


def attach_images(text, images):
    """Wrap a user turn with OpenAI image_url parts. No images -> the text."""
    images = [im for im in (images or []) if (im or {}).get("data_url")]
    if not images:
        return text
    parts = [{"type": "text", "text": text}]
    for im in images:
        tag = str(im.get("tag") or "").lstrip("@")
        parts.append({"type": "text", "text": f"@{tag} is this picture:"})
        parts.append({"type": "image_url",
                      "image_url": {"url": im["data_url"]}})
    return parts


def _parse_obj(text):
    try:
        obj = json.loads(text or "")
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _is_subjects_patch(ref_text):
    """A reply that only fills subjects, with no refs list to replace."""
    obj = _parse_obj(ref_text)
    if not obj:
        return False
    if "ref_plan" in obj and "shot_plan" not in obj:
        obj = obj.get("ref_plan") or {}
    refs = obj.get("refs") if isinstance(obj, dict) else None
    subs = obj.get("subjects") if isinstance(obj, dict) else None
    return bool(subs) and not refs


def _filled(v):
    return v not in (None, "", [], {})


def _merge_dict(base, patch):
    """Overlay patch onto base; empty values do not wipe filled ones."""
    out = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        elif _filled(v):
            out[k] = v
    return out


def _merge_register(base_text, patch_text, keep=None):
    """Keep tags/files/desc already won; overlay new non-empty fields.

    A repair that re-emits the refs without `desc`, or `subjects: {}`, used
    to replace the previous register and blank the rail's describe/context
    boxes. Empty does not win.
    """
    base = _parse_obj(base_text) or {}
    patch = _parse_obj(patch_text) or {}
    if isinstance(patch.get("ref_plan"), dict):
        patch = patch["ref_plan"]
    by_tag = {}
    for src in (base.get("refs") or []), (patch.get("refs") or []):
        for r in src:
            if not isinstance(r, dict) or not r.get("tag"):
                continue
            tag = r["tag"]
            by_tag[tag] = _merge_dict(by_tag.get(tag) or {}, r)
    if keep:
        # On the pinned path a tag that is not on the rail is not a reference,
        # it is a leftover from an attempt that renamed things. Carrying it
        # forward made every later attempt fail on the FIRST attempt's names.
        by_tag = {t: r for t, r in by_tag.items() if t in keep}
    refs = list(by_tag.values()) if by_tag else (base.get("refs") or [])
    subs = _merge_dict(base.get("subjects") or {}, patch.get("subjects") or {})
    return json.dumps({"refs": refs, "subjects": subs}, indent=2)


# Fields the RAIL owns and the writer never authors. `mp` is a pixel budget the
# person sets per row; it is absent from the valid-fields list in
# SYSTEM_PROMPT.md and the schema tells the model not to author it. Accept
# writes the returned register straight onto the rail, so without this a Write
# plan silently reset every cap to "full": chain_00047 ran three plates at
# 0.54 MP (1.58 MP total against a 0.72 MP canvas) and the next write put the
# same three back at native size, 3.23 MP, which is the run that came back with
# the reference photograph rendered instead of the beat.
RAIL_ONLY_FIELDS = ("mp",)


def _restore_rail_only(ref_text, pinned):
    """Make the rail authoritative for its own per-row fields.

    Authoritative, not merely restorative: a value the rail does not have is
    REMOVED, it is not left as the model wrote it. `mp` is exposed in the
    schema so a hand-authored plan can set one, which means a model can put a
    number there too -- gemma4-26b read the units as pixels and returned
    1000000000, then -1, then 1e+15.

    Runs with an EMPTY rail as well, which is the whole point: `mp` is not the
    writer's field whether or not there are rows to restore from. Gated on
    `pinned` it no-opped on a brief-only write, the invented value reached
    `parse_ref_plan`, and that raises -- which short-circuits every other check
    in `validate`, so all three attempts died on one line about megapixels and
    the missing files were never reported at all.
    """
    obj = _parse_obj(ref_text)
    if not isinstance(obj, dict):
        return ref_text
    by_tag, by_file = {}, {}
    for p in (pinned or []):
        if not isinstance(p, dict):
            continue
        tag = str(p.get("tag") or "").lstrip("@").strip()
        fname = str(p.get("file") or "").strip()
        if tag:
            by_tag[tag] = p
        if fname:
            by_file.setdefault(fname, p)
    touched = False
    for r in (obj.get("refs") or []):
        if not isinstance(r, dict):
            continue
        # Tag first, then filename -- `_remap_pinned_tags` restores rail names
        # by file but only when the model supplied one it can match, and when
        # it does not the row still IS that rail row. Keying on the tag alone
        # let an unrenamed @woman_face keep the mp it invented (1e+15, live)
        # and spend an attempt on a rejection the rail already had the answer
        # to.
        row = (by_tag.get(str(r.get("tag") or "").lstrip("@").strip())
               or by_file.get(str(r.get("file") or "").strip())
               or {})
        # Note the order: DROP first, then set from the rail if it has one. No
        # `continue` on a missing row -- a ref the rail knows nothing about is
        # exactly the case where the model's own number is the only one there,
        # and it is still not the model's field to set.
        for field in RAIL_ONLY_FIELDS:
            value = row.get(field)
            if r.pop(field, None) is not None:
                touched = True
            if value not in (None, "", 0):
                r[field] = value
                touched = True
    return json.dumps(obj, indent=2) if touched else ref_text


def _restore_pinned_files(ref_text, pinned):
    """Make the rail authoritative for `file` on the rows it pins.

    -> (ref_text, {tag: filename}) for whatever had to be put back.

    The rail pins a tag to a picture, so `file` is not the writer's field to
    choose on a pinned row -- and `validate` is already holding the right value
    in `by_tag` at the moment it rejects the plan for not having it. A model
    that has just looked at the photograph names the file after the tag:
    `gibsonlethal.webp` came back as `hero_face.webp`, which is tidy, and wrong.

    That cost two attempts rather than one. `_only_register_prose_gaps` fires
    the tightened-schema repair -- the one that takes the empty path out of the
    grammar -- only when EVERY error is a prose gap, and a file mismatch is not
    one. So the round that could have been repaired properly got the weak
    generic turn instead, and the mechanism that works was delayed until the
    next one. Live 2026-09-02: converged on attempt 3 of 3, one from failing.

    Keyed on a real tag match ONLY, and deliberately NOT a `RAIL_ONLY_FIELD`:
    that loop DROPS a field the rail cannot supply, which is correct for `mp`
    and destructive here. On a brief-only write the rail is empty and the
    model's filename, read off the folder listing, is the only one there.
    """
    obj = _parse_obj(ref_text)
    if not isinstance(obj, dict):
        return ref_text, {}
    by_tag = {}
    for p in (pinned or []):
        if not isinstance(p, dict):
            continue
        tag = str(p.get("tag") or "").lstrip("@").strip()
        fname = str(p.get("file") or "").strip()
        if tag and fname:
            by_tag[tag] = fname
    fixed = {}
    for r in (obj.get("refs") or []):
        if not isinstance(r, dict):
            continue
        tag = str(r.get("tag") or "").lstrip("@").strip()
        want = by_tag.get(tag)
        if want and str(r.get("file") or "").strip() != want:
            r["file"] = want
            fixed[tag] = want
    return (json.dumps(obj, indent=2) if fixed else ref_text), fixed


def _remap_pinned_tags(shot_text, ref_text, pinned):
    """Rename invented tags back to the rail's, matching on `file`.

    The rail pins a tag to a picture. A model that has just looked at that
    picture names it for what it saw -- `@girl_face` for a row the user called
    `@ref_1`, whose file is `cafe_floral_9x16.jpg`. That is the better name and
    the wrong one: Accept writes back onto rail rows keyed by tag, so a renamed
    row lands nowhere.

    The filename is the identity, so a ref whose `file` matches a rail row IS
    that row whatever it calls itself. Rewriting here, before the merge and the
    validate, turns three attempts spent arguing about names into one that only
    has to fill prose. The shot plan is rewritten in the same pass or the tag
    round trip at the bottom of `validate` would break on the way past.
    """
    rows = [p for p in (pinned or []) if isinstance(p, dict)]
    if not rows:
        return shot_text, ref_text, {}

    by_file = {}
    for p in rows:
        f = str(p.get("file") or "").strip()
        t = str(p.get("tag") or "").lstrip("@").strip()
        if f and t:
            by_file.setdefault(f, t)
    rail = {str(p.get("tag") or "").lstrip("@").strip() for p in rows}

    obj = _parse_obj(ref_text)
    if not obj:
        return shot_text, ref_text, {}
    wrapped = isinstance(obj.get("ref_plan"), dict)
    reg = obj["ref_plan"] if wrapped else obj
    refs = reg.get("refs")
    if not isinstance(refs, list):
        return shot_text, ref_text, {}

    have = {str(r.get("tag") or "").lstrip("@").strip()
            for r in refs if isinstance(r, dict)}
    mapping = {}
    for r in refs:
        if not isinstance(r, dict):
            continue
        tag = str(r.get("tag") or "").lstrip("@").strip()
        want = by_file.get(str(r.get("file") or "").strip())
        if not tag or not want or tag in rail or want == tag:
            continue
        # Do not collide with a row that already carries the rail name.
        if want in have or want in mapping.values():
            continue
        mapping[tag] = want
        r["tag"] = want
        have.discard(tag)
        have.add(want)

    if not mapping:
        return shot_text, ref_text, {}

    ref_out = json.dumps({"ref_plan": reg} if wrapped else reg, indent=2)
    shot_out = _sub_tags(shot_text, mapping)
    return shot_out, ref_out, mapping


def _sub_tags(text, mapping):
    """Rewrite @tag citations anywhere in a block, prose and beat alike."""
    if not text or not mapping:
        return text
    return re.sub(r"@([A-Za-z0-9_]+)",
                  lambda m: "@" + mapping.get(m.group(1), m.group(1)),
                  text)


def _only_register_prose_gaps(errors):
    return bool(errors) and all(
        "is missing" in e or "needs `desc`" in e or "continuity text" in e
        for e in errors)


def _missing_subject_nums(errors):
    nums = []
    for e in errors or []:
        m = re.search(r"subject (\d+)", e)
        if m and m.group(1) not in nums:
            nums.append(m.group(1))
    return nums


def _tighten_schema(base, nums):
    """The repair turn's schema, with the empty path removed. -> dict or None.

    Three byte-identical replies proved the point: while the grammar allows
    `"subjects": {}` and a ref without `desc`, that is the cheapest legal
    completion and no amount of repair prose outvotes it. The model was not
    ignoring the instruction, it was following a stronger one.

    So the repair turn gets a grammar where the missing fields are structurally
    mandatory. This is deliberately NOT the shipped schema: a plan with no
    people in it is legitimate, and `minProperties` on subjects would forbid
    it. Here we already know a ref claimed subject N, because `validate` said
    so, so requiring N is a fact about this reply rather than a house rule.

    `patternProperties` is dropped in favour of explicit `properties`; every
    required key is spelled out, which is the shape a GBNF converter handles
    most predictably.
    """
    if not base or not nums:
        return None
    try:
        sch = json.loads(json.dumps(base))
        reg = sch["properties"]["ref_plan"]["properties"]
        item = sch["properties"]["ref_plan"]["properties"]["refs"]["items"]
        req = list(item.get("required") or [])
        if "desc" not in req:
            req.append("desc")
        item["required"] = req
        item.setdefault("properties", {}).setdefault("desc", {})
        item["properties"]["desc"]["type"] = "string"
        item["properties"]["desc"]["minLength"] = 1

        subs = reg["subjects"]
        one = (subs.get("patternProperties") or {}).get("^[0-9]+$")
        one = json.loads(json.dumps(one or {"type": "object"}))
        one["required"] = ["name", "locked", "context"]
        for k in ("name", "locked", "context"):
            one.setdefault("properties", {}).setdefault(k, {"type": "string"})
            one["properties"][k]["type"] = "string"
            one["properties"][k]["minLength"] = 1
        reg["subjects"] = {
            "type": "object",
            "properties": {n: json.loads(json.dumps(one)) for n in nums},
            "required": list(nums),
            "additionalProperties": False,
            "description": subs.get("description", ""),
        }
        return sch
    except Exception as exc:
        print(f"[{TAG}] could not tighten the repair schema: {exc!r}",
              flush=True)
        return None


def _subjects_repair(errors):
    nums = _missing_subject_nums(errors)
    example = {n: {"name": "the young woman",
                   "locked": "the same face, the same long dark hair",
                   "context": "a green blouse, standing at the counter"}
               for n in (nums or ["1"])}
    return (
        "The shot_plan is fine. Do not change tags or files. Do not drop a "
        "desc you already wrote. Fill every ref's `desc` (what the photograph "
        "shows) and every subject's `name`, `locked`, and `context` "
        "(wardrobe and appearance, never a pose or a place -- it is injected "
        "on every hop and the beat owns what they are doing). An empty subjects object "
        "is rejected. Return the full ref_plan.\n\n"
        "Shape, filled from the photographs, prose never an @tag:\n"
        + json.dumps({"subjects": example}, indent=2)
        + "\n\nThe missing fields:\n"
        + "\n".join(f"- {e}" for e in errors)
    )


def _stub_missing_subjects(ref_text):
    """Last resort: name/locked/context/desc so a usable draft is not thrown
    away because the model left the prose boxes empty."""
    try:
        plan = _refs.parse_ref_plan(ref_text)
    except Exception:
        return ref_text
    refs = plan.get("refs") or []
    subs = dict(plan.get("subjects") or {})
    changed = False
    for r in refs:
        if not (r.get("desc") or "").strip():
            r["desc"] = "the reference photograph"
            changed = True
        n = r.get("subject")
        if n is None:
            continue
        info = dict(subs.get(n) or {})
        desc = (r.get("desc") or "").strip()
        if not (info.get("name") or "").strip():
            info["name"] = "the person"
            changed = True
        if not (info.get("locked") or "").strip():
            info["locked"] = desc if desc != "the reference photograph" else "the same face and hair"
            changed = True
        if not (info.get("context") or "").strip():
            info["context"] = "as they stand in the photograph"
            changed = True
        subs[n] = info
    if not changed:
        return ref_text
    out_refs = []
    for r in refs:
        row = {"tag": r["tag"]}
        for k in ("file", "subject", "retention", "desc", "shots", "mp"):
            if r.get(k) not in (None, "", [], 0, 0.0):
                row[k] = r[k]
        out_refs.append(row)
    return json.dumps(
        {"refs": out_refs, "subjects": {str(k): v for k, v in subs.items()}},
        indent=2)


async def write_plan(brief, hops, *, complete_fn, files=None,
                     pinned=None, images=None, duration=None,
                     attempts=MAX_ATTEMPTS, use_schema=True, on_step=None):
    """Generate a plan and repair it until the node would accept it.

    `complete_fn(messages, schema=...)` is injected rather than imported so the
    loop can be driven by a scripted fake in `tools/check_planner.py`. That test
    is the only proof the repair path works that does not need a GPU, a server
    and a person watching.

    `pinned` locks rail tags and filenames. `images` are data-URLs attached
    to the first user turn only; repair turns stay text.

    Never returns an unvalidated plan: if it does not converge, `ok` is False
    and `errors` holds the last set, for the person to fix by hand.
    """
    pinned = [p for p in (pinned or []) if isinstance(p, dict)]
    files = ([p.get("file") for p in pinned if p.get("file")]
             if pinned else list(files or []))
    rail_tags = {str(p.get("tag") or "").lstrip("@").strip()
                 for p in pinned if p.get("tag")}
    text = build_user_turn(brief, hops, files, pinned=pinned, duration=duration)
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": attach_images(text, images)}]
    sch = schema() if use_schema else None
    # The schema in force for the NEXT call. A prose repair swaps in a grammar
    # that cannot spell the empty answer; everything else uses the shipped one.
    turn_sch = sch

    last_errors, warnings = [], []
    shot_text = ref_text = ""

    for attempt in range(1, int(attempts) + 1):
        if on_step:
            on_step({"attempt": attempt, "of": int(attempts),
                     "errors": last_errors})
        reply = await complete_fn(messages, schema=turn_sch)
        new_shots, new_refs = split_reply(reply)
        # A model that looked at the stills names the rows for what it saw.
        # Map those back onto the rail by filename before anything else reads
        # them, so the merge stays keyed on rail tags and the repair turn is
        # free to spend its budget on the prose that is actually missing.
        if new_refs:
            new_shots, new_refs, renamed = _remap_pinned_tags(
                new_shots or shot_text, new_refs, pinned)
            if renamed:
                print(f"[{TAG}] rail tags restored by filename: "
                      + ", ".join(f"@{k} -> @{v}"
                                  for k, v in renamed.items()), flush=True)
        if new_shots:
            shot_text = new_shots
        if new_refs:
            # A later turn that re-emits refs without desc, or subjects: {},
            # must not wipe the register the previous turn already filled.
            if ref_text:
                ref_text = _merge_register(ref_text, new_refs, keep=rail_tags)
            else:
                ref_text = new_refs
            # Before validate, so a restored `mp` is what the lints and the
            # returned plan both see, on the failure path as well as the ok one.
            ref_text = _restore_rail_only(ref_text, pinned)
            # Same argument as `mp` above, and the same place to make it: the
            # rail owns `file` on a row it pins, so a renamed one is repaired
            # here rather than spending an attempt -- and, worse, blocking the
            # tightened-schema repair by not being a prose gap.
            ref_text, refiled = _restore_pinned_files(ref_text, pinned)
            if refiled:
                print(f"[{TAG}] rail filenames restored: "
                      + ", ".join(f"@{t} -> {f}" for t, f in refiled.items()),
                      flush=True)
        if not shot_text:
            last_errors = ["the reply contained no JSON. Answer with the two "
                           "JSON blocks and nothing else."]
        else:
            last_errors, warnings = validate(
                shot_text, ref_text, hops=hops, known_files=files,
                pinned=pinned, duration=duration)
        if not last_errors:
            return {"ok": True, "shot_plan": shot_text, "ref_plan": ref_text,
                    "attempts": attempt, "errors": [], "warnings": warnings}

        print(f"[{TAG}] plan attempt {attempt}/{attempts} rejected: "
              + "; ".join(last_errors[:3]), flush=True)
        if attempt >= int(attempts):
            break
        # Feed the reply back with the node's own words. Keeping the assistant
        # turn matters: without it the model re-derives the plan from scratch
        # and reliably reintroduces a different fault.
        messages.append({"role": "assistant", "content": reply})
        if _only_register_prose_gaps(last_errors):
            # qwen left "subjects": {} three times when asked to rewrite the
            # whole plan -- then three byte-identical times when asked again
            # with the images still in context. Ask only for the missing
            # block, and take the empty answer out of the grammar.
            repair = _subjects_repair(last_errors)
            turn_sch = _tighten_schema(
                sch, _missing_subject_nums(last_errors)) or sch
            if turn_sch is not sch:
                print(f"[{TAG}] repair turn: desc and subject prose are "
                      f"required by the schema", flush=True)
        else:
            turn_sch = sch
            repair = (
                "The node rejected that plan:\n\n"
                + "\n".join(f"- {e}" for e in last_errors)
                + "\n\nReturn the two corrected JSON blocks. Change "
                  "only what the errors name; leave the rest as it "
                  "was."
            )
        messages.append({"role": "user", "content": repair})

    # A usable script with empty subjects is not a throw-away. Fill name/locked
    # from each ref's own desc so Accept has something, and say so.
    if shot_text and ref_text and _only_register_prose_gaps(last_errors):
        stub = _stub_missing_subjects(ref_text)
        errs, warns = validate(
            shot_text, stub, hops=hops, known_files=files, pinned=pinned)
        if not errs:
            note = ("subjects were filled from the photo descriptions because "
                    "the model left them empty; read them before Accept.")
            return {"ok": True, "shot_plan": shot_text, "ref_plan": stub,
                    "attempts": int(attempts), "errors": [],
                    "warnings": list(warns) + [note]}

    return {"ok": False, "shot_plan": shot_text, "ref_plan": ref_text,
            "attempts": int(attempts), "errors": last_errors,
            "warnings": warnings}
