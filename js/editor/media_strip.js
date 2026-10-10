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
 * A trim prefix ending in `*` is a FIXED-WIDTH window: it owns a
 * `<prefix>_start_s` and no `_end_s`, because its width is not a choice the
 * user gets to make. Master audio is the only one -- the window is the chain
 * (duration x shots - overlap), so an OUT grip would be a control that does
 * nothing. See `createTrimBar`'s `span`.
 *
 * The sixth is a description widget, or null. Only the reference clip has one:
 * a picture in the rail carries its own `desc`, and the clip had nowhere to say
 * what it was for -- so it went to the encoder as <Video 1> with nothing naming
 * it, which is the same uncited-reference problem the stills had. */
// Display order, and it is NOT the Python widget order. `INPUT_TYPES` is
// positional -- slots 2 and 3 were appended after slot 1 because a widget added
// anywhere but the end reassigns every later value in every saved workflow --
// but nothing here is positional, so the strip is free to group by KIND while
// Python stays in the order it must. Rendering reads this array; the only other
// uses are gathering names to hide and counting filled slots, neither of which
// cares about order.
//
// Grouped clips-then-voices because the alternative shipped for one afternoon
// and read wrong immediately: REFERENCE CLIP 1, VOICE 1, REFERENCE CLIP 2 put
// the first voice between the first and second clip, which makes VOICE 1 look
// like a property of the clip beside it rather than the first of three.
const SLOTS = [
    ["start_image_file", "image", "first frame",
     "Pins hop 1's opening frame. Ignored on later hops -- they are pinned by the join.",
     null, null, null],
    // Three reference videos and three standalone reference audios, which is
    // Core's declared ceiling (io.Autogrow max=3 on both) rather than ours.
    // Numbering is DENSE -- clearing slot 2 renumbers slot 3, so a beat citing
    // an ordinal would then name a different clip. Slots 2 and 3 share slot 1's
    // decode size, which is a memory budget rather than a creative choice.
    ["reference_video_file", "video", "reference clip 1",
     "A motion or look plate the whole chain reads, cited as <Video 1>. NOT the previous hop; the join handles that.",
     "reference_video", "reference_video_desc", "reference_video_size"],
    ["reference_video_2_file", "video", "reference clip 2",
     "Second motion/look plate, cited as <Video 2>. Shares slot 1's decode size.",
     "reference_video_2", null, null],
    ["reference_video_3_file", "video", "reference clip 3",
     "Third motion/look plate, cited as <Video 3>. Shares slot 1's decode size.",
     "reference_video_3", null, null],
    ["voice_file", "audio", "voice 1",
     "Voice or timbre reference for hop 1, cited as <Audio 1>. Later hops use the pin.",
     "voice", null, null],
    ["voice_2_file", "audio", "voice 2",
     "Second voice reference, cited as <Audio 2>. Every reference audio is attended on every step of every hop -- trim it.",
     "voice_2", null, null],
    ["voice_3_file", "audio", "voice 3",
     "Third voice reference, cited as <Audio 3>. Trim it.",
     "voice_3", null, null],
    // The last two are not references and the model is never shown either of
    // them. They sit here because this is where you look for audio, not because
    // they behave like the slots above.
    //
    // The soundtrack is mixed under the finished chain after the last hop is
    // joined; the dials that shape it live in RUN > soundtrack.
    ["soundtrack_file", "audio", "soundtrack",
     "Music bed mixed under the whole chain once it is joined. Not a reference -- the model never hears it.",
     "music", null, null],
    ["master_audio_file", "audio", "master audio",
     "The spoken take for the whole chain. Every hop is given its own window of this file and generates the picture to match, so the voice is yours rather than the model's. Not a reference and not the SOUNDTRACK beside it: this one is delivered verbatim, no VAE round trip. Empty = the model generates a voice, as before.",
     "master_audio*", null, null],
];

/** The widget names this strip owns, so the caller hides exactly those. */
/** "voice_2" -> {prefix:"voice_2", fixed:false}; "master_audio*" -> fixed. */
function trimSpec(trim) {
    if (!trim) return null;
    const fixed = trim.endsWith("*");
    return { prefix: fixed ? trim.slice(0, -1) : trim, fixed };
}

