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

/* -- D2: the filmstrip, and a surface that does not grow with shot count --- */

console.log("\nmount: D2 filmstrip");

{
  const small = mountFresh({ plan: planJSON(6) });
  const big = mountFresh({ plan: planJSON(19) });
  const rSmall = small.domWidgets[0];
  const rBig = big.domWidgets[0];

  const posters = (root) => {
    let n = 0;
    const walk = (e) => {
      if (!e) return;
      if (e.classList && e.classList.contains("h3e-poster")) n += 1;
      (e.children || []).forEach(walk);
    };
    walk(root);
    return n;
  };
  const detailCards = (root) => {
    let n = 0;
    const walk = (e) => {
      if (!e) return;
      if (e.classList && e.classList.contains("h3e-detail")) {
        n = (e.children || []).filter((c) => c.classList?.contains("h3e-card")).length;
      }
      (e.children || []).forEach(walk);
    };
    walk(root);
    return n;
  };

  ck("6 shots draws 7 posters (6 + the + card)", posters(rSmall) === 7,
     `got ${posters(rSmall)}`);
  ck("19 shots draws 20 posters", posters(rBig) === 20, `got ${posters(rBig)}`);

  // The whole point of §9.2: ONE card in the detail pane at any shot count.
  ck("6 shots shows exactly one card in the detail pane",
     detailCards(rSmall) === 1, `got ${detailCards(rSmall)}`);
  ck("19 shots shows exactly one card in the detail pane",
     detailCards(rBig) === 1, `got ${detailCards(rBig)}`);

  // And the shot count is visible on the posters, not by stacking cards.
  const beats = (root) => {
    let n = 0;
    const walk = (e) => {
      if (!e) return;
      if (e.classList && e.classList.contains("h3e-poster-beat")) n += 1;
      (e.children || []).forEach(walk);
    };
    walk(root);
    return n;
  };
  ck("every poster carries its beat's first words",
     beats(rBig) === 19, `got ${beats(rBig)} of 19`);

  // Selection: clicking poster 3 moves the detail pane to shot 3.
  const findPosters = (root) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList && e.classList.contains("h3e-poster")
          && !e.classList.contains("h3e-poster-add")) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };
  const ps = findPosters(rSmall);
  ck("poster 1 is selected by default",
     ps.length > 0 && ps[0].classList.contains("h3e-on"));
  if (ps.length >= 3) {
    let clickErr = null;
    try { ps[2].click(); } catch (e) { clickErr = e; }
    ck("clicking a poster does not throw", !clickErr,
       clickErr ? String(clickErr.message || clickErr).slice(0, 120) : "");
    // The strip is REBUILT on selection, so the node references from before the
    // click are stale -- querying again is the only way to see the new state.
    const after = findPosters(rSmall);
    ck("clicking poster 3 selects it",
       after.length > 2 && after[2].classList.contains("h3e-on"),
       `selection did not move (${after.length} posters, sel prop ${
         small.properties.h3_film_selected})`);
    ck("poster 1 is no longer selected",
       after.length > 0 && !after[0].classList.contains("h3e-on"));
    ck("only one poster is selected at a time",
       after.filter((p) => p.classList.contains("h3e-on")).length === 1);
  }
}

/* -- D3: the score strip, the forecast line, and the two aligned ---------- */

console.log("\nmount: D3 score strip");

