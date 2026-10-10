/**
 * MOUNT TEST -- the node's creation path, run for real.
 *
 * Every other check in this repo is blind to the failure that made the user's
 * first contact with the new UI: `ReferenceError: button is not defined`. The
 * name parses, so `node --check` passes; the string pins read other files, so
 * they pass; and the node then fails to load on every workflow that holds it.
 * The only thing that catches it is EXECUTING the path a click runs.
 *
 * So this imports the real entry file, lets it register through the same
 * `app.registerExtension` hook the browser uses, and then calls the patched
 * `onNodeCreated` on a stub node -- for a 6-shot and a 19-shot plan, in Simple
 * and in Shots mode. Any throw anywhere on that path fails.
 *
 * Needs both stubs:
 *   node --import ./tools/app_stub_loader.mjs   (ComfyUI's app.js)
 *   the DOM stub is installed by this file itself.
 *
 * Run: node --import ./tools/app_stub_loader.mjs tools/mount_test.mjs
 */
import { installDom, StubElement } from "./dom_stub.mjs";

const teardown = installDom();

// The entry registers through the `app` it imports from ComfyUI's app.js,
// which app_stub_hook.mjs replaces with a recorder. Importing the entry is the
// whole point: the module body runs, exactly as it does in the browser.
await import("../js/h3_ref_chain_ui.js");
const captured = (globalThis.__appStub?.extensions || [])
  .find((e) => typeof e?.beforeRegisterNodeDef === "function");

let failed = 0;
const ck = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) failed += 1;
};

/* -- the plans ------------------------------------------------------------- */

function planJSON(n, { withRefs = false } = {}) {
  const shots = [];
  for (let i = 0; i < n; i++) {
    shots.push({
      id: `s${i + 1}`,
      beat: `Shot ${i + 1} beats: the figure moves across the room in the available light.`,
      directives: i === 0 ? { framing: "close", camera: "handheld" } : {},
      seed: 777777 + i,
      duration: 192,
      ...(withRefs ? { refs: ["@plate"] } : {}),
    });
  }
  return JSON.stringify(shots);
}

const REF_PLAN = JSON.stringify({
  refs: [
    { tag: "@plate", file: "h3_refs/kitchen.png", subject: 1,
      desc: "a kitchen", shots: [1, 2, 3] },
    { tag: "@coat", file: "h3_refs/coat.png", subject: 1, desc: "a coat" },
  ],
  subjects: {},
});

/* -- the node -------------------------------------------------------------- */

function makeNode({ plan = planJSON(6), refPlan = REF_PLAN, widgets = 48 } = {}) {
  const domWidgets = [];
  // The real widget NAMES matter: mountEditor() looks up shot_plan and
  // ref_plan by name and refuses to mount without them (the "restart ComfyUI"
  // warning). Naming them w0..w47 mounted nothing while still "passing", so
  // these are the actual names from INPUT_TYPES.
  const NAMES = [
    "width", "height", "batch_size", "seed", "steps", "duration", "locked",
    "tone", "anchor", "id", "chains", "hop_script", "prompt", "prompt_prefix",
    "prompt_suffix", "shot_plan", "ref_plan", "voice_file", "voice_start_s",
    "voice_end_s", "voice_every_hop", "start_image_file", "master_audio_file",
    "master_audio_start_s", "pin_to_qwen", "pin_mech", "pin_renorm", "pin_noise",
    "audio_pin_frames", "last_frame_guide", "tone_anchor_ref", "hop_refine",
    "refine_denoise", "refine_steps", "refine_align", "refine_blend",
    "refine_head", "realism_boost", "speed_mode", "upsample",
    "upsample_megapixels", "upsample_denoise", "upsample_model",
    "upsample_lora", "upsample_dit", "upsample_tile_tokens", "cache_hops",
    "cache_budget_gb", "handoff", "master_audio_out",
  ];
  const node = {
    id: 1,
    type: "HandTieClips",
    title: "H3 Ref2VA Chain",
    size: [560, 480],
    properties: {},
    widgets: NAMES.slice(0, widgets).map((name) => ({
      name, type: "custom", value: "",
      options: {}, callback: undefined,
    })),
    inputs: [], outputs: [],
    graph: {
      setDirtyCanvas() {}, _nodes: [],
      getNodesByType() { return []; },
      add(_n) { this._nodes.push(_n); },
      remove(_n) { const i = this._nodes.indexOf(_n); if (i >= 0) this._nodes.splice(i, 1); },
      serialize() { return {}; },
    },
    addDOMWidget(_name, _type, el) {
      domWidgets.push(el);
      return { element: el, options: {} };
    },
    setSize(sz) { this.size = [sz[0], sz[1]]; },
    remove() {},
    setDirtyCanvas() {},
    getInputNode() { return null; },
    computeSize() { return [560, 480]; },
    domWidgets,
  };
  withWidgets(node, { shot_plan: plan, ref_plan: refPlan });
  return node;
}

function withWidgets(node, values) {
  for (const [name, value] of Object.entries(values)) {
    const w = node.widgets.find((x) => x.name === name);
    if (w) w.value = value;
  }
  return node;
}

