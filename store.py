"""Disk-backed hop store: resume, single-shot re-roll, and lower peak RAM.

Hops are causally dependent -- hop N is rendered from hop N-1's tail -- so the
cache key **chains**: each hop's key mixes in the previous hop's key. Editing
shot 1 therefore invalidates 2..N automatically, which is correct and must be
surfaced in the UI, because otherwise it reads as a bug.

**Why 16-bit.** A cached hop's last frame becomes the next hop's Qwen pin and
its AddGuide guide. Round-tripping float32 through 8-bit would make a resumed
chain diverge from an uninterrupted one -- the cache would change the output,
which defeats the point. FFV1 at `rgb48le` keeps ~16 bits per channel, which is
far below the VAE's own noise floor, so a resumed hop is indistinguishable from
a fresh one. The cost is roughly 2x the bytes of an 8-bit lossless encode, and
FFV1 still compresses it well.

**On the RAM claim.** The node's IMAGE output is the whole clip, so the final
tensor is unavoidably full size. What the store removes is the *double and
triple buffering* during the loop: today `master_imgs` grows by concatenation
(which allocates a new full-size tensor every hop) while `prev_imgs` and `imgs`
are also live. Streaming to disk keeps one hop plus the overlap tail resident
and concatenates once at the end.
"""

import hashlib
import json
import os
import time

import av
import numpy as np
import safetensors
import torch
from safetensors.torch import save_file as _st_save

from . import latents as _latents

TAG = "HandTieClips"

VIDEO_EXT = ".mkv"
AUDIO_EXT = ".npy"
META_EXT = ".json"
LATENT_EXT = ".latent.safetensors"
# What 1.1 and earlier wrote. It is never READ -- reading it back needs
# weights_only=False, which is the pickle load this pack retired on purpose --
# but it still has to be ACCOUNTED for. A cache carried across the upgrade has
# one of these beside every entry, and a cleanup path that does not know about
# a file is how the master spill leaked 9 GB a render: the sweep sized entries
# from the three current extensions, so a legacy sidecar was invisible to the
# budget AND survived the eviction of the entry it belonged to. About 15 MB
# each, forever, under a budget that could not see them.
LEGACY_LATENT_EXT = ".latent.pt"

# FFV1 through PyAV, in process. This used to shell out to an `ffmpeg` binary
# on PATH, which cost two things:
#
#   1. The cache -- the feature that makes a tone A/B 14 seconds instead of
#      164 -- hard-failed with a RuntimeError for anyone who did not happen to
#      have ffmpeg installed. ComfyUI itself never needs it on PATH, so that is
#      most users, and the failure landed on the pack's fastest path.
#   2. The Comfy registry's YARA scan flags EVERY spawn of an external command
#      from a custom node, whatever it spawns, with no taint analysis -- so a
#      static argument list and no shell still flagged all three published
#      versions. (The rule's own name and the call forms it matches are
#      deliberately not written here: this file ships, and the scan reads a
#      comment exactly as it reads code. 1.0.2 was Flagged for a class name
#      quoted in a markdown file while explaining why it had been Flagged.)
#
# `av` is a hard dependency of ComfyUI itself -- SaveVideo and CreateVideo are
# built on it -- so this trades an optional external binary for a library that
# is already guaranteed present.
#
# The format is deliberately UNCHANGED: ffv1 / rgb48le / level 3 / coder 1 /
# context 1, in matroska. Verified bit-exact in both directions, so caches
# written by the old path stay readable and a resumed chain still matches an
# uninterrupted one.
FFV1_OPTIONS = {"level": "3", "coder": "1", "context": "1"}
PIX_FMT = "rgb48le"


def tensor_digest(t):
    """Cheap, order-sensitive digest of a tensor's actual bytes."""
    if t is None:
        return "none"
    a = t.detach().cpu().contiguous().numpy()
    h = hashlib.sha256()
    h.update(str(a.shape).encode())
    h.update(str(a.dtype).encode())
    h.update(a.tobytes())
    return h.hexdigest()[:16]


