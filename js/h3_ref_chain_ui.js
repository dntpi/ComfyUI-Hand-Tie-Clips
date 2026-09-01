import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    el, isolateEvents, installHeightGuard, setWidgetVisibility, widgetByName,
} from "./editor/widget_utils.js";
import { createPlanEditor } from "./editor/plan_editor.js";
import { createRefRail, parseRefPlan, refPlanToJson } from "./editor/ref_rail.js";
import { createRunPanel } from "./editor/run_panel.js";
import { createMediaStrip, MEDIA_WIDGETS } from "./editor/media_strip.js";

const VERSION = "v1.5.0";
/* Both ids. The pack registers the pre-rename id as a deprecated subclass so
 * workflows saved before 2026-08-29 still load. If this check knew only the
 * new id those nodes would come up with NO editor at all, which looks exactly
 * like the rename having broken the pack. */
const NODE_TYPES = new Set(["HandTieClips", "H3RefChain"]);
const NODE_WIDTH = 560;
const EDITOR_MIN_H = 460;
const STYLE_ID = "h3rc-style";

/* Widgets the editor owns. In Simple mode the plan is hidden instead.
 *
 * The run panel's share is NOT listed here: it reports the widgets it actually
 * managed to draw, and only those are hidden. A dial the panel could not render
 * -- a type it does not know, or one renamed on the Python side -- therefore
 * stays visible as a native widget instead of vanishing from the node. */
// MEDIA_WIDGETS are the filename STRINGs that replaced the start_image /
// reference_video / voice sockets, plus soundtrack_file. They are always
// hidden: the media strip owns them in both modes, and shown raw they are
// text boxes inviting you to type a path that would not resolve.
const SHOTS_HIDDEN = ["prompt", "chains", "hop_script", "shot_plan", "ref_plan"]
    .concat(MEDIA_WIDGETS);
const SIMPLE_HIDDEN = ["shot_plan", "ref_plan"].concat(MEDIA_WIDGETS);

const _cssUrl = new URL("./h3_ref_chain.css", import.meta.url).href;

/* External stylesheet, not a template literal. The old inline sheet hardcoded
 * every colour, so a light ComfyUI theme rendered the panel as dark islands on
 * a light node; the CSS file derives everything from the host theme's own
 * custom properties. */
function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement("link");
    link.id = STYLE_ID;
    link.rel = "stylesheet";
    link.href = _cssUrl;
    document.head.appendChild(link);
}

/* -- editor -------------------------------------------------------------- */

