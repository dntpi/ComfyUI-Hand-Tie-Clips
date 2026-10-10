/**
 * The run panel: every dial that is not per-shot, grouped and labelled.
 *
 * The shot cards made the *script* legible and left the other twenty-odd
 * widgets stacked underneath as a wall of raw controls, in definition order,
 * with no grouping -- so the node read as "a polished editor, then a heap".
 * The dials with the least obvious semantics (`pin_renorm`, `pin_noise`,
 * `cache_budget_gb`) were indistinguishable from the ones nobody touches.
 *
 * Same contract as the shot editor: this is a RENDERING LAYER over the native
 * widgets, never a second source of truth. Every control writes straight back
 * to `widget.value` and fires the widget's own callback, so the workflow JSON
 * is byte-identical to one produced by the stock UI, and anything this panel
 * cannot express is still reachable by dropping it from the group list.
 *
 * Help text comes from the `tooltip` written in INPUT_TYPES, via
 * `tooltipFor()`. Retyping those sentences here would be a second copy that
 * goes stale, for exactly the reason routes.py exists. Reading only
 * `widget.options.tooltip` was not enough and left the whole panel with no
 * tooltips at all while the native dials kept theirs -- see tooltipFor().
 */

import {
    el, vocab, widgetByName, widgetType, widgetOptions, tooltipFor,
} from "./widget_utils.js";

const OPEN_PROP = "h3_run_open";

/**
 * Groups, in draw order; names are native widget names.
 *
 * A name this build does not define is skipped rather than warned about, so the
 * list can carry a widget that only exists on a newer Python side. The flip
 * side is that a *renamed* widget silently drops out of the panel and reappears
 * as a raw dial below it -- the safe direction to fail, and visible the moment
 * you look at the node.
 */
const GROUPS = [
    ["output", ["resolution", "aspect", "duration", "overlap", "chains"]],
    // speed_mode leads the group because it DECLARES what is on the wire, and
    // the dials under it are read in that light. It is not in the refine group
    // even though the only thing it currently moves is refine_head: it
    // describes the base, and the moment it also sets steps/sampler that is
    // where a reader would look for it.
    ["sampling", ["speed_mode", "steps", "sampler_name", "scheduler", "seed",
                  "control_after_generate", "seed_per_shot",
                  "shift_video", "shift_audio"]],
    ["join & pin", ["hop_script", "pin_to_qwen", "ref_image_size",
                    // pin_mech sits with the other pin dials even though it is
                    // the newest widget on the node: the panel's order is
                    // display only, and INPUT_TYPES appends it last so saved
                    // workflows keep their widget indices.
                    "pin_mech",
                    "audio_pin_frames",
                    // Sits with the pin dials because that is what it is
                    // about: whether the timbre reference keeps riding, or
                    // the hop inherits the voice through the audio pin alone.
                    "voice_every_hop",
                    "pin_renorm", "pin_noise",
                    // tone_anchor sits next to the mode it modifies: it is
                    // read only when tone_compensate is `anchor`, and split
                    // across groups nothing says so. Deliberately NOT
                    // suppressed in the other modes -- suppression only stops
                    // the panel drawing a dial, and an undrawn dial reappears
                    // as a raw native widget, which is worse than an inert one.
                    "tone_compensate", "tone_anchor",
                    "last_frame_guide"]],
    // The second sampler pass. Its own group rather than folded into
    // `sampling`, because these do not describe how a hop is rendered -- they
    // describe what happens to it afterwards, and `hop_refine=off` makes every
    // other dial here inert. refine_model is a MODEL socket, so it is a wire on
    // the node and never appears in this list.
    ["refine", ["hop_refine", "refine_denoise", "refine_steps",
                "refine_sampler", "refine_scheduler", "refine_cond",
                "refine_audio",
                // The blend and the head freeze are two answers to one
                // question -- which frames keep the stock sample at the join --
                // so they sit together and below the pass that produces them.
                "refine_blend", "refine_blend_interp", "refine_head",
                // Panel order is reading order, not declaration order; the
                // widget itself is declared last so a saved workflow keeps its
                // indices. It belongs beside the schedule it replaces.
                "refine_align"]],
    // Its own group, not part of refine: boost changes how the hop is sampled.
    // The three LoRAs are combos on this node rather than loader nodes on the
    // canvas -- deliberately, to keep the graph short; the speed stack itself
    // stays drawn upstream.
    ["realism boost", ["realism_boost", "boost_lora_1", "boost_lora_1_strength",
                       "boost_lora_2", "boost_lora_2_strength",
                       "boost_lora_3", "boost_lora_3_strength",
                    "boost_lora_4", "boost_lora_4_strength"]],
    // After the last hop, over the whole film: it changes what is delivered,
    // never what a hop is generated from, so it sits after every group that
    // does.
    ["upsample", ["upsample", "upsample_model", "upsample_megapixels",
                  "upsample_denoise", "upsample_lora", "upsample_lora_strength",
                  "upsample_tile_tokens"]],
    // Wrap every hop's finished prompt: LoRA trigger words first, a trailing
    // camera/body clause last, outside everything the chain adds.
    ["prompt wrap", ["prompt_prefix", "prompt_suffix"]],
    ["cache",["cache_hops", "cache_budget_gb"]],
    // Added 2026-08-30. 0.4.0 shipped these five on the Python side and never
    // touched js/, so all five fell through to native dials -- the documented
    // fallback doing its job, not a break. Order is the order you reach for
    // them: prove the plan compiles, pick the fidelity, stop short, look.
    // `render_from` is next to `render_through` because the two are one
    // control: a range. The shot cards' ⏵ buttons write both of them.
    ["preview", ["dry_run", "quality", "render_from", "render_through",
                 "contact_sheet"]],
    // Last because it is the last thing to happen: the bed goes on after every
    // hop is joined. The file itself is picked in MEDIA -- these four only say
    // how loud it is and how it is laid down, and are inert with nothing set.
    ["soundtrack", ["music_gain_db", "music_duck", "music_fit",
                    "music_fade_s"]],
];

