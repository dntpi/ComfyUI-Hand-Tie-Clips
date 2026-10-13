/**
 * The timeline's forecast: what the next queue would re-render, as a PREVIEW.
 *
 * Pure -- no DOM, no ComfyUI import -- so `tools/check_timeline.py` runs it
 * under Node. It does not compute cache keys and must not try: a key needs the
 * patched model, the assembled prompt and the pin mechanism picked at runtime.
 * It compares what was queued last (the snapshot) with what is set now, and
 * applies the three facts the key's shape guarantees:
 *
 *   - a hop's key chains from the previous hop's, so a change runs on;
 *   - a restart keys from nothing, so a change stops before it;
 *   - a locked hop still in the cache is served by pointer, so it never
 *     re-renders and the hop after it keys from that unchanged render.
 *
 * pin_mech=hand_tie does not chain hops, but its windows chain by picture. A
 * hop's key carries its opening window and its own end window; a
 * continuation's window cites the previous window's still, a restart's cites
 * still 1. So a content edit moves that hop's window and every window after it
 * up to the next restart, and hop 1's runs through restarts too. A shot seed
 * retakes that hop alone. The run seed is every window's seed, so it
 * re-renders the film.
 *
 * Anything this cannot see -- a LoRA swapped on the MODEL wire, a reference
 * file replaced on disk under the same name -- the run itself will catch.
 */

/* DURATION_FRAMES as a formula (exact for all 26 labels). */
export function framesOf(label) {
    const s = parseFloat(String(label || ""));
    return Number.isFinite(s) ? 17 * Math.round((24 * s - 5) / 17) + 5 : 192;
}
/* Widgets whose value moves no hop key. Everything else that changes between
 * the last run and now is read as a chain-wide change. */
export const NOT_KEYED = new Set([
    "shot_plan", "ref_plan", "render_from", "render_through", "dry_run",
    "cache_hops", "cache_budget_gb", "contact_sheet", "control_after_generate",
    "prompt", "chains", "hop_script",
]);

/* Keyed on every hop that continues another, never on a chain start. */
export const CONTINUE_ONLY = new Set([
    "overlap", "pin_to_qwen", "pin_mech", "audio_pin_frames", "pin_renorm",
    "pin_noise",
]);

/** The shot as it reaches the key, without the fields that do not move it. */
export function shotSig(shot) {
    const { locked, id, ...rest } = shot || {};
    return JSON.stringify(rest);
}

/** The shot without its seed. A hand-tie window is seeded from the run, so a
 * shot seed retakes the hop and leaves the window. */
export function contentSig(shot) {
    const { locked, id, seed, ...rest } = shot || {};
    return JSON.stringify(rest);
}

export function parseJson(text) {
    try {
        const v = JSON.parse(String(text || "").trim() || "null");
        return v && typeof v === "object" ? v : null;
    } catch {
        return null;
    }
}

/**
 * @param shots    the live shot list
 * @param rec      the last run record: {snapshot, hops: [{key, status, ...}]}
 * @param now      a snapshot of what is set now (same shape as rec.snapshot)
 * @param range    {from, through} -- render_from / render_through
 * @param missing  Set of keys the cache no longer holds
 * @param seedCtl  control_after_generate: "fixed" | "increment" | ...
 * @param refsNow  the ref_plan rows now
 * @returns {state[], cause[], unpaired[]} -- per hop; `unpaired` holds the
 *          0-based index of each hop whose join with the one before would be
 *          one nobody generated.
 */
