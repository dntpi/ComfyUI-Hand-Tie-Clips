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

TAG = "HandTieClips"
ROUTE = "/h3_ref_chain/vocab"
UPLOAD_ROUTE = "/h3_ref_chain/upload"
FILES_ROUTE = "/h3_ref_chain/files"
LLM_ROUTE = "/h3_ref_chain/llm"
PLAN_ROUTE = "/h3_ref_chain/plan"
UNLOAD_ROUTE = "/h3_ref_chain/llm/unload"

# A batch of stills is a handful; this is a guard against a runaway multipart
# body, not a considered product limit.
MAX_UPLOAD_FILES = 32
MAX_UPLOAD_BYTES = 256 * 1024 * 1024


def _payload():
    from .h3_ref_chain import DURATION_FRAMES, OVERLAP_FRAMES, CANVAS, FPS

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
        "subject_fields": list(_refs.SUBJECT_FIELDS),

        "durations": {k: v for k, v in DURATION_FRAMES.items()},
        "overlaps": {k: v for k, v in OVERLAP_FRAMES.items()},
        "canvas": {res: {asp: list(wh) for asp, wh in by_asp.items()}
                   for res, by_asp in CANVAS.items()},
        "fps": FPS,
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
            kinds = request.rel_url.query.get("kinds") or ""
            want = {k.strip() for k in kinds.split(",") if k.strip()} or None
            return web.json_response({"ok": True, "files": _media.listing(want)})
        except Exception as exc:
            print(f"[{TAG}] files route failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

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
                "unload_after": conn["unload_after"],
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

        try:
            from . import llm as _llm
            from . import planner as _planner
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"the plan writer failed to load: {exc}"})

        conn = _llm.load_conn()
        if not conn.get("model"):
            return web.json_response(
                {"ok": False, "error": "no model is selected -- open Settings "
                                       "in the panel and pick one."})

        # Only the files actually on disk. The model then schedules pictures
        # that exist instead of inventing plausible filenames, which is the
        # difference between a plan that queues and one that stops at the
        # missing-picture check.
        files = [f["name"] for f in _media.listing({"image", "video"})]

        async def complete_fn(messages, schema=None):
            return await _llm.complete(
                conn["server_url"], conn["model"], messages,
                schema=schema, temperature=conn["temperature"])

        try:
            out = await _planner.write_plan(
                brief, hops, complete_fn=complete_fn, files=files)
        except _llm.LLMError as exc:
            return web.json_response({"ok": False, "error": str(exc)})
        except Exception as exc:
            print(f"[{TAG}] plan route failed: {exc!r}", flush=True)
            return web.json_response({"ok": False, "error": str(exc)})

        # Hand the VRAM back before the render that almost certainly follows.
        # A courtesy, never a failure: `unload` swallows its own errors and
        # returns False when no endpoint answers.
        if conn.get("unload_after"):
            try:
                await _llm.unload(conn["server_url"], conn["model"])
            except Exception:
                pass

        if out["ok"]:
            print(f"[{TAG}] wrote a {hops}-hop plan in {out['attempts']} "
                  f"attempt(s)", flush=True)
        return web.json_response(out)

    return True