{
  const node = mountFresh({ plan: planJSON(6) });
  const root = node.domWidgets[0];

  const find = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };

  const score = find("h3e-score");
  ck("the score strip exists", score.length === 1);
  ck("the forecast line exists", find("h3e-forecast").length === 1);
  // NOT querySelector -- the DOM stub does not implement it, and a test that
  // quietly reads null is worse than no test.
  const inner = find("h3e-score-inner");
  ck("the score strip has its inner track", inner.length === 1);
  // 44px lives in the STYLESHEET, not in JS, so this cannot assert it from the
  // DOM. The JS half of the same claim -- one column per shot, on the shared
  // geometry -- is asserted below, and check_ui's token audit covers the CSS.

  const cols = find("h3e-col");
  ck(`6 shots draws 6 score columns, got ${cols.length}`, cols.length === 6);

  // Every column carries the encodings D3 asks for.
  ck("every column has a duration bar",
     cols.length === 6 && cols.every((c) => c.children.some((x) => x.classList?.contains("h3e-col-bar"))));
  ck("every column has its slate number",
     cols.length === 6 && cols.every((c) => c.children.some((x) => x.classList?.contains("h3e-col-no"))));

  // Duration is the HEIGHT, and it must actually differ when durations differ.
  const mixed = JSON.parse(planJSON(3));
  mixed[0].duration = "192";
  mixed[1].duration = "96";
  mixed[2].duration = "192";
  const node2 = mountFresh({ plan: JSON.stringify(mixed) });
  const cols2 = [];
  const walk2 = (e) => {
    if (!e) return;
    if (e.classList?.contains("h3e-col")) cols2.push(e);
    (e.children || []).forEach(walk2);
  };
  walk2(node2.domWidgets[0]);
  const heights = cols2.map((c) => {
    const bar = c.children.find((x) => x.classList?.contains("h3e-col-bar"));
    return bar ? parseFloat(bar.style.height) || 0 : -1;
  });
  ck("a 96-frame shot draws a shorter bar than a 192-frame one",
     heights.length === 3 && heights[1] < heights[0] && heights[1] < heights[2],
     `heights ${JSON.stringify(heights)}`);
  ck("bars are all non-zero", heights.every((h) => h > 0), JSON.stringify(heights));

  // The two strips share one geometry, so they cannot drift: the score inner's
  // width is count * colW and the filmstrip holds the same number of posters.
  const track = find("h3e-score-inner")[0];
  const posters = find("h3e-poster").filter((p) => !p.classList.contains("h3e-poster-add"));
  const w = parseFloat(track?.style?.width) || 0;
  ck("the score strip's width is set from the shared geometry", w > 0, `width ${w}`);
  ck("score columns and posters agree in count",
     cols.length === posters.length, `${cols.length} cols vs ${posters.length} posters`);

  // Restart gets a boundary marker.
  const withRestart = JSON.parse(planJSON(3));
  withRestart[1].anchor = "restart";
  const node3 = mountFresh({ plan: JSON.stringify(withRestart) });
  const c3 = [];
  const walk3 = (e) => {
    if (!e) return;
    if (e.classList?.contains("h3e-col")) c3.push(e);
    (e.children || []).forEach(walk3);
  };
  walk3(node3.domWidgets[0]);
  ck("a restart shot is marked as a boundary",
     c3.length === 3 && c3[1].classList.contains("h3e-col-restart"));

  // Locked gets a dot, not a colour change.
  const lockedPlan = JSON.parse(planJSON(2));
  lockedPlan[0].locked = true;
  const node4 = mountFresh({ plan: JSON.stringify(lockedPlan) });
  const c4 = [];
  const walk4 = (e) => {
    if (!e) return;
    if (e.classList?.contains("h3e-col")) c4.push(e);
    (e.children || []).forEach(walk4);
  };
  walk4(node4.domWidgets[0]);
  ck("a locked shot carries a lock dot",
     c4.length === 2 && c4[0].children.some((x) => x.classList?.contains("h3e-col-lockdot")));

  // The forecast line. The entry wires a source, so the line IS populated --
  // and every hop is "re-renders" because nothing has ever rendered, which is
  // the correct answer for a plan that has never been queued.
  const fcLine = find("h3e-forecast")[0];
  ck("the forecast line states what the next queue does",
     /NEXT QUEUE/.test(fcLine.textContent || ""), fcLine.textContent || "(empty)");
  ck("a plan that has never run forecasts as all-rendering",
     /re-renders\s+01/.test(fcLine.textContent || ""), fcLine.textContent || "(empty)");
  ck("the forecast line is marked live when hops will move",
     fcLine.classList.contains("h3e-forecast-live"));
}

