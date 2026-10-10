// The shared column geometry, under Node. Pure arithmetic -- no DOM, no ComfyUI.
// Run by tools/check_strip_geometry.py via tools/strip_geometry_test.mjs.
import { stripGeometry, scrollToColumn, columnAt, MIN_COL } from "../js/editor/strip_geometry.js";

let failed = 0;
const ck = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) failed += 1;
};

const W = 560;   // the node's width

console.log("\nstrip geometry");

// --- the columns are always a usable size -------------------------------
for (const n of [1, 2, 6, 12, 19, 40, 97]) {
  const g = stripGeometry(n, W);
  ck(`${n} shots: columns are at least ${MIN_COL}px`,
     g.colW >= MIN_COL, `got ${g.colW} at zoom ${g.zoom}`);
  ck(`${n} shots: ${n} columns`, g.count === n && g.lefts.length === n);
}

// --- auto-zoom ----------------------------------------------------------
ck("6 shots fit without zooming", stripGeometry(6, W).zoom === 1,
   `zoomed to ${stripGeometry(6, W).zoom}`);
const g19 = stripGeometry(19, W);
ck("19 shots auto-zoom past 1x", g19.zoom > 1, `zoom ${g19.zoom}`);
ck("19 shots report that they auto-zoomed", g19.autoZoomed === true);
ck("40 shots clamp at 4x, not beyond", stripGeometry(40, W).zoom === 4,
   `zoom ${stripGeometry(40, W).zoom}`);
// The clamp is a promise: no plan gets columns thinner than the minimum.
ck("97 shots still respect the minimum", stripGeometry(97, W).colW >= MIN_COL);

// --- explicit zoom is honoured, and never shrinks below the minimum ----
ck("zoom 4 is honoured at 6 shots", stripGeometry(6, W, 4).zoom === 4);
ck("zoom 4 at 19 shots stays 4, not 8",
   stripGeometry(19, W, 4).zoom === 4, `got ${stripGeometry(19, W, 4).zoom}`);
ck("lefts are strictly increasing",
   stripGeometry(19, W).lefts.every((v, i, a) => i === 0 || v > a[i - 1]));
ck("lefts start at 0", stripGeometry(19, W).lefts[0] === 0);

// --- width accounting ---------------------------------------------------
const g = stripGeometry(6, W, 1);
ck("width is colW * count", g.width === g.colW * 6);
ck("no scroll when it fits", g.scroll === 0, `scroll ${g.scroll}`);
const gz = stripGeometry(19, W);
ck("scroll appears once the strip exceeds the node",
   gz.scroll === gz.width - W, `${gz.scroll} vs ${gz.width - W}`);
ck("last column's right edge is exactly the width",
   gz.lefts[gz.count - 1] + gz.colW === gz.width);

// --- an empty plan is not a divide by zero -----------------------------
const e = stripGeometry(0, W);
ck("0 shots: no columns and no crash", e.count === 0 && e.colW === 0);

// --- click -> column ----------------------------------------------------
ck("columnAt at x=0 is column 0", columnAt(g, 0) === 0);
ck("columnAt past the end is -1", columnAt(g, g.width + 10) === -1);
ck("columnAt on the last pixel is the last column",
   columnAt(g, g.width - 1) === g.count - 1);
ck("columnAt respects the scroll offset",
   columnAt(gz, 10, gz.colW * 3) === 3, `got ${columnAt(gz, 10, gz.colW * 3)}`);

// --- scrollToColumn keeps the neighbours --------------------------------
// The reason this is not "centre it": with 19 columns and a viewport of a few,
// centring on a click throws away the context you clicked in.
{
  const geo = stripGeometry(19, W);
  const view = W;
  ck("a column already in view is not scrolled",
     scrollToColumn(geo, 0, 0, view) === 0);
  const s3 = scrollToColumn(geo, 3, 0, view);
  ck("column 3 with scroll 0 scrolls forward", s3 === 0, `got ${s3}`);
  // Scroll far right, then ask for an early column: it must come back.
  const back = scrollToColumn(geo, 1, geo.lefts[15], view);
  ck("asking for an early column from far right scrolls back",
     back < geo.lefts[15], `got ${back}`);
  const off = scrollToColumn(geo, 15, 0, view);
  ck("a far-right column scrolls just enough to show it",
     off > 0 && off + view >= geo.lefts[15] + geo.colW, `got ${off}`);
}

console.log("");
process.exit(failed ? 1 : 0);