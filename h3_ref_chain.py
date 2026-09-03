"""Native MiniMax H3 Ref2VA chain: several generates joined into one clip.

Hop 1 is a full Ref2VA generate and may carry an official six-field H3 prompt.
Hop 2+ is a *continuation*, not a second generate: a continuation beat with the
previous hop pinned in front of it.

The pin is the whole point. Hop 2+ slices the previous hop's sampler AV latent
through MiniMaxH3MotionContext, which keeps the join in the latent domain and
end-aligns the audio window. MiniMaxH3AddGuide on decoded pixels is the
fallback -- taken when Motion-Context is missing, or when the previous hop came
from the cache and so has no sampler latent to slice.

Authoring is `shot_plan` + `ref_plan`, both JSON strings, both edited by the DOM
panel in js/ and both the single source of truth. The legacy `prompt` widget
(optional --- / JSON blocks) still runs for `hop_script=verbatim`.
"""
from __future__ import annotations

import base64
import hashlib
import queue
import threading
import io as pyio
import json
import math
import os
import re

import torch

try:
    from server import PromptServer
except Exception:
    PromptServer = None

import comfy.model_management as mm
import comfy.samplers
import comfy.utils
from comfy_extras.nodes_audio import vae_decode_audio
from comfy_extras.nodes_custom_sampler import (
    BasicGuider,
    BasicScheduler,
    KSamplerSelect,
    RandomNoise,
    SamplerCustomAdvanced,
)
try:
    from comfy_extras.nodes_minimax_h3 import (
        MiniMaxH3AddGuide,
        MiniMaxH3ReferenceToVideo,
        MiniMaxH3SigmaShift,
        align_frame_count,
    )
except ImportError as _exc:  # pragma: no cover - depends on the host build
    # The one failure a first-time installer is actually likely to hit. Left
    # bare it surfaces as "cannot import name 'MiniMaxH3AddGuide'", which says
    # nothing about what to do. The pack cannot work without these, so it still
    # refuses to load -- it just says why.
    raise ImportError(
        "Hand Tie Clips needs MiniMax H3 support in ComfyUI itself "
        "(comfy_extras/nodes_minimax_h3.py, ComfyUI PR #15439). Update ComfyUI "
        "to a build that ships it, then restart. Original error: %s" % _exc
    ) from _exc
from nodes import VAEDecode

from . import directives as _d
from . import plan as _plan
from . import refs as _refs
from . import store as _store
from . import media as _media
from . import tone as _tone
from . import sheet as _sheet
from . import music as _music
from . import latents as _latents
# One definition, in refs.py -- routes.py publishes that copy to the editor, so
# a second constant here meant the node's slot count and the number the UI was
# told could drift apart.
from .refs import MAX_REF_IMAGES

FPS = 24
TAG = "HandTieClips"

# quality=draft. Low enough to be genuinely fast, high enough that blocking,
# camera and whether a join lands are all still readable. Both values are in
# the cache key already, so a draft never overwrites the matching final.
DRAFT_RESOLUTION = "448p (0.34 MP)"
DRAFT_STEPS = 6

# H3's canvas rules, mirrored from comfy_extras/nodes_minimax_h3.py
# (CANVAS_MULTIPLE, MAX_PIXELS, adapt_canvas) so the two cannot drift silently.
# Core applies them to reference VIDEOS only -- adapt_canvas has exactly one
# call site and the generation canvas is not it. What core does with the size we
# hand it is `height // 16`, which means an off-grid canvas does not raise: it
# quietly builds a latent for a smaller frame than `master_imgs` was allocated
# for. Hence _fit_canvas below, and the assertion at the call site.
CANVAS_MULTIPLE = 32
CANVAS_AREA_CAP = 768 * 1344          # 1_032_192

# Ratio per aspect label, in dropdown order: widest landscape down to square,
# then the portraits back out. LABELS ARE PART OF THE SAVED-WORKFLOW FORMAT --
# a combo widget stores its value as a string, so renaming one resets that
# widget to the default on every graph that used it. The three 1.0.x labels
# below are therefore verbatim, spacing included.
ASPECTS = {
    "21:9 landscape": (21, 9),
    "16:9 landscape": (16, 9),
    "3:2 landscape": (3, 2),
    "4:3 landscape": (4, 3),
    "5:4 landscape": (5, 4),
    "1:1 square": (1, 1),
    "4:5 portrait": (4, 5),
    "3:4 portrait": (3, 4),
    "2:3 portrait": (2, 3),
    "9:16 portrait": (9, 16),
    "9:21 portrait": (9, 21),
}
DEFAULT_ASPECT = "16:9 landscape"

# Short edge per resolution label. H3 is a 768-short-edge model: core's
# adapt_canvas pins the short edge and derives the long one from the ratio,
# capping the area at 768*1344. It does NOT work from an area budget, and the
# distinction is not cosmetic -- 16:9 at the native tier is 1344x768, which an
# area formula asking for "0.98 megapixels" of 10^6 pixels never reaches.
#
# The MP figure in each label is that tier at 16:9, in MEBIpixels (1024*1024),
# which is where the number everyone quotes comes from: 1344*768 = 1_032_192,
# and 1_032_192 / 1_048_576 = 0.984. It is quoted because people search for it.
# It is exact for 16:9 only -- the same tier at 4:3 is 1024x768, which is
# 0.75 MP -- so the label names the tier and the parenthesis is a signpost, not
# a specification. tools/check_canvas.py asserts the 16:9 figure matches.
RESOLUTIONS = {
    "768p (0.98 MP)": 768,      # native
    "640p (0.70 MP)": 640,
    "576p (0.56 MP)": 576,
    "512p (0.44 MP)": 512,
    "448p (0.34 MP)": 448,
}
DEFAULT_RESOLUTION = "768p (0.98 MP)"

# The complete 1.0.x canvas table, pinned.
#
# Through 1.0.x this was five hand-authored resolution labels by three aspects.
# Those labels are off the dropdown now and no formula here reproduces them --
# they were never derived from the short edge, and half of them are not what an
# area budget gives either. So all fifteen are pinned verbatim rather than
# approximated, because width and height are in `chain_salt`: resolving them
# differently would re-render every chain a 1.0.x user has on disk AND change
# the pixels of a graph they already signed off.
#
# Nothing new can select one of these -- this exists only so an old saved
# workflow keeps rendering what it always rendered. tools/check_canvas.py
# asserts every entry against the table as it shipped.
LEGACY_CANVAS = {}
for _mp, _cells in {
    "0.2 MP": {"16:9 landscape": (608, 352), "9:16 portrait": (352, 608),
               "1:1 square": (448, 448)},
    "0.3 MP": {"16:9 landscape": (736, 416), "9:16 portrait": (416, 736),
               "1:1 square": (544, 544)},
    "0.5 MP": {"16:9 landscape": (960, 544), "9:16 portrait": (544, 960),
               "1:1 square": (704, 704)},
    "0.7 MP": {"16:9 landscape": (1120, 640), "9:16 portrait": (640, 1120),
               "1:1 square": (832, 832)},
    "1.0 MP": {"16:9 landscape": (1280, 736), "9:16 portrait": (736, 1280),
               "1:1 square": (992, 992)},
}.items():
    for _asp, _wh in _cells.items():
        LEGACY_CANVAS[(_mp, _asp)] = _wh
# The v1.1 pre-release labels. They shipped to nobody, but this repo's own
# workflows and any graph saved while v1.1 was in progress carry them, and they
# named the right tier under a wrong arithmetic. Alias rather than pin: these
# should resolve to the tier they were trying to describe, not to the sizes the
# area formula gave them.
LEGACY_ALIAS = {
    "0.98 MP": "768p (0.98 MP)",
    "0.75 MP": "640p (0.70 MP)",
    "0.60 MP": "576p (0.56 MP)",
    "0.45 MP": "512p (0.44 MP)",
    "0.30 MP": "448p (0.34 MP)",
}
DURATION_FRAMES = {
    # Every value satisfies align_frame_count (n % 17 == 5) at FPS 24, so the
    # label and the frames the model actually renders agree to a tenth.
    "5 s": 124,
    "7 s": 175,
    "8 s": 192,
    "10 s": 243,
    "15 s": 362,
}
OVERLAP_FRAMES = {
    "0.9 s": 22,
    "0.2 s": 5,
    "1.6 s": 39,
}
# MiniMaxH3MotionContext.apply takes `context_length` as a *string* combo and
# accepts only these values. Derived from OVERLAP_FRAMES so the two cannot
# drift: add an overlap without a matching context_length and the pin would
# silently clamp to 22 while the master trims the real value -- a misaligned
# seam with no error. _pin_continue logs and falls back instead.
MC_CONTEXT_LENGTHS = frozenset(str(v) for v in OVERLAP_FRAMES.values())


def _fit_canvas(ratio, short_edge):
    """comfy_extras.nodes_minimax_h3.adapt_canvas, with the short edge a knob.

    Line for line the same arithmetic as core, which is the point: pin the short
    edge, derive the long edge from the ratio, scale down if the area cap is
    exceeded, round each axis to the nearest 32. Core hard-codes BASE_SHORT_EDGE
    = 768; this takes it as an argument so the draft tiers below native run the
    identical path rather than a second implementation that agrees with it only
    at one rung.

    v1.1 briefly derived both axes from a megapixel budget instead. That is a
    different algorithm wearing the same rounding, and it disagreed with core at
    EVERY aspect ratio -- 16:9 came out 1312x736 against core's 1344x768, and
    4:3 came out 1152x864 against 1024x768. The tell was in core's own docstring
    ("768-short-edge canvas with 768*1344 area cap") the whole time.
    """
    short = max(CANVAS_MULTIPLE, int(short_edge))
    if ratio >= 1.0:
        w, h = short * ratio, float(short)
    else:
        w, h = float(short), short / ratio
    if w * h > CANVAS_AREA_CAP:
        scale = math.sqrt(CANVAS_AREA_CAP / (w * h))
        w, h = w * scale, h * scale
    m = CANVAS_MULTIPLE
    return (max(m, int(round(w / m)) * m), max(m, int(round(h / m)) * m))


def _canvas(resolution, aspect):
    """(width, height) for a resolution label and an aspect label.

    Three paths, in order. A 1.0.x label is pinned to the exact tuple it shipped
    with. A v1.1 pre-release label is aliased onto the tier it was trying to
    name. Anything current goes through core's arithmetic.

    An unreadable label is worth a line of output: before 1.1 this returned
    1280x736 for any unrecognised input and said nothing, so a typo in an
    API-driven graph rendered at the wrong size with no evidence anywhere.
    """
    res, asp = str(resolution), str(aspect)
    if (res, asp) in LEGACY_CANVAS:
        w, h = LEGACY_CANVAS[(res, asp)]
        print(f"[{TAG}] resolution {res!r} is a 1.0.x label: holding {w}x{h} so "
              f"this workflow keeps the pixels it was built with. Choose a "
              f"current resolution to move onto the 768p tier ladder.",
              flush=True)
        return w, h
    if res in LEGACY_ALIAS:
        moved = LEGACY_ALIAS[res]
        print(f"[{TAG}] resolution {res!r} was a v1.1 pre-release label and its "
              f"sizes were wrong; reading it as {moved!r}.", flush=True)
        res = moved
    ratio = ASPECTS.get(asp)
    if ratio is None:
        print(f"[{TAG}] unknown aspect {asp!r}; using {DEFAULT_ASPECT}",
              flush=True)
        ratio = ASPECTS[DEFAULT_ASPECT]
    short = RESOLUTIONS.get(res)
    if short is None:
        short = RESOLUTIONS[DEFAULT_RESOLUTION]
        print(f"[{TAG}] unknown resolution {resolution!r}; using "
              f"{DEFAULT_RESOLUTION}", flush=True)
    return _fit_canvas(ratio[0] / ratio[1], short)


def _duration_frames(duration):
    return int(DURATION_FRAMES.get(str(duration), 243))


def _overlap_frames(overlap):
    key = str(overlap)
    if key in OVERLAP_FRAMES:
        return OVERLAP_FRAMES[key]
    return int(overlap)

# Prompt phrasing rule: AFFIRMATIVE ONLY.
# Sampling runs through BasicGuider at cfg 1.0 with no negative branch, so every
# concept named in the prompt is additive and cannot be subtracted -- "Do not
# restart the scene" puts `restart` in front of the encoder. State what the shot
# IS doing, never what it must not do. Keep this rule when editing below.
CONTINUE_PREFIX = (
    "The clip opens on the action already in progress from the pinned frames. "
    "The same people continue from where the pinned frames leave off, in the same "
    "wardrobe, the same room, and the same lighting. "
    "After a brief hold, the action carries forward from its current point.\n\n"
)

ADVANCE_BEAT = (
    "The action already in progress carries forward from its current point."
)

MAX_REF_VIDEOS = 3


def _result(out):
    if hasattr(out, "args"):
        return out.args
    if isinstance(out, (tuple, list)):
        return tuple(out)
    return (out,)


def _model_fingerprint(model):
    """Identify the incoming MODEL by what has been patched onto it.

    The hop cache has to notice when a hop was rendered under a different LoRA
    stack or a different attention path, or it will happily serve frames that
    do not belong to the current graph -- silently wrong output, which is worse
    than no cache at all. When the patch nodes were a widget on this node the
    parsed plan went into the key directly; with them drawn upstream the only
    thing available is the ModelPatcher itself.

    Cheap and content-derived: the set of weight keys any LoRA touched plus the
    per-key strength scalars, and the scalar half of `transformer_options`,
    which is where the SLA and low-VRAM attention overrides land. Patch *values*
    are tensors and are deliberately not hashed.

    `patches_uuid` is not usable here: `ModelPatcher.add_patches` assigns a
    fresh `uuid4()` on every call, so it would change every run and bust the
    cache even when nothing about the graph moved.

    Known collision: two different LoRAs touching an identical key set at
    identical strengths fingerprint the same. Rare, and the alternative costs a
    full state-dict walk per run.
    """
    h = hashlib.sha256()
    patches = getattr(model, "patches", None) or {}
    for key in sorted(patches):
        h.update(str(key).encode())
        for entry in patches[key]:
            # (strength_patch, weights, strength_model, offset, function)
            try:
                h.update(f"{float(entry[0]):.6g}".encode())
                if len(entry) > 2 and isinstance(entry[2], (int, float)):
                    h.update(f"{float(entry[2]):.6g}".encode())
            except (TypeError, ValueError, IndexError):
                h.update(b"?")

    opts = getattr(model, "model_options", None) or {}
    transformer = opts.get("transformer_options") or {}

    def _closure_scalars(fn):
        """The scalar settings a callable closed over.

        H3-SLA-Attention installs its config by closure --
        `_make_override(state, float(sparsity_ratio), blkq, blkk,
        int(min_seq_len), bool(protect_audio))` -- so a callable rendered as
        `type(fn).__name__` hashes to the bare string "function" and SLA's
        settings vanish from the key. Changing sparsity 0.90 -> 0.50 then left
        the fingerprint unmoved and the cache served hops rendered under a
        different attention path.

        Scalars only, deliberately: the first cell is a mutable `state` dict the
        sampler counts into during the run, and hashing that would change the
        fingerprint on every queue and never hit the cache at all.
        """
        parts = [getattr(fn, "__qualname__", "") or getattr(fn, "__name__", "")]
        for cell in (getattr(fn, "__closure__", None) or ()):
            try:
                v = cell.cell_contents
            except ValueError:  # empty cell, e.g. a recursive closure
                parts.append("?")
                continue
            parts.append(repr(v)
                         if isinstance(v, (str, int, float, bool)) or v is None
                         else type(v).__name__)
        return "fn(" + ",".join(parts) + ")"

    def _scalars(obj, depth=0):
        """Only names and scalars -- tensors and mutable state are not stable."""
        if depth > 3:
            return "..."
        if isinstance(obj, dict):
            # sorted(obj, key=str), not sorted(map(str, obj)): stringifying the
            # keys first drops every non-str key from the hash, because the
            # `k in obj` guard then fails against the real key.
            return "{" + ",".join(
                f"{k}:{_scalars(obj[k], depth + 1)}" for k in sorted(obj, key=str)
            ) + "}"
        if isinstance(obj, (list, tuple)):
            return "[" + ",".join(_scalars(v, depth + 1) for v in obj) + "]"
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return repr(obj)
        if callable(obj):
            return _closure_scalars(obj)
        return type(obj).__name__

    h.update(_scalars(transformer).encode())
    return h.hexdigest()[:16]


