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
import uuid

import numpy as np
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
        "(comfy_extras/nodes_minimax_h3.py, ComfyUI PR #15439), v0.34.0 or "
        "newer -- MiniMaxH3AddGuide does not exist before that. Update "
        "ComfyUI, then restart. Original error: %s" % _exc
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
from . import audio_lock as _alock
from . import planner as _planner
from . import refine_blend as _rblend
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

    # The base checkpoint itself. Everything below describes what was PATCHED
    # onto the model and nothing identified the model underneath it, so an int8
    # build and a bf16 build of the same architecture, under the same LoRA
    # stack at the same strengths and the same attention settings, produced
    # byte-identical hop keys -- and the cache served frames rendered under the
    # other checkpoint. `model_dtype()` is ModelPatcher's own accessor
    # (comfy/model_patcher.py); it returns None when the inner model has no
    # `get_dtype`, which is hashed as a value rather than skipped so "no dtype"
    # and "some dtype" cannot collide.
    #
    # Residual gap, narrower than the one it closes: two *different* int8
    # builds of the same architecture still match. Separating those needs
    # digests of a few fixed weight keys, which costs a state-dict walk per run.
    base = getattr(model, "model", None)
    try:
        base_dtype = model.model_dtype() if hasattr(model, "model_dtype") else None
    except Exception:  # noqa: BLE001 -- a patcher that cannot answer is still a key
        base_dtype = "?"
    h.update(f"base:{type(base).__name__}:{base_dtype}".encode())
    _dm = getattr(base, "diffusion_model", None)
    if _dm is not None:
        try:
            h.update(f":n{sum(p.numel() for p in _dm.parameters()):d}".encode())
        except Exception:  # noqa: BLE001
            h.update(b":n?")

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

    def _object_scalars(obj):
        """Public scalar attributes of something that configures itself by
        instance rather than by closure.

        `_closure_scalars` digs settings out of a callable's cells, which is how
        H3-SLA-Attention carries its config. A node that installs a configured
        *object* instead -- `set_model_patch_replace(cache, "dit", "block_loop",
        0)` with an instance on it -- has no closure at all, and an instance
        inherits neither `__qualname__` nor `__name__` from its class, so it
        rendered as the bare constant "fn()" and every setting on it vanished
        from the key. Toggling such a node moved the fingerprint (a new key
        appears in `patches_replace`); changing its settings did not. That is
        the SLA bug one type away.

        Scalars only, for the same reason the closure walk is scalars only.
        Note the tradeoff this accepts: a scalar attribute the node mutates
        during a run makes the fingerprint move between runs and the cache stop
        hitting while that node is installed. That direction is deliberate --
        this pack treats serving frames from the wrong settings as worse than
        not serving them at all.
        """
        try:
            items = vars(obj).items()
        except TypeError:  # no __dict__ (slots, builtins) -- nothing to read
            return ""
        parts = [f"{k}={v!r}" for k, v in sorted(items, key=lambda kv: str(kv[0]))
                 if not str(k).startswith("_")
                 and (isinstance(v, (str, int, float, bool)) or v is None)]
        return "{" + ",".join(parts) + "}" if parts else ""

    def _callable_scalars(fn, depth=0):
        """Settings a callable carries, whichever way it carries them.

        There are four ways a node hands a configured callable to the model and
        all four have to reach the hash, because they are interchangeable from
        the installing node's point of view and indistinguishable from here:

          * a closure          -- cells               (`_closure_scalars`)
          * a configured instance -- its attributes   (`_object_scalars`)
          * a BOUND METHOD of a configured instance -- neither. `vars()` on a
            bound method proxies to the underlying *function's* `__dict__`,
            which is empty, so the instance's settings were invisible; only
            `__qualname__` survived. A node registering `self.forward` rather
            than `self` is the object case one attribute away.
          * a `functools.partial` -- neither either. It has no `__name__`, no
            `__qualname__`, no `__closure__`, and an empty `__dict__`, so it
            collapsed to the constant "fn()" exactly as a bare instance did.
            Everything it carries is in `func`, `args` and `keywords`.

        Depth-bounded because `partial` can wrap `partial`.
        """
        parts = [_closure_scalars(fn), _object_scalars(fn)]
        if depth <= 3:
            owner = getattr(fn, "__self__", None)
            if owner is not None:
                parts.append("@" + type(owner).__name__ + _object_scalars(owner))
            inner = getattr(fn, "func", None)
            if inner is not None and callable(inner):
                bound = ["<" + _callable_scalars(inner, depth + 1)]
                for a in (getattr(fn, "args", None) or ()):
                    bound.append(repr(a) if isinstance(a, (str, int, float, bool)) or a is None
                                 else type(a).__name__)
                kw = getattr(fn, "keywords", None) or {}
                for k in sorted(kw, key=str):
                    v = kw[k]
                    bound.append(f"{k}=" + (repr(v)
                                            if isinstance(v, (str, int, float, bool)) or v is None
                                            else type(v).__name__))
                parts.append(",".join(bound) + ">")
        return "".join(parts)

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
            return _callable_scalars(obj)
        return type(obj).__name__ + _object_scalars(obj)

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


def _core_call(node_cls, what, **kw):
    """Call a Core node by keyword, and fail readably when Core has moved.

    This pack is installed beside whatever ComfyUI the user already has, so
    Core's signature is an external interface it does not control. Passing
    arguments positionally made that fragile in a way that surfaced as a
    baffling error on someone else's machine -- "got multiple values for
    argument 'ref_image_size'" on hop 1, with nothing in the message to
    suggest a version mismatch.

    Keywords fix the misbinding. This adds the other half: if the installed
    Core does not accept an argument this pack passes, say which node, which
    argument, and what that Core actually takes, so the report names the
    problem instead of a traceback.
    """
    try:
        return node_cls.execute(**kw)
    except TypeError as e:
        import inspect
        try:
            params = [p for p in inspect.signature(node_cls.execute).parameters
                      if p not in ("cls", "self")]
        except (TypeError, ValueError):
            params = None
        raise RuntimeError(
            f"{TAG}: this ComfyUI's {node_cls.__name__} does not accept the "
            f"arguments this pack passes for {what} ({e}). "
            + (f"Its signature takes: {', '.join(params)}. " if params else "")
            + f"This pack passes: {', '.join(sorted(kw))}. "
            "That is a ComfyUI/pack version mismatch -- update ComfyUI, or "
            "report these two lists."
        ) from e


def _validate_anchors(shots, start_image_file):
    """Refuse an unusable anchor=restart before anything samples.

    A pure function so it can be exercised: the three rules below shipped
    twice broken -- once naming a `join` value that does not exist, once
    reading `start_image` before it was assigned -- because nothing called
    them except a real render.
    """
    for i, sh in enumerate(shots or []):
        if str((sh or {}).get("anchor") or "") != "restart":
            continue
        if i == 0:
            raise ValueError(
                f"{TAG}: shot 1 cannot be anchor=restart -- hop 1 is already "
                "a chain start. Remove it, or move it to a later shot.")
        # The FILENAME, not the loaded image: this runs before any media is
        # loaded, which is the point of it. Whether the file resolves is a
        # separate check further down.
        if not str(start_image_file or "").strip():
            raise ValueError(
                f"{TAG}: shot {i + 1} is anchor=restart but no start image is "
                "set. A restart re-anchors the chain on that photograph; "
                "without one there is nothing to restart from. Set "
                "start_image_file in MEDIA, or remove the anchor.")
        if ((sh.get("directives") or {}).get("join")) == "continuous":
            raise ValueError(
                f"{TAG}: shot {i + 1} is anchor=restart with join=continuous. "
                "A restart is a cut -- it opens on the start image's pose, not "
                "the previous hop's last frame. Use join=hard_cut or match_cut "
                "on that shot.")


def _validate_last_frame_guide(last_frame_guide, start_image_file):
    """Refuse last_frame_guide=still with no photograph, on the queue.

    Same class as `_validate_anchors`: a guard that only a render would
    otherwise exercise. Whitespace is not a file.
    """
    if str(last_frame_guide) != "still":
        return
    if not str(start_image_file or "").strip():
        raise ValueError(
            f"{TAG}: last_frame_guide=still but no start image is set. That "
            "mode pins start_image at the last pixel frame of every hop; "
            "without one there is nothing to pin. Set start_image_file in "
            "MEDIA, or leave last_frame_guide=off.")


def _guides_last_frame(mode, hop_index, shots):
    """Does THIS hop get the still pinned at its last frame? -> bool.

    `still` guides every hop. `before_restart` guides only a hop whose
    successor is `anchor="restart"`, which is the only place the guide has
    been shown to earn its keep.

    Measured, and the reason the third option exists. Guiding every hop turns
    a restart from an obvious jump into a match cut: the four hop endings of a
    4-hop chain converge to 3.8/255 of each other against 39.1/255 unguided,
    while mid-hop frames stay as varied as ever (65.8 against 61.1). Two people
    watched that clip in motion and could not see the convergence, because a
    hop's last frame passes in a twenty-fourth of a second.

    But it plants the photograph unconditionally, and a shot authored
    `framing: close` therefore plays as a close-up and then snaps to the
    still's wide framing in about 0.6 s at its own ending -- then the next hop
    pushes back in and snaps again. Watched without prompting, that reads as
    "the camera kept cutting in and out". The directive wins the middle of the
    hop and the guide wins the end, which is the worst division of the two.

    `before_restart` keeps the match cut and drops the pumping everywhere else.
    """
    mode = str(mode or "off")
    if mode == "off":
        return False
    if mode == "still":
        return True
    if mode != "before_restart":
        return False
    nxt = shots[hop_index + 1] if hop_index + 1 < len(shots) else None
    return bool(nxt) and str((nxt or {}).get("anchor") or "") == "restart"


def _last_frame_guide_key_field(mode, hop_index, shots):
    """Per-hop cache field, or None so the key stays byte-identical when off.

    Keyed on what this hop actually GETS, not on the widget: under
    `before_restart` most hops are unguided and must keep the key they had
    when the feature did not exist. Omitting the field when a hop is unguided
    is the empty-string rule from master_audio_file -- a None or "off" field
    would move every existing cache key.
    """
    if not _guides_last_frame(mode, hop_index, shots):
        return None
    return str(mode)


def _voice_rides_hop(mode, block):
    """Whether the timbre clip stays cited as <Audio N> on a continuation.

    `off` is the shipped behaviour -- hop 1 only. The reason is in the gate
    below: a second <Audio 1> with no line to attach to put a 1.35 s male
    take into the last second of chain_00038 while the written line still
    followed the woman's face. That failure needs a QUIET hop, because what
    the clip fills is frames nothing else was assigned. So `speaking` rides
    every hop whose beat actually has a spoken line and skips the ones that
    do not, which is the whole failure class the restriction was protecting
    against -- and it is the setting to recommend. `on` rides every hop
    unconditionally, for a chain where every hop talks and the author would
    rather own that risk than annotate it.

    Two dialogue forms count, because both are authored in the wild: the
    single-quoted line the example plans and the writer use ("she says,
    'You are early.'"), and the official `<d>[English] ...</d>` tag from the
    H3 contract. The quoted-line test is `planner.spoken_spans`, not a
    second regex -- the delimiter rule (an apostrophe between two
    alphanumerics is not a quote) already lives there and a copy would
    drift. Checking only one form would silently strand half the users on
    hop-1-only while the widget said otherwise.
    """
    m = str(mode or "off")
    if m == "on":
        return True
    if m != "speaking":
        return False
    text = str(block or "")
    return bool("<d>" in text or _planner.spoken_spans(text))


def _last_pixel_guide_idx():
    """AddGuide frame_idx for the last pixel frame of this hop.

    AddGuide's index is PIXEL frames, not latent tokens. Core treats a
    negative value as counted from the end, so -1 is the last pixel frame
    regardless of this hop's duration.

    Do not pass latent_T-1. FRAME_PER_TOKEN is (1, 4, 4, 4, 4); on an 8 s
    hop (192 px frames, latent T=57) that index is pixel 56 -- about 2.3 s
    in -- not the end. That is the bug this helper exists to stop.
    """
    return -1


