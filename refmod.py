"""Read and write MiniMax H3 RefMods, and turn one into a DiT reference block.

The file format is Luisa's (ComfyUI-MiniMaxH3Mod, format version 4 standalone,
version 5 bundle read-only). This module does not import that pack. A file
written here is a file her loader can open, and a file she wrote is a file
the rail can read.

The stored tensor is the VAE latent. Decoding it and encoding it again is a
different reference: the pooled latent would be gone. Callers hand the DiT
`visual_block()`, and show Qwen the pictures the builder encoded when the
file keeps them (`looks`), or a decode of the latent when it does not.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile

import torch
import torch.nn.functional as F

TAG = "HandTieClips"
META_KEY = "refmod_meta"
FORMAT_VERSION = 4
# The pictures the builder encoded, uint8 [N,H,W,3] beside `latent`. Luisa's
# loader reads `latent` by key and her bundle writer carries unknown tensors
# through, so the extra key does not cost the file its portability.
LOOKS_KEY = "looks"
SKIP_DIRS = {".git", "__pycache__", "graph_presets"}


def _fail(msg):
    raise ValueError(f"{TAG}: {msg}")

def source_problem(name, kinds=("image", "video")):
    """Why a mod_plan source cannot be used, as a sentence, or None.

    None means nothing is detectably wrong -- present, and a kind this slot
    takes. It does NOT mean a loader will succeed, so a caller that already
    watched one fail supplies its own `or "... could not be read"`. Having
    this return that sentence unconditionally made a perfectly good voice
    fail the build.

    A loader returning None means absent, wrong kind, or unreadable, and
    reporting all three as "not in h3_refs" sent someone looking for a file
    that was sitting right there. A RefMod is visual, so an audio pick is
    the likely mistake and gets told where a voice actually goes.
    """
    try:  # the offline checkers import this module without its package
        from . import media as _media
    except ImportError:
        import media as _media

    if not str(name or "").strip():
        return None
    if _media.resolve(name) is None:
        return f"{name} is not in h3_refs"
    kind = _media.kind_of(name)
    if kind not in kinds:
        if kind == "audio":
            return (f"{name} is a sound file. A RefMod's pictures are "
                    f"encoded; its voice goes in the voice slot.")
        want = " or ".join("picture" if k == "image" else k for k in kinds)
        return f"{name} is a {kind or 'unknown'} file, not a {want}"
    return None


def _ext_kind(name):
    """'image' | 'video' | 'audio' | None. `media.kind_of`, reached without
    importing it, so the parser stays free of the loaders."""
    try:
        from . import media as _media
    except ImportError:
        import media as _media
    return _media.kind_of(name)


def parse_mod_plan(text):
    """`(stills, clip, voice)` from the builder's `mod_plan` widget.

    `stills` is a list of reference filenames in the order they will occupy
    latent frames. `clip` is `{"file", "start", "end", "frames"}` or None.
    An empty widget is `([], None, "")`, which leaves the wired inputs in
    charge.

    `voice` is a sound file in h3_refs, and is the one field here that is
    not encoded into the latent -- a RefMod holds pictures. It is recorded
    so a reference row citing this mod inherits the voice instead of the
    author having to name it twice, which is what makes the file a
    character rather than a face. It may be written as `{"file", "start",
    "end"}` to trim it; `voice_window` reads that window, and this returns
    the name alone so every caller that only needs the file is unchanged.

    A still may be written as a bare string or as `{"file": ...}`; the panel
    writes the object form, a hand-typed plan is allowed either.
    """
    if isinstance(text, (dict, list)):
        data = text
    else:
        raw = (text or "").strip()
        if not raw:
            return [], None, ""
        try:
            data = json.loads(raw)
        except ValueError as exc:
            _fail(f"mod_plan is not valid JSON: {exc}")
    if isinstance(data, list):
        data = {"stills": data}
    if not isinstance(data, dict):
        _fail("mod_plan must be a JSON object or a list of stills")

    def _name(entry, where):
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get("file") or entry.get("name") or ""
        else:
            _fail(f"{where} must be a filename or an object with file")
        name = str(name).strip()
        if not name:
            _fail(f"{where} has no file")
        if "\\" in name or "/" in name or name in (".", ".."):
            _fail(f"{where}: {name!r} must be a plain filename in h3_refs")
        return name

    stills_raw = data.get("stills") or []
    if not isinstance(stills_raw, list):
        _fail("mod_plan stills must be a list")
    stills, seen = [], set()
    for i, entry in enumerate(stills_raw):
        name = _name(entry, f"mod_plan still {i + 1}")
        if name in seen:
            _fail(f"mod_plan lists {name} twice. Each still is one latent frame.")
        seen.add(name)
        stills.append(name)

    clip_raw = data.get("clip")
    clip = None
    if clip_raw not in (None, "", {}, []):
        if isinstance(clip_raw, str):
            clip_raw = {"file": clip_raw}
        if not isinstance(clip_raw, dict):
            _fail("mod_plan clip must be a filename or an object")
        clip = {
            "file": _name(clip_raw, "mod_plan clip"),
            "start": max(0.0, float(clip_raw.get("start") or 0.0)),
            "end": max(0.0, float(clip_raw.get("end") or 0.0)),
            "frames": max(0, int(clip_raw.get("frames") or 0)),
        }
        if clip["end"] and clip["end"] <= clip["start"]:
            _fail(f"mod_plan clip window ends at {clip['end']}s, which is not "
                  f"after its {clip['start']}s start")
        if _ext_kind(clip["file"]) == "audio":
            _fail(f"mod_plan clip {clip['file']} is a sound file. A voice goes "
                  f"in the RefMod's voice slot, not in the clip slot.")

    voice = data.get("voice") or ""
    if voice not in (None, "", {}, []):
        voice = _name(voice, "mod_plan voice")
        if _ext_kind(voice) != "audio":
            _fail(f"mod_plan voice {voice} is not a sound file")
        voice_window(data)  # a reversed window fails here, before any encode
    else:
        voice = ""
    return stills, clip, voice


def tensor_digest(t):
    """Same bytes as store.tensor_digest. Kept here so a file read does not
    import the hop store."""
    a = t.detach().cpu().contiguous().numpy()
    h = hashlib.sha256()
    h.update(str(a.shape).encode())
    h.update(str(a.dtype).encode())
    h.update(a.tobytes())
    return h.hexdigest()[:16]


def register_folder():
    """Make `models/refmods` a folder_paths root. Safe to call more than once."""
    import folder_paths

    base = os.path.join(folder_paths.models_dir, "refmods")
    os.makedirs(base, exist_ok=True)
    try:
        known = [os.path.normcase(os.path.normpath(p))
                 for p in folder_paths.get_folder_paths("refmods")]
    except Exception:
        known = []
    if os.path.normcase(os.path.normpath(base)) not in known:
        folder_paths.add_model_folder_path("refmods", base)
    return base


def roots():
    """Every directory a RefMod may live in, the registered root first."""
    import folder_paths

    try:
        found = list(folder_paths.get_folder_paths("refmods"))
    except Exception:
        found = []
    base = os.path.join(folder_paths.models_dir, "refmods")
    if not found:
        found = [base]
    elif os.path.normcase(os.path.normpath(base)) not in {
            os.path.normcase(os.path.normpath(p)) for p in found}:
        found.insert(0, base)
    return found


def _clean_name(name):
    """A relative safetensors stem. `..` and an absolute path are refused."""
    raw = str(name or "").strip().replace("\\", "/")
    if raw.lower().endswith(".safetensors"):
        raw = raw[: -len(".safetensors")]
    raw = raw.strip("/")
    if not raw:
        return ""
    parts = raw.split("/")
    if any(p in ("", ".", "..") for p in parts):
        _fail(f"refmod name {name!r} is not a file under the refmods folder")
    return "/".join(parts)


def resolve(name):
    """Absolute `.safetensors` path for a refmod name, or None."""
    stem = _clean_name(name)
    if not stem:
        return None
    rel = stem.replace("/", os.sep) + ".safetensors"
    for root in roots():
        base = os.path.normpath(root)
        full = os.path.normpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):
            continue
        if os.path.isfile(full):
            return full
    return None


def listing():
    """`[{name, kind}]` for the editor. `name` has no extension."""
    rows = []
    seen = set()
    for root in roots():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for filename in filenames:
                if not filename.endswith(".safetensors"):
                    continue
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, root).replace("\\", "/")
                stem = rel[: -len(".safetensors")]
                if stem in seen:
                    continue
                seen.add(stem)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                kind = "refmod"
                meta = read_meta(full)
                if isinstance(meta, dict):
                    kind = str(meta.get("kind") or "refmod")
                rows.append((mtime, stem, kind, description_of(meta),
                             _listed_voice(meta)))
    rows.sort(reverse=True)
    # `voice` is what a row citing this mod inherits, so the rail can show
    # and trim the recording that will actually ride.
    return [{"name": n, "kind": k, "description": d, "voice": v}
            for _m, n, k, d, v in rows]


def _listed_voice(meta):
    """The header's voice as `{"file", "start", "end"}`, or None."""
    plan = (meta or {}).get("mod_plan") if isinstance(meta, dict) else None
    if not isinstance(plan, dict):
        return None
    try:
        _stills, _clip, voice = parse_mod_plan(plan)
        start, end = voice_window(plan)
    except ValueError:
        return None
    if not voice:
        return None
    return {"file": voice, "start": start, "end": end}


