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
 * Help text is read from `widget.options.tooltip`, i.e. the `tooltip` written in
 * INPUT_TYPES. Retyping those sentences here would be a second copy that goes
 * stale, for exactly the reason routes.py exists.
 */

import { el, widgetByName, widgetType, widgetOptions } from "./widget_utils.js";

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
    ["sampling", ["steps", "sampler_name", "scheduler", "seed",
                  "control_after_generate", "seed_per_shot",
                  "shift_video", "shift_audio"]],
    ["join & pin", ["hop_script", "pin_to_qwen", "ref_image_size",
                    "audio_pin_frames", "pin_renorm", "pin_noise",
                    // tone_anchor sits next to the mode it modifies: it is
                    // read only when tone_compensate is `anchor`, and split
                    // across groups nothing says so. Deliberately NOT
                    // suppressed in the other modes -- suppression only stops
                    // the panel drawing a dial, and an undrawn dial reappears
                    // as a raw native widget, which is worse than an inert one.
                    "tone_compensate", "tone_anchor"]],
    ["cache", ["cache_hops", "cache_budget_gb"]],
    // Added 2026-08-30. 0.4.0 shipped these five on the Python side and never
    // touched js/, so all five fell through to native dials -- the documented
    // fallback doing its job, not a break. Order is the order you reach for
    // them: prove the plan compiles, pick the fidelity, stop short, look.
    ["preview", ["dry_run", "quality", "render_through", "contact_sheet"]],
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

function labelFor(w) {
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
function fieldFor(node, w, onChange) {
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

/* -- panel ---------------------------------------------------------------- */

export function createRunPanel(node, { onChange, suppressed, hopCount } = {}) {
    const root = el("details", "h3e-section h3e-run");
    const sum = el("summary", "h3e-run-sum");
    sum.appendChild(el("span", "h3e-title", "RUN"));
    const digest = el("span", "h3e-count h3e-run-digest");
    sum.appendChild(digest);
    root.appendChild(sum);

    const body = el("div", "h3e-run-body");
    root.appendChild(body);

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
                const tip = widgetOptions(w).tooltip;
                if (tip) l.title = String(tip);
                grid.appendChild(l);
                owned.push(name);
                readers.push(f.read);
                n += 1;
            }
            if (!n) continue;                      // nothing found: draw nothing
            const group = el("div", "h3e-run-group");
            group.appendChild(el("div", "h3e-run-label", title));
            group.appendChild(grid);
            body.appendChild(group);
        }

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
        const asp = String(v("aspect") || "").split(" ")[0];
        if (res) bits.push(asp ? `${res} ${asp}` : String(res));
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

    // Always open. Collapsing made sense while RUN was the last child of the
    // scroller and cost a screenful; pinned to the bottom it is the one
    // section you touch on every queue, so a toggle only ever hid it.
    // `<details>` is kept for the summary bar's markup, forced open and with
    // its disclosure suppressed -- see the CSS.
    root.open = true;
    root.addEventListener("toggle", () => { root.open = true; });
    // A click on the summary would otherwise close it before that fires.
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
        /** Names the panel drew; the caller hides exactly these. */
        ownedNames: () => owned.slice(),
    };
}
