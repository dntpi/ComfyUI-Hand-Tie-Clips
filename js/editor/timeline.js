/**
 * TIMELINE -- a retake board over the hop cache.
 *
 * One block per shot on a real time ruler, the cached render of each hop as a
 * scrubbable film cell, one mark per join, and a forecast of what the next
 * queue would re-render. Clicking a film cell plays that hop; clicking a join
 * plays the last two seconds of one hop into the first two of the next, trimmed
 * the way the master is.
 *
 * Three rules this file keeps, and why:
 *
 * - **It reports what a run DID, and never re-derives a cache key.** A hop's
 *   key needs the patched model, the fully assembled prompt and the pin
 *   mechanism chosen at runtime; none of that exists in the browser. So the
 *   key, the status and the geometry of each hop arrive on the chain's own
 *   `h3refchain_preview` events (`timeline` in the payload) and are kept in
 *   `node.properties`. The forecast is a preview built from what was edited
 *   since that run, and it says so.
 * - **`shot_plan` stays the only source of truth.** Solo and lock go through
 *   the plan editor's own `toggleSolo` / `toggleLock`, the functions the shot
 *   cards call; this tab owns no widget and keeps no parallel plan.
 * - **Geometry comes from `editor.timing()`,** the one place that knows a
 *   restart is charged no overlap trim.
 *
 * The cache lives in ComfyUI's temp folder and is wiped on every start, so an
 * empty film row after a restart is the ordinary state, and it says so.
 */
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { el, button, widgetByName, findNodeByQualifiedId } from "./widget_utils.js";
import { NOT_KEYED, forecastPlan, parseJson, shotSig } from "./timeline_model.js";

const RUN_PROP = "h3_timeline_run";
const PENDING_PROP = "h3_timeline_pending";
const ZOOM_PROP = "h3_timeline_pps";
const ZOOMS = [6, 12, 24];
const SPRITE_N = 24;
const FILM_H = 54;

const SPRITE_URL = "/h3_ref_chain/hop_sprite";
const CLIP_URL = "/h3_ref_chain/hop_clip";
const SEAM_URL = "/h3_ref_chain/seam_clip";


function clock(sec) {
    if (!Number.isFinite(sec) || sec < 0) return "—";
    return `${sec.toFixed(1)}s`;
}

