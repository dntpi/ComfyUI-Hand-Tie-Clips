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
 * the other three each own a `<prefix>_start_s` / `<prefix>_end_s` pair. */
const SLOTS = [
    ["start_image_file", "image", "first frame",
     "Pins hop 1's opening frame. Ignored on later hops -- they are pinned by the join.",
     null],
    ["reference_video_file", "video", "reference clip",
     "A motion or look plate the whole chain reads. NOT the previous hop; the join handles that.",
     "reference_video"],
    ["voice_file", "audio", "voice",
     "Voice or timbre reference. Rides every hop as <Audio 1>.",
     "voice"],
    // Not a reference at all: this one is never shown to the model. It is mixed
    // under the finished chain after the last hop is joined, so it sits here
    // because this is where you look for audio -- not because it behaves like
    // its neighbours. The dials that shape it live in RUN > soundtrack.
    ["soundtrack_file", "audio", "soundtrack",
     "Music bed mixed under the whole chain once it is joined. Not a reference -- the model never hears it.",
     "music"],
];

/** The widget names this strip owns, so the caller hides exactly those. */
export const MEDIA_WIDGETS = SLOTS.flatMap(
    ([name, , , , trim]) => (trim ? [name, `${trim}_start_s`, `${trim}_end_s`] : [name]));

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

    // The bars and the header, deliberately NOT the pickers: this runs from
    // inside a picker's own `set`, and re-entering that picker's render in the
    // middle of its own callback is how you get a control that fights itself.
    function afterPick() {
        syncCount();
        for (const b of bars) b.render();
    }

    for (const [name, kind, label, tip, trim] of SLOTS) {
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
            set: (v) => { commit(node, w, v); forgetPeaks(v); afterPick(); },
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
        const ws = trim && widgetByName(node, `${trim}_start_s`);
        const we = trim && widgetByName(node, `${trim}_end_s`);
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
        }
    }

    if (missing === SLOTS.length) {
        grid.appendChild(el("div", "h3e-note h3e-note-error",
            "No media widgets found — restart ComfyUI so the Python side matches."));
    }

    function render() {
        syncCount();
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
