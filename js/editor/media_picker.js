/**
 * Files in, without an IMAGE socket.
 *
 * A reference used to be a wire: nine `ref_image_N` sockets, nine `Load Image`
 * nodes, and a sixteen-socket column eating a third of the node. A reference is
 * now a *filename* under `<ComfyUI input>/h3_refs`, and this is the control that
 * puts one there.
 *
 * Two rules, both learned by PromptMasterLD the expensive way:
 *
 *   1. **Pixels never enter a widget.** Only the basename is stored. Nine
 *      base64 thumbnails measured 1.68 MB of widget value and ComfyUI then
 *      failed to save the workflow at all. Previews are URLs, which cost
 *      nothing to rebuild and survive a reload.
 *   2. **Reads go through ComfyUI's own `/view`.** It already serves the input
 *      directory with a guessed MIME type and Range support, which a `<video>`
 *      needs in order to seek. A second thumbnail route would buy nothing.
 *
 * The upload is multipart rather than JSON+base64 so a batch of stills is not
 * inflated by a third and held in memory twice on the way through the browser.
 */

import { el, button } from "./widget_utils.js";

const UPLOAD_URL = "/h3_ref_chain/upload";
const FILES_URL = "/h3_ref_chain/files";
const SUBDIR = "h3_refs";

/** Accept attributes, by what a control is for. */
/* The server's own sets, in `media.py`. Kept short deliberately: this is a
  * guard against the obvious mistake, not a content sniffer. */
const EXTS = {
    image: ["png", "jpg", "jpeg", "webp", "bmp"],
    video: ["mp4", "mov", "webm", "mkv", "avi", "m4v"],
    audio: ["wav", "mp3", "flac", "ogg", "m4a", "aac"],
};

export function extKind(name) {
    const ext = String(name || "").split(".").pop().toLowerCase();
    for (const [k, list] of Object.entries(EXTS)) {
        if (list.includes(ext)) return k;
    }
    return "";
}

export const ACCEPT = {
    image: "image/*",
    video: "video/*",
    audio: "audio/*",
};

/* -- the file list, cached and shared ------------------------------------ */

let _files = null;
let _pending = null;
const _listeners = new Set();

/** Everything currently in the reference folder. Cached; refresh() invalidates. */
export function files() {
    if (_files) return Promise.resolve(_files);
    if (!_pending) {
        _pending = fetch(FILES_URL)
            .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
            .then((j) => {
                _files = Array.isArray(j.files) ? j.files : [];
                return _files;
            })
            .catch((err) => {
                console.error("[HandTieClips] reference list unavailable:", err);
                _files = [];
                return _files;
            })
            .finally(() => { _pending = null; });
    }
    return _pending;
}

/** Re-read the folder and tell every mounted picker. */
export function refresh() {
    _files = null;
    return files().then((list) => {
        for (const fn of _listeners) {
            try {
                fn(list);
            } catch (err) {
                console.error("[HandTieClips] picker refresh failed:", err);
            }
        }
        return list;
    });
}

export function onFilesChanged(fn) {
    _listeners.add(fn);
    return () => _listeners.delete(fn);
}

/**
 * The URL a preview is drawn from.
 *
 * Cache-busted because a filename can be reused: upload `face.png` twice and
 * the second one collision-suffixes, but an *overwritten* file keeps its name
 * and the browser would happily show the old bytes.
 */
export function viewUrl(name) {
    const q = `filename=${encodeURIComponent(name)}`
        + `&subfolder=${encodeURIComponent(SUBDIR)}&type=input&t=${Date.now()}`;
    return `/view?${q}`;
}

