"""HTC RefMod Builder. Writes one visual RefMod into models/refmods.

The rail does not read this node's output by a wire. It reads the file,
the same way a still is a file in h3_refs. `name` is the string to put in
the row's `file` field.

Sources are named in `mod_plan` as filenames under `h3_refs`, not wired in
as IMAGE sockets. A DOM panel cannot fill a socket, and a fixed set of
sockets cannot be empty gracefully -- which is the whole reason the other
packs need a separate node to toggle their inputs on and off. Several
stills and a clip become one latent stacked along time, not a version-5
bundle: `load_visual` reads a bundle's first visual member and drops the
rest, so a bundle would silently throw away most of a character reference.

The `images` / `video` sockets still work, for graphs built before the
panel existed. A non-empty `mod_plan` outranks them.
"""
from __future__ import annotations

from . import media as _media
from . import refmod as _refmod

TAG = "HandTieClips"


class HTCRefModBuilder:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae": ("VAE", {"tooltip": "The H3 video VAE. The diffusion model is not used."}),
                "name": ("STRING", {"default": "hero",
                    "tooltip": "File name under models/refmods, without .safetensors. "
                               "This is what a reference row stores in file."}),
                "mode": (["encode", "training"], {"default": "encode",
                    "tooltip": "encode keeps the VAE latent, which is what holds a "
                               "face. training pools it onto a small grid, which "
                               "averages that detail away; refine_steps then fits "
                               "the grid back toward the full latent."}),
            },
            "optional": {
                "mod_plan": ("STRING", {"default": "", "multiline": True,
                    "tooltip": "The panel writes this. Stills and a clip, by "
                               "filename in h3_refs. Each still is one latent "
                               "frame; the clip's frames follow them."}),
                "images": ("IMAGE", {"tooltip": "Legacy socket. One still, first frame of a batch. Ignored when mod_plan names sources."}),
                "video": ("IMAGE", {"tooltip": "Legacy socket. A clip, at least 5 frames. Ignored when mod_plan names sources."}),
                "pool": ("INT", {"default": 16, "min": 2, "max": 64, "step": 2,
                    "tooltip": "Long-edge latent grid for training mode. Even. Ignored by encode."}),
                "latent_frames": ("INT", {"default": 16, "min": 1, "max": 64,
                    "tooltip": "How many latent frames training mode keeps. Ignored by encode."}),
                "refine_steps": ("INT", {"default": 0, "min": 0, "max": 2000,
                    "tooltip": "MSE steps that fit the pooled latent to the full encode. 0 pools only."}),
                "max_tokens": ("INT", {"default": 0, "min": 0, "max": 1048576,
                    "tooltip": "VRAM headroom guard, not a fidelity limit -- 24k-token "
                               "mods are known to run. Above this, near-duplicate "
                               "frames are dropped and then time is resampled. "
                               "0 disables the cap."}),
                "subfolder": ("STRING", {"default": "",
                    "tooltip": "Folder under models/refmods. The rail name becomes subfolder/name."}),
                "save": ("BOOLEAN", {"default": True,
                    "tooltip": "Write the safetensors file. Off returns the name and does not write."}),
                "build": (["stop", "start"], {"default": "stop",
                    "tooltip": "stop leaves the file alone and does not use the VAE, so a queue can run the chain. start writes the RefMod on this queue."}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("name",)
    FUNCTION = "build"
    # Executes on its own, the way PlagueKind's RefMod Save node does.
    # Without this the builder only runs when `name` feeds a node that is
    # itself executed, so a queue with nothing wired to it silently does
    # nothing and reads as "the build did not happen". Safe, because
    # `build=stop` returns before the VAE is touched.
    OUTPUT_NODE = True
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = "Save stills and a clip as one visual H3 RefMod."

    @classmethod
    def IS_CHANGED(cls, mod_plan="", build="stop", **_kw):
        # A stopped builder is inert, so it must not re-run when a source
        # file changes on disk. A started one must.
        if str(build) != "start":
            return "stopped"
        try:
            stills, clip, _voice = _refmod.parse_mod_plan(mod_plan)
        except ValueError:
            return str(mod_plan)
        names = list(stills) + ([clip["file"]] if clip else [])
        if _voice:
            names.append(_voice)
        return _media.stamp(names)

    def _encode_plan(self, vae, stills, clip):
        """Every source onto one canvas, encoded, stacked along time."""
        first = _media.load_image(stills[0]) if stills else None
        if first is None:
            first = _media.load_video(clip["file"], max_frames=1,
                                      start=clip["start"], end=clip["end"])
            if first is None:
                _refmod._fail(_refmod.source_problem(clip['file'], ("video",)) or f"mod_plan source {clip['file']} could not be read")
        tw, th = _refmod.canvas_size(first)
        parts, shapes = [], []
        for name in stills:
            px = _media.load_image(name)
            if px is None:
                _refmod._fail(_refmod.source_problem(name, ("image",)) or f"mod_plan source {name} could not be read")
            z = vae.encode(_refmod.to_canvas(px, tw, th))
            self._check_encode(z, name)
            parts.append(z)
            shapes.append(f"{name} 1f")
        if clip:
            px = _media.load_video(clip["file"], max_frames=clip["frames"] or None,
                                   start=clip["start"], end=clip["end"])
            if px is None:
                _refmod._fail(_refmod.source_problem(clip['file'], ("video",)) or f"mod_plan source {clip['file']} could not be read")
            px = _refmod.snap_video_frames(px)
            z = vae.encode(_refmod.to_canvas(px, tw, th))
            self._check_encode(z, clip["file"])
            parts.append(z)
            shapes.append(f"{clip['file']} {int(px.shape[0])}f")
        print(f"[{TAG}] RefMod sources on a {tw}x{th} canvas: "
              f"{', '.join(shapes)}", flush=True)
        return _refmod.stack_latents(parts, "mod_plan")

    def _check_encode(self, z, where):
        if z.ndim != 5:
            _refmod._fail(
                f"H3 VAE encode of {where} returned {tuple(z.shape)}, expected "
                f"[1,24,T,H,W]. Connect the H3 video VAE.")

    def build(self, vae, name, mode, mod_plan="", images=None, video=None, pool=16,
              latent_frames=16, refine_steps=0, max_tokens=0,
              subfolder="", save=True, build="stop"):
        stem = name
        folder = (subfolder or "").strip().replace("\\", "/").strip("/")
        if folder:
            if any(p in ("", ".", "..") for p in folder.split("/")):
                raise ValueError(f"{TAG}: subfolder {subfolder!r} leaves the refmods folder")
            stem = f"{folder}/{name}"
        if str(build) != "start":
            print(f"[{TAG}] RefMod builder is stopped. {stem} was not written.",
                  flush=True)
            try:
                shown = _refmod._clean_name(stem) or str(name or "").strip()
            except ValueError:
                shown = str(name or "").strip()
            return (shown,)

        stills, clip, voice = _refmod.parse_mod_plan(mod_plan)
        # The voice is not encoded -- a RefMod holds pictures. It is
        # checked here anyway, before the VAE is touched, because finding
        # out the sound file is gone at render time costs a whole chain.
        if voice:
            problem = _refmod.source_problem(voice, ("audio",))
            if problem:
                _refmod._fail(problem)
        plan_used = {}
        if stills or clip or voice:
            plan_used = {"stills": list(stills)}
            if clip:
                plan_used["clip"] = dict(clip)
            if voice:
                plan_used["voice"] = voice
            if images is not None or video is not None:
                print(f"[{TAG}] mod_plan names sources, so the images/video "
                      f"sockets are ignored", flush=True)
            full = self._encode_plan(vae, stills, clip)
            source = "video" if clip else ("image" if len(stills) == 1 else "images")
        else:
            if images is None and video is None:
                raise ValueError(
                    f"{TAG}: add a still or a clip in the RefMod panel, or "
                    f"connect the images / video sockets.")
            if images is not None and video is not None:
                raise ValueError(f"{TAG}: connect images or a video, not both.")
            if video is not None:
                frames = _refmod.snap_video_frames(video)
                source = "video"
            else:
                frames = images[:1]
                source = "image"
            full = vae.encode(_refmod.fit_canvas(frames))
            self._check_encode(full, stem)

        source_shape = "x".join(str(int(n)) for n in full.shape[2:])
        steps = 0
        if mode == "training":
            t = min(int(latent_frames), int(full.shape[2]))
            gh, gw = _refmod.pool_grid(pool, int(full.shape[3]), int(full.shape[4]))
            small = _refmod.pool_latent(full, t, gh, gw)
            steps = int(refine_steps or 0)
            latent = _refmod.refine_latent(small, full, steps) if steps else small
            pool_label = f"{t}x{gh}x{gw}"
        else:
            latent = full
            pool_label = "encode"
            if int(refine_steps or 0) or int(pool) != 16:
                print(f"[{TAG}] refmod mode=encode, so pool and refine_steps "
                      f"are not applied", flush=True)
        latent = _refmod.fit_token_budget(latent, max_tokens, name)
        kind = "image" if int(latent.shape[2]) == 1 else "video"
        tokens = (int(latent.shape[2]) * (int(latent.shape[3]) // 2)
                  * (int(latent.shape[4]) // 2))
        meta = _refmod.metadata_for(
            stem, kind, latent, mode, source, source_shape, pool_label, steps,
            plan=plan_used)
        if save:
            root = _refmod.register_folder()
            written = _refmod.save_latent(root, stem, latent, meta)
            print(f"[{TAG}] wrote RefMod {written} ({kind}, {meta['latent_t']}x"
                  f"{meta['latent_h']}x{meta['latent_w']}, {mode}, {tokens} tokens"
                  + (f", voice {voice}" if voice else "") + ")",
                  flush=True)
            return (written,)
        print(f"[{TAG}] RefMod {stem} was not saved (save is off)", flush=True)
        return (_refmod._clean_name(stem),)


NODE_CLASS_MAPPINGS = {"HTCRefModBuilder": HTCRefModBuilder}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCRefModBuilder": "HTC RefMod Builder"}
