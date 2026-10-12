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
    DisableNoise,
    ExtendIntermediateSigmas,
    KSamplerSelect,
    RandomNoise,
    SamplerCustomAdvanced,
    SplitSigmas,
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
from . import refmod as _refmod
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
from . import tiled_upsampler as _tu
from . import upscale_guide as _ug
from . import hand_tie as _ht
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
#
# Above native the ladder climbs in 0.1 MP steps to 2.0 MP. Those rungs are
# past what H3 was trained for; the area cap scales with the tier (_area_cap),
# so each is the native canvas grown, not a different shape. Their values are
# the short edge that lands 16:9 nearest the labelled area, which is why they
# are not the round number in the label -- the label names the edge you get.
RESOLUTIONS = {
    "1088p (2.0 MP)": 1081,
    "1056p (1.9 MP)": 1062,
    "1024p (1.8 MP)": 1044,
    "992p (1.7 MP)": 1007,
    "960p (1.6 MP)": 971,
    "960p (1.5 MP)": 952,
    "896p (1.4 MP)": 917,
    "864p (1.3 MP)": 880,
    "832p (1.2 MP)": 844,
    "800p (1.1 MP)": 808,
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
    # Every value satisfies align_frame_count (n % 17 == 5) at FPS 24: the
    # aligned count nearest the label. That grid is 17 f (0.71 s), so a label
    # can be up to ~0.35 s off the frames rendered. The five 1.x labels keep
    # the counts they always had, so saved graphs key the same.
    "5 s": 124, "6 s": 141, "7 s": 175, "8 s": 192, "9 s": 209,
    "10 s": 243, "11 s": 260, "12 s": 294, "13 s": 311, "14 s": 328,
    "15 s": 362, "16 s": 379, "17 s": 413, "18 s": 430, "19 s": 464,
    "20 s": 481, "21 s": 498, "22 s": 532, "23 s": 549, "24 s": 583,
    "25 s": 600, "26 s": 617, "27 s": 651, "28 s": 668, "29 s": 702,
    "30 s": 719,
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


def _area_cap(short_edge):
    """Core's 768*1344 cap at native and below; grown with the tier above it,
    so a 2.0 MP rung caps 21:9 the way 768p does rather than at 0.98 MP."""
    return CANVAS_AREA_CAP * max(1.0, short_edge / 768.0) ** 2


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
    cap = _area_cap(short)
    if w * h > cap:
        scale = math.sqrt(cap / (w * h))
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


def _vae_fingerprint(vae):
    """Identify the video VAE, so a hop decoded by one is not served to another.

    Cached hops are stored as DECODED frames, so the decoder is part of what a
    hop is. Without this, swapping the official H3 VAE for LynnReal's 26-block
    decoder on a cached chain served the first decoder's frames to the second
    run -- an A/B that compared a VAE with itself. The Motion-Context pin is a
    latent and was never affected; the AddGuide fallback and pin_to_qwen read
    decoded frames and were.

    The MODULE LAYOUT: every submodule's name and class. Never anything read
    off a weight. Weight values, and then even weight dtypes/shapes, read back
    differently once ComfyUI's dynamic VRAM loading had made the VAE resident,
    so one VAE keyed two ways on consecutive runs and every hop missed the
    cache (2026-10-01, twice). The 26- and 36-block decoders differ in layout.
    The result is pinned on the VAE object as well, so a session cannot drift.
    Known collision: two builds of identical layout (int8 vs fp16 of the same
    decoder, or two fine-tunes) key the same. A TensorRT VAE
    (ComfyUI-H3VAE_TRT) runs compiled .engine files, so its runners' engine
    paths, sizes and mtimes are keyed too.
    """
    if vae is None:
        return "none"
    pinned = getattr(vae, "_htc_vae_fp", None)
    if pinned:
        return pinned
    h = hashlib.sha256()
    inner = getattr(vae, "first_stage_model", vae)
    h.update(type(inner).__name__.encode())
    try:
        mods = [(n, type(m).__name__) for n, m in inner.named_modules()]
    except Exception:  # noqa: BLE001 -- key what we can rather than fail
        mods = []
    h.update(str(len(mods)).encode())
    for name, kind in mods:
        h.update(f"{name}:{kind};".encode())
    for runner in ("decoder_runner", "encoder_runner"):
        path = getattr(getattr(inner, runner, None), "model_path", None)
        if path:
            try:
                st = os.stat(path)
                h.update(f"{runner}:{os.path.basename(path)}:{st.st_size}:"
                         f"{int(st.st_mtime)}".encode())
            except OSError:
                h.update(f"{runner}:{path}".encode())
    fp = h.hexdigest()[:16]
    try:
        setattr(vae, "_htc_vae_fp", fp)
    except Exception:  # noqa: BLE001 -- an object that refuses attributes still keys
        pass
    return fp


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
                   n_stills=0,
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
        # A restart whose shot lists its own refs needs no photograph: it
        # is a fresh generate on those refs, as hop 1 is, so each location
        # cut opens on its own place instead of on one shared start image.
        # run() already skips the frame-0 guide when no image loaded.
        if not str(start_image_file or "").strip() and not sh.get("refs"):
            raise ValueError(
                f"{TAG}: shot {i + 1} is anchor=restart with no start image "
                "and no refs. A restart opens either on the start image or "
                "as a fresh generate on the shot's own refs; this one has "
                "neither. Set start_image_file in MEDIA, give the shot refs, "
                "or remove the anchor.")
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


def _start_carries_voice(hop_is_start, hand_tie, mode, block):
    """Whether a START hop carries the voice (a continuation asks `_voice_rides_hop`).

    Every hand-tie hop is assembled as a start, and a start carries the voice,
    so the quiet hops (a bite, a lick) got a timbre clip with no line to attach
    to: the chain_00038 failure, on every hop that eats. A hand-tie hop carries
    it where it speaks; `on` still rides every hop.
    """
    if not hop_is_start:
        return False
    if not hand_tie or str(mode or "off") == "on":
        return True
    return _voice_rides_hop("speaking", block)


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
    return _music.resample(wav, src_sr, dst_sr)


def _prepare_master_audio(path, start_s=0.0):
    """Load the take once: stereo, native rate kept, plus a 32 kHz copy.

    `start_s` drops the head of the file, and it is applied HERE rather than at
    the three places the take is sliced -- the hop encode, the pin the next hop
    inherits, and the delivered passthrough. Cutting once at load leaves the
    chain's own clock 0-based, so `hop_audio_window_s`, the length guard and
    the passthrough all keep reading exactly as they did; three offsets applied
    at three call sites is the arithmetic that eventually disagrees with itself
    by one hop. It also means the digest is taken AFTER the cut, so sliding the
    window invalidates the hop cache without anything being added to the salt.

    The window's WIDTH is not a parameter. It is `total_frames / FPS`, decided
    by duration x shots - overlap, so there is nothing here to choose but where
    it opens.
    """
    got = _media.load_audio(path)
    if got is None:
        raise ValueError(
            f"{TAG}: master_audio_file {path!r} did not load. The file has to "
            "resolve under h3_refs the same way voice_file does.")
    wav = _alock.force_stereo(got["waveform"].contiguous().cpu())
    sr = int(got["sample_rate"])
    full_s = float(wav.shape[-1]) / float(sr) if sr else 0.0
    off = max(0.0, float(start_s or 0.0))
    if off > 0.0:
        cut = int(round(off * sr))
        if cut >= int(wav.shape[-1]):
            raise ValueError(
                f"{TAG}: master_audio_start_s is {off:.2f}s but "
                f"master_audio_file is {full_s:.2f}s long, so the window "
                f"opens past the end of the take.")
        wav = wav[..., cut:].contiguous()
        # To the sample, not to the widget: a 0.1-step slider on a 48 kHz file
        # rounds, and the printed number should be the cut that happened.
        off = cut / float(sr)
    wav32 = _resample_wav(wav, sr, _alock.VAE_SR)
    digest = _store.audio_digest({"waveform": wav, "sample_rate": sr})
    print(f"[{TAG}] master_audio_file: loaded {path!r} "
          f"({full_s:.2f}s at {sr} Hz, stereo)"
          + (f"; window opens at {off:.2f}s, "
             f"{wav.shape[-1] / sr:.2f}s usable" if off > 0.0 else ""),
          flush=True)
    return {"path": path, "wav": wav, "sr": sr, "wav32": wav32,
            "digest": digest, "offset": off, "full_s": full_s}


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
# The row is ONE entry, refine_align=hop_tail: `denoise` builds an INDEPENDENT
# grid --
# int(steps/denoise) steps, keep the last steps+1 -- so the published 2/0.50 is
# the tail of a 4-step grid whatever the hop sampled, and its closing stride
# (0.8 -> 0) is wider in sigma than any step an 8-step hop takes. A distilled
# trunk is only accurate at the sigmas it was distilled for, so that stride is
# off-manifold by construction on exactly the bases this row is for; hop_tail
# re-runs the hop's own last sigmas instead, at identical cost.
#
# It belongs in the row and not in the default because the failure it fixes is
# a property of the BASE, not of the era's sampler fashion: an undistilled trunk
# is accurate everywhere, so `denoise` is fine there and stays the default --
# which also means a 2.1 graph reloads on the schedule it rendered under. That
# base-vs-fashion line is the whole admission test for this table: sampler,
# scheduler and step count are turbo-community fashion, they turn over every
# release, and a stamping preset that moved them would overwrite a choice the
# user made. They are deliberately absent and are not coming back.
#
# **refine_head=freeze was in this row and was REMOVED 2026-09-20.** Its
# evidence was a seam reading (1.9x/1.7x against 3.5x/7.3x), and CLAUDE.md is
# explicit that a seam measurement is corroboration, never the decision. The
# better reading is that freeze was compensating for the bug above: with
# `denoise` the refine entered hops 2+ at an off-grid sigma, so not refining the
# head was the lesser harm. hop_tail removes the cause, and the only end-to-end
# turbo evidence in this project -- a 7-hop chain on a turbo merge, hop_tail
# with refine_head=refine -- came out clean where the unrefined arm was crunchy.
# Freezing the head there would have cost grain for a seam fix already made.
# Every field a row leaves out passes the widget straight through.
SPEED_MODES = {
    "regular": {},
    "turbo": {"refine_align": "hop_tail"},
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
                    own_model=False, align="hop_tail",
                    hop_sigmas=None, hop_steps=0):
    """Second low-denoise sample of a FINISHED hop latent.

    Every ratchet lever before this one acts at the join and acts by arithmetic:
    `pin_renorm` rescales a statistic, `pin_mech=reset` blends bands of a warped
    photograph. The measured failure was always the same -- the statistics held
    while the CONTENT walked -- and arithmetic cannot put content back.
    Re-sampling can, because it runs the model. That is the whole reason this
    exists and the reason it sits here rather than at the join.

    `align` picks WHERE on the noise curve those model evals land, and it is the
    whole of the turbo story.

    `denoise` is the original path and builds an INDEPENDENT schedule:
    `BasicScheduler(steps, denoise)` asks for `int(steps/denoise)` steps and
    keeps the last `steps+1`. At the published 2 / 0.50 / shift 12 that is the
    tail of a FOUR-step grid, `[0.9231, 0.8000, 0]`. But the hop was sampled on
    an EIGHT-step grid, `[1.0, .9882, .9730, .9524, .9231, .8780, .8, .6316,
    0]`. The two grids share only their endpoints. The refine re-enters at the
    hop's step-4 sigma and then takes two strides where the hop took four --
    and its closing stride is `0.8 -> 0`, a jump 27% larger in sigma than any
    step the hop itself ever took. On a plain base that reads as the documented
    "deliberately under-converged" texture pass. On a turbo LoRA or merge it is
    a different thing entirely: a few-step distillation is only accurate AT the
    sigmas it was distilled for, so an off-grid stride of that size is taken by
    a model that was never trained to take it, from a state it was never shown.
    That is the misalignment -- not the number of evals, and not `s0`, which a
    LoRA does not move at all (the sigma transform comes from `shift_video`,
    and a LoRA does not touch `model_sampling`).

    `hop_tail` -- the default -- fixes it by construction rather than by
    tuning: the refine simply RE-RUNS THE HOP'S OWN LAST `steps` SIGMAS.
    `refine_steps=2` against that 8-step hop gives `[0.8, 0.6316, 0]` -- same
    two evals, same cost, every one of them on the grid the trunk was distilled
    for, and the closing stride is exactly the hop's own closing stride. It
    needs no re-tuning when the base changes, because it is DERIVED from
    whatever schedule the base is running; swap turbo for plain, or 8 steps for
    4, and the refine follows. `refine_denoise` is unused here and the log says
    so.

    `denoise` is kept so an archived run reproduces bit-for-bit.

    The schedule is cached under a ("refine", steps, denoise, scheduler) tuple.
    The hop schedules are cached under a bare int, so the two can never collide
    -- which matters more than it looks: a collision would hand the refine pass
    the hop's full-denoise schedule and quietly re-render the hop from noise.
    `scheduler` is in the key because refine_scheduler can differ from the hop's:
    without it, flipping simple/sgm_uniform mid-chain would be served the first
    one's sigmas and the widget would do nothing. `hop_tail` needs none of that
    -- it slices a schedule that already exists.
    """
    sched_label = str(scheduler)
    denoise_label = f"denoise={float(denoise):.2f} "
    if str(align) == "hop_tail" and hop_sigmas is not None:
        n_hop = int(hop_sigmas.shape[-1]) - 1
        # At least one of the hop's own steps always stays untouched. Without
        # the clamp `refine_steps >= hop_steps` would slice from sigma 1.0 and
        # the "refine" would silently become a full re-render -- the exact trap
        # the sigma-cache keys above were built to avoid.
        k = max(1, min(int(steps), n_hop - 1))
        if k != int(steps):
            print(f"[{TAG}] hop {hop_no}: refine steps {int(steps)} -> {k} "
                  f"(hop_tail holds one of the hop's {n_hop} steps back; "
                  f"a refine entering at sigma 1.0 is a re-render)", flush=True)
        r_sigmas = hop_sigmas[n_hop - k:]
        sched_label = f"hop_tail[-{k}] of {n_hop}"
        denoise_label = ""
    else:
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
          f"{denoise_label}"
          f"sigma={float(r_sigmas[0]):.4f}->0 "
          f"[{sampler_label}/{sched_label}, cond={cond_label}"
          + (", refine_model" if own_model else "") + "]", flush=True)
    return _result(_core_call(
        SamplerCustomAdvanced, "the refine sampler",
        noise=r_noise, guider=guider, sampler=sampler,
        sigmas=r_sigmas, latent_image=sampled))[0]


# Realism boost: The AI Brief's Singularity dual-sampling recipe, run inside the
# hop sampler instead of after it. Not a refine -- the hop is sampled ONCE, on a
# schedule that changes model after its first base step.
#
#   BasicScheduler(steps) -> ExtendIntermediateSigmas(2) -> SplitSigmas(2)
#
# Extend(2) puts one midpoint in every gap (`linspace(0, 1, 3)[1:-1]`), so the
# schedule doubles: base 6 is 12 steps, split 2 + 10. Stage 1 is therefore the
# first base step, halved -- at shift 12 / simple, sigma 1.0 -> 0.984. The boost
# model then paints the other 10 steps from a latent that is ~98% noise. The
# original's reference point is base 6; do not re-validate it at 8.
BOOST_EXTEND = 2
BOOST_SPLIT = 2
# (file, strength) per slot, from the original's Final stack: LMS 0.5 under
# People 1.0. A default only lands when the file is actually installed. Slot 3
# is not in the original and ships empty.
BOOST_LORA_DEFAULTS = (
    ("minimax_h3_lms_v1.0_r64.safetensors", 0.5),
    ("h3-realism-people-t2v-i2v-r2v.safetensors", 1.0),
    ("None", 1.0),
)
# One Core loader per slot, kept for the process, so a re-queue re-uses the
# LoRA it already read instead of going back to disk. Core caches per instance.
_BOOST_LOADERS = {}

# The chain's upsample re-draw, below the tiled upsampler recipe's 0.2. Picked
# by eye on a boosted single hop (2026-09-30): 0.2 (sigma 0.75 at shift 12)
# repaints skin texture, 0.075 smeared mid-distance faces, 0.1 textured least
# while still sharp. The side node keeps the recipe's 0.2.
UPSAMPLE_DENOISE = 0.1


def _boost_lora_choices():
    try:
        import folder_paths
        return ["None"] + list(folder_paths.get_filename_list("loras"))
    except Exception:  # noqa: BLE001 -- INPUT_TYPES must never raise
        return ["None"]


def _boost_lora_default(slot, choices):
    name = BOOST_LORA_DEFAULTS[slot][0]
    return name if name in choices else "None"