/** Push files at the upload route. -> [{name, kind, width, height}] */
export async function upload(fileList) {
    const list = [...(fileList || [])];
    if (!list.length) return [];
    const fd = new FormData();
    for (const f of list) fd.append("files", f, f.name);
    const r = await fetch(UPLOAD_URL, { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || `upload failed (HTTP ${r.status})`);
    for (const note of j.skipped || []) console.warn("[HandTieClips] upload skipped:", note);
    await refresh();
    return j.files || [];
}

/* -- one picker ---------------------------------------------------------- */

/**
 * A thumbnail that takes a drop, plus a list of what is already on disk.
 *
 * `get()` returns the current basename, `set(name)` stores a new one. The
 * control owns no state of its own -- same rendering-layer contract as the rest
 * of the editor, so undo and the JSON tab keep working.
 */
export function createPicker({ kind = "image", get, set, onChange, title } = {}) {
    const root = el("div", "h3e-pick");

    const thumb = el("div", "h3e-thumb");
    thumb.title = title || "Drop a file here, or click to browse.";
    root.appendChild(thumb);

    const input = el("input");
    input.type = "file";
    input.accept = ACCEPT[kind] || "";
    input.style.display = "none";
    root.appendChild(input);

    const picker = el("select", "h3e-select h3e-pickfile");
    root.appendChild(picker);

    let busy = false;

    function paint() {
        const name = String(get() || "");
        thumb.textContent = "";
        thumb.classList.toggle("h3e-has", Boolean(name));
        if (busy) {
            thumb.appendChild(el("span", "h3e-thumb-hint", "uploading…"));
        } else if (!name) {
            thumb.appendChild(el("span", "h3e-thumb-hint", "drop\nor click"));
        } else if (kind === "image") {
            const img = el("img", "h3e-thumb-img");
            img.src = viewUrl(name);
            img.alt = name;
            // A file deleted from the folder behind our back must not leave a
            // broken-image glyph with no explanation.
            img.addEventListener("error", () => {
                thumb.textContent = "";
                thumb.appendChild(el("span", "h3e-thumb-hint h3e-thumb-bad", "missing"));
            });
            thumb.appendChild(img);
        } else {
            /* D1f: audio and video showed a WORD -- "clip", "audio" -- in a box
             * the size of a filmstrip cell, which told the user nothing they
             * could not read from the filename beside it ("the bar for the input
             * just shows the text clip").
             *
             * A video now carries a real frame: a muted, non-looping <video>
             * seeked to 0.1s, which is enough to recognise the plate. An <audio>
             * cannot show a picture, so it keeps a glyph -- but a BETTER one,
             * drawn rather than typed, and sized to the cell.
             *
             * The video element is pointed at ComfyUI's own /view route, which
             * already serves the file with Range support, so this costs no new
             * endpoint. `preload="metadata"` keeps it from pulling the whole
             * clip for one poster frame. */
            if (kind === "video") {
                const v = el("video", "h3e-thumb-vid");
                v.src = viewUrl(name);
                v.muted = true;
                v.loop = true;
                v.playsInline = true;
                v.preload = "metadata";
                // 0.1s is past the first frame in every container we write, and
                // black leading is the most common first frame there is.
                v.addEventListener("loadedmetadata", () => {
                    try { v.currentTime = 0.1; } catch (_) { /* not seekable yet */ }
                }, { once: true });
                // A codec the browser cannot decode must not leave an empty
                // box: fall back to the glyph, which is honest.
                v.addEventListener("error", () => {
                    v.remove();
                    thumb.appendChild(el("span", "h3e-thumb-glyph", "▶"));
                }, { once: true });
                thumb.appendChild(v);
                // Hover plays a second. Not a control -- clicking the thumb
                // opens the file dialog -- so it must not swallow the click.
                thumb.addEventListener("mouseenter", () => {
                    v.play?.()?.catch?.(() => {});
                });
                thumb.addEventListener("mouseleave", () => {
                    try { v.pause(); } catch (_) { /* not playing */ }
                });
            } else {
                thumb.appendChild(el("span", "h3e-thumb-glyph h3e-thumb-wave", "♪"));
            }
        }
        if (name) {
            thumb.title = `${name}\nClick to replace, or drop a new file.`;
            const x = button("×", "Clear this picture", (e) => {
                e.stopPropagation();
                set("");
                paint();
                // D1i: repaint the DROPDOWN too. Clearing only repainted the
                // thumbnail, so the row said `empty` on the left and still
                // listed the old filename on the right -- and the next Save
                // wrote that filename back into shot_plan, which is the exact
                // thing the user had just undone.
                paintList(_files || []);
                onChange?.();
            }, "h3e-btn h3e-thumb-x");
            thumb.appendChild(x);
        } else {
            thumb.title = title || "Drop a file here, or click to browse.";
        }
    }

    function paintList(list) {
        const name = String(get() || "");
        picker.textContent = "";
        const blank = el("option", null, "— none —");
        blank.value = "";
        picker.appendChild(blank);
        let found = false;
        for (const f of list || []) {
            if (f.kind !== kind) continue;
            const o = el("option", null, f.name);
            o.value = f.name;
            if (f.name === name) found = true;
            picker.appendChild(o);
        }
        // A name that is no longer on disk still has to be selectable, or
        // switching away from it would look like the value was never set.
        //
        // ONLY when the widget still holds it. When the widget is EMPTY the
        // slot is cleared, and re-adding the old name put "Screenshot…png" in
        // a dropdown whose thumbnail said `empty` -- the panel contradicting
        // itself on the same row, and clicking Save would write the filename
        // back into shot_plan that the user had just cleared.
        if (name && !found) {
            const o = el("option", null, `${name} (missing)`);
            o.value = name;
            picker.appendChild(o);
        }
        picker.value = name;
    }

    async function take(fileList) {
        const list = [...(fileList || [])];
        if (!list.length) return;
        // `accept` only filters the file dialog -- a drop goes straight
        // past it, which is how an mp3 reached a clip slot and was not
        // caught until the VAE asked it for pixels.
        const wrong = list.slice(0, 1).filter((f) => extKind(f.name) !== kind);
        if (wrong.length) {
            const got = extKind(wrong[0].name) || "unknown";
            console.warn(`[HandTieClips] ${wrong[0].name} is ${got}, not ${kind}`);
            alert(`${wrong[0].name} is a ${got} file. This slot takes ${kind}.`);
            return;
        }
        busy = true;
        paint();
        try {
            const got = await upload(list.slice(0, 1));
            if (got.length) {
                // D1i: the server dedups by content, so the name it hands back
                // can differ from the file that was dropped -- that file is
                // already in the folder under another name. Say so, or the slot
                // looks like it silently substituted a different picture.
                if (got[0].deduped) {
                    console.info(`[HandTieClips] ${list[0] && list[0].name} is `
                        + `already in the folder as ${got[0].name}; using that one.`);
                }
                set(got[0].name);
                onChange?.();
            }
        } catch (err) {
            console.error("[HandTieClips] upload failed:", err);
            alert(`Upload failed: ${err.message || err}`);
        } finally {
            busy = false;
            paint();
        }
    }

    thumb.addEventListener("click", (e) => {
        if (e.target.closest(".h3e-thumb-x")) return;
        input.click();
    });
    input.addEventListener("change", () => {
        take(input.files);
        input.value = "";            // so re-picking the same file still fires
    });
    thumb.addEventListener("dragover", (e) => {
        // Only light up for an actual file drag. Without this check, dragging a
        // node across the canvas highlights every picker it passes over.
        if (!e.dataTransfer?.types?.includes("Files")) return;
        e.preventDefault();
        e.stopPropagation();
        thumb.classList.add("h3e-hot");
    });
    thumb.addEventListener("dragleave", () => thumb.classList.remove("h3e-hot"));
    thumb.addEventListener("drop", (e) => {
        if (!e.dataTransfer?.files?.length) return;
        e.preventDefault();
        e.stopPropagation();
        thumb.classList.remove("h3e-hot");
        take(e.dataTransfer.files);
    });
    picker.addEventListener("change", () => {
        // A parent that rebuilds its DOM in this handler disconnects us
        // and some browsers emit a second change with value "". Ignore
        // that one; the first change already stored the name.
        if (!root.isConnected) return;
        set(picker.value);
        paint();
        onChange?.();
    });

    const stop = onFilesChanged(paintList);
    files().then(paintList);
    paint();

    return {
        root,
        render() {
            paint();
            files().then(paintList);
        },
        destroy: stop,
    };
}