/* -- the run --------------------------------------------------------------- */

function mount(node) {
  // The exact path the browser takes: register the node def, which patches
  // onNodeCreated, which calls mountEditor.
  const nodeType = { prototype: {} };
  captured.beforeRegisterNodeDef(nodeType, { name: "HandTieClips" });
  nodeType.prototype.onNodeCreated.call(node);
  return node;
}

function countClass(root, cls) {
  let n = 0;
  const walk = (e) => {
    if (!e) return;
    if (e.classList && e.classList.contains(cls)) n += 1;
    (e.children || []).forEach(walk);
  };
  walk(root);
  return n;
}

/* -- the cases ------------------------------------------------------------- */

console.log("\nmount: registration");

ck("the entry registered an extension", !!captured);
ck("it exports beforeRegisterNodeDef", typeof captured?.beforeRegisterNodeDef === "function");

if (!captured?.beforeRegisterNodeDef) {
  console.log("\nMOUNT: cannot continue without the extension hook.");
  process.exit(1);
}

const cases = [
  ["6 shots, Shots mode", { plan: planJSON(6) }],
  ["19 shots, Shots mode", { plan: planJSON(19) }],
  ["6 shots, Simple mode (no shot_plan)", { plan: "" }],
  ["19 shots, with refs cited", { plan: planJSON(19, { withRefs: true }) }],
  ["an unparseable ref_plan", { plan: planJSON(6), refPlan: "{ not json" }],
  ["an empty plan", { plan: "", refPlan: "" }],
];

console.log("");
for (const [name, opts] of cases) {
  let node;
  try {
    node = mount(withWidgets(makeNode(), {
      shot_plan: opts.plan, ref_plan: opts.refPlan, chains: 3,
    }));
    ck(`mounts: ${name}`, true);
  } catch (e) {
    ck(`mounts: ${name}`, false, e && (e.stack || e.message || String(e)).slice(0, 300));
    continue;
  }
  // The panel must actually be in the DOM widget, with the global chrome.
  const root = node.domWidgets[0];
  ck(`  root present: ${name}`, !!root && root.classList.contains("h3e-root"));
  ck(`  status bar: ${name}`, countClass(root, "h3e-statusbar") === 1);
  ck(`  tab strip: ${name}`, countClass(root, "h3e-tabs") >= 1);
}

/* -- D1: only the ACTIVE tab draws its own header row --------------------- */

console.log("\nmount: D1 chrome rows");

/** Visible headers are the ones inside a pane that is not hidden. */
function visibleHeads(root) {
  const out = [];
  const walk = (e, hidden) => {
    if (!e) return;
    const hid = hidden || e.hidden === true;
    if (!hid && e.classList && e.classList.contains("h3e-head")) {
      out.push((e.querySelector ? null : null)
        || (e.children.find((c) => c.classList?.contains("h3e-title")) || {}).textContent
        || "");
    }
    (e.children || []).forEach((c) => walk(c, hid));
  };
  walk(root, false);
  return out;
}

function mountFresh(opts) {
  const node = withWidgets(makeNode(), {
    shot_plan: opts.plan ?? planJSON(6),
    ref_plan: opts.refPlan ?? REF_PLAN,
    chains: 3,
  });
  mount(node);
  return node;
}

{
  const node = mountFresh({});
  const root = node.domWidgets[0];

  // Row 1: the status bar, on every tab (global chrome).
  ck("row 1: exactly one status bar", countClass(root, "h3e-statusbar") === 1);
  // Row 2: the tab strip, global.
  ck("row 2: exactly one tab strip", countClass(root, "h3e-tabs") === 1);

  // Row 3: exactly ONE visible header -- the active tab's. All tab bodies stay
  // in the DOM (createTabs toggles .hidden), so this is the assertion that the
  // inactive ones are genuinely not drawn.
  const heads = visibleHeads(root);
  ck(`row 3: one visible header, not ${heads.length}`, heads.length === 1,
     `saw ${JSON.stringify(heads)}`);

  // And it is SCRIPT's, because that is the default surface.
  ck("row 3: the visible header is SCRIPT's",
     heads.length === 1 && /SCRIPT/i.test(heads[0]), JSON.stringify(heads));

  // Switching tabs moves the header rather than adding one.
  const tabBtns = [];
  const walkTabs = (e) => {
    if (!e) return;
    if (e.classList && e.classList.contains("h3e-tab")) tabBtns.push(e);
    (e.children || []).forEach(walkTabs);
  };
  walkTabs(root);
  ck("row 2: six tab buttons", tabBtns.length === 6, `got ${tabBtns.length}`);

  const refsBtn = tabBtns.find((b) => /refs/i.test(b.textContent));
  ck("row 2: a Refs tab exists", !!refsBtn);
  if (refsBtn) {
    refsBtn.click();
    const after = visibleHeads(root);
    ck("row 3: still one header after switching to Refs", after.length === 1,
       `saw ${JSON.stringify(after)}`);
    ck("row 3: it is now REFERENCES'",
       after.length === 1 && /REFERENCE/i.test(after[0]), JSON.stringify(after));
  }
}

console.log("");
process.exit(failed ? 1 : 0);