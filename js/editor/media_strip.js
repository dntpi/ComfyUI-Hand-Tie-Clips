/**
 * The single media inputs: first frame, reference clip, voice, soundtrack.
 *
 * These were `start_image`, `reference_video` and `voice` IMAGE/AUDIO sockets.
 * Together with the nine `ref_image_N` slots they were twelve of a sixteen
 * socket column that occupied a third of the node before the editor started.
 * They are filenames now, in `start_image_file` / `reference_video_file` /
 * `voice_file`, and this strip is what sets them.
 *
 * Same rendering-layer contract as the rest of the editor: the widget is the
 * store, this only draws it and writes back through `widget.callback`.
 */

import { el, widgetByName } from "./widget_utils.js";
import { createPicker } from "./media_picker.js";
import { createTrimBar, forgetPeaks } from "./trim_bar.js";

/* Widget name -> what it is, in the order they are drawn.
 *
 * The fifth entry is the widget PREFIX of the trim window, or null for a slot
 * that cannot be trimmed. A still has no duration, so the first frame has none;
 * the other three each own a `<prefix>_start_s` / `<prefix>_end_s` pair.
 *
 * The sixth is a description widget, or null. Only the reference clip has one:
 * a picture in the rail carries its own `desc`, and the clip had nowhere to say
 * what it was for -- so it went to the encoder as <Video 1> with nothing naming
 * it, which is the same uncited-reference problem the stills had. */
const SLOTS = [
    ["start_image_file", "image", "first frame",
     "Pins hop 1's opening frame. Ignored on later hops -- they are pinned by the join.",
     null, null, null],
    ["reference_video_file", "video", "reference clip",
     "A motion or look plate the whole chain reads. NOT the previous hop; the join handles that.",
     "reference_video", "reference_video_desc", "reference_video_size"],
    ["voice_file", "audio", "voice",
     "Voice or timbre reference for hop 1 as <Audio 1>. Later hops use the pin.",
     "voice", null, null],
    // Not a reference at all: this one is never shown to the model. It is mixed
    // under the finished chain after the last hop is joined, so it sits here
    // because this is where you look for audio -- not because it behaves like
    // its neighbours. The dials that shape it live in RUN > soundtrack.
    ["soundtrack_file", "audio", "soundtrack",
     "Music bed mixed under the whole chain once it is joined. Not a reference -- the model never hears it.",
     "music", null, null],
];

/** The widget names this strip owns, so the caller hides exactly those. */
export const MEDIA_WIDGETS = SLOTS.flatMap(([name, , , , trim, desc, size]) => [
    name,
    ...(trim ? [`${trim}_start_s`, `${trim}_end_s`] : []),
    ...(desc ? [desc] : []),
    ...(size ? [size] : []),
]);

function commit(node, w, value) {
    w.value = value;
    try {
        // Same reason as the run panel: setting `.value` alone leaves anything
        // bound to this widget unaware that it changed.
        w.callback?.(value, node.graph?.canvas, node, undefined, undefined);
    } catch (err) {
        console.error(`[HandTieClips] widget callback for ${w.name} failed:`, err);
    }
    node.graph?.setDirtyCanvas?.(true, true);
}

