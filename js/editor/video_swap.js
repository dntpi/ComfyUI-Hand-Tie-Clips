/* SWAP -- one-hop identity swap from a reference clip.
 *
 * Standalone implant, not a chain writer. Accept writes shot_plan (exactly
 * one shot) and reference_video_desc. It never writes ref_plan. Identity is
 * picked from the rail; the pictures stay where they are.
 *
 * Trim the clip on MEDIA. SWAP reads the same widgets (slot 1) and uses the
 * IN point as the frame it shows the model.
 *
 * LLM settings live on WRITE. This tab does not duplicate them.
 */
import { el, button, widgetByName } from "./widget_utils.js";
import { parseRefPlan } from "./ref_rail.js";
import { createPicker } from "./media_picker.js";

const SWAP_PLAN_URL = "/h3_ref_chain/swap_plan";
const SWAP_DESCRIBE_URL = "/h3_ref_chain/swap_describe";

function commit(node, w, value) {
    if (!w) return;
    w.value = value;
    try {
        w.callback?.(value, node.graph?.canvas, node, undefined, undefined);
    } catch (err) {
        console.error(`[HandTieClips] widget callback for ${w.name} failed:`, err);
    }
    node.graph?.setDirtyCanvas?.(true, true);
}

export function createVideoSwap(node, { onWritten } = {}) {
    const root = el("details", "h3e-section h3e-writer h3e-swap");
    const sum = el("summary", null);
    sum.appendChild(el("span", "h3e-title", "SWAP"));
    const badge = el("span", "h3e-count", "");
    sum.appendChild(badge);
    root.appendChild(sum);

    const body = el("div", "h3e-writer-body");
    root.appendChild(body);

    const hint = el("div", "h3e-note h3e-note-hint");
    hint.textContent = "One hop: replace the person in clip 1 with a still "
        + "from the rail. Trim the clip on MEDIA. Does not change REFERENCES.";
    body.appendChild(hint);

    const wVideo = widgetByName(node, "reference_video_file");
    const wStart = widgetByName(node, "reference_video_start_s");
    const wDesc = widgetByName(node, "reference_video_desc");

    const cell = el("label", "h3e-media-cell");
    cell.appendChild(el("span", "h3e-media-label", "clip 1"));
    const pick = createPicker({
        kind: "video",
        get: () => String(wVideo?.value || ""),
        set: (v) => {
            commit(node, wVideo, v);
            if (wStart) commit(node, wStart, 0);
        },
        title: "Reference clip. Same file as MEDIA slot 1.",
    });
    cell.appendChild(pick.root);
    body.appendChild(cell);

    const identRow = el("div", "h3e-writer-row");
    const identSel = el("select", "h3e-select");
    identSel.title = "Identity still already on the REFERENCES rail.";
    identRow.appendChild(identSel);
    body.appendChild(identRow);

    function railRows() {
        const plan = parseRefPlan(widgetByName(node, "ref_plan")?.value);
        if (!plan) return [];
        return (plan.refs || []).filter((r) => String(r.file || "").trim()
            && String(r.tag || "").trim());
    }

    function fillIdentity() {
        const rows = railRows();
        const prev = identSel.value;
        identSel.textContent = "";
        const blank = el("option", null, "— pick an identity —");
        blank.value = "";
        identSel.appendChild(blank);
        for (const r of rows) {
            const tag = String(r.tag || "").replace(/^@/, "");
            const o = el("option", null, "@" + tag);
            o.value = tag;
            identSel.appendChild(o);
        }
        identSel.value = rows.some((r) => String(r.tag).replace(/^@/, "") === prev)
            ? prev : "";
    }

    const row = el("div", "h3e-writer-row");
    const brief = el("input", "h3e-writer-brief");
    brief.type = "text";
    brief.placeholder = "optional -- extra context beyond the clip";
    row.appendChild(brief);

    const describeBtn = button("Describe frame",
        "Caption the frame at MEDIA's IN point. Writes the clip description "
        + "immediately. Does not change the shot list or the rail.",
        () => describeFrame());
    row.appendChild(describeBtn);

    const go = button("Analyze & write plan",
        "Draft one hop. Your cards do not change until you press Accept.",
        () => run());
    row.appendChild(go);
    body.appendChild(row);

    const status = el("div", "h3e-note h3e-writer-status");
    status.style.display = "none";
    body.appendChild(status);

    const draft = el("div", "h3e-writer-draft");
    draft.style.display = "none";
    const draftList = el("div", "h3e-writer-draft-list");
    draft.appendChild(draftList);
    const draftBtns = el("div", "h3e-writer-row");
    const acceptBtn = button("Accept",
        "Put this one hop on SCRIPT and the caption on the clip. "
        + "Does not change REFERENCES.",
        () => acceptDraft());
    const discardBtn = button("Discard",
        "Throw the draft away. The cards stay as they are.",
        () => discardDraft());
    draftBtns.appendChild(acceptBtn);
    draftBtns.appendChild(discardBtn);
    draft.appendChild(draftBtns);
    body.appendChild(draft);

    let busy = false;
    let pending = null;

    function say(text, kind) {
        status.style.display = text ? "" : "none";
        status.className = "h3e-note h3e-writer-status"
            + (kind === "error" ? " h3e-note-error"
                : kind === "hint" ? " h3e-note-hint" : "");
        status.textContent = text || "";
    }

    function setBusy(on) {
        busy = on;
        describeBtn.disabled = go.disabled = acceptBtn.disabled = on;
    }

    function oneShot(shotJson) {
        let obj;
        try {
            obj = JSON.parse(shotJson);
        } catch (err) {
            return null;
        }
        const shots = obj?.shots || obj?.shot_plan?.shots;
        if (!Array.isArray(shots) || shots.length !== 1) return null;
        return JSON.stringify({ shots: [shots[0]] }, null, 2);
    }

    function showDraft(shotJson, videoDesc) {
        const one = oneShot(shotJson);
        if (!one) {
            say("SWAP writes one hop. This draft had "
                + "a different number of shots and was not kept.", "error");
            return;
        }
        pending = { shot: one, videoDesc: videoDesc || "" };
        draftList.textContent = "";
        let beat = "";
        try { beat = JSON.parse(one).shots[0].beat || ""; } catch (err) { /* */ }
        draftList.appendChild(el("div", "h3e-writer-draft-line",
            beat ? beat.slice(0, 240) : "(one hop)"));
        if (pending.videoDesc) {
            draftList.appendChild(el("div", "h3e-writer-draft-line",
                "clip: " + pending.videoDesc.slice(0, 160)));
        }
        draft.style.display = "";
    }

    function discardDraft() {
        pending = null;
        draft.style.display = "none";
        draftList.textContent = "";
    }

    function acceptDraft() {
        if (!pending) return;
        const { shot, videoDesc } = pending;
        const one = oneShot(shot);
        if (!one) {
            say("Accept refused: SWAP writes exactly one shot.", "error");
            return;
        }
        const nShots = widgetByName(node, "shot_plan")?.value
            ? (() => {
                try {
                    const cur = JSON.parse(widgetByName(node, "shot_plan").value);
                    return (cur.shots || []).length;
                } catch (err) { return 0; }
            })()
            : 0;
        if (nShots > 1) {
            say("SCRIPT already has " + nShots + " shots. SWAP replaces the "
                + "script with one hop. Discard or save that chain first.",
                "error");
            return;
        }
        discardDraft();
        onWritten?.(one, {
            reference_video_desc: videoDesc || "",
        });
        say("Accepted. One hop on SCRIPT. REFERENCES unchanged.", "hint");
    }

    async function describeFrame() {
        if (busy) return;
        const video = String(wVideo?.value || "");
        if (!video) { say("Pick a reference clip first.", "error"); return; }
        setBusy(true);
        say("Captioning the frame at MEDIA's IN point…", "hint");
        try {
            const r = await fetch(SWAP_DESCRIBE_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    video,
                    video_start_s: Number(wStart?.value) || 0,
                }),
            });
            const j = await r.json();
            if (!j.ok) {
                say(j.error || "describe failed", "error");
                return;
            }
            commit(node, wDesc, j.description || "");
            say("Clip description written. REFERENCES and SCRIPT unchanged.",
                "hint");
        } catch (e) {
            say(String(e), "error");
        } finally {
            setBusy(false);
        }
    }

    async function run() {
        if (busy) return;
        const video = String(wVideo?.value || "");
        if (!video) { say("Pick a reference clip first.", "error"); return; }
        const tag = identSel.value;
        const identity = railRows().find(
            (r) => String(r.tag || "").replace(/^@/, "") === tag);
        if (!identity) { say("Pick an identity picture first.", "error"); return; }

        setBusy(true);
        say("Writing one hop…", "hint");
        try {
            const r = await fetch(SWAP_PLAN_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    brief: brief.value,
                    duration: String(widgetByName(node, "duration")?.value || ""),
                    video,
                    video_start_s: Number(wStart?.value) || 0,
                    identity: {
                        tag: String(identity.tag).replace(/^@/, ""),
                        file: String(identity.file || "").trim(),
                    },
                    rail_tags: railRows().map(
                        (r) => String(r.tag || "").replace(/^@/, "")),
                }),
            });
            const j = await r.json();
            if (j.shot_plan) showDraft(j.shot_plan, j.video_desc || "");
            if (!j.ok) {
                const errs = (j.errors || []).map((e) => `• ${e}`).join("\n");
                say(errs || j.error || "SWAP failed", "error");
                return;
            }
            const warn = (j.warnings || []).map((w) => `• ${w}`).join("\n");
            say("Draft of one hop in " + (j.attempts || "?")
                + " attempt(s). Accept to put it on SCRIPT."
                + (warn ? "\n\n" + warn : ""),
                warn ? "hint" : "hint");
        } catch (e) {
            say(String(e), "error");
        } finally {
            setBusy(false);
        }
    }

    function render() {
        fillIdentity();
        pick.render?.();
    }

    fillIdentity();
    return { root, render, destroy() { pick.destroy?.(); } };
}