def audio_digest(a):
    """Digest an AUDIO input -- ``{"waveform": Tensor, "sample_rate": int}``.

    AUDIO is a dict, not a tensor, so `tensor_digest` cannot take it: `.detach`
    on a dict raises AttributeError, which is what wiring `voice` with the hop
    cache on used to do before hop 1 ever started. Sample rate is part of the
    identity -- the same waveform at a different rate is different audio.
    """
    if a is None:
        return "none"
    if not isinstance(a, dict):
        return tensor_digest(a)
    h = hashlib.sha256()
    h.update(tensor_digest(a.get("waveform")).encode())
    h.update(str(a.get("sample_rate")).encode())
    return h.hexdigest()[:16]


def hop_key(prev_key, payload):
    """Chained content key. `payload` must contain everything that changes pixels.

    Anything omitted here is something the cache will fail to notice, so err
    toward including it.
    """
    h = hashlib.sha256()
    h.update((prev_key or "root").encode())
    h.update(json.dumps(payload, sort_keys=True, default=str).encode())
    return h.hexdigest()[:24]


def _latent_to_flat(latent):
    """A latent dict -> (flat tensor dict, structure metadata), or None.

    The sidecar used to be `torch.save`, which means a pickle, which means the
    read had to pass `weights_only=False`. That is the single most obvious
    thing a registry scanner reaches for after the child-process work in
    0.4.1-0.4.3, and it is unnecessary here: `samples` is the only awkward
    member, and `latents.parts()` already decomposes it into plain tensors
    because the pin levers needed exactly that.

    Returns None when anything is not representable, which the caller already
    treats as "cache the frames, skip the latent" -- the documented
    best-effort behaviour, not a new failure mode.
    """
    if not isinstance(latent, dict) or "samples" not in latent:
        return None
    x = latent["samples"]
    parts = _latents.parts(x)
    if not parts:
        return None

    flat, extra = {}, {}
    for i, t in enumerate(parts):
        if not isinstance(t, torch.Tensor):
            return None
        # .clone() because safetensors refuses tensors that share storage, and
        # unbind() on some containers hands back views of one buffer.
        flat[f"samples.{i}"] = t.detach().cpu().contiguous().clone()

    if isinstance(x, torch.Tensor):
        container = {"kind": "tensor"}
    else:
        # `parts()` recognises exactly two shapes, so the header names which one
        # rather than a class path to import. An importable name would be more
        # general and is not worth it: dynamic import reads to the registry
        # scanner as bytecode manipulation, and the generality buys nothing
        # `parts()` can actually produce.
        cls = type(x)
        container = {"kind": "nested",
                     "cls": f"{cls.__module__}.{cls.__qualname__}"}

    nested_extra = {}
    for k, v in latent.items():
        if k == "samples":
            continue
        if isinstance(v, torch.Tensor):
            flat[f"extra.{k}"] = v.detach().cpu().contiguous().clone()
        elif isinstance(v, (str, int, float, bool)) or v is None:
            extra[k] = v
        else:
            # A member can be nested for exactly the reason `samples` is: this
            # is a joint AV latent, so anything shaped like it -- `noise_mask`
            # above all -- carries one tensor per stream. `master_audio_file`
            # sets a NestedTensor mask (ones on video, zeros on audio), and
            # refusing it here meant EVERY locked hop logged "not representable
            # without pickling" and cached no latent. A later hit then left
            # `prev_sampled` empty, the hop after it fell back to the AddGuide
            # pixel pin, and cache_hops=on became worse than off -- precisely
            # the failure this sidecar exists to prevent, reintroduced by a
            # feature that shipped after it.
            v_parts = _latents.parts(v)
            if not v_parts or not all(isinstance(t, torch.Tensor)
                                      for t in v_parts):
                # Still refused. Silently dropping a key would make a restored
                # latent quietly different from the one that was stored, which
                # is the whole class of bug the hop cache exists to avoid.
                return None
            for i, t in enumerate(v_parts):
                flat[f"nested.{k}.{i}"] = t.detach().cpu().contiguous().clone()
            cls = type(v)
            nested_extra[k] = {"n": len(v_parts),
                               "cls": f"{cls.__module__}.{cls.__qualname__}"}

    meta = {"v": 1, "n": len(parts), "container": container, "extra": extra,
            "nested": nested_extra}
    return flat, {"htc": json.dumps(meta, separators=(",", ":"))}