def _parse_shots(text):
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{TAG}: prompt is empty")
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"{TAG}: prompt looks like JSON but does not parse ({e})") from e
        if isinstance(data, dict):
            shots = [str(p).strip() for p in data.get("prompts", []) if str(p).strip()]
        elif isinstance(data, list):
            shots = [str(p).strip() for p in data if str(p).strip()]
        else:
            shots = []
        if shots:
            return shots
    parts = [b.strip() for b in re.split(r"(?m)^---\s*$", text) if b.strip()]
    return parts or [text]


def _parse_state(text):
    """continuity_state input: blank -> no-op, else a JSON object (from HTCContinuityState)."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{TAG}: continuity_state looks like JSON but does not parse ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{TAG}: continuity_state must be a JSON object")
    return data


def _continue_prompt(block):
    """Wrap a verbatim-mode block as a continuation.

    Never rewrite the official summary task types here. This used to replace
    `[keyframe completion]` with `[video continuation + reference generation]`,
    which is what made hop 2 of chain_00030/00031 a new stills generate instead
    of a first-frame continue. Combine types with ` + `; never drop one that is
    already present.
    """
    text = (block or "").strip()
    if _d.is_full_h3_prompt(text):
        text = _d.flatten_official_continue(text)
    return CONTINUE_PREFIX + (text or ADVANCE_BEAT)


def _expand_shots(blocks, chains, hop_script="verbatim"):
    original = list(blocks)
    if len(blocks) > chains:
        print(f"[{TAG}] dropping {len(blocks) - chains} extra --- block(s)", flush=True)
        blocks = blocks[:chains]
    unique = len(original) if len(original) <= chains else chains
    while len(blocks) < chains:
        if hop_script == "next" and unique == 1:
            blocks.append("")
        else:
            blocks.append(blocks[-1])
    if hop_script == "next":
        return blocks, unique
    out = []
    for i, block in enumerate(blocks):
        wrap = i > 0 and (unique == 1 or i >= unique)
        out.append(_continue_prompt(block) if wrap else block)
    return out, unique


def _state_entry_text(entry, hop_index):
    """locked (verbatim) + context (current-state) + this hop's mutable beat."""
    if not isinstance(entry, dict):
        return ""
    locked = str(entry.get("locked") or "").strip()
    context = str(entry.get("context") or "").strip()
    mutable = entry.get("mutable") or []
    if isinstance(mutable, str):
        mutable = _parse_shots(mutable) if mutable.strip() else []
    mutable = [str(b).strip() for b in mutable if str(b).strip()]
    beat = ""
    if mutable:
        idx = hop_index if hop_index < len(mutable) else len(mutable) - 1
        beat = mutable[idx]
    return "\n".join(p for p in (locked, context, beat) if p)


def _state_header(state, hop_index):
    """Compose the continuity_state block for this hop (locked/context every hop, mutable indexed)."""
    if not state:
        return ""
    sections = []
    setting_text = _state_entry_text(state.get("setting") or {}, hop_index)
    if setting_text:
        sections.append("setting:\n" + setting_text)
    for char_id, entry in (state.get("characters") or {}).items():
        char_text = _state_entry_text(entry, hop_index)
        if char_text:
            sections.append(f"character {char_id}:\n" + char_text)
    return "\n\n".join(sections)


def _identity_lock(n_stills, live_picture, identity_ordinals=None, n_subjects=None):
    """Name the pictures that are people, and only those.

    `identity_ordinals` comes from the reference register, which is the only
    thing that knows a picture is a face rather than a room. Without it this
    falls back to "every wired still is an identity", which is what it always
    did and is right when nothing better is known -- but with a register wired
    that fallback tells the encoder a photograph of a kitchen has a face and a
    hairstyle to match exactly. At cfg 1.0 there is no negative branch, so that
    is additive noise on every hop.

    `n_subjects` drives number agreement, not the picture count: two
    photographs of one person is still one identity.
    """
    ords = (list(identity_ordinals) if identity_ordinals is not None
            else list(range(1, int(n_stills) + 1)))
    if not ords:
        # A register with no subject-bearing refs -- setting plates only. There
        # is no identity to lock, and _live_cite still cites the live frame.
        return ""
    pics = ", ".join(f"<Picture {i}>" for i in ords)
    count = int(n_subjects) if n_subjects is not None else len(ords)
    # Qwen3-VL is a language encoder, so number agreement is not cosmetic:
    # "<Picture 1> are the only identities" is what a single wired ref produced.
    if count == 1:
        line = (
            f"{pics} is the only identity. That face, bone structure, and hairstyle "
            "match the photograph exactly."
        ) if len(ords) == 1 else (
            f"{pics} are the same one person, and the only identity. That face, bone "
            "structure, and hairstyle match those photographs exactly."
        )
    else:
        line = (
            f"{pics} are the only identities. Each face, bone structure, and hairstyle "
            "matches its photograph exactly."
        )
    if live_picture and live_picture not in ords:
        who = ("that same person as they stand" if count == 1
               else "those same people as they stand")
        line += f" <Picture {live_picture}> shows {who} right now, mid-action."
    return line


def _refvid_cite(desc, ordinal=1):
    """One sentence saying what the author's reference clip is for.

    The clip has always gone in as <Video 1> with nothing anywhere in the prompt
    naming it -- the same uncited-reference problem the stills had on hops after
    the first, and the same consequence: a Ref2VA model handed footage and no
    reason for it tends to render the footage. `_live_cite` covers the PINNED
    tail, which is a different video and already explained.

    Empty desc returns "", so a workflow that does not fill the new field emits
    byte-identical prompts to 1.0.x. The wording is the author's; only the
    citation is ours.
    """
    text = str(desc or "").strip().rstrip(".")
    if not text:
        return ""
    return f"<Video {ordinal}> is a reference clip: {text}."


def _live_cite(live_picture, live_video):
    bits = []
    if live_picture:
        bits.append(
            f"<Picture {live_picture}> is the live frame at the start of this clip, "
            "already in progress from the pinned tail."
        )
    if live_video:
        bits.append(
            f"<Video {live_video}> is the pinned tail of the previous clip and the first "
            "moments of this generate. The motion continues at the speed it already has."
        )
    return " ".join(bits)


def _assemble_next(beat, live_picture=None, live_video=None,
                   n_stills=0, state_header="",
                   identity_ordinals=None, n_subjects=None, tail=None,
                   continuity="", retention="", wardrobe=False, refvid=""):
    """Hop 2+ in `next` mode: user text is only the new beat."""
    text = (beat or "").strip() or ADVANCE_BEAT
    cite = _live_cite(live_picture, live_video)
    lock = _identity_lock(n_stills, live_picture,
                          identity_ordinals=identity_ordinals,
                          n_subjects=n_subjects)
    solo = (n_subjects == 1) or (n_subjects is None and identity_ordinals is not None
                                 and len(identity_ordinals) == 1)
    whoever = "The same person holds" if solo else "The same people hold"
    if _d.is_full_h3_prompt(text):
        print(
            f"[{TAG}] hop 2+ full H3 block flattened to a continuation beat "
            "(a complete Ref2VA prompt on hop 2+ starts a new scene)",
            flush=True,
        )
        text = _d.flatten_official_continue(text) or ADVANCE_BEAT
    # No identity header on hop 2+ at all: register subject_prose is a
    # subject_definitions block, and putting one here made hop 2 a second
    # Ref2VA generate (chain_00033, the short drink beat). The call site passes
    # nothing, and there is no parameter to pass.
    header = str(state_header or "").strip()
    top = header + "\n\n" if header else ""
    # Labelled blocks lead, prose follows -- the same shape hop 1 gets from
    # subject_prose. Deliberately NOT folded into `inject`: that is space-joined
    # into a single paragraph, and retention_analysis is a multi-line block that
    # carries its own label. Only retention, never subject_definitions: a
    # <Subject N> introduced on hop 4 has no antecedent in its own encode, which
    # is the dangling-token defect the call site documents.
    ret_block = str(retention or "").strip()
    ret_block = (ret_block + "\n\n") if ret_block else ""
    # Order: who the pictures are, then what stays the same, then which
    # frame is live. `continuity` carries no ordinals, so it is safe on a
    # pin-only hop where `lock` is deliberately empty.
    inject = " ".join(p for p in (lock, str(continuity or "").strip(),
                                  str(refvid or "").strip(), cite) if p)
    inject = (inject + "\n\n") if inject else "\n"
    # Pin-only hops have no identity photographs. Naming them (chain_00034)
    # sent the encoder back to the face/outfit stills — commercial kitchen,
    # grey shirt, no apron.
    # The closer used to end on "still underway" unconditionally, which made it
    # the last sentence of the prompt and so overrode the shot's own `tail`
    # directive -- `settle` and `hold` were unreachable on hop 2+. On the final
    # hop nothing absorbs that instruction and the model invents late action to
    # satisfy it (observed: a line of dialogue in the last second of a 3-hop
    # chain, and the ~23 s spike noted in CLAUDE.md). Each variant below still
    # ends on the clip's terminal *state*, never on the photographs, so the
    # ordering rule documented at the return is preserved.
    terminal = {
        "settle": "and that action eases to a rest and stays there through the "
                  "final frames.",
        "hold": "and the final position holds steady through the last moments.",
    }.get(str(tail or "").strip(), "and that action is still underway as the clip ends.")

    # "Clothing follows the live frame" and a wardrobe plate scheduled onto this
    # hop are contradictory instructions, and at cfg 1.0 both are additive --
    # there is no negative branch to resolve them, so the encoder gets each with
    # equal weight. When a `partially_copy` still is actually here, it wins;
    # otherwise the live frame does, exactly as before.
    clothing = (
        "Clothing follows the wardrobe photograph, worn on the body as it "
        "already stands. " if wardrobe else
        "Clothing follows whatever is already on them in the live frame. "
    )
    if lock:
        hold = (
            f"{whoever} their current pose, room, lighting, and camera side, "
            "and the shot continues from exactly there. "
        )
        closer = (
            "Faces and hair follow the identity photographs. "
            f"{clothing}"
            "After a brief hold on the incoming action, the shot advances "
            f"through what the next-beat describes, {terminal}"
        )
    else:
        hold = (
            "The incoming frame holds the current pose, room, lighting, and "
            "camera side, and the shot continues from exactly there. "
        )
        closer = (
            (f"{clothing}Room and lighting stay as they are in the live frame. "
             if wardrobe else
             "Wardrobe, room, and lighting stay as they are in the live frame. ")
            + "After a brief hold on the incoming action, the shot advances "
            + f"through what the next-beat describes, {terminal}"
        )
    # Official field names on hop 2+ start a new Ref2VA generate
    # (chain_00030..00034). One paragraph: airlock, then the beat.
    text = re.sub(
        r"(?m)^(overall_soundscape|non_diegetic_music):\s*", "", text).strip()
    return (
        f"{top}"
        f"{ret_block}"
        "The clip opens already in progress from the pinned frames. "
        "The incoming arrangement holds for a short beat -- breath, a weight "
        "shift, an eyeline -- and only then the next action begins. "
        f"{hold}"
        f"{inject}"
        "What happens next:\n"
        f"{text}\n\n"
        # Ordering rule: the prompt must END on ongoing motion, never on the
        # photographs. The final sentence governs the terminal state of the clip,
        # and when the beat action finishes before the frames run out the model
        # renders whatever the prompt last pointed it at. Ending on "follow the
        # identity photographs" made the tail settle onto the reference image --
        # observed as the last ~3s of a 3-hop chain cutting to the ref still.
        # Hops 1..N-1 hide this because the pin consumes their tail; the final
        # hop has no successor, so its drift is what you see.
        f"{closer}"
    )


def _attach_pin_to_qwen(pin_mode, hop_images, hop_videos, last_frame, pin_clip):
    """AddGuide is invisible to Qwen. Optionally put the incoming state in ref slots.

    The live frame is <Picture 1>. Identity stills shift up. Appending it after
    the stills (chain_00034) made the pin Picture 4 against a commercial-kitchen
    face and outfit as Pictures 1–2; hop 2 hard-cut and dropped the apron.
    """
    images = dict(hop_images or {})
    videos = dict(hop_videos or {})
    live_p = live_v = None
    if pin_mode in ("last frame", "both") and last_frame is not None:
        used = len(images)
        if used >= MAX_REF_IMAGES:
            print(f"[{TAG}] pin_to_qwen last frame skipped: already {MAX_REF_IMAGES} stills",
                  flush=True)
        else:
            # Insertion order IS Picture order (core walks .values()).
            # Put the live frame in the dict first or it becomes the last picture.
            ordered = {"ref_image_1": last_frame[:1].contiguous()}
            for key, tensor in images.items():
                n = int(str(key).rsplit("_", 1)[-1])
                ordered[f"ref_image_{n + 1}"] = tensor
            images = ordered
            live_p = 1
            extra = f", {used} still(s) -> Picture 2+" if used else ""
            print(f"[{TAG}] Qwen last frame -> <Picture 1>{extra}", flush=True)
    if pin_mode in ("pin clip", "both") and pin_clip is not None:
        if pin_clip.shape[0] < 5:
            print(f"[{TAG}] pin clip too short for a video ref "
                  f"({int(pin_clip.shape[0])}f)", flush=True)
        elif len(videos) >= MAX_REF_VIDEOS:
            print(f"[{TAG}] pin_to_qwen pin clip skipped: already {MAX_REF_VIDEOS} videos",
                  flush=True)
        else:
            live_v = len(videos) + 1
            videos[f"ref_video_{live_v}"] = pin_clip.contiguous()
            print(
                f"[{TAG}] Qwen pin clip -> <Video {live_v}> "
                f"({int(pin_clip.shape[0])}f, no soundtrack — voice stays <Audio 1>)",
                flush=True,
            )
    return images, live_p, videos, live_v


