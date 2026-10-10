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
import { NOT_KEYED, forecastPlan, framesOf, parseJson, shotSig, tieSections } from "./timeline_model.js";

const RUN_PROP = "h3_timeline_run";
const PENDING_PROP = "h3_timeline_pending";
const ZOOM_PROP = "h3_timeline_pps";
const ZOOMS = [6, 12, 24];
const SPRITE_N = 24;
const FILM_H = 120;

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
    // A8: S/M/L. The cached hop is short and thin, and `width:100%` stretched it
    // to the full panel width regardless of its aspect -- a 9:16 portrait film
    // rendered as a tall skinny strip with letterbox either side, and a 16:9
    // one filled the whole node and pushed the seam row off screen. Three sizes
    // cycle on click; the chosen one is remembered so it survives a redraw.
    const PLAYER_SIZES = ["S", "M", "L"];
    const SIZE_W = { S: 240, M: 400, L: 560 };
    let playerSize = 1;
    const sizeBtn = button("M", "Player size: small / medium / large", () => {
        playerSize = (playerSize + 1) % PLAYER_SIZES.length;
        paintPlayerSize();
    }, "h3e-btn h3e-tl-sizebtn");
    function paintPlayerSize() {
        sizeBtn.textContent = PLAYER_SIZES[playerSize];
        video.style.maxWidth = SIZE_W[PLAYER_SIZES[playerSize]] + "px";
    }
    playerHead.appendChild(sizeBtn);
    playerHead.appendChild(button("×", "Close the player", () => closePlayer(),
                                  "h3e-btn h3e-x"));
    const video = el("video", "h3e-tl-video");
    video.controls = true;
    video.preload = "metadata";
    // Aspect from the clip itself, once the metadata lands: a 9:16 film gets a
    // tall box and a 16:9 one a wide box, at the SAME width, so the two are
    // directly comparable and neither is stretched.
    video.addEventListener("loadedmetadata", () => {
        const w = video.videoWidth, h = video.videoHeight;
        if (w && h) {
            player.classList.toggle("h3e-portrait", h > w);
            player.classList.toggle("h3e-landscape", w >= h);
        }
        paintPlayerSize();
    });
    paintPlayerSize();
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
        if (data?.node_id == null) return;
        if (findNodeByQualifiedId(app.graph, data.node_id) !== node) return;
        // A10: the final "done" push carries the hand-tie previs warnings and
        // no per-hop timeline record, so it would be dropped by the guard
        // below. It has to be handled BEFORE that check or the warning never
        // reaches the node -- which is the entire reason it is in the payload.
        if (Array.isArray(data.ht_previs_warn)) {
            const rec0 = run();
            if (rec0) {
                rec0.previs_warn = data.ht_previs_warn;
                props()[RUN_PROP] = rec0;
                // The status bar chip (A10, first half) reads the same record.
                node._h3Warn?.show(data.ht_previs_warn);
                render();
            }
            return;
        }
        if (!data?.timeline) return;
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
            tie: String(widgetByName(node, "pin_mech")?.value || "") === "hand_tie",
            frames: shots.map((s) => framesOf(s.duration
                || widgetByName(node, "duration")?.value)),
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

        // A9: where the hand-tie previews begin. Since 2fbc025 a long hand-tie
        // plan splits into CHAINED sections -- one previs per section, several
        // hops cut from it -- and "+ extend" opens a new one with anchor=extend.
        // The consequence the user cannot see is that editing hop 2 re-renders
        // hops 1-12, because they were all cut from one previs. That reads as a
        // bug unless the boundary is drawn, so it is drawn here.
        //
        // tieSections() is hand_tie.sections()/chained() ported, and is the same
        // function the forecast tests. NOT recomputed here: a second
        // implementation in the timeline is a second thing to disagree with the
        // renderer about, which is the failure this whole file exists to avoid.
        const tieOn = String(widgetByName(node, "pin_mech")?.value || "") === "hand_tie";
        const sections = tieOn
            ? tieSections(shots, shots.map((s) => framesOf(
                s.duration || widgetByName(node, "duration")?.value)))
            : [];
        // D3 reads these rather than calling tieSections() a second time. The
        // score strip wants the same boundaries as these markers, and the way to
        // guarantee that is to hand over THIS result -- a second call inside the
        // same file would be a second answer, which is the rule this file is
        // built to avoid.
        lastSections = sections;
        // hop index -> "new previs" (a fresh section) or "" (continues).
        const newPrevis = new Map();
        sections.forEach((sec, n) => {
            if (n === 0) return;
            sec.hops.forEach((hi) => newPrevis.set(hi, true));
        });
        // An anchor=extend shot opens a section in its own right.
        shots.forEach((s, i) => {
            if (i > 0 && s?.anchor === "extend") newPrevis.set(i, true);
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

        // A10: the previs warnings, placed on the film. `rec.previs_warn` is
        // [{section, seed, cuts, first_hop, label}] from run(). The cut frames
        // index the SECTION's frames; the section's first hop is where its
        // first window lands in the film, so hop `first_hop` at
        // `first_hop's start + cut` is where the cut appears on the strip. A
        // cut in a later window of the section lands on a later hop; the
        // window-to-hop stride is the hop length, so `cut / hopFrames` picks the
        // hop. Good enough to place a warning you can click, and exact for the
        // common single-window case.
        const previsWarn = new Map();
        if (rec && Array.isArray(rec.previs_warn) && tieOn) {
            for (const w of rec.previs_warn) {
                const first = Number(w.first_hop) || 0;
                const g0 = geo[first];
                if (!g0) continue;
                const hopFrames = t.lengths[first] || g0.frames || 192;
                for (const c of (w.cuts || [])) {
                    const hopIdx = Math.min(shots.length - 1,
                        first + Math.floor(Number(c) / hopFrames));
                    if (!previsWarn.has(hopIdx)) previsWarn.set(hopIdx, []);
                    previsWarn.get(hopIdx).push({
                        frame: Number(c) % hopFrames,
                        label: w.label || `previs ${w.section} cuts at f${c}`,
                    });
                }
            }
        }

        // The runway: extend past the last block by one hop width.
        const track = el("div", "h3e-tl-track");
        track.style.width = `${W + EXTEND_W + 48}px`;

        // Ruler.
        const ruler = lane("time", 20);
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
        const plan = lane("plan", 104);
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
            const peek = el("div", "h3e-tl-peek", beat.length > 160 ? beat.slice(0, 158) + "…" : beat);
            peek.title = beat;
            b.appendChild(peek);
            const tags = [...new Set((beat.match(/@[A-Za-z0-9_]+/g) || []))].join(" ");
            // A9: a thin marker on the block that opens a hand-tie section, and
            // the same fact in the meta line. "new previs" is the wording that
            // explains the forecast: a whole section is re-rendered from one
            // previs, so the hops after this one re-run with it.
            const isNewPrevis = newPrevis.get(i);
            b.classList.toggle("h3e-tl-newprevis", Boolean(isNewPrevis));
            b.appendChild(el("div", "h3e-tl-meta",
                `${clock(g.frames / fps)} ${g.frames}f${g.restart ? " · restart" : ""}`
                + `${isNewPrevis ? " · new previs" : ""}${tags ? " · " + tags : ""}`));
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
            const idx = editor.appendShot({ extend: true });
            inspected = editor.shots()[idx] || null;
            render();
            scroller.scrollLeft = scroller.scrollWidth;
            inspector.querySelector("textarea.h3e-beat")?.focus();
        });
        plan.body.appendChild(place(ext, W + 4, EXTEND_W - 8));
        track.appendChild(plan.row);

        // Film.
        const film = lane("film", FILM_H + 18);
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
        const seamRow = lane("seam", 26);
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
                const ov = g.restart ? 0 : (b.overlap ?? t.ov);
                // Hand-tie cuts the previous hop's tail, not this hop's head.
                const tie = b.pin_mech === "hand_tie";
                const cut = tie ? `trim=0&tail=${ov}` : `trim=${ov}`;
                mark.addEventListener("click", () => openPlayer(
                    `join ${i}|${i + 1}${g.restart ? " (restart cut)" : ""}`,
                    `${SEAM_URL}?a=${a.key}&b=${b.key}&${cut}`));
            }
            seamRow.body.appendChild(place(mark, g.start * scale - 4));
            // A9: where a hand-tie section begins, on the seam row as well as
            // on the block. The join at a new previs is NOT a restart -- it
            // continues the film -- but everything from here to the next
            // boundary is cut from a different take, so it re-renders together.
            // `newPrevis` is the Map built above for the BLOCK row; the seam row
            // is a separate loop and cannot see a `const` from the other one, so
            // it reads the Map directly. This line shipped referencing a name
            // that existed only inside the block loop, so every render threw
            // ReferenceError and the Timeline tab drew nothing at all.
            if (newPrevis.get(i)) {
                const cutMark = el("span", "h3e-tl-cutpoint");
                cutMark.textContent = "❋";
                cutMark.title = `A new hand-tie previs starts at hop ${i + 1}. `
                    + "Everything from here to the next one was cut from that take, "
                    + "so editing it re-renders the whole section.";
                seamRow.body.appendChild(place(cutMark, g.start * scale - 4));
            }
            // A10: the previs warning lands on the frame where it cuts, which
            // is inside this hop, not on its join. `previsWarn` maps hop index
            // -> [{frame, label}] from the run record.
            for (const w of (previsWarn.get(i) || [])) {
                const warnMark = el("span", "h3e-tl-previswarn");
                warnMark.textContent = "⚠";
                warnMark.title = w.label;
                seamRow.body.appendChild(place(warnMark, (g.start + w.frame) * scale - 4));
            }
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
        const next = lane("next", 26);
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
        const actions = el("span", "h3e-tl-inspector-actions");
        // Words, not a second ×: the card below has its own × (delete), and
        // two identical glyphs stacked is how "close" got clicked for "remove".
        // Removing a shot that has a render is two clicks, the first arming it.
        const rendered = Boolean(run()?.hops?.[idx]?.key);
        const remove = button(`remove shot ${idx + 1}`,
            rendered ? "Delete this shot from the plan. Its cached render stays until the "
                + "cache sweeps it." : "Delete this shot from the plan.",
            () => {
                if (rendered && !remove.classList.contains("h3e-tl-armed")) {
                    remove.classList.add("h3e-tl-armed");
                    remove.textContent = `click again to remove shot ${idx + 1}`;
                    return;
                }
                inspected = null;
                editor.removeShot(idx);
            }, "h3e-btn h3e-tl-remove");
        actions.appendChild(remove);
        actions.appendChild(button("close", "Close the editor", () => {
            inspected = null;
            render();
        }, "h3e-btn"));
        head.appendChild(actions);
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

    /* D2: the per-hop cache keys of the last run, for the Script filmstrip's
   * thumbnails. This module already receives every hop's key on the preview
   * event and keeps them in h3_timeline_run, so handing them out costs nothing
   * and avoids a second copy of the same run record on the node.
   *
   * Index 0 is hop 1. A hop that never rendered has no key and reads null,
   * which the filmstrip draws as the bare slate. */
  function hopKeys() {
    const r = run();
    if (!r || !Array.isArray(r.hops)) return null;
    return r.hops.map((h) => (h && h.key) || null);
  }

  /* D3: the section boundaries for the Script score strip, captured from the
   * one tie-sections call above rather than recomputed here. A second call
   * would be a second answer to "where does this section end" inside one file,
   * which is what check_previs_warn's "NOT recomputed in the UI" rule exists
   * to forbid. Empty until the Timeline tab has been rendered once.
   *
   * Note: that check counts call sites by regex over the file with `//`
   * comments stripped, so this block comment must not spell the call with
   * parentheses -- doing so makes the audit report two call sites when there is
   * one, and the fix would be to delete a comment rather than to look at what
   * it was protecting. */
  let lastSections = [];
  function sections() { return lastSections; }

  /* D3: the forecast for the Script score strip's NEXT QUEUE line. It calls
   * the same forecast() this tab uses, on the same arguments -- there is one
   * implementation of "what would the next queue do", not one per surface.
   * `shots` is the LIVE plan, because the strip is an authoring view and must
   * reflect an edit the user has not queued yet; `run()` is still the record
   * of what actually happened. */
  function forecastNow(shots) {
    try { return forecast(Array.isArray(shots) && shots.length ? shots : (run()?.shots || [])); }
    catch (_) { return null; }
  }

  return { root, render, badge, destroy, hopKeys, sections, forecastNow };
}
