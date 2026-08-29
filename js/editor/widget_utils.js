/**
 * Foundations for the H3 Ref Chain node editor.
 *
 * Two things here are load-bearing and neither is obvious, so both are
 * documented at the point of use rather than in a README nobody opens:
 *
 *   hideWidget()  - the four-flag recipe. Classic LiteGraph only needed
 *                   computeSize = [0, -4]; Vue Nodes 2.0 filters on
 *                   options.hidden | hideInPanel | canvasOnly, and without
 *                   those flags every hidden dial reappears as a raw form.
 *                   Invisible in packs with three widgets, unmissable at 21.
 *
 *   installHeightGuard() - the _h fixpoint. LiteGraph's _arrangeWidgets runs
 *                   every frame; report a height derived from node.size[1] and
 *                   the node grows forever.
 *
 * Both are ported from PromptMasterLD/js/claude_prompt.js, which paid for them.
 */

import { app } from "../../../scripts/app.js";

const VOCAB_URL = "/h3_ref_chain/vocab";

/* -- vocabulary ---------------------------------------------------------- */

let _vocabPromise = null;
const _vocabListeners = new Set();

/**
 * The directive vocabulary, fetched once per browser session.
 *
 * Deliberately NOT bundled into this file: `directives.py` is the single source
 * of truth so that improving one sentence improves every plan already written.
 * A copy here would go stale on the first edit.
 */
/** Drop the cached vocabulary so the next vocab() call refetches. */
export function refreshVocab() {
    _vocabPromise = null;
    for (const fn of _vocabListeners) {
        try { fn(); } catch (err) { console.error("[HandTieClips] vocab listener failed:", err); }
    }
}

/** Register a mounted editor to be re-rendered when the vocabulary reloads. */
export function onVocabRefresh(fn) {
    _vocabListeners.add(fn);
    return () => _vocabListeners.delete(fn);
}

export function vocab() {
    if (!_vocabPromise) {
        _vocabPromise = fetch(VOCAB_URL)
            .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
            .catch((err) => {
                console.error("[HandTieClips] vocabulary unavailable:", err);
                // Degrade to free-text directives rather than an empty editor.
                // `_failed` is read by the editors, which show a retry strip --
                // it used to be set here and never looked at again, so a failed
                // fetch just produced a mysteriously featureless panel.
                return { axes: [], vocab: {}, defaults: {}, retention: {},
                         durations: {}, overlaps: {}, max_ref_images: 9, fps: 24,
                         _failed: true, _error: String(err && err.message || err) };
            });
    }
    return _vocabPromise;
}

/* -- widget visibility --------------------------------------------------- */

/**
 * Hide a native widget without losing its value.
 *
 * The widget still serializes -- that is the entire point. The DOM editor is a
 * rendering layer over `shot_plan` / `ref_plan`, and those widgets remain the
 * only source of truth, so the workflow JSON is byte-identical in shape to one
 * authored by hand.
 */
/* `widget.inputEl` is a deprecated alias on frontend 1.49.6: merely reading it
 * logs a deprecation warning to the console. Prefer `element`, and only reach
 * for the alias when it is absent, i.e. on an older frontend. */
function widgetElements(w) {
    if (w.element) return [w.element];
    return w.inputEl ? [w.inputEl] : [];
}

/**
 * The widget's type as the FRONTEND defined it, seeing past our own hiding.
 *
 * `hideWidget` overwrites `w.type` with "hidden" and stashes the real one in
 * `_h3Saved`. Anything that renders a widget therefore has to ask for the type
 * this way, or it reads back the hiding rather than the widget -- which is
 * exactly how the run panel emptied itself: it hid the dials it owned on the
 * first build, then found nothing but "hidden" on the rebuild.
 */
export function widgetType(w) {
    const t = (w && w._h3Hidden) ? w._h3Saved?.type : w?.type;
    return String(t ?? "").toLowerCase();
}

/** Ditto for options: `hideWidget` replaces the object with a flagged copy. */
export function widgetOptions(w) {
    const o = (w && w._h3Hidden) ? w._h3Saved?.options : w?.options;
    return o || w?.options || {};
}