# Above this, the master frame buffer is spilled to disk instead of RAM. The
# number is a judgement, not a measurement: below it the mapping buys nothing
# worth the I/O, and above it the buffer is competing with the DiT and the VAE
# for the length of a run during which nothing reads it.
MASTER_SPILL_BYTES = 2 << 30            # 2 GiB
# The master is DELIVERY-ONLY: `prev_imgs` is cloned from `imgs`, never read
# back out of the master, so nothing in the conditioning path depends on its
# precision. fp16 halves both the footprint and the disk I/O for free.
#
# fp16 and not bf16, because the master is clamped to 0..1 so exponent range
# buys nothing and mantissa bits are the whole question. fp16's ten mantissa
# bits space the top octave at ~1/2048, about eight times finer than the 8-bit
# encode downstream; bf16's seven space it at 1/256 -- exactly 8-bit output
# precision with nothing in reserve, and highlights would band.
#
# Two things make it safer than it first looks. `tone_compensate` runs BEFORE
# the master write, so all correction arithmetic stays in fp32 and the
# quantisation never compounds with it. And the anchor mode's failure direction
# is crushed blacks, which is fp16's strongest region.
#
# Emphatically NOT true of the hop cache, where CLAUDE.md's 16-bit reasoning is
# load-bearing: a cached hop's last frame becomes the next hop's pin, so a
# lossy round trip there would make a resumed chain diverge from an
# uninterrupted one. Same-looking decision, opposite answer, different tensor.
MASTER_DTYPE = torch.float16
MASTER_NP_DTYPE = np.float16


def _open_self_deleting(path, nbytes):
    """A file sized to `nbytes` that removes itself when its last handle closes.

    The first version of this spilled to an ordinary file and swept stale ones
    on the next run. That was wrong, and measurably so: ComfyUI holds the
    previous run's IMAGE output in its execution cache, so the mapping is still
    open when the next run starts, `os.remove` raises, and the sweep skipped it
    -- silently, because the handler passed on OSError. Two renders left two
    9 GB files behind. The docstring claimed "there is never more than one".

    Delete-on-close removes the whole problem instead of policing it. Windows
    has it natively as `O_TEMPORARY`; POSIX gets the same behaviour by
    unlinking immediately while the descriptor stays open. Either way the
    bytes live exactly as long as something is using them, the file never
    appears in a listing after that, and a crashed process cleans up on exit
    because the kernel closes its handles.
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_TEMPORARY", 0)
    fh = os.fdopen(os.open(path, flags), "r+b")
    try:
        fh.truncate(int(nbytes))
        if not hasattr(os, "O_TEMPORARY"):
            os.unlink(path)          # POSIX: the inode outlives the name
    except Exception:
        fh.close()
        raise
    return fh


def _sweep_master_spills(keep):
    """Remove spill files a previous BUILD or a hard kill left behind.

    Self-deleting files make this a safety net rather than the mechanism: it
    exists for files written by the version of this code that did not use
    delete-on-close, and for anything a `kill -9` orphaned before the handle
    was open. It should normally find nothing.

    A file that cannot be removed is reported rather than swallowed. Silence is
    what let 18 GB accumulate unnoticed the first time.
    """
    import folder_paths
    root = folder_paths.get_temp_directory()
    freed = stuck = 0
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not (name.startswith("htc_master_") and name.endswith(".raw")):
            continue
        path = os.path.join(root, name)
        if path == keep:
            continue
        try:
            n = os.path.getsize(path)
            os.remove(path)
            freed += n
        except OSError:
            try:
                stuck += os.path.getsize(path)
            except OSError:
                pass                    # gone between the listing and here
    if freed:
        print(f"[{TAG}] reclaimed {freed / 2**30:.1f} GB from an earlier "
              f"master spill", flush=True)
    if stuck:
        print(f"[{TAG}] {stuck / 2**30:.1f} GB of old master spills could not "
              f"be removed (still mapped by this process). They clear when "
              f"ComfyUI restarts; temp/ is wiped at startup.", flush=True)


def _alloc_master(total_frames, height, width):
    """The master frame buffer, in RAM or memory-mapped to disk.

    It is allocated once at full chain length, slice-written as each hop lands,
    and then not read again until the final preview frame and the return. At
    8 x 15 s and 1280x736 that is ~14.4 GB in fp16 -- it was ~29 GB in fp32 --
    resident and inactive through every sampling pass, competing with the DiT,
    the VAE decode buffers, `imgs` and `prev_imgs`.

    **This does not save 14.4 GB.** ComfyUI's IMAGE type is a dense tensor, so the
    whole master still has to exist to be returned. What moves is the peak:
    from `master + inference` to `max(master, inference)`. On the chains where
    this bites that is the difference between finishing and an OOM, and it is
    not a saving -- do not write it up as one.

    A `np.memmap` is the right primitive rather than an incremental writer: one
    tensor, shape known up front, written in contiguous ranges in order. The
    problem was first put to us in these terms by silveroxides, who proposed
    the streaming writer from `unifiedefficientloader` (MIT); the diagnosis was
    right and the writer was the wrong shape for one tensor of known size, so
    no code travelled -- but the reading did, and the credit belongs here. Every
    slice-write below is unchanged, and SaveVideo walking frames in order is
    ideal page locality on the way back out.

    The honest weakness: the OS decides when pages leave RAM. Under no memory
    pressure they simply stay and nothing has been bought; under heavy pressure
    a large dirty flush can stall at an awkward moment. Which path ran is
    printed either way -- a silent memory path is the one thing nobody could
    diagnose from a bug report.
    """
    shape = (int(total_frames), int(height), int(width), 3)
    nbytes = int(np.dtype(MASTER_NP_DTYPE).itemsize)
    for d in shape:
        nbytes *= d
    gb = nbytes / 2**30

    if nbytes < MASTER_SPILL_BYTES:
        print(f"[{TAG}] master buffer: {gb:.1f} GB in RAM", flush=True)
        return torch.empty(shape, dtype=MASTER_DTYPE)

    try:
        import folder_paths
        root = folder_paths.get_temp_directory()
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, f"htc_master_{uuid.uuid4().hex}.raw")
        _sweep_master_spills(path)
        fh = _open_self_deleting(path, nbytes)
        arr = np.memmap(fh, dtype=MASTER_NP_DTYPE, mode="r+", shape=shape)
        out = torch.from_numpy(arr)
        # Name the owner. The mapping is already kept alive by the tensor's
        # storage, so this changes no lifetime -- it gives anything that needs
        # to release the file deterministically (the offline checker today, an
        # explicit writer if the memmap ever proves too passive) a handle
        # instead of digging through gc. It does not survive the trim, which
        # makes a view.
        out._htc_mmap = arr
        print(f"[{TAG}] master buffer: {gb:.1f} GB spilled to disk "
              f"({os.path.basename(path)}, self-deleting)", flush=True)
        return out
    except Exception as e:  # noqa: BLE001
        # Falling back is correct -- a chain that renders slowly beats one that
        # refuses to start because a temp directory is read-only.
        # MASTER_DTYPE, not float32: `gb` was computed from
        # MASTER_NP_DTYPE.itemsize, so a float32 buffer here would be twice
        # what this line just told the user -- and it would land on the one
        # machine that could not spare a spill file.
        print(f"[{TAG}] master spill unavailable ({e!r}); {gb:.1f} GB in RAM",
              flush=True)
        return torch.empty(shape, dtype=MASTER_DTYPE)


def _dense_media(prefix, items):
    """Number the filled slots 1..N with no gaps. -> dict or None.

    `<Video N>` and `<Audio N>` are POSITIONAL: core numbers reference blocks
    by the order it iterates them, and the prompt cites those ordinals. A gap
    would hand core `ref_video_1` and `ref_video_3`, and a beat written about
    "the second clip" would then name something else. So slot 3 becomes
    <Video 2> when slot 2 is empty, and the tooltips say so.
    """
    out = {}
    for x in items:
        if x is not None:
            out[f"{prefix}{len(out) + 1}"] = x
    return out or None


def _pin_mech_for(hop_index, overlap_n, prev_sampled, mode="auto"):
    """Which mechanism `_pin_continue` will pick, without doing the work.

    The hop cache key has to be built *before* the pin runs, and the two
    mechanisms produce different frames, so the key needs the mechanism up
    front. Every condition here mirrors _pin_continue; the one thing it cannot
    predict is Motion-Context raising at call time, which the caller catches by
    comparing this against the mechanism actually used and declining to cache
    that hop.

    `mode` is the `pin_mech` widget. "auto" is the shipped behaviour and the
    four conditions below. Forcing does not add a fifth condition -- it removes
    them, which is the point: a forced setting that quietly degrades to the
    other mechanism tells you nothing, and the reason to force one is to
    compare it against the other. The two chain-wide preconditions are
    validated before any sampling starts, so the only one that can still be
    false here is the per-hop latent.
    """
    if hop_index == 0:
        return "none"
    if mode == "addguide":
        return "addguide_pixels"
    if mode == "motion_context":
        if prev_sampled is None:
            raise ValueError(
                f"{TAG}: hop {hop_index + 1}: pin_mech=motion_context needs the "
                "previous hop's sampler latent, and this one came from a cache "
                "entry written before latents were stored. Re-render that hop "
                "(edit it, or turn cache_hops off for one run) or use pin_mech="
                "auto, which falls back to the AddGuide pixel pin here."
            )
        return "motion_context"
    if _motion_context_cls() is None:
        return "addguide_pixels"
    if str(overlap_n) not in MC_CONTEXT_LENGTHS:
        return "addguide_pixels"
    if prev_sampled is None:
        return "addguide_pixels"
    return "motion_context"


def _pin_continue(cond, latent, vae, audio_vae, overlap_n,
                  prev_sampled, prev_imgs, prev_audio, audio_ctx=24,
                  mode="auto"):
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
    # `mode` mirrors _pin_mech_for. Both have to honour it or the mechanism in
    # the per-hop key stops matching the one on disk, which is the failure that
    # made cache_hops=on measurably worse than off before the latent sidecar
    # landed. Do not change one of these without the other.
    mc = None if mode == "addguide" else _motion_context_cls()
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
            if mode == "motion_context":
                # Asked for explicitly, so falling back would answer a question
                # nobody asked and quietly poison an A/B against the other
                # mechanism.
                raise RuntimeError(
                    f"{TAG}: pin_mech=motion_context but Motion-Context raised "
                    f"({e!r}). Use pin_mech=auto to fall back to the AddGuide "
                    "pixel pin."
                ) from e
            print(
                f"[{TAG}] Motion-Context pin failed ({e!r}); AddGuide pixel pin",
                flush=True,
            )
    if mode == "addguide":
        # Asked for. Saying "not available" here would be false, and it is the
        # line a user would read while wondering why forcing it did nothing.
        print(
            f"[{TAG}] pin_mech=addguide: AddGuide pixel pin ({overlap_n}f)",
            flush=True,
        )
    elif mc is None:
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
    return _result(_core_call(
        MiniMaxH3AddGuide, "the AddGuide pixel pin",
        positive=cond, latent=latent, frame_idx=0,
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


def _batch_wav(wav):
    """Comfy AUDIO is [B, C, T]. A squeezed take is [C, T]. Same rank, always.

    The lock path used to leave hop 1's master as [C, T] and hop 2's trim as
    [B, C, T]; `_xfade_audio` then died on `torch.cat` with 'got 2 and 3'.
    GPU test 1 found it: hop 1 locked [0.00s-8.00s], hop 2 never wrote.
    """
    if wav.dim() == 2:
        return wav.unsqueeze(0)
    if wav.dim() == 3:
        return wav
    raise ValueError(
        f"{TAG}: waveform must be [C, T] or [B, C, T], got {tuple(wav.shape)}")


def _xfade_audio(left, right, sr, ms=40):
    left = _batch_wav(left)
    right = _batch_wav(right)
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


def _resample_wav(wav, src_sr, dst_sr):
    src_sr, dst_sr = int(src_sr), int(dst_sr)
    if src_sr == dst_sr:
        return wav
    import torchaudio
    return torchaudio.functional.resample(wav, src_sr, dst_sr)


def _prepare_master_audio(path):
    """Load the take once: stereo, native rate kept, plus a 32 kHz copy."""
    got = _media.load_audio(path)
    if got is None:
        raise ValueError(
            f"{TAG}: master_audio_file {path!r} did not load. The file has to "
            "resolve under h3_refs the same way voice_file does.")
    wav = _alock.force_stereo(got["waveform"].contiguous().cpu())
    sr = int(got["sample_rate"])
    wav32 = _resample_wav(wav, sr, _alock.VAE_SR)
    digest = _store.audio_digest({"waveform": wav, "sample_rate": sr})
    print(f"[{TAG}] master_audio_file: loaded {path!r} "
          f"({wav.shape[-1] / sr:.2f}s at {sr} Hz, stereo)", flush=True)
    return {"path": path, "wav": wav, "sr": sr, "wav32": wav32,
            "digest": digest}


def _encode_locked_slice(audio_vae, wav32, t0, t1, audio_latent_len):
    """VAE-encode one hop's window of the 32 kHz take. -> audio latent tensor."""
    start, end = _alock.sample_range(t0, t1, _alock.VAE_SR)
    picture_n = max(1, end - start)
    grid_n = _alock.grid_samples(audio_latent_len, _alock.VAE_SR)
    enc_n = max(picture_n, grid_n)
    enc = _alock.fit_samples(wav32, start + enc_n)[..., start:start + enc_n]
    # song_lock: encode [B, T, C]. wav32 is [C, T].
    batch = enc.unsqueeze(0).movedim(1, -1)
    try:
        z = audio_vae.encode(batch)
    except Exception as e:
        raise RuntimeError(
            f"{TAG}: audio VAE encode for master_audio_file failed ({e!r}). "
            "The lock follows PromptMasterLD song_lock.py "
            "(encode at 32 kHz on the 40 Hz grid). If this ComfyUI's "
            "audio VAE uses a different signature, that is a version "
            "mismatch -- report this error."
        ) from e
    got = int(z.shape[-1])
    if got < int(audio_latent_len):
        extra = int(math.ceil(
            (int(audio_latent_len) - got + 1) * _alock.VAE_SR / _alock.AUDIO_HZ))
        enc2 = _alock.fit_samples(wav32, start + enc_n + extra)[
            ..., start:start + enc_n + extra]
        z = audio_vae.encode(enc2.unsqueeze(0).movedim(1, -1))
        got = int(z.shape[-1])
    if got < int(audio_latent_len):
        raise RuntimeError(
            f"{TAG}: audio VAE produced {got} steps, hop needs "
            f"{int(audio_latent_len)}. The take window was "
            f"{t0:.3f}s-{t1:.3f}s.")
    return z[..., :int(audio_latent_len)]