def _latent_cpu(lat):
    """Keep the previous hop's sampler output off GPU between hops."""
    if not isinstance(lat, dict) or "samples" not in lat:
        return lat
    out = dict(lat)
    samples = lat["samples"]
    try:
        out["samples"] = samples.cpu()
    except Exception as e:
        # Falling back to the GPU tensor is correct -- the pin still works --
        # but it is also a per-hop VRAM leak, so it must not be silent.
        print(f"[{TAG}] could not move the hop latent to CPU ({e!r}); "
              f"keeping it on device", flush=True)
        out["samples"] = samples
    return out


def _motion_context_cls():
    """Upstream MiniMaxH3MotionContext, skipping forks with a different apply()."""
    try:
        import inspect
        import nodes as nodes_mod
    except Exception:
        return None
    cls = getattr(nodes_mod, "NODE_CLASS_MAPPINGS", {}).get("MiniMaxH3MotionContext")

    def compatible(c):
        try:
            params = inspect.signature(c.apply).parameters
            need = [k for k, v in params.items()
                    if v.default is inspect.Parameter.empty and k != "self"]
            return "context_frames" not in need
        except Exception:
            return False

    if cls is not None and compatible(cls):
        return cls
    import sys
    for mod in list(sys.modules.values()):
        cand = getattr(mod, "MiniMaxH3MotionContext", None)
        if cand is not None and compatible(cand):
            if cls is not None:
                print(
                    f"[{TAG}] MiniMaxH3MotionContext registry entry is a fork; "
                    f"using upstream class from {getattr(mod, '__name__', '?')}",
                    flush=True,
                )
            return cand
    return None


def _latent_parts(x):
    """-> list of component tensors, or None. See latents.parts for the why."""
    return _latents.parts(x)


def _rebuild_latent_samples(x, parts):
    """Put conditioned components back into the container they came from."""
    return _latents.rebuild(x, parts)


def _condition_pin_latent(lat, anchor, mode="off", noise=0.0, seed=0):
    """Anti-ratchet preprocessing for the latent handed to Motion-Context.

    MiniMaxH3MotionContext.apply() takes `context_latent` as-is and exposes no
    hook, so every lever has to be applied to the latent before it goes in.

    Two rescale modes, and the difference between them is the whole point.

    `sigma` rescales the pin so its standard deviation matches the anchor hop's.
    This is the original lever and **it is measurably the wrong statistic.** On
    a 3-hop chain the pin's total sigma FELL (1.0414 -> 1.0289) while the
    picture's mid-band energy climbed 8% and its high-band fraction rose 1.6%.
    Matching sigma there scales the whole latent UP by 1.2%, lifting a high
    band that was already too hot. Kept because it is what shipped, and old
    workflows say "on".

    `band` splits each spatial component into low and high and rescales only the
    high part, so the *ratio* between them returns to the anchor hop's. That
    ratio is what the ratchet actually moves. Still a scalar per band, so it
    moves no structure and cannot blur or invent detail -- the property that
    made `sigma` safe to run blind, kept.

    `noise` mixes in a seeded perturbation, attacking the same ratchet from the
    other side; measured gains reverse above 0.10, hence the widget cap.

    **Per component, not per latent** (fixed 2026-08-27). Video and audio are
    two tensors in one NestedTensor and their statistics drift independently, so
    each carries its own anchor. Before this, `.std()` raised on the nested
    object and every lever was dead -- announced once per hop as `pin
    conditioning skipped`, which read as routine noise.

    Returns `(latent, anchor)` where `anchor` is a list, one dict per component
    -- the first pinned hop establishes what later hops are matched against.
    Every lever defaults off, in which case the latent is returned untouched.
    """
    if not isinstance(lat, dict) or "samples" not in lat:
        return lat, anchor
    x = lat["samples"]
    parts = _latent_parts(x)
    if parts is None:
        print(f"[{TAG}] pin conditioning skipped: unrecognised latent "
              f"({type(x).__name__})", flush=True)
        return lat, anchor
    mode = str(mode)
    if mode == "on":                       # pre-2026-09-01 workflows
        mode = "sigma"
    try:
        cur = []
        for t in parts:
            sig = float(t.float().std())
            cur.append({"sigma": sig, "ratio": _latents.band_ratio(t)})
    except Exception as e:  # noqa: BLE001
        print(f"[{TAG}] pin conditioning skipped ({e!r})", flush=True)
        return lat, anchor
    if not all(c["sigma"] == c["sigma"] and c["sigma"] for c in cur):
        return lat, anchor                 # zero or NaN in any stream
    if anchor is None:
        anchor = cur
    if len(anchor) != len(cur):
        # Stream count changed mid-chain. Nothing sensible to match against.
        print(f"[{TAG}] pin conditioning skipped: latent has {len(cur)} "
              f"component(s), anchor has {len(anchor)}", flush=True)
        return lat, anchor

    # Always report the drift, even with every lever off. This is the number
    # that says whether a lever is needed and whether one worked, and it costs
    # nothing to read -- the alternative is inferring it from the master after
    # a decode, which is how the wrong statistic went unnoticed for a release.
    for i, (c, a) in enumerate(zip(cur, anchor)):
        if c["ratio"] is not None and a["ratio"]:
            print(f"[{TAG}] pin drift[{i}]: sigma {c['sigma']:.4f} "
                  f"(x{c['sigma'] / a['sigma']:.4f} vs anchor)  "
                  f"high-band fraction {c['ratio']:.4f} "
                  f"(x{c['ratio'] / a['ratio']:.4f})", flush=True)

    if mode not in ("sigma", "band") and noise <= 0.0:
        return lat, anchor

    out_parts, notes = [], []
    for idx, (t, c, a) in enumerate(zip(parts, cur, anchor)):
        o = t
        if mode == "sigma":
            scale = a["sigma"] / c["sigma"]
            o = o * scale
            notes.append(f"sigma[{idx}] x{scale:.4f}")
        elif mode == "band":
            o, k = _latents.match_band(o, a["ratio"])
            if k is None:
                # An audio component has no bands; leaving it alone is correct,
                # not a fallback -- `sigma` on it would be a different lever
                # applied silently under this one's name.
                notes.append(f"band[{idx}] skipped (no spatial extent)")
            else:
                notes.append(f"band[{idx}] hi x{k:.4f} "
                             f"(fraction {c['ratio']:.4f} -> {a['ratio']:.4f})")
        if noise > 0.0:
            # Per component: `.shape` on the nested object reports only the
            # first component's shape, so one draw for the whole latent would
            # size its noise to the video and broadcast that onto the audio.
            g = torch.Generator(device="cpu").manual_seed((int(seed) + idx) & 0x7FFFFFFF)
            n = torch.randn(o.shape, generator=g, dtype=torch.float32)
            o = o + n.to(dtype=o.dtype, device=o.device) * (float(noise) * a["sigma"])
            notes.append(f"noise[{idx}] {float(noise):.3f}")
        out_parts.append(o)
    if notes:
        print(f"[{TAG}] pin conditioning: " + ", ".join(notes), flush=True)
    new = dict(lat)
    new["samples"] = _rebuild_latent_samples(x, out_parts)
    return new, anchor


def _pin_mech_for(hop_index, overlap_n, prev_sampled):
    """Which mechanism `_pin_continue` will pick, without doing the work.

    The hop cache key has to be built *before* the pin runs, and the two
    mechanisms produce different frames, so the key needs the mechanism up
    front. Every condition here mirrors _pin_continue; the one thing it cannot
    predict is Motion-Context raising at call time, which the caller catches by
    comparing this against the mechanism actually used and declining to cache
    that hop.
    """
    if hop_index == 0:
        return "none"
    if _motion_context_cls() is None:
        return "addguide_pixels"
    if str(overlap_n) not in MC_CONTEXT_LENGTHS:
        return "addguide_pixels"
    if prev_sampled is None:
        return "addguide_pixels"
    return "motion_context"


def _pin_continue(cond, latent, vae, audio_vae, overlap_n,
                  prev_sampled, prev_imgs, prev_audio, audio_ctx=24):
    """Hop 2+ motion pin. Latent Motion-Context when possible; AddGuide otherwise.

    AddGuide re-encodes decoded pixels and anchors audio forwards from frame 0
    (cover-band soundtrack). Motion-Context slices the previous sampler AV
    latent and end-aligns the audio window on this clip's timeline.

    Returns `(conditioning, mech)` where mech is one of "motion_context",
    "addguide_pixels" or "none". The caller puts mech in the *per-hop* cache
    key: the two mechanisms produce different frames, so a hop rendered under
    the AddGuide fallback must not be served later to a run where the latent
    pin was available.
    """
    ctx_label = str(overlap_n)
    mc = _motion_context_cls()
    if mc is not None and ctx_label not in MC_CONTEXT_LENGTHS:
        print(
            f"[{TAG}] overlap {overlap_n}f has no Motion-Context context_length "
            f"(accepts {sorted(MC_CONTEXT_LENGTHS, key=int)}); AddGuide pixel pin",
            flush=True,
        )
        mc = None
    if mc is not None and prev_sampled is not None:
        try:
            a_ctx = int(audio_ctx)
            cond, trim = mc().apply(
                conditioning=cond, vae=vae, latent=latent,
                context_length=ctx_label, audio_context_length=a_ctx,
                context_latent=prev_sampled,
            )
            print(
                f"[{TAG}] Motion-Context pin: previous hop latent "
                f"({ctx_label}f picture, {a_ctx}f audio, trim {trim})",
                flush=True,
            )
            return cond, "motion_context"
        except Exception as e:
            # This downgrade changes the join, so it is logged with the real
            # exception rather than swallowed -- repr(), because a bare
            # TypeError from a renamed upstream kwarg stringifies to nothing
            # useful.
            print(
                f"[{TAG}] Motion-Context pin failed ({e!r}); AddGuide pixel pin",
                flush=True,
            )
    if mc is None:
        print(
            f"[{TAG}] Motion-Context not available; AddGuide pixel pin "
            f"({overlap_n}f). Install ComfyUI-H3-Motion-Context for a latent join.",
            flush=True,
        )
    elif prev_sampled is None:
        print(
            f"[{TAG}] previous hop has no sampler latent (cache hit); "
            f"AddGuide pixel pin ({overlap_n}f)",
            flush=True,
        )
    pin_image = prev_imgs[-overlap_n:] if prev_imgs is not None else None
    pin_audio = _tail_audio(prev_audio, overlap_n) if prev_audio is not None else None
    if pin_image is None and pin_audio is None:
        return cond, "none"
    return _result(MiniMaxH3AddGuide.execute(
        cond, latent, 0,
        vae=vae if pin_image is not None else None,
        audio_vae=audio_vae if pin_audio is not None else None,
        image=pin_image,
        audio=pin_audio,
    ))[0], "addguide_pixels"


def _collect_ref_images(slot_images):
    """Dense-pack the wired slots into <Picture N> order.

    Takes the slot -> tensor map the caller already built, rather than reading
    the nine node inputs a second time. Gathering them twice -- once here, once
    for `slot_images` -- is how a slot goes missing from one of the two and
    silently renumbers every later <Picture N>, which is the exact failure
    refs.py exists to prevent.
    """
    frames = [slot_images[s] for s in sorted(slot_images)]
    if not frames:
        return None
    bits = []
    for i, im in enumerate(frames):
        bits.append(f"<Picture {i + 1}> {int(im.shape[2])}x{int(im.shape[1])}")
    print(f"[{TAG}] {len(frames)} reference image(s) -> " + ", ".join(bits), flush=True)
    if len(frames) < 3:
        print(f"[{TAG}] warning: fewer than 3 stills. A reference with no "
              "picture chosen does not count.", flush=True)
    return {f"ref_image_{i + 1}": frames[i] for i in range(len(frames))}


def _audio_samples(frames, sr):
    return max(1, int(round(frames / float(FPS) * int(sr))))


def _tail_audio(audio, frames):
    wav = audio["waveform"]
    sr = int(audio["sample_rate"])
    n = min(_audio_samples(frames, sr), int(wav.shape[-1]))
    return {"waveform": wav[..., -n:].contiguous(), "sample_rate": sr}


def _trim_audio_head(audio, frames):
    wav = audio["waveform"]
    sr = int(audio["sample_rate"])
    n = min(_audio_samples(frames, sr), int(wav.shape[-1]))
    return {"waveform": wav[..., n:].contiguous(), "sample_rate": sr}, n


def _xfade_audio(left, right, sr, ms=40):
    n = max(1, int(sr * ms / 1000.0))
    k = min(n, int(left.shape[-1]), int(right.shape[-1]))
    if k < 8:
        return torch.cat([left, right], dim=-1)
    t = torch.linspace(0, 1, k, dtype=left.dtype, device=left.device)
    fade_out = torch.cos(t * math.pi / 2)
    fade_in = torch.sin(t * math.pi / 2)
    while fade_out.ndim < left.ndim:
        fade_out = fade_out.unsqueeze(0)
        fade_in = fade_in.unsqueeze(0)
    seam = left[..., -k:] * fade_out + right[..., :k] * fade_in
    return torch.cat([left[..., :-k], seam, right[..., k:]], dim=-1)


