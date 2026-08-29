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
            thumb.appendChild(el("span", "h3e-thumb-hint",
                kind === "video" ? "▶ clip" : "♪ audio"));
        }
        if (name) {
            thumb.title = `${name}\nClick to replace, or drop a new file.`;
            const x = button("×", "Clear this picture", (e) => {
                e.stopPropagation();
                set("");
                paint();
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
        busy = true;
        paint();
        try {
            const got = await upload(list.slice(0, 1));
            if (got.length) {
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
