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
# One definition, in refs.py -- routes.py publishes that copy to the editor, so
# a second constant here meant the node's slot count and the number the UI was
# told could drift apart.
from .refs import MAX_REF_IMAGES

FPS = 24
TAG = "HandTieClips"

# quality=draft. Low enough to be genuinely fast, high enough that blocking,
# camera and whether a join lands are all still readable. Both values are in
# the cache key already, so a draft never overwrites the matching final.
DRAFT_RESOLUTION = "0.3 MP"
DRAFT_STEPS = 6

# H3 canvas: multiples of 32, short-edge ~768, area cap 768*1344.
CANVAS = {
    "0.2 MP": {
        "16:9 landscape": (608, 352),
        "9:16 portrait": (352, 608),
        "1:1 square": (448, 448),
    },
    "0.3 MP": {
        "16:9 landscape": (736, 416),
        "9:16 portrait": (416, 736),
        "1:1 square": (544, 544),
    },
    "0.5 MP": {
        "16:9 landscape": (960, 544),
        "9:16 portrait": (544, 960),
        "1:1 square": (704, 704),
    },
    "0.7 MP": {
        "16:9 landscape": (1120, 640),
        "9:16 portrait": (640, 1120),
        "1:1 square": (832, 832),
    },
    "1.0 MP": {
        "16:9 landscape": (1280, 736),
        "9:16 portrait": (736, 1280),
        "1:1 square": (992, 992),
    },
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


def _canvas(resolution, aspect):
    try:
        return CANVAS[str(resolution)][str(aspect)]
    except KeyError:
        return (1280, 736)


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
                   continuity=""):
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
    # Order: who the pictures are, then what stays the same, then which
    # frame is live. `continuity` carries no ordinals, so it is safe on a
    # pin-only hop where `lock` is deliberately empty.
    inject = " ".join(p for p in (lock, str(continuity or "").strip(), cite) if p)
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

    if lock:
        hold = (
            f"{whoever} their current pose, room, lighting, and camera side, "
            "and the shot continues from exactly there. "
        )
        closer = (
            "Faces and hair follow the identity photographs. Clothing follows "
            "whatever is already on them in the live frame. After a brief hold "
            "on the incoming action, the shot advances through what the "
            f"next-beat describes, {terminal}"
        )
    else:
        hold = (
            "The incoming frame holds the current pose, room, lighting, and "
            "camera side, and the shot continues from exactly there. "
        )
        closer = (
            "Wardrobe, room, and lighting stay as they are in the live frame. "
            "After a brief hold on the incoming action, the shot advances "
            f"through what the next-beat describes, {terminal}"
        )
    # Official field names on hop 2+ start a new Ref2VA generate
    # (chain_00030..00034). One paragraph: airlock, then the beat.
    text = re.sub(
        r"(?m)^(overall_soundscape|non_diegetic_music):\s*", "", text).strip()
    return (
        f"{top}"
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
    """The component tensors inside a sampler `samples`, as a flat list.

    H3 hands back a `comfy.nested_tensor.NestedTensor` -- the video and audio
    latents in one object -- and it is not a Tensor. It has no `.std()`, and
    the attributes it *does* expose are traps: `.shape` returns
    `tensors[0].shape`, i.e. the video component's shape while silently
    speaking for both. Reading the components is the only honest way to touch
    the numbers. Returns None for anything unrecognised, which callers treat as
    "leave this latent alone".
    """
    if isinstance(x, torch.Tensor):
        return [x]
    if getattr(x, "is_nested", False) and hasattr(x, "unbind"):
        parts = list(x.unbind())
        if parts and all(isinstance(t, torch.Tensor) for t in parts):
            return parts
    return None


def _rebuild_latent_samples(x, parts):
    """Put conditioned components back into the container they came from."""
    if isinstance(x, torch.Tensor):
        return parts[0]
    return type(x)(parts)


def _condition_pin_latent(lat, anchor_std, renorm=False, noise=0.0, seed=0):
    """Anti-ratchet preprocessing for the latent handed to Motion-Context.

    MiniMaxH3MotionContext.apply() takes `context_latent` as-is and exposes no
    hook, so both levers have to be applied to the latent before it goes in.

    `renorm` rescales this pin so its standard deviation matches hop 2's. The
    pin's own sigma climbs hop over hop, and because that inflated pin is what
    conditions the next hop the growth compounds upstream of anything a master
    pass can reach. A scalar rescale moves no structure, so it cannot blur
    detail. `noise` mixes in a seeded perturbation, which attacks the same
    ratchet from the other side; measured gains reverse above 0.10, hence the
    widget cap.

    **Per component, not per latent** (fixed 2026-08-27). Video and audio are
    two tensors in one NestedTensor and their sigmas drift independently, so
    each carries its own anchor. Before this, `.std()` raised on the nested
    object and both levers were dead -- the failure announced itself once per
    hop as `pin conditioning skipped` and was easy to read as routine noise.
    A single global scale would also have been wrong on its own terms, letting
    the much larger video component dictate the audio's correction.

    Returns `(latent, new_anchor_std)` where the anchor is a list, one sigma
    per component -- hop 2 establishes what hops 3+ are matched against. Both
    levers default off, in which case the latent is returned untouched.
    """
    if not isinstance(lat, dict) or "samples" not in lat:
        return lat, anchor_std
    x = lat["samples"]
    parts = _latent_parts(x)
    if parts is None:
        print(f"[{TAG}] pin conditioning skipped: unrecognised latent "
              f"({type(x).__name__})", flush=True)
        return lat, anchor_std
    try:
        cur = [float(t.float().std()) for t in parts]
    except Exception as e:  # noqa: BLE001
        print(f"[{TAG}] pin conditioning skipped ({e!r})", flush=True)
        return lat, anchor_std
    if not all(c == c and c for c in cur):  # zero or NaN in any stream
        return lat, anchor_std
    if anchor_std is None:
        anchor_std = cur
    if len(anchor_std) != len(cur):
        # Stream count changed mid-chain. Nothing sensible to match against.
        print(f"[{TAG}] pin conditioning skipped: latent has {len(cur)} "
              f"component(s), anchor has {len(anchor_std)}", flush=True)
        return lat, anchor_std
    if not renorm and noise <= 0.0:
        return lat, anchor_std
    out_parts, notes = [], []
    for idx, (t, c, a) in enumerate(zip(parts, cur, anchor_std)):
        o = t
        if renorm:
            scale = a / c
            o = o * scale
            notes.append(f"renorm[{idx}] x{scale:.4f} (sigma {c:.4f} -> {a:.4f})")
        if noise > 0.0:
            # Per component: `.shape` on the nested object reports only the
            # first component's shape, so one draw for the whole latent would
            # size its noise to the video and broadcast that onto the audio.
            g = torch.Generator(device="cpu").manual_seed((int(seed) + idx) & 0x7FFFFFFF)
            n = torch.randn(o.shape, generator=g, dtype=torch.float32)
            o = o + n.to(dtype=o.dtype, device=o.device) * (float(noise) * a)
            notes.append(f"noise[{idx}] {float(noise):.3f}")
        out_parts.append(o)
    if notes:
        print(f"[{TAG}] pin conditioning: " + ", ".join(notes), flush=True)
    new = dict(lat)
    new["samples"] = _rebuild_latent_samples(x, out_parts)
    return new, anchor_std


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
                "resolution": (["0.2 MP", "0.3 MP", "0.5 MP", "0.7 MP", "1.0 MP"], {
                    "default": "1.0 MP",
                    "tooltip": "Output area. 1.0 MP landscape is 1280x736 (the verified H3 size). Snapped to H3's 32 px grid.",
                }),
                "aspect": (["16:9 landscape", "9:16 portrait", "1:1 square"], {
                    "default": "16:9 landscape",
                    "tooltip": "Frame shape. Combined with resolution to set width and height.",
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
                        "Blank = today's positional behaviour."
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
                "pin_renorm": (["off", "on"], {
                    "default": "off",
                    "tooltip": (
                        "Rescale each pinned latent so its spread matches hop 1's. "
                        "The pin's own sigma climbs every hop and that inflated pin "
                        "conditions the next one, so texture ratchets up along a "
                        "long chain. A scalar rescale moves no structure, so it "
                        "cannot blur detail. Off reproduces chain_00038 exactly; "
                        "turn it on for chains of 3+ hops."
                    ),
                }),
                "pin_noise": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 0.10, "step": 0.005,
                    "tooltip": (
                        "Mix seeded noise into the pinned latent before it "
                        "conditions the next hop -- the other half of the texture "
                        "ratchet fix. Small values only: measured gains fall off "
                        "and reverse above 0.10, which is why the range stops "
                        "there. 0.0 reproduces chain_00038 exactly; 0.05 is the "
                        "documented starting point."
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
                    "tooltip": "Voice or timbre reference, riding every hop as <Audio 1>. Set in the panel.",
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
                   reference_video_file="", voice_file="", **_):
        """Re-run when a reference file changes underneath its name.

        Every picture now arrives as a basename, and a basename is a stable
        input: overwrite `face.png` with a different face and ComfyUI would
        happily serve the previous render. Hashing path+mtime is the fix.

        Deliberately NOT `float("nan")` -- that is the blunt version of this and
        would force a full re-render of an expensive node on every queue.
        """
        names = [start_image_file, reference_video_file, voice_file]
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
            unique_id=None):
        dry = str(dry_run) == "on"
        draft = str(quality) == "draft"
        want_sheet = str(contact_sheet) == "on" or dry
        if draft:
            # Both of these are already in the cache key, so a draft cannot
            # collide with the final it is standing in for.
            resolution, steps = DRAFT_RESOLUTION, min(int(steps), DRAFT_STEPS)
            print(f"[{TAG}] draft: {DRAFT_RESOLUTION}, {steps} steps", flush=True)
        width, height = _canvas(resolution, aspect)
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
        if 0 < stop_at < n:
            print(f"[{TAG}] render_through={stop_at}: rendering hops 1-{stop_at} "
                  f"of {n}; the rest of the plan is untouched", flush=True)
            blocks = blocks[:stop_at]
            shots = shots[:stop_at]
            n = stop_at
        elif stop_at > n:
            print(f"[{TAG}] render_through={stop_at} is past the end of a "
                  f"{n}-hop plan; rendering all of it", flush=True)

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
        reference_video = (_media.load_video(reference_video_file)
                           if reference_video_file else None)
        voice = _media.load_audio(voice_file) if voice_file else None
        for _name, _got in (("start_image", start_image_file and start_image is None),
                            ("reference_video", reference_video_file and reference_video is None),
                            ("voice", voice_file and voice is None)):
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
            _im = _media.load_image(_r["file"])
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
        pin_renorm_on = str(pin_renorm) == "on"
        pin_noise_v = max(0.0, min(0.10, float(pin_noise)))
        audio_ctx = int(audio_pin_frames) if int(audio_pin_frames) > 0 else int(overlap_n)
        pin_anchor_std = None   # hop 2's pin sets the sigma hops 3+ match
        if pin_renorm_on or pin_noise_v > 0.0:
            print(f"[{TAG}] pin conditioning enabled: "
                  f"renorm={'on' if pin_renorm_on else 'off'} "
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
        # Everything constant across the chain, mixed into every hop key so a
        # resolution or sampler change invalidates the whole cache.
        chain_salt = {
            "w": int(width), "h": int(height), "overlap": overlap_n,
            "sampler": str(sampler_name), "scheduler": str(scheduler),
            "shift_v": float(shift_video), "shift_a": float(shift_audio),
            "ref_size": str(ref_image_size), "pin": str(pin_to_qwen),
            # No "pin_mech" here: the mechanism is decided per hop at runtime
            # in _pin_continue (Motion-Context when a sampler latent exists,
            # AddGuide pixels otherwise), so it belongs in the per-hop key
            # below, not in the chain-wide salt.
            "refs": {s: _store.tensor_digest(t)
                     for s, t in sorted(slot_images.items())},
            "voice": _store.audio_digest(voice),
            "refvid": _store.tensor_digest(reference_video),
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
            if str(hop_script) == "next" and i > 0:
                n_stills = len(base_images or {})
                hop_state_header = _state_header(state, i)
                # With a register wired, only the subject-bearing refs are
                # identities; a setting or prop plate must not be declared one.
                id_ords = None
                n_subj = None
                if hop_active:
                    id_ords = [hop_ords[r["tag"]] for r in hop_active
                               if r["subject"] is not None]
                    # Counted over this hop, not the whole plan. Counting
                    # plan-wide while listing only this hop's ordinals is how a
                    # single scheduled still produced "<Picture 2> are the only
                    # identities" -- the plural that has rendered two people
                    # from one reference.
                    n_subj = len({r["subject"] for r in hop_active
                                  if r["subject"] is not None}) or None
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
                )
                print(f"[{TAG}] hop {i + 1} next-beat assembled "
                      f"(Picture {live_p}, Video {live_v}, "
                      f"{n_stills} identity stills, "
                      f"state_header {len(hop_state_header)} chars, "
                      f"continuity {len(hop_continuity)} chars)", flush=True)

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
                    # Per hop, not chain-wide: hop 2 after a hop-1 cache hit
                    # has no sampler latent and falls back to AddGuide, which
                    # is a different render of the same inputs.
                    "pin_mech": pin_mech_pred,
                    "pin_cond": (pin_renorm_on, round(pin_noise_v, 4), audio_ctx),
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
                    ref_audios=ref_audios,
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
                        pin_latent, pin_anchor_std = _condition_pin_latent(
                            pin_latent, pin_anchor_std,
                            renorm=pin_renorm_on, noise=pin_noise_v,
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
