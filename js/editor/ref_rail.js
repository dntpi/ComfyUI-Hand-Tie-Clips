/**
 * The reference rail: what each wired still is, and what it is for.
 *
 * Mirrors `refs.py`. Two things it surfaces that the console only told you
 * about after you queued:
 *
 *   - a ref whose picture is missing or never chosen (refs.check)
 *   - duplicate tags (parse_ref_plan raises on these, so a typo used to cost
 *     you a failed run instead of a red row)
 *   - a ref carried over from when references were wired to ref_image_N
 *     sockets, which cannot be migrated automatically: the socket held a
 *     tensor and there is no filename to recover, so the row says which
 *     slot it used to be and asks for the picture.
 *
 * The ordinal badge is the point of the whole module. Core assigns
 * `<Picture N>` by position, so pulling a Load Image out renumbers everything
 * after it; here the ordinal is derived and shown live, and prose refers to the
 * stable @tag instead.
 */

import { el, button, select, vocab, onVocabRefresh } from "./widget_utils.js";
import { createPicker } from "./media_picker.js";

const MAX_SLOTS = 9;

function blankRef(existing) {
    let n = existing.length + 1;
    const tags = new Set(existing.map((r) => r.tag));
    while (tags.has(`ref_${n}`)) n += 1;
    // slot is the row's position, same as parseRefPlan / refs.py. Leaving it
    // unset made a freshly added row with a picture still fail validate
    // (`wired.has(undefined)`), so the rail showed "no picture chosen" over
    // the thumbnail that was just picked.
    return { tag: `ref_${n}`, file: "", slot: n, subject: null, retention: "", desc: "" };
}

function reindex(plan) {
    (plan.refs || []).forEach((r, i) => { r.slot = i + 1; });
}

/** Drop subject blocks no remaining row claims.
 *
 *  × on the last ref used to leave `subjects` behind, so the JSON box
 *  stayed populated with a cook who no longer had a picture. */
function pruneSubjects(plan) {
    const used = new Set(
        (plan.refs || [])
            .filter((r) => r.subject != null)
            .map((r) => String(r.subject)),
    );
    for (const k of Object.keys(plan.subjects || {})) {
        if (!used.has(String(k))) delete plan.subjects[k];
    }
}

export function parseRefPlan(text) {
    // A widget value is not guaranteed to be a string. ComfyUI restores
    // `widgets_values` POSITIONALLY, so a node whose widget list changed can
    // hand this an int or a bool from a neighbouring widget. Coerce rather
    // than throw: a wrong-typed plan should show the JSON tab, not abort the
    // whole workflow load.
    const t = String(text ?? "").trim();
    if (!t) return { refs: [], subjects: {} };
    let data;
    try {
        data = JSON.parse(t);
    } catch (_) {
        return null; // caller keeps the raw text and shows the JSON tab
    }
    // Same guard as plan_editor: valid JSON that is not a plan.
    if (data === null || typeof data !== "object") return null;
    const refs = Array.isArray(data) ? data : (data.refs || []);
    const subjects = (Array.isArray(data) ? {} : (data.subjects || {})) || {};
    return {
        refs: refs.map((r, i) => ({
            tag: String(r.tag || "").replace(/^@/, ""),
            file: String(r.file || ""),
            // A plan authored against the sockets carries a slot and no file.
            // Kept only so the row can say what it used to be plugged into.
            legacy_slot: (!r.file && r.slot != null) ? Number(r.slot) : null,
            // Derived, never authored -- mirrors refs.py.
            slot: i + 1,
            subject: r.subject == null ? null : Number(r.subject),
            retention: r.retention || "",
            desc: r.desc || "",
            shots: Array.isArray(r.shots) ? r.shots.slice() : null,
            mp: r.mp == null ? 0 : Number(r.mp) || 0,
        })),
        subjects,
    };
}

/* Offered pixel budgets, in MP. The floor mirrors refs.REF_MP_MIN -- below it
 * you can no longer tell a room from another room. There is no ceiling entry:
 * "full" (no cap) is the blank option. */
