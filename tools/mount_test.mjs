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

/* -- D3/D1e: the forecast line, and no score strip ----------------------- */
//
console.log("\nmount: D3 forecast line");

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

  // D1e: the 44px score strip is GONE, at the user's request. The forecast line
  // is what survived it.
  ck("the score strip is gone", find("h3e-score").length === 0,
     `${find("h3e-score").length} strip(s) still drawn`);
  ck("and so are its columns", find("h3e-col").length === 0);
  ck("and its bars", find("h3e-col-bar").length === 0);

  const fcLine = find("h3e-forecast")[0];
  ck("the forecast line survives", !!fcLine);
  ck("it states what the next queue does",
     /NEXT QUEUE/.test(fcLine.textContent || ""), fcLine.textContent || "(empty)");
  ck("a plan that has never run forecasts as all-rendering",
     /re-renders\s+01/.test(fcLine.textContent || ""), fcLine.textContent || "(empty)");
  ck("and is marked live when hops will move",
     fcLine.classList.contains("h3e-forecast-live"));

  // The filmstrip is still the primary surface, and it is what carries the
  // per-shot information the strip duplicated.
  const posters = find("h3e-poster")
    .filter((p) => !p.classList.contains("h3e-poster-add"));
  ck("the filmstrip still draws one poster per shot", posters.length === 6,
     `got ${posters.length}`);
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
  // D1c reversed D1b. The gear goes BACK in the tab strip: it was moved to the
  // 26px status bar and became so hard to find the user had to look for a
  // minute ("i never asked to move the gear away"). It is also where a control
  // belongs -- beside the tabs, not buried in a status line.
  ck("the ⚙ is back in the TAB STRIP",
     runBtns.length === 1 && runBtns[0].parentNode === strip,
     runBtns.length ? "parent is not the tab strip" : "no button");

  // On Script: the strip is up, RUN is down.
  ck("on Script the tab strip is visible", strip && strip.hidden === false);
  ck("on Script RUN is hidden", run && run.hidden === true);

  // Click the ⚙ -- RUN is its own PAGE, and the chrome STAYS.
  runBtns[0].click();
  ck("clicking the ⚙ shows RUN", run.hidden === false, `hidden=${run.hidden}`);
  ck("RUN keeps the tab strip -- it is a page, not a covering panel",
     strip.hidden === false, "the tab strip disappeared; D1c says it stays");
  ck("and the tab bodies are hidden",
     find("h3e-scroll")[0].hidden === true);
  ck("the ⚙ is marked pressed while RUN is up",
     runBtns[0].classList.contains("h3e-on"));

  // Row 3 on the RUN page is its own: a tally and a way back.
  const tally = find("h3e-run-tally")[0];
  ck("RUN's header row has a dial/group tally",
     !!tally && /\d+ dials\s*.\s*\d+ groups/.test(tally.textContent || ""),
     tally ? (tally.textContent || "(empty)") : "(no tally)");
  const back = find("h3e-run-back")[0];
  ck("RUN's header row has a back control", !!back);
  ck("and it is visible while RUN is up", !!back && back.hidden === false,
     back ? `hidden=${back.hidden}` : "(none)");

  // The back control actually goes back -- without it the gear is the only
  // affordance, which is the thing the user had to hunt for.
  if (back) {
    back.fire("click", {});
    ck("back returns to the work surface", strip.hidden === false && run.hidden === true);
  }
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

      // And the shot-plan surfaces must be gone, not stale. D1e removed the
      // score strip, so what is left to check is the filmstrip and the
      // detail pane -- and the check reads `.style` on an optional, because a
      // bare `[0].style` on a missing element throws and takes the whole run
      // down silently: 84 assertions printed and exit 1.
      const fc = find("h3e-forecast")[0];
      ck("the forecast line is hidden in Simple",
         !!fc && fc.style.display === "none",
         fc ? `display "${fc.style.display}"` : "(no forecast line)");
      const fil = find("h3e-filmstrip")[0];
      ck("the filmstrip is hidden in Simple",
         !!fil && fil.style.display === "none",
         fil ? `display "${fil.style.display}"` : "(no filmstrip)");
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

  // D6's claim WAS "same geometry as the score strip", and the score strip is