/**
 * Why a dial is not drawn in the current mode.
 *
 * `run()` ignores `chains` and forces `hop_script=next` the moment a shot plan
 * is present (h3_ref_chain.py:1187-1192, which prints both overrides). Drawing
 * them anyway would offer two controls that do nothing, which is worse than not
 * offering them -- so the panel drops them in Shots mode and says why.
 */
/**
 * Dials that need two grid cells.
 *
 * A seed is up to 19 digits and a truncated one cannot be read back to
 * reproduce a render, which is the only reason to look at it.
 */
const WIDE = new Set(["seed"]);

/**
 * The dial whose OFF value makes a whole group a no-op (A5).
 *
 * When the master is off, the group collapses to one dimmed line so a default
 * run shows the knobs that do something, not 65 dials behind three switches
 * that are all off. A group with no master (output, sampling, join) is always
 * drawn open -- there is no "off" that makes the canvas or the sampler
 * meaningless, so a collapsed line would only be a speed bump.
 *
 * The OFF VALUE, not just the name: `hop_refine` also has `pin_only`, which is
 * NOT off -- it still refines for the join. Matching on the widget's own
 * option list keeps this correct if Python renames or reorders the values.
 */
const GROUP_MASTER = {
    "refine": "hop_refine",
    "realism boost": "realism_boost",
    "upsample": "upsample",
    // cache_hops and dry_run both default to off, and both own dials that do
    // nothing while they are: a budget for a cache that is off, and a preview
    // range for a preview that is not running. They are the fourth and fifth
    // collapsed groups on a default run, which is most of why the default
    // count lands near the brief's 21 rather than near 65.
    "cache": "cache_hops",
    "preview": "dry_run",
};
const OFF_VALUES = new Set(["off", "disabled", "none"]);

const SUPPRESSED_WHY = {
    chains: "the shot list sets the hop count",
    hop_script: "a shot plan forces it to `next`",
};

/* -- one field per native widget ----------------------------------------- */