export function createMediaStrip(node, { onChange } = {}) {
    const root = el("div", "h3e-section h3e-media");

    const head = el("div", "h3e-head");
    head.appendChild(el("span", "h3e-title", "MEDIA"));
    const count = el("span", "h3e-count");
    head.appendChild(count);
    root.appendChild(head);

    const grid = el("div", "h3e-media-grid");
    root.appendChild(grid);

    const trims = el("div", "h3e-trims");
    root.appendChild(trims);

    const pickers = [];
    const bars = [];
    let missing = 0;

    function syncCount() {
        let set = 0;
        for (const [name] of SLOTS) {
            if (String(widgetByName(node, name)?.value || "")) set += 1;
        }
        count.textContent = set ? `${set} set` : "none set";
    }

    const trimRows = [];

    // The bars, the header and the empty-row labels, deliberately NOT the
    // pickers: this runs from inside a picker's own `set`, and re-entering
    // that picker's render in the middle of its own callback is how you get
    // a control that fights itself.
    function afterPick() {
        syncCount();
        for (const fn of trimRows) fn();
        for (const b of bars) b.render();
    }

    for (const [name, kind, label, tip, trim, descName, sizeName] of SLOTS) {
        const w = widgetByName(node, name);
        if (!w) {
            // A widget this build does not define is skipped, not warned about,
            // so the strip survives an older Python side. Same rule as the run
            // panel's group list.
            missing += 1;
            continue;
        }
        const cell = el("label", "h3e-media-cell");
        cell.appendChild(el("span", "h3e-media-label", label));
        // Looked up before the picker so a pick can zero the window. The bar
        // is built later; the widgets are the store either way.
        const ws = trim && widgetByName(node, `${trim}_start_s`);
        const we = trim && widgetByName(node, `${trim}_end_s`);
        const pick = createPicker({
            kind,
            get: () => String(w.value || ""),
            // A pick has to redraw the bar under it. The strip's `onChange` is
            // only `setDirtyCanvas`, which repaints the litegraph node and not
            // this DOM, so before this a newly picked file left its bar hidden
            // until a browser reload rebuilt the strip from scratch.
            //
            // `forgetPeaks` because the client cache is keyed by NAME while the
            // route's is keyed by (name, mtime): re-uploading different audio
            // under a basename already on the canvas would otherwise draw the
            // old file's waveform over the new one's duration.
            //
            // The window is reset because it lives on the node, not the file:
            // a 34 s IN from a previous take would otherwise sit on a 5 s
            // replacement and the bar would read "0.00 s of 4.94 s" with the
            // IN grip jammed at the end. The render already falls back
            // (`clip_window`); this makes the bar tell the same story.
            set: (v) => {
                commit(node, w, v);
                if (v) forgetPeaks(v);
                if (ws && we) { commit(node, ws, 0); commit(node, we, 0); }
                afterPick();
            },
            onChange,
            title: tip,
        });
        cell.title = tip;
        cell.appendChild(pick.root);
        grid.appendChild(cell);
        pickers.push(pick);

        // The bar goes in its own full-width row under the grid, not inside the
        // 104px cell: a waveform squeezed to a thumbnail's width is a smear,
        // and the whole point is to see where the transients are.
        if (ws && we) {
            const row = el("div", "h3e-trimrow");
            row.appendChild(el("span", "h3e-media-label", label));
            const bar = createTrimBar({
                kind,
                name: () => String(w.value || ""),
                get: () => ({ start: Number(ws.value) || 0, end: Number(we.value) || 0 }),
                set: (s, e) => { commit(node, ws, s); commit(node, we, e); },
                onChange,
            });
            row.appendChild(bar.root);
            trims.appendChild(row);
            bars.push(bar);
            // The bar hides its own root when no file is set; the row's label
            // is outside that root, so without this an empty slot still prints
            // "REFERENCE CLIP" / "SOUNDTRACK" above the one that is set.
            const syncRow = () => {
                row.style.display = String(w.value || "") ? "" : "none";
            };
            trimRows.push(syncRow);
            syncRow();
        }

        // "What is this clip for", full width under the bar. Shown only when a
        // file is set, for the same reason the trim row is: an input asking
        // about a reference that is not there is noise.
        const wd = descName && widgetByName(node, descName);
        if (wd) {
            const row = el("div", "h3e-trimrow");
            row.appendChild(el("span", "h3e-media-label", "describe it"));
            const input = el("input", "h3e-media-desc");
            input.type = "text";
            input.placeholder = "a slow dolly along the counter";
            input.title = "What the clip is for, in your words. It goes in as "
                + "<Video 1> either way; this is the only thing that tells the "
                + "encoder why. A reference the prompt never explains tends to "
                + "get rendered as the shot.";
            input.value = String(wd.value || "");
            // `input`, not `change`: a description typed and then abandoned by
            // clicking elsewhere on the canvas would never have fired `change`.
            input.addEventListener("input", () => commit(node, wd, input.value));
            row.appendChild(input);
            trims.appendChild(row);
            const syncDesc = () => {
                row.style.display = String(w.value || "") ? "" : "none";
                if (document.activeElement !== input) {
                    input.value = String(wd.value || "");
                }
            };
            trimRows.push(syncDesc);
            syncDesc();
        }

        // How large to DECODE the clip. Not a crop and not a quality dial for
        // the render: core resizes a reference video to H3's canvas anyway, but
        // only after the whole thing has been decoded, stacked and converted to
        // float32 at source resolution. "match H3" spends none of that. The
        // lower rungs go below what core would use, which is the only setting
        // here that changes what the model sees.
        // NOT `ws` -- that is the trim START widget, declared at the top of this
        // same block. Redeclaring it is a parse error, which takes the whole
        // module down and drops the node back to raw widgets.
        const wsize = sizeName && widgetByName(node, sizeName);
        if (wsize) {
            const row = el("div", "h3e-trimrow");
            row.appendChild(el("span", "h3e-media-label", "decode at"));
            const sel = el("select", "h3e-media-size");
            sel.title = "Decode size for the clip. 'match H3' is the size core "
                + "would resize it to anyway, so the model sees the same pixels "
                + "without the memory: a 10 s 4K plate costs ~36 GB decoded at "
                + "source and ~4.5 GB at H3's size. A clip already smaller is "
                + "left alone -- this never scales up.";
            for (const opt of (wsize.options?.values || [])) {
                const o = el("option", null, String(opt));
                o.value = String(opt);
                sel.appendChild(o);
            }
            sel.value = String(wsize.value ?? "");
            sel.addEventListener("change", () => commit(node, wsize, sel.value));
            row.appendChild(sel);
            trims.appendChild(row);
            const syncSize = () => {
                row.style.display = String(w.value || "") ? "" : "none";
                if (document.activeElement !== sel) {
                    sel.value = String(wsize.value ?? "");
                }
            };
            trimRows.push(syncSize);
            syncSize();
        }
    }

    if (missing === SLOTS.length) {
        grid.appendChild(el("div", "h3e-note h3e-note-error",
            "No media widgets found — restart ComfyUI so the Python side matches."));
    }

    function render() {
        syncCount();
        for (const fn of trimRows) fn();
        for (const p of pickers) p.render();
        for (const b of bars) b.render();
    }

    render();

    return {
        root,
        render,
        destroy() { for (const b of bars) b.destroy(); },
        // The six trim widgets are owned here too, so they stop rendering as
        // raw dials on the node body. MEDIA_WIDGETS is derived, not typed out.
        ownedNames: () => MEDIA_WIDGETS.slice(),
    };
}
