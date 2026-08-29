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
        return o;
    });
    return JSON.stringify({ shots: out }, null, 2);
}

/**
 * Mirrors plan.check_coherence. A framing change asks the audience to be
 * somewhere new; with a held camera the only way to get there is a cut, so
 * `continuous` and the framing change want opposite things.
 */
function coherenceWarning(shot, index) {
    if (index === 0) return null;
    const d = shot.directives || {};
    const framing = d.framing || "";
    const camera = d.camera || "";
    if (d.join === "continuous" && framing && framing !== "keep" && (!camera || camera === "hold")) {
        return `join=continuous with framing=${framing} and a held camera implies a cut. `
             + "Use push_in / pull_back / pan_follow to reach that framing on the move, or framing=keep.";
    }
    return null;
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

    const simpleNote = el("div", "h3e-empty",
        "Simple mode: the prompt widget below drives the run, repeated across `chains` hops "
        + "(set under RUN). Switch to Shots to write one beat per hop with camera and "
        + "join directives.");
    root.appendChild(simpleNote);

    const list = el("div", "h3e-shots");
    root.appendChild(list);

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

    /* -- timing -- */

    function timing() {
        const durs = V?.durations || {};
        const overs = V?.overlaps || {};
        const fps = V?.fps || 24;
        const baseDur = durationWidget?.value;
        const ov = overs[overlapWidget?.value] ?? 0;
        const lengths = shots.map((s) => durs[s.duration || baseDur] ?? durs[baseDur] ?? 0);
        const total = lengths.reduce((a, b) => a + b, 0) - ov * Math.max(0, shots.length - 1);
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
        });
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

        if (shot.locked) {
            const lock = el("span", "h3e-lock", "locked");
            lock.title = "Reuses this shot's cached render even when its inputs changed. Needs cache_hops=on.";
            bar.appendChild(lock);
        }
        bar.appendChild(button("×", "Delete this shot", () => {
            shots.splice(index, 1);
            commit();
        }, "h3e-btn h3e-x"));
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

        for (const [text, cls] of [[coherenceWarning(shot, index), "h3e-note h3e-note-hint"],
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
        body.appendChild(adv);
        card.appendChild(body);

        return card;
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
        shots.forEach((s, i) => list.appendChild(renderCard(s, i)));

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

    return { root, render: renderCards, reload, mode };
}