const REF_MP = [0.3, 0.5, 0.7, 1.0, 1.5, 2.0];

export function refPlanToJson(plan) {
    if (!plan.refs.length && !Object.keys(plan.subjects).length) return "";
    const refs = plan.refs.map((r) => {
        // `slot` is deliberately not written back: it is derived from row
        // order on both sides, and persisting it would resurrect the socket
        // number a legacy plan is trying to leave behind.
        const out = { tag: r.tag };
        if (r.file) out.file = r.file;
        else if (r.legacy_slot) out.slot = r.legacy_slot;
        if (r.subject != null) out.subject = r.subject;
        if (r.retention) out.retention = r.retention;
        if (r.desc) out.desc = r.desc;
        if (r.shots && r.shots.length) out.shots = r.shots;
        // Omitted when uncapped, so a plan authored before this existed round
        // trips byte-identical and no diff appears from merely opening it.
        if (r.mp) out.mp = r.mp;
        return out;
    });
    const body = { refs };
    if (Object.keys(plan.subjects).length) body.subjects = plan.subjects;
    return JSON.stringify(body, null, 2);
}

/** Rows that actually name a picture. Only these take a <Picture N> ordinal. */
function wiredSlots(plan) {
    const out = new Set();
    (plan.refs || []).forEach((r, i) => {
        if (r.file) out.add(i + 1);
    });
    return out;
}

/**
 * Per-hop ordinals, exactly as `refs.active_refs` + `refs.ordinals` compute
 * them: slot order, restricted to slots with an image.
 */
function ordinalsFor(plan, wired) {
    const active = plan.refs
        .filter((r) => wired.has(r.slot))
        .sort((a, b) => a.slot - b.slot);
    const ords = new Map();
    active.forEach((r, i) => ords.set(r.tag, i + 1));
    return ords;
}

function validate(plan, wired) {
    const problems = new Map(); // tag -> message
    const seenTag = new Map();
    const seenSlot = new Map();
    for (const r of plan.refs) {
        if (!r.tag) {
            problems.set(r, "a tag is required — prose refers to it as @tag");
            continue;
        }
        if (seenTag.has(r.tag)) {
            problems.set(r, `@${r.tag} is already used — tags must be unique`);
        }
        seenTag.set(r.tag, r);
        if (seenSlot.has(r.slot)) {
            problems.set(r, `slot ${r.slot} is already taken by @${seenSlot.get(r.slot).tag}`);
        }
        seenSlot.set(r.slot, r);
        // The picture, not the slot. `wired` is derived from row index; a
        // freshly clicked-in row used to have file set and slot unset, so
        // this branch fired "no picture chosen" over a visible thumbnail.
        if (!String(r.file || "")) {
            problems.set(r, r.legacy_slot
                ? `was wired to ref_image_${r.legacy_slot}, which no longer exists `
                  + `— pick its picture to bring this ref back`
                : "no picture chosen — this ref is inactive");
        }
    }
    // A subject block nothing points at is continuity prose for someone who
    // never appears; parse_ref_plan rejects it outright.
    const used = new Set(plan.refs.filter((r) => r.subject != null).map((r) => String(r.subject)));
    const orphans = Object.keys(plan.subjects).filter((k) => !used.has(String(k)));
    return { problems, orphans };
}

