"""One read-only endpoint: the vocabulary the editor draws its dropdowns from.

The alternative is a copy of `VOCAB` in JavaScript, and a copy is a second source
of truth that goes stale the first time a phrase is improved. `directives.py`
exists precisely so that improving a sentence improves every plan ever written;
duplicating those strings in the UI would undo that.

It also ships the *prose* each option compiles to, not just the option names, so
the editor can show what a directive actually puts in front of the encoder. That
is the whole answer to "I don't know how any of this works at a glance".

Since 2026-08-28 this module also carries the reference *files*: `POST
/h3_ref_chain/upload` streams dropped media into `<input>/h3_refs`, and `GET
/h3_ref_chain/files` lists what is there. Those replaced the nine `ref_image_N`
IMAGE sockets, which were most of a 16-socket column.

Everything that decides *where bytes land* lives in `media.py`, not here: one
prefix-checked resolver used by both the route and the loaders, so there is a
single place to be wrong about it. The route itself never joins a path.

Since 2026-08-30 it also carries the optional plan writer: `GET/POST
/h3_ref_chain/llm` reads and saves the local chat-server settings, and `POST
/h3_ref_chain/plan` runs the generate-validate-repair loop in `planner.py`.
Those are the only places in the pack that touch the network, and none of them
is reachable from a graph execution -- the button writes JSON into a widget,
and queueing reads the widget. `llm` and `planner` are imported inside the
handlers rather than at module scope so a broken or absent aiohttp client
cannot stop the pack from loading.
"""

import os as _os

from . import directives as _d
from . import media as _media
from . import refs as _refs
from . import waveform as _waveform

TAG = "HandTieClips"
ROUTE = "/h3_ref_chain/vocab"
UPLOAD_ROUTE = "/h3_ref_chain/upload"
FILES_ROUTE = "/h3_ref_chain/files"
PEAKS_ROUTE = "/h3_ref_chain/peaks"
REFMOD_EST_ROUTE = "/h3_ref_chain/refmod_estimate"
REFMOD_PLAN_ROUTE = "/h3_ref_chain/refmod_plan"
LLM_ROUTE = "/h3_ref_chain/llm"
PLAN_ROUTE = "/h3_ref_chain/plan"
SWAP_PLAN_ROUTE = "/h3_ref_chain/swap_plan"
SWAP_DESCRIBE_ROUTE = "/h3_ref_chain/swap_describe"
UNLOAD_ROUTE = "/h3_ref_chain/llm/unload"
HOP_SPRITE_ROUTE = "/h3_ref_chain/hop_sprite"
HOP_CLIP_ROUTE = "/h3_ref_chain/hop_clip"
SEAM_CLIP_ROUTE = "/h3_ref_chain/seam_clip"

# Scrub sheets for the timeline, keyed (hop key, n). A hop key names content,
# so an entry can never go stale -- only the cache behind it can vanish, and
# then the route says so. Cleared wholesale, like _PEAKS; ~90 KB each.
_SPRITES = {}
_SPRITES_MAX = 64

# Decoded waveform summaries, keyed (name, mtime, n).
#
# mtime is in the key for the same reason `media.stamp` puts it in IS_CHANGED:
# a filename is a stable input even when the bytes behind it change, and a trim
# bar drawn from the previous file is a bar that lies about where the beats are.
#
# Cleared wholesale rather than evicted one at a time. The entries are ~1.5 KB
# and the working set is however many references one chain uses, so an LRU would
# be more machinery than the thing it manages.
_PEAKS = {}
_PEAKS_MAX = 48

# A batch of stills is a handful; this is a guard against a runaway multipart
# body, not a considered product limit.
MAX_UPLOAD_FILES = 32
MAX_UPLOAD_BYTES = 256 * 1024 * 1024


def _secs_or_none(value):
    """A positive trim point, or None for unset / 0 / junk."""
    try:
        out = float(value or 0)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _songmap_summary(m):
    """The song map as the card wants it: the trustworthy part big, the
    uncertain part explicitly marked as uncertain.

    The bar grid is measured and reliable. Section detection is 4/9 on the
    headliner set and 2/9 with listening, so sections are SUGGESTIONS the user
    confirms or moves -- the card must not present them as measurements. And the
    whole map carries per-bar features nobody reads, so it is trimmed here
    rather than posted over a websocket.
    """
    def _f(v):
        try:
            return round(float(v), 3)
        except (TypeError, ValueError):
            return None

    secs = []
    for s in (m.get("sections") or [])[:12]:
        if not isinstance(s, dict):
            continue
        secs.append({"bar": s.get("bar"), "from": _f(s.get("from_s")),
                     "to": _f(s.get("to_s")),
                     "why": str(s.get("why") or "")[:80],
                     "confidence": _f(s.get("confidence"))})
    return {
        "bpm": _f(m.get("bpm")),
        "bar_s": _f(m.get("bar_s")),
        "beats_per_bar": m.get("beats_per_bar"),
        "dur": _f(m.get("dur")),
        # Steadiness and drift say whether the GRID can be trusted. A song that
        # wanders is why a cut lands off the bar, and the user should see that
        # before blaming the solver.
        "steady": _f(m.get("steady")),
        "grid_drift_f": _f(m.get("grid_drift_f")),
        "sections": secs,
        "sections_confidence":
            "measured 4/9, 2/9 with listening - confirm or move them",
        "lines": [str(x) for x in (m.get("lines") or [])][:24],
    }


