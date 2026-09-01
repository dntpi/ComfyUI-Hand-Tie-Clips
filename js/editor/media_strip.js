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

/* Widget name -> what it is, in the order they are drawn. */
const SLOTS = [
    ["start_image_file", "image", "first frame",
     "Pins hop 1's opening frame. Ignored on later hops -- they are pinned by the join."],
    ["reference_video_file", "video", "reference clip",
     "A motion or look plate the whole chain reads. NOT the previous hop; the join handles that."],
    ["voice_file", "audio", "voice",
     "Voice or timbre reference. Rides every hop as <Audio 1>."],
    // Not a reference at all: this one is never shown to the model. It is mixed
    // under the finished chain after the last hop is joined, so it sits here
    // because this is where you look for audio -- not because it behaves like
    // its neighbours. The dials that shape it live in RUN > soundtrack.
    ["soundtrack_file", "audio", "soundtrack",
     "Music bed mixed under the whole chain once it is joined. Not a reference -- the model never hears it."],
];

/** The widget names this strip owns, so the caller hides exactly those. */
export const MEDIA_WIDGETS = SLOTS.map(([name]) => name);

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

    const pickers = [];
    let missing = 0;

    for (const [name, kind, label, tip] of SLOTS) {
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
            set: (v) => commit(node, w, v),
            onChange,
            title: tip,
        });
        cell.title = tip;
        cell.appendChild(pick.root);
        grid.appendChild(cell);
        pickers.push(pick);
    }

    if (missing === SLOTS.length) {
        grid.appendChild(el("div", "h3e-note h3e-note-error",
            "No media widgets found — restart ComfyUI so the Python side matches."));
    }

    function render() {
        let set = 0;
        for (const [name] of SLOTS) {
            if (String(widgetByName(node, name)?.value || "")) set += 1;
        }
        count.textContent = set ? `${set} set` : "none set";
        for (const p of pickers) p.render();
    }

    render();

    return { root, render, ownedNames: () => MEDIA_WIDGETS.slice() };
}
