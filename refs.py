"""Reference register: stable @tags, subject numbers, retention tiers.

Two bugs this exists to fix.

**Ordinal instability.** Core assigns `<Picture N>` 1-based in list order
(`comfy/text_encoders/minimax.py:148-202`), and `_collect_ref_images` packs the
wired slots densely. Wire slots 1/3/5 and they become `<Picture 1/2/3>`; unplug
one and every later reference silently renumbers. Any prompt text naming a
literal ordinal is then pointing at the wrong picture. Here a ref carries a
stable `@tag` and the ordinal is *derived per hop*, so prompt prose refers to
tags and the compiler resolves them.

**Subject collapse.** H3-Multishot found that declaring every picture as a photo
of `<Subject 1>` makes the model render the average of two different people.
Subject numbers group pictures per person and state distinctness explicitly.

Phrasing here follows the same rule as `directives.py`: AFFIRMATIVE ONLY.
Sampling runs at cfg 1.0 with no negative branch, so every concept named is
additive. State what each picture IS for.
"""

from os.path import basename as _basename

TAG = "HandTieClips"

MAX_REF_IMAGES = 9

# What the model should do with a picture. Wording is deliberately about the
# *carry-over*, not about the picture, because the model is being told how much
# of it to reproduce.
RETENTION = {
    "fully_preserved": "face, bone structure, and hairstyle carry over exactly",
    "partially_copy": "the garment and its cut carry over, moving naturally with the body",
    "reference": "the layout, surfaces, and light carry over as the setting",
}

# `file` is the reference: a basename under <ComfyUI input>/h3_refs. `slot` is
# still accepted, and still *emitted*, but it means something different now --
# it is derived from the ref's position in the list rather than naming a socket,
# and it exists only so the ordinal machinery below did not have to change.
# A plan from before the sockets were removed carries an authored `slot` and no
# `file`; that is detected, kept as `legacy_slot`, and reported by check().
REF_FIELDS = ("tag", "file", "slot", "subject", "retention", "desc", "shots",
              "mp", "source", "strength", "voice")

# A video RefMod shares the encoder's three video slots with the MEDIA clips
# and, on a continuation, the pin clip. Image rows share the nine picture slots.
MAX_REF_VIDEOS = 3

# `voice` is an audio basename under h3_refs. It becomes a standalone `audio`
# block beside the row's picture or clip, cited by one timbre sentence naming
# the subject. A video row once took it as the clip's soundtrack (one fused
# `video_audio` block); the model read that as "this person saying these
# words" and recited the recording before the written line (2026-09-23).
#
# It rides the hops the row rides, and on hop 2+ of a `next` chain only as
# `voice_every_hop` lets `voice_file` ride -- a quiet hop fills its leftover
# frames with whatever recording it is shown.
MAX_REF_AUDIOS = 3
SOURCES = ("image", "refmod")

# Per-reference pixel budget, in megapixels. 0 (or absent) means no cap.
#
# This is a TOKEN dial, not a quality one. H3 turns each reference into
# `latent_h * latent_w` entries in the DiT payload and attends over all of them
# on every step of every hop, so a location plate costing what a face costs is
# waste. The floor is a picture you can still recognise a room in.
#
# The ceiling is a sanity rail, not a capability limit: H3 only ever scales a
# reference DOWN (`min(1.0, ...)` in nodes_minimax_h3.py), so any cap above the
# file's own size is already a dial wired to nothing and does no harm at render
# time. It exists because a *model* can author this field, and one read the
# units as pixels: gemma4-26b returned mp 1000000000, which passed the floor,
# survived into the register and rendered in the rail as a dropdown full of
# zeroes. Anything past this is a units mistake, not an intention.
REF_MP_MIN = 0.3
REF_MP_MAX = 16.0

# Per-subject continuity text -- the half `HTCContinuityState` owned, moved here
# so it is keyed by the same subject number that owns the picture ordinals.
#
# The old node could not express a second character at all: `continuity_state`
# is a single forceInput STRING and the node emits exactly one `characters` key
# from one `character_id` widget, so two people were unrepresentable (its own
# docstring says so). Worse, its prose named people by an arbitrary string while
# `<Picture N>` was assigned positionally by slot order, with nothing joining
# the two -- so with two people at two refs each, `_identity_lock` emitted one
# undifferentiated pool of four "identities" and the model was free to average
# the faces. Keying continuity text by subject number closes that join.
SUBJECT_FIELDS = ("name", "locked", "context")