export function hideWidget(w) {
    if (!w || w._h3Hidden) return;
    w._h3Hidden = true;
    w._h3Saved = {
        type: w.type,
        computeSize: w.computeSize,
        computeLayoutSize: w.computeLayoutSize,
        draw: w.draw,
        options: w.options,
    };

    w.type = "hidden";
    w.hidden = true;
    w.options = Object.assign({}, w.options || {}, {
        hidden: true,
        hideInPanel: true,
        // canvasOnly also drops it from the Vue widget list (shouldRenderAsVue).
        canvasOnly: true,
        // Never flip serialization off: the value IS the saved document.
        serialize: w.options?.serialize !== false,
    });
    w.computeSize = () => [0, -4];
    if (typeof w.computeLayoutSize === "function") {
        w.computeLayoutSize = () => ({ minHeight: 0, maxHeight: 0, minWidth: 0 });
    }
    w.draw = () => {};

    // A multiline STRING widget is a real DOM textarea. Collapsing only the
    // LiteGraph geometry leaves it floating at its old coordinates, on top of
    // the editor.
    for (const el of widgetElements(w)) {
        if (!el?.style) continue;
        el.style.setProperty("display", "none", "important");
        el.setAttribute?.("aria-hidden", "true");
    }
}

/** Undo hideWidget. Used when the Simple/Shots toggle flips. */
export function showWidget(w) {
    if (!w?._h3Hidden) return;
    const saved = w._h3Saved || {};
    w.type = saved.type;
    w.hidden = false;
    w.options = saved.options || {};
    if (saved.computeSize) w.computeSize = saved.computeSize;
    else delete w.computeSize;
    if (saved.computeLayoutSize) w.computeLayoutSize = saved.computeLayoutSize;
    if (saved.draw) w.draw = saved.draw;
    else delete w.draw;
    for (const el of widgetElements(w)) {
        if (!el?.style) continue;
        el.style.removeProperty("display");
        el.removeAttribute?.("aria-hidden");
    }
    w._h3Hidden = false;
    delete w._h3Saved;
}

export function widgetByName(node, name) {
    return node.widgets?.find((w) => w.name === name) || null;
}

/** Hide every widget in `names`, show the rest of `names`' complement set. */
export function setWidgetVisibility(node, hiddenNames) {
    const hide = new Set(hiddenNames);
    for (const w of node.widgets || []) {
        if (hide.has(w.name)) hideWidget(w);
        else showWidget(w);
    }
}

/* -- height guard -------------------------------------------------------- */

/**
 * Stop the node growing a little every frame.
 *
 * LiteGraph's _arrangeWidgets does, once per frame:
 *
 *     l = widgetsTop; for (w of widgets) l += w.computeSize()[1] + 4;
 *     if (l > node.size[1]) node.setSize([node.size[0], l]);
 *
 * so a panel that reports `node.size[1]` makes l = top + size[1] + 4 > size[1]
 * on EVERY frame. Observed at ~130px of growth per frame -- the workflow
 * scrolls away from you while you watch it.
 *
 * Reporting `size[1] - chrome() - SLACK` makes l <= size[1] on every frame, so
 * the arrange pass never asks to grow and the loop settles at the node's own
 * height. SLACK is what buys the inequality: panelTop is chrome minus the
 * node's bottom padding, so a panel of exactly `size - chrome` still asks for
 * up to 4px more than the node has -- every frame, forever.
 *
 * This replaced an independent stored `_h`, updated through an onResize hook.
 * The hook fired (57 times in one measured drag) but its `Array.isArray(size)`
 * gate rejected every call -- this frontend's `node.size` is not a plain Array
 * -- so `_h` kept the height the node had at install time and the panel stayed
 * 742px inside a 1911px node. Reading the node directly has no such gate to get
 * wrong.
 *
 * The second floor is the resize drag, which clamps up to
 * LGraphNode.computeSize()[1]. Rather than re-derive that formula (and have it
 * rot the next time the frontend changes), measure it: with `_measuring` set,
 * panelHeight() reports 0, so computeSize() returns pure chrome. The flag also
 * breaks the recursion, since computeSize() calls back into us.
 */