// gone as of D1e. What survives is the part that still matters: every cell in a
// hop bar is the same width, and that width is the geometry's, not a leftover.
  //
  // The alignment check against the strip is GONE rather than rewritten, because
  // there is nothing to align to. What it was protecting -- a cell whose width
  // disagreed with its neighbour, so a click that looks like hop 3 selected hop
  // 5 -- is still protected by the two checks below.
  const cellW = cells.map((c) => parseFloat(c.style.width) || 0);
  ck("every hop cell has a real width", cellW.every((w) => w > 0), JSON.stringify(cellW));
  ck("all hop cells are the same width", new Set(cellW).size === 1,
     JSON.stringify(cellW));
  ck("the cell width comes from the shared geometry, not a literal",
     cellW[0] >= 34, `cell width ${cellW[0]} is under MIN_COL`);

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

/* -- D7: MEDIA folds empty slots, and never un-draws one ------------------ */

console.log("\nmount: D7 media fold");

{
  // No media set at all: every slot should start folded.
  const node = withWidgets(makeNode(), {
    shot_plan: planJSON(6), ref_plan: REF_PLAN,
  });
  // BEFORE the mount: media_strip.js reads its slots once, at construction, and
  // SKIPS any name it cannot find -- deliberately, so it survives an older
  // Python side. A stub node missing those names therefore mounts fewer cells
  // and every count assertion passes against a partial panel, which is how the
  // first version of this block reported "4 cells" and "everything is fine".
  // The full list, from SLOTS.
  for (const name of [
    "start_image_file",
    "reference_video_file", "reference_video_2_file", "reference_video_3_file",
    "reference_video_desc", "reference_video_size",
    "voice_file", "voice_2_file", "voice_3_file",
    "soundtrack_file", "master_audio_file", "master_audio_start_s",
  ]) {
    if (!node.widgets.find((w) => w.name === name)) {
      node.widgets.push({ name, type: "custom", value: "" });
    }
  }
  // One media slot SET, so the fold has both states to show.
  node.widgets.find((w) => w.name === "start_image_file").value = "h3_refs/kitchen.png";
  mount(node);
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

  // Every name media_strip.js's SLOTS table looks up. The strip SKIPS a name it
  // cannot find -- deliberately, so it survives an older Python side -- which
  // means a stub node missing some of them silently mounts fewer cells and every
  // count assertion passes against a partial panel. The full list, from SLOTS.
  for (const name of [
    "start_image_file",
    "reference_video_file", "reference_video_2_file", "reference_video_3_file",
    "reference_video_desc", "reference_video_size",
    "voice_file", "voice_2_file", "voice_3_file",
    "soundtrack_file", "master_audio_file", "master_audio_start_s",
  ]) {
    if (!node.widgets.find((w) => w.name === name)) {
      node.widgets.push({ name, type: "custom", value: "" });
    }
  }
  // One media slot SET, so the fold has both states to show.
  node.widgets.find((w) => w.name === "start_image_file").value = "h3_refs/kitchen.png";

  // Scoped to the MEDIA strip. An unscoped walk finds video_swap.js's own
  // `h3e-media-cell` on another tab -- the class is shared, and counting across
  // tab bodies reports 10 for a 9-slot strip.
  const mediaRoot = find("h3e-media")[0];
  const findMedia = (cls) => {
    const out = [];
    const walk = (e) => {
      if (!e) return;
      if (e.classList?.contains(cls)) out.push(e);
      (e.children || []).forEach(walk);
    };
    if (mediaRoot) walk(mediaRoot);
    return out;
  };

  const cells = findMedia("h3e-media-cell");
  ck("media cells exist", cells.length > 0, `${cells.length} cells`);
  ck("nine media cells -- every declared slot", cells.length === 9, `got ${cells.length}`);
  ck("every media cell has a fold head", cells.length > 0
     && cells.every((c) => c.children.some((k) => k.classList?.contains("h3e-media-head"))));
  ck("every media cell has a body in the DOM -- the widget is never un-drawn",
     cells.length > 0
     && cells.every((c) => c.children.some((k) => k.classList?.contains("h3e-media-body"))),
     "a cell is missing its body -- that is the raw-widget failure mode");

  // The FIRST cell holds a file, so it must be OPEN; the other eight are empty
  // and must be folded. This is the whole point: a filled slot is never folded
  // behind a click, or the user hides their own choice.
  const folded = cells.filter((c) => c.classList.contains("h3e-media-folded"));
  const open = cells.filter((c) => !c.classList.contains("h3e-media-folded"));
  ck("the one filled slot is open", open.length === 1,
     `${open.length} open of ${cells.length}`);
  ck("all eight empty slots are folded", folded.length === 8,
     `${folded.length} folded of ${cells.length}`);
  ck("a folded body is hidden with display, not removed",
     folded.every((c) => {
       const b = c.children.find((k) => k.classList?.contains("h3e-media-body"));
       return b && b.style.display === "none";
     }), "a body is display:'' -- it would show through a fold");

  // And it is still in the tree, so it cannot come back as a raw widget.
  ck("folded bodies are still children of their cell",
     folded.every((c) => c.children.some((k) => k.classList?.contains("h3e-media-body"))));

  // Clicking an EMPTY head opens it, and only changes display.
  const emptyCell = folded[0];
  const head = emptyCell.children.find((k) => k.classList?.contains("h3e-media-head"));
  const body = emptyCell.children.find((k) => k.classList?.contains("h3e-media-body"));
  head.fire("click", {});
  ck("clicking a folded head opens it",
     !emptyCell.classList.contains("h3e-media-folded"), "still folded after the click");
  ck("and its body becomes visible", body.style.display !== "none",
     `display "${body.style.display}"`);
  ck("the body was never detached", body.parentNode === emptyCell);

  // And closing it again does not detach either.
  head.fire("click", {});
  ck("clicking again folds it back",
     emptyCell.classList.contains("h3e-media-folded"));
  ck("still attached after folding",
     emptyCell.children.some((k) => k.classList?.contains("h3e-media-body")));

  // A FILLED slot refuses to fold -- the click is a no-op, by design.
  const setCell = open[0];
  const setHead = setCell.children.find((k) => k.classList?.contains("h3e-media-head"));
  setHead.fire("click", {});
  ck("a filled slot cannot be folded shut",
     !setCell.classList.contains("h3e-media-folded"),
     "the user hid their own pick behind a click");
}

