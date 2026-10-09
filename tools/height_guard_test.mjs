// The height guard, under Node. Run by tools/check_height_guard.py.
//
// The guard is the one piece of this panel that used to move on its own every
// frame. Growing is the visible symptom, but the interesting property is the
// FIXPOINT: after fitting to a surface, panelHeight() must report back the
// same number, so the next layout pass asks for the same size and the node
// stops. That is what the deadband protects, and it is what these cases pin.
//
// Needs the app.js stub: `node --import ./app_stub_loader.mjs` (the .py wrapper
// passes it). widget_utils.js imports ComfyUI's app for an unrelated canvas
// helper; installHeightGuard itself never touches `app`.
import { installHeightGuard } from "../js/editor/widget_utils.js";

let failed = 0;
const ck = (name, ok, detail = "") => {
    console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
    if (!ok) failed += 1;
};

/* -- a node, a domWidget, and a root with two switchable surfaces ---------- */

// A stand-in for the LiteGraph node. setSize is what the real one does, and it
// is the ONLY way height changes, so recording the calls is the measurement.
function makeNode({ w = 560, h = 900, widgets = 40 } = {}) {
    const calls = [];
    return {
        size: [w, h],
        properties: {},
        widgets: new Array(widgets).fill(0).map(() => ({})),
        inputs: [],
        outputs: [],
        graph: null,
        setSize(sz) { this.size = [sz[0], sz[1]]; calls.push([sz[0], sz[1]]); },
        // Real nodes report their chrome (title + slots) from here.
        computeSize() { return [this.size[0], 120]; },
        calls,
        // The chrome a non-node caller cannot compute: what installHeightGuard
        // subtracts from the node height to get the panel height.
        get chrome() { return 120; },
    };
}

// The root is a flex column of visible sections; a hidden one contributes
// nothing. scrollHeight stands in for "how tall this surface wants to be".
// getClientRects() is how the guard asks "is this actually mounted?", so a
// section that is hidden (display:none) answers empty.
function section(scrollHeight, hidden = false) {
    return {
        scrollHeight,
        hidden,
        getClientRects: () => (hidden ? [] : [{}]),
    };
}

function makeRoot(sections) {
    return {
        _sections: sections,
        get children() { return this._sections; },
    };
}

// The guard reads the root's flex gap and padding off getComputedStyle.
// Node has no layout engine, so the numbers are fixed here; they only have to
// be self-consistent with what the cases below assert.
globalThis.getComputedStyle ??= () => ({ rowGap: "8px", paddingTop: "2px", paddingBottom: "2px" });

const node = makeNode();
const root = makeRoot([]);
const domWidget = {};
const g = installHeightGuard(node, domWidget, {
    minHeight: 360, minWidth: 560, root,
});

// Two surfaces of different heights: a tall tab and a short one.
const CHROME = 120, SLACK = 8;
const MIN = 360;
// naturalHeight() sums the visible sections' scrollHeight, then adds the flex
// gap between them and the root's padding on both sides -- the same terms the
// stubbed getComputedStyle above reports (8px gap, 2px padding).
const GAP = 8, PAD = 2;
const panelFor = (sections) => sections.reduce((a, s) => a + s.scrollHeight, 0)
    + GAP * (sections.length - 1) + PAD * 2;
const nodeFor = (panelPx) => CHROME + panelPx + SLACK;

// rAF is not available in Node; installHeightGuard falls back to fitting
// synchronously, which is what makes it testable here at all.
console.log("\ninstallHeightGuard -- fit both ways");

const TALL = [section(1000)];
const SHORT = [section(400)];
root._sections = TALL;
g.sync("tall");
ck("a tall surface grows the node",
    node.size[1] === nodeFor(panelFor(TALL)),
    `${node.size[1]} vs ${nodeFor(panelFor(TALL))}`);
ck("the panel height reads back the target",
    g.panelHeight() === panelFor(TALL), `${g.panelHeight()}`);

root._sections = SHORT;
g.sync("short");
ck("a shorter surface shrinks the node (A2: both directions)",
    node.size[1] === nodeFor(panelFor(SHORT)),
    `${node.size[1]} vs ${nodeFor(panelFor(SHORT))}`);
ck("the panel height still reads back the target",
    g.panelHeight() === panelFor(SHORT), `${g.panelHeight()}`);

// A hidden section is not mounted: switching to a short surface has to be
// able to ignore the tall one entirely.
const HIDDEN_TALL = [section(1000), section(300, true)];
root._sections = HIDDEN_TALL;
g.sync("short2");
ck("a hidden section does not ask for height",
    node.size[1] === nodeFor(panelFor([section(1000)])),
    `${node.size[1]}`);

// -- the deadband. This is the loop-freedom property.
console.log("\ninstallHeightGuard -- the deadband");
const before = node.size[1];
const n0 = node.calls.length;
// A surface one pixel off: within tolerance, so the node must not be told to
// resize. If it were, two layout passes rounding differently would walk the
// node up or down forever -- the bug the `_h` note is about.
root._sections = TALL;
node.size[1] = nodeFor(panelFor(TALL)) + 1;
g.sync("rounding");
ck("within the deadband the node is not resized",
    node.calls.length === n0, `${node.calls.length - n0} extra setSize`);
node.size[1] = before;

// Re-syncing the same surface must be a no-op -- the fixpoint.
const n1 = node.calls.length;
g.sync("same");
g.sync("same");
ck("re-syncing the same surface is a fixpoint (no per-frame growth)",
    node.calls.length === n1, `${node.calls.length - n1} extra setSize`);

// -- the minimum. A surface shorter than minHeight clamps, it does not crush.
console.log("\ninstallHeightGuard -- the floor");
root._sections = [section(10)];
g.sync("tiny");
ck("a tiny surface clamps the panel to the minimum (the node still fits chrome)",
    node.size[1] === CHROME + MIN + SLACK,
    `${node.size[1]} vs ${CHROME + MIN + SLACK}`);
ck("the panel never reports less than the minimum",
    g.panelHeight() === MIN, `${g.panelHeight()}`);

// -- the pin.
console.log("\ninstallHeightGuard -- the pin");
root._sections = TALL;
g.sync("pretall");
const pinnedAt = node.size[1];
g.togglePin();
ck("pinning is reflected", g.pinned === true, `${g.pinned}`);
const n2 = node.calls.length;
root._sections = SHORT;
g.sync("ignored");
ck("a pinned node does not shrink", node.size[1] === pinnedAt, `${node.size[1]}`);
ck("a pinned node is not resized at all", node.calls.length === n2,
    `${node.calls.length - n2} extra setSize`);
ck("grow() is inert while pinned", (g.grow(500), node.size[1] === pinnedAt),
    `${node.size[1]}`);
g.unpin();
ck("unpinning clears the flag", g.pinned === false, `${g.pinned}`);
ck("unpinning refits to what is showing",
    node.size[1] === nodeFor(panelFor(SHORT)),
    `${node.size[1]} vs ${nodeFor(panelFor(SHORT))}`);

// The pin lives on the node, so it survives a reload.
node.properties.h3_height_pin = true;
const g2 = installHeightGuard(node, {}, {
    minHeight: 360, minWidth: 560, root,
});
ck("a re-install returns the same guard (the reload path)",
    g2 === g, "a second install built a second guard");
ck("the pin survives as a node property", node._h3HeightGuard.pinned === true);

console.log(failed ? `\n${failed} FAILURE(S)` : "\nHEIGHT GUARD: all passed");
process.exit(failed ? 1 : 0);