def _frame_to_jpeg_b64(frame, max_side=512, quality=80):
    from PIL import Image
    arr = (frame.detach().float().cpu().numpy() * 255.0).clip(0, 255).astype("uint8")
    if arr.ndim == 3 and arr.shape[-1] > 3:
        arr = arr[..., :3]
    img = Image.fromarray(arr)
    w, h = img.size
    scale = min(1.0, float(max_side) / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = pyio.BytesIO()
    img.save(buf, format="JPEG", quality=int(quality))
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size


class _PreviewEncoder:
    """Encode preview JPEGs off the sampling thread.

    Encoding inline cost the sampler a PIL resize plus a JPEG write at every
    push. The queue is bounded and *drops* when full: a preview frame is worth
    nothing if delivering it slows the render that produced it.
    """

    def __init__(self, depth=2):
        self._q = queue.Queue(maxsize=depth)
        self._t = None

    def _run(self):
        while True:
            job = self._q.get()
            if job is None:
                return
            payload, frames = job
            try:
                for key, frame in frames.items():
                    b64, (w, h) = _frame_to_jpeg_b64(frame)
                    payload[key] = b64
                    if key == "image":
                        payload["w"], payload["h"] = w, h
            except Exception as e:
                print(f"[{TAG}] preview encode skipped: {e!r}", flush=True)
            try:
                PromptServer.instance.send_sync(
                    "h3_refchain_preview", payload, PromptServer.instance.client_id)
            except Exception as e:
                print(f"[{TAG}] preview send skipped: {e!r}", flush=True)

    def submit(self, payload, frames):
        if self._t is None:
            self._t = threading.Thread(target=self._run, name="h3rc-preview",
                                       daemon=True)
            self._t.start()
        try:
            self._q.put_nowait((payload, frames))
        except queue.Full:
            pass  # deliberate: never block the sampler for a preview


_PREVIEW = _PreviewEncoder()


def _push_preview(unique_id, status, frame=None, hop=0, total=0,
                  pin_mech=None, frac=None, seam_frame=None, meta=None):
    """Send one preview update.

    `status` stays a SHORT label. The full per-hop prompt dump belongs on the
    `info` output -- passing it here once turned the status strip into the
    prompt. Everything structured goes in its own field instead, which is what
    a panel can actually lay out.
    """
    if not unique_id or PromptServer is None:
        return
    payload = {
        "node_id": unique_id,
        "status": status,
        "hop": int(hop),
        "total": int(total),
    }
    if pin_mech:
        payload["pin_mech"] = str(pin_mech)
    if frac is not None:
        payload["frac"] = max(0.0, min(1.0, float(frac)))
    if meta:
        payload.update(meta)
    frames = {}
    if frame is not None:
        frames["image"] = frame
    if seam_frame is not None:
        frames["seam_image"] = seam_frame
    _PREVIEW.submit(payload, frames)


def _offload_text_encoder(clip, model):
    te_dev = getattr(clip.patcher, "load_device", None)
    dit_dev = getattr(model, "load_device", None)
    if te_dev is not None and dit_dev is not None and str(te_dev) != str(dit_dev):
        return
    try:
        clip.patcher.model.to(mm.text_encoder_offload_device())
    except Exception as e:
        print(f"[{TAG}] TE offload skipped: {e}", flush=True)
        return
    try:
        dev = mm.get_torch_device()
        mm.free_memory(mm.get_total_memory(dev) * 0.9, dev)
        mm.soft_empty_cache()
        free = mm.get_free_memory(dev) / (1024 ** 3)
        print(f"[{TAG}] TE evicted; {free:.1f} GB free for the DiT", flush=True)
    except Exception as e:
        print(f"[{TAG}] VRAM purge skipped: {e}", flush=True)


def _decode_av(video_vae, audio_vae, latent):
    imgs = VAEDecode().decode(video_vae, latent)[0]
    audio = vae_decode_audio(audio_vae, latent)
    return imgs, audio


class HandTieClips:
    """Refs + shot plan + N hops, assembled into one clip.

    Each hop after the first pins the previous hop's sampler latent through
    Motion-Context, falling back to an AddGuide pixel pin when that is not
    available. See the module docstring.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "prompt": ("STRING", {
                    "multiline": True,
                    "dynamicPrompts": False,
                    "default": (
                        "Live-action, natural indoor light. The person looks exactly "
                        "as in the reference photographs.\n\n"
                        "They sit at a table, look up, and speak one short line. "
                        "Then they settle, watching the room."
                    ),
                    "tooltip": (
                        "Hop 1 prompt. Separate hops with --- on its own line, "
                        "or JSON {\"prompts\": [...]}. hop_script=verbatim: one block "
                        "+ chains>1 wraps later hops. hop_script=next: later --- blocks "
                        "are only 'what happens next'."
                    ),
                }),
                "chains": (["1", "2", "3", "4", "5", "6", "7", "8"], {
                    "default": "3",
                    "tooltip": "How many generates to run and join. 3 at 10 s is about 28 s of master after the overlap trim.",
                }),
                "resolution": (list(RESOLUTIONS), {
                    "default": DEFAULT_RESOLUTION,
                    "tooltip": "Output area. 0.98 MP is the top rung because H3 caps at 768x1344 (1.03 MP); 16:9 there is 1312x736. Every size is snapped to H3's 32 px grid and kept under the cap.",
                }),
                "aspect": (list(ASPECTS), {
                    "default": DEFAULT_ASPECT,
                    "tooltip": "Frame shape. Combined with resolution to set width and height. Widest first, then square, then the portraits.",
                }),
                "duration": (["5 s", "7 s", "8 s", "10 s", "15 s"], {
                    "default": "10 s",
                    "tooltip": "Length of each hop at 24 fps (H3 17k+5 grid: 124 / 192 / 243 / 362 frames). 5 s (124f) drops the airlock on a continuous join; use 8 s or 15 s to validate a seam.",
                }),
                "overlap": (["0.9 s", "0.2 s", "1.6 s"], {
                    "default": "0.9 s",
                    "tooltip": "Pinned clip from the previous hop at frame 0. 0.9 s (22 frames) is the native continuation length.",
                }),
                "seed": ("INT", {
                    "default": 0, "min": 0, "max": 0xffffffffffffffff,
                    "control_after_generate": True,
                }),
                "seed_per_shot": ("BOOLEAN", {
                    "default": True,
                    "label_on": "vary per hop",
                    "label_off": "same seed every hop",
                }),
                "steps": ("INT", {"default": 14, "min": 1, "max": 50}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "res_multistep"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "beta"}),
                "shift_video": ("FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                "shift_audio": ("FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                "ref_image_size": (["match", "max"], {
                    "tooltip": "match = faster. max = 2048 short-edge identity, slower every step.",
                }),
            },
            "optional": {
                # The nine ref_image_N sockets, plus reference_video, voice
                # and start_image, used to live at the TOP of this block --
                # twelve of a sixteen-socket column that occupied a third of
                # the node before the editor started. They are files now, and
                # their replacements are appended at the BOTTOM of `optional`
                # instead. See the note beside them: widget ORDER is part of
                # the saved-workflow format.
                "hop_script": (["verbatim", "next"], {
                    "default": "verbatim",
                    "tooltip": (
                        "verbatim: your text is the hop prompt. next: hop 1 is the first "
                        "block; every later block is only the new beat. One block + next: "
                        "hops 2+ advance without replaying the opening."
                    ),
                }),
                "pin_to_qwen": (["off", "last frame", "pin clip", "both"], {
                    "default": "last frame",
                    "tooltip": (
                        "AddGuide is invisible to the text encoder. last frame = "
                        "<Picture 1> of the previous hop's last frame (identity stills "
                        "shift to Picture 2+). pin clip = overlap as extra <Video>. "
                        "Voice stays <Audio 1>."
                    ),
                }),
                "continuity_state": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "forceInput": True,
                    "tooltip": (
                        "Optional JSON continuity state from HTCContinuityState (or a String "
                        "Primitive node for hand-typed JSON). hop_script=next only: locked + "
                        "context text rides every hop 2+, mutable beats are indexed per hop. "
                        "Unwired = no effect."
                    ),
                }),
                "shot_plan": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": (
                        "Shot plan JSON: {\"shots\":[{\"beat\":\"...\","
                        "\"directives\":{\"join\":\"continuous\"}}, ...]}. "
                        "The shot count is the hop count, so `chains` is ignored. "
                        "Directives compile to vetted continuity prose; `prose` per shot "
                        "is appended verbatim. Blank = use the `prompt` widget instead."
                    ),
                }),
                "ref_plan": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": (
                        "Reference register JSON: {'refs':[{'tag':'hero_face',"
                        "'file':'face.png','subject':1,'retention':'fully_preserved'}]}. "
                        "'file' is a picture in the reference folder, chosen in the panel. "
                        "Beats refer to refs by @tag, resolved to the correct "
                        "<Picture N> per hop, so removing or scheduling off a ref "
                        "never renumbers the others. Refs sharing a 'subject' are "
                        "the same person; different numbers stay different people. "
                        "Blank = positional behaviour: refs are read in slot order."
                    ),
                }),
                "cache_hops": (["off", "on"], {
                    "default": "off",
                    "tooltip": (
                        "Store each hop losslessly on disk, keyed by a chained "
                        "content hash. Unchanged hops load instead of re-rendering, "
                        "so editing only the last shot re-renders only that shot, "
                        "and an interrupted chain resumes. Editing an early shot "
                        "correctly invalidates every hop after it."
                    ),
                }),
                "cache_budget_gb": ("FLOAT", {
                    "default": 20.0, "min": 1.0, "max": 500.0, "step": 1.0,
                    "tooltip": "Least-recently-used hops are evicted above this size.",
                }),
                "audio_pin_frames": ("INT", {
                    "default": 24, "min": 0, "max": 240, "step": 24,
                    "tooltip": (
                        "Audio context handed to the Motion-Context pin, in frames. "
                        "24 is one second and lands on the model's 40 Hz audio grid; "
                        "multiples of 24 keep whole seconds. Longer audio context "
                        "costs conditioning rows but NO delivered frames, so it is "
                        "the cheap lever on speech that breaks across a join -- try "
                        "96 (4 s) for continuous dialogue. 0 follows the picture "
                        "overlap. Video pin length is not adjustable here: it "
                        "follows the overlap widget."
                    ),
                }),
                "pin_renorm": (["off", "sigma", "band"], {
                    "default": "off",
                    "tooltip": (
                        "Rescale each pinned latent back toward the first pinned "
                        "hop's, to fight the texture ratchet -- measured at +4.2% "
                        "mid-band per join, flat inside each hop. Both modes are "
                        "scalar rescales, so neither moves structure or can blur "
                        "detail. "
                        "band: match the HIGH-BAND FRACTION, the statistic the "
                        "ratchet actually moves. "
                        "sigma: match total spread -- the original lever, kept "
                        "for old workflows, and measurably the wrong statistic: "
                        "total sigma FALLS across a chain whose picture is "
                        "baking, so it corrects the wrong way. Saved as `on` "
                        "before 0.5. "
                        "off leaves every pin untouched. The log prints "
                        "`pin drift` every hop either way, so you can read the "
                        "ratchet without changing anything."
                    ),
                }),
                "pin_noise": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 0.10, "step": 0.005,
                    "tooltip": (
                        "Mix seeded noise into the pinned latent before it "
                        "conditions the next hop -- the other half of the texture "
                        "ratchet fix. Small values only: measured gains fall off "
                        "and reverse above 0.10, which is why the range stops "
                        "there. 0.0 leaves the pin untouched; 0.05 is the "
                        "suggested starting point."
                    ),
                }),
                "tone_compensate": (_tone.MODES, {
                    "default": "off",
                    "tooltip": (
                        "Undo the denoiser's tone bias on each hop, measured on the "
                        "overlap that hop regenerated. The estimate needs both copies "
                        "of the overlap, which only exist inside this node -- a "
                        "downstream node cannot do this. frame_shift is the mode that "
                        "suits regenerated content; gain_bias is more robust; lut "
                        "overfits. Enabling any mode also clamps the master to 0..1."
                    ),
                }),
                # ---------------------------------------------------------
                # APPEND-ONLY ZONE. `widgets_values` in a saved workflow is a
                # POSITIONAL array -- ComfyUI restores value[i] into widget[i]
                # and never checks the name. A widget inserted anywhere but the
                # end renumbers everything after it, and every previously saved
                # workflow silently loads its values into the wrong widgets.
                #
                # These three were first added at the top of `optional`, which
                # shifted hop_script..tone_compensate by +3. `ref_plan` then
                # received audio_pin_frames' integer and the editor threw
                # `(text || "").trim is not a function` on load -- the only
                # visible symptom of a much wider silent corruption.
                #
                # Add new widgets HERE, at the bottom. Old workflows are then
                # short rather than misaligned, and the new widget takes its
                # default.
                # ---------------------------------------------------------
                "start_image_file": ("STRING", {
                    "default": "",
                    "tooltip": "First-frame pin for hop 1 only. Set in the panel.",
                }),
                "reference_video_file": ("STRING", {
                    "default": "",
                    "tooltip": "Motion/look plate. Not the previous hop. Set in the panel.",
                }),
                "voice_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Voice or timbre reference for hop 1, cited as <Audio 1>. "
                        "Later hops use the audio pin instead -- an uncited "
                        "timbre clip on a quiet hop fills leftover frames with "
                        "that recording. Set in the panel."
                    ),
                }),
                # Appended here on 2026-08-29, per the note above: LAST, so
                # every saved workflow keeps its widget alignment and simply
                # takes the default.
                "establish": ("STRING", {
                    # Single line on purpose. The editor hides only the widgets
                    # it explicitly owns, so this one renders as a native widget
                    # on the node body -- and one sentence in a one-line box is
                    # discoverable where a multiline textarea would be a slab.
                    "default": _d.ESTABLISH,
                    "tooltip": (
                        "Opening line prepended to hop 1 only, before the beat. "
                        "The default asserts live action; clear it, or replace it "
                        "with your own medium, for anything else. Dropped "
                        "automatically when shot 1 already names a medium."
                    ),
                }),
                # Appended 2026-08-30, still obeying the append-only rule above.
                "render_through": ("INT", {
                    "default": 0, "min": 0, "max": 64,
                    "tooltip": (
                        "Stop after this many hops. 0 renders the whole plan. "
                        "With cache_hops=on the hops you already rendered are "
                        "kept, so 3 then 5 then 8 builds a chain up in stages "
                        "and only ever renders the new hops. The plan is not "
                        "changed -- shot 4 still knows it is shot 4."
                    ),
                }),
                "quality": (["final", "draft"], {
                    "default": "final",
                    "tooltip": (
                        "draft forces 0.3 MP and 6 steps for a fast structural "
                        "read of the whole chain -- does the story hold, do the "
                        "joins land. Resolution and steps are both in the cache "
                        "key, so drafts and finals never overwrite each other; "
                        "they simply cost two entries."
                    ),
                }),
                "dry_run": (["off", "on"], {
                    "default": "off",
                    "tooltip": (
                        "Compile every hop's prompt and stop -- no sampling, no "
                        "model, seconds not minutes. Read them on `info`, or as "
                        "a page on `contact_sheet`. This is the only way to see "
                        "what the text encoder will actually receive before "
                        "paying for it."
                    ),
                }),
                "contact_sheet": (["off", "on"], {
                    "default": "off",
                    "tooltip": (
                        "Build the `contact_sheet` output: one row per hop with "
                        "its first and last delivered frame, its beat, its "
                        "directives and what happened to it. Wire it to a Save "
                        "Image. Always built during a dry run."
                    ),
                }),
                "tone_anchor": ("FLOAT", {
                    "default": _tone.ANCHOR_STRENGTH, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": (
                        "Strength of tone_compensate=anchor's pull back toward "
                        "hop 1's exposure. Ignored by every other mode. 0 "
                        "disables the pull and leaves plain frame_shift; 0.35 "
                        "closes about a third of the gap per hop, which arrests "
                        "a long slide without visibly pumping. A shot can opt "
                        "out with \"tone\": \"free\" or move the anchor to itself "
                        "with \"tone\": \"rebase\"."
                    ),
                }),
                # APPENDED, never inserted -- the same rule the ref sockets at
                # the top of this block were deleted for. `widgets_values` is a
                # positional list, so a widget added anywhere but the end shifts
                # every widget after it, and a workflow saved before the change
                # reads its own settings out of the wrong slots. Silently: the
                # numbers all still parse, they are just the wrong numbers.
                "soundtrack": ("AUDIO", {
                    "tooltip": (
                        "Optional music bed under the whole chain, mixed in once "
                        "after the last hop is joined. A mix, not a replacement: "
                        "H3's own dialogue and effects stay. Wire a Load Audio, "
                        "or anything with an AUDIO output. Unwired, the audio "
                        "output is untouched."
                    ),
                }),
                "music_gain_db": ("FLOAT", {
                    "default": -14.0, "min": -60.0, "max": 6.0, "step": 0.5,
                    "tooltip": (
                        "Level of the bed against the generated audio. -14 sits "
                        "a track under speech without fighting it; -6 is a "
                        "music-led cut. Push it far enough and the peak guard "
                        "trims the whole mix rather than let it clip -- which it "
                        "says in `info` rather than doing quietly."
                    ),
                }),
                "music_duck": ("FLOAT", {
                    "default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": (
                        "Pull the bed down while anyone is talking and let it "
                        "back up in the gaps. 0 is off: a flat bed at "
                        "music_gain_db and nothing else. 0.6 drops it about 8 dB "
                        "under speech, which is what keeps dialogue intelligible "
                        "under a loud track. Fast attack, slow release, no "
                        "model -- same result every run."
                    ),
                }),
                "music_fit": (["loop", "once"], {
                    "default": "loop",
                    "tooltip": (
                        "loop: repeat the track to cover the chain, crossfading "
                        "each wrap so it cannot click. once: play it through and "
                        "leave silence after. A track longer than the chain is "
                        "trimmed either way."
                    ),
                }),
                "music_fade_s": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.25,
                    "tooltip": (
                        "Seconds of fade on the bed at the start and end of the "
                        "finished chain, so it neither begins on a cut nor stops "
                        "on a dropout. Also sets the loop crossfade length."
                    ),
                }),
                # The picker's half of the soundtrack, and the one most people
                # use. Same shape as voice_file: a basename under h3_refs, set
                # in the panel. LAST, per the note at the top of this block.
                "soundtrack_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Music bed as a filename, set in the panel next to the "
                        "voice reference. The `soundtrack` socket wins when both "
                        "are set."
                    ),
                }),
                # Trim windows, appended 2026-09-01 -- LAST, per the note at
                # the top of this block. Six floats rather than one JSON blob
                # because these three slots are fixed and named, the same
                # reason `voice_file` is its own widget. The rail's references
                # are a LIST, which is why their per-item settings live in
                # `ref_plan` instead.
                #
                # An end of 0.0 is the sentinel for "to the end of the file",
                # so the default pair (0, 0) is untrimmed and costs nothing.
                # media.clip_window is the single definition of what they mean.
                "voice_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": (
                        "Trim window into the voice reference, in seconds. Leave both at 0 for "
                        "the whole file; an end of 0 always means "
                        "'to the end', so a longer replacement file "
                        "still plays out. "
                        "Worth setting: H3 encodes the WHOLE voice file into the "
                        "conditioning with no cap, and every latent frame of it "
                        "is attended over on every step of every hop. A "
                        "three-minute take is a large invisible tax."
                    ),
                }),
                "voice_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "End of the voice window. 0 = to the end of the file.",
                }),
                "reference_video_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": (
                        "Trim window into the reference clip, in seconds. Leave both at 0 for "
                        "the whole file; an end of 0 always means "
                        "'to the end', so a longer replacement file "
                        "still plays out. "
                        "H3 already truncates the clip to the hop length, but "
                        "only from frame 0 -- so without this there is no way to "
                        "point at the motion you actually want."
                    ),
                }),
                "reference_video_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "End of the reference clip window. 0 = to the end.",
                }),
                "music_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": (
                        "Trim window into the soundtrack, in seconds. Leave both at 0 for "
                        "the whole file; an end of 0 always means "
                        "'to the end', so a longer replacement file "
                        "still plays out. "
                        "The window is cut from the TRACK first; music_fit then "
                        "loops or trims that to the chain. Without it a mastered "
                        "track always starts the chain on its intro."
                    ),
                }),
                "music_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "End of the soundtrack window. 0 = to the end.",
                }),
                # Appended 2026-09-03 -- LAST, per the note at the top of this
                # block. `render_through` has been here since 0.4 and stops the
                # chain; this is the other end of the same range.
                "render_from": ("INT", {
                    "default": 0, "min": 0, "max": 64,
                    "tooltip": (
                        "Start at this hop instead of hop 1. 0 starts at the "
                        "beginning. Everything before it is replayed from the "
                        "hop cache rather than rendered, so re-running one shot "
                        "in the middle of a long chain costs that shot. "
                        "Needs cache_hops=on, and every earlier hop must "
                        "already be in the cache -- it names the first one that "
                        "is not rather than guessing at the join. Pair it with "
                        "render_through to render a range."
                    ),
                }),
                "reference_video_desc": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "What the reference clip is for, in your words -- "
                        "\"a slow dolly along a counter\", \"the way she turns "
                        "and looks back\". The clip goes in as <Video 1> either "
                        "way; this is the only thing that tells the encoder "
                        "why it is there, and a reference the prompt never "
                        "explains tends to get rendered as the shot. Leave it "
                        "empty and the prompt is exactly what it was."
                    ),
                }),
                # Appended 2026-09-03 -- LAST, per the note at the top of this
                # block.
                "reference_video_size": (list(_media.VIDEO_SIZES), {
                    "default": _media.DEFAULT_VIDEO_SIZE,
                    "tooltip": (
                        "How large to decode the reference clip, as an area "
                        "budget. MAX asks core what it would resize the clip "
                        "to anyway, so the model sees the same pixels and the "
                        "memory is not spent -- a 10 s 4K plate costs about "
                        "36 GB of system RAM decoded at source and about "
                        "4.5 GB at MAX. The megapixel values go below that, "
                        "trading reference detail for memory. A clip already "
                        "smaller than the value you pick is left alone; "
                        "nothing here ever scales up. Megapixels are decimal "
                        "here -- 0.5 MP is 500,000 pixels, whatever the clip's "
                        "aspect ratio, which is the point of budgeting by area "
                        "rather than by edge."
                    ),
                }),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    # contact_sheet is APPENDED, never inserted: a saved workflow's links name
    # an output by index, so putting it anywhere but last would silently rewire
    # every graph that already uses this node.
    RETURN_TYPES = ("IMAGE", "AUDIO", "STRING", "IMAGE")
    RETURN_NAMES = ("images", "audio", "info", "contact_sheet")
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = (
        "MiniMax H3 Ref2VA chain. Hop 1 is a full generate; every later hop is a "
        "continuation pinned to the previous hop's sampler latent via "
        "Motion-Context, falling back to an AddGuide pixel pin when that is "
        "unavailable. Author with shot_plan + ref_plan; hop_script=next treats "
        "later blocks as what-happens-next; pin_to_qwen shows the incoming frame "
        "to the text encoder; continuity_state (from HTCContinuityState) carries "
        "locked/context setting text forward."
    )

    @classmethod
    def IS_CHANGED(cls, ref_plan="", start_image_file="",
                   reference_video_file="", voice_file="",
                   soundtrack_file="", **_):
        """Re-run when a reference file changes underneath its name.

        Every picture now arrives as a basename, and a basename is a stable
        input: overwrite `face.png` with a different face and ComfyUI would
        happily serve the previous render. Hashing path+mtime is the fix.

        Deliberately NOT `float("nan")` -- that is the blunt version of this and
        would force a full re-render of an expensive node on every queue.
        """
        names = [start_image_file, reference_video_file, voice_file,
                 soundtrack_file]
        try:
            for r in (_refs.parse_ref_plan(ref_plan).get("refs") or []):
                if r.get("file"):
                    names.append(r["file"])
        except Exception:
            # A malformed plan is run()'s error to report, with a message that
            # names the field. Raising here would surface as a cache failure.
            pass
        return _media.stamp(names)

    def run(self, model, clip, vae, audio_vae, prompt,
            chains, resolution,
            aspect, duration, overlap, seed, seed_per_shot, steps,
            sampler_name, scheduler, shift_video, shift_audio, ref_image_size,
            start_image_file="", reference_video_file="", voice_file="",
            hop_script="verbatim", pin_to_qwen="last frame", continuity_state="",
            shot_plan="", ref_plan="", cache_hops="off", cache_budget_gb=20.0,
            audio_pin_frames=24, pin_renorm="off", pin_noise=0.0,
            tone_compensate="off", establish=None,
            render_through=0, quality="final", dry_run="off",
            contact_sheet="off", tone_anchor=_tone.ANCHOR_STRENGTH,
            soundtrack=None, music_gain_db=-14.0, music_duck=0.6,
            music_fit="loop", music_fade_s=1.0, soundtrack_file="",
            voice_start_s=0.0, voice_end_s=0.0,
            reference_video_start_s=0.0, reference_video_end_s=0.0,
            music_start_s=0.0, music_end_s=0.0, render_from=0,
            reference_video_desc="",
            reference_video_size=None,
            unique_id=None):
        # First thing, before a single model is touched: hand the writer's VRAM
        # back. The plan writer stays resident between plans now, which is the
        # right trade everywhere except here -- a 27B and an H3 render do not
        # share a card, and "write a plan, then queue" would OOM in a way that
        # reads as this node's fault.
        #
        # This BLOCKS, deliberately. `run()` is the execution worker thread, not
        # the aiohttp event loop that `llm.py`'s no-blocking rule protects, and
        # the render is precisely what the VRAM is being freed for. Costs
        # nothing when the writer was never configured or nothing is resident,
        # which is almost every render; see `llm.free_for_render`.
        try:
            from . import llm as _llm
            _llm.free_for_render()
        except Exception as _le:  # noqa: BLE001
            print(f"[{TAG}] writer unload skipped ({_le!r})", flush=True)

        dry = str(dry_run) == "on"
        draft = str(quality) == "draft"
        want_sheet = str(contact_sheet) == "on" or dry
        if draft:
            # Both of these are already in the cache key, so a draft cannot
            # collide with the final it is standing in for.
            resolution, steps = DRAFT_RESOLUTION, min(int(steps), DRAFT_STEPS)
            print(f"[{TAG}] draft: {DRAFT_RESOLUTION}, {steps} steps", flush=True)
        width, height = _canvas(resolution, aspect)
        # Nothing downstream checks this. Core takes the generation canvas on
        # trust and floor-divides it by 16 for the latent, so an off-grid size
        # does not raise -- it renders a smaller frame than `master_imgs` is
        # allocated for and the mismatch surfaces later as a shape error with no
        # obvious cause. Fail here, where the size and its two inputs are known.
        if width % CANVAS_MULTIPLE or height % CANVAS_MULTIPLE:
            raise ValueError(
                f"canvas {width}x{height} from resolution={resolution!r} "
                f"aspect={aspect!r} is not a multiple of {CANVAS_MULTIPLE}; "
                "H3 cannot render it.")
        print(f"[{TAG}] canvas {width}x{height} "
              f"({width * height / 1e6:.2f} MP, {width / height:.3f}:1) "
              f"from {resolution} {aspect}", flush=True)
        length = align_frame_count(_duration_frames(duration))
        overlap_n = _overlap_frames(overlap)

        # A shot plan is authoritative when present: its shot count is the hop
        # count, and every shot after the first is a beat, so `next` semantics
        # are the only correct reading of it.
        shots = _plan.parse_plan(shot_plan)
        if shots:
            if str(hop_script) != "next":
                print(f"[{TAG}] shot_plan present -> hop_script=next", flush=True)
            hop_script = "next"
            if int(chains) != len(shots):
                print(f"[{TAG}] shot_plan has {len(shots)} shot(s); "
                      f"chains={chains} ignored", flush=True)
            # The place-handoff check needs to know which tags are places,
            # which only the ref plan says. Parsed defensively here: the
            # authoritative parse (and its error) is still the one below, so a
            # malformed ref_plan fails in the same place it always did.
            try:
                _rp_for_check = _refs.parse_ref_plan(ref_plan)
            except Exception:
                _rp_for_check = None
            blocks = _plan.compile_blocks(shots, establish, _rp_for_check)
            unique = len(shots)
            print(f"[{TAG}] shot plan:\n" + _plan.describe(shots), flush=True)
            for i, sh in enumerate(shots):
                if i > 0 and _d.is_full_h3_prompt((sh or {}).get("beat")):
                    print(
                        f"[{TAG}] hop {i + 1}: full H3 prompt flattened to a "
                        "continuation beat (a complete Ref2VA block on hop 2+ "
                        "starts a new scene)",
                        flush=True,
                    )
        else:
            shots = [None] * int(chains)
            blocks, unique = _expand_shots(
                _parse_shots(prompt), int(chains), hop_script=str(hop_script))
        n = len(blocks)

        # render_through truncates the RENDER, never the plan: the shots that
        # survive keep their own indices, seeds and cache keys, so extending the
        # stop point later re-uses every hop already on disk instead of
        # renumbering them into fresh misses.
        stop_at = int(render_through or 0)
        # render_from is the other end of the same range, and unlike
        # render_through it truncates nothing at all: the hops before it still
        # run through the loop, they just have to come out of the cache instead
        # of the sampler. Validated against the store further down, once there
        # is a store to validate against.
        start_at = int(render_from or 0)
        # An impossible range is refused BEFORE anything is announced. Both
        # halves of that matter. Against the original `render_through`, because
        # `n` is truncated to it below and testing against the truncated value
        # reports an inverted range as "past the end of the plan" and then
        # renders everything. And before the print below, because that print
        # says "rendering hops 1-2" -- which was the last thing in the log
        # ahead of a refusal to render anything at all.
        if start_at > 1 and 0 < stop_at < start_at:
            raise ValueError(
                f"{TAG}: render_from={start_at} is past "
                f"render_through={stop_at}, so the range is empty. "
                "render_through is the LAST hop to render, not a count.")

        if 0 < stop_at < n:
            print(f"[{TAG}] render_through={stop_at}: rendering hops 1-{stop_at} "
                  f"of {n}; the rest of the plan is untouched", flush=True)
            blocks = blocks[:stop_at]
            shots = shots[:stop_at]
            n = stop_at
        elif stop_at > n:
            print(f"[{TAG}] render_through={stop_at} is past the end of a "
                  f"{n}-hop plan; rendering all of it", flush=True)

        if start_at > n:
            print(f"[{TAG}] render_from={start_at} is past the end of a "
                  f"{n}-hop plan; starting at hop 1", flush=True)
            start_at = 0
        replay_before = max(0, start_at - 1)
        if replay_before:
            # Checked here rather than beside the hop store, which is built much
            # further down -- after the master tensor, which at 8 x 15 s and
            # 1280x736 is ~31 GB. A misconfigured range must not cost that
            # allocation before it is told it is misconfigured.
            if dry:
                raise ValueError(
                    f"{TAG}: render_from={start_at} needs the hop cache, and a "
                    "dry run never touches it. Use render_through to limit what "
                    "a dry run compiles.")
            if str(cache_hops) != "on":
                raise ValueError(
                    f"{TAG}: render_from={start_at} replays hops 1-"
                    f"{replay_before} from the hop cache, so cache_hops must be "
                    "on. With it off there is nothing to replay from, and the "
                    f"join into hop {start_at} would be invented rather than "
                    "continued.")
            print(f"[{TAG}] render_from={start_at}: hops 1-{replay_before} come "
                  f"from the cache, {start_at}-{n} render", flush=True)

        # Per-shot duration overrides, validated up front so a bad value fails
        # before any sampling happens rather than three hops in.
        lengths = []
        for i, sh in enumerate(shots):
            dur = (sh or {}).get("duration") if sh else None
            if dur and str(dur) not in DURATION_FRAMES:
                raise ValueError(
                    f"{TAG}: shot {i + 1}: duration '{dur}' is not valid. "
                    f"Use one of: {', '.join(DURATION_FRAMES)}"
                )
            lengths.append(align_frame_count(_duration_frames(dur or duration)))
        if str(hop_script) == "next":
            for i, sh in enumerate(shots):
                if i == 0:
                    continue
                join = ((sh or {}).get("directives") or {}).get("join")
                dur = ((sh or {}).get("duration") if sh else None) or duration
                if join == "continuous" and str(dur) == "5 s":
                    print(
                        f"[{TAG}] note: shot {i + 1} is join=continuous at 5 s "
                        "(124f). That budget drops the airlock; 8 s / 15 s is "
                        "the join-validation canvas. A lucky seed can still "
                        "join at 5 s.",
                        flush=True,
                    )
        for i, ln in enumerate(lengths):
            if overlap_n >= ln:
                raise ValueError(
                    f"{TAG}: shot {i + 1}: overlap {overlap} ({overlap_n} frames) must "
                    f"be smaller than duration ({ln} frames)"
                )
        state = _parse_state(continuity_state)
        # The three singles first, so a name that does not resolve is reported
        # before anything expensive starts. Each returns exactly what the socket
        # it replaced delivered, so everything downstream is unchanged.
        start_image = _media.load_image(start_image_file) if start_image_file else None
        # max_frames: core truncates a reference clip to the hop's frame count
        # (`frames[:frame_count]` in nodes_minimax_h3.py) AFTER decoding all of
        # it, so a 60 s plate on a 10 s hop decodes 1440 frames to use 243. The
        # longest hop is the upper bound -- never the shortest, or a clip would
        # be cut before core had the chance not to need it.
        reference_video = (_media.load_video(
            reference_video_file,
            max_frames=max(lengths) if lengths else length,
            start=float(reference_video_start_s), end=float(reference_video_end_s),
            size=reference_video_size or _media.DEFAULT_VIDEO_SIZE)
            if reference_video_file else None)
        voice = (_media.load_audio(voice_file,
                                   start=float(voice_start_s), end=float(voice_end_s))
                 if voice_file else None)
        for _name, _got in (("start_image", start_image_file and start_image is None),
                            ("reference_video", reference_video_file and reference_video is None),
                            ("voice", voice_file and voice is None),
                            ("soundtrack", soundtrack_file and soundtrack is None
                             and _media.resolve(soundtrack_file, kinds={"audio"}) is None)):
            if _got:
                print(f"[{TAG}] note: {_name} file could not be read; continuing "
                      f"without it", flush=True)

        ref_plan_obj = _refs.parse_ref_plan(ref_plan)
        ref_plan_refs = ref_plan_obj["refs"]
        ref_subjects = ref_plan_obj["subjects"]
        # Derived slot -> tensor. The register still needs to know which picture
        # a @tag is pinned to; the slot is now the ref's position in the rail
        # rather than a socket number, and _collect_ref_images below dense-packs
        # and throws it away.
        slot_images = {}
        for _r in ref_plan_refs:
            if not _r["file"]:
                continue
            _im = _media.load_image(_r["file"], cap_mp=_r.get("mp") or 0.0)
            if _im is not None and _im.shape[0] > 0:
                slot_images[_r["slot"]] = _im[:1]
        if ref_plan_refs:
            print(f"[{TAG}] reference register:", flush=True)
            print(_refs.describe(ref_plan_obj), flush=True)
            # A named picture that is not on disk stops the queue. Warning and
            # continuing renders the whole chain with that reference silently
            # inactive, which is the failure the register exists to prevent --
            # and the docs have always promised a stop here.
            _absent = _refs.missing_files(ref_plan_obj, set(slot_images))
            if _absent:
                raise ValueError(
                    f"[{TAG}] reference picture not found in "
                    f"ComfyUI/input/h3_refs: "
                    + "; ".join(f"@{_t} names '{_f}'" for _t, _f in _absent)
                    + ". Drop the file onto that row in the REFERENCES rail, or "
                      "clear the row's picture to render without it.")
            for _w in _refs.check(ref_plan_obj, set(slot_images)):
                print(f"[{TAG}] note: {_w}", flush=True)
            # ref_plan subjects and a `characters` block in continuity_state
            # both feed the prompt header, so filling in both injects identity
            # prose twice. HTCContinuityState no longer emits characters, so this
            # can only come from hand-authored JSON -- still worth warning about
            # rather than raising, since the setting half stays useful.
            _chars = sorted(
                cid for cid, c in (state.get("characters") or {}).items()
                if (c or {}).get("locked") or (c or {}).get("context")
                or (c or {}).get("mutable")
            )
            if ref_subjects and _chars:
                print(f"[{TAG}] note: ref_plan defines subject(s) "
                      f"{sorted(ref_subjects)} and continuity_state also carries "
                      f"character(s) {_chars}. Both inject identity text -- drop "
                      f"the characters block and keep setting only.", flush=True)
        ref_images = _collect_ref_images(slot_images)
        base_videos = None if reference_video is None else {"ref_video_1": reference_video}
        # The author's clip is always <Video 1>: _attach_pin_to_qwen APPENDS the
        # pinned tail (`live_v = len(videos) + 1`), so unlike the stills nothing
        # shifts it. One line, built once, and empty unless the field is filled.
        refvid_line = (_refvid_cite(reference_video_desc, 1)
                       if reference_video is not None else "")
        if refvid_line:
            print(f"[{TAG}] reference clip described: {refvid_line}", flush=True)
        elif reference_video is not None:
            print(f"[{TAG}] note: a reference clip is wired but has no "
                  "description, so it goes to the encoder as <Video 1> with "
                  "nothing saying why. Fill reference_video_desc if the render "
                  "keeps drifting toward the clip.", flush=True)
        ref_audios = None if voice is None else {"ref_audio_1": voice}

        # Fingerprint the model as it arrives -- after whatever LoRA and
        # attention nodes are drawn upstream, before this node touches it.
        # Skipped on a dry run: it only feeds the hop cache key, and a dry run
        # writes no cache. Hashing patched weights is not free.
        model_fp = None if dry else _model_fingerprint(model)

        sampler = base_sigmas = None
        sigma_cache = {}
        if not dry:
            model = _result(MiniMaxH3SigmaShift.execute(model, float(shift_video), float(shift_audio)))[0]
            sampler = _result(KSamplerSelect.execute(sampler_name))[0]
            base_sigmas = _result(BasicScheduler.execute(model, scheduler, int(steps), 1.0))[0]
            sigma_cache = {int(steps): base_sigmas}

        print(
            f"[{TAG}] {n} hop(s), {length}f ({length / FPS:.1f}s) @ {width}x{height} "
            f"({resolution}, {aspect}), overlap {overlap_n}f, "
            f"hop_script={hop_script}, pin_to_qwen={pin_to_qwen}, "
            f"{unique} authored block(s), {steps} steps {sampler_name}/{scheduler}",
            flush=True,
        )

        # Preallocate the master instead of growing it with torch.cat. cat
        # allocates a fresh full-size tensor every hop, so at hop N the old and
        # new masters are briefly live *together* alongside prev_imgs and imgs.
        # Total length is known up front, so one allocation plus slice-writes
        # removes that doubling.
        total_frames = sum(lengths) - overlap_n * (n - 1)
        # A dry run must not allocate the master. At 8 x 15 s and 1280x736 that
        # is 2742 full float frames -- ~31 GB -- for a feature whose entire
        # point is that it costs seconds.
        master_imgs = None if dry else torch.empty(
            (total_frames, int(height), int(width), 3), dtype=torch.float32)
        write_pos = 0
        master_wav = None
        sr = None
        prev_imgs = None
        prev_audio = None
        prev_sampled = None
        pbar = comfy.utils.ProgressBar(n)

        hop_store = None
        # "on" is what pre-0.5 workflows saved for what is now "sigma".
        pin_renorm_mode = {"on": "sigma"}.get(str(pin_renorm), str(pin_renorm))
        if pin_renorm_mode not in ("sigma", "band"):
            pin_renorm_mode = "off"
        pin_noise_v = max(0.0, min(0.10, float(pin_noise)))
        audio_ctx = int(audio_pin_frames) if int(audio_pin_frames) > 0 else int(overlap_n)
        pin_anchor = None   # the first pinned hop sets what 3+ match
        if pin_renorm_mode != "off" or pin_noise_v > 0.0:
            print(f"[{TAG}] pin conditioning enabled: "
                  f"renorm={pin_renorm_mode} "
                  f"noise={pin_noise_v:.3f}", flush=True)
        tone_mode = str(tone_compensate)
        tone_on = tone_mode != "off" and tone_mode in _tone.MODES
        if tone_on:
            print(f"[{TAG}] tone compensation: {tone_mode} "
                  f"(overlap {overlap_n}f)", flush=True)
        if str(cache_hops) == "on" and not dry:
            import folder_paths
            hop_store = _store.HopStore(
                os.path.join(folder_paths.get_temp_directory(), "h3_ref_chain_hops"),
                budget_gb=float(cache_budget_gb), fps=FPS)
            print(f"[{TAG}] hop cache: {hop_store.root} "
                  f"(budget {float(cache_budget_gb):.0f} GB)", flush=True)
        # Note on how render_from works, since this is where the store appears:
        # the leading hops are NOT seeded into prev_imgs / prev_audio /
        # prev_sampled / prev_key from here. They run through the loop like any
        # other hop and are simply required to hit the cache. The hit branch
        # already carries all four forward exactly as a render does -- the
        # sampler latent especially, which decides whether the next hop joins by
        # Motion-Context or falls back to AddGuide -- and a second copy of that
        # logic is a second thing to get subtly and silently wrong.
        # Everything constant across the chain, mixed into every hop key so a
        # resolution or sampler change invalidates the whole cache.
        chain_salt = {
            "w": int(width), "h": int(height),
            # No "overlap" here, for the reason "pin_mech" is not here either
            # and "pin_cond" is keyed only from hop 2: the trim and the pin are
            # both hop-2+ work (`hop 2: dropped 22 frames`, and _pin_mech_for
            # returns "none" for index 0), so hop 1's pixels cannot depend on
            # it. Keyed chain-wide it threw away a byte-identical cached hop 1
            # on every overlap A/B -- half the cost of the test, on the hop the
            # lever does not reach.
            "sampler": str(sampler_name), "scheduler": str(scheduler),
            "shift_v": float(shift_video), "shift_a": float(shift_audio),
            "ref_size": str(ref_image_size), "pin": str(pin_to_qwen),
            # No "pin_mech" here: the mechanism is decided per hop at runtime
            # in _pin_continue (Motion-Context when a sampler latent exists,
            # AddGuide pixels otherwise), so it belongs in the per-hop key
            # below, not in the chain-wide salt.
            # No "refs" here any more -- they are keyed per hop below.
            #
            # Digesting every wired reference chain-wide meant swapping the file
            # behind @outfit moved hop 1's key even when @outfit rides only hop
            # 5, and because the key is chained that re-rendered the entire
            # chain. Changing one late reference cost a full run. A reference
            # can only change the pixels of a hop it is actually handed to, so
            # that is where it belongs.
            "voice": _store.audio_digest(voice),
            "refvid": _store.tensor_digest(reference_video),
            # tensor_digest already covers the pixels, and the pixels change
            # with the size -- but only once the clip is decoded. Naming the
            # setting keeps the key readable when a cache miss has to be
            # explained to somebody.
            "refvid_size": str(reference_video_size or _media.DEFAULT_VIDEO_SIZE),
            "start": _store.tensor_digest(start_image),
            # A cached hop rendered under different LoRAs or a different
            # attention path is not the same hop, so what has been patched onto
            # the incoming model is part of the key. See _model_fingerprint.
            "model": model_fp,
        }
        prev_key = None
        hop_keys = []
        # tone_compensate=anchor state. `anchor_ref` is hop 1's per-channel mean
        # -- the one tone in the chain that nothing drifted into -- and every
        # later hop is eased back toward it. A shot with tone="rebase" moves the
        # reference onto itself, which is how a scene that is genuinely darker
        # from here on stops being fought.
        anchor_on = tone_mode == "anchor" and float(tone_anchor) > 0.0
        anchor_ref = None
        sheet_rows = []

        assembled = []
        for i, block in enumerate(blocks):
            mm.throw_exception_if_processing_interrupted()
            shot = shots[i] or {}
            hop_length = lengths[i]
            print(f"[{TAG}] hop {i + 1}/{n}...", flush=True)
            _push_preview(unique_id, f"hop {i + 1}/{n} sampling…", hop=i + 1, total=n,
                           frac=(write_pos / float(total_frames)) if total_frames else None)

            # With a register, this hop's refs are only the ones active on it,
            # re-packed in slot order. The image dict and the ordinals the prompt
            # cites are built from the same list, so they cannot drift apart.
            hop_active = []
            hop_subject_prose = ""
            if ref_plan_refs:
                hop_active = _refs.active_refs(ref_plan_refs, i, set(slot_images))
                # Continuation: omit shots[] = hop 1 only. Face/outfit plates
                # of a different room (chain_00034) beat the pin as Pictures 1–3
                # and hop 2 opened a new Ref2VA generate — commercial kitchen,
                # apron gone. List hop numbers on a ref to ride later hops.
                if i > 0 and str(hop_script) == "next":
                    dropped = [r["tag"] for r in hop_active if r.get("shots") is None]
                    hop_active = [r for r in hop_active if r.get("shots") is not None]
                    if dropped:
                        print(
                            f"[{TAG}] hop {i + 1}: unscheduled stills stay off "
                            f"this continue ({', '.join('@' + t for t in dropped)}); "
                            f"pin carries wardrobe and room",
                            flush=True,
                        )
                base_images = {
                    f"ref_image_{k + 1}": slot_images[r["slot"]]
                    for k, r in enumerate(hop_active)
                } or None
            else:
                base_images = ref_images
                if i > 0 and str(hop_script) == "next":
                    print(
                        f"[{TAG}] hop {i + 1}: identity stills stay off this "
                        "continue (no shots[] schedule); pin carries wardrobe "
                        "and room",
                        flush=True,
                    )
                    base_images = None

            hop_images = base_images
            hop_videos = base_videos
            live_p = live_v = None
            still_shift = 0
            if i > 0:
                hop_images, live_p, hop_videos, live_v = _attach_pin_to_qwen(
                    str(pin_to_qwen), base_images, base_videos,
                    prev_imgs[-1:], prev_imgs[-overlap_n:],
                )
                if live_p == 1:
                    still_shift = 1
                if not hop_images:
                    hop_images = None
                if not hop_videos:
                    hop_videos = None

            # One ordinal map for this hop. The prompt's <Picture N> citations
            # and the identity lock's ordinals have to agree, and computing the
            # same shift twice is exactly how they drift apart. `p`, not `n` --
            # `n` is the hop count in the enclosing scope.
            hop_ords = _refs.ordinals(hop_active)
            if still_shift:
                hop_ords = {t: p + still_shift for t, p in hop_ords.items()}
            if ref_plan_refs:
                # Plan-wide subjects so @hero_face still resolves when that
                # photograph is off this hop.
                # Names, not ordinals, from hop 2 on. `subject_definitions:`
                # is hop-1 material, so a `<Subject N>` reaching hop 4 has no
                # antecedent in its own encode -- the dangling-token defect
                # that turned an undescribed "the bowl" into a steel one.
                # `continuity_line` rides every continuation hop and is what
                # the name binds to.
                block = _refs.resolve_tags(
                    block, hop_ords, _refs.subjects(ref_plan_refs),
                    where=f"shot {i + 1}",
                    declared={r["tag"] for r in ref_plan_refs},
                    subject_names=({k: (v or {}).get("name")
                                    for k, v in (ref_subjects or {}).items()}
                                   if i > 0 else None))
                if i == 0:
                    # Hop 1 only: subject_prose derives its own ordinals with no
                    # still_shift, correct only because hop 1 has no pin.
                    # Computing it on hop 2+ invites an off-by-one against the
                    # live frame.
                    hop_subject_prose = _refs.subject_prose(hop_active, ref_subjects)
            if i == 0 and hop_subject_prose and not _d.is_full_h3_prompt(block):
                block = hop_subject_prose + "\n\n" + block
            # Hop 1 has no _assemble_next to fold this into. A full H3 block is
            # the author's own prompt end to end, so it is left alone there --
            # the same rule subject_prose follows two lines up.
            if i == 0 and refvid_line and not _d.is_full_h3_prompt(block):
                block = block.rstrip() + "\n\n" + refvid_line
            if str(hop_script) == "next" and i > 0:
                n_stills = len(base_images or {})
                hop_state_header = _state_header(state, i)
                # With a register wired, only the subject-bearing refs are
                # identities; a setting or prop plate must not be declared one.
                id_ords = None
                n_subj = None
                if hop_active:
                    # `subject is not None` on its own is not enough. The
                    # canonical outfit ref (README) carries a subject AND
                    # retention `partially_copy`, so it was being declared "the
                    # only identity ... that face, bone structure, and hairstyle
                    # match the photograph exactly" -- asserted about a
                    # photograph of a garment. Only a fully_preserved still is a
                    # face plate. refs.py already defaults a subject-bearing ref
                    # to fully_preserved, so an ordinary identity reference is
                    # unaffected by this narrowing; a wardrobe or setting plate
                    # stops being called a person.
                    faces = [r for r in hop_active
                             if r["subject"] is not None
                             and r["retention"] == "fully_preserved"]
                    id_ords = [hop_ords[r["tag"]] for r in faces]
                    # Counted over this hop, not the whole plan. Counting
                    # plan-wide while listing only this hop's ordinals is how a
                    # single scheduled still produced "<Picture 2> are the only
                    # identities" -- the plural that has rendered two people
                    # from one reference.
                    n_subj = len({r["subject"] for r in faces}) or None
                elif ref_plan_refs:
                    # Pin-only hop: no identity to lock. _identity_lock returns
                    # "" on an empty ordinal list whatever the count says.
                    id_ords = []
                # Plan-wide, not per-hop: a character in the chain is in
                # the chain whether or not their photograph rides this hop.
                # This is the text that has to survive hop 5 of the showcase,
                # which schedules no references at all.
                hop_continuity = _refs.continuity_line(
                    ref_subjects,
                    {r["subject"] for r in ref_plan_refs
                     if r["subject"] is not None}) if ref_plan_refs else ""
                # What each still riding THIS hop is for. Hop 1 gets this from
                # subject_prose; without it here a scheduled still reaches the
                # encoder as an uncited photograph with no stated role, and a
                # Ref2VA model handed a picture and no reason for it renders the
                # picture. `hop_ords`, not fresh ordinals -- the live frame is
                # <Picture 1> on every continuation hop.
                hop_retention = _refs.retention_prose(hop_active, hop_ords)
                hop_wardrobe = any(r["retention"] == "partially_copy"
                                   for r in hop_active)
                block = _assemble_next(
                    block,
                    live_picture=live_p,
                    live_video=live_v,
                    n_stills=n_stills,
                    state_header=hop_state_header,
                    identity_ordinals=id_ords,
                    n_subjects=n_subj,
                    tail=(shot.get("directives") or {}).get("tail"),
                    continuity=hop_continuity,
                    retention=hop_retention,
                    wardrobe=hop_wardrobe,
                    refvid=refvid_line,
                )
                print(f"[{TAG}] hop {i + 1} next-beat assembled "
                      f"(Picture {live_p}, Video {live_v}, "
                      f"{len(id_ords or [])} identity stills of {n_stills}, "
                      f"state_header {len(hop_state_header)} chars, "
                      f"continuity {len(hop_continuity)} chars, "
                      f"retention {len(hop_retention)} chars"
                      f"{', wardrobe plate' if hop_wardrobe else ''})",
                      flush=True)

            elif i > 0 and hop_active:
                # Verbatim mode assembles nothing -- the author owns the text --
                # so the retention block above is not injected here. But a still
                # scheduled onto this hop and never named in it reaches the
                # encoder as the same uncited photograph, authored rather than
                # assembled. Say so rather than silently rendering it.
                uncited = [r["tag"] for r in hop_active
                           if f"<Picture {hop_ords[r['tag']]}>" not in block]
                if uncited:
                    print(f"[{TAG}] hop {i + 1}: "
                          + ", ".join("@" + t for t in uncited)
                          + " rides this hop but is never cited in its prompt. "
                          "An uncited reference tends to be rendered as the "
                          "shot; name it with its @tag, or take it off this "
                          "hop in the rail.", flush=True)

            # Voice is hop-1 only under hop_script=next. The pin already
            # carries hop 1's spoken audio; leaving the clip on hop 2 as a
            # second <Audio 1> with no line to attach to is what put a
            # 1.35 s male take into the last second of chain_00038 while
            # the written line still followed the woman's face.
            hop_voice = voice is not None and (
                i == 0 or str(hop_script) != "next")
            if i > 0 and voice is not None and not hop_voice:
                print(
                    f"[{TAG}] hop {i + 1}: voice ref stays off this continue "
                    "(pin carries the spoken audio)",
                    flush=True,
                )
            if hop_voice and "<Audio 1>" not in block:
                block = (
                    block.rstrip()
                    + "\n\nThe speaker's voice follows <Audio 1> as a "
                      "timbre reference."
                )

            assembled.append((i + 1, block))

            # A dry run has everything it came for the moment the block is
            # compiled: this is the text the encoder would receive. Stop here,
            # before the key, the cache and the sampler.
            if dry:
                sheet_rows.append({
                    "hop": i + 1,
                    "first": None, "last": None,
                    "beat": (shot.get("beat") or "").strip() or "(continues)",
                    "directives": dict(shot.get("directives") or {}),
                    "meta": [f"{hop_length}f ({hop_length / FPS:.1f}s)",
                             f"{len(block)} chars compiled",
                             f"{len(hop_active)} ref(s)" if hop_active else None,
                             # NOT the pin mechanism: which one a hop gets is
                             # decided at render time by whether a sampler
                             # latent exists, and a dry run has none. Reporting
                             # the setting is honest; reporting AddGuide for
                             # every hop would not be.
                             f"pin_to_qwen={pin_to_qwen}" if i > 0 else None,
                             f"tone={shot.get('tone')}" if shot.get("tone") else None],
                })
                # The next hop's prompt asks how many pictures precede it, not
                # what is in them, so a token stand-in compiles identical text
                # for none of the memory.
                prev_imgs = torch.zeros((max(overlap_n, 1), 8, 8, 3),
                                        dtype=torch.float32)
                pbar.update(1)
                continue

            # Key this hop. prev_key makes the key chained, so editing shot 1
            # invalidates every hop after it -- correct, and the reason the UI
            # must show staleness before queuing or it reads as a bug.
            hop_key = None
            cached = None
            pin_mech_pred = _pin_mech_for(i, overlap_n, prev_sampled)
            pin_mech_used = pin_mech_pred
            if hop_store is not None:
                hop_key = _store.hop_key(prev_key, {
                    "chain": chain_salt,
                    "block": block,
                    "len": hop_length,
                    "steps": int(shot.get("steps") or steps),
                    "seed": (int(shot["seed"]) if shot.get("seed") is not None
                             else ((int(seed) + i) if seed_per_shot else int(seed))),
                    "tags": [r["tag"] for r in hop_active],
                    # The reference PIXELS this hop is handed, not the whole
                    # rail (see chain_salt). `base_images` is the pre-pin dict,
                    # which is the right thing on both paths: with a ref plan it
                    # is this hop's scheduled stills, without one it is every
                    # wired ref, and the pin frame it excludes is already
                    # accounted for by `prev_key`.
                    "refs": {k: _store.tensor_digest(t)
                             for k, t in sorted((base_images or {}).items())},
                    # Per hop, not chain-wide: hop 2 after a hop-1 cache hit
                    # has no sampler latent and falls back to AddGuide, which
                    # is a different render of the same inputs.
                    "pin_mech": pin_mech_pred,
                    # Only from hop 2. Hop 1 has no pin -- `_pin_mech_for`
                    # returns "none" for index 0 and the conditioning branch is
                    # `elif i > 0` -- so its frames cannot depend on these
                    # levers, and keying them in threw away a byte-identical
                    # cached hop 1 every time one was flipped. That is a third
                    # of the cost of every lever A/B, on the one hop nobody
                    # needed to re-render.
                    "pin_cond": ((pin_renorm_mode, round(pin_noise_v, 4), audio_ctx)
                                 if i > 0 else None),
                    # Same rule, moved out of chain_salt: how many frames the
                    # previous hop hands over changes this hop's conditioning
                    # and its trim, and nothing on hop 1.
                    "overlap": (overlap_n if i > 0 else None),
                    # Whether this hop actually received the voice tensor.
                    # chain_salt already digests the file; without this a hop 2
                    # rendered with the clip on would be served to a later run
                    # that kept it off.
                    "voice_on": hop_voice,
                })
                # A locked shot reuses its last render even though its inputs
                # changed -- that is the point of locking. The content key would
                # have moved, so the pointer is what finds it.
                shot_name = str(shot.get("id") or f"shot{i + 1}")
                if shot.get("locked"):
                    pinned = hop_store.get_pointer(shot_name)
                    if pinned:
                        if pinned != hop_key:
                            print(f"[{TAG}] hop {i + 1} is locked: reusing its "
                                  f"earlier render (inputs changed)", flush=True)
                        hop_key = pinned
                    else:
                        print(f"[{TAG}] hop {i + 1} is locked but has no cached "
                              f"render yet; rendering it once", flush=True)
                hop_keys.append(hop_key)
                cached = hop_store.get(hop_key)
                if i < replay_before and cached is None:
                    # Name the hop. "Cache miss" on its own sends people to the
                    # temp folder to count files; what they need to know is
                    # which shot moved and that the sweep may simply have
                    # reclaimed it -- the store is under ComfyUI's temp
                    # directory, which is deleted on startup and shutdown.
                    raise ValueError(
                        f"{TAG}: render_from={start_at} needs hop {i + 1} in "
                        "the cache and it is not there. Either its inputs "
                        "changed since it rendered -- editing an earlier shot "
                        "moves every key after it -- or the cache was swept. "
                        f"Render hops 1-{replay_before} first, or set "
                        "render_from back to 0.")

            this_sampled = None
            if cached is not None:
                imgs, wav, sr, cached_latent = cached
                audio = {"waveform": wav, "sample_rate": sr}
                # Carry the stored sampler latent forward exactly as a render
                # would. Without this the next hop sees no latent, predicts the
                # AddGuide fallback, and its key stops matching what is on disk
                # -- so nothing past hop 1 could ever hit, and the hop after a
                # hit was joined by the weaker mechanism.
                this_sampled = cached_latent
                print(f"[{TAG}] hop {i + 1}: loaded from cache "
                      f"({int(imgs.shape[0])}f, key {hop_key[:8]}"
                      f"{'' if cached_latent is not None else ', no latent'})",
                      flush=True)
            else:
                packed = MiniMaxH3ReferenceToVideo.execute(
                    clip, vae, audio_vae, block, int(width), int(height), hop_length,
                    ref_image_size=ref_image_size,
                    ref_images=hop_images,
                    ref_videos=hop_videos,
                    ref_audios=(ref_audios if hop_voice else None),
                )
                cond, latent = _result(packed)[0], _result(packed)[1]

                if i == 0 and start_image is not None:
                    cond = _result(MiniMaxH3AddGuide.execute(
                        cond, latent, 0, vae=vae, audio_vae=None,
                        image=start_image[:1], audio=None,
                    ))[0]
                elif i > 0:
                    pin_latent = prev_sampled
                    if pin_latent is not None:
                        pin_latent, pin_anchor = _condition_pin_latent(
                            pin_latent, pin_anchor,
                            mode=pin_renorm_mode, noise=pin_noise_v,
                            seed=(int(seed) + i))
                    cond, pin_mech_used = _pin_continue(
                        cond, latent, vae, audio_vae, overlap_n,
                        pin_latent, prev_imgs, prev_audio,
                        audio_ctx=audio_ctx,
                    )

                _offload_text_encoder(clip, model)

                guider = _result(BasicGuider.execute(model, cond))[0]
                if shot.get("seed") is not None:
                    shot_seed = int(shot["seed"])
                else:
                    shot_seed = (int(seed) + i) if seed_per_shot else int(seed)
                hop_steps = int(shot.get("steps") or steps)
                if hop_steps not in sigma_cache:
                    sigma_cache[hop_steps] = _result(
                        BasicScheduler.execute(model, scheduler, hop_steps, 1.0))[0]
                hop_sigmas = sigma_cache[hop_steps]
                if hop_steps != int(steps) or shot.get("seed") is not None:
                    print(f"[{TAG}] hop {i + 1} override: seed={shot_seed} "
                          f"steps={hop_steps}", flush=True)
                noise = _result(RandomNoise.execute(shot_seed))[0]
                sampled = _result(SamplerCustomAdvanced.execute(
                    noise, guider, sampler, hop_sigmas, latent
                ))[0]

                imgs, audio = _decode_av(vae, audio_vae, sampled)
                imgs = imgs.contiguous().cpu()
                wav = audio["waveform"].contiguous().cpu()
                sr = int(audio["sample_rate"])
                audio = {"waveform": wav, "sample_rate": sr}
                this_sampled = _latent_cpu(sampled)

                del sampled, latent, cond, guider, noise
                mm.soft_empty_cache()

                if (hop_store is not None and hop_key is not None
                        and pin_mech_used != pin_mech_pred):
                    print(f"[{TAG}] hop {i + 1} pinned by {pin_mech_used} but its "
                          f"cache key says {pin_mech_pred}; not caching this hop",
                          flush=True)
                elif hop_store is not None and hop_key is not None:
                    hop_store.put(hop_key, imgs, wav, sr,
                                  {"hop": i + 1, "of": n, "block": block[:400],
                                   "pin_mech": pin_mech_used},
                                  latent=this_sampled)
                    hop_store.set_pointer(
                        str(shot.get("id") or f"shot{i + 1}"), hop_key)

            # Tone compensation, at the one point both the render and the
            # cache-hit paths have converged.
            #
            # It sits AFTER hop_store.put on purpose, so the cache holds raw
            # hops and the mode stays out of the hop key -- switching modes
            # then costs nothing instead of invalidating ~285 MB an entry.
            # It sits BEFORE the master write and before `prev_imgs` is taken,
            # which is the half that matters: `prev_imgs` is what feeds the next
            # hop's Qwen <Picture 1> pin and the AddGuide guide image, so
            # correcting here is what stops the drift compounding rather than
            # merely repainting the master. Each hop is measured against the
            # previous hop's ALREADY CORRECTED tail, so the whole chain lands on
            # hop 1's tone.
            tone_note = ""
            if tone_on:
                if i > 0 and prev_imgs is not None:
                    imgs, tone_note = _tone.compensate(
                        prev_imgs, imgs, tone_mode, overlap_n)
                else:
                    # Hop 1 has nothing to match against, but the corrected hops
                    # come back clamped and an unclamped hop 1 beside them would
                    # make the master inconsistent with itself.
                    imgs = imgs.clamp(0.0, 1.0)
                if tone_note:
                    print(f"[{TAG}] hop {i + 1} tone: {tone_note}", flush=True)

            # The chain-wide half of `anchor`, stacked on the seam correction
            # above. It runs here for the same reason that one does: `prev_imgs`
            # is taken below, so correcting now is what stops the drift feeding
            # the next hop rather than merely repainting the master.
            if anchor_on:
                shot_tone = str(shot.get("tone") or "")
                if i == 0:
                    anchor_ref = _tone.anchor_stats(imgs)
                    if anchor_ref is not None:
                        print(f"[{TAG}] tone anchor set from hop 1: "
                              + " ".join(f"{c}{v:.4f}" for c, v
                                         in zip("rgb", anchor_ref.tolist())),
                              flush=True)
                elif shot_tone == "rebase":
                    anchor_ref = _tone.anchor_stats(imgs)
                    print(f"[{TAG}] hop {i + 1}: tone=rebase, anchor moved to "
                          f"this hop; later hops hold ITS level", flush=True)
                elif shot_tone == "free":
                    print(f"[{TAG}] hop {i + 1}: tone=free, anchor pull skipped",
                          flush=True)
                else:
                    imgs, anchor_note = _tone.anchor_pull(
                        imgs, anchor_ref, strength=float(tone_anchor))
                    if anchor_note:
                        tone_note = (tone_note + " + " + anchor_note
                                     if tone_note else anchor_note)
                        print(f"[{TAG}] hop {i + 1} tone: {anchor_note}",
                              flush=True)

            if i == 0:
                master_imgs[0:imgs.shape[0]] = imgs
                write_pos = int(imgs.shape[0])
                master_wav = wav
            else:
                if imgs.shape[0] <= overlap_n:
                    raise ValueError(
                        f"{TAG}: hop {i + 1} decoded {int(imgs.shape[0])} frames; "
                        f"need more than overlap {overlap_n}"
                    )
                keep_n = int(imgs.shape[0]) - overlap_n
                if write_pos + keep_n > total_frames:
                    raise ValueError(
                        f"{TAG}: hop {i + 1} overruns the preallocated master "
                        f"({write_pos + keep_n} > {total_frames}). A hop decoded a "
                        f"different length than planned.")
                master_imgs[write_pos:write_pos + keep_n] = imgs[overlap_n:]
                write_pos += keep_n
                trimmed, dropped = _trim_audio_head(audio, overlap_n)
                master_wav = _xfade_audio(master_wav, trimmed["waveform"], sr)
                print(
                    f"[{TAG}] hop {i + 1}: dropped {overlap_n} frames / {dropped} audio samples",
                    flush=True,
                )
                del trimmed

            if want_sheet:
                # The frames this hop actually CONTRIBUTES: hop 1 gives all of
                # them, every later hop gives what survives the overlap trim.
                # Showing imgs[0] on a continuation would show a frame the
                # master never contains.
                _f0 = imgs[0] if i == 0 else imgs[overlap_n]
                _row_seed = (int(shot["seed"]) if shot.get("seed") is not None
                             else ((int(seed) + i) if seed_per_shot else int(seed)))
                sheet_rows.append({
                    "hop": i + 1,
                    "first": _sheet.small(_f0),
                    "last": _sheet.small(imgs[-1]),
                    "beat": (shot.get("beat") or "").strip() or "(continues)",
                    "directives": dict(shot.get("directives") or {}),
                    "note": ("tone: " + tone_note) if tone_note else None,
                    "meta": [
                        f"{int(imgs.shape[0])}f",
                        f"seed {_row_seed}",
                        f"{int(shot.get('steps') or steps)} steps",
                        "cached" if cached is not None else None,
                        f"pin {pin_mech_used}" if i > 0 else None,
                        f"tone={shot.get('tone')}" if shot.get("tone") else None,
                    ],
                })

            # The join, as two pictures: the previous hop's last delivered frame
            # and this hop's first. Sent together so the panel can show the
            # actual seam rather than one frame per hop.
            seam_frame = imgs[0] if i > 0 else None
            # Overlap tail for Qwen; full sampler latent for Motion-Context.
            tail_n = overlap_n if overlap_n else 1
            prev_imgs = imgs[-tail_n:].clone()
            prev_audio = {"waveform": _tail_audio(audio, overlap_n)["waveform"].clone(),
                          "sample_rate": sr}
            prev_sampled = this_sampled
            prev_key = hop_key
            _push_preview(
                unique_id, f"hop {i + 1}/{n} done",
                frame=prev_imgs[-1], hop=i + 1, total=n,
                pin_mech=(pin_mech_used if i > 0 else None),
                frac=(write_pos / float(total_frames) if total_frames else None),
                seam_frame=seam_frame,
                meta={"cached": cached is not None,
                      "key": (hop_key[:8] if hop_key else None),
                      "frames": int(write_pos), "of_frames": int(total_frames),
                      "seed": int(shot_seed) if cached is None else None,
                      "steps": int(hop_steps) if cached is None else None,
                      "tone": tone_note or None})
            del imgs, wav, audio
            pbar.update(1)

        if dry:
            _span = (str(lengths[0]) if len(set(lengths)) == 1
                     else "/".join(str(v) for v in lengths))
            head = (f"DRY RUN - {n} hop(s) compiled, nothing rendered. "
                    f"{_span}f each, overlap {overlap_n}, "
                    f"would deliver {total_frames} frames "
                    f"({total_frames / FPS:.1f}s) at {int(width)}x{int(height)}.")
            print(f"[{TAG}] {head}", flush=True)
            _sep = chr(10) * 2
            info = head + _sep + _sep.join(
                ("===== hop %d prompt =====" + chr(10) + "%s") % (k, t)
                for k, t in assembled)
            sheet = _sheet.build(
                sheet_rows,
                title=f"DRY RUN - {n} hop(s), {total_frames} frames "
                      f"({total_frames / FPS:.1f}s) - nothing rendered")
            _push_preview(unique_id, f"dry run - {n} hop(s) compiled",
                          hop=n, total=n, frac=1.0,
                          meta={"dry_run": True, "hops": int(n),
                                "would_be_frames": int(total_frames),
                                "done": True})
            # Sized to the geometry the chain WOULD have produced, not 1x1:
            # the images output usually lands in a video encoder, and one black
            # frame at the real resolution both encodes cleanly and shows the
            # dimensions the plan resolved to. One frame is not the master --
            # the 31 GB allocation this mode exists to avoid is untouched.
            return (_sheet.placeholder(width, height),
                    {"waveform": torch.zeros((1, 2, 1024), dtype=torch.float32),
                     "sample_rate": 44100},
                    info,
                    sheet)

        if write_pos != total_frames:
            print(f"[{TAG}] note: wrote {write_pos} of {total_frames} planned "
                  f"frames; trimming", flush=True)
            master_imgs = master_imgs[:write_pos]
        if hop_store is not None:
            hop_store.sweep(keep=hop_keys)
        # lengths[0], not `length`: when every shot overrides duration to the
        # same value the set is still size 1, but `length` is the chain default.
        span = str(lengths[0]) if len(set(lengths)) == 1 else "/".join(str(v) for v in lengths)
        info = (
            f"{n} hops x {span}f overlap {overlap_n} -> "
            f"{int(master_imgs.shape[0])} frames ({master_imgs.shape[0] / FPS:.1f}s) "
            f"{int(master_imgs.shape[2])}x{int(master_imgs.shape[1])}"
        )
        print(f"[{TAG}] {info}", flush=True)
        # The assembled prompts being inspectable is the whole point of the
        # directive layer -- wire `info` to a Preview Text node to read exactly
        # what each hop sent to the text encoder.
        _sep = '\n\n'
        info = info + _sep + _sep.join('===== hop %d prompt =====\n%s' % (k, t) for k, t in assembled)
        # `info` is the third return (Preview Text). Never send it as the
        # preview status — that strip is ~22 px and will render the dump.
        _v_secs = float(master_imgs.shape[0]) / FPS
        _a_secs = float(master_wav.shape[-1]) / float(sr) if sr else 0.0
        _push_preview(
            unique_id,
            f"done · {int(master_imgs.shape[0])}f · {_v_secs:.1f}s",
            frame=master_imgs[-1], hop=n, total=n, frac=1.0,
            meta={"video_s": round(_v_secs, 3), "audio_s": round(_a_secs, 3),
                  "drift_ms": round((_a_secs - _v_secs) * 1000.0, 1),
                  "hops": int(n), "frames": int(master_imgs.shape[0]),
                  "done": True})

        # The soundtrack goes on LAST, after every hop is joined and the seams
        # are crossfaded. That placement is the whole safety argument: it runs
        # once, downstream of every latent, every pin and every cache key, so it
        # cannot move a generated frame or sample -- it only decides what is
        # laid over them. It also means a cached chain can be re-mixed at a
        # different level for the price of the mix alone.
        # The socket wins over the picker when both are set. A wire is a
        # deliberate act; a filename left in the panel from an earlier take is
        # not, and silently preferring the stale one would be the worse guess.
        _bed = soundtrack
        if _bed is None and soundtrack_file:
            _bed = _media.load_audio(soundtrack_file,
                                     start=float(music_start_s),
                                     end=float(music_end_s))
        elif _bed is not None and soundtrack_file:
            print(f"[{TAG}] soundtrack: using the wired socket, not "
                  f"{soundtrack_file!r}", flush=True)
        if _bed is not None and master_wav is not None:
            try:
                master_wav, _mnote = _music.apply(
                    master_wav, sr,
                    _bed.get("waveform"), _bed.get("sample_rate", sr),
                    gain_db=float(music_gain_db), duck=float(music_duck),
                    fit_mode=str(music_fit), fade_s=float(music_fade_s))
                if _mnote:
                    print(f"[{TAG}] {_mnote}", flush=True)
                    info = info + "\n" + _mnote
            except Exception as _me:  # noqa: BLE001
                # A bad music file must not destroy a render that has already
                # cost minutes of GPU. Report it and hand back the audio the
                # chain actually generated.
                print(f"[{TAG}] soundtrack skipped ({_me!r})", flush=True)
                info = info + f"\nsoundtrack skipped: {_me}"

        master_audio = {"waveform": master_wav, "sample_rate": sr}
        sheet = _sheet.placeholder()
        if want_sheet:
            sheet = _sheet.build(
                sheet_rows,
                title=(f"Hand Tie Clips - {n} hop(s), "
                       f"{int(master_imgs.shape[0])} frames ({_v_secs:.1f}s) "
                       f"@ {int(master_imgs.shape[2])}x{int(master_imgs.shape[1])}"
                       + (" - DRAFT" if draft else "")))
            print(f"[{TAG}] contact sheet: {int(sheet.shape[2])}x"
                  f"{int(sheet.shape[1])}", flush=True)
        return (master_imgs, master_audio, info, sheet)


class HTCContinuityState:
    """Author locked/context/mutable *setting* text once; HandTieClips consumes it per hop.

    locked and context ride every hop 2+ unchanged. mutable is --- delimited like the
    prompt field: one beat per hop, padded by repeating the last block if there are
    fewer blocks than hops.

    Setting only. Characters live in `ref_plan`'s reference register, which is
    the only thing that knows a photograph is a face rather than a room. This
    node used to carry `characters_*` as well, so filling in both it and the
    register injected identity prose twice into every hop 2+ -- run() warned
    about that collision rather than preventing it. With the character half
    gone the collision is structurally impossible.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "setting_locked": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Verbatim setting text (location, lighting). Injected unchanged into every hop 2+.",
                }),
                "setting_context": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Current-state setting text, less rigid than locked. Injected every hop 2+.",
                }),
                "setting_mutable": ("STRING", {
                    "multiline": True, "default": "",
                    "tooltip": "Per-hop setting beat text, --- delimited like characters_mutable.",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("continuity_state",)
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = (
        "Builds a JSON continuity-state blob (locked/context/mutable) for the "
        "*setting* only, feeding HandTieClips's continuity_state input. "
        "hop_script=next only. Characters belong in ref_plan's register."
    )

    def run(self, setting_locked="", setting_context="", setting_mutable=""):
        state = {
            "setting": {
                "locked": setting_locked.strip(),
                "context": setting_context.strip(),
                "mutable": _parse_shots(setting_mutable) if setting_mutable.strip() else [],
            },
        }
        return (json.dumps(state),)




# -- pre-rename ids ----------------------------------------------------------
# A plain alias in NODE_CLASS_MAPPINGS keeps old workflows loading, but it also
# lists the node a second time in search: ComfyUI falls back to the mapping key
# when NODE_DISPLAY_NAME_MAPPINGS has no entry. Subclassing and setting
# DEPRECATED gets both -- server.py publishes `deprecated: True`, and the
# frontend's `Comfy.Node.ShowDeprecated` (off by default) hides it from search
# while leaving it fully functional in workflows that name it.


class _LegacyH3RefChain(HandTieClips):
    DEPRECATED = True


class _LegacyH3ContinuityState(HTCContinuityState):
    DEPRECATED = True


NODE_CLASS_MAPPINGS = {
    "HandTieClips": HandTieClips,
    "HTCContinuityState": HTCContinuityState,
    "H3RefChain": _LegacyH3RefChain,
    "H3ContinuityState": _LegacyH3ContinuityState,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "HandTieClips": "H3 Ref2VA Chain",
    "HTCContinuityState": "H3 Continuity State",
}
