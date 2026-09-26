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

import torch

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
                "description": ("STRING", {"default": "",
                    "tooltip": "One sentence of what the mod shows, stored in the file. "
                               "A reference row that picks this mod with its describe "
                               "box empty is filled from it; after that the row's own "
                               "text is what reaches the prompt."}),
                "subject_mask": (["off", "auto"], {"default": "off",
                    "tooltip": "auto cuts the subject out of every source with rembg "
                               "and repaints the backdrop from the subject's own "
                               "edge colours before encoding, so a sheet's backdrop "
                               "does not ride into the render. A wired mask outranks it."}),
                "background_retention": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0,
                    "step": 0.05,
                    "tooltip": "How much of the backdrop a mask keeps. 0 replaces it, "
                               "1 leaves it untouched, 0.3-0.6 fades it."}),
                "mask": ("MASK", {"tooltip": "Subject mask, 1 = keep. One mask covers every "
                                             "source; a batch gives one per source, stills "
                                             "then the clip."}),
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

    def _isolated(self, canvas, index, count, masking):
        """`canvas` with its backdrop replaced, or as it was when nothing masks it."""
        if not masking:
            return canvas
        wired = masking.get("wired")
        if wired is not None:
            if wired.shape[0] == 1:
                pick = wired
            elif count == 1 and wired.shape[0] == canvas.shape[0]:
                pick = wired  # one clip, a mask per frame
            else:
                pick = wired[index:index + 1]
            # A matte the author made is taken as given.
            m = _refmod.mask_to_canvas(pick, canvas.shape[2], canvas.shape[1])
        else:
            m = _refmod.refine_cutout(_refmod.cutout_masks(canvas), canvas)
        if m.shape[0] not in (1, canvas.shape[0]):
            m = m[:1]
        kept = float(m.mean())
        print(f"[{TAG}] RefMod source {index + 1}: subject covers {kept:.0%} of "
              f"the canvas", flush=True)
        if kept < 0.02:
            print(f"[{TAG}] RefMod source {index + 1}: the mask keeps almost "
                  f"nothing, so the backdrop is left as it was", flush=True)
            return canvas
        return _refmod.isolate_subject(canvas, m, masking["retention"])

    def _encode_plan(self, vae, stills, clip, masking=None):
        """Every source onto one canvas, encoded, stacked along time.

        Returns the latent and the pictures to keep for Qwen: each canvas as
        it went into the VAE, mask and all, so the text encoder and the DiT
        are shown the same subject.
        """
        first = _media.load_image(stills[0]) if stills else None
        if first is None:
            first = _media.load_video(clip["file"], max_frames=1,
                                      start=clip["start"], end=clip["end"])
            if first is None:
                _refmod._fail(_refmod.source_problem(clip['file'], ("video",)) or f"mod_plan source {clip['file']} could not be read")
        tw, th = _refmod.canvas_size(first)
        parts, shapes, looks = [], [], []
        count = len(stills) + (1 if clip else 0)
        for i, name in enumerate(stills):
            px = _media.load_image(name)
            if px is None:
                _refmod._fail(_refmod.source_problem(name, ("image",)) or f"mod_plan source {name} could not be read")
            canvas = self._isolated(_refmod.to_canvas(px, tw, th), i, count, masking)
            z = vae.encode(canvas)
            self._check_encode(z, name)
            parts.append(z)
            looks.append(_refmod.looks_from(canvas[:1]))
            shapes.append(f"{name} 1f")
        if clip:
            px = _media.load_video(clip["file"], max_frames=clip["frames"] or None,
                                   start=clip["start"], end=clip["end"])
            if px is None:
                _refmod._fail(_refmod.source_problem(clip['file'], ("video",)) or f"mod_plan source {clip['file']} could not be read")
            px = _refmod.snap_video_frames(px)
            canvas = self._isolated(_refmod.to_canvas(px, tw, th), count - 1,
                                    count, masking)
            z = vae.encode(canvas)
            self._check_encode(z, clip["file"])
            parts.append(z)
            looks.append(_refmod.looks_from(canvas, clip=True))
            shapes.append(f"{clip['file']} {int(px.shape[0])}f")
        print(f"[{TAG}] RefMod sources on a {tw}x{th} canvas: "
              f"{', '.join(shapes)}", flush=True)
        return _refmod.stack_latents(parts, "mod_plan"), torch.cat(looks, dim=0)

    def _check_encode(self, z, where):
        if z.ndim != 5:
            _refmod._fail(
                f"H3 VAE encode of {where} returned {tuple(z.shape)}, expected "
                f"[1,24,T,H,W]. Connect the H3 video VAE.")

    def build(self, vae, name, mode, mod_plan="", images=None, video=None, pool=16,
              latent_frames=16, refine_steps=0, max_tokens=0,
              subfolder="", save=True, build="stop", description="",
              subject_mask="off", background_retention=0.0, mask=None):
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
        # Settled before the VAE is touched, so a mask batch that does not
        # match the sources fails in a second rather than after an encode.
        masking, mask_meta = None, {}
        retention = float(background_retention or 0.0)
        if mask is not None or str(subject_mask) == "auto":
            if mask is not None:
                wired = mask if mask.dim() == 3 else mask.unsqueeze(0)
                if stills or clip:
                    count = len(stills) + (1 if clip else 0)
                    ok = wired.shape[0] in (1, count) or (
                        count == 1 and clip and not stills)
                else:
                    count = 1
                    ok = True  # one socket source: a mask or a mask per frame
                if not ok:
                    raise ValueError(
                        f"{TAG}: the mask batch has {wired.shape[0]} masks for "
                        f"{count} sources. Wire one mask for all of them, or "
                        f"one per source, stills then the clip.")
                if str(subject_mask) == "auto":
                    print(f"[{TAG}] a mask is wired, so subject_mask=auto is "
                          f"not run", flush=True)
                masking = {"wired": wired, "retention": retention}
                mask_meta = {"source": "wired", "fill": "pull-push",
                             "background_retention": retention}
            else:
                masking = {"wired": None, "retention": retention}
                mask_meta = {"source": "auto", "model": _refmod.CUTOUT_MODEL,
                             "fill": "pull-push", "background_retention": retention}
            if retention >= 1.0:
                print(f"[{TAG}] background_retention=1 keeps the whole "
                      f"background, so the mask changes nothing", flush=True)

        plan_used = {}
        if stills or clip or voice:
            plan_used = {"stills": list(stills)}
            if clip:
                plan_used["clip"] = dict(clip)
            if voice:
                v_start, v_end = _refmod.voice_window(mod_plan)
                # The object form only when trimmed, so an untrimmed mod
                # writes the header it always has.
                plan_used["voice"] = ({"file": voice, "start": v_start, "end": v_end}
                                      if v_start or v_end else voice)
            if images is not None or video is not None:
                print(f"[{TAG}] mod_plan names sources, so the images/video "
                      f"sockets are ignored", flush=True)
            full, looks = self._encode_plan(vae, stills, clip, masking)
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
            canvas = self._isolated(_refmod.fit_canvas(frames), 0, 1, masking)
            full = vae.encode(canvas)
            self._check_encode(full, stem)
            looks = _refmod.looks_from(canvas, clip=video is not None)

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
            plan=plan_used, description=description, mask=mask_meta,
            looks=int(looks.shape[0]))
        if save:
            root = _refmod.register_folder()
            written = _refmod.save_latent(root, stem, latent, meta, looks=looks)
            print(f"[{TAG}] wrote RefMod {written} ({kind}, {meta['latent_t']}x"
                  f"{meta['latent_h']}x{meta['latent_w']}, {mode}, {tokens} tokens, "
                  f"{meta['looks']} kept for Qwen"
                  + (f", voice {voice}" if voice else "") + ")",
                  flush=True)
            return (written,)
        print(f"[{TAG}] RefMod {stem} was not saved (save is off)", flush=True)
        return (_refmod._clean_name(stem),)


NODE_CLASS_MAPPINGS = {"HTCRefModBuilder": HTCRefModBuilder}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCRefModBuilder": "HTC RefMod Builder"}