def _splice_locked_audio(latent, z_audio):
    """Replace the hop's audio latent and freeze it. Video stays live."""
    import comfy.nested_tensor as nt
    parts = _latents.from_dict(latent)
    if parts is None or len(parts) < 2:
        raise RuntimeError(
            f"{TAG}: master_audio_file splice needs a joint AV latent "
            f"(video+audio); got {type((latent or {}).get('samples')).__name__}.")
    video, audio = parts[0], parts[1]
    z = z_audio.to(device=audio.device, dtype=audio.dtype)
    if int(z.shape[-1]) != int(audio.shape[-1]):
        raise RuntimeError(
            f"{TAG}: locked audio latent length {int(z.shape[-1])} != "
            f"hop audio length {int(audio.shape[-1])}.")
    # Match batch/leading dims; take the first encoded copy if we produced extra.
    while z.dim() < audio.dim():
        z = z.unsqueeze(0)
    while z.dim() > audio.dim():
        z = z[0]
    if tuple(z.shape[:-1]) != tuple(audio.shape[:-1]):
        try:
            z = z.expand(audio.shape)
        except RuntimeError as e:
            raise RuntimeError(
                f"{TAG}: locked audio shape {tuple(z.shape)} will not fit "
                f"hop audio {tuple(audio.shape)} ({e}).") from e
    parts[1] = z
    out = dict(latent)
    out["samples"] = _latents.rebuild(latent["samples"], parts)
    # song_lock polarity: ones on video (denoise), zeros on audio (freeze).
    v_shape = (1, 1) + tuple(int(d) for d in video.shape[2:])
    a_shape = (1, 1) + tuple(int(d) for d in audio.shape[2:])
    vmask = torch.ones(v_shape, device=video.device, dtype=torch.float32)
    amask = torch.zeros(a_shape, device=audio.device, dtype=torch.float32)
    _alock.assert_mask_polarity(vmask, amask)
    out["noise_mask"] = nt.NestedTensor((vmask, amask))
    return out


# How many of the frozen head's last steps ramp back up to full denoise under
# refine_head=freeze. Two: enough that the held region does not end on a hard
# edge, short enough that it stays INSIDE the head and never reaches a frame
# anyone sees. It is not a lever and does not want to be one -- the lever for
# where the refine starts is `refine_blend`, which works on delivered frames.
REFINE_HEAD_RAMP = 2


# The speed preset. One table, one place: "make turbo actually work" later means
# editing a row here, not unpicking branches through the refine block.
#
# NOTHING AUTO-DETECTS. A ModelPatcher carries no name -- `_model_fingerprint`
# returns a content hash of the patched weight keys and the attention scalars,
# which can tell two checkpoints apart but cannot tell you either one is
# distilled. So the mode is DECLARED by the person on the rail, and the log
# prints the fingerprint beside it so a run can be attributed afterwards.
#
# `regular` is empty by construction -- the widgets, untouched -- so a graph
# built before this widget existed renders byte-identically.
#
# `turbo` is a distilled few-step base, and the honest state of that row is that
# MOST OF IT IS UNMEASURED. What is measured, on matched runs at a pinned seed,
# is that a turbo base is the degrader: junction MAE 8.40-10.85 and climbing hop
# over hop, against 2.07-4.02 flat for the plain hybrid over 9-10 hops. The
# turbo checkpoint went into the BASE loader only and the chain still collapsed,
# so it compounds through the conditioning path and not through the latent, and
# there is no corner of this loop where turbo is free. The preset does not claim
# to fix that.
#
# The one row entry that IS evidence-backed is refine_head=freeze: turbo fails
# at the seam first, and freeze is the only refine lever measured to move a seam
# (1.9x/1.7x against 3.5x/7.3x), at a grain cost that is real and is invisible
# on bokeh. Every field a row leaves out passes the widget straight through.
SPEED_MODES = {
    "regular": {},
    "turbo": {"refine_head": "freeze"},
}

# Stated with the mode, once per run, so the console carries the cost of the
# choice next to the choice. There is no silent correction anywhere in this
# path: every field the table moves is named in the log with the value the
# widget read.
SPEED_MODE_NOTES = {
    "turbo": ("a turbo base measured ~2.5x the seam error of a plain hybrid "
              "and climbing (junction MAE 8.40-10.85 vs 2.07-4.02 over 9-10 "
              "hops); the refine pass reduces that, it does not undo it"),
}


def _apply_speed_mode(mode, values):
    """(effective_values, [(field, widget_value, preset_value)]) for one mode.

    A new dict rather than a mutation, and the report lists only what actually
    MOVED -- so `regular`, and a turbo row whose value the widget already
    carries, both print nothing and both key identically to a run without the
    preset. An unknown mode falls back to the widgets rather than raising: the
    preset is an accelerator, and a typo in it is not worth losing a queue over.
    """
    table = SPEED_MODES.get(str(mode)) or {}
    out = dict(values)
    moved = []
    for field in sorted(table):
        if field in out and out[field] != table[field]:
            moved.append((field, out[field], table[field]))
            out[field] = table[field]
    return out, moved


def _stream_5d(t):
    """Video latent as [B,C,T,H,W]."""
    if t.ndim == 4:
        t = t.unsqueeze(0)
    if t.ndim != 5:
        raise RuntimeError(
            f"{TAG}: expected video latent [B,C,T,H,W], got {tuple(t.shape)}")
    return t


def _stream_audio(t):
    """Audio latent as [B,C,2,T40]."""
    if t.ndim == 3:
        t = t.unsqueeze(0)
    if t.ndim != 4:
        raise RuntimeError(
            f"{TAG}: expected audio latent [B,C,2,T], got {tuple(t.shape)}")
    return t


def _refine_head_freeze(latent, overlap_n, freeze_head=True, freeze_audio=True):
    """Hold the pinned overlap head, the audio stream, or both, out of the
    refine.

    Measured, not assumed. On an 8x8-pooled (geometry-only) frame difference a
    stock chain's joins are indistinguishable from ordinary motion -- 1.00x
    against its own neighbourhood. Every refine mode puts them at 2.4-2.8x, and
    the size does NOT track refine_denoise: 0.20 measures 2.75x and 0.50
    measures 2.41x. Texture moves far less (0.94x -> 1.4-1.6x), so what the
    refine breaks at the join is position, not grain. What is constant across
    those runs is that the head goes through an extra, independently-noised pass
    that the tail it has to continue never saw.

    THE RAMP LIVES INSIDE THE HEAD. The head is exactly the region the writer
    discards -- `incoming = imgs[drop_n:]` -- so latent step `v_steps` is the
    FIRST DELIVERED frame. Ramping forward from there puts a partial denoise on
    the first delivered frames of every hop, which is a texture dent precisely
    at the seam. The ramp climbs across the last steps OF the head instead, so
    the mask is already 1.0 by the time it reaches a frame anyone sees. Ramping
    on the delivered side is `refine_blend`'s job, and it does it on finished
    samples where it cannot dent anything.

    AUDIO IS HELD WHOLE, AND ON EVERY HOP. Leaving audio live while the video
    head was frozen produced a strained voice at the end of each hop; mirroring
    the video mask into the audio head made it worse. The defect is not WHICH
    audio is refined, it is that the audio stream acquires a BOUNDARY at all --
    at 40 Hz a latent step is 25 ms, and a magnitude jump between a held region
    and a refined one is audible as a warble. Holding it only on hops 2+ fixes
    nothing audible either: hop 1's voice is what every later hop's context pin
    continues, so the damage is applied once and then carried. Hence the audio
    hold is gated separately from the head hold and applies to hop 1 too.

    noise_mask polarity is 1 = denoise, 0 = freeze. ANDs with whatever mask is
    already there -- under master_audio_file that is the audio lock, and
    replacing it would hand the refine a live audio stream to re-cook.

    Returns (latent, v_steps, audio_held). v_steps is 0 when no head was frozen,
    which is not an error: hop 1 has no pinned head and an off-grid overlap has
    no whole number of latent steps. Audio can still be held in both of those
    cases, so callers must test both halves of the result.
    """
    import comfy.nested_tensor as nt  # noqa: PLC0415
    parts = _latents.from_dict(latent)
    if parts is None or len(parts) < 2:
        return latent, 0, False
    video = _stream_5d(parts[0])
    audio = _stream_audio(parts[1])
    t_total = int(video.shape[2])

    # A head that is off-grid, or long enough to swallow the hop, is no head.
    # That does not stop the audio hold, which needs neither.
    v_steps = (int(_rblend.whole_steps_for_frames(overlap_n) or 0)
               if freeze_head else 0)
    if v_steps and t_total <= v_steps + 1:
        v_steps = 0
    if not v_steps and not freeze_audio:
        return latent, 0, False

    vmask = torch.ones((1, 1) + tuple(int(d) for d in video.shape[2:]),
                       device=video.device, dtype=torch.float32)
    if v_steps:
        vmask[:, :, :v_steps] = 0.0
        # Ramp across the LAST steps of the head, never past its end. At least
        # one step stays fully frozen, or there is no held head left to hold.
        v_ramp = max(0, min(int(REFINE_HEAD_RAMP), v_steps - 1))
        for k in range(v_ramp):
            # Step (v_steps - v_ramp + k) climbs toward 1.0; step v_steps, the
            # first delivered one, is already 1.0 and stays that way.
            vmask[:, :, v_steps - v_ramp + k] = float(k + 1) / float(v_ramp + 1)

    ashape = (1, 1) + tuple(int(d) for d in audio.shape[2:])
    amask = (torch.zeros(ashape, device=audio.device, dtype=torch.float32)
             if freeze_audio
             else torch.ones(ashape, device=audio.device, dtype=torch.float32))

    prev = latent.get("noise_mask")
    if prev is not None:
        try:
            pv, pa = list(prev.unbind())[:2]
            vmask = vmask * pv.to(device=vmask.device, dtype=vmask.dtype)
            amask = amask * pa.to(device=amask.device, dtype=amask.dtype)
        except Exception as e:
            raise RuntimeError(
                f"{TAG}: the refine could not AND its hold with the existing "
                f"noise mask ({e}). Refusing to drop it -- that would let the "
                "refine re-cook locked audio.") from e
    out = dict(latent)
    out["noise_mask"] = nt.NestedTensor((vmask, amask))
    return out, v_steps, bool(freeze_audio)


