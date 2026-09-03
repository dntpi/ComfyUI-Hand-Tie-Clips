import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    createTabs, el, isolateEvents, installHeightGuard, setWidgetVisibility,
    widgetByName,
} from "./editor/widget_utils.js";
import { createPlanEditor } from "./editor/plan_editor.js";
import { createRefRail, parseRefPlan, refPlanToJson } from "./editor/ref_rail.js";
import { createRunPanel } from "./editor/run_panel.js";
import { createWriterBar } from "./editor/writer_bar.js";
import { createMediaStrip, MEDIA_WIDGETS } from "./editor/media_strip.js";

const VERSION = "v1.5.3";
/* Both ids. The pack registers the pre-rename id as a deprecated subclass so
 * workflows saved before 2026-08-29 still load. If this check knew only the
 * new id those nodes would come up with NO editor at all, which looks exactly
 * like the rename having broken the pack. */
const NODE_TYPES = new Set(["HandTieClips", "H3RefChain"]);
// The size a node OPENS at: 4:3, and large enough to read a two-shot script and
// the whole RUN panel without dragging anything first. 640x480 was the first
// attempt at "4:3 box" and got the ratio right and the scale wrong -- a default
// nobody can use without resizing is not a default.
//
// This is NOT the minimum. NODE_MIN_WIDTH is, and it stays small on purpose: a
// node that cannot be made narrow is worse on a laptop than a node that opens
// small is on a desktop. The two were the same constant until that was noticed,
// so raising the default would silently have raised the floor with it.
const NODE_WIDTH = 1200;
const NODE_HEIGHT = 900;
const NODE_MIN_WIDTH = 560;
// Mirrors MODE_PROP in editor/plan_editor.js -- the writer forces Shots mode
// after a plan lands, and the property is the only part of that it should touch.
const EDITOR_MODE_PROP = "h3_editor_mode";
// Which tab is showing. A node property, so it rides the saved workflow and
// ComfyUI's restored graph rather than resetting to Script on every reload.
const ACTIVE_TAB_PROP = "h3_active_tab";
// One pane plus the tab strip plus RUN's summary bar. Was 460, when this had to
// hold four stacked sections before the first scroll.
const EDITOR_MIN_H = 360;
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
        // syncBadges is declared further down; these closures only ever run on
        // user interaction, long after the mount has finished.
        onChange: () => {
            syncBadges();
            node.graph?.setDirtyCanvas?.(true, true);
        },
        hopCount,
    });

    const editor = createPlanEditor(node, {
        onChange: () => {
            applyVisibility();
            syncBadges();
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });

    const runPanel = createRunPanel(node, {
        onChange: () => {
            // render_from / render_through are drawn in RUN and also driven by
            // the ⏵ button on each shot card, so a change here has to re-mark
            // which cards the range now leaves out. Cheap: at most 24 cards,
            // and only ever on a user gesture.
            editor.render();
            node.graph?.setDirtyCanvas?.(true, true);
        },
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

    // Accept, not Write plan, lands the two strings in the widgets and then
    // goes through the same reload path a hand-paste does. Write plan only
    // holds a draft. Collapsed by default and inert until opened: this is
    // an accelerator for the manual recipe, not a step in it.
    const writer = createWriterBar(node, {
        hopCount,
        onWritten: (shotJson, refJson) => {
            if (shotJson) planWidget.value = shotJson;
            if (refJson) refWidget.value = refJson;
            // Force Shots mode by setting the property rather than calling
            // setMode(): setMode's "shots" branch is written for an EMPTY
            // editor and migrates the prompt widget into cards, which would
            // overwrite the plan that was just written. reload() below fills
            // `shots` from the widget, which is the whole job here.
            if (shotJson) {
                node.properties ??= {};
                node.properties[EDITOR_MODE_PROP] = "shots";
            }
            node._h3Editor?.refresh();
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });

    // One pane at a time, RUN pinned underneath.
    //
    // The four authoring sections used to be stacked in a single scroller, so
    // reaching the script meant scrolling past the writer, the rail and the
    // media strip -- and the taller the rail got, the further down the work
    // was. They are tabs now, which is also what lets the node be a squarish
    // box instead of a column: only one section is asking for height at a time.
    //
    // RUN stays outside the tab body and pinned to the bottom, which it already
    // was, for the same reason it was moved there -- it is the one section
    // touched on every queue.
    const tabs = createTabs([
        { id: "script", label: "Script", title: "The shot list", body: editor.root },
        { id: "refs", label: "Refs", title: "Reference pictures and @tags", body: rail.root },
        { id: "media", label: "Media", title: "Start image, reference clip, voice, soundtrack", body: mediaStrip.root },
        { id: "write", label: "Write", title: "Draft a plan with a local model", body: writer.root },
    ], {
        active: node.properties?.[ACTIVE_TAB_PROP],
        onShow: (id) => {
            node.properties ??= {};
            node.properties[ACTIVE_TAB_PROP] = id;
            // WRITE is a <details> and its contents are loaded on first open --
            // opening it here keeps that laziness (nothing is fetched until you
            // visit the tab) while making the pane usable once you have.
            if (id === "write") writer.root.open = true;
            // The panel does not follow the node's height on its own; see the
            // note on installHeightGuard. sync() grows the node if the pane now
            // showing needs more room than the last one did.
            node._h3HeightGuard?.sync();
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });
    root.appendChild(tabs.strip);
    root.appendChild(tabs.bodies);
    root.appendChild(runPanel.root);

    const domWidget = node.addDOMWidget("h3_editor", "h3_editor", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => EDITOR_MIN_H,
    });
    domWidget.serialize = false;

    installHeightGuard(node, domWidget, { minHeight: EDITOR_MIN_H, minWidth: NODE_MIN_WIDTH });

    function applyVisibility() {
        // The rail always owns ref_plan; the plan widget's visibility follows
        // the mode, so Simple mode never leaves an inert second text area up.
        // sync() first: ownedNames() decides what gets hidden, so rebuilding
        // after the hide would hide a dial the panel had stopped drawing.
        runPanel.sync();
        const base = editor.mode() === "shots" ? SHOTS_HIDDEN : SIMPLE_HIDDEN;
        setWidgetVisibility(node, base.concat(runPanel.ownedNames()));
    }

    // A hidden pane still has to report. Without these the shot count and the
    // reference count are only visible on the tab you are already looking at,
    // which is the one place you did not need to be told.
    const syncBadges = () => {
        try {
            tabs.badge("script", hopCount());
            tabs.badge("refs", (refPlan.refs || []).length || "");
        } catch (err) {
            // Decoration. A bad count must never take the refresh down with it.
            console.warn("[HandTieClips] tab badges:", err);
        }
    };

    node._h3Editor = {
        refresh() {
            refPlan = readRefPlan();
            // LiteGraph calls onNodeCreated BEFORE configure(), so the mount
            // above chose a tab from empty properties. The saved one only
            // exists by the time we get here.
            const saved = node.properties?.[ACTIVE_TAB_PROP];
            if (saved && saved !== tabs.active()) tabs.show(saved);
            editor.reload();
            rail.render();
            mediaStrip.render();
            // Properties arrive with the workflow, AFTER the mount, so a loaded
            // graph has to be told to put the writer's brief and held draft
            // back -- the mount ran before there was anything to restore.
            writer.restore();
            writer.syncHops();
            applyVisibility();   // syncs the run panel on the way through
            syncBadges();
        },
        rail,
        editor,
        runPanel,
        writer,
        tabs,
    };

    applyVisibility();
    rail.render();
    mediaStrip.render();
    editor.render();
    syncBadges();

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
            // Exact, not Math.max. LiteGraph sizes a fresh node from its widget
            // count, and this node has 48 -- so the computed height is already
            // far past 480 and a max() never brought anything down to the 4:3
            // box it was written to produce. The editor hides those widgets, so
            // the height they imply is not a floor worth respecting.
            //
            // Creation only. A node arriving from a saved workflow keeps
            // whatever size its author dragged it to; onConfigure does not do
            // this, and resizing somebody's graph on load would be worse than
            // the wrong default ever was.
            this.setSize([NODE_WIDTH, NODE_HEIGHT]);
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