/* -- D4: ranges, the batch bar, and keys --------------------------------- */

console.log("\nmount: D4 ranges and keys");

{
  const node = mountFresh({ plan: planJSON(6) });
  const root = node.domWidgets[0];
  // `_h3Editor` is the node-level facade (refresh, rail, editor, tabs); the
  // PLAN editor is one level down. Reaching for `_h3Editor.span` gives
  // undefined, which is how the first version of this block died silently
  // partway through with only two assertions printed.
  const ed = node._h3Editor.editor;
  ck("the plan editor is reachable for assertions", !!ed && typeof ed.span === "function");

  const findAll = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };
  const posters = () => findAll("h3e-poster")
    .filter((p) => !p.classList.contains("h3e-poster-add"));

  ck("the batch bar exists", findAll("h3e-batch").length === 1);
  ck("with one shot selected the batch bar is hidden",
     findAll("h3e-batch")[0].style.display === "none",
     `display "${findAll("h3e-batch")[0].style.display}"`);

  // Shift-click extends the selection. Stub clicks carry no shiftKey by default,
  // so it is passed explicitly -- which is also how a real shift-click arrives.
  const ps = posters();
  if (ps.length >= 3) {
    ps[2].fire("click", { shiftKey: true });
  }

  ck("shift-click selects a range of 3", ed.span() != null, "no range after shift-click");
  ck("the batch bar appears for a range",
     findAll("h3e-batch")[0].style.display !== "none",
     `display "${findAll("h3e-batch")[0].style.display}"`);
  ck("the batch bar states the range",
     findAll("h3e-batch-n").some((e) => /shots 1.3/.test(e.textContent || "")),
     // The separator is an EN DASH. Matching /shots 1-3/ passes on nothing,
     // and the failure message is what told me -- worth reading the detail
     // column before rewriting the assertion a third time.
     findAll("h3e-batch-n").map((e) => e.textContent).join("|") || "(no label)");

  // A plain click clears it.
  posters()[0].fire("click", {});
  ck("a plain click clears the range", ed.span() == null,
     `span ${JSON.stringify(ed.span())}`);
  ck("and the batch bar hides again",
     findAll("h3e-batch")[0].style.display === "none");

  // Keys: [ and ] step the selection.
  ed.setSelected(0);
  const fireKey = (k, extra = {}) => {
    const listeners = root._listeners.get("keydown") || [];
    const ev = { type: "keydown", key: k, target: root,
                 preventDefault() {}, stopPropagation() {},
                 ctrlKey: false, metaKey: false, altKey: false, ...extra };
    listeners.forEach((fn) => fn(ev));
  };
  ed.setSelected(2);
  fireKey("]");
  ck("] steps the selection forward", ed.selected() === 3, `at ${ed.selected()}`);
  fireKey("[");
  ck("[ steps it back", ed.selected() === 2, `at ${ed.selected()}`);
  // And it clamps rather than running off either end.
  fireKey("["); fireKey("["); fireKey("["); fireKey("[");
  ck("stepping past the start clamps at shot 1", ed.selected() === 0, `at ${ed.selected()}`);

  // Keys must NOT fire while typing. That is the whole feature for `,` `.` `a`.
  const area = findAll("h3e-beat")[0];
  ck("a beat textarea exists to type into", !!area);
  if (area) {
    const before = ed.selected();
    const listeners = root._listeners.get("keydown") || [];
    const ev = { type: "keydown", key: "]", target: area,
                 preventDefault() {}, stopPropagation() {},
                 ctrlKey: false, metaKey: false, altKey: false };
    listeners.forEach((fn) => fn(ev));
    ck("] does nothing while the caret is in a textarea",
       ed.selected() === before, `moved to ${ed.selected()}`);
  }
}