export function createRefRail(node, { getPlan, setPlan, onChange, hopCount,
                                      getRaw, isBad }) {
    const root = el("div", "h3e-section");
    const head = el("div", "h3e-head");
    const title = el("span", "h3e-title", "REFERENCES");
    const count = el("span", "h3e-count");
    head.appendChild(title);
    head.appendChild(count);
    head.appendChild(el("span", "h3e-spacer"));
    head.appendChild(button("+ ref", "Add a reference row", () => {
        const plan = getPlan();
        plan.refs.push(blankRef(plan.refs));
        reindex(plan);
        setPlan(plan);
        render();
        onChange?.();
    }));
    root.appendChild(head);

    const list = el("div", "h3e-refs");
    root.appendChild(list);

    /* JSON escape hatch, mirroring the one in the SCRIPT section.
     *
     * Without it the rail was the ONLY editor for `ref_plan`, which made a
     * model-authored register unpastable: prompt_pack hands the author two
     * JSON documents and only `shot_plan` had a box to paste into.
     *
     * It also closes a quieter bug. `parseRefPlan` returns null on text it
     * cannot read, and the caller swapped in an empty plan -- so the next rail
     * edit wrote "" over the very text the author was trying to repair. The
     * paste was destroyed by the act of fixing it. Now the raw value stays in
     * the widget until something parses, and this box is where it shows.
     */
    const jsonWrap = el("details", "h3e-json-wrap");
    const jsonSum = el("summary", null, "JSON");
    jsonSum.title = "The literal ref_plan value. Editing here updates the rows.";
    jsonWrap.appendChild(jsonSum);
    const jsonArea = el("textarea", "h3e-json");
    jsonArea.spellcheck = false;
    jsonArea.placeholder = '{"refs": [{"tag": "hero_face", "file": "face.jpg", '
        + '"subject": 1, "retention": "fully_preserved", "shots": [1, 2]}], '
        + '"subjects": {"1": {"name": "the cook", "locked": "..."}}}';
    jsonWrap.appendChild(jsonArea);
    const jsonErr = el("div", "h3e-note");
    jsonErr.style.display = "none";
    jsonWrap.appendChild(jsonErr);
    jsonArea.addEventListener("input", () => {
        const parsed = parseRefPlan(jsonArea.value);
        if (parsed === null) {
            jsonErr.textContent =
                "Not valid JSON -- the rows are showing the last good version.";
            jsonErr.classList.add("h3e-note-error");
            jsonErr.style.display = "";
            return;
        }
        jsonErr.style.display = "none";
        jsonErr.classList.remove("h3e-note-error");
        setPlan(parsed);
        onChange?.();
        render();
    });
    root.appendChild(jsonWrap);

    let V = null;
    vocab().then((v) => { V = v; render(); });
    // Re-render when a retry in the plan editor reloads the vocabulary.
    onVocabRefresh(() => vocab().then((v) => { V = v; render(); }));

    function commit() {
        setPlan(getPlan());
        onChange?.();
        render();
    }

    function renderSubject(num) {
        const plan = getPlan();
        const s = plan.subjects[num] || (plan.subjects[num] = { name: "", locked: "", context: "" });
        const box = el("div", "h3e-subject");
        box.appendChild(el("span", "h3e-subj-badge", `Subject ${num}`));

        const mk = (key, placeholder, hint) => {
            const i = el("input", "h3e-input");
            i.type = "text";
            i.value = s[key] || "";
            i.placeholder = placeholder;
            i.title = hint;
            i.addEventListener("input", () => { s[key] = i.value; setPlan(plan); onChange?.(); });
            return i;
        };
        box.appendChild(mk("name", "what to call them",
            "Used as “<Subject N> is <name>, the person in <Picture …>”. A role works better than a proper name."));
        box.appendChild(mk("locked", "traits that must not drift",
            "Injected verbatim on every hop. Only put things that are actually in the photograph here — at cfg 1.0 every word is additive."));
        box.appendChild(mk("context", "current standing state",
            "Same as locked but less rigid: what they are wearing right now, current mood."));
        return box;
    }

    function render() {
        list.textContent = "";
        // Never while focused: it would reformat mid-keystroke and steal the caret.
        if (document.activeElement !== jsonArea) jsonArea.value = getRaw?.() ?? "";
        const plan = getPlan();
        if (!plan) return;
        reindex(plan);
        if (isBad?.()) {
            jsonWrap.open = true;
            list.appendChild(el("div", "h3e-note h3e-note-error",
                "ref_plan is not valid JSON. Fix it in the JSON section below "
                + "and the rows will come back."));
        }
        const wired = wiredSlots(plan);
        const ords = ordinalsFor(plan, wired);
        const { problems, orphans } = validate(plan, wired);
        count.textContent = `${plan.refs.length} · ${wired.size} with a picture`;

        if (!plan.refs.length) {
            const empty = el("div", "h3e-empty",
                "No references. Add one per picture, then write @tag in your beats.");
            list.appendChild(empty);
        }

        const bySubject = new Map();
        for (const r of plan.refs) {
            const row = el("div", "h3e-ref");
            const bad = problems.get(r);
            if (bad) row.classList.add("h3e-bad");
            if (!wired.has(r.slot)) row.classList.add("h3e-inactive");

            const ord = ords.get(r.tag);
            const badge = el("span", "h3e-ord", ord ? String(ord) : "–");
            badge.title = ord
                ? `Resolves to <Picture ${ord}> on every hop this ref is active.`
                : "No picture, so it takes no ordinal this run.";
            row.appendChild(badge);

            const tag = el("input", "h3e-input h3e-tag");
            tag.type = "text";
            tag.value = "@" + r.tag;
            tag.title = "Stable name. Write this in a beat and it resolves to the right picture per hop.";
            // Push to the widget per keystroke; commit() re-renders and would
            // steal the caret, so only the blur handler may call it.
            tag.addEventListener("input", () => {
                r.tag = tag.value.trim().replace(/^@/, "").replace(/[^A-Za-z0-9_]/g, "_");
                setPlan(plan);
            });
            tag.addEventListener("change", commit);
            row.appendChild(tag);

            // Was a `slot N` dropdown naming one of nine IMAGE sockets. The
            // picture itself lives here now: drop a file on it, click to
            // browse, or choose one already in the reference folder.
            const pick = createPicker({
                kind: "image",
                get: () => r.file || "",
                set: (v) => {
                    r.file = v;
                    // Choosing a picture is what retires a legacy row, so the
                    // "was wired to slot N" note has to go with it.
                    if (v) r.legacy_slot = null;
                },
                // Rebuild on a later turn. `commit` tears down this <select>
                // in the middle of its own `change`; the browser then fires
                // again with value "", which used to wipe `file` and leave
                // the red "no picture chosen" banner on a row that just
                // showed a thumbnail. Paste-JSON never hit this because it
                // never goes through the dropdown.
                onChange: () => queueMicrotask(() => commit()),
                title: "The picture this reference stands for. Drop a file, or click to browse.",
            });
            pick.root.classList.add("h3e-slot");
            row.appendChild(pick.root);

            const subj = el("select", "h3e-select h3e-subj");
            const none = el("option", null, "setting/prop");
            none.value = "";
            subj.appendChild(none);
            for (let i = 1; i <= 4; i += 1) {
                const o = el("option", null, `Subject ${i}`);
                o.value = String(i);
                subj.appendChild(o);
            }
            subj.value = r.subject == null ? "" : String(r.subject);
            subj.title = "Group pictures of the SAME person under one subject number. "
                + "Two different people sharing a number makes the model render the average of their faces.";
            subj.addEventListener("change", () => {
                r.subject = subj.value === "" ? null : Number(subj.value);
                if (!r.retention) r.retention = r.subject ? "fully_preserved" : "reference";
                commit();
            });
            row.appendChild(subj);

            const retOpts = Object.keys(V?.retention || {});
            const ret = select(retOpts, r.retention, (v) => { r.retention = v; commit(); }, {
                blankLabel: r.subject ? "fully preserved" : "reference",
                titles: V?.retention || {},
            });
            ret.classList.add("h3e-ret");
            ret.title = "How much of this picture carries over. Hover an option for the exact sentence.";
            row.appendChild(ret);

            /* Pixel budget for THIS picture.
             *
             * A token dial, not a quality one: H3 turns each reference into
             * latent_h*latent_w entries in the DiT payload and attends over all
             * of them on every step of every hop, so a location plate costing
             * what a face costs is waste.
             *
             * "full" is the top and it is not a number, because H3 only ever
             * scales a reference DOWN -- a cap above the file's own size is a
             * dial wired to nothing, and a number there would imply upscaling
             * that never happens. */
            /* A cap that is not one of the offered values still has to SHOW.
             * `select` assigns a value matching no option, which renders the
             * control blank -- and blank here reads as "full", so a row capped
             * at 0.54 looked uncapped while it was working. Offer the row's
             * own value alongside the standard ones. */
            const mpOpts = REF_MP.map(String);
            if (r.mp && !mpOpts.includes(String(r.mp))) {
                mpOpts.push(String(r.mp));
                mpOpts.sort((a, b) => Number(a) - Number(b));
            }
            const mp = select(mpOpts, r.mp ? String(r.mp) : "", (v) => {
                r.mp = v ? Number(v) : 0;
                commit();
            }, {
                blankLabel: "full",
                titles: Object.fromEntries(mpOpts.map((v) =>
                    [String(v), `Cap this picture at ${v} MP before the encoder sees it.`])),
            });
            mp.classList.add("h3e-mp");
            mp.title = "Pixel budget for this reference. Lower is fewer tokens and a "
                + "faster step; 'full' leaves the file alone. Feed a face big and a "
                + "location plate small.";
            row.appendChild(mp);

            const desc = el("input", "h3e-input h3e-desc");
            desc.type = "text";
            desc.value = r.desc || "";
            desc.placeholder = "describe this photo";
            desc.title = "Goes into retention_analysis verbatim. Describe the picture you actually wired — "
                + "at cfg 1.0 a detail that is not there is asked for, not ignored.";
            desc.addEventListener("input", () => { r.desc = desc.value; setPlan(plan); });
            desc.addEventListener("change", commit);
            row.appendChild(desc);

            row.appendChild(button("×", "Remove this reference", () => {
                const p = getPlan();
                p.refs.splice(p.refs.indexOf(r), 1);
                pruneSubjects(p);
                reindex(p);
                setPlan(p);
                onChange?.();
                render();
            }, "h3e-btn h3e-x"));

            /* Which hops this photograph rides. An empty schedule means hop 1
             * only on a `next` chain -- that is the pin-only recipe
             * chain_00037/00038 were rendered with, and until now it could
             * only be set by hand in the JSON. */
            const hops = Math.max(1, Number(hopCount?.() || 1));
            const chips = el("div", "h3e-chips");
            chips.appendChild(el("span", "h3e-chips-label", "rides hops"));
            for (let h = 1; h <= hops; h += 1) {
                const on = Array.isArray(r.shots) && r.shots.includes(h);
                const chip = el("span", "h3e-chip" + (on ? " h3e-chip-on" : ""), String(h));
                chip.title = on
                    ? `This photograph is sent to the encoder on hop ${h}. Click to remove it.`
                    : `Click to also send this photograph on hop ${h}. `
                      + "Leave every hop off to let the motion pin carry wardrobe and room.";
                chip.addEventListener("click", () => {
                    const cur = Array.isArray(r.shots) ? r.shots.slice() : [];
                    const at = cur.indexOf(h);
                    if (at >= 0) cur.splice(at, 1); else cur.push(h);
                    cur.sort((a, b) => a - b);
                    r.shots = cur.length ? cur : null;
                    commit();
                    render();
                });
                chips.appendChild(chip);
            }
            if (!Array.isArray(r.shots) || !r.shots.length) {
                const note = el("span", "h3e-chips-label", "— unscheduled: hop 1 only");
                note.title = "With no hops selected this still stays off hop 2+ of a `next` chain, "
                    + "so the motion pin carries continuity instead of the photograph.";
                chips.appendChild(note);
            }
            row.appendChild(chips);

            list.appendChild(row);
            if (bad) {
                const note = el("div", "h3e-note h3e-note-error", bad);
                list.appendChild(note);
            }
            if (r.subject != null) bySubject.set(r.subject, true);
        }

        for (const num of [...bySubject.keys()].sort((a, b) => a - b)) {
            list.appendChild(renderSubject(num));
        }
        for (const num of orphans) {
            const warn = el("div", "h3e-note",
                `Subject ${num} has continuity text but no ref claims it. Give a ref that subject number, or clear the text — the run will refuse otherwise.`);
            list.appendChild(warn);
        }
    }

    return { root, render };
}