export function installHeightGuard(node, domWidget, { minHeight = 420, minWidth = 520 } = {}) {
    if (node._h3HeightGuard) return node._h3HeightGuard;

    let measuring = false;
    let chromeVal = null;
    let chromeKey = "";

    const chrome = () => {
        // The hidden count is part of the key because hiding a widget changes
        // what computeSize() measures while leaving widgets.length alone --
        // so without it the cached chrome height goes stale the moment
        // applyVisibility runs, and every panel height after that is wrong.
        const hidden = (node.widgets || []).reduce((n, w) => n + (w._h3Hidden ? 1 : 0), 0);
        const key = `${node.inputs?.length}|${node.outputs?.length}|${node.widgets?.length}|${hidden}`;
        if (key === chromeKey && chromeVal != null) return chromeVal;
        measuring = true;
        try {
            chromeVal = +node.computeSize()[1] || 0;
            chromeKey = key;
        } catch (_) {
            chromeVal = 0;
        } finally {
            measuring = false;
        }
        return chromeVal;
    };

    // The margin that keeps the arrange pass from asking for one more pixel
    // than the node has. See the note above. Costs an invisible strip at the
    // bottom of the panel; 4 would do, 8 leaves room for the frontend to
    // change its mind about padding.
    const SLACK = 8;
    const nodeHeight = () => Math.max(minHeight, +(node.size?.[1]) || minHeight);
    const nodeWidth = () => Math.max(minWidth, +(node.size?.[0]) || minWidth);

    const guard = {
        get minHeight() { return minHeight; },
        /** The node's own height is the authority; nothing mirrors it. */
        get _h() { return nodeHeight(); },
        panelHeight() {
            if (measuring) return 0;
            return Math.max(minHeight, nodeHeight() - chrome() - SLACK);
        },
        /** Grow the node if it is too short to hold a minimum panel. Never
         *  shrinks it -- the node's height is the user's to choose. */
        sync() {
            const need = chrome() + minHeight + SLACK;
            if (nodeHeight() < need - 1) node.setSize([nodeWidth(), need]);
            node.graph?.setDirtyCanvas?.(true, true);
        },
        /** Ask for a taller panel, e.g. after adding a card. */
        grow(px) {
            node.setSize([nodeWidth(), Math.max(minHeight, nodeHeight() + px)]);
            node.graph?.setDirtyCanvas?.(true, true);
        },
    };
    node._h3HeightGuard = guard;

    if (domWidget) {
        // The two callers want different answers and, helpfully, pass
        // different arguments -- which is the only way to tell them apart:
        //
        //   LGraphNode.computeSize()  calls computeSize(size[0]), WITH a width.
        //     Its result is used purely as a FLOOR (the resize-drag clamp,
        //     expandToFitContent). Reporting the live height there pins the
        //     floor to the current height, so the node can only ever grow --
        //     drag it smaller and nothing happens. Report the MINIMUM.
        //
        //   _arrangeWidgets()         calls computeSize(), no argument.
        //     The real layout pass. Report the live height.
        domWidget.computeSize = (width) => [
            Math.max(minWidth, +(node.size?.[0]) || minWidth),
            measuring ? 0 : (width === undefined ? guard.panelHeight() : minHeight),
        ];
        if (typeof domWidget.computeLayoutSize === "function") {
            // TRACK, DO NOT FREEZE. Reporting a constant `minWidth` here is
            // what collapses the node: the layout pass re-reads it on every
            // recompute -- selecting the node is enough -- and faithfully
            // re-declares the node at its stated minimum, so a wide node
            // becomes a 560px node on the first click and stays there.
            // Safe against feedback: max() against a constant is a fixpoint,
            // not an accumulator like the height chain, and width never feeds
            // height.
            domWidget.computeLayoutSize = () => ({
                minHeight: measuring ? 0 : guard.panelHeight(),
                maxHeight: measuring ? 0 : guard.panelHeight(),
                minWidth: Math.max(minWidth, +(node.size?.[0]) || minWidth),
            });
        }
        // The other half of the collapse. ComfyUI's DOM-widget position
        // updater computes `size = [(widget.width ?? node.width) - margin*2, ...]`.
        // Once anything writes a stale number onto widget.width, node.width is
        // never consulted again -- which is why fixing only the layout size is
        // not enough. A live getter makes the stale value unrepresentable:
        // width always IS the node's width, and writes are dropped.
        try {
            Object.defineProperty(domWidget, "width", {
                get: () => Math.max(minWidth, +(node.size?.[0]) || minWidth),
                set: () => {},
                configurable: true,
            });
        } catch (err) {
            console.warn("[HandTieClips] width getter failed:", err);
        }
    }

    // With no widget declaring a width, LiteGraph falls back to
    // NODE_WIDTH * 1.5 = 210, and every resize command is then free to crush
    // the panel. Floor it.
    const prevComputeSize = node.computeSize;
    if (typeof prevComputeSize === "function") {
        node.computeSize = function (...args) {
            const r = prevComputeSize.apply(this, args) || [0, 0];
            if (r[0] < minWidth) r[0] = minWidth;
            return r;
        };
    }

    // No onResize hook any more. There is nothing to mirror: panelHeight()
    // reads node.size[1] at the moment it is asked, so a resize is reflected
    // by the very next arrange pass with nothing to keep in step.

    return guard;
}