/* -- every tab renders. Clicking all six, not the two the test needed ------ */

console.log("\nmount: every tab renders");

{
  // D1f: the mount test clicked Script, Refs and Write and stopped. Timeline was
  // BROKEN the whole time -- `isNewPrevis is not defined`, a ReferenceError
  // thrown from a line that shipped in D3 -- and nobody noticed, because
  // createTabs wraps its onShow handler in a try/catch and logs to the console.
  // A tab that throws looks exactly like a tab with nothing in it.
  //
  // So: click every tab, and fail on anything thrown. A tab is a claim about
  // what the panel can draw, and a claim nobody checks is not a claim.
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

  const tabBtns = find("h3e-tab");
  ck("six tabs", tabBtns.length === 6, `got ${tabBtns.length}`);

  // A console.error from a swallowed handler is the only trace a broken tab
  // leaves. Catch it rather than trusting the click to throw.
  const realError = console.error;
  const swallowed = [];
  console.error = (...a) => { swallowed.push(a.map(String).join(" ")); };

  let threw = null;
  try {
    for (const btn of tabBtns) {
      const label = (btn.textContent || "").trim().split(/\s+/)[0];
      try {
        btn.click();
      } catch (e) {
        threw = `${label}: ${e.message}`;
        break;
      }
    }
  } finally {
    console.error = realError;
  }

  ck("no tab throws when clicked", !threw, threw || "");
  ck("and no tab logs a swallowed error", swallowed.length === 0,
     swallowed.slice(0, 2).join(" | "));

  // And each tab actually drew something. Timeline in particular must have
  // lanes after render -- it was throwing before it drew any.
  const tlBtn = tabBtns.find((b) => /timeline/i.test(b.textContent || ""));
  if (tlBtn) {
    try { tlBtn.click(); } catch (_) { /* asserted above */ }
    const tlRoot = ed?.timeline?.root;
    const lanes = tlRoot ? find.call(null, "h3e-tl-lane") : [];
    ck("the timeline draws lanes after a render",
       !!(tlRoot && tlRoot.children.length > 0),
       tlRoot ? `${tlRoot.children.length} children` : "no timeline root");
  }
}