def description_of(meta):
    """The caption a RefMod carries in its header, or "".

    Luisa's layout has had a `description` field all along; this pack wrote it
    empty until the builder grew one, and other builders fill it. The editor
    offers it as the row's `desc` -- it is never read on the render path, so a
    caption the person has not accepted reaches no prompt. A bundle's own
    field wins, then the member `load_visual` would pick.
    """
    if not isinstance(meta, dict):
        return ""
    text = str(meta.get("description") or "").strip()
    if text or meta.get("kind") != "bundle":
        return text
    for member in meta.get("members") or []:
        if isinstance(member, dict) and member.get("kind") in ("image", "video"):
            return str(member.get("description") or "").strip()
    return ""


def stamp(names):
    """`name:mtime` for IS_CHANGED. A missing file stays a stable token."""
    bits = []
    for name in names:
        if not name:
            continue
        path = resolve(name)
        try:
            bits.append(f"{name}:{os.path.getmtime(path)}" if path else f"{name}:missing")
        except OSError:
            bits.append(f"{name}:missing")
    return "|".join(bits)


def read_meta(path):
    """Header JSON, or None. Does not load the tensor."""
    from safetensors import safe_open

    try:
        with safe_open(path, framework="pt") as handle:
            meta = handle.metadata() or {}
        raw = meta.get(META_KEY)
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def voice_window(plan):
    """`(start, end)` seconds of the mod_plan's voice. `(0, 0)` is the whole file.

    Same sentinel as `media.clip_window`: an end of 0 is the end of the file.
    Only the object form of `voice` carries a window.
    """
    if not isinstance(plan, dict):
        try:
            plan = json.loads(plan) if str(plan or "").strip() else {}
        except ValueError:
            return 0.0, 0.0
    entry = plan.get("voice") if isinstance(plan, dict) else None
    if not isinstance(entry, dict):
        return 0.0, 0.0
    try:
        start = max(0.0, float(entry.get("start") or 0.0))
        end = max(0.0, float(entry.get("end") or 0.0))
    except (TypeError, ValueError):
        _fail("mod_plan voice window must be numbers of seconds")
    if end and end <= start:
        _fail(f"mod_plan voice window ends at {end:g}s, which is not after "
              f"its {start:g}s start")
    return start, end