/**
 * Push a value back through the widget.
 *
 * `callback` is how a ComfyUI widget tells the rest of the app it changed --
 * `control_after_generate` binds to the seed through it, and setting `.value`
 * alone would leave that link dead.
 */
function commitWidget(node, w, value, onChange) {
    w.value = value;
    try {
        w.callback?.(value, node.graph?.canvas, node, undefined, undefined);
    } catch (err) {
        console.error(`[HandTieClips] widget callback for ${w.name} failed:`, err);
    }
    node.graph?.setDirtyCanvas?.(true, true);
    onChange?.();
}

export function labelFor(w) {
    return String(w.name || "").replace(/_/g, " ");
}

function comboField(node, w, onChange) {
    const values = widgetOptions(w).values;
    const opts = typeof values === "function" ? values(w, node) : values;
    if (!Array.isArray(opts)) return null;
    const s = el("select", "h3e-select");
    for (const opt of opts) {
        const o = el("option", null, String(opt));
        o.value = String(opt);
        s.appendChild(o);
    }
    s.value = String(w.value ?? "");
    s.addEventListener("change", () => commitWidget(node, w, s.value, onChange));
    return { input: s, read: () => { s.value = String(w.value ?? ""); } };
}

function toggleField(node, w, onChange) {
    const wrap = el("span", "h3e-run-toggle");
    const cb = el("input");
    cb.type = "checkbox";
    cb.checked = Boolean(w.value);
    const text = el("span", "h3e-run-togglabel");
    const paint = () => {
        // A BOOLEAN with label_on/label_off says what each state MEANS
        // ("vary per hop" / "same seed every hop"); a bare on/off throws that away.
        const o = widgetOptions(w);
        text.textContent = cb.checked
            ? (o.label_on || "on")
            : (o.label_off || "off");
    };
    paint();
    cb.addEventListener("change", () => {
        paint();
        commitWidget(node, w, cb.checked, onChange);
    });
    wrap.appendChild(cb);
    wrap.appendChild(text);
    return { input: wrap, read: () => { cb.checked = Boolean(w.value); paint(); } };
}

function numberField(node, w, onChange) {
    const i = el("input", "h3e-input");
    i.type = "number";
    const o = widgetOptions(w);
    if (o.min != null) i.min = String(o.min);
    if (o.max != null) i.max = String(o.max);
    // LiteGraph's `options.step` is ten times the author's step -- a legacy of
    // the canvas drag increment. `step2` carries the real one when the frontend
    // is new enough; divide as the fallback rather than showing a 10x arrow.
    const step = o.step2 ?? (o.step != null ? o.step / 10 : null);
    if (step != null) i.step = String(step);
    // Whether to round on commit. Testing the step alone is not enough: if a
    // frontend ever reports the true step in `step`, the /10 fallback turns an
    // INT's step into 0.1 and `steps` would happily accept 7.5. Requiring the
    // bounds and the current value to be integral too keeps pin_noise (max
    // 0.10) and the shifts (min 0.01) out of it. `cache_budget_gb` is integral
    // on every count and so gets rounded -- a whole-GB budget, which is what
    // its 1.0 step already asks for.
    const isInt = [o.min, o.max, w.value, step]
        .every((v) => v == null || Number.isInteger(Number(v)));
    i.value = String(w.value ?? "");
    const push = () => {
        if (i.value === "") return;               // mid-edit; wait for change
        let v = Number(i.value);
        if (!Number.isFinite(v)) return;
        if (o.min != null) v = Math.max(o.min, v);
        if (o.max != null) v = Math.min(o.max, v);
        if (isInt) v = Math.round(v);
        commitWidget(node, w, v, onChange);
    };
    // Commit on input as well as change: the shot editor learned the hard way
    // that a field committing only on blur loses whatever was typed when the
    // workflow is saved with the caret still in the box.
    i.addEventListener("input", push);
    i.addEventListener("change", () => {
        push();
        i.value = String(w.value ?? "");          // show the clamped value back
    });
    return { input: i, read: () => { i.value = String(w.value ?? ""); } };
}