def _refine_sampled(sampled, *, model, guider, sampler, scheduler,
                    steps, denoise, seed, sigma_cache, hop_no,
                    sampler_label="same", cond_label="base",
                    own_model=False):
    """Second low-denoise sample of a FINISHED hop latent.

    Every ratchet lever before this one acts at the join and acts by arithmetic:
    `pin_renorm` rescales a statistic, `pin_mech=reset` blends bands of a warped
    photograph. The measured failure was always the same -- the statistics held
    while the CONTENT walked -- and arithmetic cannot put content back.
    Re-sampling can, because it runs the model. That is the whole reason this
    exists and the reason it sits here rather than at the join.

    The schedule is cached under a ("refine", steps, denoise, scheduler) tuple.
    The hop schedules are cached under a bare int, so the two can never collide
    -- which matters more than it looks: a collision would hand the refine pass
    the hop's full-denoise schedule and quietly re-render the hop from noise.
    `scheduler` is in the key because refine_scheduler can differ from the hop's:
    without it, flipping simple/sgm_uniform mid-chain would be served the first
    one's sigmas and the widget would do nothing.
    """
    key = ("refine", int(steps), round(float(denoise), 4), str(scheduler))
    if key not in sigma_cache:
        sigma_cache[key] = _result(_core_call(
            BasicScheduler, "the refine sigma schedule",
            model=model, scheduler=str(scheduler), steps=int(steps),
            denoise=float(denoise)))[0]
    r_sigmas = sigma_cache[key]
    # An independent seed. Re-noising on the hop's own seed would push along the
    # direction the hop already travelled, which is a weaker perturbation than a
    # fresh draw -- it would read as "refine barely did anything" and be
    # indistinguishable from the lever not working.
    r_noise = _result(_core_call(
        RandomNoise, "the refine noise source",
        noise_seed=(int(seed) ^ 0x5EF1) & 0x7FFFFFFF))[0]
    print(f"[{TAG}] hop {hop_no}: refine {int(r_sigmas.shape[-1]) - 1} steps "
          f"denoise={float(denoise):.2f} "
          f"sigma={float(r_sigmas[0]):.4f}->0 "
          f"[{sampler_label}/{scheduler}, cond={cond_label}"
          + (", refine_model" if own_model else "") + "]", flush=True)
    return _result(_core_call(
        SamplerCustomAdvanced, "the refine sampler",
        noise=r_noise, guider=guider, sampler=sampler,
        sigmas=r_sigmas, latent_image=sampled))[0]


def _refine_blend_latent(raw, refined, keys, interp="linear", hop_no=0):
    """Lerp the refined video back toward the raw sample over the pinned head.

    The refine taken whole is what ships and what the next hop continues, and
    the head of the hop is exactly the region that has to continue the PREVIOUS
    hop -- which was never put through a second sampler. `0:0, 22:0, 44:1` keeps
    the raw sample across the overlap and crosses to fully refined 22 frames
    later, so the join is stock and the picture is refined.

    VIDEO ONLY, on purpose. xyzdist's node takes audio wholly from the refined
    latent with no ramp; that is what `refine_audio=freeze` exists to avoid, and
    a ramp on audio would install exactly the magnitude boundary that made the
    voice warble. Whatever branch `refine_audio` selected passes through here
    untouched.

    A lerp and not a mask: both inputs are finished samples, so there is no
    sampler left to hand a mask to. The arithmetic lives in `refine_blend.py`
    and is checked on the CPU by `tools/check_refine_blend.py`.
    """
    if not keys:
        return refined
    r_parts = _latents.from_dict(raw)
    f_parts = _latents.from_dict(refined)
    if not r_parts or not f_parts or len(r_parts) != len(f_parts):
        print(f"[{TAG}] hop {hop_no}: refine_blend skipped (the refine did not "
              "return a matching latent); the refine ships whole", flush=True)
        return refined
    v_raw = _stream_5d(r_parts[0])
    v_ref = _stream_5d(f_parts[0])
    weights = _rblend.step_weights(int(v_ref.shape[2]), keys, interp)
    mixed = _rblend.blend_video(v_raw, v_ref, weights)
    f_parts[0] = mixed.reshape(f_parts[0].shape)
    out = dict(refined)
    out["samples"] = _latents.rebuild(refined["samples"], f_parts)
    print(f"[{TAG}] hop {hop_no}: refine blend "
          + _rblend.describe(weights, keys, interp), flush=True)
    return out