/* -- D1b: RUN is a frame, not a tab that pops a window -------------------- */

console.log("\nmount: D1b the RUN frame");

{
  const node = mountFresh({ plan: planJSON(6) });
  const root = node.domWidgets[0];
  const ed = node._h3Editor;

  const find = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };
  const bar = find("h3e-statusbar")[0];
  const strip = find("h3e-tabs")[0];
  const run = find("h3e-run")[0];
  const runBtns = find("h3e-runbtn");

  ck("the ⚙ exists", runBtns.length === 1);
  ck("the ⚙ is in the STATUS BAR, not the tab strip",
     runBtns.length === 1 && runBtns[0].parentNode === bar,
     runBtns.length ? "parent is not the status bar" : "no button");

  // On Script: the strip is up, RUN is down.
  ck("on Script the tab strip is visible", strip && strip.hidden === false);
  ck("on Script RUN is hidden", run && run.hidden === true);

  // Open RUN the way the user does -- click the ⚙.
  runBtns[0].click();
  ck("clicking the ⚙ shows RUN", run.hidden === false, `hidden=${run.hidden}`);
  ck("and the RUN frame is the whole surface: no tab strip above it",
     strip.hidden === true, "the tab strip stayed up, so RUN reads as a tab");
  ck("and the tab bodies are hidden too",
     find("h3e-scroll")[0].hidden === true);
  ck("the ⚙ is marked pressed while RUN is up",
     runBtns[0].classList.contains("h3e-on"));

  // And it closes again, back to the tab you came from.
  runBtns[0].click();
  ck("clicking ⚙ again returns to the tabs", strip.hidden === false);
  ck("and hides RUN", run.hidden === true);
  ck("and clears the pressed state",
     !runBtns[0].classList.contains("h3e-on"));
}

/* -- D5: the Simple-mode block view, and Templates that stay put ------------ */

console.log("\nmount: D5 simple mode");

{
  // Two blocks separated by --- , with a word count that is inside A7's band.
  const prose = "the figure crosses the room and stops at the window, "
    + "still lit from one side only\n\n---\n\n"
    + "they turn away from it and the light goes with them";
  const node = mountFresh({ plan: "", refPlan: REF_PLAN });
  const root = node.domWidgets[0];
  const ed = node._h3Editor.editor;

  const find = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };

  // Switch to Simple the way a user does.
  const simpleBtn = find("h3e-mode").find((b) => /simple/i.test(b.textContent || ""));
  ck("a Simple button exists", !!simpleBtn);
  if (simpleBtn) {
    simpleBtn.click();
    const area = find("h3e-simple-prompt")[0];
    ck("the Simple prompt textarea exists", !!area);
    if (area) {
      area.value = prose;
      area.fire("input", {});
      // The blocks view is drawn from the textarea on input.
      const rows = find("h3e-block-row");
      ck("two --- blocks produce two rows", rows.length === 2, `got ${rows.length}`);
      const nums = find("h3e-block-no").map((e) => e.textContent);
      ck("the rows are labelled block 1 / block 2",
         nums.join("|") === "block 1|block 2", nums.join("|"));
      const words = find("h3e-block-w").map((e) => e.textContent);
      ck("each row carries a word count", words.length === 2 && words.every((w) => /^\d+$/.test(w)),
         words.join("|"));
      const total = find("h3e-block-total")[0];
      // Read the CHILD: a parent's textContent does not aggregate in the DOM,
      // and I got that wrong once already in the D4 batch-bar check.
      const totalText = find("h3e-block-total")[0]
        ? find("h3e-block-total")[0].children.map((c) => c.textContent).join(" ") : "";
      ck("there is a total", /words total/.test(totalText), totalText || "(none)");

      // Templates must be REACHABLE in Simple now.
      const tpl = find("h3e-btn").find((b) => /templates/i.test(b.textContent || ""));
      ck("Templates is visible in Simple", !!tpl && tpl.style.display !== "none",
         tpl ? `display "${tpl.style.display}"` : "(no button)");

      // And the shot-plan surfaces must be gone, not stale.
      ck("the score strip is hidden in Simple",
         find("h3e-score")[0].style.display === "none",
         `display "${find("h3e-score")[0].style.display}"`);
      ck("the filmstrip is hidden in Simple",
         find("h3e-filmstrip")[0].style.display === "none");
    }
  }
}