def _latent_from_flat(flat, meta_json):
    """Inverse of `_latent_to_flat`. -> latent dict, or None if unreadable."""
    meta = json.loads(meta_json)
    if int(meta.get("v", 0)) != 1:
        return None
    n = int(meta["n"])
    parts = [flat[f"samples.{i}"] for i in range(n)]

    c = meta["container"]
    if c["kind"] == "tensor":
        samples = parts[0]
    elif c["kind"] == "nested":
        # Static import, deliberately. If core ever renames or moves this, the
        # import raises, the caller logs it and falls back to the pixel pin --
        # a loud, already-handled failure, which is the right trade against a
        # dynamic import that a scanner reads as bytecode manipulation.
        from comfy.nested_tensor import NestedTensor
        if c.get("cls") != f"{NestedTensor.__module__}.{NestedTensor.__qualname__}":
            return None
        samples = NestedTensor(parts)
    else:
        return None

    out = {"samples": samples}
    out.update(meta.get("extra") or {})
    for k, v in flat.items():
        if k.startswith("extra."):
            out[k[len("extra."):]] = v

    # Nested members, rebuilt the same way `samples` was and refused on the
    # same terms -- a restored latent that quietly lost its noise_mask would
    # denoise the audio it was supposed to freeze.
    for k, spec in (meta.get("nested") or {}).items():
        from comfy.nested_tensor import NestedTensor
        if spec.get("cls") != f"{NestedTensor.__module__}.{NestedTensor.__qualname__}":
            return None
        try:
            out[k] = NestedTensor([flat[f"nested.{k}.{i}"]
                                   for i in range(int(spec["n"]))])
        except KeyError:
            return None
    return out