/* -- tiny DOM helpers ---------------------------------------------------- */

export function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

export function button(label, title, onClick, className = "h3e-btn") {
    const b = el("button", className, label);
    b.type = "button";
    if (title) b.title = title;
    b.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        onClick(e);
    });
    return b;
}

export function select(options, value, onChange, { blankLabel = "—", titles = {} } = {}) {
    const s = el("select", "h3e-select");
    const blank = el("option", null, blankLabel);
    blank.value = "";
    s.appendChild(blank);
    for (const opt of options) {
        const o = el("option", null, opt.replace(/_/g, " "));
        o.value = opt;
        if (titles[opt]) o.title = titles[opt];
        s.appendChild(o);
    }
    s.value = value || "";
    s.addEventListener("change", () => onChange(s.value));
    return s;
}

/**
 * Keep canvas gestures out of the panel.
 *
 * Without this a click inside a textarea also starts a node drag, and a scroll
 * inside the shot list zooms the graph instead.
 */
export function isolateEvents(root) {
    for (const name of ["pointerdown", "pointerup", "mousedown", "mouseup",
                        "click", "dblclick", "contextmenu"]) {
        root.addEventListener(name, (e) => e.stopPropagation());
    }
    root.addEventListener("keydown", (e) => e.stopPropagation());

    // Wheel is NOT simply swallowed. Stopping it outright meant the graph could
    // not be zoomed while the pointer was anywhere over the panel -- and the
    // panel is most of the node. Scroll the nearest scrollable ancestor if
    // there is somewhere left to scroll; otherwise hand the gesture to the
    // canvas so zoom keeps working. Same approach as KJNodes' wheel passthrough.
    root.addEventListener("wheel", (e) => {
        let n = e.target;
        while (n && n !== root.parentNode) {
            if (n.scrollHeight > n.clientHeight + 1) {
                const style = getComputedStyle(n).overflowY;
                if (style === "auto" || style === "scroll") {
                    const atTop = n.scrollTop <= 0;
                    const atEnd = n.scrollTop + n.clientHeight >= n.scrollHeight - 1;
                    if (!((e.deltaY < 0 && atTop) || (e.deltaY > 0 && atEnd))) {
                        e.stopPropagation();
                        return;
                    }
                }
            }
            n = n.parentNode;
        }
        const canvasEl = document.querySelector("#graph-canvas")
            || app?.canvas?.canvas;
        if (!canvasEl) return;
        e.preventDefault();
        e.stopPropagation();
        canvasEl.dispatchEvent(new WheelEvent(e.type, e));
    }, { passive: false });
}