def _slice_take_audio(prepared, t0, t1, sr):
    """The take window, resampled to `sr`, as an AUDIO dict. For the pin."""
    start, end = _alock.sample_range(t0, t1, prepared["sr"])
    chunk = _alock.fit_samples(prepared["wav"], end)[..., start:end]
    chunk = _resample_wav(chunk, prepared["sr"], int(sr))
    return {"waveform": chunk.unsqueeze(0), "sample_rate": int(sr)}


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
                "steps": ("INT", {"default": 14, "min": 1, "max": 50, "tooltip":
                    "Video tolerates a low count; GENERATED AUDIO DOES NOT. "
                    "Measured single-hop at 576p, seed pinned: 6 -> 10 steps "
                    "cut the voice's spectral flatness 15% (less noise, more "
                    "harmonic) and raised voiced share 83 -> 87%, while "
                    "laplacian texture did not move. Most of it lands by 10. "
                    "Audio gets no second pass -- refine_audio=freeze keeps "
                    "the refine off the audio stream -- so this widget alone "
                    "sets voice quality. Put `steps` on the speaking shots in "
                    "the plan to pay for voice only where there is a voice."}),
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
                        "downstream node cannot do this. Enabling any mode also "
                        "clamps the master to 0..1. "
                        "MEASURED on a 3-hop chain, mean seam step against off "
                        "(2.36/255): anchor 0.68, gain_bias 0.76, lut 0.77, "
                        "frame_shift 1.35. anchor is the one to reach for. Every "
                        "mode OVERSHOOTS -- an uncorrected seam brightens, a "
                        "corrected one darkens -- and none of them fixes the FIRST "
                        "join, which all four overshoot by a similar margin."
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
                        "hop 1's look -- its L* level, its a*/b* colour and its "
                        "L* spread. Ignored by every other mode. 0 disables the "
                        "pull and leaves plain frame_shift; 0.35 closes about a "
                        "third of the gap per hop, which arrests a long slide "
                        "without visibly pumping. Raise it to 0.6-0.8 for long "
                        "chains that grey out: a measured 9-hop study lost a "
                        "fifth of its chroma by the end, and 0.35 only slows "
                        "that. Costs about 15 s per 15 s hop at 1344x768: the "
                        "measurement is in Lab, which is a colour-space round "
                        "trip over every frame. A shot can opt out with "
                        "\"tone\": \"free\" or move the anchor to itself with "
                        "\"tone\": \"rebase\"."
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
                        "rather than by edge. "
                        "It is also an INFLUENCE dial, not only a memory one. "
                        "Area sets how many tokens the clip costs, and that is "
                        "how loudly it speaks: core aligns reference frame N "
                        "with output frame N, so wherever the clip shows a "
                        "clear face it competes with your identity stills for "
                        "that same face. At MAX it wins. An identity swap that "
                        "only takes hold part-way through the hop -- the "
                        "clip's person at the start, yours once the "
                        "clip's face is obscured -- is this, and 0.3 MP "
                        "fixed it on a measured case. Lower it when "
                        "identity matters more than the clip's detail."
                    ),
                }),
                # APPENDED, never inserted. `widgets_values` is a bare ordered
                # array matched to this schema by index, so a widget added
                # anywhere but the end silently reassigns every later value in
                # every saved workflow. Adding options to an existing combo is
                # safe; adding a widget is not.
                "pin_mech": (["auto", "motion_context", "addguide"], {
                    "default": "auto",
                    "tooltip": (
                        "Which mechanism pins hops 2+ to the previous hop. "
                        "auto = Motion-Context when the pack is installed, the "
                        "overlap has a matching context_length and the previous "
                        "hop left a sampler latent; AddGuide pixels otherwise. "
                        "Forcing one does not fall back -- it fails with the "
                        "reason, because a lever that silently becomes the "
                        "other setting cannot be compared against it. "
                        "motion_context: latent join, no decode/re-encode. "
                        "addguide: re-encodes decoded pixels, which is itself a "
                        "VAE round trip and may scrub differently. The "
                        "mechanism is in the per-hop cache key, so switching "
                        "re-renders hops 2+ and leaves hop 1 on disk."
                    ),
                }),
                # APPENDED, never inserted -- see the note above `pin_mech`.
                "tone_anchor_ref": (["hop1", "still"], {
                    "default": "hop1",
                    "tooltip": (
                        "What tone_compensate=anchor pulls TOWARD. Ignored by "
                        "every other mode. hop1 is the original behaviour: the "
                        "chain holds whatever tone hop 1 rendered. still uses "
                        "start_image instead, and pulls hop 1 as well -- which "
                        "matters because hop 1 already misses the photograph "
                        "before any relay has happened. A measured 9-hop study "
                        "read the still at chroma 33.6 and hop 1 at 30, so a "
                        "chain anchored on hop 1 is holding a target that is "
                        "already short. Needs start_image_file set. Under the "
                        "Motion-Context join the correction still only reaches "
                        "the delivered frames, not the pin -- set "
                        "pin_mech=addguide for it to feed back."
                    ),
                }),
                # APPENDED after pin_mech and tone_anchor_ref. Those two already
                # shipped on this trunk; the 3x3 extra slots go last so existing
                # v2 workflows keep their last two values as pin_mech /
                # tone_anchor_ref. H3 natively takes 9 reference images, 3
                # reference videos and 3 standalone reference audios; the pack
                # matched the 9 and passed exactly one of each of the others.
                "reference_video_2_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Reference clip 2 of 3. H3 takes three; this pack "
                        "passed one until now. Cited as <Video 2> when every "
                        "earlier slot is filled -- the numbering is dense, so "
                        "clearing slot 2 renumbers slot 3. Decoded at the same "
                        "reference video size as slot 1. Set in the panel."
                    ),
                }),
                "reference_video_2_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim in, seconds, for reference clip 2.",
                }),
                "reference_video_2_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim out, seconds, for reference clip 2. 0 = to the end.",
                }),
                "reference_video_3_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Reference clip 3 of 3. H3 takes three; this pack "
                        "passed one until now. Cited as <Video 3> when every "
                        "earlier slot is filled -- the numbering is dense, so "
                        "clearing slot 2 renumbers slot 3. Decoded at the same "
                        "reference video size as slot 1. Set in the panel."
                    ),
                }),
                "reference_video_3_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim in, seconds, for reference clip 3.",
                }),
                "reference_video_3_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim out, seconds, for reference clip 3. 0 = to the end.",
                }),
                "voice_2_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Voice reference 2 of 3, cited as <Audio 2>. H3 takes "
                        "three standalone reference audios; this pack passed one "
                        "until now. Dense numbering, so clearing slot 2 renumbers "
                        "slot 3 -- and a beat that names an ordinal would then "
                        "cite the wrong voice. Every reference audio is attended "
                        "on every step of every hop, so trim them. Set in the panel."
                    ),
                }),
                "voice_2_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim in, seconds, for voice 2.",
                }),
                "voice_2_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim out, seconds, for voice 2. 0 = to the end.",
                }),
                "voice_3_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "Voice reference 3 of 3, cited as <Audio 3>. H3 takes "
                        "three standalone reference audios; this pack passed one "
                        "until now. Dense numbering, so clearing slot 2 renumbers "
                        "slot 3 -- and a beat that names an ordinal would then "
                        "cite the wrong voice. Every reference audio is attended "
                        "on every step of every hop, so trim them. Set in the panel."
                    ),
                }),
                "voice_3_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim in, seconds, for voice 3.",
                }),
                "voice_3_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Trim out, seconds, for voice 3. 0 = to the end.",
                }),
                # APPENDED, never inserted. Empty string leaves every existing
                # path byte-identical: the lock is not called, the cache key
                # does not move, the delivered audio is still the generated
                # chain. The file is ElevenLabs (or any) TTS / a real take;
                # every hop lip-syncs to one continuous window of it.
                "master_audio_file": ("STRING", {
                    "default": "",
                    "tooltip": (
                        "One continuous voice take every hop lip-syncs to. "
                        "Basename under h3_refs. Empty = off, generated voice "
                        "as before. When set: the take is sliced on the same "
                        "clock as the picture (hop 1 starts at 0.00s), encoded "
                        "on the 40 Hz audio-latent grid, and frozen with a "
                        "noise_mask so only the picture is denoised. Delivered "
                        "audio is a passthrough of this file, no VAE round "
                        "trip. The beat still needs the words in "
                        "<d>[English] ...</d> -- unmatched text can pull the "
                        "mouth off the take. Changing the file invalidates "
                        "the hop cache."
                    ),
                }),
                "last_frame_guide": (["off", "before_restart", "still"], {
                    "default": "off",
                    "tooltip": (
                        "Pin start_image at a hop's last PIXEL frame "
                        "(AddGuide frame_idx=-1), so the hop ENDS on the "
                        "photograph. off = shipped behaviour, frame 0 only. "
                        "before_restart = only on a hop whose NEXT shot is "
                        "anchor=restart; that restart opens on the same "
                        "photograph, so both sides of the cut meet on one "
                        "image and it reads as a match cut rather than a "
                        "jump. That is the recommended setting. "
                        "still = every hop: the same benefit at the restart, "
                        "but it overrides an authored framing directive at "
                        "EVERY hop ending. A shot set framing=close plays "
                        "close for six seconds, snaps to the still's wider "
                        "framing in about 0.6 s, and the next hop pushes "
                        "back in -- watched, that reads as the camera "
                        "cutting in and out. Safe only when no shot authors "
                        "a framing. Needs start_image_file. Does NOT become "
                        "the next hop's frame 0; that is keyframe chaining, "
                        "which this is not."
                    ),
                }),
                # APPENDED 2026-09-09, LAST for the same reason as everything
                # else down here: widgets_values is positional and a saved
                # workflow reads it by index.
                "voice_every_hop": (["off", "speaking", "on"], {
                    "default": "off",
                    "tooltip": (
                        "Whether voice 1-3 stay cited as <Audio 1..3> after "
                        "hop 1. off = shipped behaviour, hop 1 only: later "
                        "hops inherit the timbre through the audio pin, "
                        "which drifts over a long chain. speaking = ride "
                        "every hop whose beat has a spoken line, skip the "
                        "quiet ones -- RECOMMENDED, and the setting that "
                        "keeps one voice across a whole chain. on = ride "
                        "every hop regardless. What off is protecting "
                        "against: an uncited timbre clip on a hop with no "
                        "line fills the leftover frames with that "
                        "recording, which once put a 1.35 s male take into "
                        "the last second of a hop whose written line "
                        "followed a woman's face. That needs a QUIET hop, "
                        "so speaking cannot reach it. Moves the cache key "
                        "of every hop it changes."
                    ),
                }),
                # APPENDED 2026-09-14 (the refine pass). Same rule again:
                # widgets_values is positional, so these go LAST and nothing
                # above them moves. Everything here ships inert -- hop_refine
                # defaults off and the hop key only carries these fields when
                # it is on, so a workflow saved before today keys byte-identical.
                "hop_refine": (["off", "full", "pin_only"], {
                    "default": "off",
                    "tooltip": (
                        "Re-sample each finished hop a second time at low "
                        "denoise, to put back the texture the chain loses. "
                        "off = shipped behaviour, one sampler pass. "
                        "full = the refined latent is what ships AND what the "
                        "next hop continues. pin_only = the refine is handed "
                        "forward as the next hop's teacher but not one "
                        "delivered pixel of THIS hop moves, so the cost lands "
                        "on the chain and not on the picture; skipped on the "
                        "last hop, which teaches nobody. Targets same-frame "
                        "consistency -- limits over a long chain are still "
                        "being characterised. Moves every hop's cache key, "
                        "including hop 1's, because the refine runs after "
                        "every sampler."
                    ),
                }),
                "refine_denoise": ("FLOAT", {
                    "default": 0.50, "min": 0.05, "max": 1.0, "step": 0.05,
                    "tooltip": (
                        "How far back toward noise the second pass starts. "
                        "0.50 is the published value. At H3's shift=12.0 this "
                        "is coarser than it reads: 0.7, 0.8 and 0.9 all "
                        "collapse to the same two-sigma schedule, so the dial "
                        "does its real work below ~0.6. Measured across three "
                        "runs the seam cost does NOT track this number -- 0.20 "
                        "and 0.50 both land at 2.4-2.8x -- so do not reach "
                        "here first when a join looks wrong; reach for "
                        "refine_blend."
                    ),
                }),
                "refine_steps": ("INT", {
                    "default": 2, "min": 0, "max": 40,
                    "tooltip": (
                        "Steps in the second pass. 0 = the same count the hop "
                        "sampled at. 2 is the published value and it is the "
                        "lever that actually separates the good runs from the "
                        "bad ones: at shift 12 / denoise 0.50 the schedule is "
                        "[0.9231, 0.8000, 0] and the pass is deliberately "
                        "under-converged -- it perturbs the texture without "
                        "re-deciding the picture. 3 already looks like a "
                        "different shot."
                    ),
                }),
                "refine_sampler": (["same"] + comfy.samplers.KSampler.SAMPLERS, {
                    "default": "same",
                    "tooltip": (
                        "Sampler for the second pass. same = whatever the hop "
                        "used. The best pairing measured so far is a plain "
                        "base sampler with res_multistep here."
                    ),
                }),
                "refine_scheduler": (["same"] + comfy.samplers.KSampler.SCHEDULERS, {
                    "default": "simple",
                    "tooltip": (
                        "Scheduler for the second pass. It is in the sigma "
                        "cache key, so flipping it really does rebuild the "
                        "schedule rather than re-serving the first one."
                    ),
                }),
                "refine_cond": (["base", "hop"], {
                    "default": "base",
                    "tooltip": (
                        "What the second pass is pulled toward. base = the "
                        "conditioning as Ref2VA built it, before any pin or "
                        "guide -- the references and the text, nothing about "
                        "the join. hop = the same pinned conditioning the hop "
                        "sampled under, which re-asserts the pin on a picture "
                        "that has already left it."
                    ),
                }),
                "refine_model": ("MODEL", {
                    "tooltip": (
                        "Optional. A DIFFERENT model for the second pass, e.g. "
                        "an undistilled base when the chain itself runs turbo. "
                        "Unwired = refine on the same model the hop sampled "
                        "with. Gets its own guider, since the hop's is bound "
                        "to the hop's model. Fingerprinted into the hop key."
                    ),
                }),
                "refine_audio": (["freeze", "refine"], {
                    "default": "freeze",
                    "tooltip": (
                        "freeze = the audio stream is masked out of the second "
                        "pass, so audio leaves a refine chain bit-identical to "
                        "a no-refine one. RECOMMENDED and measured: the refine "
                        "has nothing to gain on audio (it exists to fix "
                        "texture, which is a picture property) and re-cooking "
                        "it made the voice strained across three runs. Held on "
                        "EVERY hop including hop 1, because hop 1's voice is "
                        "what every later hop's pin continues. refine = let "
                        "the second pass touch audio too; kept for the A/B."
                    ),
                }),
                "refine_blend": ("STRING", {
                    "default": _rblend.DEFAULT_RAMP, "multiline": False,
                    "tooltip": (
                        "frame:weight keyframes deciding which frames ship the "
                        "raw sample (0) and which ship the refined one (1). "
                        "The default '0:0, 22:0, 44:1' keeps the pinned "
                        "overlap stock and crosses to fully refined over the "
                        "22 frames after it -- so the join continues a hop "
                        "that was sampled the same way it was, which is the "
                        "measured seam cost of refining. Frames are mapped "
                        "onto the latent grid through H3's real (1,4,4,4,4) "
                        "token cycle and read off this render's own length, "
                        "not off a duration widget. Empty = no blend, the "
                        "refine ships whole."
                    ),
                }),
                "refine_blend_interp": (["linear", "smooth", "step"], {
                    "default": "linear",
                    "tooltip": (
                        "How the blend moves between keyframes. linear is what "
                        "the published run used. smooth eases both ends; step "
                        "holds each key until the next one, which is a hard "
                        "switch and mostly a diagnostic."
                    ),
                }),
                "refine_head": (["refine", "freeze"], {
                    "default": "refine",
                    "tooltip": (
                        "The OTHER way to keep the join stock, kept for the "
                        "A/B against refine_blend. freeze masks the pinned "
                        "head out of the second pass instead of lerping it "
                        "back afterwards, with a 2-step ramp that stays inside "
                        "the head so no delivered frame gets a partial "
                        "denoise. It kills the seam flash and wins on the "
                        "face; the grain cost is real and is invisible on "
                        "bokeh. Only hops 2+ have a head. Off-grid overlaps "
                        "have no whole number of latent steps to freeze and "
                        "say so in the log."
                    ),
                }),
                # APPENDED 2026-09-15 (the speed preset). Last slot again, for
                # the same positional reason: nothing above it moves.
                "speed_mode": (["regular", "turbo"], {
                    "default": "regular",
                    "tooltip": (
                        "Which base is on the wire -- DECLARED, because nothing "
                        "here can detect it. A ModelPatcher carries no name. "
                        "regular = the refine widgets are used exactly as they "
                        "read. turbo = a distilled few-step base, and the "
                        "refine block takes the preset's values instead, with "
                        "every override named in the log beside the value the "
                        "widget read. It makes no parity claim: on matched runs "
                        "at a pinned seed a turbo base measured junction MAE "
                        "8.40-10.85 and CLIMBING against 2.07-4.02 flat for a "
                        "plain hybrid over 9-10 hops, with the turbo "
                        "checkpoint in the base loader only -- so it compounds "
                        "through the conditioning path, and the preset reduces "
                        "that rather than undoing it. It reaches the hop key "
                        "only through the refine fields it moves, so with "
                        "hop_refine=off it is inert."
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
                   soundtrack_file="",
                   reference_video_2_file="", reference_video_3_file="",
                   voice_2_file="", voice_3_file="",
                   master_audio_file="", **_):
        """Re-run when a reference file changes underneath its name.

        Every picture now arrives as a basename, and a basename is a stable
        input: overwrite `face.png` with a different face and ComfyUI would
        happily serve the previous render. Hashing path+mtime is the fix.

        Deliberately NOT `float("nan")` -- that is the blunt version of this and
        would force a full re-render of an expensive node on every queue.
        """
        names = [start_image_file, reference_video_file, voice_file,
                 soundtrack_file,
                 reference_video_2_file, reference_video_3_file,
                 voice_2_file, voice_3_file,
                 master_audio_file]
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
            pin_mech="auto", tone_anchor_ref="hop1",
            reference_video_2_file="", reference_video_2_start_s=0.0,
            reference_video_2_end_s=0.0,
            reference_video_3_file="", reference_video_3_start_s=0.0,
            reference_video_3_end_s=0.0,
            voice_2_file="", voice_2_start_s=0.0, voice_2_end_s=0.0,
            voice_3_file="", voice_3_start_s=0.0, voice_3_end_s=0.0,
            master_audio_file="",
            last_frame_guide="off",
            voice_every_hop="off",
            hop_refine="off", refine_denoise=0.50, refine_steps=2,
            refine_sampler="same", refine_scheduler="simple",
            refine_cond="base", refine_model=None, refine_audio="freeze",
            refine_blend=_rblend.DEFAULT_RAMP, refine_blend_interp="linear",
            refine_head="refine", speed_mode="regular",
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
        _validate_anchors(shots, start_image_file)
        _validate_last_frame_guide(last_frame_guide, start_image_file)
        try:
            _rp_for_refs = _refs.parse_ref_plan(ref_plan)
        except Exception:
            _rp_for_refs = None
        if _rp_for_refs is not None:
            _plan.validate_shot_refs(shots, _rp_for_refs.get("refs"))
        # Same rule, same reason: checked on the queue, by filename, before any
        # media is loaded. A chain that cannot reach its anchor should say so in
        # a second rather than nine hops later.
        if (str(tone_compensate) == "anchor" and float(tone_anchor) > 0.0
                and str(tone_anchor_ref) == "still"
                and not str(start_image_file or "").strip()):
            raise ValueError(
                f"{TAG}: tone_anchor_ref=still but no start image is set. That "
                "mode holds the chain on the photograph's colour and contrast; "
                "without one there is nothing to hold. Set start_image_file in "
                "MEDIA, or use tone_anchor_ref=hop1.")
        # pin_mech=motion_context: both chain-wide preconditions checked before
        # any sampling, so a forced setting fails on the queue rather than three
        # hops in. The per-hop one (no sampler latent) cannot be known here and
        # is raised by _pin_mech_for at the hop it affects.
        if str(pin_mech) == "motion_context":
            if _motion_context_cls() is None:
                raise ValueError(
                    f"{TAG}: pin_mech=motion_context but ComfyUI-H3-Motion-Context "
                    "is not installed. Install it, or use pin_mech=auto for the "
                    "AddGuide pixel pin."
                )
            if str(overlap_n) not in MC_CONTEXT_LENGTHS:
                raise ValueError(
                    f"{TAG}: pin_mech=motion_context but overlap {overlap} "
                    f"({overlap_n} frames) has no Motion-Context context_length. "
                    f"It accepts {', '.join(sorted(MC_CONTEXT_LENGTHS, key=int))} "
                    "frames; pick an overlap with one of those, or use pin_mech=auto."
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
        # Empty string is off. The lock is not entered, the cache key does
        # not grow a new field, delivered audio stays generated. That is
        # the byte-identical claim.
        locked = (_prepare_master_audio(master_audio_file)
                  if str(master_audio_file or "").strip() else None)

        # Slots 2 and 3. All three decode at the same reference_video_size --
        # it is an area budget for the decode, not a per-clip creative choice,
        # and three of them is already three times the RAM.
        def _more_video(fname, t0, t1):
            return (_media.load_video(
                fname, max_frames=max(lengths) if lengths else length,
                start=float(t0), end=float(t1),
                size=reference_video_size or _media.DEFAULT_VIDEO_SIZE)
                if fname else None)

        def _more_voice(fname, t0, t1):
            return (_media.load_audio(fname, start=float(t0), end=float(t1))
                    if fname else None)

        reference_video_2 = _more_video(reference_video_2_file,
                                        reference_video_2_start_s, reference_video_2_end_s)
        reference_video_3 = _more_video(reference_video_3_file,
                                        reference_video_3_start_s, reference_video_3_end_s)
        voice_2 = _more_voice(voice_2_file, voice_2_start_s, voice_2_end_s)
        voice_3 = _more_voice(voice_3_file, voice_3_start_s, voice_3_end_s)

        # Each clip's own soundtrack, paired to the same ordinal. Core takes
        # ref_video_audios beside ref_videos and this pack never passed them,
        # so a reference clip reached the model silent even when the file had
        # sound. Trimmed to the same window as its picture.
        def _clip_audio(fname, t0, t1):
            if not fname:
                return None
            try:
                return _media.load_audio(fname, start=float(t0), end=float(t1),
                                         kinds=("audio", "video"))
            except Exception as e:  # noqa: BLE001 -- a silent clip is not a failure
                print(f"[{TAG}] reference clip {fname}: no usable audio track "
                      f"({e!r}); passing it silent", flush=True)
                return None

        _vid_slots = [
            (reference_video, _clip_audio(reference_video_file,
                                          reference_video_start_s, reference_video_end_s)),
            (reference_video_2, _clip_audio(reference_video_2_file,
                                            reference_video_2_start_s, reference_video_2_end_s)),
            (reference_video_3, _clip_audio(reference_video_3_file,
                                            reference_video_3_start_s, reference_video_3_end_s)),
        ]
        for _name, _got in (("reference_video_2", reference_video_2_file and reference_video_2 is None),
                            ("reference_video_3", reference_video_3_file and reference_video_3 is None),
                            ("voice_2", voice_2_file and voice_2 is None),
                            ("voice_3", voice_3_file and voice_3 is None),
                            ("start_image", start_image_file and start_image is None),
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
        base_videos, base_video_audios = {}, {}
        for _v, _a in _vid_slots:
            if _v is None:
                continue
            _n = len(base_videos) + 1
            base_videos[f"ref_video_{_n}"] = _v
            if _a is not None:
                base_video_audios[f"ref_video_audio_{_n}"] = _a
        base_videos = base_videos or None
        base_video_audios = base_video_audios or None
        if base_videos and len(base_videos) > 1:
            print(f"[{TAG}] {len(base_videos)} reference clips"
                  + (f", {len(base_video_audios)} with sound" if base_video_audios else "")
                  , flush=True)
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
        ref_audios = _dense_media("ref_audio_", [voice, voice_2, voice_3])
        if ref_audios and len(ref_audios) > 1:
            print(f"[{TAG}] {len(ref_audios)} voice references", flush=True)

        # Fingerprint the model as it arrives -- after whatever LoRA and
        # attention nodes are drawn upstream, before this node touches it.
        # Skipped on a dry run: it only feeds the hop cache key, and a dry run
        # writes no cache. Hashing patched weights is not free.
        model_fp = None if dry else _model_fingerprint(model)
        # Same reasoning as model_fp: a hop refined under a different model is a
        # different hop, and serving it from cache is the silent-wrong-output
        # failure the fingerprint exists to stop.
        refine_model_fp = (None if (dry or refine_model is None)
                           else _model_fingerprint(refine_model))

        # The speed preset, folded in BEFORE anything reads a refine field:
        # before the ramp is parsed, before the refine sampler object is built,
        # and before the hop key is assembled. The key therefore moves because
        # the EFFECTIVE values moved, which is exactly why `speed_mode` itself
        # is deliberately absent from the hop payload -- there is no second copy
        # of the truth to desync, and flipping the preset under hop_refine=off
        # correctly keys the same, because it changed nothing.
        _speed = str(speed_mode) if str(speed_mode) in SPEED_MODES else "regular"
        if _speed != "regular":
            _fp = (model_fp or "")[:12] or "dry-run"
            _note = SPEED_MODE_NOTES.get(_speed)
            print(f"[{TAG}] speed_mode={_speed} on model {_fp}"
                  + (f" -- {_note}" if _note else ""), flush=True)
            if str(hop_refine) == "off":
                print(f"[{TAG}]   hop_refine=off, so the {_speed} row moves "
                      f"nothing this run", flush=True)
            else:
                _sv, _smoved = _apply_speed_mode(_speed, {
                    "refine_denoise": float(refine_denoise),
                    "refine_steps": int(refine_steps),
                    "refine_sampler": str(refine_sampler),
                    "refine_scheduler": str(refine_scheduler),
                    "refine_cond": str(refine_cond),
                    "refine_audio": str(refine_audio),
                    "refine_blend": str(refine_blend),
                    "refine_blend_interp": str(refine_blend_interp),
                    "refine_head": str(refine_head),
                })
                refine_denoise = _sv["refine_denoise"]
                refine_steps = _sv["refine_steps"]
                refine_sampler = _sv["refine_sampler"]
                refine_scheduler = _sv["refine_scheduler"]
                refine_cond = _sv["refine_cond"]
                refine_audio = _sv["refine_audio"]
                refine_blend = _sv["refine_blend"]
                refine_blend_interp = _sv["refine_blend_interp"]
                refine_head = _sv["refine_head"]
                for _f, _was, _now in _smoved:
                    print(f"[{TAG}]   {_speed} sets {_f}={_now} "
                          f"(widget read {_was})", flush=True)
                if not _smoved:
                    print(f"[{TAG}]   the widgets already match the {_speed} "
                          f"row", flush=True)

        # Parsed up front so a malformed ramp fails on the queue rather than
        # two minutes into hop 1, and so a dry run catches it too.
        try:
            refine_keys = (_rblend.parse(refine_blend)
                           if str(hop_refine) != "off" else [])
        except ValueError as e:
            raise RuntimeError(f"{TAG}: {e}") from e

        sampler = base_sigmas = None
        refine_sampler_obj = refine_sched = None
        sigma_cache = {}
        if not dry:
            model = _result(_core_call(
                MiniMaxH3SigmaShift, "the sigma shift",
                model=model, shift_video=float(shift_video),
                shift_audio=float(shift_audio)))[0]
            sampler = _result(_core_call(
                KSamplerSelect, "the sampler",
                sampler_name=sampler_name))[0]
            # Built once, beside the hop sampler, for the same reason that one
            # is: KSamplerSelect is pure, and rebuilding it per hop would be one
            # more core call per hop for an identical object.
            refine_sampler_obj = sampler
            if str(hop_refine) != "off" and str(refine_sampler) != "same":
                refine_sampler_obj = _result(_core_call(
                    KSamplerSelect, "the refine sampler choice",
                    sampler_name=str(refine_sampler)))[0]
            refine_sched = (str(scheduler) if str(refine_scheduler) == "same"
                            else str(refine_scheduler))
            base_sigmas = _result(_core_call(
                BasicScheduler, "the sigma schedule",
                model=model, scheduler=scheduler, steps=int(steps),
                denoise=1.0))[0]
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
        #
        # A restart is a chain start: it overlaps with nothing, so it must
        # not be charged an overlap trim. `sum(lengths) - overlap * (n - 1)`
        # assumed every hop past the first was trimmed and silently dropped
        # 0.9 s of new content per restart.
        hop_starts = [
            i == 0 or str((sh or {}).get("anchor") or "") == "restart"
            for i, sh in enumerate(shots)
        ]
        total_frames = _alock.master_frame_count(lengths, overlap_n, hop_starts)
        n_trims = sum(1 for flag in hop_starts if not flag)
        if n_trims != n - 1:
            print(f"[{TAG}] master length {total_frames}f "
                  f"({n - n_trims} chain start(s), {n_trims} overlap trim(s); "
                  f"old formula would have been "
                  f"{sum(lengths) - overlap_n * (n - 1)}f)",
                  flush=True)
        # Is the take long enough for the chain it is locked to?
        #
        # Every other duration in this pack is validated on the queue, and this
        # one was not. `_prepare_master_audio` loads the file, prints how long
        # it is, and nothing ever compares that to the chain. A take shorter
        # than the render runs the last hops past its end, `fit_samples`
        # zero-pads them, and those hops come back MUTE -- discovered after
        # paying for the render.
        #
        # It is the defect the review of the contributed patch listed third
        # ("`master_audio_secs` is computed, printed, and never used again"),
        # and rebuilding that feature from its prose reproduced it faithfully.
        # It was also hit during this project's own GPU testing and worked
        # around by hand, which is the clearest possible argument for a check.
        #
        # Raises rather than warns. Trailing silence is expressible -- pad the
        # take file -- but a mute final hop that nobody asked for is not worth
        # the minutes it costs to find out about.
        if locked is not None:
            take_s = float(locked["wav"].shape[-1]) / float(locked["sr"])
            need_s = float(total_frames) / FPS
            if take_s + 1.0 / FPS < need_s:
                raise ValueError(
                    f"{TAG}: master_audio_file is {take_s:.2f}s but this chain "
                    f"is {need_s:.2f}s ({total_frames}f at {FPS:g} fps). The "
                    f"last {need_s - take_s:.2f}s would be locked to silence "
                    f"the take does not contain. Shorten the chain, or pad the "
                    f"recording to at least {need_s:.2f}s.")
            if take_s > need_s + 1.0:
                print(f"[{TAG}] master_audio_file is {take_s:.2f}s for a "
                      f"{need_s:.2f}s chain; the last {take_s - need_s:.2f}s "
                      f"is not used", flush=True)

        # A dry run must not allocate the master. At 8 x 15 s and 1280x736 that
        # is 2742 full float frames -- ~31 GB -- for a feature whose entire
        # point is that it costs seconds.
        master_imgs = None if dry else _alloc_master(total_frames, height, width)
        write_pos = 0
        # Where each join actually landed. Recorded rather than derived,
        # because it can no longer BE derived: `seam.seam_positions()` solves
        # for a uniform hop length from (total, hops, overlap), and a restart
        # hop writes its full length instead of being trimmed. On a 4-hop chain
        # with a restart on hop 4 that estimate lands on 198/373/548 where the
        # joins are at 192/362/532 -- it measures the middle of hops and calls
        # them seams. The node that writes the frames is the only thing that
        # knows for certain, so it says so on `info`.
        seam_marks = []
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
            # The fingerprint is printed because it is the one cache input a
            # user cannot see and cannot derive. If a run that should have hit
            # re-rendered everything, this line moving between two runs says so
            # in one glance -- and a node that mutates a public scalar attribute
            # on itself between queues (see `_object_scalars`) is exactly the
            # case that would otherwise look like the cache is simply broken.
            print(f"[{TAG}] hop cache: {hop_store.root} "
                  f"(budget {float(cache_budget_gb):.0f} GB, model {model_fp})",
                  flush=True)
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
            "ref_size": str(ref_image_size),
            # No "pin" here either, for the same reason as "overlap" above:
            # `_attach_pin_to_qwen` is called only under `if i > 0`, so
            # pin_to_qwen cannot reach hop 1's pixels. Keyed chain-wide it threw
            # away a byte-identical cached hop 1 on every pin_to_qwen A/B. It is
            # in the per-hop key below, from hop 2.
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
            # All three slots, not just the first. Digesting only slot 1 would
            # let a chain rendered with a second voice be served to a run that
            # dropped it -- the silently-wrong-frames class this key exists to
            # prevent.
            "voice": [_store.audio_digest(v) for v in (voice, voice_2, voice_3)],
            "refvid": [_store.tensor_digest(v) for v in
                       (reference_video, reference_video_2, reference_video_3)],
            "refvid_audio": sorted(base_video_audios or {}),
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
        if locked is not None:
            # Only present when the lock is on. Adding a None field while
            # off would move every existing cache key and break the
            # empty-string byte-identical claim.
            chain_salt["master_audio"] = locked["digest"]
        prev_key = None
        hop_keys = []
        # tone_compensate=anchor state. `anchor_ref` is the Lab look every hop
        # is eased back toward. A shot with tone="rebase" moves the reference
        # onto itself, which is how a scene that is genuinely darker from here
        # on stops being fought.
        #
        # Two places it can come from. `hop1` is the original: the first hop's
        # own look, the one tone in the chain nothing has drifted into yet.
        # `still` is start_image, and for a long chain it is the better target,
        # because hop 1 is NOT the reference -- an outside 9-hop study measured
        # the photograph at chroma 33.6 and hop 1 at 30, b* 26.6 against 22,
        # before any relay had happened. Anchored on hop 1 the chain holds a
        # target that already fell short; anchored on the still it holds the
        # thing the user actually chose. The still also lets hop 1 itself be
        # pulled, which the hop1 mode cannot do by construction.
        anchor_on = tone_mode == "anchor" and float(tone_anchor) > 0.0
        anchor_from_still = anchor_on and str(tone_anchor_ref) == "still"
        anchor_ref = None
        if anchor_from_still:
            anchor_ref = _tone.anchor_stats(start_image)
            print(f"[{TAG}] tone anchor set from the start image: "
                  + _tone.anchor_note(anchor_ref), flush=True)
        sheet_rows = []

        assembled = []
        for i, block in enumerate(blocks):
            mm.throw_exception_if_processing_interrupted()
            shot = shots[i] or {}
            hop_length = lengths[i]
            print(f"[{TAG}] hop {i + 1}/{n}...", flush=True)
            # Computed HERE, not beside the sampler where it used to be. A
            # restart is a chain start, and almost everything that makes a hop
            # a start rather than a continuation -- which references ride it,
            # whether the previous frame is pinned as <Picture 1>, whether the
            # prompt says "opens already in progress" -- is decided in the two
            # hundred lines below this point. Deciding `hop_restart` after all
            # of them meant a restart hop was assembled as a continuation and
            # only then handed the photograph, which is how it ended up being
            # told it continues from pinned frames it does not have.
            hop_restart = i > 0 and str(shot.get("anchor") or "") == "restart"
            hop_is_start = i == 0 or hop_restart
            if hop_restart:
                print(f"[{TAG}] hop {i + 1}: ANCHOR RESTART -- start image is "
                      f"frame 0, the previous hop is not relayed", flush=True)
            _push_preview(unique_id, f"hop {i + 1}/{n} sampling…", hop=i + 1, total=n,
                           frac=(write_pos / float(total_frames)) if total_frames else None)

            # With a register, this hop's refs are only the ones active on it.
            # shot.refs, when present, is the whole rail for this hop --
            # including the empty list, which is how you drop identity stills
            # on a pin-less restart without changing the rest of the chain.
            # Omitted keeps the register default: unscheduled stills ride
            # chain starts and stay off continuations under hop_script=next.
            hop_active = []
            hop_subject_prose = ""
            if ref_plan_refs:
                shot_refs = shot.get("refs")
                if shot_refs is not None:
                    hop_active = _refs.select_for_shot(
                        ref_plan_refs, shot_refs, set(slot_images))
                    print(
                        f"[{TAG}] hop {i + 1}: shot.refs "
                        + (", ".join("@" + t for t in shot_refs) or "(none)"),
                        flush=True,
                    )
                else:
                    hop_active = _refs.active_refs(
                        ref_plan_refs, i, set(slot_images))
                    # Continuation: omit shots[] = hop 1 only. Face/outfit plates
                    # of a different room (chain_00034) beat the pin as Pictures 1–3
                    # and hop 2 opened a new Ref2VA generate — commercial kitchen,
                    # apron gone. List hop numbers on a ref to ride later hops.
                    if not hop_is_start and str(hop_script) == "next":
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
                if not hop_is_start and str(hop_script) == "next":
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
            # Not on a restart. Pinning the previous hop's last frame as
            # <Picture 1> is the definition of a continuation, and a restart
            # takes NOTHING from the previous hop -- the branch at the sampler
            # says so in as many words. It was still handing over the tail here.
            if not hop_is_start:
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
            if hop_is_start and hop_subject_prose and not _d.is_full_h3_prompt(block):
                block = hop_subject_prose + "\n\n" + block
            # Hop 1 has no _assemble_next to fold this into. A full H3 block is
            # the author's own prompt end to end, so it is left alone there --
            # the same rule subject_prose follows two lines up.
            if hop_is_start and refvid_line and not _d.is_full_h3_prompt(block):
                block = block.rstrip() + "\n\n" + refvid_line
            if str(hop_script) == "next" and not hop_is_start:
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

            elif not hop_is_start and hop_active:
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
            # `voice_every_hop` lifts it deliberately; `_voice_rides_hop`
            # holds the reasoning and why `speaking` is not the same risk
            # as `on`.
            _voice_lift = _voice_rides_hop(voice_every_hop, block)
            hop_voice = voice is not None and (
                hop_is_start or str(hop_script) != "next" or _voice_lift)
            if not hop_is_start and voice is not None and not hop_voice:
                _why = ("no spoken line in this beat"
                        if str(voice_every_hop) == "speaking"
                        else "pin carries the spoken audio; "
                             "voice_every_hop=speaking rides it")
                print(
                    f"[{TAG}] hop {i + 1}: voice ref stays off this continue "
                    f"({_why})",
                    flush=True,
                )
            elif not hop_is_start and voice is not None and _voice_lift:
                print(
                    f"[{TAG}] hop {i + 1}: voice ref rides this continue "
                    f"(voice_every_hop={voice_every_hop})",
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
            # A restart outranks a forced `pin_mech`: it relays nothing, so
            # there is no mechanism left to pick. Asking for motion_context on
            # a restart hop is not a contradiction to raise on, it is a setting
            # that does not reach this hop.
            pin_mech_pred = ("none" if hop_restart
                             else _pin_mech_for(i, overlap_n, prev_sampled,
                                                mode=str(pin_mech)))
            pin_mech_used = pin_mech_pred
            if hop_store is not None:
                # `None`, not prev_key, on a restart: the hop genuinely does
                # not depend on its predecessor, so chaining it would re-render
                # every restart whenever anything earlier moved -- and later
                # hops should chain from the restart, which they do because
                # this key becomes their prev_key.
                hop_payload = {
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
                    # Explicit, not implied by pin_mech="none": a restart also
                    # drops prev_key, and a key must say what produced it.
                    "restart": hop_restart,
                    # Only from hop 2. Hop 1 has no pin -- `_pin_mech_for`
                    # returns "none" for index 0 and the conditioning branch is
                    # `elif i > 0` -- so its frames cannot depend on these
                    # levers, and keying them in threw away a byte-identical
                    # cached hop 1 every time one was flipped. That is a third
                    # of the cost of every lever A/B, on the one hop nobody
                    # needed to re-render.
                    "pin_cond": ((pin_renorm_mode, round(pin_noise_v, 4), audio_ctx)
                                 if not hop_is_start else None),
                    # Same rule, moved out of chain_salt: how many frames the
                    # previous hop hands over changes this hop's conditioning
                    # and its trim, and nothing on a chain start. A restart
                    # is a start -- it neither pins nor trims -- so overlap
                    # cannot reach its pixels. `i > 0` here used to re-render
                    # every restart whenever overlap moved.
                    "overlap": (overlap_n if not hop_is_start else None),
                    # Tone, and the same "only from hop 2" rule again. The
                    # comment above `hop_store.put` explains why the mode is
                    # kept out of the key: the cache holds RAW hops, corrected
                    # afterwards, so switching modes need not invalidate them.
                    # That is true of the hop it corrects and false of the next
                    # one, because the corrected frames are what `prev_imgs`
                    # becomes and `prev_imgs` is the pin. Hop 2 rendered under
                    # anchor=still is a different render from hop 2 under
                    # anchor=off, and without this it was served either way --
                    # its key was byte-identical. Hop 1 has no pin, so it still
                    # survives every tone switch, which is the half of that
                    # claim that was always true.
                    "tone": ((tone_mode, round(float(tone_anchor), 4),
                              str(tone_anchor_ref)) if not hop_is_start else None),
                    # Same rule. `_attach_pin_to_qwen` runs only when this hop
                    # has a predecessor pin, so a restart (hop_is_start) is
                    # keyed like hop 1: the setting cannot reach its pixels.
                    "pin_qwen": (str(pin_to_qwen) if not hop_is_start else None),
                    # Whether this hop actually received the voice tensor.
                    # chain_salt already digests the file; without this a hop 2
                    # rendered with the clip on would be served to a later run
                    # that kept it off.
                    "voice_on": hop_voice,
                }
                # last_frame_guide reaches EVERY hop, including hop 1, so it
                # is not gated on hop_is_start. Only present when on: adding
                # "off" would move every existing cache key and break the
                # default-off byte-identical claim.
                _lfg = _last_frame_guide_key_field(last_frame_guide, i, shots)
                if _lfg is not None:
                    hop_payload["last_frame_guide"] = _lfg
                # The refine reaches EVERY hop, hop 1 included -- it runs after
                # every sampler, and under `full` it rewrites hop 1's delivered
                # pixels while under `pin_only` it rewrites the latent hop 1
                # hands forward. So a refine run must never be served a hop
                # cached by an `off` run; that is the whole A/B, silently void.
                # Present only when on, for the same reason last_frame_guide is:
                # adding "off" would move every existing cache key.
                if str(hop_refine) != "off":
                    hop_payload["refine"] = [
                        str(hop_refine), round(float(refine_denoise), 4),
                        int(refine_steps), str(refine_sampler),
                        str(refine_scheduler), str(refine_cond),
                        str(refine_head), str(refine_audio),
                        str(refine_blend).strip(), str(refine_blend_interp),
                        refine_model_fp]
                hop_key = _store.hop_key(
                    None if hop_restart else prev_key, hop_payload)
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
                # Every argument by NAME, none by position. Passing the first
                # seven positionally worked here and broke for a user on a
                # different ComfyUI build: their core orders the parameters
                # differently, so the seventh positional landed on
                # `ref_image_size` and the keyword collided with it --
                # "got multiple values for argument 'ref_image_size'", raised
                # on hop 1 before anything sampled. Core's signature is not
                # ours to depend on; its parameter names are the contract.
                packed = _core_call(
                    MiniMaxH3ReferenceToVideo, "the reference conditioning",
                    clip=clip, vae=vae, audio_vae=audio_vae, prompt=block,
                    width=int(width), height=int(height), length=hop_length,
                    ref_image_size=ref_image_size,
                    ref_images=hop_images,
                    ref_videos=hop_videos,
                    # Paired by ordinal with ref_videos. The pin clip, when
                    # pin_to_qwen appends one, takes the next number and simply
                    # has no entry here -- it is the previous hop's picture and
                    # has no soundtrack of its own.
                    ref_video_audios=base_video_audios,
                    ref_audios=(ref_audios if hop_voice else None),
                )
                cond, latent = _result(packed)[0], _result(packed)[1]
                # The conditioning as Ref2VA built it, before any pin or guide
                # is added to it. `refine_cond=base` pulls the second pass
                # toward this -- the references and the text, nothing about the
                # join. Captured here rather than reconstructed later because
                # every branch below rebinds `cond`.
                base_cond = cond

                if (i == 0 or hop_restart) and start_image is not None:
                    # A restart hop is a chain start. It gets the photograph as
                    # its frame-0 anchor exactly as hop 1 does, and NOTHING from
                    # the previous hop reaches it -- no sampler latent, no
                    # decoded tail. That is the entire point: every hop
                    # otherwise inherits its predecessor's end state, and a
                    # clip's end is its most settled moment, so motion and
                    # lighting response decay hop over hop. A restart bounds
                    # that accumulation to the distance between restarts
                    # instead of letting it run the length of the chain.
                    cond = _result(_core_call(
                        MiniMaxH3AddGuide,
                        "the restart anchor" if hop_restart else "the hop-1 start image",
                        positive=cond, latent=latent, frame_idx=0,
                        vae=vae, audio_vae=None,
                        image=start_image[:1], audio=None,
                    ))[0]
                elif i > 0 and not hop_restart:
                    pin_latent = prev_sampled
                    if pin_latent is not None:
                        pin_latent, pin_anchor = _condition_pin_latent(
                            pin_latent, pin_anchor,
                            mode=pin_renorm_mode, noise=pin_noise_v,
                            seed=(int(seed) + i))
                    cond, pin_mech_used = _pin_continue(
                        cond, latent, vae, audio_vae, overlap_n,
                        pin_latent, prev_imgs, prev_audio,
                        audio_ctx=audio_ctx, mode=str(pin_mech),
                    )

                if (_guides_last_frame(last_frame_guide, i, shots)
                        and start_image is not None):
                    # Conservative half: pin the still at the last PIXEL
                    # frame. Does NOT become the next hop's frame 0 -- that
                    # is keyframe chaining, a v3 conversation. After the
                    # frame-0 / Motion-Context pin so both keyframes sit
                    # on `cond`; before the audio lock, which mutates the
                    # latent not the conditioning.
                    _end = _last_pixel_guide_idx()
                    cond = _result(_core_call(
                        MiniMaxH3AddGuide,
                        "the last-frame guide",
                        positive=cond, latent=latent, frame_idx=_end,
                        vae=vae, audio_vae=None,
                        image=start_image[:1], audio=None,
                    ))[0]
                    print(f"[{TAG}] hop {i + 1}: last-frame guide "
                          f"(still at pixel frame_idx={_end})",
                          flush=True)

                if locked is not None:
                    # AFTER the pin: Motion-Context may rewrite this hop's
                    # latent. Locking first would be overwritten. BEFORE
                    # the sampler: the freeze has to be in place when
                    # denoise runs.
                    _t0, _t1 = _alock.hop_audio_window_s(
                        i, hop_length, overlap_n, FPS,
                        lengths=lengths, start_at=hop_starts)
                    _parts = _latents.from_dict(latent)
                    if _parts is None or len(_parts) < 2:
                        raise RuntimeError(
                            f"{TAG}: hop {i + 1}: master_audio_file needs a "
                            "joint AV latent and this hop did not have one.")
                    _alen = int(_parts[1].shape[-1])
                    _z = _encode_locked_slice(
                        audio_vae, locked["wav32"], _t0, _t1, _alen)
                    latent = _splice_locked_audio(latent, _z)
                    print(f"[{TAG}] hop {i + 1}: audio locked "
                          f"[{_t0:.2f}s-{_t1:.2f}s] of master_audio_file",
                          flush=True)

                _offload_text_encoder(clip, model)

                guider = _result(_core_call(
                    BasicGuider, "the guider",
                    model=model, conditioning=cond))[0]
                if shot.get("seed") is not None:
                    shot_seed = int(shot["seed"])
                else:
                    shot_seed = (int(seed) + i) if seed_per_shot else int(seed)
                hop_steps = int(shot.get("steps") or steps)
                if hop_steps not in sigma_cache:
                    sigma_cache[hop_steps] = _result(_core_call(
                        BasicScheduler, "the sigma schedule",
                        model=model, scheduler=scheduler, steps=hop_steps,
                        denoise=1.0))[0]
                hop_sigmas = sigma_cache[hop_steps]
                if hop_steps != int(steps) or shot.get("seed") is not None:
                    print(f"[{TAG}] hop {i + 1} override: seed={shot_seed} "
                          f"steps={hop_steps}", flush=True)
                noise = _result(_core_call(
                    RandomNoise, "the noise source",
                    noise_seed=shot_seed))[0]
                sampled = _result(_core_call(
                    SamplerCustomAdvanced, "the sampler",
                    noise=noise, guider=guider, sampler=sampler,
                    sigmas=hop_sigmas, latent_image=latent))[0]

                # Refine, before the decode so `full` reaches the delivered
                # pixels. After the sampler, so pin_mech / the guides / the
                # audio lock are all already spent and none of them can see it.
                refined_for_pin = None
                if str(hop_refine) != "off":
                    if str(hop_refine) == "pin_only" and i >= n - 1:
                        # Nothing downstream would ever read it.
                        print(f"[{TAG}] hop {i + 1}: refine skipped "
                              "(pin_only, last hop teaches nobody)", flush=True)
                    else:
                        _rguider = guider
                        _rmodel = model
                        if refine_model is not None:
                            # Its own model, so its own guider: the hop's guider
                            # is bound to the hop's model and would ignore this
                            # input entirely. Conditioning still follows
                            # refine_cond -- the model changes, not what the
                            # refine is being pulled toward.
                            _rmodel = refine_model
                            _rguider = _result(_core_call(
                                BasicGuider, "the refine guider (refine_model)",
                                model=refine_model,
                                conditioning=(base_cond
                                              if str(refine_cond) == "base"
                                              else cond)))[0]
                        elif str(refine_cond) == "base":
                            _rguider = _result(_core_call(
                                BasicGuider, "the refine guider (base cond)",
                                model=model, conditioning=base_cond))[0]
                        # Hold the pinned head out of the refine, and the audio
                        # out of it separately. Only hops 2+ have a head -- hop
                        # 1's first frames continue nothing, so freezing them
                        # would just leave the opening grainier than the rest of
                        # the chain. Audio has no such exemption: hop 1's voice
                        # is what every later hop's pin continues, so gating the
                        # two together left the whole chain sounding re-cooked.
                        _rin, _rfrozen, _rafrz = sampled, 0, False
                        _do_head = (str(refine_head) == "freeze"
                                    and not hop_is_start)
                        _do_aud = (str(refine_audio) == "freeze")
                        if _do_head or _do_aud:
                            _rin, _rfrozen, _rafrz = _refine_head_freeze(
                                sampled, overlap_n, freeze_head=_do_head,
                                freeze_audio=_do_aud)
                            if _do_head and not _rfrozen:
                                print(f"[{TAG}] hop {i + 1}: refine_head="
                                      "freeze had nothing to freeze "
                                      f"(overlap {overlap_n}f is off the "
                                      "latent grid)", flush=True)
                            if _rfrozen or _rafrz:
                                _held = []
                                if _rfrozen:
                                    _held.append(
                                        f"head ({overlap_n}f pin = {_rfrozen} "
                                        f"video steps, ramp "
                                        f"{REFINE_HEAD_RAMP})")
                                if _rafrz:
                                    _held.append("audio whole")
                                print(f"[{TAG}] hop {i + 1}: refine holds "
                                      + " + ".join(_held), flush=True)
                        _ref = _refine_sampled(
                            _rin, model=_rmodel, guider=_rguider,
                            sampler=refine_sampler_obj,
                            scheduler=refine_sched,
                            steps=(int(refine_steps) or int(hop_steps)),
                            denoise=float(refine_denoise), seed=shot_seed,
                            sigma_cache=sigma_cache, hop_no=i + 1,
                            sampler_label=(sampler_name
                                           if str(refine_sampler) == "same"
                                           else str(refine_sampler)),
                            cond_label=str(refine_cond),
                            own_model=(refine_model is not None))
                        # SamplerCustomAdvanced copies the input dict, so the
                        # freeze mask would ride out on the result and into
                        # prev_sampled. Put back whatever the hop itself left
                        # there -- usually nothing, the audio lock when a master
                        # track is loaded.
                        if _rfrozen or _rafrz:
                            _ref = dict(_ref)
                            if sampled.get("noise_mask") is None:
                                _ref.pop("noise_mask", None)
                            else:
                                _ref["noise_mask"] = sampled["noise_mask"]
                        _ref = _refine_blend_latent(
                            sampled, _ref, refine_keys,
                            str(refine_blend_interp), hop_no=i + 1)
                        if str(hop_refine) == "full":
                            # Delivered pixels AND the next teacher.
                            sampled = _ref
                        else:
                            # pin_only: the teacher only. The decode below still
                            # runs on the first pass, so not one delivered pixel
                            # of this hop moves.
                            refined_for_pin = _latent_cpu(_ref)
                        del _ref

                imgs, audio = _decode_av(vae, audio_vae, sampled)
                imgs = imgs.contiguous().cpu()
                wav = audio["waveform"].contiguous().cpu()
                sr = int(audio["sample_rate"])
                audio = {"waveform": wav, "sample_rate": sr}
                if locked is not None:
                    # The pin of the NEXT hop must carry the take, not a
                    # decoded generate. Replace this hop's audio with the
                    # matching window of the recording.
                    _t0, _t1 = _alock.hop_audio_window_s(
                        i, hop_length, overlap_n, FPS,
                        lengths=lengths, start_at=hop_starts)
                    audio = _slice_take_audio(locked, _t0, _t1, sr)
                    wav = _batch_wav(audio["waveform"].contiguous().cpu())
                    audio = {"waveform": wav, "sample_rate": sr}
                this_sampled = (refined_for_pin if refined_for_pin is not None
                                else _latent_cpu(sampled))

                # base_cond is deleted with cond because it is usually the
                # same object: keeping the name alive would hold one hop's
                # conditioning resident until the next hop overwrites it.
                del sampled, latent, cond, base_cond, guider, noise
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
                if i == 0 and not anchor_from_still:
                    anchor_ref = _tone.anchor_stats(imgs)
                    if anchor_ref is not None:
                        print(f"[{TAG}] tone anchor set from hop 1: "
                              + _tone.anchor_note(anchor_ref), flush=True)
                elif shot_tone == "rebase":
                    anchor_ref = _tone.anchor_stats(imgs)
                    print(f"[{TAG}] hop {i + 1}: tone=rebase, anchor moved to "
                          f"this hop; later hops hold ITS level", flush=True)
                elif shot_tone == "free":
                    print(f"[{TAG}] hop {i + 1}: tone=free, anchor pull skipped",
                          flush=True)
                else:
                    # The ramp exists to keep a JOIN exact -- frame 0 of a hop
                    # has to equal the previous hop's last frame. Hop 1 and a
                    # restart hop have no such frame: one opens the chain, the
                    # other opens on the photograph by construction. Ramping
                    # them would spend two seconds fading INTO the correction
                    # at the exact moment the viewer is deciding what the shot
                    # looks like.
                    imgs, anchor_note = _tone.anchor_pull(
                        imgs, anchor_ref, strength=float(tone_anchor),
                        ramp=(0 if (i == 0 or hop_restart) else _tone.ANCHOR_RAMP))
                    if anchor_note:
                        tone_note = (tone_note + " + " + anchor_note
                                     if tone_note else anchor_note)
                        print(f"[{TAG}] hop {i + 1} tone: {anchor_note}",
                              flush=True)

            if hop_is_start:
                keep_n = int(imgs.shape[0])
                if write_pos + keep_n > total_frames:
                    raise ValueError(
                        f"{TAG}: hop {i + 1} overruns the preallocated master "
                        f"({write_pos + keep_n} > {total_frames}). A hop decoded a "
                        f"different length than planned.")
                if i > 0:
                    seam_marks.append(int(write_pos))
                master_imgs[write_pos:write_pos + keep_n] = imgs
                write_pos += keep_n
                if i == 0:
                    master_wav = wav
                else:
                    # A cut, but 40 ms of xfade still kills the click at the
                    # sample boundary. Nothing is trimmed: this hop does not
                    # overlap the previous one.
                    master_wav = _xfade_audio(master_wav, wav, sr)
                    print(
                        f"[{TAG}] hop {i + 1}: restart, wrote all {keep_n} "
                        f"frames (no overlap trim)",
                        flush=True,
                    )
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
                seam_marks.append(int(write_pos))
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
                # The frames this hop actually CONTRIBUTES: a chain start
                # (hop 1 or a restart) gives all of them, a continuation
                # gives what survives the overlap trim. Showing imgs[0] on
                # a continuation would show a frame the master never contains.
                _f0 = imgs[0] if hop_is_start else imgs[overlap_n]
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
        # Machine-readable, on its own line, first thing after the header. The
        # seam report reads this instead of asking the user to retype `hops`
        # and `overlap` and then solving for them -- which is both a chore and,
        # since restart hops stopped being trimmed, wrong.
        if seam_marks:
            info += chr(10) + "seams: " + ", ".join(str(f) for f in seam_marks)
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

        if locked is not None and master_wav is not None:
            _n = _alock.passthrough_n_samples(
                int(master_imgs.shape[0]), locked["sr"], FPS)
            _out = _alock.fit_samples(locked["wav"], _n)
            master_wav = _out.unsqueeze(0)
            sr = locked["sr"]
            _dur = _n / float(sr) if sr else 0.0
            print(f"[{TAG}] final audio: passthrough of master_audio_file "
                  f"[0.00s-{_dur:.2f}s]", flush=True)

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
