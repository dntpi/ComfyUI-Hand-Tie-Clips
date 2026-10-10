import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import {
    button, createTabs, el, isolateEvents, installHeightGuard, setWidgetVisibility,
    widgetByName,
} from "./editor/widget_utils.js";
import { createPlanEditor } from "./editor/plan_editor.js";
import { createRefRail, parseRefPlan, refPlanToJson } from "./editor/ref_rail.js";
import { createRunPanel } from "./editor/run_panel.js";
import { createWriterBar } from "./editor/writer_bar.js";
import { createVideoSwap } from "./editor/video_swap.js";
import { createMediaStrip, MEDIA_WIDGETS } from "./editor/media_strip.js";
import { createTimeline } from "./editor/timeline.js";

const VERSION = "v2.2.0";
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
// A4's surface flag. One object rather than a `let showingRun` because
// createTabs' onShow closure is created before the surface code runs.
const surface = { showingRun: false };
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
// `prompt` is hidden in Simple too: the SCRIPT tab draws it as its own box.
const SIMPLE_HIDDEN = ["prompt", "shot_plan", "ref_plan"].concat(MEDIA_WIDGETS);

const _cssUrl = new URL("./h3_ref_chain.css", import.meta.url).href;

/* Combo values that were removed from a menu. A saved graph still holds them,
 * and ComfyUI refuses to queue a value that is not in the list, so they are
 * mapped on load -- to the setting that is closest in effect -- and logged. */
const RETIRED = {
    // `sigma` corrected the wrong statistic (total spread falls as a chain
    // bakes); `on` is what it was saved as before 0.5. Neither did any good.
    pin_renorm: { sigma: "off", on: "off" },
};