function textField(node, w, onChange) {
    const i = el("input", "h3e-input");
    i.type = "text";
    i.value = String(w.value ?? "");
    i.addEventListener("input", () => commitWidget(node, w, i.value, onChange));
    return { input: i, read: () => { i.value = String(w.value ?? ""); } };
}

/** null when this widget's type is not one the panel can draw. */
export function fieldFor(node, w, onChange) {
    const t = widgetType(w);
    if (t === "combo") return comboField(node, w, onChange);
    if (t === "toggle" || t === "boolean") return toggleField(node, w, onChange);
    if (t === "number" || t === "int" || t === "float") return numberField(node, w, onChange);
    if (t === "string" || t === "text") {
        // Multiline STRINGs are documents, not dials -- they belong to the shot
        // editor or to their own widget, never squeezed into a 104px grid cell.
        if (widgetOptions(w).multiline) return null;
        return textField(node, w, onChange);
    }
    return null;
}

/* -- speed_mode -----------------------------------------------------------

 * The preset WRITES the widgets instead of shadowing them.
 *
 * run()'s `_apply_speed_mode` overrides its values at render time and logs what
 * moved, which is correct for the server but leaves the panel lying: it would
 * read `refine_head: refine` while the render used `freeze`. That already cost
 * this project a round of measurements where every turbo comparison was
 * silently also a refine_head comparison. Stamping the values in means the
 * dials say what will run, you can disagree with any of them afterwards, and
 * the server-side override then finds nothing to move and stays quiet.
 *
 * The table comes from /vocab (`_speed_mode_table`), never from a copy here.
 */
function stampSpeedMode(node, table, mode, onChange) {
    const row = table?.[mode];
    if (!row) return null;
    const moved = [];
    for (const [name, value] of Object.entries(row)) {
        const w = widgetByName(node, name);
        if (!w) continue;                       // older Python side; say nothing
        const was = w.value;
        if (String(was) === String(value)) continue;
        commitWidget(node, w, value, onChange);
        moved.push(`${name}: ${was} → ${value}`);
    }
    return moved;
}


/* -- panel ---------------------------------------------------------------- */

/* D1c: the row-3 tally. Takes the ELEMENT as an argument rather than closing
 * over it, because it is declared inside the factory -- a module-level closure
 * over `tally` reads as defined and resolves to nothing at call time, which is
 * a ReferenceError at mount rather than a typo you can see. */
function syncTally(node2) {
    // GROUPS is [groupName, [widgetName, ...]], so the dial count is the sum of
    // the inner arrays. Counted from the TABLE rather than from the DOM, so the
    // number is right when every group is collapsed and does not depend on a
    // render having happened.
    let dials = 0;
    for (const [, names] of GROUPS) {
        if (Array.isArray(names)) dials += names.length;
    }
    node2.textContent = `${dials} dials · ${GROUPS.length} groups`;
}

// syncTally() is called from inside the factory, after `tally` exists. Calling
// it here at module scope threw ReferenceError: tally is not defined -- the
// element belongs to ONE panel instance, not to the module.

