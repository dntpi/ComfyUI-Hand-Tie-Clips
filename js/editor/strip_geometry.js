/**
 * One geometry for three surfaces: the score strip (D3), the filmstrip (D2) and
 * the REFS hop bar (D6).
 *
 * Why this file exists rather than a `colW` computed in each place: the brief
 * wants the score strip and the filmstrip ALIGNED, and D6 wants the REFS hop
 * bar on "the score strip's geometry (same columns, so the two line up)". Three
 * places computing `nodeWidth / n` independently agree exactly until they
 * don't -- a gap here, a rounding difference there -- and the failure is visual
 * and obvious to the user and invisible to every check. So the columns are
 * defined once, here, and all three read them.
 *
 * The rule: columns are equal width, `width / n`, because a score strip that
 * sized columns by DURATION would duplicate the timeline's job and would not
 * line up with anything. Duration is encoded as column HEIGHT (D3), which is
 * the axis that does not have to agree with anything else.
 */

/** Below this a column cannot show a slate number and still be a target. */
export const MIN_COL = 34;

/** Zoom steps. 1 fits the whole plan; 2 and 4 window it. */
export const ZOOMS = [1, 2, 4];

/**
 * The column layout for `n` shots at `width` px and zoom `z`.
 *
 * Returns absolute pixel geometry so callers can position with `left`/`width`
 * and never re-derive anything. `scroll` is how many pixels the strip needs
 * beyond the viewport, which is what the scrollbar reads.
 *
 * `autoZoom` is the interesting one: a 19-shot plan on a 560px node gives
 * 29px columns, under MIN_COL, so it opens zoomed rather than handing back a
 * grid of slivers. It clamps at 4x and stops there -- past that the user is
 * reading one column at a time, which is what clicking a column is for.
 */
export function stripGeometry(n, width, zoom = 1, { autoZoom = true } = {}) {
  const count = Math.max(0, n | 0);
  const view = Math.max(MIN_COL, width | 0);
  if (!count) {
    return { count: 0, colW: 0, zoom: 1, lefts: [], width: 0, scroll: 0, autoZoomed: false };
  }
  const fit = view / count;
  let z = Math.max(1, Number(zoom) || 1);
  let autoZoomed = false;
  if (autoZoom && fit * z < MIN_COL) {
    const need = Math.ceil(MIN_COL / fit);
    const target = ZOOMS.find((s) => s >= need) || ZOOMS[ZOOMS.length - 1];
    if (target > z) { z = target; autoZoomed = true; }
  }
  const colW = Math.max(MIN_COL, Math.round(fit * z));
  const total = colW * count;
  const lefts = [];
  for (let i = 0; i < count; i++) lefts.push(i * colW);
  return {
    count, colW, zoom: z, lefts,
    width: total,
    scroll: Math.max(0, total - view),
    autoZoomed,
  };
}

/**
 * Scroll so column `i` is visible, given a current scrollLeft.
 *
 * Deliberately not "centre it": with 19 columns and a viewport of 4, centring
 * on click loses the neighbours, and the neighbours are the context for why
 * you clicked. This brings it just into view, and returns 0 when it already is.
 */
export function scrollToColumn(geo, i, scrollLeft, view) {
  if (!geo || !geo.count) return 0;
  const idx = Math.max(0, Math.min(geo.count - 1, i | 0));
  const left = geo.lefts[idx];
  const right = left + geo.colW;
  const v = Math.max(1, view | 0);
  if (left < scrollLeft) return left;
  if (right > scrollLeft + v) return right - v;
  return Math.max(0, scrollLeft);
}

/**
 * Which column a pointer x lands in. Returns -1 outside the strip.
 *
 * D6's hop bar needs this to turn a click into a shot number without counting
 * pixels itself, and D3 needs it for click-to-select. Shared for the same
 * reason the geometry is.
 */
export function columnAt(geo, x, scrollLeft = 0) {
  if (!geo || !geo.count || !geo.colW) return -1;
  const px = x + Math.max(0, scrollLeft);
  if (px < 0 || px >= geo.width) return -1;
  return Math.min(geo.count - 1, Math.floor(px / geo.colW));
}