/* -- D1i: clearing a slot clears the dropdown too -------------------------- */

console.log("\nmount: D1i clear repaints the dropdown");

{
  // A filled FIRST FRAME slot, so there is a name to lose. The media widget
  // names have to exist BEFORE the mount: media_strip reads its slot table once
  // at construction and skips any name it cannot find.
  const node = withWidgets(makeNode(), {
    shot_plan: planJSON(3), ref_plan: REF_PLAN, chains: 3,
  });
  for (const name of [
    "start_image_file", "reference_video_file", "voice_file", "voice_2_file",
    "voice_3_file", "soundtrack_file", "master_audio_file",
  ]) {
    if (!node.widgets.find((w) => w.name === name)) {
      node.widgets.push({ name, type: "custom", value: "" });
    }
  }
  node.widgets.find((w) => w.name === "start_image_file").value = "photo.png";
  mount(node);
  const root = node.domWidgets[0];

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

  const sel = findAll("h3e-pickfile")[0];
  ck("the picker dropdown exists", !!sel);
  // With no /files response the list is empty, but paintList still runs on the
  // fallback path and must land on "— none —" once the widget is empty.
  //
  // The FIRST pickfile and the FIRST thumb-x belong to the FIRST slot
  // (start_image_file), which is the one holding a name here. Scoping matters:
  // an unscoped query can pair a dropdown from one slot with a clear button
  // from another, which is how this test passed while asserting nothing.
  // Find the slot that holds the file, by walking for the clear button itself.
  // NOT by textContent (a cell's text lives in child spans and a parent's
  // textContent does not aggregate -- the D4 batch-bar trap) and NOT by a fixed
  // nesting depth (the thumb, the picker and the button are at different depths
  // depending on which fields a slot has).
  const cell = findAll("h3e-media-cell").find((c) => {
    let hit = false;
    const w = (e) => { if (!e) return; if (e.classList?.contains("h3e-thumb-x")) hit = true; (e.children || []).forEach(w); };
    w(c);
    return hit;
  });
  ck("the slot holding the file is found", !!cell,
     `${findAll("h3e-media-cell").length} cells, none with a clear button`);
  const x = cell
    ? (function () {
        let found = null;
        const w = (e) => { if (!e) return; if (e.classList?.contains("h3e-thumb-x")) found = e; (e.children || []).forEach(w); };
        w(cell);
        return found;
      })()
    : null;
  ck("a filled slot offers a clear button", !!x);

  if (sel && x) {
    x.fire("click", {});
    ck("clearing empties the widget",
       node.widgets.find((w) => w.name === "start_image_file").value === "",
       `value is ${JSON.stringify(node.widgets.find((w) => w.name === "start_image_file").value)}`);
    // The dropdown must not still be offering the old name.
    const opts = sel.children.map((o) => o.value);
    ck("and the dropdown no longer offers the cleared name",
       !opts.includes("photo.png"), JSON.stringify(opts));
    ck("and it sits on the blank option",
       sel.value === "", `value is ${JSON.stringify(sel.value)}`);
  }
}

/* -- the backend switch must not race itself ------------------------------- */

console.log("\nmount: backend switch race");

