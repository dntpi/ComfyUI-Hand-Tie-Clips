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

def validate(shot_text, ref_text, *, hops=None, known_files=None, pinned=None):
    """Run the node's own checkers. Returns (errors, warnings).

    `errors` are what the node would REJECT -- they go back to the model.
    `warnings` are the lints the node prints and keeps going on; they are shown
    to the person but never retried, because a model asked to fix a lint it
    disagrees with tends to rewrite the parts that were fine.
    """
    from .h3_ref_chain import DURATION_FRAMES

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
                f"wardrobe and pose as they stand now.")

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

def build_user_turn(brief, hops, files, pinned=None):
    """The one user message. Names the files that actually exist, so the model
    schedules pictures it has rather than inventing plausible filenames.

    `pinned` is the REFERENCES rail: those tags and files are locked. The
    folder listing is the fallback for a brief-only write with an empty rail.
    """
    lines = [(brief or "").strip() or "A short scene of your choosing.",
             "",
             f"Write exactly {int(hops)} hop(s)."]
    pinned = [p for p in (pinned or []) if isinstance(p, dict)]
    if pinned:
        lines += ["",
                  "These pictures are already on the rail. Keep these tags "
                  "and filenames. Do not add other files. Do not rename tags. "
                  "Look at each still: a person gets subject N + "
                  "retention fully_preserved and a subjects.N block; a place "
                  "gets retention reference and no subject.",
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
        "(wardrobe and pose as they stand now). An empty subjects object "
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
                     pinned=None, images=None,
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
    text = build_user_turn(brief, hops, files, pinned=pinned)
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
        if not shot_text:
            last_errors = ["the reply contained no JSON. Answer with the two "
                           "JSON blocks and nothing else."]
        else:
            last_errors, warnings = validate(
                shot_text, ref_text, hops=hops, known_files=files,
                pinned=pinned)
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