export function forecastPlan({ shots, rec, now, range = {}, missing = new Set(),
                               seedCtl = "fixed", refsNow = [] }) {
    const snap = rec?.snapshot || null;
    const n = shots.length;
    const handTie = now?.widgets?.pin_mech === "hand_tie";
    const isStart = (i) => i === 0 || shots[i]?.anchor === "restart";
    const why = new Map();   // hop index -> reason it moves on its own
    const contentChanged = [];
    const note = (i, text, kind = "content") => {
        if (!why.has(i)) why.set(i, text);
        if (kind !== "seed") contentChanged[i] = true;
    };

    let seedMoves = false;
    if (!rec) {
        for (let i = 0; i < n; i++) note(i, "never rendered");
    } else {
        let chainWide = null;
        let continueWide = null;
        if (snap && now) {
            for (const [name, value] of Object.entries(now.widgets || {})) {
                if (snap.widgets?.[name] === value) continue;
                if (CONTINUE_ONLY.has(name)) continueWide ??= name;
                else chainWide ??= name;
            }
        }
        seedMoves = seedCtl !== "fixed" || Boolean(snap && now && snap.seed !== now.seed);
        const changedRefs = snap && now
            ? Object.keys({ ...snap.refs, ...now.refs })
                .filter((t) => snap.refs?.[t] !== now.refs?.[t])
            : [];
        const refsThen = snap ? Object.values(snap.refs || {}).map(parseJson) : [];
        const ridesHops = (tag) => {
            const out = new Set();
            for (const r of [...refsNow, ...refsThen]) {
                if (!r || r.tag !== tag) continue;
                const list = Array.isArray(r.shots) && r.shots.length ? r.shots : [1];
                list.forEach((h) => out.add(Number(h) - 1));
            }
            shots.forEach((s, i) => {
                if (Array.isArray(s.refs) && s.refs.includes(tag)) out.add(i);
            });
            return out;
        };
        for (let i = 0; i < n; i++) {
            const s = shots[i];
            const id = String(s.id || "");
            const h = rec.hops?.[i];
            if (!h?.key) { note(i, "not in the last run"); continue; }
            if (missing.has(h.key)) { note(i, "gone from the cache"); continue; }
            if (chainWide) { note(i, `${chainWide} changed`); continue; }
            if (continueWide && !isStart(i)) { note(i, `${continueWide} changed`); continue; }
            if (!snap) continue;
            if (snap.ids?.[i] !== id) { note(i, "shots reordered"); continue; }
            if (handTie && snap.content) {
                if (snap.content[id] !== contentSig(s)) {
                    note(i, `shot ${i + 1} edited`);
                    continue;
                }
                if (snap.shots?.[id] !== shotSig(s)) {
                    note(i, `shot ${i + 1} seed changed`, "seed");
                    continue;
                }
            } else if (snap.shots?.[id] !== shotSig(s)) {
                // An old hand-tie snapshot has no content map. Any shot change
                // counts as content, so a seed retake is over-invalidated.
                note(i, `shot ${i + 1} edited`);
                continue;
            }
            if (seedMoves && (handTie || s.seed == null || s.seed === "")) {
                note(i, seedCtl !== "fixed"
                    ? `seed is set to ${seedCtl} each queue`
                    : "seed changed",
                    handTie ? "content" : "seed");
                continue;
            }
            if (isStart(i) && now && snap.subjects !== now.subjects) {
                note(i, "subjects edited");
                continue;
            }
        }
        for (const tag of changedRefs) {
            for (const i of ridesHops(tag)) if (i < n) note(i, `@${tag} changed`);
        }
    }

    // hand_tie: which windows move, and the edit that moves each.
    const windowMoved = [];
    const windowCause = [];
    for (let i = 0; i < n; i++) {
        const src = i === 0 ? -1 : (isStart(i) ? 0 : i - 1);
        if (contentChanged[i]) {
            windowMoved.push(true);
            windowCause.push(why.get(i));
        } else if (src >= 0 && windowMoved[src]) {
            windowMoved.push(true);
            windowCause.push(windowCause[src]);
        } else {
            windowMoved.push(false);
            windowCause.push(null);
        }
    }

    const { from = 0, through = 0 } = range;
    const state = [];
    const moved = [];
    const cause = [];
    for (let i = 0; i < n; i++) {
        const h = rec?.hops?.[i];
        const servedLocked = Boolean(shots[i]?.locked && h?.key && !missing.has(h.key));
        let m = false;
        let c = null;
        if (servedLocked) {
            m = false;
        } else if (why.has(i)) {
            m = true;
            c = why.get(i);
        } else if (handTie && windowMoved[i]) {
            m = true;
            c = windowCause[i];
        } else if (!handTie && i > 0 && moved[i - 1] && !isStart(i)) {
            m = true;
            c = cause[i - 1];
        }
        moved.push(m);
        cause.push(c);
        let st = servedLocked ? "locked" : (m ? "render" : "keeps");
        if (through && i + 1 > through) st = "skip";
        else if (from && i + 1 < from) st = m ? "replay-miss" : "replay";
        state.push(st);
    }
    const unpaired = new Set();
    for (let i = 1; i < n; i++) {
        if (state[i] !== "locked" || isStart(i) || state[i - 1] === "skip") continue;
        const openingMoved = handTie ? windowMoved[i - 1] : moved[i - 1];
        if (openingMoved) unpaired.add(i);
    }
    // hand_tie: a locked hop's frames end on the window it was rendered with.
    // When that window moves, the next hop opens on a different one.
    if (handTie) {
        for (let i = 0; i + 1 < n; i++) {
            if (state[i] !== "locked" || !windowMoved[i] || isStart(i + 1)
                || state[i + 1] === "skip") continue;
            unpaired.add(i + 1);
        }
    }
    return { state, cause, unpaired: [...unpaired].sort((a, b) => a - b) };
}