def _fail(msg):
    raise ValueError(f"{TAG}: {msg}")


def _norm_subject(key, raw):
    """Validate one subject entry. Errors name the subject and valid fields."""
    try:
        num = int(key)
    except (TypeError, ValueError):
        _fail(f"subject key {key!r} is not a number. Use the same subject numbers "
              f"the refs use, e.g. \"1\".")
    if num < 1:
        _fail(f"subject {key}: subject numbers start at 1")
    if not isinstance(raw, dict):
        _fail(f"subject {num}: expected an object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - set(SUBJECT_FIELDS))
    if unknown:
        _fail(f"subject {num}: unknown field(s) {unknown}. "
              f"Valid: {list(SUBJECT_FIELDS)}")
    out = {
        "name": str(raw.get("name") or "").strip(),
        "locked": str(raw.get("locked") or "").strip(),
        "context": str(raw.get("context") or "").strip(),
    }
    # All three fields are PROSE that reaches the encoder verbatim -- `name`
    # is what `resolve_tags` substitutes for a subject's tag on hop 2+, and
    # `locked`/`context` are copied into the continuity line. A reference tag
    # written here therefore resolves to itself: a model that wrote
    # `"name": "@cook_face"` put a literal at-sign into the conditioning of
    # every continuation hop, and nothing anywhere caught it -- the plan
    # parsed, the tags resolved, the render completed, and two of three hops
    # were conditioned on a token no one intended. Loud here, or silent
    # forever.
    import re                       # local, as everywhere else in this module
    for field, text in out.items():
        hit = re.search(r"@[A-Za-z0-9_]+", text)
        if hit:
            _fail(f"subject {num}: `{field}` contains the reference tag "
                  f"'{hit.group(0)}'. These fields are prose that reaches the "
                  f"text encoder literally -- @tags only resolve inside a "
                  f"shot's beat. Write the plain words instead, e.g. "
                  f"\"the cook\".")
    return num, out


def _norm_ref(raw, i):
    """Validate one ref entry. Errors name the ref and the valid options."""
    where = f"ref {i + 1}"
    if not isinstance(raw, dict):
        _fail(f"{where}: expected an object, got {type(raw).__name__}")

    unknown = sorted(set(raw) - set(REF_FIELDS))
    if unknown:
        _fail(f"{where}: unknown field(s) {unknown}. Valid: {list(REF_FIELDS)}")

    tag = str(raw.get("tag") or "").strip().lstrip("@")
    if not tag:
        _fail(f"{where}: 'tag' is required (a short stable name like 'hero_face')")

    source = str(raw.get("source") or "image").strip().lower()
    if source not in SOURCES:
        _fail(f"{where} (@{tag}): source '{source}' is not valid. "
              f"Use one of: {', '.join(SOURCES)}")

    # A still is a bare filename. media.resolve refuses a directory component,
    # so accepting one here would only produce a ref that silently never loads.
    # A RefMod name may include a subfolder (`people/hero`) because that is
    # how the refmods folder is laid out. `..` is still refused.
    raw_file = str(raw.get("file") or "").strip()
    if source == "refmod":
        file = raw_file.replace("\\", "/").strip("/")
        if file.lower().endswith(".safetensors"):
            file = file[: -len(".safetensors")]
        if file and any(p in ("", ".", "..") for p in file.split("/")):
            _fail(f"{where} (@{tag}): refmod file {raw_file!r} leaves the "
                  f"refmods folder")
    else:
        file = _basename(raw_file)

    # A voice is a plain basename like a still: media.resolve refuses a
    # directory component, so a path here would only ever fail to load.
    voice = _basename(str(raw.get("voice") or "").strip())

    strength = raw.get("strength", 1.0)
    if strength in (None, ""):
        strength = 1.0
    try:
        strength = float(strength)
    except (TypeError, ValueError):
        _fail(f"{where} (@{tag}): strength {strength!r} is not a number")
    if strength < 0.0 or strength > 1.0:
        _fail(f"{where} (@{tag}): strength must be between 0 and 1")

    # Legacy: a plan authored against the ref_image_N sockets. Not an error --
    # failing here would stop the editor opening the very plan the author needs
    # to repair, so it is carried through and reported by check().
    legacy_slot = None
    if not file and raw.get("slot") is not None:
        try:
            legacy_slot = int(raw["slot"])
        except (TypeError, ValueError):
            legacy_slot = None

    subject = raw.get("subject")
    if subject is not None:
        try:
            subject = int(subject)
        except (TypeError, ValueError):
            _fail(f"{where} (@{tag}): subject {subject!r} is not a number")
        if subject < 1:
            _fail(f"{where} (@{tag}): subject must be 1 or greater")

    retention = str(raw.get("retention") or "").strip()
    if not retention:
        # A ref tied to a person defaults to identity; anything else is setting.
        retention = "fully_preserved" if subject else "reference"
    if retention not in RETENTION:
        _fail(f"{where} (@{tag}): retention '{retention}' is not valid. "
              f"Use one of: {', '.join(sorted(RETENTION))}")

    # Absent and 0 are the same thing -- no cap -- so an author who never
    # opens the rail is never asked to think about this.
    mp = raw.get("mp")
    if mp in (None, "", 0, 0.0):
        mp = 0.0
    else:
        try:
            mp = float(mp)
        except (TypeError, ValueError):
            _fail(f"{where} (@{tag}): mp {mp!r} is not a number")
        if mp < REF_MP_MIN:
            _fail(f"{where} (@{tag}): mp {mp:g} is below the {REF_MP_MIN:g} MP "
                  f"floor. Use 0 for no cap.")
        if mp > REF_MP_MAX:
            _fail(f"{where} (@{tag}): mp {mp:g} is above the {REF_MP_MAX:g} MP "
                  f"ceiling -- this field is MEGApixels, not pixels. Use 0 for "
                  f"no cap.")

    shots = raw.get("shots")
    if shots is not None:
        if not isinstance(shots, (list, tuple)):
            _fail(f"{where} (@{tag}): 'shots' must be a list of 1-based shot "
                  f"numbers, e.g. [1, 2]")
        clean = []
        for s in shots:
            try:
                clean.append(int(s))
            except (TypeError, ValueError):
                _fail(f"{where} (@{tag}): shot number {s!r} is not a number")
        if not clean:
            _fail(f"{where} (@{tag}): 'shots' is empty. Omit it for every shot.")
        shots = sorted(set(clean))

    return {
        "tag": tag,
        "file": file,
        "legacy_slot": legacy_slot,
        # Filled in by parse_ref_plan from list position; see REF_FIELDS.
        "slot": 0,
        "subject": subject,
        "retention": retention,
        "desc": str(raw.get("desc") or "").strip(),
        "shots": shots,
        "mp": mp,
        "source": source,
        "strength": strength,
        "voice": voice,
    }


def parse_ref_plan(text):
    """Parse the ref_plan widget.

    Returns {"refs": [...], "subjects": {n: {...}}}. Blank -> empty plan, and
    the caller falls back to raw slot order.
    """
    import json

    t = (text or "").strip()
    if not t:
        return {"refs": [], "subjects": {}}
    try:
        data = json.loads(t)
    except json.JSONDecodeError as e:
        _fail(f"ref_plan is not valid JSON: {e}")

    raw_subjects = {}
    if isinstance(data, list):
        raw_refs = data
    elif isinstance(data, dict):
        raw_refs = data.get("refs")
        if raw_refs is None:
            _fail("ref_plan object needs a 'refs' array")
        raw_subjects = data.get("subjects") or {}
        if not isinstance(raw_subjects, dict):
            _fail("ref_plan 'subjects' must be an object keyed by subject number, "
                  "e.g. {\"1\": {\"name\": \"...\", \"locked\": \"...\"}}")
    else:
        _fail(f"ref_plan must be an object or array, got {type(data).__name__}")

    if not isinstance(raw_refs, (list, tuple)):
        _fail("ref_plan 'refs' must be an array")

    refs = [_norm_ref(r, i) for i, r in enumerate(raw_refs)]
    _check_hop_load(refs)
    # The ordinal is derived from list position, which is what the author sees
    # in the rail. This keeps `active_refs`/`ordinals` unchanged while the
    # authored identity moves from a socket number to a filename.
    for i, r in enumerate(refs):
        r["slot"] = i + 1
    subjects = dict(_norm_subject(k, v) for k, v in raw_subjects.items())

    # A subject block that no ref points at is continuity text for a person with
    # no face wired -- it would ride every hop describing someone who never
    # appears, which at cfg 1.0 is purely additive.
    used = {r["subject"] for r in refs if r["subject"] is not None}
    for num in sorted(set(subjects) - used):
        _fail(f"subject {num} has continuity text but no ref claims subject "
              f"{num}. Give one of the refs \"subject\": {num}, or remove the "
              f"subject block.")

    seen_tags = {}
    for i, r in enumerate(refs):
        if r["tag"] in seen_tags:
            _fail(f"ref {i + 1}: tag '@{r['tag']}' is already used by ref "
                  f"{seen_tags[r['tag']] + 1}. Tags must be unique.")
        seen_tags[r["tag"]] = i
    return {"refs": refs, "subjects": subjects}


def _is_video(ref):
    return ref.get("kind") == "video" or ref.get("source_kind") == "video"


def _check_hop_load(refs):
    """Fail if any single shot would carry more stills than one encode takes.

    RefMod rows are not counted here. Their kind (image or video) is a
    property of the file, which this parser does not open. `check_kind_caps`
    runs once the files have been read.

    The limit is per ENCODE, not per plan -- `active_refs` is what decides who
    turns up at each one. This counted the whole rail until 1.1, so twelve
    references scheduled three to a shot across four shots were refused against
    a ceiling no shot came near.

    `shots: null` is counted on every shot, which is the worst case rather than
    always the truth: under `hop_script=next` an unscheduled still is dropped
    from continuation hops, so it really only rides hop 1. Counting it
    everywhere can only refuse a plan that would have been fine in `next` mode,
    never accept one that would overflow -- and the ceiling is a hard encoder
    limit, so erring toward refusing is the right direction.
    """
    picture_refs = [r for r in refs if r.get("source") != "refmod"]
    refs = picture_refs
    always = [r for r in refs if r["shots"] is None]
    by_shot = {}
    for r in refs:
        for h in (r["shots"] or []):
            by_shot.setdefault(h, []).append(r)

    worst_shot, worst = None, len(always)
    for h in sorted(by_shot):
        if len(always) + len(by_shot[h]) > worst:
            worst_shot, worst = h, len(always) + len(by_shot[h])
    if worst > MAX_REF_IMAGES:
        where = f"shot {worst_shot}" if worst_shot is not None else "every shot"
        rides = (f" {len(always)} of them have no shots set, so they are "
                 "counted on every shot." if always else "")
        _fail(f"{where} would carry {worst} references; one encode takes at "
              f"most {MAX_REF_IMAGES}.{rides}")
    # A hop that is already full has no room for the incoming frame, and
    # _attach_pin_to_qwen drops it rather than a still -- which turns a
    # continuation into a fresh generate with only the invisible latent pin
    # holding it. Better said here, once, than discovered as a hard cut.
    if worst == MAX_REF_IMAGES:
        where = f"shot {worst_shot}" if worst_shot is not None else "every shot"
        print(f"[{TAG}] note: {where} carries the full {MAX_REF_IMAGES} "
              "references, leaving no slot for the incoming frame. On a "
              "continuation hop pin_to_qwen will be skipped there; drop one "
              "reference if you want the pin shown to the encoder.", flush=True)


def check_kind_caps(refs, media_videos=0):
    """Fail if one hop's loaded refs exceed the picture or video slot cap.

    Call after each refmod row has `kind` set from its file. Plain stills
    count as pictures. `media_videos` is how many MEDIA clips already occupy
    video slots on every hop.
    """
    media_videos = int(media_videos or 0)
    pictures = [r for r in refs if not _is_video(r)]
    videos = [r for r in refs if _is_video(r)]

    def _worst(rows):
        always = [r for r in rows if r["shots"] is None]
        by_shot = {}
        for r in rows:
            for h in (r["shots"] or []):
                by_shot.setdefault(h, []).append(r)
        worst_shot, worst = None, len(always)
        for h in sorted(by_shot):
            n = len(always) + len(by_shot[h])
            if n > worst:
                worst_shot, worst = h, n
        return worst_shot, worst

    shot, n = _worst(pictures)
    if n > MAX_REF_IMAGES:
        where = f"shot {shot}" if shot is not None else "every shot"
        _fail(f"{where} would carry {n} pictures; one encode takes at most "
              f"{MAX_REF_IMAGES}.")
    shot, n = _worst(videos)
    if media_videos + n > MAX_REF_VIDEOS:
        where = f"shot {shot}" if shot is not None else "every shot"
        _fail(f"{where} would carry {n} video RefMod(s) on top of "
              f"{media_videos} reference clip(s); one encode takes at most "
              f"{MAX_REF_VIDEOS} videos.")


def check_audio_caps(refs, media_voices=0):
    """Fail if one hop would carry more standalone voices than H3 accepts.

    Every row voice spends one, a video row's included: it is never fused
    onto the clip as a soundtrack, because that made the model recite it.

    `media_voices` is how many `voice_file` widgets are filled; those ride
    every hop the gate lets them ride, so they count as always-on.
    """
    rows = [r for r in refs if r.get("voice")]
    always = [r for r in rows if r["shots"] is None]
    by_shot = {}
    for r in rows:
        for h in (r["shots"] or []):
            by_shot.setdefault(h, []).append(r)
    worst_shot, worst = None, len(always)
    for h in sorted(by_shot):
        n = len(always) + len(by_shot[h])
        if n > worst:
            worst_shot, worst = h, n
    media_voices = int(media_voices or 0)
    if media_voices + worst > MAX_REF_AUDIOS:
        where = f"shot {worst_shot}" if worst_shot is not None else "every shot"
        _fail(f"{where} would carry {worst} row voice(s) on top of "
              f"{media_voices} voice_file(s); one encode takes at most "
              f"{MAX_REF_AUDIOS} standalone voices. Take a voice off a row "
              f"or schedule the rows onto different shots.")


def active_refs(refs, hop_index, wired_slots):
    """Refs live on this hop, in rail order, restricted to those whose file loaded.

    `hop_index` is 0-based; `shots` in the plan is 1-based because that is what
    the shot plan shows the author.
    """
    shot_no = hop_index + 1
    out = []
    for r in refs:
        if r["slot"] not in wired_slots:
            continue
        if r["shots"] is not None and shot_no not in r["shots"]:
            continue
        out.append(r)
    return sorted(out, key=lambda r: r["slot"])


def select_for_shot(refs, tags, wired_slots):
    """The stills a shot.refs list named, in that list's order.

    Order is the author's Picture 1, 2, 3 -- not rail order. Putting the
    room first is how you stop identity stills winning the middle of a
    pin-less hop. Tags the register does not have are the caller's
    problem (`plan.validate_shot_refs` refuses them on the queue). A
    tag whose file did not load is skipped, same as active_refs.
    """
    by_tag = {r["tag"]: r for r in refs}
    out = []
    seen = set()
    for t in tags or []:
        if t in seen:
            continue
        seen.add(t)
        r = by_tag.get(t)
        if r is None:
            continue
        if r["slot"] not in wired_slots:
            continue
        out.append(r)
    return out


def ordinals(active):
    """tag -> `<Picture N>` ordinal for this hop.

    This is the whole point: the ordinal is derived from what is active on this
    hop, so prose written against @tags stays correct when refs are added,
    removed, or scheduled off.
    """
    return {r["tag"]: i + 1 for i, r in enumerate(active)}


def resolve_tags(text, tag_map, subject_map=None, where="", declared=None,
                 subject_names=None, video_map=None):
    """Replace `@tag` in prose with what this hop calls that reference.

    A ref tied to a person resolves to `<Subject N>`, not `<Picture N>`: beat
    prose describes someone *acting*, and a picture cannot act. The
    `subject_definitions:` block binds the subject to its pictures, so the
    identity still lands. Setting and prop refs have no subject and resolve to
    their picture ordinal.

    `subject_map` may include people whose photograph is off this hop
    (continuation: pin-only). Those tags still resolve to `<Subject N>`.

    An unknown tag is a hard error -- silently leaving `@hero_face` in the
    prompt would put the literal word in front of the encoder, which at cfg 1.0
    is additive noise.

    `subject_names` maps subject number -> the name to use in prose, and is
    passed on **continuation hops only**. There, `<Subject N>` is a dangling
    token: `subject_definitions:` is hop-1 material, so on hop 4 the ordinal has
    nothing in its own encode to bind to -- the same defect that turned an
    undescribed "the bowl" into a stainless steel one. A name binds to the
    identity sentence `continuity_line` puts on every continuation hop, so the
    prose reads "The cook walks down the hallway" and every word is anchored.
    Without a name for that subject it falls back to `<Subject N>`, which is no
    worse than before.

    `declared` is every tag in the register, active on this hop or not. A tag
    that is in it has been spelled correctly and the fault lies elsewhere --
    a missing picture, or a `shots` list that leaves this hop out. Reporting
    that as "unknown reference" sent authors hunting through their beats for a
    typo that was never there.
    """
    import re

    t = str(text or "")
    if "@" not in t:
        return t
    subject_map = subject_map or {}
    declared = declared or set()
    video_map = video_map or {}

    def sub(m):
        tag = m.group(1)
        # A person tag stays <Subject N> even when that photograph is off this
        # hop (continuation: the pin carries wardrobe and room, the still does
        # not). Requiring the tag in tag_map first made @hero_face a hard error
        # the moment hop 2 dropped the face plate.
        if tag in subject_map:
            num = subject_map[tag]
            name = str((subject_names or {}).get(num) or "").strip()
            if name:
                # Sentence-initial gets a capital: "@hero_face walks" became
                # "the cook walks" mid-paragraph, which reads as a fragment.
                head = t[:m.start()].rstrip()
                if not head or head[-1] in '.!?:;':
                    return name[0].upper() + name[1:]
                return name
            return f"<Subject {num}>"
        if tag in video_map:
            return f"<Video {video_map[tag]}>"
        if tag in tag_map:
            return f"<Picture {tag_map[tag]}>"
        w = where + ": " if where else ""
        if tag in declared:
            _fail(f"{w}@{tag} is in the reference register but is not on this "
                  f"hop. Either its file is missing, or its `shots` list "
                  f"leaves this hop out.")
        known = ", ".join("@" + k for k in sorted(set(tag_map) | set(subject_map))) or "(none)"
        _fail(f"{w}unknown reference '@{tag}'. Known: {known}")

    return re.sub(r"@([A-Za-z0-9_]+)", sub, t)


def subjects(active):
    """tag -> subject number, for refs that name a person."""
    return {r["tag"]: r["subject"] for r in active if r["subject"] is not None}


def subject_prose(active, subjects=None, video_ords=None):
    """Build `subject_definitions:` and `retention_analysis:` for this hop.

    Pictures are grouped by subject number so two people stay two people, and
    each subject's continuity text is emitted *inside its own block*, directly
    after the ordinals that subject owns. That adjacency is the whole fix: the
    old split put identity prose in one place (keyed by an arbitrary string) and
    picture ordinals in another (assigned positionally), so the encoder was
    never told which face belonged to which description.

    A ref with no subject is a setting or prop and appears only in retention.

    **A subject is not always a picture.** A character RefMod assembled from
    several stills is stored and encoded as one clip, so the reference the
    encoder receives is `<Video N>` -- and it is still that person. Each
    subject-bearing ref is cited at whichever slot it actually occupies;
    citing `<Picture 2>` for a reference that arrived as a video defines the
    subject against nothing, which is worse than leaving it undefined.
    """
    if not active:
        return ""

    subjects = subjects or {}
    pictures = [r for r in active if not _is_video(r)]
    ords = ordinals(pictures)
    video_ords = video_ords or {}
    cites = {}
    for r in active:
        if _is_video(r):
            pos = video_ords.get(r["tag"])
            cites[r["tag"]] = f"<Video {pos}>" if pos is not None else None
        else:
            pos = ords.get(r["tag"])
            cites[r["tag"]] = f"<Picture {pos}>" if pos is not None else None
    by_subject = {}
    for r in active:
        # No ordinal means the reference is not in this hop's payload, so
        # there is nothing to define the subject against.
        if r["subject"] is not None and cites.get(r["tag"]):
            by_subject.setdefault(r["subject"], []).append(r)

    parts = []
    if by_subject:
        lines = []
        for subj in sorted(by_subject):
            pics = ", ".join(cites[r["tag"]] for r in by_subject[subj])
            info = subjects.get(subj) or {}
            name = info.get("name")
            who = f"{name}, the person in {pics}" if name else f"the person in {pics}"
            lines.append(f"<Subject {subj}> is {who}.")
            for extra in (info.get("locked"), info.get("context")):
                if extra:
                    lines.append(f"<Subject {subj}>: {extra}")
        if len(by_subject) > 1:
            names = ", ".join(f"<Subject {s}>" for s in sorted(by_subject))
            lines.append(
                f"{names} are separate people. Each one keeps the face from its "
                f"own pictures throughout."
            )
        parts.append("subject_definitions:\n" + "\n".join(lines))

    parts.append(retention_prose(active, ords, video_ords))

    return "\n\n".join(parts)


def retention_prose(active, ords=None, video_ords=None):
    """`retention_analysis:` alone, at the ordinals this hop actually uses.

    Split out of `subject_prose` so a continuation hop can emit it too. This
    block is what tells the encoder what a picture is FOR, and it used to be
    hop-1 only because `subject_prose` is -- so a still scheduled onto hop 3
    arrived as an uncited image with no stated role. A Ref2VA model handed a
    photograph and no reason for it renders the photograph: that is the
    "pinning outfit to anything but the first hop throws the actual image in"
    report.

    `ords` is passed in rather than derived. On hop 2+ the pinned live frame is
    <Picture 1> and every still shifts up by one; deriving the map here would
    reproduce the off-by-one the caller has already solved once, which is
    exactly the drift the comment above `hop_ords` warns about.
    """
    if not active:
        return ""
    ords = ords if ords is not None else ordinals(active)
    video_ords = video_ords or {}
    ret = []
    for r in active:
        # A tag with no ordinal is not in this hop's payload at all. Citing a
        # <Picture N> the encoder cannot see is worse than saying nothing.
        if _is_video(r):
            pos = video_ords.get(r["tag"])
            pic = f"<Video {pos}>" if pos is not None else None
        else:
            pos = ords.get(r["tag"])
            pic = f"<Picture {pos}>" if pos is not None else None
        if pic is None:
            continue
        detail = RETENTION[r["retention"]]
        desc = f" ({r['desc']})" if r["desc"] else ""
        ret.append(f"{pic}{desc}: {detail}.")
    if not ret:
        return ""
    return "retention_analysis:\n" + "\n".join(ret)


def continuity_line(subjects, subject_nums=None):
    """Identity prose for a hop that cites no photographs.

    `subject_prose` binds continuity text to picture ordinals, which makes it
    hop-1 material: repeating a `<Subject N> is the person in <Picture M>`
    block on a later hop makes that hop a second Ref2VA generate. But the text
    itself -- `locked` and `context` -- is not about a photograph. It is what
    must not change, and a hop with no reference scheduled is exactly where the
    encoder has nothing else to go on.

    So this emits the same text with **no ordinals of any kind**. No
    `<Picture N>` (which sends the encoder back to the plates -- chain_00034)
    and no `<Subject N>` (which would dangle, with no definitions block on this
    hop to bind it). One person is "The same person"; several are named, and
    fall back to "one person" only when a subject has no name.

    Returns "" when nothing is worth saying, so callers can `if p` it away.
    """
    subjects = subjects or {}
    nums = sorted(subject_nums if subject_nums is not None else subjects)
    nums = [n for n in nums if (subjects.get(n) or {})]
    if not nums:
        return ""

    solo = len(nums) == 1
    lines = []
    for num in nums:
        info = subjects.get(num) or {}
        bits = [t.strip() for t in (info.get("locked"), info.get("context"))
                if t and t.strip()]
        if not bits:
            continue
        # "The same person" reads as continuation; a bare name reads as an
        # introduction, which on a continuation hop is a new scene. With more
        # than one subject there is no way round naming them, and a `name`
        # carries its own article in practice ("the cook") -- so it is used as
        # written rather than glued behind "The same", which produced "The same
        # the cook continues".
        name = (info.get("name") or "").strip()
        if solo or not name:
            who = "The same person"
        else:
            who = name[0].upper() + name[1:]
        body = "; ".join(b.rstrip(".") for b in bits)
        lines.append(f"{who} continues, with {body}.")
    return " ".join(lines)


def describe(plan):
    """Console-auditable summary, printed once at chain start."""
    refs = plan.get("refs") or []
    subjects = plan.get("subjects") or {}
    if not refs:
        return "(no ref plan)"
    out = []
    for num in sorted(subjects):
        info = subjects[num]
        label = info.get("name") or "(unnamed)"
        out.append(f"  <Subject {num}> {label}")
    for r in refs:
        subj = f"subject {r['subject']}" if r["subject"] else "setting"
        when = "all shots" if r["shots"] is None else "shots " + ",".join(
            str(s) for s in r["shots"])
        src = r["file"] or (f"(was slot {r['legacy_slot']}, no file)"
                            if r["legacy_slot"] else "(no file)")
        if r.get("source") == "refmod":
            src = "refmod:" + src
            if float(r.get("strength") or 1.0) != 1.0:
                src += f" @{float(r['strength']):g}"
        line = (f"  @{r['tag']:<16} {src:<28} {subj:<10} "
                f"{r['retention']:<16} {when}")
        if r.get("voice"):
            # Always its own audio block now, a video mod included: fused as
            # a clip soundtrack, the model recited the recording.
            line += "  + voice " + r["voice"] + " (beside)"
        out.append(line)
    return "\n".join(out)


def missing_files(plan, wired_slots):
    """Refs that name a picture which did not load. Fatal, unlike `check`.

    A ref with no `file` at all is a legitimate half-built plan -- the Starter
    ships that way on purpose, so it runs before you have supplied anything. A
    ref that *names* `ref_face.jpg` when no such file is in `h3_refs` is a typo
    or a missing asset, and there is no reading of it under which rendering six
    hops without that picture is what the author wanted: it produces exactly the
    uncontrolled output the register exists to prevent, and the only signal used
    to be one console line under the sampler output.

    Returns a list of `(tag, file)`. The caller raises; this stays free of disk
    access, like `check`.
    """
    out = []
    for r in plan.get("refs") or []:
        if r["slot"] in wired_slots or not r["file"]:
            continue
        out.append((r["tag"], r["file"]))
    return out


def check(plan, wired_slots):
    """Warn (never raise) about refs whose picture is missing or never chosen.

    `wired_slots` is the set of derived slots whose file actually loaded, so
    the caller does the resolving and this stays free of disk access.
    """
    refs = plan.get("refs") or []
    subjects = plan.get("subjects") or {}
    warnings = []
    # A person with pictures but no continuity text still renders, but nothing
    # carries their state across the seam -- which is the failure this register
    # exists to fix, so it is worth naming.
    for num in sorted({r["subject"] for r in refs if r["subject"] is not None}):
        if num not in subjects:
            warnings.append(
                f"subject {num} has pictures but no continuity text. Add "
                f"subjects.{num}.locked to carry their identity across seams.")
    for r in refs:
        if r["slot"] in wired_slots:
            continue
        if r["legacy_slot"]:
            warnings.append(
                f"@{r['tag']} was wired to ref_image_{r['legacy_slot']}, which no "
                f"longer exists. Pick its picture in the REFERENCES rail -- this "
                f"ref is inactive until you do.")
        elif not r["file"]:
            warnings.append(
                f"@{r['tag']} has no picture chosen. This ref is inactive.")
        else:
            folder = ("models/refmods" if r.get("source") == "refmod"
                      else "the reference folder")
            warnings.append(
                f"@{r['tag']} names '{r['file']}', which is not in {folder} "
                f"or could not be read. This ref is inactive.")
    return warnings