export const MEDIA_WIDGETS = SLOTS.flatMap(([name, , , , trim, desc, size]) => {
    const t = trimSpec(trim);
    return [
        name,
        // A fixed-width window has no `_end_s` to own. Listing one anyway
        // would put a name in `ownedNames` that the node never defines, and
        // the caller hides widgets BY NAME.
        ...(t ? [`${t.prefix}_start_s`,
            ...(t.fixed ? [] : [`${t.prefix}_end_s`])] : []),
        ...(desc ? [desc] : []),
        ...(size ? [size] : []),
    ];
});

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

/**
 * @param chainSeconds () => seconds the chain will run, for the fixed-width
 *   master-audio window. Absent or 0 falls back to an ordinary two-grip trim,
 *   so an older caller still gets a working bar.
 */
export function createMediaStrip(node, { onChange, chainSeconds } = {}) {
    const root = el("div", "h3e-section h3e-media");

    const head = el("div", "h3e-head");
    head.appendChild(el("span", "h3e-title", "MEDIA"));
    const count = el("span", "h3e-count");
    head.appendChild(count);
    root.appendChild(head);

    const grid = el("div", "h3e-media-grid");
    root.appendChild(grid);

    /* D1g: there is NO tray at the bottom any more.
     *
     * The trim window, the description and the size went into one container
     * under the whole grid, which meant a VOICE 3 scrubber sat below MASTER
     * AUDIO, labelled "VOICE 3" because it had to name itself -- detached from
     * the file it scrubs, and repeating a label the fold head already shows.
     * They now live inside their own slot's body, where the label beside them
     * already says what they are. */

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
    /* D7: one repaint per fold row. A pick, a clear and a workflow load all
     * need the head's state and the body's visibility to agree with the widget,
     * and they arrive by three different routes -- so they all funnel here. */
    const foldRows = [];

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
        /* D7: an EMPTY slot folds to one dimmed line.
         *
         * Eleven of the twelve media slots are empty on a typical chain, and a
         * grid of twelve full cells for eleven blanks is the panel saying
         * "there is more here" when there is not. So an unfilled slot shows
         * `voice 2 · empty` and a disclosure, and expands on click.
         *
         * **The widget is never un-drawn.** run_panel.js's group header says
         * why, and it is the whole constraint: a slot whose DOM is not created
         * comes back as a RAW LiteGraph widget below the panel, which is worse
         * than the clutter this removes. So the cell, its label and its picker
         * are always built and always in the DOM -- folding hides the BODY and
         * the cell stays a real element. What changes is `display`, which is
         * the safe direction: nothing can fail to reappear.
         *
         * The fold state is view state in node.properties, so it survives a
         * re-render and a workflow reload, exactly like the plan editor's shot
         * folds. */
        const foldKey = `h3_media_fold_${name}`;
        const isSet = () => String(w.value || "").trim() !== "";
        // A slot that holds a file starts OPEN. Folding something the user
        // just picked would hide their own choice behind a click.
        const isFolded = () => {
            if (isSet()) return false;
            const v = node.properties?.[foldKey];
            return v === undefined ? true : Boolean(v);
        };

        /* D1h: a DIV, not a <label>.
     *
     * It was a label, and a label forwards every click on a descendant to the
     * control it wraps -- there is a hidden <input type="file"> in here. The
     * picker has its OWN click handler on the thumbnail (input.click(), line
     * ~287), which is what should open the browser and all it ever needed.
     *
     * The label was therefore doing the same job twice, and the second time it
     * covered the WHOLE row: dragging the scrubber, dragging the window grips,
     * clicking the waveform -- any of them ended with a file dialog opening.
     * The user found it immediately, and it is the reason the file browser
     * "appeared" after scrubbing rather than after clicking the picture.
     *
     * A div has no activation behaviour, so only the thumbnail opens anything.
     * The label was also doing no accessibility work: the control it labelled
     * was `display: none`, so there was no labelable element to point at. */
    const cell = el("div", "h3e-media-cell");
        const head = el("span", "h3e-media-head");
        const tri = el("span", "h3e-media-tri", "");
        head.appendChild(tri);
        const labelEl = el("span", "h3e-media-label", label);
        head.appendChild(labelEl);
        const stateTag = el("span", "h3e-media-state");
        head.appendChild(stateTag);
        cell.appendChild(head);

        const body = el("span", "h3e-media-body");
        // Looked up before the picker so a pick can zero the window. The bar
        // is built later; the widgets are the store either way.
        const tspec = trimSpec(trim);
        const ws = tspec && widgetByName(node, `${tspec.prefix}_start_s`);
        const we = tspec && !tspec.fixed
            && widgetByName(node, `${tspec.prefix}_end_s`);
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
                if (ws) commit(node, ws, 0);
                if (we) commit(node, we, 0);
                afterPick();
            },
            onChange,
            title: tip,
        });
        cell.title = tip;
        body.appendChild(pick.root);
        grid.appendChild(cell);
        // The body is appended here, after everything that fills it. Built at
        // the END rather than inserted at the top so the fold head is already
        // the cell's first child.
        cell.appendChild(body);

        /* The fold. A <details> would do this in one element, but the cell is a
         * <label> wrapping a file input, and nesting a <summary> there puts a
         * click target inside a label -- clicking the disclosure would also
         * activate the input. So it is a span with a click handler, and the
         * triangle is a text character rather than a ::marker. */
        const setFold = () => {
            const folded = isFolded();
            cell.classList.toggle("h3e-media-folded", folded);
            body.style.display = folded ? "none" : "";
            tri.textContent = folded ? "▸" : "▾";
            stateTag.textContent = isSet() ? "" : (folded ? "empty ▸" : "empty");
            cell.classList.toggle("h3e-media-set", isSet());
            labelEl.classList.toggle("h3e-media-label-empty", !isSet());
        };
        head.addEventListener("click", (e) => {
            // The head is inside a <label>; without this a click on the label
            // text opens the file dialog as well as the fold.
            e.preventDefault();
            e.stopPropagation();
            if (isSet()) return;         // a filled slot stays open
            node.properties ??= {};
            node.properties[foldKey] = !isFolded();
            setFold();
        });
        // A pick fills the slot, which must open it -- otherwise the user picks
        // a file and the slot stays folded, hiding what they just chose.
        foldRows.push(() => {
            if (isSet() && isFolded()) node.properties[foldKey] = false;
            setFold();
        });
        setFold();
        cell.title = isSet()
            ? `${label}: set. Click the picker below to change it.`
            : `${label}: empty. ${tip}`;
        pickers.push(pick);

        // The bar goes in its own full-width row under the grid, not inside a
        // media cell: a waveform squeezed to a thumbnail's width is a smear,
        // and the whole point is to see where the transients are.
        if (ws && (we || tspec.fixed)) {
            // NOT wide: this is the scrubber, and it shares the line with the
            // file it trims. Only the description and the size take a whole
            // line of their own.
            const row = el("div", "h3e-trimrow");
            const bar = createTrimBar({
                kind,
                name: () => String(w.value || ""),
                get: () => ({
                    start: Number(ws.value) || 0,
                    end: we ? Number(we.value) || 0 : 0,
                }),
                set: (s, e) => {
                    commit(node, ws, s);
                    if (we) commit(node, we, e);
                },
                onChange,
                // Only the fixed slot asks for a width, and only when the
                // caller knows one. Passing `undefined` keeps every other bar
                // on exactly the code path it had.
                span: tspec.fixed && chainSeconds ? chainSeconds : undefined,
            });
            row.appendChild(bar.root);
            body.appendChild(row);
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
            const row = el("div", "h3e-trimrow h3e-trimrow-wide");
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
            body.appendChild(row);
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
            const row = el("div", "h3e-trimrow h3e-trimrow-wide");
            row.appendChild(el("span", "h3e-media-label", "video input size"));
            const sel = el("select", "h3e-media-size");
            sel.title = "How large to decode the clip. MAX is the size core "
                + "would resize it to anyway, so the model sees the same pixels "
                + "without the memory: a 10 s 4K plate costs ~36 GB decoded at "
                + "source and ~4.5 GB at MAX. The megapixel values go below "
                + "that, trading reference detail for memory. A clip already "
                + "smaller than the value you pick is left alone -- this never "
                + "scales up.";
            for (const opt of (wsize.options?.values || [])) {
                const o = el("option", null, String(opt));
                o.value = String(opt);
                sel.appendChild(o);
            }
            sel.value = String(wsize.value ?? "");
            sel.addEventListener("change", () => commit(node, wsize, sel.value));
            row.appendChild(sel);
            body.appendChild(row);
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
        for (const fn of foldRows) fn();
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