export function createRunPanel(node, { onChange, suppressed, hopCount, onBack } = {}) {
    /* D1c: this is a PAGE, not a disclosure.
     *
     * It is a <details> because it was originally one -- a collapsible block at
     * the bottom of every tab, which is where "the tab opens from the bottom,
     * covering the UI" came from. Now that RUN has its own surface it is always
     * open, its <summary> is row 3 of the chrome rather than a triangle, and it
     * carries the dial count and a way back. `open` is forced below and the
     * triangle is removed in CSS.
     *
     * The elements are created exactly as before -- only the summary's contents
     * grew -- so nothing downstream (the digest, applyVisibility, the hide list)
     * has to know this is a page now. */
    const root = el("details", "h3e-section h3e-run");
    const sum = el("summary", "h3e-run-sum");
    sum.appendChild(el("span", "h3e-title", "RUN"));
    // The dial/group count, so the page says what it holds. D1c.
    const tally = el("span", "h3e-count h3e-run-tally");
    // The digest lives HERE, not in the summary. A3 hoisted it to the node's
    // permanent status bar, because the state it carries (resolution, aspect,
    // duration, hops, steps, sampler, cache) is the thing you check before
    // every queue, and burying it under a <details> meant you only saw it on
    // the one tab where the panel happened to be open. This panel still owns
    // the text -- it is the only thing that knows which widget holds what --
    // and the caller decides where the element is mounted.
    const digest = el("span", "h3e-count h3e-run-digest");
    // D1c: the tally sits beside the title in row 3, so the RUN page says what
    // it holds before you scroll it. Counted from GROUPS rather than from the
    // DOM, because the count must be right even when every group is collapsed.
    sum.appendChild(tally);
    syncTally(tally);
    root.appendChild(sum);

    const body = el("div", "h3e-run-body");
    root.appendChild(body);

    /* D1c: the way back. Created here because it has to live INSIDE the summary
     * -- row 3 of the page -- and the entry hides it whenever RUN is not the
     * surface. It calls back through `onBack` because run_panel does not know
     * about the surface swap; that is the entry's business. */
    const back = el("button", "h3e-btn h3e-run-back", "← back to work");
    back.type = "button";
    back.title = "Back to the tabs";
    back.hidden = true;
    back.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        onBack?.();
    });
    sum.appendChild(back);

    // resolution label -> aspect label -> [w, h], from /vocab. Fetched once per
    // browser session and shared with every other consumer of the vocabulary.
    let CANVAS = null;
    // mode -> widget -> value, from /vocab. Null until it arrives; the
    // speed_mode dial then stamps nothing and behaves as a plain combo, which
    // is the same thing that happens on an older Python side.
    let SPEED = null;
    vocab().then((v) => {
        CANVAS = v?.canvas || null;
        SPEED = v?.speed_modes || null;
        paintDigest();
    }).catch((err) => {
        // The digest falls back to the two labels; nothing else needs this.
        console.warn("[HandTieClips] canvas table unavailable:", err);
    });

    /** Widgets this panel actually drew, so the caller hides exactly those. */
    const owned = [];
    const readers = [];
    let builtFor = null;   // the suppression set the current DOM was built for

    function suppressedSet() {
        try {
            return new Set(suppressed?.() || []);
        } catch (err) {
            console.error("[HandTieClips] run panel suppression check failed:", err);
            return new Set();
        }
    }

    // What the last speed_mode pick changed. Lives outside build() only so the
    // text survives the repaint that stamping triggers.
    let speedNote = null;
    let speedSaid = "";

    function build() {
        body.textContent = "";
        owned.length = 0;
        readers.length = 0;
        // Widgets found by name but whose type fieldFor could not draw. A
        // frontend that renames widget types would empty this panel silently,
        // and "silent" is exactly what made the last mount bug hard to find.
        const rejected = [];

        const skip = suppressedSet();
        builtFor = [...skip].sort().join(",");

        for (const [title, names] of GROUPS) {
            const master = GROUP_MASTER[title];
            // When the master is off the whole group is one dimmed line. Its
            // dials are still BUILT -- only the details is closed -- because
            // an undrawn dial reappears as a raw widget below the panel, and a
            // raw `upsample_denoise` is worse than a collapsed group the user
            // opened once to find it.
            const isOff = master
                ? OFF_VALUES.has(String(widgetByName(node, master)?.value ?? "").toLowerCase())
                : false;

            const group = el("details", "h3e-run-group");
            group.open = !isOff;
            const det = el("summary", "h3e-run-label");
            // The state lives IN the summary, so a collapsed group still says
            // what it is and that it is off -- a bare "REFINE" with a closed
            // triangle reads like an empty section, not a disabled feature.
            const state = el("span", "h3e-run-gstate");
            det.appendChild(el("span", null, title));
            det.appendChild(state);
            group.appendChild(det);
            if (isOff) group.classList.add("h3e-run-off");

            const grid = el("div", "h3e-grid");
            let n = 0;
            for (const name of names) {
                if (skip.has(name)) continue;
                const w = widgetByName(node, name);
                if (!w) continue;
                const f = fieldFor(node, w, () => { paintDigest(); onChange?.(); });
                if (!f) {                          // stays a native dial below
                    rejected.push(`${name}:${widgetType(w) || "?"}`);
                    continue;
                }
                const l = el("label",
                    WIDE.has(name) ? "h3e-field h3e-run-wide" : "h3e-field");
                l.appendChild(el("span", null, labelFor(w)));
                l.appendChild(f.input);
                // The tooltip is the widget's own, straight from INPUT_TYPES.
                // Set on the label, which wraps both the caption and the
                // control, so the hover target is the whole field rather than
                // the six pixels of text. `title` on the input as well because
                // a DOM <select>/<input> does not always inherit the ancestor's
                // tooltip while it has focus.
                const tip = tooltipFor(node, w);
                if (tip) {
                    l.title = tip;
                    f.input.title = tip;
                }
                if (name === "speed_mode") {
                    // A second listener on the same select: comboField's own
                    // fires first and commits the mode, so by the time this
                    // runs the widget already holds the new value.
                    f.input.addEventListener("change", () => {
                        const moved = stampSpeedMode(node, SPEED, f.input.value,
                                                     () => { paintDigest(); onChange?.(); });
                        speedSaid = moved == null
                            ? ""
                            : (moved.length
                                ? `speed_mode = ${f.input.value} set ${moved.join(", ")}.`
                                : `speed_mode = ${f.input.value} — nothing to change.`);
                        if (speedNote) {
                            speedNote.textContent = speedSaid;
                            speedNote.style.display = speedSaid ? "" : "none";
                        }
                        // Repaint every field, not just the ones it moved: the
                        // stamp writes widgets this panel also draws, and a
                        // dial showing a stale value is the exact failure the
                        // write-don't-shadow rule exists to prevent.
                        render();
                    });
                }
                grid.appendChild(l);
                owned.push(name);
                readers.push(f.read);
                n += 1;
            }
            if (!n) continue;                      // nothing found: draw nothing
            group.appendChild(grid);
            body.appendChild(group);
            // Repaint this group's off-state whenever anything is re-read, so
            // flipping hop_refine to `full` un-collapses the dials it was
            // hiding without waiting for a rebuild.
            if (master) {
                const mark = () => {
                    const off = OFF_VALUES.has(
                        String(widgetByName(node, master)?.value ?? "").toLowerCase());
                    group.classList.toggle("h3e-run-off", off);
                    state.textContent = off ? " · off" : "";
                    state.title = off
                        ? `${master} is off; open to adjust it anyway`
                        : "";
                    // Opening a group by hand is a deliberate request to see
                    // the dials, so a user who opened an off group keeps it
                    // open until they collapse it themselves. Only the OFF
                    // transition auto-closes, and only going off.
                    if (off) group.open = false;
                };
                readers.push(mark);
                state.textContent = "";
            }
        }

        speedNote = el("div", "h3e-note h3e-note-hint");
        speedNote.style.display = speedSaid ? "" : "none";
        speedNote.textContent = speedSaid;
        body.appendChild(speedNote);

        const why = [...skip]
            .filter((name) => SUPPRESSED_WHY[name])
            .map((name) => `${name} — ${SUPPRESSED_WHY[name]}`);
        if (why.length) {
            body.appendChild(el("div", "h3e-note h3e-note-hint",
                `Not shown here: ${why.join("; ")}.`));
        }

        if (!owned.length) {
            // Two very different failures, and the message has to say which:
            // nothing found by name means a stale Python side; found but not
            // drawable means this frontend reports widget types fieldFor does
            // not know, which is a bug here rather than a version mismatch.
            if (rejected.length) {
                console.warn("[HandTieClips] run panel cannot draw these widget "
                    + "types:", rejected.join(", "));
            }
            body.appendChild(el("div", "h3e-note h3e-note-error",
                rejected.length
                    ? `No drawable run widgets: this frontend reports types the `
                      + `panel does not handle (${rejected.slice(0, 4).join(", ")}`
                      + `${rejected.length > 4 ? ", …" : ""}). Native dials left `
                      + `visible below; see the console.`
                    : "No run widgets found — the Python side is probably a "
                      + "different version. The native dials are left visible below."));
        } else if (rejected.length) {
            console.warn("[HandTieClips] run panel left these as native dials:",
                rejected.join(", "));
        }
        // An empty build must not stick: builtFor is what stops sync() from
        // rebuilding, so caching a failure would make it permanent for the
        // life of the node.
        if (!owned.length) builtFor = null;
    }

    /* The one-line summary, readable with the panel shut. Deliberately no seed:
     * `control_after_generate` rewrites it after every queue without telling
     * us, so a seed shown here would be wrong more often than right. */
    function paintDigest() {
        const v = (n) => widgetByName(node, n)?.value;
        const bits = [];
        const res = v("resolution");
        const asp = String(v("aspect") || "");
        if (res) {
            // The actual pixels, resolved in Python and published on /vocab.
            // Deliberately not recomputed here: it is a function of an area
            // budget, a ratio, a 32 px grid and an area cap, and a second
            // implementation in JavaScript is a second thing to be wrong --
            // with this one wrong in the direction of quietly disagreeing with
            // what rendered. Falls back to the labels until the fetch lands.
            const wh = CANVAS?.[res]?.[asp];
            bits.push(wh ? `${wh[0]}x${wh[1]}` : `${res} ${asp.split(" ")[0]}`.trim());
        }
        const dur = v("duration");
        // The hop count, not `chains`: with a shot plan loaded the two disagree
        // and the plan is the one run() obeys.
        const ch = hopCount?.() || v("chains");
        if (dur && ch) bits.push(`${String(dur).replace(/\s+/g, "")} ×${ch}`);
        const steps = v("steps");
        if (steps) bits.push(`${steps} steps ${v("sampler_name") || ""}`.trim());
        const cache = v("cache_hops");
        if (cache) bits.push(`cache ${cache}`);
        digest.textContent = bits.join(" · ");
    }

    /** Re-read every field from its widget. */
    function render() {
        for (const read of readers) {
            try {
                read();
            } catch (err) {
                console.error("[HandTieClips] run field refresh failed:", err);
            }
        }
        paintDigest();
    }

    /**
     * Rebuild only when the suppression set actually changed.
     *
     * Called from the same place that recomputes widget visibility, and it has
     * to run BEFORE that: `ownedNames()` is what decides which native dials get
     * hidden, so a stale build would hide a dial the panel no longer draws.
     */
    function sync() {
        const key = [...suppressedSet()].sort().join(",");
        if (key !== builtFor) build();
        render();
    }

    build();
    render();

    // Always open, and the summary cannot close it. Both were right when RUN
    // was the last child of the scroller, where collapsing it hid the one
    // section you touch on every queue. A4 made RUN a SURFACE: the entry
    // module shows and hides the whole thing and opens it itself, so forcing
    // it open here fought the surface swap -- entering RUN set `open = true`,
    // then a toggle event snapped it back, and leaving RUN left an open <details>
    // in the flow that would show through the tab strip.
    //
    // So the state is the caller's. `root.open` is only forced open here for
    // the case nobody else is driving: the summary click is suppressed so a
    // stray click cannot fold the surface away while it is on screen.
    root.open = true;
    sum.addEventListener("click", (e) => e.preventDefault());
    // Values change behind the panel's back -- `control_after_generate` bumps
    // the seed on every queue, and undo rewrites widgets wholesale -- so what
    // used to be re-read on open is now re-read whenever the node is drawn
    // through sync(). node.properties[OPEN_PROP] is left alone: old workflows
    // carry it and it costs nothing.

    return {
root,
            render,
            sync,
            /* D1c: the row-3 back control. The entry owns the surface swap, so
             * it hides this unless RUN is the surface and wires onBack. */
            back,
          /** The status text, for the caller to mount in the status bar (A3).
           *  Mounted once and left there, so the node's top row reads the same
           *  on every tab and on RUN itself. */
          digestEl: digest,
          /** Names the panel drew; the caller hides exactly these. */
          ownedNames: () => owned.slice(),
      };
}