class HopStore:
    def __init__(self, root, budget_gb=20.0, fps=24):
        self.root = str(root)
        self.budget = float(budget_gb) * (1024 ** 3)
        self.fps = int(fps)
        os.makedirs(self.root, exist_ok=True)

    # -- paths ------------------------------------------------------------
    def _p(self, key, ext):
        return os.path.join(self.root, key + ext)

    def has(self, key):
        return all(os.path.exists(self._p(key, e))
                   for e in (VIDEO_EXT, AUDIO_EXT, META_EXT))

    # -- write ------------------------------------------------------------
    def put(self, key, imgs, wav, sr, meta=None, latent=None):
        """imgs: float [N,H,W,3] in 0..1 on cpu. wav: float [.., C, S].

        `latent` is this hop's sampler output and is optional; see the comment
        at the write below for why storing it is what makes the cache useful
        past hop 1.
        """
        n, hgt, wid = int(imgs.shape[0]), int(imgs.shape[1]), int(imgs.shape[2])
        # The .part suffix defeats extension-based format detection, so the
        # muxer is named explicitly. Writing to .part and renaming on success
        # keeps a killed render from leaving a half-file that `has()` trusts.
        vid_tmp = self._p(key, VIDEO_EXT + ".part")
        try:
            container = av.open(vid_tmp, mode="w", format="matroska")
            try:
                stream = container.add_stream("ffv1", rate=self.fps)
                stream.width, stream.height = wid, hgt
                stream.pix_fmt = PIX_FMT
                stream.options = dict(FFV1_OPTIONS)
                # Frame at a time: never materialise a second full-size copy.
                for i in range(n):
                    f = (imgs[i].clamp(0, 1) * 65535.0).round().to(torch.int32)
                    frame = av.VideoFrame.from_ndarray(
                        f.numpy().astype("<u2"), format=PIX_FMT)
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():          # flush the encoder
                    container.mux(packet)
            finally:
                container.close()
        except Exception as e:                          # noqa: BLE001
            # Leave no .part behind for the next run to trip over.
            try:
                os.remove(vid_tmp)
            except OSError:
                pass
            raise RuntimeError(f"{TAG}: FFV1 encode failed for hop {key}: {e}") from e
        os.replace(vid_tmp, self._p(key, VIDEO_EXT))

        np.save(self._p(key, AUDIO_EXT),
                wav.detach().cpu().contiguous().numpy().astype(np.float32))

        # The sampler latent, not just the decoded frames. Without it a cache
        # hit leaves the caller's `prev_sampled` empty, so the *next* hop
        # predicts the AddGuide pixel fallback instead of Motion-Context --
        # which is part of its key, so that key stops matching what was stored.
        # The cache could therefore never hit past hop 1, and the hop after a
        # hit was joined by the inferior mechanism, making `cache_hops=on`
        # actively worse than off. Optional and best-effort: a hop whose latent
        # will not serialise is still worth caching for its frames, it just
        # cannot seed a latent join.
        if latent is not None:
            tmp = self._p(key, LATENT_EXT + ".part")
            try:
                packed = _latent_to_flat(latent)
                if packed is None:
                    raise ValueError(
                        "latent is not representable without pickling "
                        "(unrecognised samples container, or a non-scalar "
                        "non-tensor member)")
                flat, st_meta = packed
                _st_save(flat, tmp, metadata=st_meta)
                os.replace(tmp, self._p(key, LATENT_EXT))
            except Exception as e:  # noqa: BLE001
                print(f"[{TAG}] hop {key[:8]}: latent not cached ({e!r}); a hit "
                      f"on this hop will fall back to the pixel pin", flush=True)
                try:
                    os.remove(tmp)
                except OSError:
                    pass

        info = dict(meta or {})
        info.update({"frames": n, "height": hgt, "width": wid,
                     "sample_rate": int(sr), "fps": self.fps,
                     "written": time.time()})
        with open(self._p(key, META_EXT), "w", encoding="utf-8") as fh:
            json.dump(info, fh, indent=1)
        return info

    # -- read -------------------------------------------------------------
    def _get_latent(self, key):
        """The stored sampler latent, or None when this entry has none.

        None is always safe: the caller falls back to the pixel pin, which is
        what every cache hit did before latents were stored. Entries written by
        an older build simply have no sidecar, so they keep working.
        """
        path = self._p(key, LATENT_EXT)
        if not os.path.exists(path):
            return None
        try:
            # safetensors, deliberately: the previous format was torch.save,
            # and reading it back needed weights_only=False -- a pickle load of
            # a file on disk, which is the next thing a registry scanner
            # reaches for and is not something this pack needs. Nothing here is
            # unrepresentable as plain tensors plus a small JSON header.
            with safetensors.safe_open(path, framework="pt", device="cpu") as fh:
                meta = (fh.metadata() or {}).get("htc")
                if meta is None:
                    raise ValueError("sidecar has no htc header")
                flat = {k: fh.get_tensor(k) for k in fh.keys()}
            out = _latent_from_flat(flat, meta)
            if out is None:
                raise ValueError("sidecar header is a version this build "
                                 "does not read")
            return out
        except Exception as e:  # noqa: BLE001
            print(f"[{TAG}] hop {key[:8]}: cached latent unreadable ({e!r}); "
                  f"falling back to the pixel pin", flush=True)
            return None

    def get(self, key):
        """-> (imgs float32 [N,H,W,3], wav, sample_rate, latent|None) or None."""
        if not self.has(key):
            return None
        with open(self._p(key, META_EXT), encoding="utf-8") as fh:
            info = json.load(fh)
        n, hgt, wid = int(info["frames"]), int(info["height"]), int(info["width"])
        # Decoded straight into one preallocated array rather than a list of
        # frames: the meta file already states the geometry, so the destination
        # is known up front and there is never a second full-size copy to stack.
        arr = np.empty((n, hgt, wid, 3), dtype=np.uint16)
        seen = 0
        try:
            container = av.open(self._p(key, VIDEO_EXT))
            try:
                for frame in container.decode(container.streams.video[0]):
                    if seen >= n:                      # more frames than meta
                        seen += 1
                        break
                    arr[seen] = frame.to_ndarray(format=PIX_FMT)
                    seen += 1
            finally:
                container.close()
        except Exception as e:                         # noqa: BLE001
            raise RuntimeError(f"{TAG}: FFV1 decode failed for hop {key}: {e}") from e
        if seen != n:
            # Same contract as the old byte-count check: a truncated or
            # over-long cache entry is a hard error, not a short clip, because
            # a hop silently missing its tail would poison every hop after it.
            raise RuntimeError(
                f"{TAG}: cached hop {key} decoded {seen} frame(s), expected {n}. "
                f"Delete the cache entry and re-render.")
        imgs = torch.from_numpy(arr.astype(np.float32) / 65535.0)
        wav = torch.from_numpy(np.load(self._p(key, AUDIO_EXT)))
        os.utime(self._p(key, VIDEO_EXT), None)  # LRU touch
        return imgs, wav, int(info["sample_rate"]), self._get_latent(key)

    # -- shot locks -------------------------------------------------------
    # A locked shot reuses whatever it last rendered even when its inputs
    # change, so it needs a name that survives an edit -- the content key by
    # definition does not. The pointer maps a stable shot name to the key that
    # shot last produced.
    def _ptr(self, name):
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
        return os.path.join(self.root, "ptr_" + safe + ".json")

    def set_pointer(self, name, key):
        with open(self._ptr(name), "w", encoding="utf-8") as fh:
            json.dump({"key": key, "written": time.time()}, fh)

    def get_pointer(self, name):
        try:
            with open(self._ptr(name), encoding="utf-8") as fh:
                key = json.load(fh).get("key")
        except (OSError, ValueError):
            return None
        return key if key and self.has(key) else None

    # -- housekeeping -----------------------------------------------------
    def entries(self):
        out = []
        for fn in os.listdir(self.root):
            if not fn.endswith(VIDEO_EXT):
                continue
            key = fn[: -len(VIDEO_EXT)]
            try:
                st = os.stat(self._p(key, VIDEO_EXT))
            except OSError:
                continue
            size = st.st_size
            for e in (AUDIO_EXT, META_EXT, LATENT_EXT, LEGACY_LATENT_EXT):
                try:
                    size += os.path.getsize(self._p(key, e))
                except OSError:
                    pass
            out.append((key, size, st.st_mtime))
        return out

    def sweep(self, keep=()):
        """Evict least-recently-used entries until under budget.

        `keep` names hops this run still needs, so a budget smaller than one
        chain cannot delete the hop that is about to be read back.
        """
        items = self.entries()
        total = sum(s for _, s, _ in items)
        if total <= self.budget:
            return 0
        keep = set(keep)
        freed = 0
        for key, size, _ in sorted(items, key=lambda r: r[2]):
            if total - freed <= self.budget:
                break
            if key in keep:
                continue
            for e in (VIDEO_EXT, AUDIO_EXT, META_EXT, LATENT_EXT,
                      LEGACY_LATENT_EXT):
                try:
                    os.remove(self._p(key, e))
                except OSError:
                    pass
            freed += size
        if freed:
            print(f"[{TAG}] cache: evicted {freed / 1024 ** 3:.2f} GB "
                  f"(budget {self.budget / 1024 ** 3:.1f} GB)", flush=True)
        return freed