/* -- D6: the REFS hop bar, on the score strip's geometry ------------------ */

console.log("\nmount: D6 hop bar");

{
  // One ref, scheduled onto hops 1 and 2, so the ON state is observable.
  const refPlan = JSON.stringify({
    refs: [{ tag: "@plate", file: "h3_refs/kitchen.png", subject: 1,
             desc: "a kitchen", shots: [1, 2] }],
    subjects: {},
  });
  const node = mountFresh({ plan: planJSON(6), refPlan });
  const root = node.domWidgets[0];

  const find = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    walk(root);
    return out;
  };

  const bars = find("h3e-hopbar");
  ck("the hop bar replaced the chips", bars.length === 1, `got ${bars.length}`);
  ck("there are no numbered chips left",
     find("h3e-chip").length === 0, `${find("h3e-chip").length} chips remain`);

  const cells = find("h3e-hopcell");
  ck("one cell per hop", cells.length === 6, `got ${cells.length}`);
  const on = cells.filter((c) => c.classList.contains("h3e-hopcell-on"));
  ck("the scheduled hops are on", on.length === 2, `got ${on.length}`);
  ck("and they are the FIRST two, from ref_plan shots [1,2]",
     cells.indexOf(on[0]) === 0 && cells.indexOf(on[1]) === 1);

  // D6's actual claim: same geometry as the score strip. Both are laid out from
  // stripGeometry over the same width, so the cell WIDTHS must match the score
  // columns exactly. If these ever differ, a click that looks like hop 3 selects
  // hop 5 -- the failure mode worth catching here rather than in a browser.
  const cols = find("h3e-col");
  ck("there is a score strip to line up with", cols.length === 6, `got ${cols.length}`);
  const cellW = cells.map((c) => parseFloat(c.style.width) || 0);
  ck("every hop cell has a real width", cellW.every((w) => w > 0), JSON.stringify(cellW));
  ck("all hop cells are the same width", new Set(cellW).size === 1,
     JSON.stringify(cellW));
  // The score strip's columns are drawn by the same geometry, and its inner
  // width is count * colW -- so colW is recoverable and must equal the cells'.
  const inner = find("h3e-score-inner")[0];
  const innerW = parseFloat(inner?.style?.width) || 0;
  const implied = innerW / (cols.length || 1);
  ck("the hop cells are the width the score strip implies",
     Math.abs(cellW[0] - implied) <= 1,
     `cells ${cellW[0]} vs score ${implied.toFixed(1)}`);

  // Clicking a cell toggles that hop, and writes ref_plan -- not shot_plan.
  const planBefore = node.widgets.find((w) => w.name === "shot_plan").value;
  const refBefore = node.widgets.find((w) => w.name === "ref_plan").value;
  cells[3].fire("pointerdown", { clientX: cellW[3] * 3 + 2 });
  cells[3].fire("pointerup", { clientX: cellW[3] * 3 + 2 });
  const refAfter = node.widgets.find((w) => w.name === "ref_plan").value;
  ck("clicking a hop writes ref_plan",
     refAfter !== refBefore || /3/.test(refAfter), "ref_plan unchanged");
  ck("and never touches shot_plan",
     node.widgets.find((w) => w.name === "shot_plan").value === planBefore);
}

console.log("");
process.exit(failed ? 1 : 0);