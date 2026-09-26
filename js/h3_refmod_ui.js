/**
 * Mounts the RefMod builder's panel on `HTCRefModBuilder`.
 *
 * A separate extension and a separate node, deliberately. The chain node is
 * already the widest surface in the pack and CLAUDE.md's standing instruction
 * is not to fold more chrome onto it; building a reference is also a different
 * job from rendering a chain, done once and then left alone with `build=stop`.
 */
import { app } from "../../scripts/app.js";
import { setWidgetVisibility, installHeightGuard } from "./editor/widget_utils.js";
import { createModBuilder } from "./editor/mod_builder.js";

const VERSION = "0.1.0";
const NODE_TYPES = new Set(["HTCRefModBuilder"]);
const STYLE_ID = "h3e-style";
const NODE_WIDTH = 720;
const NODE_HEIGHT = 560;
const PANEL_MIN_H = 420;
const NODE_MIN_WIDTH = 520;
/* Every saved widget layout this node has had, oldest first. `widgets_values`
 * is positional, so a layout change moves every later value; a saved graph is
 * repaired by name before the frontend applies it. */
const LAYOUTS = [
    ["name", "mode", "pool", "latent_frames", "refine_steps", "max_tokens",
     "subfolder", "save", "build"],
    ["name", "mode", "mod_plan", "pool", "latent_frames", "refine_steps",
     "max_tokens", "subfolder", "save", "build"],
    ["name", "mode", "mod_plan", "pool", "latent_frames", "refine_steps",
     "max_tokens", "subfolder", "save", "build", "description"],
    ["name", "mode", "mod_plan", "pool", "latent_frames", "refine_steps",
     "max_tokens", "subfolder", "save", "build", "description", "subject_mask",
     "background_retention"],
];
// Today's: training mode and its three dials removed (2026-09-27).
const CURRENT = ["name", "mod_plan", "max_tokens", "subfolder", "save", "build",
                 "description", "subject_mask", "background_retention"];
const DEFAULTS = { mod_plan: "", max_tokens: 0, subfolder: "", save: true,
                   build: "stop", description: "", subject_mask: "off",
                   background_retention: 0 };

const _cssUrl = new URL("./h3_ref_chain.css", import.meta.url).href;

/* The same stylesheet the chain editor uses, so the two panels cannot drift
 * apart visually, and so a light ComfyUI theme keeps working here too. */
function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement("link");
    link.id = STYLE_ID;
    link.rel = "stylesheet";
    link.href = _cssUrl;
    document.head.appendChild(link);
}

/* Repair a saved graph's values before the frontend applies them, because
 * afterwards the original values are gone.
 *
 * Detection is by content, not length: every old layout holds `mode` --
 * "encode" or "training" -- at index 1, and today's holds `mod_plan` there,
 * which is never either. Length alone collides: the first layout and
 * today's both have nine values. */
function migrateWidgetValues(info) {
    const v = info?.widgets_values;
    if (!Array.isArray(v) || !["encode", "training"].includes(v[1])) return;
    const old = LAYOUTS.find((l) => l.length === v.length);
    if (!old) return;
    const byName = Object.fromEntries(old.map((n, i) => [n, v[i]]));
    const next = CURRENT.map((n) => (n in byName ? byName[n] : DEFAULTS[n]));
    v.splice(0, v.length, ...next);
    console.log("[HandTieClips] migrated a RefMod builder's saved values"
        + (byName.mode === "training" ? " -- it was set to training, which is gone; it now encodes" : ""));
}

function mountBuilder(node) {
    if (node._h3Mod) return;
    if (typeof node.addDOMWidget !== "function") {
        console.warn("[HandTieClips] no addDOMWidget on this frontend; RefMod panel not mounted");
        return;
    }
    // Stale node definition: Python side not reloaded. Silent here made the
    // same failure hard to diagnose on the chain node, so it is loud here.
    if (!node.widgets?.some((w) => w.name === "mod_plan")) {
        console.warn(
            "[HandTieClips] mod_plan widget missing -- restart ComfyUI. Widgets present:",
            (node.widgets || []).map((w) => w.name),
        );
        return;
    }
    ensureStyles();

    const panel = createModBuilder(node, {
        onChange: () => node.graph?.setDirtyCanvas(true, true),
    });

    const domWidget = node.addDOMWidget("h3_modbuilder", "h3_modbuilder", panel.root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => PANEL_MIN_H,
    });
    domWidget.serialize = false;

    installHeightGuard(node, domWidget, {
        minHeight: PANEL_MIN_H, minWidth: NODE_MIN_WIDTH,
    });

    function applyVisibility() {
        // Ownership-based, not a fixed list: the panel reports exactly which
        // widgets it drew this pass, so a dial it declined to draw (a frontend
        // that renamed a widget type, say) stays visible as a native control
        // rather than disappearing with nothing in its place.
        setWidgetVisibility(node, panel.ownedNames());
    }

    applyVisibility();

    node._h3Mod = {
        panel,
        refresh() {
            panel.refresh();
            applyVisibility();
        },
    };
}

console.log(`[HandTieClips] refmod builder ui ${VERSION} loaded`);

app.registerExtension({
    name: "HandTieClips.refmod",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_TYPES.has(nodeData.name)) return;

        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onNodeCreated?.apply(this, arguments);
            mountBuilder(this);
            // Creation only, for the same reason the chain node does it there:
            // a node arriving from a saved workflow keeps whatever size its
            // author dragged it to.
            this.setSize([NODE_WIDTH, NODE_HEIGHT]);
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            migrateWidgetValues(info);
            const r = onConfigure?.apply(this, arguments);
            mountBuilder(this);
            // Widget values arrive with the workflow, after the mount, so the
            // panel has to re-read them or a loaded graph shows an empty list.
            this._h3Mod?.refresh();
            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            this._h3Mod?.panel?.dispose?.();
            this._h3Mod = null;
            return onRemoved?.apply(this, arguments);
        };
    },
});