function mountEditor(node) {
    if (node._h3Editor) return;
    if (typeof node.addDOMWidget !== "function") {
        console.warn("[HandTieClips] no addDOMWidget on this frontend; editor not mounted");
        return;
    }
    const planWidget = widgetByName(node, "shot_plan");
    const refWidget = widgetByName(node, "ref_plan");
    if (!planWidget || !refWidget) {
        // Stale node definition: the Python side was not reloaded. Silent here
        // is what made this hard to diagnose the first time.
        console.warn(
            "[HandTieClips] shot_plan/ref_plan widget missing -- restart ComfyUI. "
            + "Widgets present:", (node.widgets || []).map((w) => w.name),
        );
        return;
    }
    ensureStyles();

    const root = el("div", "h3e-root");
    isolateEvents(root);

    // A ref_plan that does not parse must NOT be silently replaced by an empty
    // one: the rail would then write "" over it on the next edit, destroying a
    // paste the author was in the middle of repairing. Keep the raw text in the
    // widget, flag it, and let the rail's JSON section show and fix it.
    let refBad = false;
    const readRefPlan = () => {
        const parsed = parseRefPlan(refWidget.value);
        refBad = parsed === null;
        return parsed || { refs: [], subjects: {} };
    };
    let refPlan = readRefPlan();

    // How many hops this run will actually do. The shot plan is authoritative
    // when present -- run() ignores `chains` the moment one is loaded -- so both
    // the rail's schedule chips and the run panel's summary read it from here
    // rather than from the widget that no longer decides.
    const hopCount = () => {
        const plan = widgetByName(node, "shot_plan")?.value;
        try {
            const parsed = JSON.parse((plan || "").trim() || "null");
            const arr = Array.isArray(parsed) ? parsed : parsed?.shots;
            if (Array.isArray(arr) && arr.length) return arr.length;
        } catch (_) { /* unparseable plan: fall back to chains */ }
        return Number(widgetByName(node, "chains")?.value) || 1;
    };

    const rail = createRefRail(node, {
        getPlan: () => refPlan,
        setPlan: (p) => { refPlan = p; refBad = false; refWidget.value = refPlanToJson(p); },
        getRaw: () => refWidget.value,
        isBad: () => refBad,
        onChange: () => node.graph?.setDirtyCanvas?.(true, true),
        hopCount,
    });

    const editor = createPlanEditor(node, {
        onChange: () => {
            applyVisibility();
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });

    const runPanel = createRunPanel(node, {
        onChange: () => node.graph?.setDirtyCanvas?.(true, true),
        hopCount,
        // In Shots mode these two are decided by the plan, not by the user:
        // run() ignores `chains` and forces `hop_script=next`. They are already
        // in SHOTS_HIDDEN, so dropping them here leaves them hidden rather than
        // offering a control that does nothing.
        suppressed: () => (editor.mode() === "shots" ? ["chains", "hop_script"] : []),
    });

    const mediaStrip = createMediaStrip(node, {
        onChange: () => node.graph?.setDirtyCanvas?.(true, true),
    });

    // Authoring sections scroll; RUN does not. RUN is the one section touched
    // on every queue, and it used to be the last child of the scroller -- so
    // it sat below however many shot cards the script had and you had to
    // scroll the panel just to reach the summary bar. It is pinned to the
    // bottom of the panel now, collapsed to its ~28px summary until opened.
    const scroll = el("div", "h3e-scroll");
    scroll.appendChild(rail.root);
    scroll.appendChild(mediaStrip.root);
    scroll.appendChild(editor.root);
    root.appendChild(scroll);
    root.appendChild(runPanel.root);

    const domWidget = node.addDOMWidget("h3_editor", "h3_editor", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => EDITOR_MIN_H,
    });
    domWidget.serialize = false;

    installHeightGuard(node, domWidget, { minHeight: EDITOR_MIN_H, minWidth: NODE_WIDTH });

    function applyVisibility() {
        // The rail always owns ref_plan; the plan widget's visibility follows
        // the mode, so Simple mode never leaves an inert second text area up.
        // sync() first: ownedNames() decides what gets hidden, so rebuilding
        // after the hide would hide a dial the panel had stopped drawing.
        runPanel.sync();
        const base = editor.mode() === "shots" ? SHOTS_HIDDEN : SIMPLE_HIDDEN;
        setWidgetVisibility(node, base.concat(runPanel.ownedNames()));
    }

    node._h3Editor = {
        refresh() {
            refPlan = readRefPlan();
            editor.reload();
            rail.render();
            mediaStrip.render();
            applyVisibility();   // syncs the run panel on the way through
        },
        rail,
        editor,
        runPanel,
    };

    applyVisibility();
    rail.render();
    mediaStrip.render();
    editor.render();

    // `control_after_generate` bumps the seed widget as the prompt is queued,
    // and undo rewrites widgets wholesale -- both behind this panel's back.
    // RUN used to re-read on open, which is no longer a moment that exists.
    // `promptQueued` fires client-side after the bump has been applied.
    const onQueued = () => {
        try {
            runPanel.sync();
        } catch (err) {
            console.warn("[HandTieClips] run panel resync failed:", err);
        }
    };
    api.addEventListener("promptQueued", onQueued);
    // Removing the node must drop the listener, or every add/remove cycle
    // leaves another closure holding this node alive.
    const prevRemoved = node.onRemoved;
    node.onRemoved = function (...args) {
        api.removeEventListener("promptQueued", onQueued);
        // Same reason, one layer down: each trim bar holds a requestAnimationFrame
        // loop and a media element with an open connection, and neither stops
        // just because the node left the canvas.
        try { mediaStrip.destroy?.(); } catch (err) {
            console.error("[HandTieClips] media strip teardown failed:", err);
        }
        return prevRemoved?.apply(this, args);
    };

    console.log(`[HandTieClips] ${VERSION} editor mounted on node ${node.id}`);
}

console.log(`[HandTieClips] editor ui ${VERSION} loaded`);

app.registerExtension({
    name: "HandTieClips.ui",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_TYPES.has(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            mountEditor(this);
            this.setSize([
                Math.max(this.size[0], NODE_WIDTH),
                Math.max(this.size[1], 640),
            ]);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            mountEditor(this);
            // Widget values arrive with the workflow, after the mount, so the
            // panel has to re-read them or a loaded graph shows an empty editor.
            this._h3Editor?.refresh();
            return r;
        };

        // Wiring or unwiring a ref_image_N input changes the ordinals and the
        // wired/unwired dots, so the rail has to be told.
        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const r = onConnectionsChange?.apply(this, arguments);
            this._h3Editor?.rail?.render();
            return r;
        };
    },
});