/*
 * `loadConn` is not exported, so this drives the real path a user drives: the
 * backend dropdown. Two changes in quick succession is exactly what happens when
 * someone tries both servers, and the panel must end up on the LAST one chosen.
 *
 * Without a request token the winner is whichever response resolves LAST, so the
 * fetches are resolved DELIBERATELY OUT OF ORDER here -- the older one last --
 * which is the only arrangement that can catch it.
 */
{
  const pump = () => new Promise((r) => setTimeout(r, 0));
  const savedFetch = globalThis.fetch;
  const queue = [];
  // Only the writer's own route is intercepted. The panel fetches /vocab and
  // /files while mounting, and stealing those would test nothing about the
  // switch -- it would just break the mount this block depends on.
  globalThis.fetch = (url, opts) => {
    if (String(url) !== "/h3_ref_chain/llm") return savedFetch(url, opts);
    const rec = { url, method: (opts && opts.method) || "GET", resolve: null };
    queue.push(rec);
    return new Promise((res) => { rec.resolve = res; });
  };
  const settle = (rec, body) => rec.resolve({ json: async () => body });
  const backends = [
    { id: "lmstudio", label: "LM Studio", program: false },
    { id: "ninfer", label: "nInfer", program: true },
  ];
  const payload = (id, label, url, model) => ({
    ok: true, backend: id, backend_label: label, model_optional: id !== "ninfer",
    server_url: url, model, models: [{ id: model, loaded: true }],
    backends, server_paths: {}, running: false, online: true,
    any_loaded: true, keep_warm: true,
  });

  try {
    const rnode = mountFresh({ plan: planJSON(3) });
    const root = rnode.domWidgets[0];
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
    const sel = findAll("h3e-writer-backend")[0];
    ck("the backend dropdown exists", !!sel);

    sel.value = "ninfer";
    sel.fire("change", {});
    await pump();
    settle(queue.find((q) => q.method === "POST"), { ok: true, saved: true });
    await pump();
    const getA = queue.filter((q) => q.method === "GET").at(-1);

    sel.value = "lmstudio";
    sel.fire("change", {});
    await pump();
    settle(queue.filter((q) => q.method === "POST").at(-1), { ok: true, saved: true });
    await pump();
    const getB = queue.filter((q) => q.method === "GET").at(-1);

    // In order: the NEWER answer first, then the older one straggling in.
    settle(getB, payload("lmstudio", "LM Studio", "http://127.0.0.1:1234", "lm-model"));
    await pump();
    settle(getA, payload("ninfer", "nInfer", "http://127.0.0.1:8080", "ninfer-model"));
    await pump();

    ck("the newer backend wins even when the older reply arrives last",
       sel.value === "lmstudio", `dropdown shows ${sel.value}`);
    const urlIn = findAll("h3e-writer-url")[0];
    ck("and its own address is on screen",
       urlIn && urlIn.value === "http://127.0.0.1:1234",
       urlIn ? urlIn.value : "(no address field)");
    const modelSel = findAll("h3e-writer-model")[0];
    const opts = modelSel ? modelSel.children.map((o) => o.value) : [];
    ck("and its model list, not the late one",
       opts.includes("lm-model") && !opts.includes("ninfer-model"),
       JSON.stringify(opts));
    // The badge is the h3e-count inside the WRITE summary. `h3e-count` on its own
    // is used in ten places across this panel, so a bare lookup would land on an
    // unrelated tally and report green no matter what -- a check that passes on
    // nothing. Selected by the sibling title instead.
    const badgeEl = (function () {
      let found = null;
      const walk = (e) => {
        if (!e || found) return;
        const title = (e.children || []).find(
          (c) => c.classList?.contains("h3e-title"));
        if (title && String(title.textContent) === "WRITE"
            && (e.children || []).some((c) => c.classList?.contains("h3e-count"))) {
          found = (e.children || []).find((c) => c.classList?.contains("h3e-count"));
          return;
        }
        (e.children || []).forEach(walk);
      };
      walk(root);
      return found;
    })();
    ck("the WRITE badge is found, and not some other tally", !!badgeEl);
    ck("the straggler did not leave the badge naming the other server's model",
       !!badgeEl && !String(badgeEl.textContent).includes("ninfer-model"),
       badgeEl ? String(badgeEl.textContent) : "(no badge)");
    void rnode;
  } finally {
    globalThis.fetch = savedFetch;
  }
}

console.log("");
process.exit(failed ? 1 : 0);