def _pinned_refs(raw, limit):
    """Rail rows from the Write-plan POST. Only rows with a tag and a file."""
    out = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        tag = str(r.get("tag") or "").lstrip("@").strip()
        fname = str(r.get("file") or "").strip()
        if not tag or not fname:
            continue
        subj = r.get("subject")
        try:
            subj = int(subj) if subj not in (None, "", False) else None
        except (TypeError, ValueError):
            subj = None
        # `mp` rides along so planner._restore_rail_only can put it back on the
        # written register. The model is never shown it and cannot author it,
        # but Accept overwrites the rail with what comes back, so a cap that
        # does not survive this round trip is a cap the next write deletes.
        try:
            mp = float(r.get("mp") or 0) or None
        except (TypeError, ValueError):
            mp = None
        # `source` / `strength` / `voice` ride for the same reason as `mp`:
        # planner.RAIL_ONLY_FIELDS puts them back. Defaults go up as None so a
        # plain still does not come back with `source: image` written on it.
        source = "refmod" if str(r.get("source") or "") == "refmod" else None
        try:
            strength = float(r["strength"]) if r.get("strength") not in (
                None, "") else None
        except (TypeError, ValueError):
            strength = None
        if strength == 1.0:
            strength = None
        out.append({
            "tag": tag,
            "file": fname,
            "subject": subj,
            "retention": str(r.get("retention") or ""),
            "desc": str(r.get("desc") or ""),
            "mp": mp,
            "source": source,
            "strength": strength,
            "voice": str(r.get("voice") or "").strip() or None,
            # None rather than 0, as for `mp`: 0 is "untrimmed", and an unset
            # field must come back absent, not written as a zero.
            "voice_start": _secs_or_none(r.get("voice_start")),
            "voice_end": _secs_or_none(r.get("voice_end")),
        })
        if len(out) >= int(limit):
            break
    return out


def _speed_mode_table():
    """`SPEED_MODES`, flattened so every mode names every key any mode touches.

    `SPEED_MODES["regular"]` is `{}` -- run() reads a missing key as "leave the
    widget alone", which is right for a server-side override but wrong for a
    panel that WRITES: picking turbo then regular would leave refine_head on
    the turbo value with the dial claiming regular. So each mode is filled out
    with the widget's own INPUT_TYPES default for anything it does not set, and
    the editor stamps the row whole.

    Served rather than retyped in JS for the reason the vocab route exists at
    all: a second copy of the table is a second thing to get wrong, and this
    one decides what renders.
    """
    from .h3_ref_chain import SPEED_MODES, HandTieClips
    opt = HandTieClips.INPUT_TYPES().get("optional") or {}

    def _default(name):
        spec = opt.get(name)
        return (spec[1] if spec and len(spec) > 1 else {}).get("default")

    keys = sorted({k for row in SPEED_MODES.values() for k in row})
    table = {}
    for mode, row in SPEED_MODES.items():
        filled = {}
        for k in keys:
            v = row.get(k, _default(k))
            if v is not None:
                filled[k] = v
        table[mode] = filled
    return table


def _payload():
    from .h3_ref_chain import (
        ASPECTS, DURATION_FRAMES, OVERLAP_FRAMES, RESOLUTIONS, FPS, _canvas)
    # Imported here, not at module top, for the same reason h3_ref_chain is:
    # planner reads the prompt pack off disk and pulls in the rest of this
    # module, so a top-level import is a cycle.
    from . import planner as _planner

    return {
        # axis -> option -> the sentence it compiles to. Order matters: AXES is
        # the order directive_prose concatenates in, so the UI shows them in the
        # order the encoder reads them.
        "axes": list(_d.AXES),
        "vocab": {axis: dict(opts) for axis, opts in _d.VOCAB.items()},
        "defaults": dict(_d.DEFAULTS),
        "establish": _d.ESTABLISH,
        # join has nothing to attach to on the first hop; the editor hides it
        # there to match directive_prose's hop_index == 0 skip.
        "join_axis": "join",

        "retention": dict(_refs.RETENTION),
        "max_ref_images": _refs.MAX_REF_IMAGES,
        "refs_subdir": _media.REFS_SUBDIR,
        "image_exts": sorted(_media.IMAGE_EXTS),
        "video_exts": sorted(_media.VIDEO_EXTS),
        "audio_exts": sorted(_media.AUDIO_EXTS),
        "ref_fields": list(_refs.REF_FIELDS),
        "ref_sources": list(_refs.SOURCES),
        "subject_fields": list(_refs.SUBJECT_FIELDS),

        # mode -> widget -> value the run panel stamps when you pick it.
        "speed_modes": _speed_mode_table(),

        "durations": {k: v for k, v in DURATION_FRAMES.items()},
        "overlaps": {k: v for k, v in OVERLAP_FRAMES.items()},
        # Resolved here rather than in the editor. The size is a function of two
        # combo labels, an area cap and a 32 px grid; a second implementation in
        # JavaScript is a second place for it to be wrong, and the run panel's
        # digest would be the thing quietly disagreeing with what rendered.
        "canvas": {res: {asp: list(_canvas(res, asp)) for asp in ASPECTS}
                   for res in RESOLUTIONS},
        "fps": FPS,

        # Duration label -> the beat's own word/line band, parsed out of the
        # writer's prompt by planner.beat_table(). The shot cards print
        # "47 / 35-60 words" under every beat, and that band is the SAME table
        # the model was given -- read from the prompt, not restated, so the
        # number the user is judged against cannot drift from the number the
        # model was asked for. Labels are whitespace-normalised here because
        # the prompt's table is aligned with spaces ("8 s" vs "8  s").
        "beat_table": {k: list(v) for k, v in _planner.beat_table().items()},
    }