def _boost_model(model, loras):
    """The hop's own model with the boost LoRAs on top.

    Built from the model AFTER the sigma shift, so both stages sample the same
    noise curve -- the stage-2 hand-off only cancels if they do. The LoRAs
    land after the drawn speed stack, as the original's Final stack does: turbo
    stays under them. Then the AdaLN port, when PlagueKind's node is installed,
    because a dense LoRA on a pruned base otherwise drops its 51 AdaLN keys
    with an error line each; the fix is measured at ~0.02% of the modulation
    signal, so its absence is logged, not refused.
    """
    import nodes as nodes_mod
    out = model
    for slot, (name, strength) in enumerate(loras):
        if name == "None" or float(strength) == 0.0:
            continue
        loader = _BOOST_LOADERS.get(slot)
        if loader is None:
            loader = _BOOST_LOADERS[slot] = nodes_mod.LoraLoaderModelOnly()
        out = loader.load_lora_model_only(out, name, float(strength))[0]
    fix = getattr(nodes_mod, "NODE_CLASS_MAPPINGS", {}).get("H3AdaLNLoRAFix")
    if fix is None:
        print(f"[{TAG}] realism boost: H3AdaLNLoRAFix not installed, boost "
              "LoRAs apply unported", flush=True)
    else:
        out = _result(_core_call(fix, "the boost AdaLN port",
                                 model=out, mode="port"))[0]
    return out


def _boost_sigmas(hop_sigmas, sigma_cache, hop_steps):
    """(stage-1 sigmas, stage-3 sigmas), sharing the hand-off sigma."""
    key = ("boost", int(hop_steps))
    if key not in sigma_cache:
        ext = _result(_core_call(
            ExtendIntermediateSigmas, "the boost schedule",
            sigmas=hop_sigmas, steps=BOOST_EXTEND, start_at_sigma=1.0,
            end_at_sigma=0.0, spacing="linear"))[0]
        hi, lo = _result(_core_call(
            SplitSigmas, "the boost split", sigmas=ext, step=BOOST_SPLIT))[:2]
        sigma_cache[key] = (hi, lo)
    return sigma_cache[key]


def _boost_sampled(latent, *, model, guider, boost_guider, sampler,
                   hop_sigmas, sigma_cache, hop_steps, seed, hop_no, label):
    """One hop, sampled on the realism-boost spine. -> the sampler output dict.

    Stage 2 is the original's "Second Sampling Pass": a SamplerCustomAdvanced on
    a single sigma, which makes no model call and only scales -- in with
    `noise_scaling` (sigma*noise + (1-sigma)*x0), out with
    `inverse_noise_scaling` (/(1-sigma)). Done here with the same two
    model_sampling methods on the same RandomNoise draw, which is that node's
    whole effect without loading the model for zero steps. Stage 3's
    DisableNoise then multiplies by (1-sigma) on the way in and the pair
    cancels to a correctly noised latent. That is why stage 2 must NOT use
    DisableNoise (it leaves 1.6% of x0 and no noise -- the orange frames) and
    why both stages must share the hand-off sigma exactly.

    Video re-noises from stage 1's DENOISED estimate; audio passes on from its
    OUTPUT, which is already noised at the hand-off sigma. Same wiring as the
    original.
    """
    hi, lo = _boost_sigmas(hop_sigmas, sigma_cache, hop_steps)
    n1 = int(hi.shape[-1]) - 1
    n3 = int(lo.shape[-1]) - 1
    if n3 < 1:
        raise RuntimeError(
            f"{TAG}: hop {hop_no}: realism boost needs steps >= 2 -- at "
            f"{hop_steps} the split leaves the boost model nothing to sample.")
    s = lo[0]
    print(f"[{TAG}] hop {hop_no}: realism boost {n1} + {n3} steps, {label}, "
          f"hand-off sigma {float(s):.4f}", flush=True)
    noise = _result(_core_call(RandomNoise, "the boost noise source",
                               noise_seed=int(seed)))[0]
    out, den = _result(_core_call(
        SamplerCustomAdvanced, "the boost stage-1 sampler",
        noise=noise, guider=guider, sampler=sampler,
        sigmas=hi, latent_image=latent))[:2]
    out_parts = _latents.from_dict(out)
    den_parts = _latents.from_dict(den)
    if not out_parts or not den_parts:
        raise RuntimeError(f"{TAG}: hop {hop_no}: realism boost could not "
                           "read the stage-1 latent.")
    x0 = den_parts[0]
    n = noise.generate_noise({"samples": x0}).to(x0.device, x0.dtype)
    ms = model.get_model_object("model_sampling")
    v = ms.inverse_noise_scaling(s, ms.noise_scaling(s, n, x0))
    mid = dict(out)
    mid["samples"] = _latents.rebuild(out["samples"], [v] + out_parts[1:])
    del den, den_parts, n, x0
    return _result(_core_call(
        SamplerCustomAdvanced, "the boost stage-3 sampler",
        noise=_result(_core_call(DisableNoise, "the boost stage-3 noise"))[0],
        guider=boost_guider, sampler=sampler, sigmas=lo,
        latent_image=mid))[0]


# hop_refine=split: the refine as the hop's own finish, not a second pass. The
# hop samples its schedule down to the start of its last K steps (K =
# refine_steps), then those K steps -- each cut in SPLIT_EXTEND -- continue the
# same latent with no new noise, on refine_model / refine_sampler. Nothing is
# re-noised and there is no second copy to blend: what is decoded is what the
# next hop is pinned to. Continuity holds because both stages carry the hop's
# own conditioning, Motion-Context pin included -- the same reason a boosted
# hop still continues. SPLIT_EXTEND is BOOST_EXTEND's midpoint rule, not its
# value by coincidence: one midpoint per gap.
SPLIT_EXTEND = 2
# Below this the upsample tile budget is read as "the recipe's": 0 is the
# documented default, and a graph saved with split_finish_aug in that widget
# slot casts its 0.999 / 0.85 to 0. One widget step, so no real budget a user
# dials in is ever overridden.
UPSAMPLE_MIN_TILE_TOKENS = 1024
SPLIT_DEFAULT_K = 2         # refine_steps=0 ("the hop's steps") means this here


def _split_k(refine_steps, hop_steps):
    """K for this hop: refine_steps (0 = SPLIT_DEFAULT_K), at least one step
    left for stage 1 and at least one finished finely."""
    k = int(refine_steps) or SPLIT_DEFAULT_K
    return max(1, min(k, int(hop_steps) - 1))


def _split_sigmas(hop_sigmas, sigma_cache, hop_steps, k):
    """(stage-1 sigmas, stage-2 sigmas), sharing the hand-off sigma."""
    key = ("split", int(hop_steps), int(k))
    if key not in sigma_cache:
        n = int(hop_sigmas.shape[-1]) - 1
        hi = hop_sigmas[:n - k + 1].clone()
        lo = _result(_core_call(
            ExtendIntermediateSigmas, "the split finish",
            sigmas=hop_sigmas[n - k:].clone(), steps=SPLIT_EXTEND,
            start_at_sigma=1.0, end_at_sigma=0.0, spacing="linear"))[0]
        sigma_cache[key] = (hi, lo)
    return sigma_cache[key]