function migrateRetired(node) {
    for (const [name, map] of Object.entries(RETIRED)) {
        const w = widgetByName(node, name);
        if (w && Object.hasOwn(map, w.value)) {
            console.log(`[HandTieClips] ${name}=${w.value} was retired; set to ${map[w.value]}`);
            w.value = map[w.value];
        }
    }
}

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
            node._h3Editor?.swap?.render?.();
            node.graph?.setDirtyCanvas?.(true, true);
        },
        hopCount,
    });

    /* The rail draws one "rides hops" chip per hop, from hopCount(), at the
     * moment it renders. Nothing was telling it to render again when the hop
     * count changed -- so adding a shot left the rail showing the old number of
     * chips, and a reference could not be scheduled onto the new hop except by
     * editing the JSON by hand. Both places that can change the count call
     * this: the script (a shot added or removed) and RUN (`chains`, which is
     * what hopCount falls back to in Simple mode).
     *
     * Guarded on the count actually changing, because the script's onChange
     * also fires on every keystroke in a beat, and rebuilding the rail's DOM
     * per character would be waste with a focus bug waiting inside it. */
    let lastHops = hopCount();
    const syncRailHops = () => {
        const now = hopCount();
        if (now === lastHops) return;
        lastHops = now;
        rail.render();
    };

    // Declared before the editor and assigned after it: the editor's
    // onChange refreshes the media strip, and a `const` below this point
    // would be in the temporal dead zone if the editor ever fired onChange
    // while constructing.
    let mediaStrip = null;

    const editor = createPlanEditor(node, {
        onChange: () => {
            applyVisibility();
            syncBadges();
            syncRailHops();
            // Adding a shot, or changing `duration` / `overlap`, changes the
            // chain length -- which IS the width of the master-audio window.
            // Without this the box keeps the previous chain's width until
            // something else happens to rebuild the strip.
            mediaStrip?.render();
            timeline?.render();
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });

    // Declared before the run panel because both panels' onChange redraw it:
    // solo and lock are driven from here and from the cards and RUN alike.
    let timeline = null;

    // D2: the filmstrip draws a sprite for each hop the last run rendered, and
    // the TIMELINE is what already receives those hop keys. Wired here rather
    // than inside the editor because the timeline is built after it -- so this
    // is a late bind, not a call at construction time.
    editor.setHopKeySource(() => timeline?.hopKeys?.() || null);

    // D3: the score strip's under-band reads the ref plan, and its NEXT QUEUE
    // line and section markers read the timeline -- which is where forecastPlan
    // is already called, on the same arguments, for its own note. Wired here
    // because the rail and the timeline are built after the editor.
    editor.setForecastSources({
        refPlan: () => rail.getPlan?.() || null,
        forecast: () => timeline?.forecastNow?.(editor.shots()) || null,
        tieSections: (shots, frames) => timeline?.sections?.() || [],
    });

    const runPanel = createRunPanel(node, {
        onChange: () => {
            // render_from / render_through are drawn in RUN and also driven by
            // the ⏵ button on each shot card, so a change here has to re-mark
            // which cards the range now leaves out. Cheap: at most 24 cards,
            // and only ever on a user gesture.
            editor.render();
            syncRailHops();
            timeline?.render();
            syncBadges();
            node.graph?.setDirtyCanvas?.(true, true);
        },
        hopCount,
        // In Shots mode these two are decided by the plan, not by the user:
        // run() ignores `chains` and forces `hop_script=next`. They are already
        // in SHOTS_HIDDEN, so dropping them here leaves them hidden rather than
        // offering a control that does nothing.
        suppressed: () => (editor.mode() === "shots" ? ["chains", "hop_script"] : []),
    });

    timeline = createTimeline(node, {
        editor,
        // Badges only: a redraw of the timeline must never redraw the editor.
        // A workflow saved on this tab draws it while the tabs are built,
        // before syncBadges exists; the refresh after configure badges it.
        onChange: () => {
            try { syncBadges(); } catch { /* not mounted yet */ }
        },
    });

    mediaStrip = createMediaStrip(node, {
        onChange: () => node.graph?.setDirtyCanvas?.(true, true),
        // The master-audio window is drawn the width of the chain, so it has
        // to ask the editor rather than the widgets: `duration` is only the
        // default, a shot may override it, and the count comes from the cards.
        // Returns 0 before the vocabulary arrives, which `createTrimBar` reads
        // as "no span" and falls back to an ordinary trim -- a bar with the
        // wrong width would be worse than one that waits.
        chainSeconds: () => {
            try {
                const t = editor.timing?.();
                const fps = t?.fps || 24;
                return t && t.total > 0 ? t.total / fps : 0;
            } catch {
                return 0;
            }
        },
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

    const videoSwap = createVideoSwap(node, {
        onWritten: (shotJson, extra) => {
            if (shotJson) {
                planWidget.value = shotJson;
                node.properties ??= {};
                node.properties[EDITOR_MODE_PROP] = "shots";
            }
            if (extra?.reference_video_desc) {
                const w = widgetByName(node, "reference_video_desc");
                if (w) {
                    w.value = extra.reference_video_desc;
                    try { w.callback?.(w.value, node.graph?.canvas, node); }
                    catch (err) {
                        console.error("[HandTieClips] desc callback failed:", err);
                    }
                }
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
        { id: "timeline", label: "Timeline", title: "Retake board: each hop's render, the joins, and what the next queue re-renders", body: timeline.root },
        { id: "media", label: "Media", title: "Start image, reference clip, voice, soundtrack", body: mediaStrip.root },
        { id: "write", label: "Write", title: "Draft a plan with a local model", body: writer.root },
        { id: "swap", label: "Swap", title: "One-hop identity swap from clip 1", body: videoSwap.root },
    ], {
        active: node.properties?.[ACTIVE_TAB_PROP],
        onShow: (id) => {
            node.properties ??= {};
            node.properties[ACTIVE_TAB_PROP] = id;
            // WRITE is a <details> and its contents are loaded on first open --
            // opening it here keeps that laziness (nothing is fetched until you
            // visit the tab) while making the pane usable once you have.
            if (id === "write") writer.root.open = true;
            if (id === "timeline") timeline.render();
            if (id === "swap") {
                videoSwap.root.open = true;
                videoSwap.render?.();
            }
            // The panel does not follow the node's height on its own; see the
            // note on installHeightGuard. sync() resizes the node to what is
            // now showing, up OR down, which is the whole of A2: the Script
            // tab is tall and Media is short, so a switch has to be able to
            // make the node smaller as well as bigger.
            // Skipped while RUN is the surface (A4) -- the tab is not what is
            // on screen then, and refitting to a hidden tab would fight the
            // height the user just saw settle on the RUN column. A plain flag
            // read through an object: onShow is defined before setSurface and
            // cannot close over a const that is still in its TDZ.
            if (surface.showingRun) return;
            node._h3HeightGuard?.sync("tab " + id);
            node.graph?.setDirtyCanvas?.(true, true);
        },
    });
    // A3's status bar: permanent, above the tabs, on every tab and on RUN
    // itself. The run panel owns the text (it is the only thing that knows
    // which widget holds what) and this row owns the placement.
    //
    // No rhythm/metronome item here, deliberately. On a plain plan equal hop
    // lengths are normal -- a continuous film wants them -- so surfacing it as
    // a fault would teach the user to ignore the one row that does report
    // faults. "Does it land on the music" is a question MV mode asks, and
    // MV mode puts it on its own card where it means something.
    const statusBar = el("div", "h3e-statusbar");
    const statusText = runPanel.digestEl;
    statusBar.appendChild(statusText);
    // A10's warning chip. Hidden unless run() reported something; see the
    // ht_previs_warn field in the preview payload.
    const warnChip = el("span", "h3e-warnchip");
    warnChip.hidden = true;
    statusBar.appendChild(warnChip);
    node._h3Warn = {
        // `warns` is [{label, frame}] from the run. Keep the first few: a chip
        // is a pointer, not a log, and four is already more than fits.
        show(warns) {
            if (!warns || !warns.length) {
                warnChip.hidden = true;
                warnChip.textContent = "";
                warnChip.title = "";
                return;
            }
            warnChip.hidden = false;
            warnChip.textContent = `⚠ ${warns.length} previs cut`
                + (warns.length === 1 ? "" : "s");
            warnChip.title = warns.map((w) => w.label).join("\n");
        },
    };

    // A4: RUN is its own surface rather than a <details> stacked under every
    // tab. That stacking is why the Script tab measured about 3,600px: the
    // authoring surface and all 65 dials were in one column, and the height
    // guard -- correctly, at the time -- grew the node to hold both. Only one
    // thing may ask for height at a time.
    root.appendChild(statusBar);
    root.appendChild(tabs.strip);
    root.appendChild(tabs.bodies);
    root.appendChild(runPanel.root);

    // The ⚙ control. A tab rather than a seventh work tab, because RUN is not
    // where you write the film -- it is the panel you check before every
    // queue, and keeping it out of the tab strip is what keeps the strip
    // legible.
    const runBtn = button("⚙", "The render controls: canvas, sampler, cache, upscaler",
        () => setSurface(getSurface() === "run" ? lastTab : "run"),
        "h3e-btn h3e-mode h3e-runbtn");
    tabs.strip.appendChild(runBtn);

    // A2's manual pin, on the tab strip so it is reachable without opening
    // RUN. Freezing the height is the answer to "I sized this node and it
    // moved anyway" -- which is the only complaint a fit-to-content node can
    // legitimately earn.
    const pinBtn = button(
        "⇕", "Pin the node's height: stop it following the panel",
        () => {
            const on = node._h3HeightGuard?.togglePin();
            if (!on) node._h3HeightGuard?.sync("unpin");
            paintPin();
        }, "h3e-btn h3e-pin");
    function paintPin() {
        const on = !!node._h3HeightGuard?.pinned;
        pinBtn.classList.toggle("h3e-on", on);
        pinBtn.title = on
            ? "Height pinned - the node keeps this size until you unpin"
            : "Pin the node's height: stop it following the panel";
    }
tabs.strip.appendChild(pinBtn);
tabs.strip.appendChild(el("span", "h3e-stripgap", ""));
node._h3PaintPin = paintPin;

    // ---- A4: the surface ----------------------------------------------------
    //
    // "work" shows a tab; "run" shows the render controls. Remembered in
    // node.properties beside h3_active_tab rather than in a module variable,
    // because a reload has to land the user back on the surface they left --
    // same reason the tab itself is stored there.
    const SURFACE_PROP = "h3_surface";
    let lastTab = tabs.active() || "script";

    function getSurface() {
        const s = node.properties?.[SURFACE_PROP];
        return s === "run" ? "run" : "work";
    }

    function setSurface(next) {
        const run = next === "run";
        surface.showingRun = run;
        node.properties ??= {};
        node.properties[SURFACE_PROP] = run ? "run" : "work";
        if (!run) {
            // Leaving RUN puts the work tab back, so the two controls agree.
            lastTab = tabs.active() || lastTab;
            tabs.show(lastTab);
        }
        tabs.bodies.hidden = run;
        runPanel.root.hidden = !run;
        // A <details> cannot be told to be open by `hidden` alone -- if it was
        // left open, its body is still in the flow and would show through the
        // tab strip. Open it on entry and close it on exit so exactly one
        // surface is laid out at a time.
        runPanel.root.open = run;
        runBtn.classList.toggle("h3e-on", run);
        runBtn.title = run
            ? "Back to the tabs"
            : "The render controls: canvas, sampler, cache, upscaler";
        if (run) runPanel.sync();
        // The height is the surface's, not the tab's: RUN is a long column of
        // groups and the Script tab is short by comparison.
        node._h3HeightGuard?.sync(run ? "run" : "tab " + tabs.active());
    }

    // `r` toggles RUN, unless the user is typing. A keystroke landing in a
    // textarea is text, not a command, and swallowing the letter is the kind
    // of thing that makes a shortcut feel broken rather than absent.
    root.addEventListener("keydown", (e) => {
        if (e.key !== "r" && e.key !== "R") return;
        if (e.ctrlKey || e.metaKey || e.altKey) return;
        const t = e.target;
        const tag = t?.tagName || "";
        const typing = tag === "INPUT" || tag === "TEXTAREA"
            || t?.isContentEditable === true;
        if (typing) return;
        e.preventDefault();
        e.stopPropagation();
        setSurface(getSurface() === "run" ? "work" : "run");
    });

    const domWidget = node.addDOMWidget("h3_editor", "h3_editor", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => EDITOR_MIN_H,
    });
    domWidget.serialize = false;

    installHeightGuard(node, domWidget, {
        minHeight: EDITOR_MIN_H,
        minWidth: NODE_MIN_WIDTH,
        root,   // A2: the height target is measured off this, not declared per tab.
    });

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
            tabs.badge("timeline", timeline.badge());
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
            // Same story for the surface: properties arrive with the workflow,
            // after the mount already put it on the work tabs. Without this a
            // workflow saved on RUN came back on Script, and the node's height
            // jumped on load.
            if (node.properties?.[SURFACE_PROP]) {
                setSurface(getSurface());
            }
            editor.reload();
            rail.render();
            mediaStrip.render();
            // Properties arrive with the workflow, AFTER the mount, so a loaded
            // graph has to be told to put the writer's brief and held draft
            // back -- the mount ran before there was anything to restore.
            writer.restore();
            writer.syncHops();
            videoSwap.render?.();
            timeline.render();
            applyVisibility();   // syncs the run panel on the way through
            syncBadges();
        },
        rail,
        editor,
        runPanel,
        writer,
        swap: videoSwap,
        timeline,
tabs,
        // A4: lets the surface be driven from outside the panel (and lets a
        // checker assert on it without reaching into the DOM).
        getSurface,
        setSurface,
    };

applyVisibility();
    rail.render();
    mediaStrip.render();
    editor.render();
    syncBadges();
    paintPin();
    // The mount runs before properties arrive, so this only matters for a
    // fresh node; refresh() re-applies it once a saved workflow lands.
    setSurface(getSurface());

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
        try { videoSwap.destroy?.(); } catch (err) {
            console.error("[HandTieClips] SWAP teardown failed:", err);
        }
        try { timeline.destroy(); } catch (err) {
            console.error("[HandTieClips] timeline teardown failed:", err);
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
            migrateRetired(this);
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
