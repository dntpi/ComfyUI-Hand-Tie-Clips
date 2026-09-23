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
// Layout before `mod_plan` was added, and where it went in.
const OLD_WIDGET_COUNT = 9;
const MOD_PLAN_INDEX = 2;

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

/* `widgets_values` is positional, so inserting `mod_plan` ahead of `pool`
 * shifted every saved value one slot right -- a graph saved before it
 * existed loads with latent_frames in pool (out of range) and subfolder in
 * max_tokens (wrong type). Repair the array before the frontend applies it,
 * because afterwards the original values are gone.
 *
 * Detection is the length plus the type at the insertion point: the current
 * layout always holds mod_plan, a string, at index 2. */
function migrateWidgetValues(info) {
    const v = info?.widgets_values;
    if (!Array.isArray(v) || v.length !== OLD_WIDGET_COUNT) return;
    if (typeof v[MOD_PLAN_INDEX] === "string") return;
    v.splice(MOD_PLAN_INDEX, 0, "");
    console.log("[HandTieClips] migrated a pre-mod_plan RefMod builder's widget values");
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