def register():
    """Attach the route if a PromptServer exists. Never raises on import."""
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return False
    instance = getattr(PromptServer, "instance", None)
    if instance is None or not hasattr(instance, "routes"):
        return False

    @instance.routes.get(ROUTE)
    async def _vocab(_request):
        try:
            return web.json_response(_payload())
        except Exception as exc:  # a broken payload must not take the server down
            print(f"[{TAG}] vocab route failed: {exc!r}", flush=True)
            return web.json_response({"error": str(exc)}, status=500)

    @instance.routes.get(FILES_ROUTE)
    async def _files(request):
        """What is already in the reference folder, for the editor's picker."""
        try:
            folder = request.rel_url.query.get("folder") or ""
            if folder == "refmods":
                from . import refmod as _refmod
                return web.json_response({"ok": True, "files": _refmod.listing()})
            kinds = request.rel_url.query.get("kinds") or ""
            want = {k.strip() for k in kinds.split(",") if k.strip()} or None
            return web.json_response({"ok": True, "files": _media.listing(want)})
        except Exception as exc:
            print(f"[{TAG}] files route failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @instance.routes.post(REFMOD_EST_ROUTE)
    async def _refmod_estimate(request):
        """Price a mod_plan before it is built: canvas, frames, tokens.

        The builder's own `canvas_for_hw` and `frame_tokens` answer this, so
        the panel never carries a second copy of the sizing rules. Stills are
        exact. A clip's latent frame count is the VAE's to decide, so this
        reports its pixel frames and says so rather than guessing a ratio.
        """
        try:
            from . import refmod as _refmod

            body = await request.json()
            stills, clip, voice = _refmod.parse_mod_plan({
                "stills": body.get("stills") or [],
                "clip": body.get("clip") or None,
                "voice": body.get("voice") or "",
            })
            first = stills[0] if stills else (clip or {}).get("file")
            if not first:
                return web.json_response({"ok": True, "empty": True})
            size = _media.probe_size(first)
            if size is None:
                return web.json_response(
                    {"ok": False,
                     "error": _refmod.source_problem(first) or
                              f"{first} could not be measured"})
            tw, th = _refmod.canvas_for_hw(size[1], size[0])
            per = _refmod.frame_tokens(tw, th)
            out = {
                "ok": True,
                "canvas": [tw, th],
                "per_frame_tokens": per,
                "stills": len(stills),
                "still_tokens": len(stills) * per,
                "problems": [_refmod.source_problem(n) or
                             f"{n} could not be read"
                             for n in stills
                             if _media.probe_size(n) is None],
                "voice": voice,
            }
            if clip:
                n = _media.probe_frames(clip["file"], clip["start"],
                                        clip["end"], clip["frames"])
                if n is None:
                    out["problems"] = out["problems"] + [
                        _refmod.source_problem(clip["file"], ("video",)) or
                        f"{clip['file']} could not be read"]
                else:
                    snapped = n
                    while snapped % 17 != 5 and snapped > 0:
                        snapped -= 1
                    out["clip_frames"] = snapped
                    out["clip_dropped"] = n - snapped
            if voice and _media.resolve(voice) is None:
                out["problems"] = out["problems"] + [
                    _refmod.source_problem(voice, ("audio",)) or
                    f"{voice} could not be read"]
            return web.json_response(out)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)})

    @instance.routes.post(REFMOD_PLAN_ROUTE)
    async def _refmod_plan(request):
        """The sources a saved RefMod was built from: `{ok, plan}`.

        Header read only -- no tensor is loaded and no VAE is touched, so
        this is cheap enough to call while the name field is being typed.
        An older mod, or one built through the sockets, answers `{}`; the
        panel shows that as "no source list", never as an error.
        """
        try:
            from . import refmod as _refmod

            body = await request.json()
            name = str(body.get("name") or "").strip()
            if not name:
                return web.json_response({"ok": True, "plan": {}})
            path = _refmod.resolve(name)
            return web.json_response(
                {"ok": True, "plan": _refmod.stored_plan(name),
                 "description": _refmod.description_of(
                     _refmod.read_meta(path) if path else None)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)})

    @instance.routes.post(PEAKS_ROUTE)
    async def _peaks(request):
        """A waveform summary for the trim bar: `{peaks: [...], seconds}`.

        The decode runs in an executor, NOT on this coroutine. These handlers
        share ComfyUI's event loop, and decoding a three-minute file on it
        freezes the canvas, the queue and the progress bar together -- the same
        constraint `llm.py`'s docstring spells out for the writer.
        """
        import asyncio

        try:
            body = await request.json()
        except Exception:
            body = {}
        name = str((body or {}).get("name") or "").strip()
        n = (body or {}).get("n") or _waveform.DEFAULT_N

        path = _media.resolve(name, kinds={"audio", "video"})
        if path is None:
            # Not an error the panel can act on -- the file is simply gone, and
            # the picker already says so. A flat line draws an empty bar.
            return web.json_response({"ok": True, "peaks": [], "seconds": 0.0})

        try:
            mtime = _os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        key = (name, mtime, int(n))
        hit = _PEAKS.get(key)
        if hit is not None:
            return web.json_response({"ok": True, "peaks": hit[0],
                                      "seconds": hit[1], "cached": True})

        def work():
            audio = _media.load_audio(name)
            if audio is None:
                # A video with no audio track, or a file PyAV cannot open. The
                # bar still has to position its grips, so report the duration
                # even when there is nothing to draw.
                return [], 0.0
            return _waveform.peaks(audio, n)

        try:
            got = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as exc:
            print(f"[{TAG}] peaks route failed for {name!r}: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

        if len(_PEAKS) >= _PEAKS_MAX:
            _PEAKS.clear()
        _PEAKS[key] = got
        return web.json_response({"ok": True, "peaks": got[0], "seconds": got[1]})

    @instance.routes.post(UPLOAD_ROUTE)
    async def _upload(request):
        """Stream dropped media into the reference folder.

        Multipart, not JSON+base64: a batch of stills should not be inflated by
        a third and held in memory twice on the way through the browser. The
        destination is fixed -- `media.refs_dir()` -- so a filename can only
        ever name a file *inside* it, and `media.resolve` re-checks that on the
        way back out.
        """
        if not (request.content_type or "").startswith("multipart/"):
            return web.json_response(
                {"ok": False, "error": "expected multipart/form-data"}, status=400)
        try:
            reader = await request.multipart()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"bad multipart: {exc}"}, status=400)

        dest = _media.refs_dir(create=True)
        saved, skipped, total = [], [], 0
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if not getattr(part, "filename", None):
                    continue
                if len(saved) >= MAX_UPLOAD_FILES:
                    skipped.append(f"{part.filename}: batch limit "
                                   f"{MAX_UPLOAD_FILES} reached")
                    break
                # Basename only, and the extension has to be one we can open.
                # Rejecting here means nothing unreadable is ever written.
                name = _os.path.basename(part.filename or "")
                kind = _media.kind_of(name)
                if kind is None:
                    skipped.append(f"{name}: not an image, video or audio file")
                    continue
                path = _media.unique_path(dest, name)
                size = 0
                try:
                    with open(path, "wb") as fh:
                        while True:
                            chunk = await part.read_chunk()
                            if not chunk:
                                break
                            size += len(chunk)
                            total += len(chunk)
                            if total > MAX_UPLOAD_BYTES:
                                raise ValueError("upload too large")
                            fh.write(chunk)
                except Exception:
                    # A partial file is worse than no file: it would list in
                    # the picker and fail to open.
                    try:
                        _os.remove(path)
                    except OSError:
                        pass
                    raise
                w = h = 0
                if kind == "image":
                    path, w, h = _media.shrink_image(path)
                saved.append({"name": _os.path.basename(path), "kind": kind,
                              "width": w, "height": h, "bytes": size})
        except Exception as exc:
            print(f"[{TAG}] upload failed: {exc!r}", flush=True)
            return web.json_response(
                {"ok": False, "error": str(exc), "files": saved}, status=400)

        if saved:
            print(f"[{TAG}] uploaded {len(saved)} reference file(s) -> "
                  + ", ".join(f["name"] for f in saved), flush=True)
        for note in skipped:
            print(f"[{TAG}] upload skipped {note}", flush=True)
        return web.json_response({"ok": True, "files": saved, "skipped": skipped})

    # -- the optional plan writer -----------------------------------------
    #
    # Everything below is inert until someone opens Settings in the panel and
    # points it at a server. With no server configured the panel shows the
    # manual paste recipe and nothing here is ever called.

    @instance.routes.get(LLM_ROUTE)
    async def _llm_get(_request):
        """Saved settings plus whatever models the server currently offers.

        Answers 200 with an empty model list when the server is down, rather
        than an error: the user needs this panel open in order to fix the URL
        that is the reason the list is empty.
        """
        try:
            from . import llm as _llm
            conn = _llm.load_conn()
            found = await _llm.models(conn["server_url"])
            return web.json_response({
                "ok": True,
                "server_url": conn["server_url"],
                # The saved model goes back with the list so the panel can
                # PRESELECT it. Without this the dropdown lands on option[0]
                # and the next save silently rewrites the configured model to
                # whatever happens to be first -- the exact bug recorded at
                # PromptMasterLD's h3_studio_ui.js:5007.
                "model": conn["model"],
                "temperature": conn["temperature"],
                "keep_warm": conn["keep_warm"],
                "unload_on_run": conn["unload_on_run"],
                "vram_settle_s": conn["vram_settle_s"],
                # Which server this is, what to call it in a message, and
                # whether an empty model is legal here. The panel draws the
                # backend picker from `backends` and spells errors with
                # `backend_label`, so adding a backend is a change in llm.py
                # alone rather than an edit in three places.
                "backend": conn.get("backend") or "lmstudio",
                "backend_label": _llm.backend_spec(conn.get("backend"))["label"],
                "model_optional": bool(_llm.backend_spec(
                    conn.get("backend"))["model_optional"]),
                "backends": [{"id": k, "label": v["label"]}
                             for k, v in _llm.BACKENDS.items()],
                # Whether the writer is up. The panel's Start button is disabled
                # when it is, so a second click cannot launch a second 27B --
                # which is a real way to end up with two processes fighting over
                # 32 GB.
                "running": await _llm.server_running_async(),
                # [{id, loaded}] -- `loaded` is None on servers with no
                # native state route. The panel marks the difference, because
                # picking an unloaded model is picking a 400.
                "models": found,
                "online": bool(found),
                "any_loaded": any(m.get("loaded") for m in found),
            })
        except Exception as exc:
            print(f"[{TAG}] llm route failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @instance.routes.post(LLM_ROUTE)
    async def _llm_set(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "expected a JSON body"}, status=400)
        try:
            from . import llm as _llm
            # `action: "start"` is the ONLY thing here that launches a process,
            # and it is deliberately a separate verb rather than something a
            # settings save can trigger. The panel's Start button posts it; a
            # queued graph never can.
            if isinstance(body, dict) and body.get("action") == "start":
                _ok, _msg = await _llm.start_process_async(
                    model_file=str(body.get("model_file") or "").strip() or None)
                return web.json_response({"ok": _ok, "error": "" if _ok else _msg,
                                          "note": _msg,
                                          "running": await _llm.server_running_async(),
                                          **_llm.load_conn()})
            if isinstance(body, dict) and body.get("action") == "stop":
                _n = _llm.stop_process()
                return web.json_response({"ok": True,
                                          "note": f"stopped {_n} process(es)",
                                          "running": await _llm.server_running_async(),
                                          **_llm.load_conn()})
            if isinstance(body, dict) and body.get("action") == "status":
                return web.json_response({"ok": True,
                                          "running": await _llm.server_running_async(),
                                          **_llm.load_conn()})
            conn = _llm.save_conn(body if isinstance(body, dict) else {})
            return web.json_response({"ok": True, **conn})
        except Exception as exc:
            print(f"[{TAG}] llm save failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @instance.routes.post(UNLOAD_ROUTE)
    async def _llm_unload(request):
        """Free the writer's VRAM on demand. Never a 500, never a hard error.

        The automatic unload after writing covers the ordinary case. This is
        for the ones it cannot: the checkbox was off, the write failed before
        it ran, or LM Studio's JIT put a different model in memory than the one
        configured. Pressing it when nothing is loaded is a no-op that says so.
        """
        try:
            from . import llm as _llm
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        try:
            conn = _llm.load_conn()
            n, note = await _llm.unload_all(conn["server_url"], conn["model"])
            return web.json_response({"ok": True, "unloaded": n, "note": note})
        except Exception as exc:
            print(f"[{TAG}] unload failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)})

    @instance.routes.post(PLAN_ROUTE)
    async def _plan(request):
        """Write a plan, and make the model repair it until the node accepts it.

        Returns `ok: false` with a readable `error` for every failure mode --
        no server, no model, a model that will not converge. None of them is a
        500: they are all ordinary states of a machine the user controls, and
        an exception page in a status line helps nobody.
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "expected a JSON body"}, status=400)

        brief = str(body.get("brief") or "").strip()
        try:
            hops = max(1, min(24, int(body.get("hops") or 3)))
        except (TypeError, ValueError):
            hops = 3
        # Phase B: which of the three stages this is. One route, three jobs,
        # because they share the connection, the stills and the rail -- and a
        # second route is a second place for the "no model selected" answer and
        # the unload policy to drift apart.
        stage = str(body.get("stage") or "develop").strip().lower()
        if stage not in ("propose", "develop", "edit", "song"):
            return web.json_response(
                {"ok": False,
                 "error": f"unknown stage {stage!r}: propose, develop, edit, song"},
                status=400)
        # The conversation. Bounded server-side as well as in the panel: a
        # hand-rolled client that posts a thousand turns gets the last twelve,
        # not a request nobody can answer.
        thread = [m for m in (body.get("thread") or [])
                  if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        proposal = body.get("proposal") or None
        if proposal is not None and not isinstance(proposal, (dict, str)):
            proposal = None
        pin_mech = str(body.get("pin_mech") or "").strip()
        # Stage 3 edits what is already on the node. It reads shot_plan /
        # ref_plan straight off the request rather than the rail alone, so an
        # edit never depends on the panel having re-sent a register the user
        # has since hand-edited.
        cur_shots = str(body.get("shot_plan") or "").strip()
        cur_refs = str(body.get("ref_plan") or "").strip()
        if stage == "edit" and not cur_shots:
            return web.json_response(
                {"ok": False, "error": "there is no plan on the node to edit"},
                status=400)
        # The node's `duration` widget. The prompt's length table has a row per
        # hop length and the model was never told which row it was writing to
        # -- the prompt says to ASK, which a button cannot answer. Unknown or
        # absent is fine: `build_user_turn` adds nothing and the lint no-ops.
        duration = str(body.get("duration") or "").strip()

        try:
            from . import llm as _llm
            from . import planner as _planner
            from . import refs as _refs
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"the plan writer failed to load: {exc}"})

        conn = _llm.load_conn()
        if not conn.get("model"):
            return web.json_response(
                {"ok": False, "error": "no model is selected -- open Settings "
                                       "in the panel and pick one."})

        # A render on the card and a 27B on the card do not fit. ComfyUI keeps
        # its models resident after a queue finishes, so "is something
        # rendering" is the wrong question -- the right one is whether the
        # writer is running at all, and if it is, whether the card is free
        # enough to start it. Measured: nInfer asks for an 11 GB runtime
        # reservation and fails to start with ~6.5 GB free, which is what
        # ComfyUI's own idle models leave behind.
        #
# So: ask the writer's own eviction first, and only then complain. The
        # note comes back to the panel, which says it rather than letting a
        # cryptic server error arrive thirty seconds later.
        #
        # ONLY while it is down. This used to run on every plan request, so the
        # sequence Start -> Write beats stopped the server the Start button had
        # just launched, thirty seconds earlier, and then failed to reach it:
        # the writer evicting itself out from under the very request about to
        # use it. A serving backend has already proven it fits, and stopping it
        # frees nothing this branch needs -- the render path is where the card
        # is actually contended.
        _writer_down = not await _llm.server_running_async(
            conn.get("backend"))
        if _llm.shares_this_gpu(conn["server_url"]) and _writer_down:
            try:
                _note = _llm.free_for_render()
            except Exception:
                _note = ""
            if _note and "could not be stopped" in _note:
                return web.json_response({"ok": False, "error": _note})

        pinned = _pinned_refs(body.get("refs"), _refs.MAX_REF_IMAGES)
        # A filled rail is the scene. The folder listing is only the fallback
        # for a brief-only write with empty boxes -- that listing is what made
        # the model pick files the user never chose.
        if pinned:
            files = [p["file"] for p in pinned]
        else:
            files = [f["name"] for f in _media.listing({"image", "video"})]

        import asyncio
        images = []
        if pinned:
            def encode():
                out = []
                for p in pinned:
                    # A RefMod is a latent, not a picture in h3_refs; the
                    # writer gets its `desc` and nothing to look at.
                    if p.get("source") == "refmod":
                        continue
                    url = _media.vision_data_url(p["file"])
                    if url:
                        out.append({"tag": p["tag"], "data_url": url})
                return out
            try:
                images = await asyncio.get_running_loop().run_in_executor(
                    None, encode)
            except Exception as exc:
                print(f"[{TAG}] could not attach rail stills: {exc!r}",
                      flush=True)
                images = []

        vision_notes = []
        mods = [p["tag"] for p in pinned if p.get("source") == "refmod"]
        if mods:
            vision_notes.append(
                "the writer cannot look at a RefMod; it read the description "
                "of " + ", ".join("@" + t for t in mods) + " instead")

        async def complete_fn(messages, schema=None):
            try:
                return await _llm.complete(
                    conn["server_url"], conn["model"], messages,
                    schema=schema, temperature=conn["temperature"])
            except _llm.LLMError as exc:
                if _llm.has_images(messages) and "HTTP 4" in str(exc):
                    print(f"[{TAG}] server rejected the attached stills; "
                          f"retrying from filenames only", flush=True)
                    vision_notes.append(
                        "the model could not look at the pictures; "
                        "the draft used filenames only")
                    return await _llm.complete(
                        conn["server_url"], conn["model"],
                        _llm.text_only(messages),
                        schema=schema, temperature=conn["temperature"])
                raise

        try:
            if stage == "song":
                # MV mode's entry point. Measures a song and returns it; writes
                # nothing. `listen` is off by default and stays off unless the
                # user asks, because it downloads a stem -- 4/9 sections become
                # 2/9 WITH listening, which is a real but small improvement for
                # a fetch, so it is the user's call rather than a default.
                from . import songmap as _songmap
                _song = str(body.get("song_file") or "").strip()
                if not _song:
                    return web.json_response(
                        {"ok": False,
                         "error": "no song file -- pick one in MEDIA, or set a "
                                  "master_audio_file, then ask for the song "
                                  "again."})
                out = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: _songmap.song_map(
                        _song, listen=bool(body.get("listen")),
                        status=lambda m: print(f"[{TAG}] {m}", flush=True),
                        allow_download=False))
                out = {"ok": True, "song": _songmap_summary(out)}
            elif stage == "propose":
                out = await _planner.write_propose(
                    brief, complete_fn=complete_fn, hops=hops, files=files,
                    pinned=pinned, images=images, duration=duration,
                    thread=thread, pin_mech=pin_mech)
            elif stage == "edit":
                out = await _planner.write_edit(
                    brief, cur_shots, cur_refs, complete_fn=complete_fn,
                    hops=hops, files=files, pinned=pinned, duration=duration,
                    thread=thread)
            else:
                out = await _planner.write_plan(
                    brief, hops, complete_fn=complete_fn, files=files,
                    pinned=pinned, images=images, duration=duration,
                    thread=thread, proposal=proposal)
        except _llm.LLMError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:
            print(f"[{TAG}] plan route ({stage}) failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)})
        if vision_notes:
            out["warnings"] = list(out.get("warnings") or []) + vision_notes

        # Stay resident by default. The render is where the card is actually
        # contended, and the node evicts there (`llm.free_for_render`), so
        # unloading here only makes the NEXT plan pay a full model load --
        # and nobody writes exactly one plan. `keep_warm` off restores the old
        # unload-immediately behaviour for a machine too tight to hold both.
        # A courtesy either way, never a failure: `unload` swallows its own
        # errors and returns False when no endpoint answers.
        if not conn.get("keep_warm"):
            try:
                await _llm.unload(conn["server_url"], conn["model"])
            except Exception:
                pass

        if out["ok"]:
            print(f"[{TAG}] wrote a {stage} ({hops}-hop) in "
                  f"{out.get('attempts', 1)} attempt(s)", flush=True)
        return web.json_response(out)

    def _swap_conn():
        from . import llm as _llm
        from . import planner as _planner
        conn = _llm.load_conn()
        return _llm, _planner, conn

    @instance.routes.post(SWAP_DESCRIBE_ROUTE)
    async def _swap_describe(request):
        """Caption the clip frame at the trim IN point. No plan, no rail."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "expected a JSON body"}, status=400)
        video = str(body.get("video") or "").strip()
        if not video:
            return web.json_response(
                {"ok": False, "error": "pick a reference clip first"})
        try:
            start = float(body.get("video_start_s") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(body.get("video_end_s") or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        try:
            _llm, _planner, conn = _swap_conn()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"SWAP failed to load: {exc}"})
        if not conn.get("model"):
            return web.json_response(
                {"ok": False, "error": "no model is selected -- open WRITE, "
                                       "Settings, and pick one."})

        import asyncio

        # Three, like the plan path. A caption written from one frame describes
        # a pose, and this caption reaches the encoder as what <Video 1> IS.
        def encode():
            return _media.video_frame_data_urls(
                video, start=start, end=end, count=3)

        try:
            frames = await asyncio.get_running_loop().run_in_executor(
                None, encode)
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"could not read the clip: {exc}"})
        if not frames:
            return web.json_response(
                {"ok": False, "error": "could not extract frames from that "
                                       "window. Trim the clip on MEDIA."})

        async def complete_fn(messages, schema=None):
            return await _llm.complete(
                conn["server_url"], conn["model"], messages,
                schema=schema, temperature=conn["temperature"])

        try:
            out = await _planner.describe_frame(
                complete_fn=complete_fn, frames=frames)
        except _llm.LLMError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:
            print(f"[{TAG}] swap_describe failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)})
        if not conn.get("keep_warm"):
            try:
                await _llm.unload(conn["server_url"], conn["model"])
            except Exception:
                pass
        return web.json_response(out)

    @instance.routes.post(SWAP_PLAN_ROUTE)
    async def _swap_plan(request):
        """One-hop identity swap. Never returns a ref_plan key."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "expected a JSON body"}, status=400)
        brief = str(body.get("brief") or "").strip()
        duration = str(body.get("duration") or "").strip()
        video = str(body.get("video") or "").strip()
        identity = body.get("identity") if isinstance(body.get("identity"), dict) else {}
        ident_tag = str(identity.get("tag") or "").lstrip("@").strip()
        ident_file = str(identity.get("file") or "").strip()
        if not video:
            return web.json_response(
                {"ok": False, "error": "pick a reference clip first"})
        mode_raw = str(body.get("mode") or "").strip().lower()
        background = str(body.get("background") or "").strip().lower()
        background_tag = str(body.get("background_tag") or "").lstrip("@").strip()
        wardrobe_tag = str(body.get("wardrobe_tag") or "").lstrip("@").strip()
        try:
            start = float(body.get("video_start_s") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(body.get("video_end_s") or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        rail_tags = body.get("rail_tags")
        if not isinstance(rail_tags, list):
            rail_tags = [ident_tag]
        try:
            _llm, _planner, conn = _swap_conn()
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"SWAP failed to load: {exc}"})
        if not conn.get("model"):
            return web.json_response(
                {"ok": False, "error": "no model is selected -- open WRITE, "
                                       "Settings, and pick one."})
        mode = _planner.normalise_swap_mode(mode_raw)
        # Checked here rather than with the other body fields because it needs
        # the planner, and the planner arrives with the connection.
        # keep_person uses the clip as a scene plate and swaps nobody, so
        # requiring an identity there would make the user pick a face the
        # instruct then tells the model to leave alone.
        if not ident_tag and _planner.swap_mode_needs_identity(mode):
            return web.json_response(
                {"ok": False, "error": "pick an identity picture from the rail"})
        # Say what was actually used. A mode picked in the browser and a mode
        # the server acted on are two different things whenever ComfyUI has not
        # been restarted since the Python changed -- an older route ignores the
        # field entirely and silently writes the default, which reads exactly
        # like "the mode does not work".
        print(f"[{TAG}] SWAP: mode={mode} background={background or 'clip'}"
              + (f" background_tag=@{background_tag}" if background_tag else "")
              + (f" wardrobe=@{wardrobe_tag}" if wardrobe_tag else "")
              + (f" identity=@{ident_tag}" if ident_tag else " identity=none"),
              flush=True)

        import asyncio

        def encode():
            images = []
            if ident_file:
                url = _media.vision_data_url(ident_file)
                if url:
                    images.append({"tag": ident_tag, "data_url": url})
            # Three frames across the trimmed window, not one. A single frame
            # is a POSE: the model described somebody standing with their hands
            # on a bed and could not describe what the clip was DOING, because
            # motion does not exist in one frame. The beat then fought the clip
            # it was supposed to follow, since at cfg 1.0 a beat is additive.
            #
            # This is also what "video vision" is in practice: the VLMs that
            # advertise it sample frames and present them in order. Saying the
            # order out loud is what turns three pictures into a movement.
            frames = _media.video_frame_data_urls(
                video, start=start, end=end, count=3)
            # Not the loop variable: `video_frame_data_urls` documents [] as
            # its failure mode, and returning `frame` from an empty loop raised
            # UnboundLocalError into the handler below -- which then blamed the
            # identity stills and threw away the ones it had already encoded.
            first = frames[0] if frames else None
            for k, frame in enumerate(frames, 1):
                images.append({
                    "caption": (
                        f"Reference clip, frame {k} of {len(frames)}, in time "
                        f"order{' (the trim IN point)' if k == 1 else ''}. "
                        "Read them together to see what the clip is DOING -- "
                        "the movement between them is the action to describe. "
                        "These are NOT a @tag. Do not write @reference_video."
                    ),
                    "data_url": frame,
                })
            return images, first

        try:
            images, frame = await asyncio.get_running_loop().run_in_executor(
                None, encode)
        except Exception as exc:
            print(f"[{TAG}] SWAP could not attach stills: {exc!r}", flush=True)
            images, frame = [], None
        if not frame:
            return web.json_response(
                {"ok": False, "error": "could not extract a frame at that "
                                       "IN point. Trim the clip on MEDIA."})

        vision_notes = []

        async def complete_fn(messages, schema=None):
            try:
                return await _llm.complete(
                    conn["server_url"], conn["model"], messages,
                    schema=schema, temperature=conn["temperature"])
            except _llm.LLMError as exc:
                if _llm.has_images(messages) and "HTTP 4" in str(exc):
                    print(f"[{TAG}] server rejected SWAP stills; "
                          f"retrying from filenames only", flush=True)
                    vision_notes.append(
                        "the model could not look at the pictures; "
                        "the draft used filenames only")
                    return await _llm.complete(
                        conn["server_url"], conn["model"],
                        _llm.text_only(messages),
                        schema=schema, temperature=conn["temperature"])
                raise

        try:
            out = await _planner.write_swap_plan(
                brief, complete_fn=complete_fn, identity_tag=ident_tag,
                rail_tags=rail_tags, images=images, duration=duration,
                mode=mode, background=background,
                background_tag=background_tag, wardrobe_tag=wardrobe_tag)
        except _llm.LLMError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:
            print(f"[{TAG}] swap_plan failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)})
        if vision_notes:
            out["warnings"] = list(out.get("warnings") or []) + vision_notes
        if "ref_plan" in out:
            out.pop("ref_plan", None)
        if not conn.get("keep_warm"):
            try:
                await _llm.unload(conn["server_url"], conn["model"])
            except Exception:
                pass
        if out.get("ok"):
            print(f"[{TAG}] SWAP wrote a 1-hop plan in {out.get('attempts')} "
                  f"attempt(s)", flush=True)
        return web.json_response(out)

    # -- the retake timeline: pictures and playback of cached hops --------
    #
    # Read-only views of the hop store, shaped like /peaks: the decode and
    # the encode run in an executor, never on the event loop. A key the store
    # does not hold is a 404 with `missing: true` -- the cache lives in
    # ComfyUI's temp folder, which is wiped on every start, so "not there" is
    # the ordinary state after a restart, not an error.

    def _hop_store():
        import folder_paths
        from . import store as _store

        root = _os.path.join(folder_paths.get_temp_directory(), "h3_ref_chain_hops")
        return _store.HopStore(root) if _os.path.isdir(root) else None

    def _key_arg(request, name="key"):
        key = str(request.query.get(name) or "").strip().lower()
        # Hex only: the key becomes a filename, so nothing else gets near a path.
        if not key or len(key) > 128 or any(c not in "0123456789abcdef" for c in key):
            return None
        return key

    def _missing(what):
        return web.json_response(
            {"ok": False, "missing": True,
             "error": f"{what} is not in the hop cache (ComfyUI restarted, the "
                      f"cache was swept, or cache_hops is off). Queue once."},
            status=404)

    async def _run(work):
        import asyncio

        return await asyncio.get_running_loop().run_in_executor(None, work)

    @instance.routes.get(HOP_SPRITE_ROUTE)
    async def _hop_sprite(request):
        """A hop's scrub sheet: `n` frames side by side, JPEG."""
        from . import strip as _strip

        key = _key_arg(request)
        if key is None:
            return web.json_response({"ok": False, "error": "bad key"}, status=400)
        try:
            n = max(1, min(64, int(request.query.get("n") or _strip.SPRITE_N)))
        except ValueError:
            n = _strip.SPRITE_N
        hit = _SPRITES.get((key, n))
        if hit is None:
            hs = _hop_store()
            if hs is None or not hs.has(key):
                return _missing(f"hop {key[:8]}")

            def work():
                # Only the n frames the sheet shows, decoded small. `get()`
                # would build a full-resolution float copy of the whole hop.
                info = hs.meta(key)
                if info is None:
                    return None
                total = int(info["frames"])
                idx = [round(j * (total - 1) / max(1, n - 1)) for j in range(n)]
                frames = hs.frames(key, _strip.SPRITE_H, idx)
                return None if frames is None else _strip.sprite(frames, n)

            try:
                hit = await _run(work)
            except Exception as exc:  # noqa: BLE001
                print(f"[{TAG}] hop_sprite failed for {key[:8]}: {exc!r}", flush=True)
                return web.json_response({"ok": False, "error": str(exc)}, status=500)
            if hit is None:
                return _missing(f"hop {key[:8]}")
            if len(_SPRITES) >= _SPRITES_MAX:
                _SPRITES.clear()
            _SPRITES[(key, n)] = hit
        return web.Response(body=hit[0], content_type="image/jpeg",
                            headers={"X-Cells": str(hit[1]),
                                     "Cache-Control": "max-age=3600"})

    async def _serve_clip(name, build):
        """Encode once into the store's preview folder, then serve the file.

        A file response, because a browser `<video>` seeks with byte ranges.
        The folder sits inside the hop store, so it is wiped with it.
        """
        hs = _hop_store()
        if hs is None:
            return None
        out_dir = _os.path.join(hs.root, "previews")
        _os.makedirs(out_dir, exist_ok=True)
        path = _os.path.join(out_dir, name + ".mp4")
        if not _os.path.exists(path):
            ok = await _run(lambda: build(hs, path))
            if not ok:
                return None
        return web.FileResponse(path, headers={"Content-Type": "video/mp4"})

    @instance.routes.get(HOP_CLIP_ROUTE)
    async def _hop_clip(request):
        """One cached hop, whole, with its audio, as a small MP4."""
        from . import strip as _strip

        key = _key_arg(request)
        if key is None:
            return web.json_response({"ok": False, "error": "bad key"}, status=400)

        def build(hs, path):
            frames = hs.frames(key, _strip.CLIP_H)
            sound = hs.audio(key)
            if frames is None or sound is None:
                return False
            _strip.encode_clip(frames, sound[0], sound[1], path, fps=hs.fps)
            return True

        try:
            resp = await _serve_clip("hop_" + key, build)
        except Exception as exc:  # noqa: BLE001
            print(f"[{TAG}] hop_clip failed for {key[:8]}: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return resp or _missing(f"hop {key[:8]}")

    @instance.routes.get(SEAM_CLIP_ROUTE)
    async def _seam_clip(request):
        """A join as the master delivers it: `a`'s tail, then `b` past the trim.

        `trim` is the overlap in frames, 0 for a restart. The timeline passes
        the value the run itself used, from the run record.
        """
        from . import strip as _strip

        a = _key_arg(request, "a")
        b = _key_arg(request, "b")
        if a is None or b is None:
            return web.json_response({"ok": False, "error": "bad key"}, status=400)
        try:
            trim = max(0, min(240, int(request.query.get("trim") or 0)))
        except ValueError:
            trim = 0
        try:
            tail = max(0, min(240, int(request.query.get("tail") or 0)))
        except ValueError:
            tail = 0

        def build(hs, path):
            ma, mb = hs.meta(a), hs.meta(b)
            sa, sb = hs.audio(a), hs.audio(b)
            if None in (ma, mb, sa, sb):
                return False
            ia, ib = _strip.seam_indices(ma["frames"], mb["frames"], trim, fps=hs.fps,
                                         tail_frames=tail)
            fa = hs.frames(a, _strip.CLIP_H, ia)
            fb = hs.frames(b, _strip.CLIP_H, ib)
            if fa is None or fb is None:
                return False
            imgs, wav = _strip.seam(fa, sa[0], fb, sb[0], sa[1], trim, fps=hs.fps,
                                    tail_frames=tail)
            _strip.encode_clip(imgs, wav, sa[1], path, fps=hs.fps)
            return True

        try:
            resp = await _serve_clip(f"seam_{a}_{b}_{trim}_{tail}", build)
        except Exception as exc:  # noqa: BLE001
            print(f"[{TAG}] seam_clip failed for {a[:8]}|{b[:8]}: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        return resp or _missing(f"the {a[:8]}|{b[:8]} join")

    return True