def stored_plan(name):
    """The `mod_plan` a saved RefMod was built from, or `{}`.

    Empty for a mod built through the sockets, for one written before this
    field existed, or for a file that is not a RefMod at all -- all three
    mean the same thing to a caller: nothing to reload.
    """
    path = resolve(name)
    if path is None:
        return {}
    meta = read_meta(path) or {}
    plan = meta.get("mod_plan")
    if not isinstance(plan, dict):
        return {}
    try:
        stills, clip, voice = parse_mod_plan(plan)
    except ValueError:
        # A header that no longer parses is not worth failing a panel over.
        return {}
    if not (stills or clip or voice):
        return {}
    out = {"stills": stills, "clip": clip, "voice": voice}
    if voice:
        try:
            start, end = voice_window(plan)
        except ValueError:
            start, end = 0.0, 0.0
        if start or end:
            out["voice_start"], out["voice_end"] = start, end
    return out


def stored_voice(name):
    """`{"file", "start", "end"}` for the voice a saved RefMod names, or `{}`.

    A reference row that cites the mod and leaves its own voice empty
    inherits this, window and all, so the character is assembled once in the
    builder and the rail only has to point at it.
    """
    plan = stored_plan(name)
    if not plan.get("voice"):
        return {}
    return {"file": str(plan["voice"]),
            "start": float(plan.get("voice_start") or 0.0),
            "end": float(plan.get("voice_end") or 0.0)}


def _load_tensor(path, key):
    from safetensors.torch import load_file

    # Clone releases the file mapping so Windows can overwrite the file later.
    return load_file(path, device="cpu")[key].clone()


def _load_looks(path):
    """The kept pictures, or None for a file written without them."""
    from safetensors import safe_open

    with safe_open(path, framework="pt", device="cpu") as handle:
        if LOOKS_KEY not in handle.keys():
            return None
        looks = handle.get_tensor(LOOKS_KEY).clone()
    if looks.ndim != 4 or looks.shape[-1] != 3 or looks.shape[0] < 1:
        print(f"[{TAG}] {os.path.basename(path)}: looks tensor is "
              f"{tuple(looks.shape)}, not [N,H,W,3]. Showing Qwen a decode.",
              flush=True)
        return None
    return looks