def _split_sampled(latent, *, noise, guider, finish_guider, sampler,
                   finish_sampler, hop_sigmas, sigma_cache, hop_steps, k,
                   hop_no, label):
    """One hop, finished on the split. -> the sampler output dict.

    Stage 1's SamplerCustomAdvanced output is inverse-noise-scaled at the
    hand-off sigma; stage 2's DisableNoise multiplies it back by (1 - sigma) on
    the way in, so the pair cancels to the very latent stage 1 stopped at. A
    noise_mask on the latent (the audio lock) rides out of stage 1 on the
    copied dict and holds the take through stage 2 as it did through stage 1.
    """
    hi, lo = _split_sigmas(hop_sigmas, sigma_cache, hop_steps, k)
    print(f"[{TAG}] hop {hop_no}: split refine {int(hi.shape[-1]) - 1} + "
          f"{int(lo.shape[-1]) - 1} steps ({k} finished in {SPLIT_EXTEND}), "
          f"{label}, hand-off sigma {float(lo[0]):.4f}", flush=True)
    mid = _result(_core_call(
        SamplerCustomAdvanced, "the split stage-1 sampler",
        noise=noise, guider=guider, sampler=sampler,
        sigmas=hi, latent_image=latent))[0]
    return _result(_core_call(
        SamplerCustomAdvanced, "the split stage-2 sampler",
        noise=_result(_core_call(DisableNoise, "the split stage-2 noise"))[0],
        guider=finish_guider, sampler=finish_sampler, sigmas=lo,
        latent_image=mid))[0]


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
                    "tooltip": ("The prompt when no shot plan is set. Separate hops with --- on its own "
                                "line."),
                }),
                "chains": (["1", "2", "3", "4", "5", "6", "7", "8"], {
                    "default": "3",
                    "tooltip": ("How many hops to render when no shot plan is set. With a plan, the shot "
                                "count decides."),
                }),
                "resolution": (list(RESOLUTIONS), {
                    "default": DEFAULT_RESOLUTION,
                    "tooltip": ("Output size, as an area. 0.98 MP is H3's native size; the 1.1-2.0 MP "
                                "rungs above it are past what the model was trained for."),
                }),
                "aspect": (list(ASPECTS), {
                    "default": DEFAULT_ASPECT,
                    "tooltip": "Frame shape. Sets width and height together with resolution.",
                }),
                "duration": (list(DURATION_FRAMES), {
                    "default": "10 s",
                    "tooltip": ("Length of each hop. A shot can set its own. Judge joins at 8 s or longer; "
                                "5 s is too short to show them."),
                }),
                "overlap": (["0.9 s", "0.2 s", "1.6 s"], {
                    "default": "0.9 s",
                    "tooltip": ("How much of the previous hop each hop continues from. 0.9 s is the default "
                                "and usually right."),
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
                    ("Sampling steps per hop. A shot can set its own. 8 for a turbo LoRA; more "
                     "steps mostly improve generated speech.")}),
                "sampler_name": (comfy.samplers.KSampler.SAMPLERS, {"default": "res_multistep"}),
                "scheduler": (comfy.samplers.KSampler.SCHEDULERS, {"default": "beta"}),
                "shift_video": ("FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                "shift_audio": ("FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01}),
                "ref_image_size": (["match", "max"], {
                    "tooltip": ("How large reference pictures go in. match is faster; max keeps more face "
                                "detail but is slower."),
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
                    "tooltip": ("Only without a shot plan. next: later hops get only the new part of the "
                                "prompt. verbatim: every hop gets its text as written."),
                }),
                "pin_to_qwen": (["off", "last frame", "pin clip", "both"], {
                    "default": "last frame",
                    "tooltip": ("Shows the text encoder where the previous hop ended. last frame (default) "
                                "adds its last frame as a picture; pin clip adds the overlap as a short "
                                "video."),
                }),
                "shot_plan": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": "The shot list, written by the SCRIPT tab. One shot per hop.",
                }),
                "ref_plan": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "tooltip": ("The reference list, written by the REFS tab. Beats cite each reference by "
                                "@tag."),
                }),
                "cache_hops": (["off", "on"], {
                    "default": "off",
                    "tooltip": ("Keep each rendered hop on disk. Unchanged hops load instead of "
                                "re-rendering. Needed for retakes, render_from and the TIMELINE film."),
                }),
                "cache_budget_gb": ("FLOAT", {
                    "default": 20.0, "min": 1.0, "max": 500.0, "step": 1.0,
                    "tooltip": "Disk space for cached hops. The oldest are removed above this.",
                }),
                "audio_pin_frames": ("INT", {
                    "default": 24, "min": 0, "max": 240, "step": 24,
                    "tooltip": ("How much of the previous hop's sound each hop continues from, in frames "
                                "(24 = 1 s). Raise it for dialogue that runs across joins. 0 matches the "
                                "overlap."),
                }),
                "pin_renorm": (["off", "band"], {
                    "default": "off",
                    "tooltip": ("Counters the fine detail that builds up at each join. band applies it; off "
                                "leaves the join as generated."),
                }),
                "pin_noise": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 0.10, "step": 0.005,
                    "tooltip": ("Adds a little noise to the join, another way to stop detail building up. "
                                "Try 0.05; 0 is off."),
                }),
                "tone_compensate": (_tone.MODES, {
                    "default": "off",
                    "tooltip": ("Evens out the brightness shift between hops. anchor holds the whole chain "
                                "to one look; the other modes correct each join; off leaves hops as "
                                "rendered."),
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
                    "tooltip": "A picture for hop 1 to start on. Set it on the MEDIA tab.",
                }),
                "reference_video_file": ("STRING", {
                    "default": "",
                    "tooltip": "A clip to borrow motion or look from. Set it on the MEDIA tab.",
                }),
                "voice_file": ("STRING", {
                    "default": "",
                    "tooltip": "A voice sample, cited as <Audio 1>. Set it on the MEDIA tab.",
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
                    "tooltip": ("A line added at the start of hop 1. The default says live action; change "
                                "it for another style, or clear it."),
                }),
                # Appended 2026-08-30, still obeying the append-only rule above.
                "render_through": ("INT", {
                    "default": 0, "min": 0, "max": 64,
                    "tooltip": "Stop after this hop. 0 renders every hop.",
                }),
                "quality": (["final", "draft"], {
                    "default": "final",
                    "tooltip": ("draft renders small and fast to check the story and the joins. final is "
                                "the real render."),
                }),
                "dry_run": (["off", "on", "previs"], {
                    "default": "off",
                    "tooltip": ("Build every prompt without rendering, to check what each hop will send. "
                                "Read them on the info output. previs (pin_mech=hand_tie): render only "
                                "each section's previs and output it, to check it before the hops; it "
                                "is cached, so the full run then skips it."),
                }),
                "contact_sheet": (["off", "on"], {
                    "default": "off",
                    "tooltip": "Also output an image with each hop's first and last frame and its beat.",
                }),
                "tone_anchor": ("FLOAT", {
                    "default": _tone.ANCHOR_STRENGTH, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": ("How strongly anchor pulls each hop back to the reference look. 0.35 is "
                                "gentle; raise it if a long chain fades."),
                }),
                # APPENDED, never inserted -- the same rule the ref sockets at
                # the top of this block were deleted for. `widgets_values` is a
                # positional list, so a widget added anywhere but the end shifts
                # every widget after it, and a workflow saved before the change
                # reads its own settings out of the wrong slots. Silently: the
                # numbers all still parse, they are just the wrong numbers.
                "music_gain_db": ("FLOAT", {
                    "default": -14.0, "min": -60.0, "max": 6.0, "step": 0.5,
                    "tooltip": "Music level. -14 sits under speech; -6 is music-led.",
                }),
                "music_duck": ("FLOAT", {
                    "default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05,
                    "tooltip": "Lowers the music while someone speaks. 0 is off; 0.6 keeps speech clear.",
                }),
                "music_fit": (["loop", "once"], {
                    "default": "loop",
                    "tooltip": ("loop repeats the track to fill the chain; once plays it through and then "
                                "stops."),
                }),
                "music_fade_s": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 10.0, "step": 0.25,
                    "tooltip": "Fade in and out at the start and end of the music, in seconds.",
                }),
                # The picker's half of the soundtrack, and the one most people
                # use. Same shape as voice_file: a basename under h3_refs, set
                # in the panel. LAST, per the note at the top of this block.
                "soundtrack_file": ("STRING", {
                    "default": "",
                    "tooltip": "A music file for the soundtrack. Set it on the MEDIA tab.",
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
                    "tooltip": ("Where the voice sample starts, in seconds. Leave both at 0 to use its "
                                "first 8 s."),
                }),
                "voice_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where the voice sample ends, in seconds. 0 is the end of the file.",
                }),
                "reference_video_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where the reference clip starts, in seconds.",
                }),
                "reference_video_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where the reference clip ends, in seconds. 0 is the end.",
                }),
                "music_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where the music starts in the file, in seconds.",
                }),
                "music_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where the music ends in the file, in seconds. 0 is the end.",
                }),
                # Appended 2026-09-03 -- LAST, per the note at the top of this
                # block. `render_through` has been here since 0.4 and stops the
                # chain; this is the other end of the same range.
                "render_from": ("INT", {
                    "default": 0, "min": 0, "max": 64,
                    "tooltip": ("Start rendering at this hop; earlier hops load from the cache. 0 starts at "
                                "hop 1. Needs cache_hops on."),
                }),
                "reference_video_desc": ("STRING", {
                    "default": "",
                    "tooltip": ("What the reference clip is for, in a few words, e.g. \"a slow dolly along "
                                "the counter\". Without it the model may copy the clip outright."),
                }),
                # Appended 2026-09-03 -- LAST, per the note at the top of this
                # block.
                "reference_video_size": (list(_media.VIDEO_SIZES), {
                    "default": _media.DEFAULT_VIDEO_SIZE,
                    "tooltip": ("How large the reference clip is decoded. Lower saves memory and makes the "
                                "clip influence the result less, which helps when it fights your faces."),
                }),
                # APPENDED, never inserted. `widgets_values` is a bare ordered
                # array matched to this schema by index, so a widget added
                # anywhere but the end silently reassigns every later value in
                # every saved workflow. Adding options to an existing combo is
                # safe; adding a widget is not.
                "pin_mech": (["auto", "motion_context", "addguide", "hand_tie"], {
                    "default": "auto",
                    "tooltip": ("How hops are joined. auto uses Motion-Context when available, AddGuide "
                                "otherwise. Forcing one fails rather than falling back. hand_tie renders one "
                                "previs per section and ties every hop to it at both ends, so no hop reads "
                                "another and texture does not compound (needs a shot plan, overlap 0.9 s)."),
                }),
                # APPENDED, never inserted -- see the note above `pin_mech`.
                "tone_anchor_ref": (["hop1", "still"], {
                    "default": "hop1",
                    "tooltip": ("What anchor holds the chain to. hop1 is the first hop's look; still is the "
                                "start image, which needs start_image_file."),
                }),
                # APPENDED after pin_mech and tone_anchor_ref. Those two already
                # shipped on this trunk; the 3x3 extra slots go last so existing
                # v2 workflows keep their last two values as pin_mech /
                # tone_anchor_ref. H3 natively takes 9 reference images, 3
                # reference videos and 3 standalone reference audios; the pack
                # matched the 9 and passed exactly one of each of the others.
                "reference_video_2_file": ("STRING", {
                    "default": "",
                    "tooltip": "A second reference clip. Set it on the MEDIA tab.",
                }),
                "reference_video_2_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where reference clip 2 starts, in seconds.",
                }),
                "reference_video_2_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where reference clip 2 ends, in seconds. 0 is the end.",
                }),
                "reference_video_3_file": ("STRING", {
                    "default": "",
                    "tooltip": "A third reference clip. Set it on the MEDIA tab.",
                }),
                "reference_video_3_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where reference clip 3 starts, in seconds.",
                }),
                "reference_video_3_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where reference clip 3 ends, in seconds. 0 is the end.",
                }),
                "voice_2_file": ("STRING", {
                    "default": "",
                    "tooltip": "A second voice sample. Set it on the MEDIA tab.",
                }),
                "voice_2_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where voice 2 starts, in seconds. Leave both at 0 to use its first 8 s.",
                }),
                "voice_2_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where voice 2 ends, in seconds. 0 is the end.",
                }),
                "voice_3_file": ("STRING", {
                    "default": "",
                    "tooltip": "A third voice sample. Set it on the MEDIA tab.",
                }),
                "voice_3_start_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where voice 3 starts, in seconds. Leave both at 0 to use its first 8 s.",
                }),
                "voice_3_end_s": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 3600.0, "step": 0.1,
                    "tooltip": "Where voice 3 ends, in seconds. 0 is the end.",
                }),
                # APPENDED, never inserted. Empty string leaves every existing
                # path byte-identical: the lock is not called, the cache key
                # does not move, the delivered audio is still the generated
                # chain. The file is ElevenLabs (or any) TTS / a real take;
                # every hop lip-syncs to one continuous window of it.
                "master_audio_file": ("STRING", {
                    "default": "",
                    "tooltip": ("One recorded take for the whole chain; the picture lip-syncs to it and the "
                                "audio is used as-is. The beats still need the words. Empty is off."),
                }),
                "last_frame_guide": (["off", "before_restart", "still"], {
                    "default": "off",
                    "tooltip": ("Makes a hop end on the start image. before_restart does it only before a "
                                "restart, so the cut matches; still does it on every hop."),
                }),
                # APPENDED 2026-09-09, LAST for the same reason as everything
                # else down here: widgets_values is positional and a saved
                # workflow reads it by index.
                "voice_every_hop": (["off", "speaking", "on"], {
                    "default": "off",
                    "tooltip": ("Whether the voice samples ride after hop 1. speaking (recommended) keeps "
                                "them on hops with a spoken line; off is hop 1 only; on is every hop."),
                }),
                # APPENDED 2026-09-14 (the refine pass). Same rule again:
                # widgets_values is positional, so these go LAST and nothing
                # above them moves. Everything here ships inert -- hop_refine
                # defaults off and the hop key only carries these fields when
                # it is on, so a workflow saved before today keys byte-identical.
                "hop_refine": (["off", "full", "pin_only", "split"], {
                    "default": "off",
                    "tooltip": ("A light second pass on each hop to restore texture. full ships the refined "
                                "result; pin_only uses it only for the next hop's join; off skips it. "
                                "split is no second pass: the hop's last refine_steps steps are finished "
                                "in finer steps with no new noise, on refine_model / refine_sampler, and "
                                "that one latent is both shown and handed on. Not with realism_boost."),
                }),
                "refine_denoise": ("FLOAT", {
                    "default": 0.50, "min": 0.05, "max": 1.0, "step": 0.05,
                    "tooltip": "How much the second pass changes. 0.5 is the usual value.",
                }),
                "refine_steps": ("INT", {
                    "default": 2, "min": 0, "max": 40,
                    "tooltip": "Steps in the second pass. 2 is the usual value; 0 uses the hop's steps.",
                }),
                "refine_sampler": (["same"] + comfy.samplers.KSampler.SAMPLERS, {
                    "default": "same",
                    "tooltip": "Sampler for the second pass. same uses the hop's.",
                }),
                "refine_scheduler": (["same"] + comfy.samplers.KSampler.SCHEDULERS, {
                    "default": "simple",
                    "tooltip": "Scheduler for the second pass. same uses the hop's.",
                }),
                "refine_cond": (["base", "hop"], {
                    "default": "base",
                    "tooltip": ("What the second pass follows. base is the prompt and references; hop also "
                                "includes the join."),
                }),
                "refine_model": ("MODEL", {
                    "tooltip": ("Optional different model for the second pass, e.g. an undistilled base "
                                "under a turbo chain."),
                }),
                "refine_audio": (["freeze", "refine"], {
                    "default": "freeze",
                    "tooltip": ("freeze (recommended) leaves the sound untouched by the second pass; refine "
                                "lets it change the audio too."),
                }),
                "refine_blend": ("STRING", {
                    "default": _rblend.AUTO, "multiline": False,
                    "tooltip": ("Which frames use the second pass. auto keeps the start of each hop as "
                                "generated so joins stay clean. Empty uses the second pass everywhere."),
                }),
                "refine_blend_interp": (["linear", "smooth", "step"], {
                    "default": "linear",
                    "tooltip": "How refine_blend changes between its points. linear is the usual choice.",
                }),
                "refine_head": (["refine", "freeze"], {
                    "default": "refine",
                    "tooltip": ("Another way to keep joins clean: freeze leaves each hop's opening out of "
                                "the second pass. refine includes it."),
                }),
                # APPENDED 2026-09-15 (the speed preset). Last slot again, for
                # the same positional reason: nothing above it moves.
                "speed_mode": (["regular", "turbo"], {
                    "default": "regular",
                    "tooltip": ("Set turbo when a turbo model or LoRA is on the wire; nothing detects it. "
                                "It adjusts the second pass to suit, but a turbo chain still drifts more "
                                "at the joins. regular otherwise."),
                }),
                # APPENDED 2026-09-19, LAST for the same reason as everything
                # else down here: widgets_values is positional and a saved
                # workflow reads it by index.
                "master_audio_start_s": ("FLOAT", {
                    # 0.01, not the 0.1 the other trim widgets declare. The
                    # frontend derives its rounding from the step, so a 0.1 step
                    # quantises a stored 4.03 to 4.00 -- 1440 samples at 48 kHz,
                    # 30 ms, measured against a real render. On a two-grip trim
                    # that error lands on a reference clip's in-point and is
                    # inaudible; here it moves a locked take under a frozen
                    # mouth, which is the one place in this node where 30 ms of
                    # audio placement is a visible result. `round` is set
                    # explicitly rather than left to be derived, because that
                    # derivation is frontend behaviour and not a contract.
                    "default": 0.0, "min": 0.0, "max": 3600.0,
                    "step": 0.01, "round": 0.01,
                    "tooltip": "Where the chain starts inside master_audio_file, in seconds.",
                }),
                # APPENDED 2026-09-20, LAST, for the reason stated above:
                # inserting it beside the other refine_* widgets would have
                # renumbered refine_cond onward in every saved workflow.
                "refine_align": (["hop_tail", "denoise"], {
                    "default": "denoise",
                    "tooltip": ("Where the second pass lands on the schedule. hop_tail suits turbo models; "
                                "denoise is the original behaviour."),
                }),
                # APPENDED 2026-09-23, LAST, same positional reason.
                "realism_boost": (["off", "on"], {
                    "default": "off",
                    "tooltip": ("Two-stage sampling with the boost LoRAs for more realistic texture. It "
                                "doubles the steps: base 6 is 2 + 10. Not with master_audio_file."),
                }),
                "boost_lora_1": (_boost_lora_choices(), {
                    "default": _boost_lora_default(0, _boost_lora_choices()),
                    "tooltip": ("First boost LoRA. The recipe uses minimax_h3_lms_v1.0_r64 at 0.5. None "
                                "skips it."),
                }),
                "boost_lora_1_strength": ("FLOAT", {
                    "default": BOOST_LORA_DEFAULTS[0][1],
                    "min": -4.0, "max": 4.0, "step": 0.05,
                }),
                "boost_lora_2": (_boost_lora_choices(), {
                    "default": _boost_lora_default(1, _boost_lora_choices()),
                    "tooltip": ("Second boost LoRA. The recipe uses h3-realism-people-t2v-i2v-r2v at 1.0. "
                                "None skips it."),
                }),
                "boost_lora_2_strength": ("FLOAT", {
                    "default": BOOST_LORA_DEFAULTS[1][1],
                    "min": -4.0, "max": 4.0, "step": 0.05,
                }),
                # APPENDED 2026-09-28, LAST, same positional reason.
                "boost_lora_3": (_boost_lora_choices(), {
                    "default": "None",
                    "tooltip": ("Third boost LoRA, applied on top of the first two. Not in the recipe. "
                                "None skips it."),
                }),
                "boost_lora_3_strength": ("FLOAT", {
                    "default": BOOST_LORA_DEFAULTS[2][1],
                    "min": -4.0, "max": 4.0, "step": 0.05,
                }),
                # APPENDED 2026-09-29, LAST, same positional reason.
                "upsample": (["off", "on"], {
                    "default": "off",
                    "tooltip": ("After the last hop, make the finished film bigger and sharper: the H3 "
                                "latent upscaler, then one light re-draw, over the whole film at once. "
                                "Changes only the delivered video, never what a hop continues from. "
                                "Needs minimax_h3_latent_upscaler_3d_bf16.safetensors in "
                                "models/latent_upscale_models."),
                }),
                "upsample_megapixels": ("FLOAT", {
                    "default": _tu.MEGAPIXELS, "min": 0.1, "max": 16.0, "step": 0.05,
                    "tooltip": ("Output size for upsample, as area with the aspect kept "
                                "(1 MP = 1024x1024). 1.5-2 is the usual range."),
                }),
                # APPENDED 2026-09-30, LAST, same positional reason.
                "upsample_denoise": ("FLOAT", {
                    "default": UPSAMPLE_DENOISE, "min": 0.0, "max": 1.0, "step": 0.005,
                    "tooltip": ("How hard upsample re-draws after enlarging. 0.1 (sigma 0.57 under the "
                                "video shift) textures the least while still cleaning up the enlarge. "
                                "0.2 is the original recipe (sigma 0.75) and repaints skin texture; "
                                "below ~0.075 faces start to smear. 0 skips the re-draw."),
                }),
                # APPENDED 2026-09-30, LAST, same positional reason.
                "prompt_prefix": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": ("Text put at the very start of every hop's prompt, before anything the "
                                "chain adds. For LoRA trigger words, which work best first. Not "
                                "tag-resolved: write it as the encoder should read it."),
                }),
                "prompt_suffix": ("STRING", {
                    "default": "", "multiline": False,
                    "tooltip": ("Text put at the very end of every hop's prompt, after the chain's own "
                                "closing lines. For LoRAs that want a camera angle or similar last."),
                }),
                # APPENDED 2026-10-01, LAST, same positional reason.
                "upsample_model": (_tu.choices(), {
                    "default": _tu.UPSCALER_FILE,
                    "tooltip": ("The latent upscaler upsample enlarges with, from "
                                "models/latent_upscale_models. h3_upscaler_lms_v0.1 is the same network "
                                "fine-tuned on Alissonerdx's sharpness data: a sharper enlarge, which "
                                "needs less re-draw (try a lower upsample_denoise)."),
                }),
                # APPENDED 2026-10-01, LAST, same positional reason.
                "boost_lora_4": (_boost_lora_choices(), {
                    "default": "None",
                    "tooltip": ("Fourth boost LoRA, applied on top of the other three. Not in the "
                                "recipe. None skips it."),
                }),
                "boost_lora_4_strength": ("FLOAT", {
                    "default": 1.0, "min": -4.0, "max": 4.0, "step": 0.05,
                }),
                # APPENDED 2026-10-04, LAST, same positional reason.
                "upsample_lora": (_boost_lora_choices(), {
                    "default": "None",
                    "tooltip": ("A LoRA on the upsample re-draw only, on top of whatever model "
                                "re-draws (the boost model under realism_boost, else the MODEL "
                                "wire). Hops never see it. None skips it. An h3upscale "
                                "LoRA also gets its guide in that same step: the film "
                                "at half size, so the re-draw restores the film's own "
                                "detail instead of inventing texture."),
                }),
                "upsample_lora_strength": ("FLOAT", {
                    "default": 1.0, "min": -4.0, "max": 4.0, "step": 0.05,
                }),
                # The last widget (upsample_dit below is a socket and takes no
                # widgets_values slot). This slot held split_finish_aug
                # (2026-10-06, removed the same week: 0.85 burnt late hops in
                # chain_00101 and changed nothing at 0.999). A graph saved with
                # it carries 0.999 / 0.85 here, which ComfyUI casts to 0 for an
                # INT -- and 0 means the recipe value, so it still queues.
                "upsample_tile_tokens": ("INT", {
                    "default": _tu.TOKEN_BUDGET, "min": 0, "max": 1048576, "step": 1024,
                    "tooltip": ("Upsample VRAM: the largest sequence one tile forward may carry. "
                                "Lower = more, smaller tiles -- less memory, slower, the same "
                                f"seamless merge. {_tu.TOKEN_BUDGET} (or 0) is the recipe's "
                                "value. Delivery only: no hop re-renders when it changes."),
                }),
                # APPENDED 2026-10-04, LAST. A socket, so it takes no widget slot.
                "upsample_dit": ("MODEL", {
                    "tooltip": ("Optional model for the upsample re-draw instead of the boost "
                                "model / MODEL wire. Wire the plain UNETLoader output here "
                                "(before SLA and the LoRA stack): the h3upscale LoRA was "
                                "trained on the bare DiT. The chain applies its own sigma "
                                "shift to it; upsample_lora still goes on top."),
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
        "to the text encoder."
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
        refmod_names = []
        try:
            for r in (_refs.parse_ref_plan(ref_plan).get("refs") or []):
                if not r.get("file"):
                    continue
                if r.get("source") == "refmod":
                    refmod_names.append(r["file"])
                else:
                    names.append(r["file"])
                # A row voice is an input like any other file: swap the wav
                # in place and the render must change.
                if r.get("voice"):
                    names.append(r["voice"])
        except Exception:
            # A malformed plan is run()'s error to report, with a message that
            # names the field. Raising here would surface as a cache failure.
            pass
        stamped = _media.stamp(names)
        extra = _refmod.stamp(refmod_names)
        return stamped if not extra else f"{stamped}|{extra}"

    def run(self, model, clip, vae, audio_vae, prompt,
            chains, resolution,
            aspect, duration, overlap, seed, seed_per_shot, steps,
            sampler_name, scheduler, shift_video, shift_audio, ref_image_size,
            start_image_file="", reference_video_file="", voice_file="",
            hop_script="verbatim", pin_to_qwen="last frame",
            shot_plan="", ref_plan="", cache_hops="off", cache_budget_gb=20.0,
            audio_pin_frames=24, pin_renorm="off", pin_noise=0.0,
            tone_compensate="off", establish=None,
            render_through=0, quality="final", dry_run="off",
            contact_sheet="off", tone_anchor=_tone.ANCHOR_STRENGTH,
            music_gain_db=-14.0, music_duck=0.6,
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
            master_audio_file="", master_audio_start_s=0.0,
            last_frame_guide="off",
            voice_every_hop="off",
            hop_refine="off", refine_denoise=0.50, refine_steps=2,
            refine_sampler="same", refine_scheduler="simple",
            refine_align="denoise",
            refine_cond="base", refine_model=None, refine_audio="freeze",
            refine_blend=_rblend.AUTO, refine_blend_interp="linear",
            refine_head="refine", speed_mode="regular",
            realism_boost="off", boost_lora_1="None",
            boost_lora_1_strength=0.5, boost_lora_2="None",
            boost_lora_2_strength=1.0, boost_lora_3="None",
            boost_lora_3_strength=1.0,
            upsample="off", upsample_megapixels=_tu.MEGAPIXELS,
            upsample_denoise=UPSAMPLE_DENOISE, prompt_prefix="",
            prompt_suffix="", upsample_model=_tu.UPSCALER_FILE,
            boost_lora_4="None", boost_lora_4_strength=1.0,
            upsample_lora="None", upsample_lora_strength=1.0,
            upsample_tile_tokens=_tu.TOKEN_BUDGET, upsample_dit=None,
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
        # pin_mech=hand_tie: render each section's previs, output it, render no
        # hop. Everything a hop is, the previs is too, so this is the cheap look
        # at the one generate the whole section is tied to (chain_00135's previs
        # split between two kitchens and every hop followed it).
        previs_only = str(dry_run) == "previs"
        if previs_only and str(pin_mech) != "hand_tie":
            raise ValueError(
                f"{TAG}: dry_run=previs needs pin_mech=hand_tie: only hand-tie "
                "renders a previs.")
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
            if str(pin_mech) == "hand_tie":
                # Every hop is tied to the previs, not to the hop before it, so
                # every hop is a start: its own refs, its own subject prose, no
                # continuation airlock and no `join` sentence.
                # Each hop's own line is placed after its head window (time_lines),
                # before a crossing line is split across the join (split_crossing).
                _tl_frames = [align_frame_count(_duration_frames((s or {}).get("duration") or duration))
                              for s in shots]
                blocks = [_d.compile_shot(s, 0, establish)
                          for s in _ht.split_crossing(_ht.time_lines(shots, hop_frames=_tl_frames))]
                for _w in _ht.crossing_lints(shots):
                    print(f"[{TAG}] note: {_w}", flush=True)
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
        if str(pin_mech) == "hand_tie":
            if not shots:
                raise ValueError(
                    f"{TAG}: pin_mech=hand_tie needs a shot plan: its previs is "
                    "compiled from the shots (their beats, or `moment` where set). "
                    "Switch the editor to Shots, or use pin_mech=auto.")
            if str(master_audio_file or "").strip():
                raise ValueError(
                    f"{TAG}: pin_mech=hand_tie does not take master_audio_file yet: "
                    "it pins each window's previs sound. Clear master_audio_file, or "
                    "use pin_mech=auto.")
            _ht_errors = _ht.settings_errors(overlap_n, last_frame_guide, shots, lengths)
            if _ht_errors:
                raise ValueError(f"{TAG}: " + " ".join(_ht_errors))
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
        voice = (_media.load_voice(voice_file, start=float(voice_start_s),
                                   end=float(voice_end_s))[0]
                 if voice_file else None)
        # Empty string is off. The lock is not entered, the cache key does
        # not grow a new field, delivered audio stays generated. That is
        # the byte-identical claim.
        locked = (_prepare_master_audio(master_audio_file,
                                        master_audio_start_s)
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
            return (_media.load_voice(fname, start=float(t0), end=float(t1))[0]
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
                            ("soundtrack", soundtrack_file
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
        slot_videos = {}
        # id(presentation tensor) -> stored DiT block. Built once per run.
        # The hop that cites the row is the only one that receives either.
        refmod_image_blocks = {}
        refmod_video_blocks = {}
        for _r in ref_plan_refs:
            if not _r["file"]:
                continue
            if _r.get("source") == "refmod":
                if float(_r.get("strength") or 0.0) <= 0.0:
                    raise ValueError(
                        f"[{TAG}] @{_r['tag']} has strength 0, which drops the "
                        f"RefMod. Remove the row, or set a strength above 0.")
                _loaded = _refmod.load_visual(_r["file"])
                if _loaded is None:
                    continue
                # A video RefMod may be a subject. A character mod built from
                # several stills is stored as one clip, so refusing a subject
                # here refused the whole point of the builder. `subject_prose`
                # cites each subject at the slot it occupies, so this one is
                # defined as the person in <Video N>.
                _r["kind"] = _loaded["kind"]
                _r["latent_digest"] = _loaded["digest"]
                # A mod assembled in the builder carries the name of its
                # voice. The row inherits it, so a character is put
                # together once; a voice typed on the row still wins,
                # because the rail is the more specific statement.
                # The window comes with it, unless the row sets its own:
                # the row trim is the one on the rail, beside the waveform.
                if not _r.get("voice"):
                    _inherited = _refmod.stored_voice(_r["file"])
                    if _inherited:
                        _r["voice"] = _inherited["file"]
                        if not (_r.get("voice_start") or _r.get("voice_end")):
                            _r["voice_start"] = _inherited["start"]
                            _r["voice_end"] = _inherited["end"]
                        print(f"[{TAG}] @{_r['tag']}: voice {_inherited['file']} "
                              f"from the RefMod", flush=True)
                _block = _refmod.visual_block(_loaded, _r["strength"])
                # The kept photographs when the builder stored them, so Qwen
                # reads a face at the resolution it was photographed, not a
                # VAE decode of it. Keyed per hop through `looks_digest`.
                _looks = _loaded.get("looks")
                _r["looks_digest"] = _loaded.get("looks_digest")
                _pixels = _refmod.presentation_pixels(
                    vae, _loaded["latent"], _r.get("mp") or 0.0,
                    looks=_looks, kind=_loaded["kind"])
                if _loaded["kind"] == "image":
                    _pixels = _pixels[:1]
                    slot_images[_r["slot"]] = _pixels
                    refmod_image_blocks[id(_pixels)] = _block
                else:
                    if int(_pixels.shape[0]) < 5:
                        raise ValueError(
                            f"[{TAG}] @{_r['tag']} decodes to "
                            f"{int(_pixels.shape[0])} frames. A video reference "
                            f"needs at least 5. Re-extract it with more latent "
                            f"frames, or save it as an image mod.")
                    slot_videos[_r["slot"]] = _pixels
                    refmod_video_blocks[id(_pixels)] = _block
                _view = (f"Qwen sees {int(_looks.shape[0])} kept photo"
                         f"{'s' if int(_looks.shape[0]) != 1 else ''}"
                         if _looks is not None else
                         "Qwen sees a decode (no kept photos; rebuild to keep them)")
                print(f"[{TAG}] @{_r['tag']}: RefMod {_loaded['kind']} "
                      f"{_loaded['tokens']} tokens, strength {_r['strength']:g}, "
                      f"{_view}", flush=True)
                continue
            _r["kind"] = "image"
            _im = _media.load_image(_r["file"], cap_mp=_r.get("mp") or 0.0)
            if _im is not None and _im.shape[0] > 0:
                slot_images[_r["slot"]] = _im[:1]
        _wired_slots = set(slot_images) | set(slot_videos)

        # Every @tag, checked against the hop it is written on BEFORE hop 1
        # samples. resolve_tags fails a place/prop tag whose picture is off
        # that hop, but it runs per hop inside the loop -- so a stray @bedroom
        # in shot 2 surfaced only after hop 1 had rendered (2026-10-01). Same
        # routing as the loop: shot.refs wins, else the register default with
        # unscheduled refs off continuations under `next`. Person tags always
        # resolve (to a name or <Subject N>), so only the rest are checked.
        if ref_plan_refs:
            _person = _refs.subjects(ref_plan_refs)
            _declared = {r["tag"] for r in ref_plan_refs}
            _problems = []
            for _i, _blk in enumerate(blocks):
                _sh = shots[_i] if _i < len(shots) else None
                _sh = _sh or {}
                _start = (_i == 0 or str(_sh.get("anchor") or "") == "restart"
                          or str(pin_mech) == "hand_tie")
                if _sh.get("refs") is not None:
                    _on = _refs.select_for_shot(ref_plan_refs, _sh["refs"], _wired_slots)
                else:
                    _on = _refs.active_refs(ref_plan_refs, _i, _wired_slots)
                    if not _start and str(hop_script) == "next":
                        _on = [r for r in _on if r.get("shots") is not None]
                _here = {r["tag"] for r in _on}
                for _t in sorted(set(re.findall(r"@([A-Za-z0-9_]+)", str(_blk or "")))):
                    if _t in _person or _t in _here or _t not in _declared:
                        continue
                    _problems.append(f"shot {_i + 1}: @{_t}")
            if _problems:
                raise ValueError(
                    f"{TAG}: a place or prop tag is written on a hop its picture "
                    f"does not ride -- {', '.join(_problems)}. Write the place in "
                    f"plain words on that shot, or add the shot number to that "
                    f"reference's `shots` on the REFS tab. Nothing was rendered.")

        # Row voices. A second pass rather than a branch inside the loop
        # above, because the refmod arm returns early and a voice belongs to
        # both arms equally -- a face is a face whether it arrived as a still
        # or as a mod.
        slot_voices = {}
        for _r in ref_plan_refs:
            _vf = str(_r.get("voice") or "")
            if not _vf or _r["slot"] not in _wired_slots:
                continue
            _wav, _vauto = _media.load_voice(
                _vf, start=float(_r.get("voice_start") or 0.0),
                end=float(_r.get("voice_end") or 0.0), kinds=("audio", "video"))
            if _wav is None:
                # Fatal, like a missing picture: a row voice is the thing
                # that says which face speaks, and rendering the chain with
                # it silently absent is the failure the register exists to
                # prevent.
                raise ValueError(
                    f"[{TAG}] @{_r['tag']} names voice '{_vf}' "
                    f"(ComfyUI/input/h3_refs), which could not be read. Drop "
                    f"the file onto that row, or clear the row's voice.")
            slot_voices[_r["slot"]] = _wav
            # The trimmed length, since that is what core encodes whole. The
            # hop key needs nothing new: it digests this waveform, so a
            # window moves the key and an untrimmed voice keeps its old one.
            _vsec = _wav["waveform"].shape[-1] / float(_wav["sample_rate"])
            _vwin = _refs.window_label(_r.get("voice_start"), _r.get("voice_end"))
            if _vwin:
                _vhow = f" {_vwin}"
            elif _vauto and _vsec >= _media.VOICE_DEFAULT_S - 0.05:
                _vhow = (f" (first {_media.VOICE_DEFAULT_S:g}s by default; "
                         f"trim the row's voice to choose another sentence)")
            else:
                _vhow = " (whole file)"
            print(f"[{TAG}] @{_r['tag']}: voice {_vsec:.1f}s{_vhow}", flush=True)
            if _vsec > _refs.VOICE_LONG_S:
                print(f"[{TAG}] WARNING: @{_r['tag']}'s voice runs {_vsec:.1f}s, "
                      f"past the {_refs.VOICE_LONG_S:g}s H3 reference clips are "
                      f"trained on. Core encodes it whole, and a hop whose "
                      f"lines run out replays it in the frames left over. "
                      f"Trim it to one clean sentence on the row's voice "
                      f"(or in the RefMod's voice slot).", flush=True)
        if ref_plan_refs:
            print(f"[{TAG}] reference register:", flush=True)
            print(_refs.describe(ref_plan_obj), flush=True)
            # A named picture that is not on disk stops the queue. Warning and
            # continuing renders the whole chain with that reference silently
            # inactive, which is the failure the register exists to prevent --
            # and the docs have always promised a stop here.
            _absent = _refs.missing_files(ref_plan_obj, _wired_slots)
            if _absent:
                _bits = []
                for _t, _f in _absent:
                    _row = next((r for r in ref_plan_refs if r["tag"] == _t), {})
                    _where = ("models/refmods" if _row.get("source") == "refmod"
                              else "ComfyUI/input/h3_refs")
                    _bits.append(f"@{_t} names '{_f}' ({_where})")
                raise ValueError(
                    f"[{TAG}] reference not found: " + "; ".join(_bits)
                    + ". Drop the file onto that row in the REFERENCES rail, or "
                      "clear the row to render without it.")
            _refs.check_kind_caps(
                [r for r in ref_plan_refs if r["slot"] in _wired_slots],
                media_videos=sum(1 for _v, _a in _vid_slots if _v is not None))

            _refs.check_audio_caps(
                [r for r in ref_plan_refs if r["slot"] in _wired_slots],
                media_voices=sum(1 for _v in (voice, voice_2, voice_3)
                                 if _v is not None))
            for _w in _refs.check(ref_plan_obj, _wired_slots):
                print(f"[{TAG}] note: {_w}", flush=True)
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
        vae_fp = None if dry else _vae_fingerprint(vae)
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
                    "refine_align": str(refine_align),
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
                refine_align = _sv["refine_align"]
                for _f, _was, _now in _smoved:
                    print(f"[{TAG}]   {_speed} sets {_f}={_now} "
                          f"(widget read {_was})", flush=True)
                if not _smoved:
                    print(f"[{TAG}]   the widgets already match the {_speed} "
                          f"row", flush=True)

        # `auto` becomes a literal here -- before parse and before the hop key,
        # so the key stores the ramp that ran and `auto` at a 22 f overlap keys
        # exactly as the published string does.
        _blend_was = str(refine_blend)
        refine_blend = _rblend.resolve(refine_blend, overlap_n)
        if str(refine_blend) != _blend_was and str(hop_refine) not in ("off", "split"):
            print(f"[{TAG}] refine_blend=auto -> {refine_blend!r} "
                  f"(raw across the {overlap_n} f pin, crossing over the "
                  f"{_rblend.CROSS_FRAMES} f after it)", flush=True)
        # Parsed up front so a malformed ramp fails on the queue rather than
        # two minutes into hop 1, and so a dry run catches it too.
        try:
            refine_keys = (_rblend.parse(refine_blend)
                           if str(hop_refine) not in ("off", "split") else [])
        except ValueError as e:
            raise RuntimeError(f"{TAG}: {e}") from e

        # Realism boost. Refused beside the audio lock, whose frozen stream
        # would reach stage 3 un-noised under DisableNoise. Allowed beside
        # hop_refine: the refine runs on whatever the boost hands out, both
        # are in the hop key, and a long boosted chain is the case the refine
        # was built for. It refines on the MODEL wire (or refine_model), not
        # the boost model, so it can pull the boost's look back -- said once.
        boost_on = str(realism_boost) == "on"
        boost_loras = [(str(boost_lora_1), float(boost_lora_1_strength)),
                       (str(boost_lora_2), float(boost_lora_2_strength)),
                       (str(boost_lora_3), float(boost_lora_3_strength))]
        # Slot 4 joins the list only when it does something, so a boosted hop
        # keyed before the slot existed keeps its key (the hop key hashes this
        # list) and an unset slot 4 costs no cached hop.
        if str(boost_lora_4) != "None" and float(boost_lora_4_strength) != 0.0:
            boost_loras.append((str(boost_lora_4), float(boost_lora_4_strength)))
        split_on = str(hop_refine) == "split"
        if split_on:
            if boost_on:
                raise RuntimeError(
                    f"{TAG}: hop_refine=split does not run with realism_boost: "
                    "the boost already splits each hop and finishes it on the boost "
                    "LoRAs. Pick one -- realism_boost with hop_refine full/pin_only, "
                    "or split without the boost.")
            _k_note = (f"{int(refine_steps)}" if int(refine_steps)
                       else f"{SPLIT_DEFAULT_K} (refine_steps 0)")
            print(f"[{TAG}] hop_refine=split: each hop's last {_k_note} steps are "
                  f"finished in {SPLIT_EXTEND}, with no new noise, on "
                  + ("the wired refine_model" if refine_model is not None
                     else "the MODEL wire")
                  + f" with {'the hop sampler' if str(refine_sampler) == 'same' else refine_sampler}"
                  "; the hop's conditioning carries the pin through both stages. "
                  "refine_denoise / refine_scheduler / refine_cond / refine_audio / "
                  "refine_blend / refine_head / refine_align do not apply.", flush=True)
        if boost_on:
            if str(hop_refine) != "off":
                print(f"[{TAG}] realism_boost + hop_refine={hop_refine}: each "
                      "hop is boosted, then refined on "
                      + ("the wired refine_model" if refine_model is not None
                         else "the MODEL wire (not the boost LoRAs)")
                      + ".", flush=True)
            if str(master_audio_file or "").strip():
                raise RuntimeError(
                    f"{TAG}: realism_boost does not run with master_audio_file: "
                    "the locked audio would enter the boost stage un-noised.")
            if all(nm == "None" or s == 0.0 for nm, s in boost_loras):
                raise RuntimeError(
                    f"{TAG}: realism_boost is on with no boost LoRA set -- "
                    "pick at least one of boost_lora_1..4.")

        # Upsample: the tiled upsampler's recipe once over the finished film,
        # after the last hop. Refused NOW if the upscaler file is missing,
        # rather than after every hop has rendered.
        upsample_on = str(upsample) == "on" and not dry and not previs_only
        upsample_hops = []
        upsample_guide_factor = 0
        if upsample_on:
            import folder_paths  # noqa: PLC0415
            _tu.register_folder()
            if not folder_paths.get_full_path(_tu.FOLDER, str(upsample_model)):
                raise RuntimeError(
                    f"{TAG}: upsample is on but {upsample_model} is not in "
                    f"models/{_tu.FOLDER} "
                    "(huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler).")
            _uw, _uh = _tu.target_size(int(width), int(height),
                                       float(upsample_megapixels))
            print(f"[{TAG}] upsample on: after the last hop the film goes "
                  f"{int(width)}x{int(height)} -> {_uw}x{_uh} "
                  f"({float(upsample_megapixels):g} MP) with {upsample_model}. "
                  "Delivery only; no hop's pin changes.", flush=True)
            _ud = float(upsample_denoise)
            if _ud <= 0.0:
                print(f"[{TAG}] upsample_denoise 0: no re-draw, the enlarged "
                      "latent is decoded as is", flush=True)
                if upsample_dit is not None:
                    print(f"[{TAG}] upsample_dit is wired but upsample_denoise is "
                          "0: there is no re-draw for it to run", flush=True)
            if str(upsample_lora) != "None":
                if not folder_paths.get_full_path("loras", str(upsample_lora)):
                    raise RuntimeError(f"{TAG}: upsample_lora {upsample_lora} is not "
                                       "in models/loras.")
                if _ud <= 0.0:
                    print(f"[{TAG}] upsample_lora is set but upsample_denoise is 0: "
                          "there is no re-draw for it to act on", flush=True)
                # An h3upscale LoRA (a guide LoRA, by its header) gets its guide
                # inside the same re-draw step. Read here so a LoRA with guide
                # geometry this pack cannot pack fails before any hop renders.
                upsample_guide_factor = _ug.lora_factor(
                    folder_paths.get_full_path("loras", str(upsample_lora)))
                if upsample_guide_factor and _ud > 0.0:
                    _gw, _gh = _tu.target_size(int(width), int(height),
                                               float(upsample_megapixels),
                                               align=32 * upsample_guide_factor)
                    print(f"[{TAG}] upsample: {upsample_lora} is an h3upscale guide "
                          f"LoRA (factor {upsample_guide_factor}); the re-draw follows "
                          f"the film at {_gw // upsample_guide_factor}x"
                          f"{_gh // upsample_guide_factor} under the LoRA's caption, "
                          f"output {_gw}x{_gh}", flush=True)

        sampler = base_sigmas = None
        split_model = None
        boost_model = None
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
            # split's finish model: refine_model shifted like the hops, so the
            # finish steps land on the same timesteps the sigmas name (shifting
            # an already-shifted model is a no-op), else the MODEL wire.
            split_model = model
            if split_on and refine_model is not None:
                split_model = _result(_core_call(
                    MiniMaxH3SigmaShift, "the sigma shift (split refine_model)",
                    model=refine_model, shift_video=float(shift_video),
                    shift_audio=float(shift_audio)))[0]
            base_sigmas = _result(_core_call(
                BasicScheduler, "the sigma schedule",
                model=model, scheduler=scheduler, steps=int(steps),
                denoise=1.0))[0]
            sigma_cache = {int(steps): base_sigmas}
            if boost_on:
                boost_model = _boost_model(model, boost_loras)
                print(f"[{TAG}] realism boost: "
                      + ", ".join(f"{nm} {s:g}" for nm, s in boost_loras
                                  if nm != "None" and s != 0.0), flush=True)

        # The steps each hop will really run. A shot's own `steps` outranks the
        # widget, and printing only the widget made a plan pinned to 8 look
        # like it was obeying a widget set to 2 -- and the boost split with it.
        _hop_steps = [int((sh or {}).get("steps") or steps) for sh in shots]
        _steps_txt = (f"{_hop_steps[0]} steps" if len(set(_hop_steps)) == 1
                      else "steps " + "/".join(str(v) for v in _hop_steps))
        if _hop_steps and all((sh or {}).get("steps") for sh in shots) \
                and set(_hop_steps) != {int(steps)}:
            _steps_txt += f" (set per shot; the steps widget's {int(steps)} is unused)"
        if boost_on:
            _steps_txt += " -> realism boost " + ", ".join(sorted(
                {f"{BOOST_SPLIT} + {BOOST_EXTEND * v - BOOST_SPLIT}" for v in _hop_steps}))
        print(
            f"[{TAG}] {n} hop(s), {length}f ({length / FPS:.1f}s) @ {width}x{height} "
            f"({resolution}, {aspect}), overlap {overlap_n}f, "
            f"hop_script={hop_script}, pin_to_qwen={pin_to_qwen}, "
            f"{unique} authored block(s), {_steps_txt} {sampler_name}/{scheduler}",
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
            off = float(locked.get("offset") or 0.0)
            full_s = float(locked.get("full_s") or take_s)
            _from = f" from {off:.2f}s" if off > 0.0 else ""
            if take_s + 1.0 / FPS < need_s:
                # Name the latest start that WOULD fit. "Pad the recording" is
                # the only advice this could give before the window existed,
                # and it is the wrong one when the file is long enough and the
                # window is simply too far in.
                latest = full_s - need_s
                fix = (f"Move master_audio_start_s to {latest:.2f}s or "
                       f"earlier, shorten the chain, or pad the recording."
                       if latest >= 0.0 else
                       f"Shorten the chain, or pad the recording to at least "
                       f"{need_s:.2f}s.")
                raise ValueError(
                    f"{TAG}: master_audio_file gives {take_s:.2f}s{_from} "
                    f"but this chain is {need_s:.2f}s ({total_frames}f at "
                    f"{FPS:g} fps). The last {need_s - take_s:.2f}s would be "
                    f"locked to silence the take does not contain. {fix}")
            if take_s > need_s + 1.0:
                print(f"[{TAG}] master_audio_file leaves {take_s:.2f}s{_from} "
                      f"for a {need_s:.2f}s chain; the last "
                      f"{take_s - need_s:.2f}s is not used", flush=True)

        # A dry run must not allocate the master. At 8 x 15 s and 1280x736 that
        # is 2742 full float frames -- ~31 GB -- for a feature whose entire
        # point is that it costs seconds.
        master_imgs = (None if (dry or previs_only)
                       else _alloc_master(total_frames, height, width))
        write_pos = 0
        ht_hop_start = 0           # hand-tie: where the last written hop starts
        ht_section_starts = []     # hand-tie: where each section starts, for deflicker
        ht_cuts = []               # hand-tie: where each tied join landed, for detail_settle
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
        # Which subjects the relay is actually carrying. Not the register:
        # see the continuity_line call site.
        prev_subjects = set()
        pbar = comfy.utils.ProgressBar(n)

        hop_store = None
        # "on" is what pre-0.5 workflows saved for what is now "sigma".
        # `sigma` (saved as `on` before 0.5) matched total spread, which falls
        # across a chain whose picture is baking, so it corrected the wrong
        # way. Removed from the menu; the editor migrates old graphs to off.
        pin_renorm_mode = "band" if str(pin_renorm) == "band" else "off"
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
                  f"(budget {float(cache_budget_gb):.0f} GB, model {model_fp}, "
                  f"vae {vae_fp})",
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
            # Cached hops are decoded frames, so the decoder is part of the
            # hop. See _vae_fingerprint.
            "vae": vae_fp,
        }
        if locked is not None:
            # Only present when the lock is on. Adding a None field while
            # off would move every existing cache key and break the
            # empty-string byte-identical claim.
            chain_salt["master_audio"] = locked["digest"]
        prev_key = None
        hop_keys = []
        # Hops served by lock pointer against a different predecessor than
        # they were generated with; see the locked branch below.
        unpaired_joins = []
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

        # pin_mech=hand_tie: one previs per section, compiled here, rendered at
        # the section's first hop (it needs that hop's refs and assembly), cut
        # into 22-frame windows that guide every hop of the section at both ends.
        # See hand_tie.py.
        ht = str(pin_mech) == "hand_tie"
        ht_pos = {}          # hop index -> (section, position in it)
        ht_layouts = []
        ht_previs_raw = []
        ht_windows = {}      # section -> (window pictures, window sounds)
        ht_previs_info = []
        ht_previs_media = []
        ht_previs_warn = []     # (section, seed, cuts) of a previs that cut on every try
        if ht:
            for _w in _ht.line_lints(shots, lengths):
                print(f"[{TAG}] note: {_w}", flush=True)
            ht_chained = []      # section -> continues the hop before it
            ht_speaks = []       # section -> [window carries a planned line]
            for _si, _sec in enumerate(_ht.sections(shots, lengths)):
                ht_chained.append(_ht.chained(shots, _sec))
                _frames = [lengths[j] for j in _sec]
                _text, _lay = _ht.compile_previs([shots[j] for j in _sec], _frames, FPS)
                _first, _last = shots[_sec[0]] or {}, shots[_sec[-1]] or {}
                _dirs = {a: v for a, v in (_first.get("directives") or {}).items()
                         if a in ("camera", "framing", "pace") and v}
                _tail = (_last.get("directives") or {}).get("tail")
                if _tail:
                    _dirs["tail"] = _tail
                ht_previs_raw.append(_d.compile_shot({"beat": _text, "directives": _dirs},
                                                     0, establish))
                ht_layouts.append(_lay)
                ht_speaks.append(_ht.window_speaks([shots[j] for j in _sec], len(_lay["windows"])))
                for _k, _j in enumerate(_sec):
                    ht_pos[_j] = (_si, _k)
                print(f"[{TAG}] hand-tie section {_si + 1}: hops {_sec[0] + 1}-{_sec[-1] + 1}, "
                      f"previs {_lay['previs']}f, windows at {_lay['windows']}"
                      + (f", continuing hop {_sec[0]}'s last frames" if ht_chained[-1] else ""),
                      flush=True)
            if tone_on or anchor_on:
                print(f"[{TAG}] hand-tie: tone_compensate={tone_mode} is replaced by the "
                      f"hand-tie tone match (each hop pulled onto its windows)", flush=True)

        def _ht_previs(i):
            """Render (or load) the hand-tie previs for hop i's section, and cut it
            into windows. Called at the section's first hop, after that hop's
            prompt is assembled: the previs carries the same references, subject
            prose, voices and prefix/suffix, so it is assembled the same way,
            from those same values (read here at call time)."""
            _si = ht_pos[i][0]
            _lay = ht_layouts[_si]
            pblock = ht_previs_raw[_si]
            # A chained section continues the film: its previs is pinned at frame 0
            # on the last 22 frames hop i-1 actually rendered (picture and sound),
            # and those frames ARE its first window, so the join between sections
            # is an ordinary hand-tie join. Nothing earlier moves.
            _chain = ht_chained[_si]
            if _chain and prev_imgs is None:
                if not previs_only:
                    raise RuntimeError(f"{TAG}: hand-tie section {_si + 1} continues hop {i}, "
                                       "which has no frames yet.")
                print(f"[{TAG}] hand-tie previs {_si + 1}: continues hop {i}'s frames, which "
                      "dry_run=previs does not render; skipped", flush=True)
                ht_windows[_si] = None
                return
            _from = ({"frames": _store.tensor_digest(prev_imgs),
                      "sound": _store.audio_digest(prev_audio)} if _chain else None)
            if ref_plan_refs:
                pblock = _refs.resolve_tags(
                    pblock, hop_ords, _refs.subjects(ref_plan_refs),
                    where=f"hand-tie previs {_si + 1}",
                    declared={r["tag"] for r in ref_plan_refs},
                    subject_names=None, video_map=video_ords)
            if hop_subject_prose and not _d.is_full_h3_prompt(pblock):
                pblock = hop_subject_prose + "\n\n" + pblock
            if refvid_line and not _d.is_full_h3_prompt(pblock):
                pblock = pblock.rstrip() + "\n\n" + refvid_line
            if _voice_ord and f"<Audio {_voice_ord}>" not in pblock:
                pblock = (pblock.rstrip() + f"\n\nThe speaker's voice follows "
                          f"<Audio {_voice_ord}> as a timbre reference.")
            if _bindings:
                _pst = ([_m.start() for _m in re.finditer(r"<d>", pblock)]
                        + [_a for _a, _b in _planner.spoken_spans(pblock)])
                pblock = _refs.bind_voices(pblock, _bindings, _pst)
            if _pre:
                pblock = _pre + " " + pblock.lstrip()
            if _suf:
                pblock = pblock.rstrip() + " " + _suf

            _sh = shots[i] or {}
            # The run seed, never the shot's own: a shot's `seed` is how one hop
            # is retaken, and reading it here re-rolled the previs, so every hop
            # of the section, with it (00164: hop 1 could not be retaken alone).
            _p_seed0 = (((int(seed) + i) if seed_per_shot else int(seed))
                        + _ht.PREVIS_SEED_OFFSET)
            _p_steps = int(_sh.get("steps") or steps)
            _p_len = int(_lay["previs"])
            # The previs is the one thing every hop follows, so it is checked
            # before any hop renders: a hard cut in it (chain_00146: medium to
            # wide at 13.08 s) re-renders it on the next seed, PREVIS_TRIES at
            # most, and the least-cut render is kept. Nobody dry-runs a previs
            # first, so the node has to.
            _pc = _pl = None
            _best = None
            _last_cuts = []
            for _try in range(_ht.PREVIS_TRIES):
                _p_seed = _p_seed0 + _try * _ht.PREVIS_RETRY_STRIDE
                _p_key = None
                _hit = None
                if hop_store is not None:
                    _p_key = _store.hop_key(None, {
                        "chain": chain_salt, "previs": pblock, "len": _p_len,
                        "seed": _p_seed, "steps": _p_steps,
                        "refs": {k: _store.tensor_digest(t)
                                 for k, t in sorted((hop_images or {}).items())},
                        "videos": {k: _store.tensor_digest(t)
                                   for k, t in sorted((hop_videos or {}).items())},
                        "audios": {k: _store.audio_digest(a)
                                   for k, a in sorted((hop_ref_audios or {}).items())},
                        "start": _store.tensor_digest(start_image),
                        **({"from": _from} if _from else {}),
                    })
                    _hit = hop_store.get(_p_key)
                if _hit is not None:
                    _p_imgs, _p_wav, _p_sr, _ = _hit
                    print(f"[{TAG}] hand-tie previs {_si + 1}: loaded from cache "
                          f"({int(_p_imgs.shape[0])}f, seed {_p_seed}, key {_p_key[:8]})",
                          flush=True)
                else:
                    print(f"[{TAG}] hand-tie previs {_si + 1}: rendering {_p_len}f "
                          f"(seed {_p_seed}, {_p_steps} steps)", flush=True)
                    _push_preview(unique_id, f"previs {_si + 1} sampling…", hop=i + 1, total=n)
                    if _pc is None:
                        packed = _core_call(
                            MiniMaxH3ReferenceToVideo, "the hand-tie previs",
                            clip=clip, vae=vae, audio_vae=audio_vae, prompt=pblock,
                            width=int(width), height=int(height), length=_p_len,
                            ref_image_size=ref_image_size, ref_images=hop_images,
                            ref_videos=hop_videos,
                            ref_video_audios=(hop_video_audios or None),
                            ref_audios=hop_ref_audios,
                        )
                        _pc, _pl = _result(packed)[0], _result(packed)[1]
                        del packed
                        if refmod_image_blocks or refmod_video_blocks:
                            _pc = _refmod.splice_blocks(
                                _pc, hop_images, hop_videos,
                                refmod_image_blocks, refmod_video_blocks)
                        if start_image is not None and (i == 0 or str(_sh.get("anchor") or "") == "restart"):
                            # The section opens on the photograph, so window A does.
                            _pc = _result(_core_call(
                                MiniMaxH3AddGuide, "the previs start image",
                                positive=_pc, latent=_pl, frame_idx=0,
                                vae=vae, audio_vae=None, image=start_image[:1], audio=None,
                            ))[0]
                        if _chain:
                            _pc = _result(_core_call(
                                MiniMaxH3AddGuide, "the chained previs pin",
                                positive=_pc, latent=_pl, frame_idx=0,
                                vae=vae, audio_vae=audio_vae,
                                image=prev_imgs, audio=prev_audio,
                            ))[0]
                        _offload_text_encoder(clip, model)
                    if _p_steps not in sigma_cache:
                        sigma_cache[_p_steps] = _result(_core_call(
                            BasicScheduler, "the sigma schedule",
                            model=model, scheduler=scheduler, steps=_p_steps,
                            denoise=1.0))[0]
                    _pg = _result(_core_call(BasicGuider, "the previs guider",
                                             model=model, conditioning=_pc))[0]
                    _pn = _result(_core_call(RandomNoise, "the previs noise",
                                             noise_seed=_p_seed))[0]
                    _ps = _result(_core_call(
                        SamplerCustomAdvanced, "the previs sampler",
                        noise=_pn, guider=_pg, sampler=sampler,
                        sigmas=sigma_cache[_p_steps], latent_image=_pl))[0]
                    _p_imgs, _p_audio = _decode_av(vae, audio_vae, _ps)
                    _p_imgs = _p_imgs.contiguous().cpu()
                    _p_wav = _p_audio["waveform"].contiguous().cpu()
                    _p_sr = int(_p_audio["sample_rate"])
                    del _pg, _pn, _ps
                    mm.soft_empty_cache()
                    if hop_store is not None:
                        hop_store.put(_p_key, _p_imgs, _p_wav, _p_sr,
                                      {"previs": _si + 1, "block": pblock[:400]})
                        # The hops key on these frames' digests, so they must
                        # be the bytes a cache hit returns.
                        _p_imgs, _p_wav = _store.as_stored(_p_imgs, _p_wav)
                # Every cut earns a new take: a hop copies one between its windows
                # about half the time (chain_00154's hop 1, from f66). A cut inside
                # a window is worse, so it ranks the takes when all of them cut.
                _cuts = _ht.previs_cuts(_p_imgs[:_p_len])
                _rank = (len(_ht.window_cuts(_cuts, _lay["windows"])), len(_cuts))
                if _best is None or _rank < _best[5]:
                    _best = (_p_imgs, _p_wav, _p_sr, _cuts, _p_seed, _rank)
                if not _cuts:
                    break
                _free = [c for c in _cuts if c not in _ht.moment_cuts(_cuts, _lay["windows"])]
                if not _free:
                    # Every cut is on a moment boundary: the text's, which a new
                    # seed only moves a few frames. The hops check themselves.
                    print(f"[{TAG}] hand-tie previs {_si + 1}: seed {_p_seed} cuts at "
                          + ", ".join(f"f{c}" for c in _cuts) + ", each on a moment "
                          "boundary (the text's); keeping it, each hop checks its own cut",
                          flush=True)
                    break
                _again = _ht.same_cut(_last_cuts, _cuts)
                _last_cuts = _cuts
                print(f"[{TAG}] hand-tie previs {_si + 1}: seed {_p_seed} cuts at "
                      + ", ".join(f"f{c} ({c / FPS:.2f}s)" for c in _cuts)
                      + ("; the same place as the last take, so the text asks for it: "
                         "no more takes" if _again
                         else "; rendering it again on the next seed"
                         if _try + 1 < _ht.PREVIS_TRIES else ""), flush=True)
                if _again:
                    break
            del _pc, _pl
            _p_imgs, _p_wav, _p_sr, _cuts, _p_seed = _best[:5]
            if [c for c in _cuts if c not in _ht.moment_cuts(_cuts, _lay["windows"])]:
                # A cut that survives every take is usually the text's: a beat that
                # moves to a new place. The hop there may carry it as a camera move
                # (00150's hop 5) or copy it (00154's hop 1).
                print(f"[{TAG}] WARNING: hand-tie previs {_si + 1}: using seed {_p_seed}, "
                      "which cuts at " + ", ".join(f"f{c}" for c in _cuts) + ". A beat that "
                      "moves to a new place asks for a cut; check the film there.", flush=True)
                ht_previs_warn.append((_si + 1, _p_seed, _cuts))
            elif _p_seed != _p_seed0:
                print(f"[{TAG}] hand-tie previs {_si + 1}: seed {_p_seed} holds one take",
                      flush=True)
            if int(_p_imgs.shape[0]) < _p_len:
                raise ValueError(
                    f"{TAG}: hand-tie previs {_si + 1} decoded {int(_p_imgs.shape[0])} "
                    f"frames, {_p_len} planned.")
            pics, snds = [], []
            for _wk, w in enumerate(_lay["windows"]):
                pics.append(_p_imgs[w:w + _ht.WINDOW].clone())
                if not ht_speaks[_si][_wk]:
                    # Picture only: the previs's sound here is a line nobody wrote.
                    snds.append(None)
                    continue
                _a0, _ad = _ht.window_audio(w)
                _s0 = int(round(_a0 * _p_sr))
                _s1 = _s0 + int(round(_ad * _p_sr))
                snds.append({"waveform": _p_wav[..., _s0:_s1].contiguous(),
                             "sample_rate": _p_sr})
            if _chain:
                # Window 0 is the real thing, not the previs's copy of it: hop i
                # then opens on exactly the frames the film has before the join.
                pics[0] = prev_imgs[-_ht.WINDOW:].clone()
                snds[0] = {"waveform": prev_audio["waveform"].clone(),
                           "sample_rate": int(prev_audio["sample_rate"])}
            ht_windows[_si] = (pics, snds)
            ht_previs_info.append((_si + 1, _p_len, _lay["windows"], pblock))
            if previs_only:
                ht_previs_media.append((_p_imgs, _p_wav, _p_sr))

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
            # Whether the master drops this hop's first `overlap` frames. Equal
            # to `not hop_is_start` except under hand_tie, where every hop is
            # ASSEMBLED as a start (it reads no other hop) yet still overlaps its
            # predecessor by one window and is cut inside it.
            hop_trims = i > 0 and not hop_restart
            if ht:
                hop_is_start = True
            if hop_restart:
                print(f"[{TAG}] hop {i + 1}: ANCHOR RESTART -- start image is "
                      f"frame 0, the previous hop is not relayed", flush=True)
            _push_preview(unique_id, f"hop {i + 1}/{n} sampling…", hop=i + 1, total=n,
                           frac=(write_pos / float(total_frames)) if total_frames else None)
            # The same lint the writer's `validate` runs, on the queue, for a
            # plan nobody generated. A hand-written beat written in the official
            # `<d>` form overruns its hop exactly as a generated one does, and
            # here the only place left to say so is before this hop spends its
            # minutes. Warn, never raise: the seconds are an estimate at
            # SPEECH_WPS and the author is the one who knows the delivery.
            for _w in _planner.dialogue_warnings(shot.get("beat"),
                                                 hop_length / float(FPS)):
                print(f"[{TAG}] hop {i + 1}: {_w}", flush=True)

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
                        ref_plan_refs, shot_refs, _wired_slots)
                    print(
                        f"[{TAG}] hop {i + 1}: shot.refs "
                        + (", ".join("@" + t for t in shot_refs) or "(none)"),
                        flush=True,
                    )
                else:
                    hop_active = _refs.active_refs(
                        ref_plan_refs, i, _wired_slots)
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
                hop_pictures = [r for r in hop_active if r.get("kind") != "video"]
                hop_mod_videos = [r for r in hop_active if r.get("kind") == "video"]
                base_images = {
                    f"ref_image_{k + 1}": slot_images[r["slot"]]
                    for k, r in enumerate(hop_pictures)
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

            if ref_plan_refs:
                pass
            else:
                hop_pictures = []
                hop_mod_videos = []
            hop_video_seed = dict(base_videos or {})
            # Soundtracks pair with clips BY ORDINAL, so this dict has to be
            # grown in lockstep with the one above -- `ref_video_audio_2`
            # belongs to `ref_video_2` and to nothing else.
            hop_video_audios = dict(base_video_audios or {})
            # A mod's voice is NOT fused onto its clip as a soundtrack. It
            # was, and a `video_audio` block reads as "this person saying
            # these words": the woman opened a single hop with a second of
            # the ElevenLabs script before her own <d> line (2026-09-23).
            # Core also trims the clip to a fraction of a second and the
            # sound not at all. The voice goes beside, as a timbre
            # reference, the same as a still's -- see `hop_ref_audios`.
            for _vr in hop_mod_videos:
                _vn = len(hop_video_seed) + 1
                hop_video_seed[f"ref_video_{_vn}"] = slot_videos[_vr["slot"]]
            video_ords = {
                r["tag"]: len(base_videos or {}) + n + 1
                for n, r in enumerate(hop_mod_videos)
            }
            hop_images = base_images
            hop_videos = hop_video_seed or None
            live_p = live_v = None
            still_shift = 0
            # Not on a restart. Pinning the previous hop's last frame as
            # <Picture 1> is the definition of a continuation, and a restart
            # takes NOTHING from the previous hop -- the branch at the sampler
            # says so in as many words. It was still handing over the tail here.
            if not hop_is_start:
                hop_images, live_p, hop_videos, live_v = _attach_pin_to_qwen(
                    str(pin_to_qwen), base_images, hop_video_seed,
                    prev_imgs[-1:], prev_imgs[-overlap_n:],
                )
                if live_p == 1:
                    still_shift = 1
                if not hop_images:
                    hop_images = None
                if not hop_videos:
                    hop_videos = None

            # Who this hop is holding: its own subjects, plus whoever the pin
            # hands it. A chain start is handed nobody, so it holds only its
            # own -- and because this is what seeds `prev_subjects`, a person
            # carried through a still-less hop stays carried, which is the
            # case `continuity_line` exists for.
            hop_subjects = {r["subject"] for r in hop_active
                            if r["subject"] is not None}
            carried_subjects = (hop_subjects if hop_is_start
                                else hop_subjects | prev_subjects)
            prev_subjects = carried_subjects

            # One ordinal map for this hop. The prompt's <Picture N> citations
            # and the identity lock's ordinals have to agree, and computing the
            # same shift twice is exactly how they drift apart. `p`, not `n` --
            # `n` is the hop count in the enclosing scope.
            hop_ords = _refs.ordinals(hop_pictures)
            if still_shift:
                hop_ords = {t: p + still_shift for t, p in hop_ords.items()}
            if ref_plan_refs:
                # Plan-wide subjects so @hero_face still resolves when that
                # photograph is off this hop.
                # Names, not ordinals, on a CONTINUATION. `subject_definitions:`
                # rides every chain START, so a `<Subject N>` on one has its
                # antecedent in its own encode; on a continuation it does not --
                # the dangling-token defect that turned an undescribed "the
                # bowl" into a steel one. `continuity_line` rides every
                # continuation hop and is what the name binds to.
                block = _refs.resolve_tags(
                    block, hop_ords, _refs.subjects(ref_plan_refs),
                    where=f"shot {i + 1}",
                    declared={r["tag"] for r in ref_plan_refs},
                    subject_names=({k: (v or {}).get("name")
                                    for k, v in (ref_subjects or {}).items()}
                                   if not hop_is_start else None),
                    video_map=video_ords)
                if hop_is_start:
                    # Every chain start, not just hop 1. subject_prose derives
                    # its own ordinals with no still_shift, which is correct
                    # exactly when the hop has no pin -- and a restart has
                    # none: `still_shift` is 0 and `live_p` is None twenty
                    # lines up, because the pin branch is gated on
                    # `not hop_is_start`.
                    #
                    # Reading `i == 0` here left the consumer below -- which
                    # already asks `hop_is_start` -- dead for every restart.
                    # Those hops were handed their reference pictures in the
                    # DiT and as `<Picture N>` in the tokenizer with NOTHING
                    # in the prose citing them, and an uncited plate is the
                    # one thing this pack knows the model free-associates
                    # into the frame: a cast member who is not in the beat
                    # walks into the shot. `@tag` did not save it either,
                    # because `subject_names` was flattening those to bare
                    # prose on the same off-by-one test.
                    hop_subject_prose = _refs.subject_prose(
                        hop_active, ref_subjects, video_ords)
            if hop_is_start and hop_subject_prose and not _d.is_full_h3_prompt(block):
                block = hop_subject_prose + "\n\n" + block
            # Hop 1 has no _assemble_next to fold this into. A full H3 block is
            # the author's own prompt end to end, so it is left alone there --
            # the same rule subject_prose follows two lines up.
            if hop_is_start and refvid_line and not _d.is_full_h3_prompt(block):
                block = block.rstrip() + "\n\n" + refvid_line
            if str(hop_script) == "next" and not hop_is_start:
                n_stills = len(base_images or {})
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
                    faces = [r for r in hop_pictures
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
                # `carried_subjects`, NOT every subject in the register.
                # This line says "X continues, <wardrobe>" about each person
                # it is given, and at cfg 1.0 there is no negative branch, so
                # every name in it is additive -- naming someone puts them in
                # the frame. Built plan-wide it was correct for a one- or
                # two-hander and catastrophic past that: an eight-character
                # register told hop 2 that all eight continue, with wardrobe,
                # and the model duly rendered the whole cast standing in a
                # row. Seed-independent, because the register is not the seed.
                #
                # The rationale for reaching beyond `hop_active` still holds
                # -- a person the pin carries with no still riding must be
                # named -- and `carried_subjects` keeps exactly that and
                # nothing more.
                hop_continuity = _refs.continuity_line(
                    ref_subjects, carried_subjects) if ref_plan_refs else ""
                # What each still riding THIS hop is for. Hop 1 gets this from
                # subject_prose; without it here a scheduled still reaches the
                # encoder as an uncited photograph with no stated role, and a
                # Ref2VA model handed a picture and no reason for it renders the
                # picture. `hop_ords`, not fresh ordinals -- the live frame is
                # <Picture 1> on every continuation hop.
                hop_retention = _refs.retention_prose(
                    hop_active, hop_ords, video_ords)
                hop_wardrobe = any(r["retention"] == "partially_copy"
                                   for r in hop_active)
                block = _assemble_next(
                    block,
                    live_picture=live_p,
                    live_video=live_v,
                    n_stills=n_stills,
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
            _voice_start = _start_carries_voice(hop_is_start, ht, voice_every_hop, block)
            hop_voice = voice is not None and (
                _voice_start or str(hop_script) != "next" or _voice_lift)
            if ht and hop_is_start and not _voice_start and (
                    voice is not None or slot_voices):
                print(f"[{TAG}] hop {i + 1}: hand-tie hop with no spoken line; "
                      "the voice stays off it", flush=True)
            elif not hop_is_start and voice is not None and not hop_voice:
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
            # <Audio N> is positional, and core emits every clip's soundtrack
            # BEFORE any standalone voice. So the global voice stops being
            # <Audio 1> the moment a reference clip carries sound -- it was
            # cited as <Audio 1> unconditionally, which pointed that sentence
            # at the clip's track instead. Walk core's own iteration order.
            _audio_n = 0
            _voice_ords = {}
            _vid_key_tag = {f"ref_video_{_v}": _t for _t, _v in video_ords.items()}
            for _k in (hop_videos or {}):
                _suffix = _k.rsplit(chr(95), 1)[-1]
                if f"ref_video_audio_{_suffix}" not in hop_video_audios:
                    continue
                _audio_n += 1
                if _k in _vid_key_tag:
                    _voice_ords[_vid_key_tag[_k]] = _audio_n
            hop_ref_audios = {}
            if hop_voice:
                for _a in (ref_audios or {}).values():
                    hop_ref_audios[f"ref_audio_{len(hop_ref_audios) + 1}"] = _a
            _voice_ord = _audio_n + 1 if hop_ref_audios else 0
            _audio_n += len(hop_ref_audios)
            # Row voices obey the same gate as `voice_file`. A row that rides
            # all seven hops used to carry its recording onto all seven, and
            # a quiet hop fills its leftover frames with whatever it is shown.
            _row_voice_on = (_voice_start or str(hop_script) != "next"
                             or _voice_lift)
            _hop_voice_tags = []
            _row_slot = {r["tag"]: r["slot"] for r in (hop_active or [])}
            for _pr in list(hop_pictures) + list(hop_mod_videos):
                _rv = slot_voices.get(_pr["slot"])
                if _rv is None:
                    continue
                if not _row_voice_on:
                    if hop_is_start:  # hand-tie, quiet: said once above
                        continue
                    print(f"[{TAG}] hop {i + 1}: @{_pr['tag']} voice stays off "
                          f"this continue (voice_every_hop={voice_every_hop})",
                          flush=True)
                    continue
                _hop_voice_tags.append(_pr["tag"])
                hop_ref_audios[f"ref_audio_{len(hop_ref_audios) + 1}"] = _rv
                _audio_n += 1
                _voice_ords[_pr["tag"]] = _audio_n
            hop_ref_audios = hop_ref_audios or None

            if _voice_ord and f"<Audio {_voice_ord}>" not in block:
                block = (
                    block.rstrip()
                    + f"\n\nThe speaker\'s voice follows "
                      f"<Audio {_voice_ord}> as a timbre reference."
                )
            # One binding per row voice, affirmative and without pronouns,
            # tying a timbre to a person rather than leaving the model to
            # guess which of two faces owns which of two recordings. Where
            # it goes, and the speaker IDs it carries: `refs.bind_voices`.
            _bindings = []
            for _rr in hop_active:
                _ord = _voice_ords.get(_rr["tag"])
                if not _ord or f"<Audio {_ord}>" in block:
                    continue
                if _rr.get("subject"):
                    _who = f"<Subject {_rr['subject']}>"
                elif _rr["tag"] in video_ords:
                    _who = f"<Video {video_ords[_rr['tag']]}>"
                else:
                    _who = f"<Picture {hop_ords.get(_rr['tag'], 0)}>"
                _bindings.append({"audio": _ord, "subject": _rr.get("subject"),
                                  "who": _who})
            if _bindings:
                _starts = ([_m.start() for _m in re.finditer(r"<d>", block)]
                           + [_a for _a, _b in _planner.spoken_spans(block)])
                block = _refs.bind_voices(block, _bindings, _starts)

            # Outermost, after everything the chain adds: a LoRA trigger has to
            # be the first thing the encoder reads and a trailing camera/body
            # clause the last, and on hop 2+ the airlock and closer otherwise
            # bury both. Applied here, before the key and the encode, so the
            # cache, info and the upsampler all see the same text. Empty
            # strings leave the block byte-identical, so old keys still hit.
            _pre = str(prompt_prefix or "").strip()
            _suf = str(prompt_suffix or "").strip()
            if _pre:
                block = _pre + " " + block.lstrip()
            if _suf:
                block = block.rstrip() + " " + _suf

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

            if ht and ht_pos[i][0] not in ht_windows:
                _ht_previs(i)
            if previs_only:
                pbar.update(1)
                continue

            # Key this hop. prev_key makes the key chained, so editing shot 1
            # invalidates every hop after it -- correct, and the reason the UI
            # must show staleness before queuing or it reads as a bug.
            hop_key = None
            cached = None
            locked_served = False
            # A restart outranks a forced `pin_mech`: it relays nothing, so
            # there is no mechanism left to pick. Asking for motion_context on
            # a restart hop is not a contradiction to raise on, it is a setting
            # that does not reach this hop.
            pin_mech_pred = ("hand_tie" if ht
                             else "none" if hop_restart
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
                    # Absent unless this hop cites a RefMod, so a still-only
                    # hop keeps the key it had before RefMods existed. The
                    # pixel digest above is the Qwen view; this is the stored
                    # latent and the strength that actually reach the DiT.
                    # `looks` only when the file keeps photographs: a video
                    # mod's Qwen view is not in `refs`, and a mod without
                    # looks keeps the key it had before they existed.
                    **({"refmods": [
                            {"tag": r["tag"],
                             "strength": round(float(r["strength"]), 4),
                             "latent": r.get("latent_digest"),
                             **({"looks": r["looks_digest"]}
                                if r.get("looks_digest") else {})}
                            for r in hop_active if r.get("source") == "refmod"
                        ]} if any(r.get("source") == "refmod" for r in hop_active)
                       else {}),
                    # Same rule as the refs above: per hop, and absent when
                    # no row on this hop carries a voice, so every key
                    # written before row voices existed still hits.
                    # Only the voices this hop actually received, since the
                    # `voice_every_hop` gate can hold a row's voice back.
                    **({"voices": {t: _store.audio_digest(
                            slot_voices.get(_row_slot[t]))
                        for t in _hop_voice_tags}} if _hop_voice_tags else {}),
                    # A mod voice used to be fused onto its clip. The same
                    # inputs now render it beside, so those hops must miss.
                    **({"voice_bind": "beside"}
                       if any(t in video_ords for t in _hop_voice_tags)
                       else {}),
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
                if split_on:
                    # Only what split reads. Its own key space: a split hop is
                    # neither an off hop nor a refined one.
                    hop_payload["refine"] = [
                        "split", _split_k(refine_steps, hop_payload["steps"]),
                        str(refine_sampler), refine_model_fp, SPLIT_EXTEND]
                elif str(hop_refine) != "off":
                    hop_payload["refine"] = [
                        str(hop_refine), round(float(refine_denoise), 4),
                        int(refine_steps), str(refine_sampler),
                        str(refine_scheduler), str(refine_cond),
                        str(refine_head), str(refine_audio),
                        str(refine_blend).strip(), str(refine_blend_interp),
                        refine_model_fp]
                    # Appended only for hop_tail, so a run pinned back to
                    # `denoise` keys exactly as it did before this mode
                    # existed -- same key, same frames, and an archived
                    # entry still hits. hop_tail gets its own key space
                    # because it really does sample different sigmas.
                    if str(refine_align) != "denoise":
                        hop_payload["refine"].append(str(refine_align))
                    # pin_only skips the refine on the LAST hop (nothing reads
                    # its pin). That render is a different hop from the same
                    # shot refined, and the key has to say so: extend the chain
                    # and the old last hop is no longer last, so without this
                    # the cache served its unrefined render and the new hop
                    # pinned from a latent an uninterrupted run never makes.
                    if str(hop_refine) == "pin_only" and i >= n - 1:
                        hop_payload["refine"].append("skipped:last")
                # Every hop, hop 1 included, and present only when on -- same
                # byte-identical-when-off rule as the refine above. The LoRA
                # names and strengths stand in for a fingerprint: the boost
                # model is derived from `model`, whose fingerprint is already
                # in chain_salt.
                if boost_on:
                    hop_payload["boost"] = [
                        [[nm, round(s, 4)] for nm, s in boost_loras],
                        BOOST_EXTEND, BOOST_SPLIT]
                if ht:
                    # The two windows, not the previous hop: a hand-tie hop reads
                    # nothing from its predecessor, so editing an earlier beat
                    # leaves it cached, and a new previs moves both digests.
                    _si, _k = ht_pos[i]
                    _wp, _ws = ht_windows[_si]
                    _tl = _k + 1 < len(_wp)
                    hop_payload.update(_ht.hop_key_fields(
                        _store.tensor_digest(_wp[_k]),
                        _store.tensor_digest(_wp[_k + 1]) if _tl else None,
                        _store.audio_digest(_ws[_k]),
                        _store.audio_digest(_ws[_k + 1]) if _tl else None))
                hop_key = _store.hop_key(
                    None if (hop_restart or ht) else prev_key, hop_payload)
                # A locked shot reuses its last render even though its inputs
                # changed -- that is the point of locking. The content key would
                # have moved, so the pointer is what finds it.
                shot_name = str(shot.get("id") or f"shot{i + 1}")
                content_key = hop_key
                if shot.get("locked"):
                    pinned = hop_store.get_pointer(shot_name)
                    if pinned:
                        if pinned != hop_key:
                            print(f"[{TAG}] hop {i + 1} is locked: reusing its "
                                  f"earlier render (inputs changed)", flush=True)
                        # A locked hop generated against a different hop i
                        # than the one this run has: the join between them
                        # was never generated. Tone correction can shift its
                        # brightness, not its content. Entries written before
                        # `prev_key` was recorded are not judged.
                        _pm = hop_store.meta(pinned) or {}
                        if ht and "ht_windows" in _pm and _pm["ht_windows"] != [
                                hop_payload["head"], hop_payload["tail"]]:
                            # A hand-tie hop's join partners are its windows. A
                            # locked hop tied to another previs meets this run's
                            # neighbours on frames it never saw.
                            unpaired_joins.append(i + 1)
                            print(f"[{TAG}] WARNING: hop {i + 1} is locked, but it was "
                                  f"tied to a different previs than this run's. Its "
                                  f"joins were never generated with these neighbours. "
                                  f"Unlock it to render it against this previs.",
                                  flush=True)
                        if (not hop_restart and not ht and "prev_key" in _pm
                                and _pm["prev_key"] != prev_key):
                            unpaired_joins.append(i + 1)
                            print(f"[{TAG}] WARNING: hop {i + 1} is locked, but "
                                  f"hop {i} is not the render it was generated "
                                  f"against. The hop {i}|{i + 1} join was never "
                                  f"generated together. Unlock hop {i + 1}, or "
                                  f"pin hop {i}'s ending with last_frame_guide "
                                  f"or anchor=restart.", flush=True)
                        hop_key = pinned
                        locked_served = True
                    else:
                        print(f"[{TAG}] hop {i + 1} is locked but has no cached "
                              f"render yet; rendering it once", flush=True)
                hop_keys.append(hop_key)
                cached = hop_store.get(hop_key)
                if (cached is not None and locked_served
                        and tuple(cached[0].shape[1:3]) != (int(height), int(width))):
                    # A lock outranks the content key, not the canvas. Served at
                    # another size it cannot be written into this master, and
                    # under hand_tie its windows would not match (2026-10-09:
                    # hop 1 locked at 512p, re-queued at 768p).
                    print(f"[{TAG}] hop {i + 1} is locked, but its render is "
                          f"{int(cached[0].shape[2])}x{int(cached[0].shape[1])} and this "
                          f"run is {int(width)}x{int(height)}; rendering it again at "
                          f"this size", flush=True)
                    cached = None
                    locked_served = False
                    hop_key = content_key
                    hop_keys[-1] = hop_key
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

            def _hop_ref2va():
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
                    ref_video_audios=(hop_video_audios or None),
                    # Row voices are already folded in, at the ordinals the
                    # citations above were written against.
                    ref_audios=hop_ref_audios,
                )
                c, lat = _result(packed)[0], _result(packed)[1]
                if refmod_image_blocks or refmod_video_blocks:
                    # ReferenceToVideo encoded the decode. Put the stored
                    # latent back in that slot before anything else copies it.
                    c = _refmod.splice_blocks(
                        c, hop_images, hop_videos,
                        refmod_image_blocks, refmod_video_blocks)
                return c, lat

            this_sampled = None
            # What upsample needs from this hop: the latent it DELIVERED and
            # the conditioning that made it.
            up_latent = up_cond = None
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
                if upsample_on:
                    # A cached hop kept its latent but not its conditioning;
                    # the upsampler's re-draw wants the one that made it.
                    up_latent = cached_latent
                    up_cond = _hop_ref2va()[0]
            else:
                cond, latent = _hop_ref2va()
                # The conditioning as Ref2VA built it, before any pin or guide
                # is added to it. `refine_cond=base` pulls the second pass
                # toward this -- the references and the text, nothing about the
                # join. Captured here rather than reconstructed later because
                # every branch below rebinds `cond`.
                base_cond = cond

                if ht:
                    # Window k at frame 0, window k+1 at the last 22 frames,
                    # picture and sound together. Nothing from the previous hop.
                    _si, _k = ht_pos[i]
                    _wp, _ws = ht_windows[_si]
                    # A section's last hop has no window k+1: tied at its start only.
                    _ties = [(0, _k)] + ([(-_ht.WINDOW, _k + 1)] if _k + 1 < len(_wp) else [])
                    for _fi, _w in _ties:
                        cond = _result(_core_call(
                            MiniMaxH3AddGuide, "the hand-tie window",
                            positive=cond, latent=latent, frame_idx=_fi,
                            vae=vae, audio_vae=audio_vae,
                            image=_wp[_w], audio=_ws[_w],
                        ))[0]
                    pin_mech_used = "hand_tie"
                    _heard = [str(_w + 1) for _fi, _w in _ties if _ws[_w] is not None]
                    print(f"[{TAG}] hop {i + 1}: hand-tie, "
                          + (f"windows {_k + 1} and {_k + 2}" if len(_ties) == 2
                             else f"window {_k + 1} (its start; the section's last hop)")
                          + f" of section {_si + 1} (picture"
                          + (f"; sound at window {', '.join(_heard)}" if _heard else " only")
                          + ")", flush=True)
                elif (i == 0 or hop_restart) and start_image is not None:
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
                if boost_model is not None:
                    # Same conditioning on both guiders, so the pin and the
                    # guides reach the boost stage too. The latent that comes
                    # out is an ordinary hop latent on the hop canvas: the
                    # decode, the next hop's pin and the cache take it as is.
                    boost_guider = _result(_core_call(
                        BasicGuider, "the boost guider",
                        model=boost_model, conditioning=cond))[0]
                    sampled = _boost_sampled(
                        latent, model=model, guider=guider,
                        boost_guider=boost_guider, sampler=sampler,
                        hop_sigmas=hop_sigmas, sigma_cache=sigma_cache,
                        hop_steps=hop_steps, seed=shot_seed, hop_no=i + 1,
                        label=f"{sampler_name}/{scheduler}")
                    del boost_guider
                elif split_on:
                    # The finish runs on its own guider only when it has its
                    # own model; the conditioning is the hop's either way, so
                    # the pin and the guides reach the finish too.
                    finish_guider = guider
                    if refine_model is not None:
                        finish_guider = _result(_core_call(
                            BasicGuider, "the split finish guider",
                            model=split_model, conditioning=cond))[0]
                    sampled = _split_sampled(
                        latent, noise=noise, guider=guider,
                        finish_guider=finish_guider, sampler=sampler,
                        finish_sampler=refine_sampler_obj,
                        hop_sigmas=hop_sigmas, sigma_cache=sigma_cache,
                        hop_steps=hop_steps,
                        k=_split_k(refine_steps, hop_steps), hop_no=i + 1,
                        label=(sampler_name if str(refine_sampler) == "same"
                               else f"{sampler_name} -> {refine_sampler}")
                              + (", refine_model" if refine_model is not None else ""))
                    del finish_guider
                else:
                    sampled = _result(_core_call(
                        SamplerCustomAdvanced, "the sampler",
                        noise=noise, guider=guider, sampler=sampler,
                        sigmas=hop_sigmas, latent_image=latent))[0]

                # Refine, before the decode so `full` reaches the delivered
                # pixels. After the sampler, so pin_mech / the guides / the
                # audio lock are all already spent and none of them can see it.
                refined_for_pin = None
                if str(hop_refine) not in ("off", "split"):
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
                            align=str(refine_align),
                            hop_sigmas=hop_sigmas, hop_steps=int(hop_steps),
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
                if ht and boost_model is None and not split_on and str(hop_refine) == "off":
                    # chain_00155: hop 4 held its head window for 22 frames, then cut
                    # to another shopfront the moment the guide ran out; its previs
                    # was clean. The hop checks itself like the previs does and
                    # renders once more on a seed clear of every other. The kept take is
                    # cached under the hop's own key (its planned seed), so a hit serves it.
                    _hcuts = _ht.previs_cuts(imgs)
                    if _hcuts:
                        _rs = (shot_seed + _ht.PREVIS_RETRY_STRIDE) & 0x7FFFFFFF
                        print(f"[{TAG}] hand-tie hop {i + 1}: seed {shot_seed} cuts at "
                              + ", ".join(f"f{c} ({c / FPS:.2f}s)" for c in _hcuts)
                              + f"; rendering it again on seed {_rs}", flush=True)
                        _n2 = _result(_core_call(RandomNoise, "the hop retake noise",
                                                 noise_seed=_rs))[0]
                        _s2 = _result(_core_call(
                            SamplerCustomAdvanced, "the hop retake sampler",
                            noise=_n2, guider=guider, sampler=sampler,
                            sigmas=hop_sigmas, latent_image=latent))[0]
                        _i2, _a2 = _decode_av(vae, audio_vae, _s2)
                        _i2 = _i2.contiguous().cpu()
                        _c2 = _ht.previs_cuts(_i2)
                        if len(_c2) < len(_hcuts):
                            sampled, imgs = _s2, _i2
                            wav = _a2["waveform"].contiguous().cpu()
                            sr = int(_a2["sample_rate"])
                            audio = {"waveform": wav, "sample_rate": sr}
                        if _c2:
                            print(f"[{TAG}] WARNING: hand-tie hop {i + 1}: the retake cuts too, at "
                                  + ", ".join(f"f{c}" for c in _c2) + "; keeping the take with "
                                  "fewer cuts. A beat that moves to a new place asks for a cut.",
                                  flush=True)
                        else:
                            print(f"[{TAG}] hand-tie hop {i + 1}: seed {_rs} holds one take",
                                  flush=True)
                        del _n2, _s2, _i2, _a2
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
                if upsample_on:
                    # `sampled`, not `this_sampled`: under pin_only those
                    # differ, and the upsampler redraws what was delivered.
                    up_latent = (this_sampled if refined_for_pin is None
                                 else _latent_cpu(sampled))
                    up_cond = base_cond

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
                    # `prev_key` is the hop this one was generated against.
                    # A locked hop is served by pointer whatever its key, so
                    # this is the only record of which join it belongs to.
                    hop_store.put(hop_key, imgs, wav, sr,
                                  {"hop": i + 1, "of": n, "block": block[:400],
                                   "pin_mech": pin_mech_used,
                                   "prev_key": None if (hop_restart or ht) else prev_key,
                                   **({"ht_windows": [hop_payload["head"], hop_payload["tail"]]}
                                      if ht else {})},
                                  latent=this_sampled)
                    hop_store.set_pointer(
                        str(shot.get("id") or f"shot{i + 1}"), hop_key)
                    # Go on with what a hit returns: a chained hand-tie section
                    # keys its previs on this hop's frames.
                    imgs, wav = _store.as_stored(imgs, wav)

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
            if ht:
                # The HEAD window only: the tail span is cut from the film, and
                # guided at -22 it renders ~10 /255 brighter than what is kept
                # (chain_00144), so matching it darkened the kept frames. The join
                # is closed by seam_bend at the master write.
                _si, _k = ht_pos[i]
                _wp, _ws = ht_windows[_si]
                imgs, (_ho, _to) = _ht.tone_match(imgs, _wp[_k], None)
                tone_note = ("hand-tie head " + "/".join("%+.1f" % (float(v) * 255) for v in _ho)
                             + " /255")
                print(f"[{TAG}] hop {i + 1} tone: {tone_note}", flush=True)
            if tone_on and not ht:
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
            if anchor_on and not ht:
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

            if not hop_trims:
                keep_n = int(imgs.shape[0])
                if write_pos + keep_n > total_frames:
                    raise ValueError(
                        f"{TAG}: hop {i + 1} overruns the preallocated master "
                        f"({write_pos + keep_n} > {total_frames}). A hop decoded a "
                        f"different length than planned.")
                if i > 0:
                    seam_marks.append(int(write_pos))
                ht_hop_start = int(write_pos)
                ht_section_starts.append(int(write_pos))
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
            elif ht:
                # Hand-tie cuts at the START of the shared window, not its end.
                # Both hops render that window; the previous hop renders it
                # under the -22 guide, which steps its level frame to frame
                # (raw hops of chain_00142: -11/255 at frame -5, +4..+6 at -20),
                # while this hop renders it under the frame-0 guide and holds
                # (-3 at worst). So the previous hop's tail span is overwritten
                # and this hop is written whole. Same film length.
                keep_n = int(imgs.shape[0])
                write_pos -= overlap_n
                if write_pos + keep_n > total_frames:
                    raise ValueError(
                        f"{TAG}: hop {i + 1} overruns the preallocated master "
                        f"({write_pos + keep_n} > {total_frames}). A hop decoded a "
                        f"different length than planned.")
                seam_marks.append(int(write_pos))
                # The previous hop, already in the master, bent to meet this
                # hop's first frame (both are on their head windows' tone).
                _bend = _ht.seam_bend(master_imgs, ht_hop_start, write_pos, imgs)
                if _bend is not None:
                    print(f"[{TAG}] hop {i} bent " + "/".join(
                        "%+.1f" % (-float(v) * 255) for v in _bend)
                        + f" /255 across its length to meet hop {i + 1}", flush=True)
                ht_hop_start = int(write_pos)
                ht_cuts.append(int(write_pos))
                master_imgs[write_pos:write_pos + keep_n] = imgs
                write_pos += keep_n
                master_wav = _batch_wav(master_wav)
                dropped = min(_audio_samples(overlap_n, sr), int(master_wav.shape[-1]) - 1)
                _in = _batch_wav(audio["waveform"])
                # The sound leaves the outgoing hop at its first quiet point in
                # the window, not mid-word at the picture cut (chain_00147).
                _late = _ht.audio_cut(master_wav[..., -dropped:], _in[..., :dropped], sr)
                # The 40 ms crossfade overlaps its two sides, so it used to eat 40 ms
                # of timeline at every join: 11 joins = sound 440 ms ahead of the
                # lips by hop 12 (chain_00153, HTCChainPreview "drift -440 ms").
                # The outgoing hop's dropped tail has the sound to spare, so the
                # left side runs on by the fade length and the length is kept.
                _xf = min(max(1, int(sr * 40 / 1000.0)), dropped - _late)
                master_wav = _xfade_audio(
                    master_wav[..., :int(master_wav.shape[-1]) - dropped + _late + _xf],
                    _in[..., _late:], sr)
                print(
                    f"[{TAG}] hop {i + 1}: cut at the window start, dropped hop {i}'s "
                    f"last {overlap_n} frames; its sound runs {_late / float(sr):.2f}s on "
                    f"to a quiet point",
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

            if upsample_on:
                _up = _latents.from_dict(up_latent) if up_latent is not None else None
                if ht and hop_trims and upsample_hops:
                    # The master cut the previous hop's tail span, not this head.
                    upsample_hops[-1]["tail"] = int(overlap_n)
                    upsample_hops[-1]["frames"] -= int(overlap_n)
                upsample_hops.append({
                    "hop": i + 1,
                    "video": (_up[0] if _up and len(_up) == 2 else None),
                    "audio": (_up[1] if _up and len(_up) == 2 else None),
                    "cond": up_cond,
                    "start": not hop_trims,
                    "drop": int(overlap_n) if (hop_trims and not ht) else 0,
                    "frames": int(keep_n),
                    # pin_only caches the REFINED latent, not the delivered one
                    "stand_in": bool(cached is not None
                                     and str(hop_refine) == "pin_only"),
                })
                del _up

            if want_sheet:
                # The frames this hop actually CONTRIBUTES: a chain start
                # (hop 1 or a restart) gives all of them, a continuation
                # gives what survives the overlap trim. Showing imgs[0] on
                # a continuation would show a frame the master never contains.
                _f0 = imgs[overlap_n] if (hop_trims and not ht) else imgs[0]
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
                      "tone": tone_note or None,
                      "unpaired": (i + 1) in unpaired_joins,
                      # The retake timeline's run record: which render this
                      # hop is, how it was obtained, and where it sits on the
                      # master. Full key, because the timeline fetches the
                      # hop's pictures and playback by it.
                      "timeline": {
                          "key": hop_key,
                          "shot_id": str(shot.get("id") or f"s{i + 1}"),
                          "status": ("locked" if locked_served
                                     else "cached" if cached is not None
                                     else "rendered"),
                          "restart": bool(hop_is_start and i > 0),
                          "start": int(write_pos - keep_n),
                          "delivered": int(keep_n),
                          "overlap": int(overlap_n),
                          "fps": int(FPS),
                      }})
            del imgs, wav, audio
            pbar.update(1)

        if previs_only:
            _imgs = torch.cat([m[0] for m in ht_previs_media], dim=0)
            _sr = int(ht_previs_media[0][2])
            _wav = torch.cat([m[1] for m in ht_previs_media], dim=-1)
            head = (f"PREVIS ONLY - {len(ht_previs_media)} section(s), "
                    f"{int(_imgs.shape[0])} frames ({_imgs.shape[0] / FPS:.1f}s) at "
                    f"{int(width)}x{int(height)}. No hop rendered; "
                    + ("the previs is cached, so the full run (dry_run=off) skips it."
                       if hop_store is not None else
                       "cache_hops is off, so the full run renders the previs again. "
                       "Turn cache_hops on to reuse this one."))
            print(f"[{TAG}] {head}", flush=True)
            _sep = chr(10) * 2
            info = head + _sep + _sep.join(
                "===== hand-tie previs %d (%d frames, windows at %s) =====\n%s" % row
                for row in ht_previs_info)
            _push_preview(unique_id, "previs done", hop=n, total=n, frac=1.0,
                          frame=_imgs[-1], meta={"done": True})
            return (_imgs, {"waveform": _wav, "sample_rate": _sr}, info,
                    _sheet.placeholder(width, height))

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
        if ht and master_imgs is not None:
            # One- and two-frame level blinks (every guided head dips -3 /255
            # at its frame 17), per section: a restart is a real cut.
            _ends = ht_section_starts[1:] + [int(write_pos)]
            _fix = max([_ht.deflicker(master_imgs[a:b])
                        for a, b in zip(ht_section_starts, _ends)] or [0.0])
            print(f"[{TAG}] hand-tie deflicker: largest correction {_fix * 255:.1f} /255",
                  flush=True)
            for _c in ht_cuts:
                _g = _ht.detail_settle(master_imgs, _c)
                if _g:
                    print(f"[{TAG}] hand-tie detail at f{_c}: softest gain {min(_g):.3f}",
                          flush=True)
        if hop_store is not None:
            hop_store.sweep(keep=hop_keys)

        # Upsample, once, over the whole film. After every hop, so it cannot
        # reach a pin; before `info` and the preview, so both describe what is
        # actually delivered. A failure here keeps the canvas film rather than
        # losing a render that has already cost every hop -- and says so.
        upsample_note = ""
        if upsample_on and upsample_hops:
            if any(h["stand_in"] for h in upsample_hops):
                print(f"[{TAG}] upsample: hop_refine=pin_only cached the "
                      "refined latent, not the delivered one; cached hops are "
                      "upsampled from the refined version", flush=True)
            # Under realism_boost the re-draw runs on the boost model. At
            # denoise 0.2 the re-draw repaints the fine band, which is where
            # the boost's look lives; on the MODEL wire it painted the base's
            # texture back over it. Same sigma curve either way -- the boost
            # model is built on the shifted model -- so the recipe is intact.
            _up_model = boost_model if boost_model is not None else model
            if upsample_dit is not None:
                # Its own model, shifted the way the hops were so the re-draw
                # lands on the same sigma. Nothing drawn on the MODEL wire or
                # in the boost reaches it.
                _up_model = _result(_core_call(
                    MiniMaxH3SigmaShift, "the sigma shift (upsample_dit)",
                    model=upsample_dit, shift_video=float(shift_video),
                    shift_audio=float(shift_audio)))[0]
                print(f"[{TAG}] upsample: re-drawing on the wired upsample_dit"
                      + (", not the realism boost model" if boost_model is not None
                         else ", not the MODEL wire"), flush=True)
            elif boost_model is not None:
                print(f"[{TAG}] upsample: re-drawing on the realism boost "
                      "model, not the MODEL wire", flush=True)
            if str(upsample_lora) != "None" and float(upsample_lora_strength) != 0.0:
                # On top of the re-draw model, so the render's own LoRAs (or the
                # boost's) stay under it. Delivery only: no hop key sees it.
                import nodes as nodes_mod  # noqa: PLC0415
                _up_model = nodes_mod.LoraLoaderModelOnly().load_lora_model_only(
                    _up_model, str(upsample_lora), float(upsample_lora_strength))[0]
                print(f"[{TAG}] upsample: re-draw LoRA {upsample_lora} "
                      f"{float(upsample_lora_strength):g}", flush=True)
            _push_preview(unique_id, "upsampling", hop=n, total=n, frac=1.0)
            # 0 (and anything under a floor no tile could fit, e.g. a stale
            # split_finish_aug value cast to INT) is the recipe's budget.
            _up_budget = (int(upsample_tile_tokens)
                          if int(upsample_tile_tokens) >= UPSAMPLE_MIN_TILE_TOKENS
                          else _tu.TOKEN_BUDGET)
            if _up_budget != _tu.TOKEN_BUDGET:
                print(f"[{TAG}] upsample: tile budget {_up_budget} tokens (recipe "
                      f"{_tu.TOKEN_BUDGET}): more, smaller tiles", flush=True)
            try:
                _up_guide = None
                if (upsample_guide_factor and float(upsample_denoise) > 0.0
                        and float(upsample_lora_strength) != 0.0):
                    _ug.install()
                    # The caption alone, no references: what the LoRA learned
                    # the guide under. Width/height/length only size the
                    # latent this discards.
                    _cap_cond = _result(_core_call(
                        MiniMaxH3ReferenceToVideo, "the h3upscale caption",
                        clip=clip, prompt=_ug.CAPTION, width=int(width),
                        height=int(height), length=5,
                        ref_image_size=ref_image_size))[0]
                    _up_guide = _tu.Guide(
                        factor=int(upsample_guide_factor), cond=_cap_cond,
                        decode=None, encode=vae.encode, pixels=_ug.guide_pixels)
                _up_imgs, upsample_note = _tu.upsample_chain(
                    _up_model, sampler, vae, upsample_hops,
                    float(upsample_megapixels),
                    master=(master_imgs if (tone_on or anchor_on) else None),
                    upscaler=str(upsample_model),
                    denoise=float(upsample_denoise),
                    alloc=_alloc_master, guide=_up_guide,
                    budget=_up_budget)
                master_imgs = _up_imgs
                del _up_imgs
                print(f"[{TAG}] {upsample_note}", flush=True)
            except mm.InterruptProcessingException:
                raise
            except Exception as _ue:  # noqa: BLE001
                upsample_note = f"upsample skipped, canvas film delivered: {_ue}"
                print(f"[{TAG}] WARNING: {upsample_note}", flush=True)
            upsample_hops = []
            mm.soft_empty_cache()
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
        if unpaired_joins:
            info += chr(10) + "unpaired joins: " + ", ".join(
                f"{h - 1}|{h}" for h in unpaired_joins)
        if upsample_note:
            info += chr(10) + upsample_note
        for _sec, _seed, _cuts in ht_previs_warn:
            info += (chr(10) + f"hand-tie previs {_sec} (seed {_seed}) cuts at "
                     + ", ".join(f"f{c}" for c in _cuts) + "; check the film there")
        print(f"[{TAG}] {info}", flush=True)
        # The assembled prompts being inspectable is the whole point of the
        # directive layer -- wire `info` to a Preview Text node to read exactly
        # what each hop sent to the text encoder.
        _sep = '\n\n'
        info = info + _sep + _sep.join('===== hop %d prompt =====\n%s' % (k, t) for k, t in assembled)
        if ht_previs_info:
            info = info + _sep + _sep.join(
                '===== hand-tie previs %d (%d frames, windows at %s) =====\n%s' % row
                for row in ht_previs_info)
        # `info` is the third return (Preview Text). Never send it as the
        # preview status — that strip is ~22 px and will render the dump.
        _v_secs = float(master_imgs.shape[0]) / FPS
        _a_secs = float(master_wav.shape[-1]) / float(sr) if sr else 0.0
        # A10: the hand-tie previs warnings, so the node can show them. They
        # were console-only, and the user's point is that nobody would know a
        # section was cut. The cut frames are PREVIS-LOCAL (they index the
        # section's own frames, not the film), so each entry carries the first
        # hop of its section and that hop's window offsets -- enough for the
        # timeline to place every cut at the right place in the film without
        # re-deriving the section geometry here. `ht_pos[i][0]` is the section
        # of hop i, so the section's first hop is the lowest i that maps to it.
        _warn_payload = []
        if ht_previs_warn:
            _first_hop = {}
            for _hi, (_hsec, _hk) in ht_pos.items():
                if _hsec not in _first_hop or _hi < _first_hop[_hsec]:
                    _first_hop[_hsec] = _hi
            for (_sec, _seed, _cuts) in ht_previs_warn:
                _warn_payload.append({
                    "section": int(_sec), "seed": int(_seed),
                    "cuts": [int(c) for c in _cuts],
                    "first_hop": int(_first_hop.get(_sec - 1, 0)),
                    "label": (f"hand-tie previs {_sec} (seed {_seed}) cuts at "
                              + ", ".join(f"f{c}" for c in _cuts)
                              + "; check the film there"),
                })
        _push_preview(
            unique_id,
            f"done · {int(master_imgs.shape[0])}f · {_v_secs:.1f}s",
            frame=master_imgs[-1], hop=n, total=n, frac=1.0,
            meta={"video_s": round(_v_secs, 3), "audio_s": round(_a_secs, 3),
                  "drift_ms": round((_a_secs - _v_secs) * 1000.0, 1),
                  "hops": int(n), "frames": int(master_imgs.shape[0]),
                  "ht_previs_warn": _warn_payload,
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
        # The music comes from `soundtrack_file` (the MEDIA picker). A wired
        # AUDIO socket used to sit beside it; removed 2026-10-07 as a duplicate.
        _bed = None
        if soundtrack_file:
            _bed = _media.load_audio(soundtrack_file,
                                     start=float(music_start_s),
                                     end=float(music_end_s))
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


# -- pre-rename ids ----------------------------------------------------------
# A plain alias in NODE_CLASS_MAPPINGS keeps old workflows loading, but it also
# lists the node a second time in search: ComfyUI falls back to the mapping key
# when NODE_DISPLAY_NAME_MAPPINGS has no entry. Subclassing and setting
# DEPRECATED gets both -- server.py publishes `deprecated: True`, and the
# frontend's `Comfy.Node.ShowDeprecated` (off by default) hides it from search
# while leaving it fully functional in workflows that name it.


class _LegacyH3RefChain(HandTieClips):
    DEPRECATED = True


NODE_CLASS_MAPPINGS = {
    "HandTieClips": HandTieClips,
    "H3RefChain": _LegacyH3RefChain,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "HandTieClips": "H3 Ref2VA Chain",
}
