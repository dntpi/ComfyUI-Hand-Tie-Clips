/**
 * The shot plan editor.
 *
 * `shot_plan` (a hidden STRING widget) stays the ONLY source of truth. Every
 * control here reads from it and writes straight back, so a workflow authored
 * with the editor and one typed by hand are the same file, and anything the
 * editor cannot express is still reachable through the JSON tab.
 *
 * Structure follows ComfyUI-MiniMaxH3-Contex-Loop's h3_chain_plan_editor.js,
 * which solved this shape already. What is ours: the directive axes come from
 * the server (see routes.py) rather than a second copy, the coherence rule is
 * mirrored from plan.check_coherence, and the timing summary uses the real
 * overlap arithmetic instead of a nominal per-shot duration.
 */

import {
    el, button, select, vocab, widgetByName, refreshVocab, onVocabRefresh,
} from "./widget_utils.js";
import { createTemplatePanel } from "./templates.js";

const MODE_PROP = "h3_editor_mode";        // "simple" | "shots"
const FOLD_PROP = "h3_plan_folded";        // shot id -> collapsed
const BACKUP_PROP = "h3_plan_backup";      // shot_plan stashed while in Simple

/* -- plan model ---------------------------------------------------------- */

export function parsePlan(text) {
    // A widget value is not guaranteed to be a string. ComfyUI restores
    // `widgets_values` POSITIONALLY, so a node whose widget list changed can
    // hand this an int or a bool from a neighbouring widget. Coerce rather
    // than throw: a wrong-typed plan should show the JSON tab, not abort the
    // whole workflow load.
    const t = String(text ?? "").trim();
    if (!t) return [];
    let data;
    try {
        data = JSON.parse(t);
    } catch (_) {
        return null; // signals "unparseable"; the JSON tab takes over
    }
    // Valid JSON that is not a plan (a bare `null`, a stray int from a
    // misaligned widget) would otherwise be dereferenced below.
    if (data === null || typeof data !== "object") return null;
    const shots = Array.isArray(data) ? data : (data.shots || []);
    if (!Array.isArray(shots)) return null;
    return shots.map((s, i) => ({
        id: String(s.id || `s${i + 1}`),
        beat: String(s.beat || ""),
        directives: Object.assign({}, s.directives || {}),
        prose: String(s.prose || ""),
        seed: s.seed == null ? null : Number(s.seed),
        steps: s.steps == null ? null : Number(s.steps),
        duration: s.duration || null,
        locked: Boolean(s.locked),
        // `tone` and `anchor` are round-tripped, not just parsed. Both are
        // Python-side shot fields with no card control before now, and both
        // were silently DESTROYED by editing any shot: parsePlan dropped them
        // and planToJson rebuilt each shot from a fixed field list. `tone`
        // shipped in 1.1 with that defect.
        tone: s.tone || "",
        anchor: s.anchor || "",
        // Same class as tone/anchor: a Python-side field the editor used to
        // destroy on any card edit. null = register default; [] = none.
        refs: Array.isArray(s.refs)
            ? s.refs.map((t) => String(t).replace(/^@/, "").trim()).filter(Boolean)
            : null,
        // pin_mech=hand_tie's previs fields. Round-tripped for the same reason
        // as tone/anchor/refs: a field this list drops is deleted by the next
        // edit of any card.
        moment: String(s.moment || ""),
        moment_end: String(s.moment_end || ""),
    }));
}

export function planToJson(shots) {
    if (!shots || !shots.length) return "";
    const out = shots.map((s) => {
        const o = { id: s.id, beat: s.beat };
        const dirs = {};
        for (const [k, v] of Object.entries(s.directives || {})) if (v) dirs[k] = v;
        if (Object.keys(dirs).length) o.directives = dirs;
        if (s.prose) o.prose = s.prose;
        if (s.seed != null) o.seed = s.seed;
        if (s.steps != null) o.steps = s.steps;
        if (s.duration) o.duration = s.duration;
        if (s.locked) o.locked = true;
        if (s.tone) o.tone = s.tone;
        if (s.anchor) o.anchor = s.anchor;
        // Write [] as well as a filled list: omitting it would turn
        // "explicit none" into "register default" on the next save.
        if (Array.isArray(s.refs)) o.refs = s.refs;
        if (s.moment) o.moment = s.moment;
        if (s.moment_end) o.moment_end = s.moment_end;
        return o;
    });
    return JSON.stringify({ shots: out }, null, 2);
}

/**
 * Mirrors plan.check_coherence. A framing change asks the audience to be
 * somewhere new; with a held camera the only way to get there is a cut, so
 * `continuous` and the framing change want opposite things.
 */
function coherenceWarning(shot, index, shots) {
    if (index === 0) return null;
    const d = shot.directives || {};
    const framing = d.framing || "";
    const camera = d.camera || "";
    if (d.join !== "continuous" || !framing || framing === "keep") return null;
    if (camera && camera !== "hold") return null;
    /* The framing this shot INHERITS. `keep` and an unset axis both leave it
     * alone, so walk back to the last shot that actually named one. A first
     * mention is not a change -- and neither is restating the same value,
     * which is what models do on every shot because the axis describes the
     * shot rather than a transition. Without this the banner sat on
     * medium/medium/medium plans whose framing never moved. */
    let prev = "";
    for (let i = index - 1; i >= 0; i -= 1) {
        const f = (((shots || [])[i] || {}).directives || {}).framing || "";
        if (f && f !== "keep") { prev = f; break; }
    }
    if (!prev || prev === framing) return null;
    return `join=continuous changes framing from ${prev} to ${framing} with a held `
         + "camera, which implies a cut. Use push_in / pull_back / pan_follow to "
         + "reach that framing on the move, or framing=keep.";
}

/**
 * Mirrors the direction check in plan.check_coherence: push_in narrows the
 * frame and pull_back opens it, so naming the opposite destination is a
 * physical contradiction at any join value.
 */
