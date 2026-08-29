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
"""

import os as _os

from . import directives as _d
from . import media as _media
from . import refs as _refs

TAG = "HandTieClips"
ROUTE = "/h3_ref_chain/vocab"
UPLOAD_ROUTE = "/h3_ref_chain/upload"
FILES_ROUTE = "/h3_ref_chain/files"

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

    return True