def _as_visual(meta, latent, where):
    kind = str(meta.get("kind") or "")
    if kind not in ("image", "video"):
        _fail(f"{where} is a '{kind or 'unknown'}' RefMod. This pass reads "
              f"image and video mods.")
    if latent.ndim != 5 or latent.shape[0] != 1 or latent.shape[1] != 24:
        _fail(f"{where} latent must be [1,24,T,H,W], got {tuple(latent.shape)}")
    _b, _c, t, h, w = latent.shape
    if t < 1 or h < 2 or w < 2 or h % 2 or w % 2:
        _fail(f"{where} latent grid {t}x{h}x{w} is not a usable visual ref")
    if kind == "image":
        latent = latent[:, :, :1]
        t = 1
    latent = latent.contiguous()
    return {
        "kind": "image" if t == 1 else "video",
        "latent": latent,
        "latent_t": int(t),
        "latent_h": int(h),
        "latent_w": int(w),
        "name": str(meta.get("name") or where),
        "mode": str(meta.get("mode") or ""),
        "digest": tensor_digest(latent),
        "tokens": int(t) * (int(h) // 2) * (int(w) // 2),
    }


def load_visual(name):
    """The visual latent behind `name`, or None when the file is absent.

    A version-5 bundle contributes its first image or video member. An
    audio-only file is an error: this pass does not apply audio mods.
    """
    path = resolve(name)
    if path is None:
        return None
    meta = read_meta(path)
    if not isinstance(meta, dict):
        _fail(f"{name} has no {META_KEY} header. It is not a RefMod.")
    if meta.get("kind") == "bundle":
        members = meta.get("members") or []
        if not isinstance(members, list):
            _fail(f"{name} bundle has no members list")
        picked = None
        for i, member in enumerate(members):
            if isinstance(member, dict) and member.get("kind") in ("image", "video"):
                picked = (i, member)
                break
        if picked is None:
            _fail(f"{name} has no visual member. Audio RefMods are not applied "
                  f"by this node.")
        index, member = picked
        if index:
            print(f"[{TAG}] {name}: using visual member {index} "
                  f"({member.get('name') or member.get('kind')})", flush=True)
        latent = _load_tensor(path, f"ref_{index}")
        loaded = _as_visual(member, latent, name)
        loaded["looks"] = None
        loaded["looks_digest"] = None
        return loaded
    latent = _load_tensor(path, "latent")
    loaded = _as_visual(meta, latent, name)
    looks = _load_looks(path)
    loaded["looks"] = looks
    loaded["looks_digest"] = tensor_digest(looks) if looks is not None else None
    return loaded


def _blur_latent(z, factor=8):
    """Luisa's weakening target: a spatial low-pass, not noise and not zero."""
    if z.dim() != 5:
        _fail(f"cannot blur a RefMod latent of shape {tuple(z.shape)}")
    t, h, w = z.shape[2], z.shape[3], z.shape[4]
    down = F.adaptive_avg_pool3d(z.float(), (t, max(1, h // factor), max(1, w // factor)))
    up = F.interpolate(down, size=(t, h, w), mode="trilinear", align_corners=False)
    return up.to(dtype=z.dtype)


def refine_cutout(mask, frames, low=0.1, pocket=0.005):
    """A matting model's soft mask, made solid inside and clean at the edge.

    The two halves of a cutout fail differently, so they are treated apart.

    Inside, a matting model is unsure as well as at the edge -- on a
    character sheet the robes came back as grey patches, and every grey
    pixel is subject half-lost. So the question is turned round: backdrop
    is what is confidently empty (`< low`) AND touches the frame's border
    or is a pocket larger than `pocket` of it. Everything else, shrunk
    back from its outline, is a core held at full weight.

    At the edge, the model's own soft alpha is kept -- that is what it is
    good at -- except that on a plain sheet an edge pixel the colour of
    the sheet is let go. That removes the white rim a cutout leaves round
    a figure, the thin panel dividers a sheet grid is drawn with, and the
    wedges of sheet caught between hair and shoulder. What the model was
    sure of inside the figure is never keyed, so a white collar stays.
    """
    import numpy as np  # noqa: PLC0415
    from scipy import ndimage  # noqa: PLC0415

    out = []
    for raw_t, px_t in zip(mask, frames):
        raw = raw_t.float().cpu().numpy()
        px = px_t[..., :3].float().cpu().numpy()
        labels, count = ndimage.label(raw < low)
        if count:
            edge = np.unique(np.concatenate(
                [labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
            sizes = np.bincount(labels.ravel(), minlength=count + 1)
            backdrop_ids = set(int(i) for i in edge if i) | set(
                int(i) for i in np.nonzero(sizes >= pocket * raw.size)[0] if i)
            backdrop = np.isin(labels, sorted(backdrop_ids))
        else:
            backdrop = np.zeros_like(raw, dtype=bool)
        inset = max(1, round(min(raw.shape) / 100))
        core = ndimage.binary_erosion(~backdrop, iterations=inset)
        m = np.maximum(raw, core.astype(np.float32))

        sure = backdrop & (raw < 0.02)
        if sure.sum() > 0.01 * raw.size:
            sheet = np.median(px[sure], axis=0)
            spread = float(np.abs(px[sure] - sheet).mean())
            if spread < 0.06:  # a plain sheet, not a photographed room
                near = np.abs(px - sheet).max(axis=-1)
                key = np.clip((near - 0.04) / 0.12, 0.0, 1.0)
                m = np.where(core & (raw >= 0.9), m, m * key)
        out.append(torch.from_numpy(m.astype(np.float32)))
    return torch.stack(out)


def pull_push_fill(frames, mask):
    """Every pixel coloured from the subject nearest it, smoothly.

    The standard pull-push hole fill: pool colour-times-weight and weight
    down to one pixel, then walk back up, keeping a level's own colour in
    proportion to how much subject it holds and taking the coarser guess
    for the rest. The backdrop ends up a soft extension of the subject's
    own edge colours -- no structure of its own, and none of its tone.
    """
    c = frames[..., :3].float().movedim(-1, 1)          # [T, 3, H, W]
    w = mask.float().to(c.device).unsqueeze(1)          # [T, 1, H, W]
    colours, weights = [c * w], [w]
    while max(colours[-1].shape[-2:]) > 1:
        colours.append(F.avg_pool2d(colours[-1], 2, ceil_mode=True))
        weights.append(F.avg_pool2d(weights[-1], 2, ceil_mode=True))
    fill = colours[-1] / weights[-1].clamp_min(1e-6)
    for cl, wl in zip(reversed(colours[:-1]), reversed(weights[:-1])):
        coarse = F.interpolate(fill, size=cl.shape[-2:], mode="bilinear",
                               align_corners=False)
        own = cl / wl.clamp_min(1e-6)
        fill = wl * own + (1.0 - wl) * coarse
    return fill.movedim(1, -1).clamp(0.0, 1.0)


def isolate_subject(frames, mask, retention=0.0):
    """`[T,H,W,C]` canvas frames with the backdrop replaced, before encode.

    Done on pixels, not on the latent, for three reasons. A latent blend
    keeps the backdrop's tone -- a blurred white sheet is still white --
    and a blur taken over the whole latent smears the subject outward
    across its own edge. And a mask pooled onto the 16x latent grid loses
    anything thinner than a cell, which on a sheet is hairpins and loose
    strands. Filled in pixel space, the VAE encodes one ordinary picture
    with a clean edge, and the decode Qwen is shown is the same picture.

    `retention` is how much of the original backdrop is kept: 0 replaces
    it, 1 changes nothing. A mask with one frame covers every frame.
    """
    m = mask.float().to(frames.device)
    if m.shape[0] == 1 and frames.shape[0] > 1:
        m = m.expand(frames.shape[0], -1, -1)
    if tuple(m.shape) != tuple(frames.shape[:3]):
        _fail(f"subject mask {tuple(m.shape)} does not cover frames "
              f"{tuple(frames.shape[:3])}")
    r = min(1.0, max(0.0, float(retention)))
    if r >= 1.0 or float(m.amax()) <= 0.0:
        return frames
    px = frames[..., :3].float()
    keep = (r + (1.0 - r) * m).unsqueeze(-1)
    out = keep * px + (1.0 - keep) * pull_push_fill(px, m)
    return out.to(frames.dtype)


def mask_to_canvas(mask, tw, th):
    """`[T,H,W]` MASK stretched onto the canvas the way `to_canvas` stretches pixels."""
    import comfy.utils

    if mask.dim() == 2:
        mask = mask.unsqueeze(0)
    if mask.dim() != 3:
        _fail(f"a subject mask must be [T,H,W], got {tuple(mask.shape)}")
    m = comfy.utils.common_upscale(mask.float().unsqueeze(1), int(tw), int(th),
                                   "bilinear", "disabled")
    return m.squeeze(1).clamp(0.0, 1.0)


CUTOUT_MODEL = "isnet-general-use"
_cutout_session = None


def cutout_masks(frames):
    """`[T,H,W]` subject masks for `[T,H,W,C]` canvas frames, by rembg.

    rembg is optional -- ComfyUI does not ship it, comfyui_essentials
    installs it -- so it is imported here and a missing install fails the
    build with the fix rather than breaking the node on load. The first use
    downloads the model into rembg's own folder (~/.u2net).
    """
    global _cutout_session
    try:
        from rembg import new_session, remove  # noqa: PLC0415
    except ImportError:
        _fail("subject_mask=auto needs rembg (pip install rembg), or wire a "
              "MASK into the builder's mask socket")
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    if _cutout_session is None:
        _cutout_session = new_session(CUTOUT_MODEL)
    out = []
    for frame in frames:
        px = (frame[..., :3].float().clamp(0, 1) * 255).round().byte().cpu().numpy()
        m = remove(Image.fromarray(px), session=_cutout_session, only_mask=True)
        out.append(torch.from_numpy(np.asarray(m.convert("L"), dtype=np.float32) / 255.0))
    return torch.stack(out)


def apply_strength(latent, strength):
    """`w * latent + (1-w) * blur`. `1` returns the same tensor."""
    w = float(strength)
    if w >= 1.0:
        return latent
    if w <= 0.0:
        return None
    return w * latent + (1.0 - w) * _blur_latent(latent)


def visual_block(loaded, strength):
    """The `minimax_refs` entry for this mod, or None when strength drops it."""
    latent = apply_strength(loaded["latent"], strength)
    if latent is None:
        return None
    block = {
        "kind": loaded["kind"],
        "latent_h": int(latent.shape[3]),
        "latent_w": int(latent.shape[4]),
        "latent": latent,
    }
    if loaded["kind"] == "video":
        block["latent_t"] = int(latent.shape[2])
        block["ref_audio_t"] = 0
        block["audio_latent"] = None
    return block


def qwen_stride():
    """Frames between the samples core shows Qwen of a reference video.

    Core's `FPS // 2`: a reference clip reaches the text encoder at 2 fps.
    Read from core so a change there moves this with it.
    """
    try:
        from comfy_extras.nodes_minimax_h3 import FPS
    except Exception:
        FPS = 24
    return max(1, int(FPS) // 2)


def looks_from(canvas, clip=False):
    """The pictures to keep for Qwen from one encoded source, as uint8.

    A still keeps itself. A clip keeps the frames core would sample for Qwen
    anyway, so a mod does not carry a whole clip's pixels to show two a
    second of it.
    """
    frames = canvas[::qwen_stride()] if clip else canvas
    return (frames[..., :3].float().clamp(0, 1) * 255.0).round().to(torch.uint8).cpu()


def hold_looks(looks):
    """Lay the kept pictures out as a reference clip Qwen sees one each of.

    Core snaps a reference video down to 17k+5 frames and samples it every
    `qwen_stride()` frames, so each picture is held for one stride and the
    last is held until the snapped length still reaches it. The last picture
    may be sampled twice; no earlier one is skipped.
    """
    stride = qwen_stride()
    n = int(looks.shape[0])
    need = max(5, stride * (n - 1) + 1)
    length = 5
    while length < need:
        length += 17
    idx = torch.clamp(torch.arange(length) // stride, max=n - 1)
    return looks[idx]


def presentation_pixels(vae, latent, cap_mp=0.0, looks=None, kind="image"):
    """IMAGE pixels for the text encoder.

    With `looks`, the pictures the builder encoded: sharp, one Qwen frame per
    picture. Without them, a decode of the stored latent -- the only view a
    mod written elsewhere, or before looks existed, can give. A decode of a
    stacked multi-still latent is also a clip the VAE invents between the
    stills, which is not what anybody photographed.

    Image mods come back `[1,H,W,C]`. Video mods come back `[T,H,W,C]`.
    `cap_mp` shrinks this view only. The latent handed to the DiT is the
    stored one.
    """
    try:
        from . import media as _media
    except ImportError:
        import media as _media

    if looks is not None:
        view = looks[:1] if kind == "image" else hold_looks(looks)
        return _cap(view.float() / 255.0, cap_mp, _media)
    pixels = vae.decode(latent)
    if pixels.ndim == 5 and pixels.shape[0] == 1 and pixels.shape[1] == 1:
        pixels = pixels[:, 0]
    elif pixels.ndim == 5 and pixels.shape[0] == 1:
        pixels = pixels[0]
    elif pixels.ndim == 5:
        pixels = pixels.reshape(-1, pixels.shape[-3], pixels.shape[-2], pixels.shape[-1])
    if pixels.ndim != 4 or pixels.shape[-1] != 3:
        _fail(f"VAE decode of a RefMod came back as {tuple(pixels.shape)}, "
              f"which is not an image batch")
    return _cap(pixels, cap_mp, _media)


def _cap(pixels, cap_mp, _media):
    h, w = int(pixels.shape[1]), int(pixels.shape[2])
    tw, th = _media._mp_cap_size(w, h, cap_mp)
    if (tw, th) != (w, h):
        samples = pixels.movedim(-1, 1).float()
        samples = F.interpolate(samples, size=(th, tw), mode="bicubic", align_corners=False)
        pixels = samples.movedim(1, -1).clamp_(0, 1)
    return pixels


def splice_blocks(cond, hop_images, hop_videos, image_ids, video_ids):
    """Replace the blocks `ReferenceToVideo` just encoded for RefMod slots.

    `image_ids` / `video_ids` map `id(pixel tensor)` to a `visual_block`.
    Picture order is the dict order core walks. Video blocks follow every
    image block, which is the order core builds `minimax_refs`.
    """
    import node_helpers

    images = list((hop_images or {}).values())
    videos = list((hop_videos or {}).values())
    # The maps are built once per run. A later hop that does not cite the
    # mod has no pixel of ours, and ReferenceToVideo then emits no
    # minimax_refs. That hop is finished. Splicing it was the hop-2 crash.
    if not any(id(t) in (image_ids or {}) for t in images) and not any(
            id(t) in (video_ids or {}) for t in videos):
        return cond
    blocks = None
    for entry in cond or []:
        meta = entry[1] if len(entry) > 1 else None
        if isinstance(meta, dict) and meta.get("minimax_refs"):
            blocks = meta["minimax_refs"]
            break
    if not blocks:
        _fail("This hop cites a RefMod but the conditioning has no "
              "minimax_refs. The video VAE has to be connected.")
    blocks = [dict(b) for b in blocks]

    def _place(index, block, expected):
        if index >= len(blocks):
            _fail(f"RefMod block {index} is past the {len(blocks)} reference "
                  f"blocks the encoder built")
        got = blocks[index].get("kind")
        if expected == "image" and got != "image":
            _fail(f"reference block {index} is '{got}', not the image a RefMod "
                  f"still has to replace")
        if expected == "video" and got not in ("video", "video_audio"):
            _fail(f"reference block {index} is '{got}', not the video a RefMod "
                  f"clip has to replace")
        placed = dict(block)
        old = blocks[index].get("latent")
        if torch.is_tensor(old) and torch.is_tensor(placed.get("latent")):
            placed["latent"] = placed["latent"].to(device=old.device, dtype=old.dtype)
        # Carry the encoded audio across. `visual_block` builds a silent
        # block -- it only ever sees a latent from a file, and an audio mod
        # is not applied here -- so replacing wholesale would throw away the
        # soundtrack the encoder just made from the row's `voice`. The voice
        # would load, encode, and vanish: a failure with no error anywhere.
        if got == "video_audio":
            placed["kind"] = "video_audio"
            placed["ref_audio_t"] = blocks[index].get("ref_audio_t", 0)
            placed["audio_latent"] = blocks[index].get("audio_latent")
        blocks[index] = placed

    for i, tensor in enumerate(images):
        block = image_ids.get(id(tensor))
        if block is not None:
            _place(i, block, "image")
    n_img = len(images)
    for j, tensor in enumerate(videos):
        block = video_ids.get(id(tensor))
        if block is not None:
            _place(n_img + j, block, "video")
    return node_helpers.conditioning_set_values(cond, {"minimax_refs": blocks})


# -- builder math ----------------------------------------------------------

def fit_token_budget(latent, budget, label):
    """Drop near-duplicate frames, then resample time, until tokens fit."""
    budget = int(budget or 0)
    if budget <= 0:
        return latent
    h, w = int(latent.shape[3]), int(latent.shape[4])
    per = (h // 2) * (w // 2)
    t = int(latent.shape[2])
    if per > budget:
        _fail(f"{label}: one frame costs {per} tokens, above the {budget} budget. "
              f"Lower the pool size.")
    if per * t <= budget:
        return latent
    flat = latent[0].float()
    kept = [0]
    prev = flat[:, 0]
    for i in range(1, t):
        cur = flat[:, i]
        denom = (cur.abs().mean() + prev.abs().mean()) / 2 + 1e-6
        if (cur - prev).abs().mean() / denom >= 0.02:
            kept.append(i)
            prev = cur
    if len(kept) < t:
        latent = latent[:, :, kept]
        t = int(latent.shape[2])
    if per * t > budget:
        fit_t = max(1, budget // per)
        idx = torch.linspace(0, t - 1, fit_t).round().long()
        latent = latent[:, :, idx]
    return latent


def metadata_for(name, kind, latent, mode, source, source_shape, pool, steps,
                 plan=None, description="", mask=None, looks=0):
    t, h, w = (int(latent.shape[2]), int(latent.shape[3]), int(latent.shape[4]))
    return {
        "name": name,
        "kind": kind,
        "latent_h": h,
        "latent_w": w,
        "latent_t": t,
        "mode": mode,
        "source": source,
        "source_shape": source_shape,
        "pool": pool,
        "optimize_steps": int(steps or 0),
        "tags": [],
        "description": str(description or "").strip(),
        "concept_type": "generic",
        # Which sources went in, not just how many pixels came out.
        # `source_shape` records the canvas; this records the files, so a
        # mod is self-describing six months later and can be loaded back
        # into the builder to add a still. Same shape `parse_mod_plan`
        # accepts, so the round trip needs no second format. Empty when the
        # legacy sockets built it -- there are no filenames behind a wire.
        "mod_plan": plan if isinstance(plan, dict) else {},
        # How the background was suppressed, if it was: `{"source":
        # "auto"|"wired", "background_retention": r}`. Empty is unmasked.
        "subject_mask": mask if isinstance(mask, dict) else {},
        # How many pictures the `looks` tensor holds. 0: Qwen is shown a
        # decode of the latent.
        "looks": int(looks or 0),
        "_format_version": FORMAT_VERSION,
        "sample_rate": 32000,
    }


def save_latent(directory, name, latent, meta, looks=None):
    """Atomic write. Returns the stem the rail stores in `file`."""
    from safetensors.torch import save_file

    stem = _clean_name(name)
    if not stem:
        _fail("refmod name is empty")
    directory = os.path.normpath(directory)
    dest = os.path.join(directory, stem.replace("/", os.sep) + ".safetensors")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".refmod-", suffix=".tmp",
                                     dir=os.path.dirname(dest) or ".")
    os.close(fd)
    try:
        tensors = {"latent": latent.detach().cpu().contiguous()}
        if looks is not None:
            tensors[LOOKS_KEY] = looks.to(torch.uint8).cpu().contiguous()
        save_file(tensors, temporary, metadata={META_KEY: json.dumps(meta)})
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return stem


def canvas_for_hw(h, w, short=1024):
    """The `(tw, th)` a source of this size lands on. Never upscales.

    Split out from `canvas_size` so the panel's estimate can ask about a
    file's dimensions without decoding it, and get the same answer the
    build will.
    """
    h, w = int(h), int(w)
    if h < 1 or w < 1:
        _fail(f"a source cannot be {w}x{h}")
    scale = 1.0 if min(h, w) <= short else short / float(min(h, w))
    tw = max(32, int(round(w * scale) / 32) * 32)
    th = max(32, int(round(h * scale) / 32) * 32)
    tw = min(tw, max(32, w // 32 * 32))
    th = min(th, max(32, h // 32 * 32))
    return tw, th


def canvas_size(frames, short=1024):
    """The `(tw, th)` `fit_canvas` would pick for `[T,H,W,C]`."""
    if frames.ndim != 4 or frames.shape[-1] < 3:
        _fail(f"builder frames must be [T,H,W,C], got {tuple(frames.shape)}")
    return canvas_for_hw(int(frames.shape[1]), int(frames.shape[2]), short)


def frame_tokens(tw, th, factor=16):
    """DiT tokens one latent frame of this canvas costs.

    The H3 VAE is 16x spatial and the DiT patchifies 2x2, so a 1024x1280
    canvas is an 80x64 latent and 1280 tokens -- which is what a real
    built mod measures.
    """
    return max(1, (int(th) // factor // 2) * (int(tw) // factor // 2))


def to_canvas(frames, tw, th):
    """Resize `[T,H,W,C]` onto an exact canvas.

    Every source in one mod goes through this with the same `(tw, th)`,
    because latents only concatenate along time when H and W agree.
    """
    import comfy.utils

    if frames.ndim != 4 or frames.shape[-1] < 3:
        _fail(f"builder frames must be [T,H,W,C], got {tuple(frames.shape)}")
    samples = frames[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, int(tw), int(th), "lanczos", "disabled")
    return samples.movedim(1, -1)


def fit_canvas(frames, short=1024):
    """Resize `[T,H,W,C]` onto the 32px grid, short edge at most `short`.

    Never upscales. A still and a clip share this so the VAE sees the same
    class of canvas the reference pipeline does.
    """
    tw, th = canvas_size(frames, short)
    return to_canvas(frames, tw, th)


def stack_latents(parts, label):
    """Concatenate `[1,24,T,H,W]` encodes along time into one visual mod.

    This is how several stills and a clip become a single RefMod rather
    than a version-5 bundle: `load_visual` reads one latent, and `_as_visual`
    already calls anything with `T > 1` a video mod. A bundle would hand the
    rail its first member and drop the rest.
    """
    parts = [p for p in parts if p is not None]
    if not parts:
        _fail(f"{label}: nothing to encode")
    head = parts[0]
    for i, p in enumerate(parts[1:], start=1):
        if p.shape[3:] != head.shape[3:]:
            _fail(f"{label}: source {i} encoded to {tuple(p.shape[2:])}, which "
                  f"does not stack onto {tuple(head.shape[2:])}. Sources must "
                  f"share a canvas.")
    if len(parts) == 1:
        return parts[0]
    return torch.cat([p.to(dtype=head.dtype, device=head.device) for p in parts], dim=2)


def snap_video_frames(frames):
    """Core's reference-video length: at least 5, and `n % 17 == 5`."""
    n = int(frames.shape[0])
    if n < 5:
        _fail(f"a video RefMod needs at least 5 frames (got {n})")
    while n % 17 != 5:
        n -= 1
    if n < 5:
        _fail(f"a video RefMod needs at least 5 frames after the 17k+5 snap "
              f"(started at {int(frames.shape[0])})")
    return frames[:n]