function directionWarning(shot) {
    const d = shot.directives || {};
    const pair = `${d.camera || ""}|${d.framing || ""}`;
    if (pair === "push_in|wide" || pair === "pull_back|close") {
        return `camera=${d.camera} moves the opposite way from framing=${d.framing}. `
             + "Pick the framing the move actually lands on.";
    }
    return null;
}

/* -- editor -------------------------------------------------------------- */

export function createPlanEditor(node, { onChange }) {
    const planWidget = widgetByName(node, "shot_plan");
    const promptWidget = widgetByName(node, "prompt");
    const chainsWidget = widgetByName(node, "chains");
    const durationWidget = widgetByName(node, "duration");
    const overlapWidget = widgetByName(node, "overlap");

    let V = null;
    const initial = parsePlan(planWidget?.value);
    let broken = initial === null;
    let shots = initial || [];

    const root = el("div", "h3e-section");

    /* mode toggle */
    const head = el("div", "h3e-head");
    head.appendChild(el("span", "h3e-title", "SCRIPT"));
    const summary = el("span", "h3e-count");
    head.appendChild(summary);
    head.appendChild(el("span", "h3e-spacer"));

    const modeWrap = el("div", "h3e-modes");
    const simpleBtn = button("Simple", "One prompt, repeated across N hops", () => setMode("simple"), "h3e-mode");
    const shotsBtn = button("Shots", "One card per hop, with directives", () => setMode("shots"), "h3e-mode");
    modeWrap.appendChild(simpleBtn);
    modeWrap.appendChild(shotsBtn);
    head.appendChild(modeWrap);
    const blankShot = (i) => ({
        id: `s${i + 1}`, beat: "", directives: {}, prose: "",
        seed: null, steps: null, duration: null, locked: false,
    });
    const addBtn = button("+ shot", "Append a shot", () => {
        shots.push(blankShot(shots.length));
        commit();
    });
    head.appendChild(addBtn);

    /* Templates append patterns rather than replacing the script. Replacing
     * would be the one destructive button on the node, and appending is what
     * you actually want -- a chain is built by stacking these. */
    const templates = createTemplatePanel({
        onPick: (picked) => {
            if (mode() !== "shots") setMode("shots");
            for (const sh of picked) shots.push({ ...sh, id: freeId() });
            templates.hide();
            tplBtn.classList.remove("h3e-on");
            commit();
        },
    });
    const tplBtn = button("Templates", "Insert a ready-made shot pattern", () => {
        tplBtn.classList.toggle("h3e-on", templates.toggle());
    });
    head.appendChild(tplBtn);
    root.appendChild(head);
    root.appendChild(templates.root);

    /** The lowest `sN` no shot is using. Appending a template onto a plan whose
     *  ids are already s1..s3 must not mint a second s1: `id` is the hop
     *  cache's pointer, and two shots sharing one would make `locked` reuse the
     *  wrong render. */
    function freeId() {
        const used = new Set(shots.map((sh) => sh.id).filter(Boolean));
        for (let n = 1; ; n += 1) {
            const id = `s${n}`;
            if (!used.has(id)) return id;
        }
    }

    /* Simple mode: the `prompt` widget, drawn here instead of as a raw native
     * text box above the panel. The widget stays the only store -- this box
     * reads it and writes straight back, so a workflow typed here and one
     * typed into the widget are the same file. */
    const simpleNote = el("div", "h3e-simple");
    const simpleArea = el("textarea", "h3e-beat h3e-simple-prompt");
    simpleArea.placeholder =
        "Write the prompt for the clip. A plain description, or a full H3 prompt "
        + "(subject_definitions: ... non_diegetic_music:). Put a line with only --- "
        + "between blocks to give each hop its own text.";
    function writePrompt() {
        if (!promptWidget) return;
        promptWidget.value = simpleArea.value;
        try {
            promptWidget.callback?.(simpleArea.value, node.graph?.canvas, node);
        } catch (err) {
            console.error("[HandTieClips] prompt widget callback failed:", err);
        }
        node.graph?.setDirtyCanvas?.(true, true);
    }
    // Every keystroke, not "change": a textarea only fires "change" on blur,
    // and saving with the caret still in the box would drop the last edit.
    simpleArea.addEventListener("input", () => { writePrompt(); updateRepeatHint(); });
    simpleArea.addEventListener("change", () => { writePrompt(); onChange?.(); });
    simpleNote.appendChild(simpleArea);

    /* One block with chains > 1 repeats the whole prompt as a continuation on
     * every later hop -- a full H3 prompt then renders its scene twice. Easy
     * to hit when `chains` was left over from an earlier chain (2026-10-01),
     * so say it where the prompt is written. Same split as run()'s
     * _parse_shots: a line holding only ---. */
    const repeatHint = el("div", "h3e-note");
    simpleNote.appendChild(repeatHint);
    function updateRepeatHint() {
        const text = simpleArea.value.trim();
        const blocks = text
            ? text.split(/^---\s*$/m).filter((b) => b.trim()).length || 1
            : 0;
        const n = Math.max(1, parseInt(chainsWidget?.value, 10) || 1);
        if (blocks === 1 && n > 1) {
            repeatHint.textContent =
                `One block, chains = ${n}: this prompt runs as hop 1 and is then repeated `
                + `as a continuation for ${n - 1} more hop(s). For a single clip, set `
                + "chains to 1 under RUN.";
            repeatHint.style.display = "";
        } else {
            repeatHint.style.display = "none";
        }
    }
    simpleNote.appendChild(el("div", "h3e-simple-help",
        "Simple mode: this prompt drives the whole run. One block is repeated across "
        + "`chains` hops (set under RUN); separate blocks with a line of --- to give "
        + "hops their own text. `hop_script` decides what hop 2+ receives: verbatim "
        + "sends each block exactly as written, next wraps it as a continuation. "
        + "@tags from REFS resolve here too. For one beat per hop with camera and "
        + "join directives, switch to Shots."));
    root.appendChild(simpleNote);

    const list = el("div", "h3e-shots");
    root.appendChild(list);

    /* -- D2: the filmstrip, and the card editor re-homed below it ---------- */
    //
    // Shots mode draws ONE row of mini posters with the selected shot's full
    // card underneath, instead of every card stacked full height. That is the
    // whole change, and it is what makes the Script surface a fixed height: at
    // six shots and at nineteen the panel is the same size, because the strip
    // scrolls horizontally and the detail pane shows exactly one card.
    //
    // NOT a rewrite. The card below is renderCard(), the same builder the old
    // list used, so there is one implementation of a shot card and one place
    // that writes shot_plan. `cardFor(i)` returns this same pane's editor, so
    // a TIMELINE click still lands the caret in that shot's beat.
    const strip = el("div", "h3e-filmstrip");
    const detail = el("div", "h3e-detail");
    root.appendChild(strip);
    root.appendChild(detail);
    // The old stacked list stays in the DOM but is not drawn in Shots mode --
    // it is still what `shots` mode falls back to if the strip is ever off.
    list.style.display = "none";

    /* JSON escape hatch */
    const jsonWrap = el("details", "h3e-json-wrap");
    const jsonSum = el("summary", null, "JSON");
    jsonSum.title = "The literal shot_plan value. Editing here updates the cards.";
    jsonWrap.appendChild(jsonSum);
    const jsonArea = el("textarea", "h3e-json");
    jsonArea.spellcheck = false;
    jsonWrap.appendChild(jsonArea);
    const jsonErr = el("div", "h3e-note");
    jsonErr.style.display = "none";
    jsonWrap.appendChild(jsonErr);
    jsonArea.addEventListener("input", () => {
        const parsed = parsePlan(jsonArea.value);
        if (parsed === null) {
            jsonErr.textContent = "Not valid JSON — the cards are showing the last good version.";
            jsonErr.style.display = "";
            return;
        }
        jsonErr.style.display = "none";
        shots = parsed;
        writeWidget();
        renderCards();
        onChange?.();
    });
    root.appendChild(jsonWrap);

    /* The vocabulary is fetched once per browser session. When it fails the
     * editor used to render with no directive controls at all and no
     * explanation -- `_failed` was set and never read. Surface it, with a
     * retry, and re-render every mounted editor when the retry succeeds. */
    const offline = el("div", "h3e-offline");
    offline.style.display = "none";
    root.insertBefore(offline, root.firstChild);

    function loadVocab() {
        return vocab().then((v) => {
            V = v;
            offline.textContent = "";
            if (v?._failed) {
                offline.style.display = "";
                const note = el("div", "h3e-note h3e-note-error",
                    "Directive vocabulary unavailable"
                    + (v._error ? ` (${v._error})` : "")
                    + " — dropdowns are empty until it loads. Beats and JSON still work.");
                offline.appendChild(note);
                offline.appendChild(button("Retry", "Re-fetch /h3_ref_chain/vocab",
                    () => { refreshVocab(); loadVocab(); }));
            } else {
                offline.style.display = "none";
            }
            renderCards();
        });
    }
    loadVocab();
    onVocabRefresh(() => { loadVocab(); });

    /* -- state plumbing -- */

    /* Which shot the filmstrip has selected. View state only -- the brief is
     * explicit that shot_plan and ref_plan stay the only sources of truth for
     * the PLAN, and a selection is neither written to the widget nor read by
     * run(). It lives in node.properties so it survives a re-render, a tab
     * switch and a workflow reload, exactly like the fold state above. */
    const SEL_PROP = "h3_film_selected";
    function selected() {
        const v = parseInt(node.properties?.[SEL_PROP], 10);
        return Number.isFinite(v) ? v : 0;
    }
    function setSelected(i) {
        node.properties ??= {};
        node.properties[SEL_PROP] = Math.max(0, i);
    }
    /** Coerce the selection back into range when shots are removed. */
    function clampSelection() {
        if (!shots.length) { setSelected(0); return; }
        if (selected() >= shots.length) setSelected(shots.length - 1);
    }

    /* Collapsed cards persist in node.properties so the state survives both a
     * re-render and a workflow reload. This is presentation state only -- it
     * never affects what run() sees, so it is not a second source of truth for
     * the plan itself. */
    function foldState() {
        const v = node.properties?.[FOLD_PROP];
        return (v && typeof v === "object") ? v : {};
    }
    function setFoldState(next) {
        if (!node.properties) node.properties = {};
        node.properties[FOLD_PROP] = next;
    }

    function mode() {
        const stored = node.properties?.[MODE_PROP];
        if (stored === "simple" || stored === "shots") return stored;
        // No stored preference: a saved plan means Shots, otherwise Simple.
        return shots.length ? "shots" : "simple";
    }

    function setMode(next) {
        node.properties ??= {};
        if (next === "simple" && shots.length) {
            // Simple mode has to clear shot_plan, or run() would ignore the
            // prompt and use a stale plan. Stash it first -- silently losing an
            // authored script to a toggle would be indefensible.
            node.properties[BACKUP_PROP] = planToJson(shots);
        }
        if (next === "shots" && !shots.length && node.properties[BACKUP_PROP]) {
            const restored = parsePlan(node.properties[BACKUP_PROP]);
            if (restored && restored.length) {
                shots = restored;
                node.properties[MODE_PROP] = next;
                commit();
                return;
            }
        }
        if (next === "shots" && !shots.length) {
            // Offer the migration plan.py already implements rather than
            // writing a second one here: one block + chains -> N shots.
            const text = String(promptWidget?.value || "").trim();
            const n = Math.max(1, parseInt(chainsWidget?.value, 10) || 1);
            if (text) {
                const blocks = text.split(/^\s*---\s*$/m).map((b) => b.trim()).filter(Boolean);
                const use = blocks.length > 1 ? blocks.slice(0, n) : [text];
                while (use.length < n) use.push("");
                shots = use.map((b, i) => ({
                    id: `s${i + 1}`, beat: b, directives: {}, prose: "",
                    seed: null, steps: null, duration: null, locked: false,
                }));
            } else {
                shots = [{ id: "s1", beat: "", directives: {}, prose: "",
                           seed: null, steps: null, duration: null, locked: false }];
            }
        }
        node.properties[MODE_PROP] = next;
        commit();
    }

    function writeWidget() {
        if (!planWidget) return;
        // In Simple mode the plan must be empty, or run() would ignore `prompt`
        // and silently use a stale plan — exactly the confusion this replaces.
        planWidget.value = mode() === "shots" ? planToJson(shots) : "";
        if (document.activeElement !== jsonArea) jsonArea.value = planWidget.value;
    }

    function commit() {
        writeWidget();
        renderCards();
        onChange?.();
    }

    /* -- render range -- */

    /* The two widgets the per-card ⏵ buttons drive. Read back rather than
     * cached, because the RUN panel draws the same two numbers and either side
     * may have moved them since the cards were last drawn. */
    function renderRange() {
        return {
            from: Number(widgetByName(node, "render_from")?.value) || 0,
            through: Number(widgetByName(node, "render_through")?.value) || 0,
        };
    }

    function setRenderRange(from, through) {
        const f = widgetByName(node, "render_from");
        const t = widgetByName(node, "render_through");
        // A node definition from before 1.1 has no render_from. Setting only
        // half a range would render from hop 1 to `through` and look like the
        // button did something subtly different rather than nothing.
        if (!f || !t) {
            console.warn("[HandTieClips] render_from/render_through missing -- "
                + "restart ComfyUI to pick up the 1.1 node definition");
            return;
        }
        f.value = from;
        t.value = through;
        f.callback?.(f.value);
        t.callback?.(t.value);
    }

    /** Arm or clear "render only shot N". Shared by the card's ⏵ and the
     *  timeline, so the two can never disagree about what solo means. */
    function toggleSolo(shotNo) {
        const now = renderRange();
        const solo = now.from === shotNo && now.through === shotNo;
        setRenderRange(solo ? 0 : shotNo, solo ? 0 : shotNo);
        renderCards();
        onChange?.();
    }

    /** Flip `locked` on shot `index` (0-based). Same path as the card's button. */
    function toggleLock(index) {
        const shot = shots[index];
        if (!shot) return;
        if (shot.locked) delete shot.locked;
        else shot.locked = true;
        commit();
    }

    /** Append a shot that continues the chain; returns its 0-based index.
     *  `freeId`, not `s${n}`: after a delete or a reorder `s${n}` can already
     *  be taken, and two shots sharing an id share a cache pointer. */
    function appendShot({ extend = false } = {}) {
        const shot = blankShot(shots.length);
        shot.id = freeId();
        if (shots.length) shot.directives = { join: "continuous" };
        // "+ extend" after a render: under hand-tie this shot opens its own
        // previs, continuing the film, so nothing already rendered moves.
        if (shots.length && extend) shot.anchor = "extend";
        shots.push(shot);
        commit();
        return shots.length - 1;
    }

    /** Delete shot `index` (0-based). The card's × and the timeline both call it. */
    function removeShot(index) {
        if (!shots[index]) return;
        shots.splice(index, 1);
        commit();
    }

    /** The SCRIPT card for shot `index`, unfolded, for editing elsewhere.
     *  The same builder the detail pane uses, so both write the same
     *  `shot_plan`. After D2 this also SELECTS the shot: a TIMELINE block
     *  click has to land the caret in that shot's beat, and the card the user
     *  can reach is now the one in the detail pane. */
    function cardFor(index) {
        if (!shots[index]) return null;
        setSelected(index);
        renderCards();
        return detail.firstChild || null;
    }

    /** 1-based shot number against the range. 0 on either end means open. */
    function inRange(shotNo, { from, through }) {
        return (!from || shotNo >= from) && (!through || shotNo <= through);
    }

    /* -- timing -- */

    function timing() {
        const durs = V?.durations || {};
        const overs = V?.overlaps || {};
        const fps = V?.fps || 24;
        const baseDur = durationWidget?.value;
        const ov = overs[overlapWidget?.value] ?? 0;
        const lengths = shots.map((s) => durs[s.duration || baseDur] ?? durs[baseDur] ?? 0);
        // A restart is a chain START: it relays nothing, so nothing is trimmed
        // off it. Charging every hop after the first -- `(shots.length - 1)`,
        // which is what stood here -- reads SHORT by `overlap` per restart, and
        // short is the dangerous direction: `timing()` also draws the
        // master-audio window via `chainSeconds`, so the window looked narrower
        // than the audio the run would actually consume, and the room left for
        // more shots looked bigger than it was. Same rule as
        // `audio_lock.master_frame_count`, which is the one run() obeys.
        const trims = shots.reduce(
            (n, s, i) => n + (i > 0 && s.anchor !== "restart" ? 1 : 0), 0);
        const total = lengths.reduce((a, b) => a + b, 0) - ov * trims;
        return { lengths, ov, fps, total: Math.max(0, total) };
    }

    function clock(sec) {
        if (!Number.isFinite(sec) || sec <= 0) return "—";
        return `${sec.toFixed(1)}s`;
    }

    /* -- rendering -- */

    function renderCard(shot, index) {
        const card = el("div", "h3e-card");
        card.dataset.index = String(index);

        const bar = el("div", "h3e-card-head");
        const drag = el("span", "h3e-drag", "⠿");
        drag.title = "Drag to reorder";
        drag.draggable = true;
        drag.addEventListener("dragstart", (e) => {
            e.dataTransfer.setData("text/plain", String(index));
            e.dataTransfer.effectAllowed = "move";
            // The stylesheet has had `.h3e-card.h3e-dragging { opacity: .45 }`
            // since the drag was written and nothing ever set the class, so the
            // card being moved looked identical to the ones standing still.
            card.classList.add("h3e-dragging");
        });
        drag.addEventListener("dragend", () => card.classList.remove("h3e-dragging"));
        bar.appendChild(drag);

        // Collapse state is keyed by shot id, not index, so it survives a
        // reorder as well as a re-render.
        const key = shot.id || `s${index + 1}`;
        const folded = Boolean(foldState()[key]);
        if (folded) card.classList.add("h3e-collapsed");
        const fold = button("", folded ? "Expand this shot" : "Collapse this shot", () => {
            const st = foldState();
            st[key] = !st[key];
            setFoldState(st);
            card.classList.toggle("h3e-collapsed");
            fold.title = card.classList.contains("h3e-collapsed")
                ? "Expand this shot" : "Collapse this shot";
        }, "h3e-btn h3e-fold");
        fold.appendChild(el("span", "h3e-fold-mark", "▾"));
        bar.appendChild(fold);

        bar.appendChild(el("span", "h3e-num", String(index + 1)));

        const t = timing();
        const frames = t.lengths[index] || 0;
        const delivered = index === 0 ? frames : Math.max(0, frames - t.ov);
        const badge = el("span", "h3e-timing", `${clock(delivered / t.fps)}`);
        badge.title = index === 0
            ? `${frames} frames.`
            : `${frames} frames, ${t.ov} dropped into the previous hop's tail.`;
        bar.appendChild(badge);

        // Collapsed cards still need to say which shot they are.
        const peekText = (shot.beat || "").replace(/\s+/g, " ").trim();
        const peek = el("span", "h3e-peek", peekText.length > 70
            ? peekText.slice(0, 67) + "…" : (peekText || "(continues)"));
        peek.title = peekText;
        bar.appendChild(peek);

        // Per-shot render control. These write `render_from` / `render_through`,
        // which have always been able to do this -- there was just nowhere to
        // say it except two integer boxes at the bottom of the RUN panel, where
        // you had to count your own shots to use them.
        const range = renderRange();
        const shotNo = index + 1;
        if (range.from || range.through) {
            card.classList.toggle("h3e-outside",
                                  !inRange(shotNo, range));
        }
        const only = button("⏵", `Render only shot ${shotNo}: everything before `
            + "it is replayed from the hop cache and everything after is left "
            + "alone. Needs cache_hops=on. Click again to clear.",
            () => toggleSolo(shotNo), "h3e-btn h3e-only");
        only.classList.toggle("h3e-on",
                              range.from === shotNo && range.through === shotNo);
        bar.appendChild(only);

        // Was a read-only badge that appeared once `locked` was set in the JSON
        // and offered no way to set or clear it.
        const lock = button("lock", "Reuse this shot's cached render even when "
            + "its inputs change -- freeze it while you work on the others. "
            + "Needs cache_hops=on.",
            () => toggleLock(index), "h3e-btn h3e-lock");
        lock.classList.toggle("h3e-on", Boolean(shot.locked));
        bar.appendChild(lock);
        bar.appendChild(button("×", "Delete this shot", () => removeShot(index),
                               "h3e-btn h3e-x"));
        card.appendChild(bar);

        card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("h3e-drop"); });
        card.addEventListener("dragleave", () => card.classList.remove("h3e-drop"));
        card.addEventListener("drop", (e) => {
            e.preventDefault();
            card.classList.remove("h3e-drop");
            const from = parseInt(e.dataTransfer.getData("text/plain"), 10);
            if (!Number.isFinite(from) || from === index) return;
            const [moved] = shots.splice(from, 1);
            shots.splice(index, 0, moved);
            commit();
        });

        const body = el("div", "h3e-card-body");
        const beat = el("textarea", "h3e-beat");
        beat.value = shot.beat || "";
        beat.rows = 3;
        beat.placeholder = index === 0
            ? "The whole opening: who, where, what they are doing."
            : "Only what is NEW this hop. The identity lock and the join are added for you.";
        beat.title = index === 0
            ? "Shot 1 is the full establishing prompt."
            : "Shots after the first are the new beat only — do not re-describe the face or replay the scene.";
        // Write on every keystroke, not on "change". A textarea only fires
        // "change" on blur, so saving the workflow with the caret still in the
        // box silently discarded everything typed since the last blur.
        beat.addEventListener("input", () => { shot.beat = beat.value; writeWidget(); });
        beat.addEventListener("change", () => { writeWidget(); onChange?.(); });
        body.appendChild(beat);

        // A7: the beat's word count against the band for its length, e.g.
        // "47 / 35-60 words". The band comes from /vocab, which reads it out of
        // the writer's own prompt table via planner.beat_table() -- so this is
        // the number the model was ASKED for, not a second set of numbers typed
        // into JavaScript and left to drift.
        //
        // Word count is `split()` on whitespace, the same rule count_beat uses
        // on the Python side. A CJK beat undercounts by the same factor on both
        // sides, which keeps the two honest about each other even though the
        // absolute number is low; the LINE count is the one to read there.
        const wordCount = el("div", "h3e-beatcount");
        body.appendChild(wordCount);
        const paintCount = () => {
            const label = shot.duration || durationWidget?.value || "";
            const band = (V?.beat_table || {})[label];
            const n = String(shot.beat || "").trim().split(/\s+/).filter(Boolean).length;
            if (!band) {
                wordCount.textContent = n ? `${n} words` : "";
                wordCount.classList.remove("h3e-over");
                wordCount.title = band ? "" : "No band for this length.";
                return;
            }
            const [w0, w1] = band;
            const over = n > w1 || (n > 0 && n < w0);
            wordCount.textContent = `${n} / ${w0}-${w1} words`;
            // Over the ceiling is the one that matters -- a beat past the band
            // is a hop whose dialogue overruns it (planner's SPEECH_MAX_SHARE
            // lint). Under is shown in the neutral colour, not a warning: a
            // short beat that names silence is correct, and flagging it would
            // push the user to pad.
            wordCount.classList.toggle("h3e-over", over);
            wordCount.title = over
                ? `Outside the ${w0}-${w1} word band for a ${label} hop.`
                : "";
        };
        beat.addEventListener("input", paintCount);
        paintCount();

        // directive row
        const dirs = el("div", "h3e-dirs");
        for (const axis of (V?.axes || [])) {
            // join has nothing to attach to on hop 1 — directive_prose skips it.
            if (axis === "join" && index === 0) continue;
            const opts = Object.keys(V.vocab[axis] || {});
            const wrap = el("label", "h3e-dir");
            wrap.appendChild(el("span", "h3e-dir-label", axis));
            const def = V.defaults?.[axis];
            const sel = select(opts, shot.directives[axis] || "", (v) => {
                shot.directives[axis] = v;
                commit();
            }, {
                blankLabel: def ? `${def.replace(/_/g, " ")} (default)` : "—",
                titles: V.vocab[axis] || {},
            });
            sel.title = `Hover an option to read the exact sentence it puts in the prompt.`;
            wrap.appendChild(sel);
            dirs.appendChild(wrap);
        }
        body.appendChild(dirs);

        for (const [text, cls] of [[coherenceWarning(shot, index, shots), "h3e-note h3e-note-hint"],
                                   [directionWarning(shot), "h3e-note"]]) {
            if (text) body.appendChild(el("div", cls, text));
        }
        if (directionWarning(shot)) card.classList.add("h3e-invalid");

        // advanced
        const adv = el("details", "h3e-adv");
        adv.appendChild(el("summary", null, "advanced"));
        const grid = el("div", "h3e-grid");

        const num = (label, key, placeholder, hint) => {
            const l = el("label", "h3e-field");
            l.appendChild(el("span", null, label));
            const i = el("input", "h3e-input");
            i.type = "number";
            i.value = shot[key] == null ? "" : String(shot[key]);
            i.placeholder = placeholder;
            i.title = hint;
            i.addEventListener("input", () => {
                shot[key] = i.value === "" ? null : Number(i.value);
                writeWidget();
            });
            i.addEventListener("change", () => {
                shot[key] = i.value === "" ? null : Number(i.value);
                commit();
            });
            l.appendChild(i);
            return l;
        };
        grid.appendChild(num("seed", "seed", "chain seed", "Override the seed for this hop only."));
        grid.appendChild(num("steps", "steps", "chain steps", "Override the step count for this hop only."));

        const durL = el("label", "h3e-field");
        durL.appendChild(el("span", null, "duration"));
        const durSel = select(Object.keys(V?.durations || {}), shot.duration || "", (v) => {
            shot.duration = v || null;
            // The band is per-length, so changing the length changes the
            // target the same beat is measured against.
            paintCount();
            commit();
        }, { blankLabel: `${durationWidget?.value || "chain"} (default)` });
        durSel.title = "Override this hop's length. Must be longer than the overlap.";
        durL.appendChild(durSel);
        grid.appendChild(durL);

        const lockL = el("label", "h3e-field h3e-check");
        const cb = el("input");
        cb.type = "checkbox";
        cb.checked = Boolean(shot.locked);
        cb.title = "Pin this hop to its cached render regardless of what changed. Needs cache_hops=on.";
        cb.addEventListener("change", () => { shot.locked = cb.checked; commit(); });
        lockL.appendChild(cb);
        lockL.appendChild(el("span", null, "locked"));
        grid.appendChild(lockL);

        // anchor=restart. Hidden on shot 1, where it is invalid: hop 1 is
        // already a chain start.
        if (index > 0) {
            const anchorL = el("label", "h3e-field");
            anchorL.appendChild(el("span", null, "anchor"));
            const anchorS = el("select", "h3e-input");
            for (const [v, label] of [["", "relay (default)"], ["restart", "restart"], ["extend", "extend (hand-tie: own previs)"]]) {
                const o = el("option", null, label);
                o.value = v;
                anchorS.appendChild(o);
            }
            anchorS.value = shot.anchor || "";
            anchorS.title =
                "restart: re-anchor the chain on the start image at this hop "
                + "instead of relaying the previous one. Bounds the quality "
                + "decay on a long chain -- every hop otherwise inherits its "
                + "predecessor's most settled frames. It is a CUT: the hop "
                + "opens on the start image's pose, so set join to hard_cut "
                + "and write the beat as a fresh start. Needs a start image.";
            anchorS.addEventListener("change", () => {
                shot.anchor = anchorS.value;
                commit();
            });
            anchorL.appendChild(anchorS);
            grid.appendChild(anchorL);
        }

        const refsL = el("label", "h3e-field");
        refsL.appendChild(el("span", null, "refs"));
        const refsI = el("input", "h3e-input");
        refsI.type = "text";
        refsI.value = shot.refs == null
            ? ""
            : (shot.refs.length ? shot.refs.map((t) => "@" + t).join(" ") : "none");
        refsI.placeholder = "register default";
        refsI.title =
            "Which stills ride this hop. Blank = register default "
            + "(unscheduled stills on chain starts, off continuations). "
            + "'none' = no stills -- the choice that drops identity photos "
            + "on a pin-less restart. @tag @tag = these only, in that order.";
        const parseRefsField = (raw) => {
            const t = String(raw || "").trim();
            if (!t) return null;
            if (t.toLowerCase() === "none" || t === "-") return [];
            return t.split(/[\s,]+/).map((x) => x.replace(/^@/, "").trim()).filter(Boolean);
        };
        refsI.addEventListener("input", () => { shot.refs = parseRefsField(refsI.value); writeWidget(); });
        refsI.addEventListener("change", () => { shot.refs = parseRefsField(refsI.value); commit(); });
        refsL.appendChild(refsI);
        grid.appendChild(refsL);

        const idL = el("label", "h3e-field");
        idL.appendChild(el("span", null, "id"));
        const idI = el("input", "h3e-input");
        idI.type = "text";
        idI.value = shot.id || "";
        idI.title = "Stable name, used as the cache pointer for `locked`.";
        idI.addEventListener("input", () => { shot.id = idI.value.trim() || `s${index + 1}`; writeWidget(); });
        idI.addEventListener("change", () => { shot.id = idI.value.trim() || `s${index + 1}`; commit(); });
        idL.appendChild(idI);
        grid.appendChild(idL);
        adv.appendChild(grid);

        const prose = el("textarea", "h3e-beat");
        prose.rows = 2;
        prose.value = shot.prose || "";
        prose.placeholder = "prose appended verbatim (for anything the directives lack)";
        prose.title = "Added to the end of this hop's body, unmodified. Affirmative phrasing only — "
            + "sampling runs at cfg 1.0, so anything named is added, never subtracted.";
        prose.addEventListener("input", () => { shot.prose = prose.value; writeWidget(); });
        prose.addEventListener("change", () => { writeWidget(); onChange?.(); });
        adv.appendChild(prose);

        // pin_mech=hand_tie only, and optional: blank, the previs uses each
        // beat's first sentence, so a normal plan needs nothing here. In
        // `advanced` because hand-tie must not ask for more input than any
        // other plan (user 2026-10-09). A written value is kept in every mode.
        if (String(widgetByName(node, "pin_mech")?.value || "") === "hand_tie") {
            const momentBox = (key, placeholder, hint) => {
                const t = el("textarea", "h3e-beat h3e-moment");
                t.rows = 2;
                t.value = shot[key] || "";
                t.placeholder = placeholder;
                t.title = hint;
                t.addEventListener("input", () => { shot[key] = t.value; writeWidget(); });
                t.addEventListener("change", () => { writeWidget(); onChange?.(); });
                return t;
            };
            adv.appendChild(momentBox("moment",
                index === 0
                    ? "moment: the film's first frame (blank = the beat's first sentence)"
                    : "moment: what is on screen as this hop starts (blank = the beat's first sentence)",
                "hand-tie: the previs shows this at the start of this hop, and this hop and the one "
                + "before it are tied to it. A pose and a place, one sentence. A spoken line that "
                + "crosses the join goes here, once, as <d>[English] ...</d> -- the node splits it "
                + "between the two hops. Do not repeat it in the beat."));
            if (index === shots.length - 1) {
                adv.appendChild(momentBox("moment_end",
                    "moment end: what is on screen as the film ends (blank = the beat's last sentence)",
                    "hand-tie: the previs ends on this, and the last hop is tied to it. A line here "
                    + "is spoken so that it ends with the film."));
            }
        }

        body.appendChild(adv);
        card.appendChild(body);

        return card;
    }

    /* -- D2: the filmstrip ----------------------------------------------------- */

    /* Where the thumbnails come from. The route serves a sprite for a hop the
     * last run rendered; anything else draws the slate alone. Never
     * HopStore.get() -- that builds a ~3 GB float copy of a whole hop in
     * memory, which is what the timeline's frames() route exists to avoid.
     *
     * The keys arrive from the last run's preview event, which the TIMELINE
     * already receives and keeps. They are read through `getHopKeys`, which
     * the host wires up after construction -- the timeline is built after this
     * editor, so a direct reference would be a cycle. Absent that, every
     * poster draws its slate, which is the correct answer for a plan that has
     * never been rendered. */
    const SPRITE_ROUTE = "/h3_ref_chain/hop_sprite";
    let getHopKeys = null;
    function setHopKeySource(fn) { getHopKeys = fn; }
    function hopKeyAt(i) {
        try {
            const keys = getHopKeys?.();
            return Array.isArray(keys) ? (keys[i] || null) : null;
        } catch (_) {
            return null;
        }
    }
    function spriteFor(hopKey) {
        if (!hopKey) return "";
        return `${SPRITE_ROUTE}?key=${encodeURIComponent(hopKey)}&n=24`;
    }

    /* One mini poster: slate number, framing + duration, the beat's first
     * words, and the two controls that have to be reachable per shot without
     * opening it (Solo and Lock). Everything else is in the detail pane. */
    function renderPoster(shot, index) {
        const shotNo = index + 1;
        const card = el("div", "h3e-poster" + (index === selected() ? " h3e-on" : ""));

        const t = timing();
        const frames = t.lengths[index] || 0;
        const delivered = index === 0 ? frames : Math.max(0, frames - t.ov);

        // Thumbnail, if the last run rendered this hop.
        const thumb = el("div", "h3e-poster-thumb");
        const src = spriteFor(hopKeyAt(index));
        if (src) {
            const img = el("img", "h3e-poster-img");
            img.src = src;
            img.alt = "";
            img.loading = "lazy";
            img.draggable = false;
            img.title = `hop ${shotNo} as last rendered`;
            thumb.appendChild(img);
        } else {
            // No sprite: the slate alone, rather than a broken-image box.
            thumb.appendChild(el("span", "h3e-poster-noimg", "—"));
        }
        card.appendChild(thumb);

        // Slate number in tabular mono, so a column of them lines up.
        card.appendChild(el("span", "h3e-poster-no", String(shotNo).padStart(2, "0")));

        const d = shot.directives || {};
        const meta = [];
        if (d.framing) meta.push(d.framing);
        if (frames) meta.push(clock(delivered / t.fps));
        card.appendChild(el("span", "h3e-poster-meta", meta.join(" · ")));

        const peek = String(shot.beat || "").replace(/\s+/g, " ").trim();
        card.appendChild(el("span", "h3e-poster-beat",
            peek.length > 58 ? peek.slice(0, 58) + "…" : peek));

        // Solo / Lock, without opening the shot.
        const row = el("span", "h3e-poster-row");
        const range = renderRange();
        const inR = inRange(shotNo, range);
        const solo = button("solo", `Render only shot ${shotNo}`, (e) => {
            e?.stopPropagation?.();
            toggleSolo(shotNo);
            commit();
        }, "h3e-btn h3e-mini" + (range.from === shotNo && range.through === shotNo ? " h3e-on" : ""));
        solo.classList.toggle("h3e-soft", !inR && !(range.from === shotNo && range.through === shotNo));
        const lock = button(shot.locked ? "locked" : "lock",
            shot.locked ? `Shot ${shotNo} is locked: reuse its cached render` : `Lock shot ${shotNo}`,
            (e) => {
                e?.stopPropagation?.();
                toggleLock(index);
                commit();
            }, "h3e-btn h3e-mini" + (shot.locked ? " h3e-on" : ""));
        row.appendChild(solo);
        row.appendChild(lock);
        card.appendChild(row);

        // The whole poster selects. The click target is the card, not the
        // buttons above, so a shot with a long beat is still one click away.
        card.addEventListener("click", (e) => {
            if (e.target !== card && e.target.closest?.(".h3e-mini")) return;
            setSelected(index);
            renderCards();
        });
        card.title = `Shot ${shotNo}` + (shot.beat ? ` — ${String(shot.beat).slice(0, 120)}` : "");
        return card;
    }

    /* The dashed + that ends the strip. It appends through the SAME
     * appendShot() the header button and the TIMELINE's "+ extend" call, so
     * there is one definition of what appending a shot does -- including the
     * `join: continuous` that makes the new hop continue rather than restart. */
    function renderAddCard() {
        const c = el("button", "h3e-poster h3e-poster-add");
        c.type = "button";
        c.appendChild(el("span", "h3e-poster-plus", "+"));
        c.appendChild(el("span", "h3e-poster-no",
            String(shots.length + 1).padStart(2, "0")));
        c.title = "Add a shot. It continues the one before it.";
        c.addEventListener("click", () => {
            appendShot();
            setSelected(shots.length - 1);
            renderCards();
        });
        return c;
    }

    function renderStrip() {
        strip.textContent = "";
        if (broken || !shots.length) return;
        shots.forEach((s, i) => strip.appendChild(renderPoster(s, i)));
        strip.appendChild(renderAddCard());
    }

    /* The detail pane is the selected shot's card -- the same renderCard() the
     * old list used. Not rebuilt while the caret is inside it, because a
     * re-render under a live textarea eats the selection and the undo stack. */
    function renderDetail() {
        if (detailIsCurrent()) return;
        detail.textContent = "";
        const i = selected();
        if (broken) {
            detail.appendChild(el("div", "h3e-note h3e-note-error",
                "shot_plan is not valid JSON. Fix it in the JSON section below."));
            return;
        }
        if (!shots.length) {
            detail.appendChild(el("div", "h3e-empty",
                "No shots yet. The + at the end of the strip adds shot 1."));
            return;
        }
        if (!shots[i]) { setSelected(0); return renderDetail(); }
        const card = renderCard(shots[i], i);
        card.classList.remove("h3e-collapsed");
        // The detail card is always open; its own fold control would have
        // nothing to expand, since there is exactly one card down here.
        detail.appendChild(card);
        detailKey = shotKeyOf(shots[i], i);
    }
    let detailKey = null;
    function shotKeyOf(shot, i) { return `${shot.id || "s" + (i + 1)}:${selected()}`; }
    function detailIsCurrent() {
        if (!detailKey) return false;
        return detailKey === shotKeyOf(shots[selected()] || {}, selected());
    }

    function renderCards() {
        const m = mode();
        simpleBtn.classList.toggle("h3e-on", m === "simple");
        shotsBtn.classList.toggle("h3e-on", m === "shots");
        simpleNote.style.display = m === "simple" ? "" : "none";
        list.style.display = m === "simple" ? "none" : "";
        jsonWrap.style.display = m === "simple" ? "none" : "";
        addBtn.style.display = m === "simple" ? "none" : "";
        tplBtn.style.display = m === "simple" ? "none" : "";
        if (m === "simple") {
            templates.hide();
            tplBtn.classList.remove("h3e-on");
        }

        if (m === "simple") {
            // Re-read the widget unless the caret is in the box: a loaded or
            // pasted workflow changes `prompt` underneath us.
            if (document.activeElement !== simpleArea) {
                simpleArea.value = String(promptWidget?.value ?? "");
            }
            updateRepeatHint();
            summary.textContent = `${chainsWidget?.value || 1} hop(s)`;
            return;
        }

        list.textContent = "";
        if (broken) {
            const note = el("div", "h3e-note h3e-note-error",
                "shot_plan is not valid JSON. Fix it in the JSON section below and the cards will come back.");
            list.appendChild(note);
            return;
        }
        if (!shots.length) {
            const empty = el("div", "h3e-empty",
                "No shots yet. Shot 1 is the whole opening; every shot after it is only what happens next.");
            empty.appendChild(button("+ Add shot 1", "Add the opening shot", () => {
                shots.push(blankShot(shots.length));
                commit();
            }, "h3e-btn h3e-empty-cta"));
            list.appendChild(empty);
        }
        clampSelection();
        // D2: the strip and the one card under it, rather than every card.
        renderStrip();
        renderDetail();

        const t = timing();
        summary.textContent = shots.length
            ? `${shots.length} shots · ${clock(t.total / t.fps)} master`
            : "empty";
    }

    /** Re-read the widget after an external change (workflow load, undo). */
    function reload() {
        const parsed = parsePlan(planWidget?.value);
        broken = parsed === null;
        if (!broken) shots = parsed;
        if (document.activeElement !== jsonArea) jsonArea.value = planWidget?.value || "";
        renderCards();
    }

    // `timing` is exported so the master-audio window can be drawn the width
    // of the chain. It reads the LIVE card list, not the shot_plan widget, so
    // the box resizes as shots are added rather than on the next accept.
    // The timeline reads the live card list and drives solo and lock through
    // the same functions the cards use -- no second copy of either rule.
return { root, render: renderCards, reload, mode, timing,
               shots: () => shots, renderRange, toggleSolo, toggleLock,
               appendShot, removeShot, cardFor, setHopKeySource,
               selected, setSelected };
}