export function createTimeline(node, { editor, onChange } = {}) {
    const root = el("div", "h3e-section h3e-tl");
    const head = el("div", "h3e-tl-head");
    const zoomBox = el("span", "h3e-tl-zoom");
    const status = el("span", "h3e-tl-status");
    head.appendChild(zoomBox);
    head.appendChild(status);
    root.appendChild(head);

    const scroller = el("div", "h3e-tl-scroll");
    root.appendChild(scroller);
    const forecastNote = el("div", "h3e-note h3e-note-hint h3e-tl-why");
    root.appendChild(forecastNote);
    const warnings = el("div", "h3e-tl-warn");
    root.appendChild(warnings);
    // The selected shot's SCRIPT card, in place. The same builder SCRIPT uses
    // (`editor.cardFor`), so an edit here is an edit there.
    const inspector = el("div", "h3e-tl-inspector");
    root.appendChild(inspector);

    const player = el("div", "h3e-tl-player");
    const playerHead = el("div", "h3e-tl-player-head");
    const playerTitle = el("span", "h3e-tl-player-title");
    playerHead.appendChild(playerTitle);
    playerHead.appendChild(button("×", "Close the player", () => closePlayer(),
                                  "h3e-btn h3e-x"));
    const video = el("video", "h3e-tl-video");
    video.controls = true;
    video.preload = "metadata";
    const playerNote = el("div", "h3e-note h3e-note-hint",
        "The cached hop, before tone correction and without the 40 ms join "
        + "crossfade. A preview, not the master.");
    player.append(playerHead, video, playerNote);
    player.hidden = true;
    root.appendChild(player);

    // Set by a zoom click, so the redraw keeps the same place in the chain
    // rather than the same pixel offset.
    let zoomed = false;

    // The shot open in the inspector, by object rather than index: a delete or
    // a reorder moves indices, and a stale index would open the wrong card.
    let inspected = null;
    const EXTEND_W = 96;

    // Keys the sprite route answered 404 for: gone from the cache.
    const missing = new Set();
    const sprites = new Map();   // key -> {cells, w, h} once loaded

    const props = () => (node.properties ??= {});
    const run = () => props()[RUN_PROP] || null;
    const pps = () => {
        const v = Number(props()[ZOOM_PROP]);
        return ZOOMS.includes(v) ? v : 12;
    };

    /* -- the snapshot a run is compared against ---------------------------- */

    function snapshot() {
        const shots = editor.shots() || [];
        const refs = parseJson(widgetByName(node, "ref_plan")?.value) || {};
        const widgets = {};
        for (const w of node.widgets || []) {
            if (!w?.name || NOT_KEYED.has(w.name) || w.name === "seed") continue;
            if (typeof w.value === "object") continue;
            widgets[w.name] = w.value;
        }
        return {
            ids: shots.map((s) => String(s.id || "")),
            shots: Object.fromEntries(shots.map((s) => [String(s.id || ""), shotSig(s)])),
            refs: Object.fromEntries((refs.refs || []).map(
                (r) => [String(r.tag || ""), JSON.stringify(r)])),
            subjects: JSON.stringify(refs.subjects || {}),
            widgets,
            seed: widgetByName(node, "seed")?.value ?? null,
        };
    }

    /* -- the run record, from the chain's own events ----------------------- */

    function onQueued() {
        props()[PENDING_PROP] = snapshot();
    }

    function onPreview(e) {
        const data = e.detail;
        if (!data?.timeline || data.node_id == null) return;
        if (findNodeByQualifiedId(app.graph, data.node_id) !== node) return;
        const hop = Number(data.hop) || 0;
        if (hop < 1) return;
        let rec = run();
        if (hop === 1 || !rec) {
            // A new run. Hops it does not reach (render_through) keep the
            // last record's entry, marked as older, rather than vanishing.
            const old = rec?.hops || [];
            rec = {
                at: Date.now(),
                total: Number(data.total) || 0,
                snapshot: props()[PENDING_PROP] || rec?.snapshot || null,
                hops: old.map((h) => (h ? { ...h, older: true } : h)),
            };
            delete props()[PENDING_PROP];
        }
        rec.total = Number(data.total) || rec.total;
        rec.hops[hop - 1] = {
            ...data.timeline,
            pin_mech: data.pin_mech || null,
            unpaired: Boolean(data.unpaired),
        };
        rec.hops.length = Math.max(rec.hops.length, rec.total);
        props()[RUN_PROP] = rec;
        if (data.timeline.key) missing.delete(data.timeline.key);
        render();
    }

    api.addEventListener("promptQueued", onQueued);
    api.addEventListener("h3_refchain_preview", onPreview);

    /* -- forecast: what the next queue would re-render --------------------- */

    function forecast(shots) {
        return forecastPlan({
            shots,
            rec: run(),
            now: snapshot(),
            range: editor.renderRange(),
            missing,
            seedCtl: String(widgetByName(node, "control_after_generate")?.value || "fixed"),
            refsNow: parseJson(widgetByName(node, "ref_plan")?.value)?.refs || [],
        });
    }

    /* -- players ------------------------------------------------------------ */

    function openPlayer(title, url) {
        playerTitle.textContent = title;
        player.hidden = false;
        video.src = url;
        video.load();
        video.play?.().catch(() => { /* the user can press play */ });
        node._h3HeightGuard?.sync();
    }

    function closePlayer() {
        video.pause();
        video.removeAttribute("src");
        video.load();
        player.hidden = true;
    }

    /* -- film cells --------------------------------------------------------- */

    function filmCell(key, width) {
        const cell = el("div", "h3e-tl-film");
        cell.style.width = `${width}px`;
        cell.style.height = `${FILM_H}px`;
        if (!key) {
            cell.classList.add("h3e-tl-film-empty");
            return cell;
        }
        const url = `${SPRITE_URL}?key=${key}&n=${SPRITE_N}`;
        const paint = (frac) => {
            const s = sprites.get(key);
            if (!s) return;
            const cw = FILM_H * (s.w / s.cells) / s.h;       // one cell, shown
            const idx = Math.min(s.cells - 1, Math.max(0, Math.floor(frac * s.cells)));
            cell.style.backgroundImage = `url("${url}")`;
            cell.style.backgroundSize = `${cw * s.cells}px ${FILM_H}px`;
            cell.style.backgroundPosition = `${-(idx * cw) + (width - cw) / 2}px 0`;
        };
        if (sprites.has(key)) {
            paint(0.5);
        } else if (!missing.has(key)) {
            const img = new Image();
            img.onload = () => {
                sprites.set(key, { cells: SPRITE_N, w: img.naturalWidth, h: img.naturalHeight });
                paint(0.5);
            };
            img.onerror = () => {
                // 404: the cache no longer holds it. The forecast needs to know.
                missing.add(key);
                render();
            };
            img.src = url;
        }
        if (missing.has(key)) cell.classList.add("h3e-tl-film-empty");
        cell.addEventListener("mousemove", (e) => {
            const r = cell.getBoundingClientRect();
            paint((e.clientX - r.left) / Math.max(1, r.width));
        });
        cell.addEventListener("mouseleave", () => paint(0.5));
        return cell;
    }

    /* -- render ------------------------------------------------------------- */

    function lane(label, height) {
        const row = el("div", "h3e-tl-row");
        row.appendChild(el("span", "h3e-tl-label", label));
        const body = el("div", "h3e-tl-lane");
        if (height) body.style.height = `${height}px`;
        row.appendChild(body);
        return { row, body };
    }

    function place(child, left, width) {
        child.style.left = `${left}px`;
        if (width != null) child.style.width = `${Math.max(2, width)}px`;
        return child;
    }

    function render() {
        // A hidden pane draws nothing and fetches no film; the record is kept
        // either way, and the tab redraws when it is shown.
        if (root.hidden) return;
        const shots = (editor.mode?.() === "shots" && editor.shots?.()) || [];
        // Clearing the track collapses the scroller to zero width, which
        // clamps scrollLeft to 0 -- and every finished hop redraws. Keep the
        // position, as a fraction when the zoom changes the width under it.
        const keepX = scroller.scrollLeft;
        const keepW = scroller.scrollWidth || 1;
        scroller.textContent = "";
        warnings.textContent = "";
        forecastNote.textContent = "";

        zoomBox.textContent = "";
        zoomBox.appendChild(el("span", "h3e-tl-dim", "zoom"));
        for (const z of ZOOMS) {
            const b = button(String(z), `${z} px per second`, () => {
                props()[ZOOM_PROP] = z;
                zoomed = true;
                render();
            }, "h3e-btn h3e-tl-zbtn");
            b.classList.toggle("h3e-on", z === pps());
            zoomBox.appendChild(b);
        }

        if (!shots.length) {
            status.textContent = "";
            scroller.appendChild(el("div", "h3e-empty",
                editor.mode?.() === "shots"
                    ? "No shots yet. Add one on SCRIPT."
                    : "The timeline reads a shot plan. Switch SCRIPT to Shots."));
            onChange?.();
            return;
        }

        const t = editor.timing();
        const fps = t.fps || 24;
        if (!(t.total > 0)) {
            scroller.appendChild(el("div", "h3e-empty", "Loading the vocabulary…"));
            return;
        }
        const scale = pps() / fps;                 // px per frame
        const isStart = (i) => i === 0 || shots[i]?.anchor === "restart";
        const geo = [];
        let f = 0;
        shots.forEach((s, i) => {
            const raw = t.lengths[i] || 0;
            const trim = isStart(i) ? 0 : t.ov;
            geo.push({ start: f, frames: raw - trim, restart: i > 0 && isStart(i) });
            f += raw - trim;
        });
        const W = Math.ceil(f * scale);
        const rec = run();
        const fc = forecast(shots);

        // Status line.
        if (rec) {
            const hops = (rec.hops || []).filter(Boolean);
            const count = (k) => hops.filter((h) => !h.older && h.status === k).length;
            const when = new Date(rec.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            const bits = [`last run ${when}`];
            for (const k of ["cached", "rendered", "locked"]) if (count(k)) bits.push(`${count(k)} ${k}`);
            const up = hops.filter((h) => !h.older && h.unpaired).length;
            if (up) bits.push(`${up} unpaired join${up > 1 ? "s" : ""}`);
            status.textContent = bits.join(" · ");
        } else {
            status.textContent = "no run recorded yet -- queue with cache_hops on";
        }

        const track = el("div", "h3e-tl-track");
        track.style.width = `${W + EXTEND_W + 48}px`;

        // Ruler.
        const ruler = lane("time", 16);
        ruler.body.style.width = `${W}px`;
        const every = pps() >= 24 ? 2 : pps() >= 12 ? 5 : 10;
        for (let s = 0; s * fps <= f; s += 1) {
            const tick = el("span", s % every === 0 ? "h3e-tl-tick h3e-tl-tick-major" : "h3e-tl-tick");
            place(tick, s * pps());
            if (s % every === 0) tick.textContent = `${s}s`;
            ruler.body.appendChild(tick);
        }
        track.appendChild(ruler.row);

        // Plan blocks.
        const range = editor.renderRange();
        const plan = lane("plan", 74);
        plan.body.style.width = `${W}px`;
        shots.forEach((s, i) => {
            const g = geo[i];
            const b = el("div", "h3e-tl-block");
            if (s.locked) b.classList.add("h3e-tl-locked");
            if (g.restart) b.classList.add("h3e-tl-restart");
            if (s === inspected) b.classList.add("h3e-tl-selected");
            b.title = "Click to edit this shot here.";
            b.addEventListener("click", (e) => {
                if (e.target.closest("button")) return;   // ⏵ and lock act on their own
                inspected = inspected === s ? null : s;
                render();
            });
            const top = el("div", "h3e-tl-bhead");
            top.appendChild(el("span", "h3e-tl-no",
                `${i + 1}${fc.state[i] === "render" && fc.cause[i]?.startsWith(`shot ${i + 1} `) ? " ✎" : ""}`));
            const solo = button("⏵", `Render only shot ${i + 1}; every other hop replays `
                + "from the cache. Click again to clear.", () => editor.toggleSolo(i + 1),
                "h3e-btn h3e-tl-mini");
            solo.classList.toggle("h3e-on", range.from === i + 1 && range.through === i + 1);
            const lock = button(s.locked ? "locked" : "lock",
                "Reuse this shot's cached render even when its inputs change.",
                () => editor.toggleLock(i), "h3e-btn h3e-tl-mini");
            lock.classList.toggle("h3e-on", Boolean(s.locked));
            top.append(solo, lock);
            b.appendChild(top);
            const beat = String(s.beat || "").replace(/\s+/g, " ").trim();
            const peek = el("div", "h3e-tl-peek", beat.length > 48 ? beat.slice(0, 46) + "…" : beat);
            peek.title = beat;
            b.appendChild(peek);
            const tags = [...new Set((beat.match(/@[A-Za-z0-9_]+/g) || []))].join(" ");
            b.appendChild(el("div", "h3e-tl-meta",
                `${clock(g.frames / fps)} ${g.frames}f${g.restart ? " · restart" : ""}${tags ? " · " + tags : ""}`));
            plan.body.appendChild(place(b, g.start * scale, g.frames * scale - 2));
        });
        // Extend: append a shot that continues from the last one. Everything
        // before it keeps its key, so a queue renders only the new hop.
        const ext = el("div", "h3e-tl-block h3e-tl-extend");
        ext.appendChild(el("div", "h3e-tl-extend-plus", "+"));
        ext.appendChild(el("div", "h3e-tl-meta", "extend"));
        ext.title = "Add a shot that continues from the last one. The shots before "
            + "it stay cached, so the next queue renders only the new hop.";
        ext.addEventListener("click", () => {
            const idx = editor.appendShot();
            inspected = editor.shots()[idx] || null;
            render();
            scroller.scrollLeft = scroller.scrollWidth;
            inspector.querySelector("textarea.h3e-beat")?.focus();
        });
        plan.body.appendChild(place(ext, W + 4, EXTEND_W - 8));
        track.appendChild(plan.row);

        // Film.
        const film = lane("film", FILM_H + 14);
        film.body.style.width = `${W}px`;
        shots.forEach((s, i) => {
            const g = geo[i];
            const h = rec?.hops?.[i];
            const key = h?.key || null;
            const wrap = el("div", "h3e-tl-filmwrap");
            const w = Math.max(2, g.frames * scale - 2);
            const cell = filmCell(key, w);
            if (key && !missing.has(key)) {
                cell.title = `Hover to scrub, click to play hop ${i + 1}.`;
                cell.addEventListener("click", () => openPlayer(
                    `hop ${i + 1} · ${h.status} · ${key.slice(0, 8)}`,
                    `${CLIP_URL}?key=${key}`));
            }
            wrap.appendChild(cell);
            const cap = !key ? "not rendered yet"
                : missing.has(key) ? "not in cache -- queue once"
                : `${h.status}${h.older ? " (earlier run)" : ""} · ${key.slice(0, 8)}`;
            wrap.appendChild(el("div", "h3e-tl-cap", cap));
            film.body.appendChild(place(wrap, g.start * scale, w));
        });
        track.appendChild(film.row);

        // Seams.
        const seamRow = lane("seam", 30);
        seamRow.body.style.width = `${W}px`;
        for (let i = 1; i < shots.length; i++) {
            const g = geo[i];
            const a = rec?.hops?.[i - 1];
            const b = rec?.hops?.[i];
            const was = Boolean(b?.unpaired && !b.older);
            const will = fc.unpaired.includes(i);
            const mark = el("span", "h3e-tl-seam");
            const mech = g.restart ? "cut" : (b?.pin_mech === "motion_context" ? "mc"
                : b?.pin_mech === "addguide" ? "ag" : "");
            mark.textContent = `${was || will ? "▲" : "●"} ${i}|${i + 1}${mech ? " " + mech : ""}`;
            if (was || will) mark.classList.add("h3e-tl-bad");
            const playable = a?.key && b?.key && !missing.has(a.key) && !missing.has(b.key);
            mark.title = (was ? `The ${i}|${i + 1} join was never generated together. ` : "")
                + (will ? `Queued now, ${i}|${i + 1} would be unpaired. ` : "")
                + (playable ? "Click to play the join." : "Nothing cached to play.");
            if (playable) {
                mark.classList.add("h3e-tl-click");
                const trim = g.restart ? 0 : (b.overlap ?? t.ov);
                mark.addEventListener("click", () => openPlayer(
                    `join ${i}|${i + 1}${g.restart ? " (restart cut)" : ""}`,
                    `${SEAM_URL}?a=${a.key}&b=${b.key}&trim=${trim}`));
            }
            seamRow.body.appendChild(place(mark, g.start * scale - 4));
        }
        track.appendChild(seamRow.row);

        // Forecast.
        const LABEL = {
            keeps: "keeps", render: "RE-RENDER", locked: "locked", skip: "skipped",
            replay: "replays", "replay-miss": "REPLAY MISSING",
        };
        // Literal class names, so tools/check_ui.py can see each one is set.
        const CLASS = {
            render: "h3e-tl-next-render", locked: "h3e-tl-next-locked",
            skip: "h3e-tl-next-skip", "replay-miss": "h3e-tl-next-replay-miss",
        };
        const next = lane("next", 20);
        next.body.style.width = `${W}px`;
        shots.forEach((s, i) => {
            const g = geo[i];
            const cellEl = el("div", "h3e-tl-next", LABEL[fc.state[i]]);
            if (CLASS[fc.state[i]]) cellEl.classList.add(CLASS[fc.state[i]]);
            cellEl.title = fc.cause[i] ? `Because ${fc.cause[i]}.` : "";
            next.body.appendChild(place(cellEl, g.start * scale, g.frames * scale - 2));
        });
        track.appendChild(next.row);

        scroller.appendChild(track);
        scroller.scrollLeft = zoomed
            ? keepX / keepW * scroller.scrollWidth
            : keepX;
        zoomed = false;

        // Why, in one line: the first cause, and where it stops.
        const firstMove = fc.state.findIndex((st) => st === "render" || st === "replay-miss");
        if (firstMove >= 0) {
            const renders = fc.state.map((st, i) => (st === "render" ? i + 1 : null)).filter(Boolean);
            forecastNote.textContent = `If queued now (preview): ${fc.cause[firstMove] || "changes"} `
                + `→ re-renders ${renders.join(", ") || "nothing"}.`
                + (fc.state.includes("replay-miss")
                    ? " render_from needs a hop the cache no longer matches, so the queue would stop." : "");
        } else if (rec) {
            forecastNote.textContent = "If queued now (preview): nothing re-renders.";
        }

        // Warnings, in the wording the run itself logs.
        const shown = new Set();
        const warn = (i, text) => {
            if (shown.has(i)) return;
            shown.add(i);
            warnings.appendChild(el("div", "h3e-note h3e-note-error", text));
        };
        for (let i = 1; i < shots.length; i++) {
            const b = rec?.hops?.[i];
            if (b?.unpaired && !b.older) {
                warn(i, `▲ ${i}|${i + 1} was never generated together. Shot ${i + 1} is locked, `
                    + `but it was rendered against a different shot ${i}. Unlock shot ${i + 1}, `
                    + `or pin shot ${i}'s ending with last_frame_guide or anchor=restart.`);
            }
        }
        for (const i of fc.unpaired) {
            warn(i, `▲ Queued now, shot ${i + 1} stays locked while shot ${i} re-renders, so the `
                + `${i}|${i + 1} join would be one nobody generated. Unlock shot ${i + 1}, or pin `
                + `shot ${i}'s ending with last_frame_guide or anchor=restart.`);
        }

        // An extension that would cost more than its own hop, said plainly.
        const last = shots.length - 1;
        const isNew = last > 0 && rec && !rec.hops?.[last]?.key && rec.hops?.[last - 1]?.key;
        if (isNew) {
            const seedCtl = String(widgetByName(node, "control_after_generate")?.value || "fixed");
            if (fc.state.slice(0, last).some((st) => st === "render")
                && fc.cause.some((c) => String(c || "").startsWith("seed is set to"))) {
                warnings.appendChild(el("div", "h3e-note h3e-note-error",
                    `The seed is set to ${seedCtl} each queue, so extending re-renders every `
                    + "shot without its own seed. Set control_after_generate to fixed to "
                    + "render only the new shot."));
            }
            const tail = String(shots[last - 1]?.directives?.tail || "");
            if (tail === "settle" || tail === "hold") {
                warnings.appendChild(el("div", "h3e-note h3e-note-hint",
                    `Shot ${last} ends at rest (tail: ${tail}), so shot ${last + 1} opens from `
                    + `rest. To carry the motion on, set shot ${last}'s tail to ongoing -- `
                    + `that re-renders shot ${last} too.`));
            }
        }

        renderInspector(shots);
        onChange?.();
    }

    function renderInspector(shots) {
        const idx = inspected ? shots.indexOf(inspected) : -1;
        if (idx < 0) {
            inspected = null;
            inspector.textContent = "";
            return;
        }
        // Mid-edit, leave the card alone: rebuilding it would move the caret
        // (a finished hop redraws the whole tab). It already shows its edit.
        const typing = inspector.contains(document.activeElement)
            && inspector.dataset.shot === String(idx);
        if (typing) return;
        inspector.textContent = "";
        inspector.dataset.shot = String(idx);
        const head = el("div", "h3e-tl-inspector-head");
        head.appendChild(el("span", "h3e-tl-dim", `editing shot ${idx + 1} -- the same card as SCRIPT`));
        head.appendChild(button("×", "Close", () => {
            inspected = null;
            render();
        }, "h3e-btn h3e-x"));
        inspector.appendChild(head);
        const card = editor.cardFor(idx);
        if (card) inspector.appendChild(card);
        node._h3HeightGuard?.sync();
    }

    /** `⏵N` while a solo is armed, `⏵a–b` for any other range. */
    function badge() {
        const { from, through } = editor.renderRange();
        if (!from && !through) return "";
        if (from && from === through) return `⏵${from}`;
        return `⏵${from || 1}–${through || "end"}`;
    }

    function destroy() {
        api.removeEventListener("promptQueued", onQueued);
        api.removeEventListener("h3_refchain_preview", onPreview);
        closePlayer();
    }

    return { root, render, badge, destroy };
}
