"""Read and write MiniMax H3 RefMods, and turn one into a DiT reference block.

The file format is Luisa's (ComfyUI-MiniMaxH3Mod, format version 4 standalone,
version 5 bundle read-only). This module does not import that pack. A file
written here is a file her loader can open, and a file she wrote is a file
the rail can read.

The stored tensor is the VAE latent. Decoding it and encoding it again is a
different reference: the pooled latent would be gone. Callers show Qwen a
decode and hand the DiT `visual_block()`.
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
    character rather than a face.

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
                rows.append((mtime, stem, kind))
    rows.sort(reverse=True)
    return [{"name": n, "kind": k} for _m, n, k in rows]


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
    return {"stills": stills, "clip": clip, "voice": voice}


def stored_voice(name):
    """The sound file a saved RefMod names, or `""`.

    A reference row that cites the mod and leaves its own voice empty
    inherits this, so the character is assembled once in the builder and
    the rail only has to point at it.
    """
    return str(stored_plan(name).get("voice") or "")


def _load_tensor(path, key):
    from safetensors.torch import load_file

    # Clone releases the file mapping so Windows can overwrite the file later.
    return load_file(path, device="cpu")[key].clone()


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
        return _as_visual(member, latent, name)
    latent = _load_tensor(path, "latent")
    return _as_visual(meta, latent, name)


def _blur_latent(z, factor=8):
    """Luisa's weakening target: a spatial low-pass, not noise and not zero."""
    if z.dim() != 5:
        _fail(f"cannot blur a RefMod latent of shape {tuple(z.shape)}")
    t, h, w = z.shape[2], z.shape[3], z.shape[4]
    down = F.adaptive_avg_pool3d(z.float(), (t, max(1, h // factor), max(1, w // factor)))
    up = F.interpolate(down, size=(t, h, w), mode="trilinear", align_corners=False)
    return up.to(dtype=z.dtype)


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


def presentation_pixels(vae, latent, cap_mp=0.0):
    """Decode a stored latent into IMAGE pixels for the text encoder.

    Image mods come back `[1,H,W,C]`. Video mods come back `[T,H,W,C]`.
    `cap_mp` shrinks this view only. The latent handed to the DiT is the
    stored one.
    """
    try:
        from . import media as _media
    except ImportError:
        import media as _media

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

def pool_grid(pool, latent_h, latent_w):
    """Even grid. The long edge follows `pool`; the short edge keeps aspect."""
    long_edge = max(2, int(pool) // 2 * 2)
    aspect = float(latent_h) / float(max(1, latent_w))
    if aspect >= 1.0:
        h, w = long_edge, long_edge / aspect
    else:
        w, h = long_edge, long_edge * aspect
    return max(2, round(h / 2) * 2), max(2, round(w / 2) * 2)


def pool_latent(z, latent_t, latent_h, latent_w):
    if latent_h % 2 or latent_w % 2:
        _fail(f"pool grid must be even, got {latent_h}x{latent_w}")
    if z.shape[2] == latent_t and z.shape[3] == latent_h and z.shape[4] == latent_w:
        return z
    pooled = F.adaptive_avg_pool3d(z.float(), (int(latent_t), int(latent_h), int(latent_w)))
    return pooled.to(dtype=z.dtype)


def refine_latent(z_small, z_full, steps):
    """MSE of the upsampled small latent against the frozen encode."""
    steps = int(steps or 0)
    if steps <= 0:
        return z_small
    import torch.nn as nn

    device = z_full.device
    with torch.inference_mode(False), torch.set_grad_enabled(True):
        target = z_full.detach().float().to(device)
        param = nn.Parameter(z_small.detach().float().to(device))
        opt = torch.optim.Adam([param], lr=0.02)
        size = tuple(target.shape[2:])
        for i in range(steps):
            opt.zero_grad()
            up = F.interpolate(param, size=size, mode="trilinear", align_corners=False)
            loss = F.mse_loss(up, target)
            loss.backward()
            opt.step()
            if (i + 1) % 50 == 0 or i + 1 == steps:
                print(f"[{TAG}] refmod refine {i + 1}/{steps}", flush=True)
        return param.detach().to(dtype=z_small.dtype)


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
                 plan=None):
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
        "description": "",
        "concept_type": "generic",
        # Which sources went in, not just how many pixels came out.
        # `source_shape` records the canvas; this records the files, so a
        # mod is self-describing six months later and can be loaded back
        # into the builder to add a still. Same shape `parse_mod_plan`
        # accepts, so the round trip needs no second format. Empty when the
        # legacy sockets built it -- there are no filenames behind a wire.
        "mod_plan": plan if isinstance(plan, dict) else {},
        "_format_version": FORMAT_VERSION,
        "sample_rate": 32000,
    }


def save_latent(directory, name, latent, meta):
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
        save_file({"latent": latent.detach().cpu().contiguous()}, temporary,
                  metadata={META_KEY: json.dumps(meta)})
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
