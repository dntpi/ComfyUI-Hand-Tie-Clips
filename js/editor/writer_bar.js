/* WRITE -- have a local chat model fill in both JSON boxes.
 *
 * Optional in the strongest sense: with no server configured this section is a
 * one-line hint pointing at the manual recipe in `prompt_pack/README.md`, and
 * nothing it does can affect a render. The button writes into `shot_plan` and
 * `ref_plan` exactly as a paste would, so the cards, the lint badges and the
 * JSON escape hatch all keep working with no knowledge of where the text came
 * from. Queueing reads the widgets; the model is never in the execution path.
 *
 * The status line is the point of the design, not decoration. The server-side
 * loop generates, validates with the node's own checkers, and feeds any error
 * back for another attempt -- and a repair that happens silently is
 * indistinguishable from a model that got it right first time. Showing
 * "attempt 2/3 -- fixing: shot 1: unknown reference '@kitchen'" is what makes
 * the feature legible rather than magic, and it is also the thing that teaches
 * the format to someone reading it.
 */
import { el, button } from "./widget_utils.js";

const LLM_URL = "/h3_ref_chain/llm";
const PLAN_URL = "/h3_ref_chain/plan";
const UNLOAD_URL = "/h3_ref_chain/llm/unload";

export function createWriterBar(node, { onWritten, hopCount } = {}) {
    const root = el("details", "h3e-section h3e-writer");
    const sum = el("summary", null);
    sum.appendChild(el("span", "h3e-title", "WRITE"));
    const badge = el("span", "h3e-count", "");
    sum.appendChild(badge);
    root.appendChild(sum);

    const body = el("div", "h3e-writer-body");
    root.appendChild(body);

    /* -- the brief ----------------------------------------------------- */
    const row = el("div", "h3e-writer-row");
    const brief = el("input", "h3e-writer-brief");
    brief.type = "text";
    brief.placeholder = "a cook plates a dish, then walks out into the hallway";
    brief.title = "Describe the scene in plain language. The reference files "
        + "already in your h3_refs folder are sent along, so name what you want "
        + "to happen rather than which picture goes where.";
    row.appendChild(brief);

    const hops = el("input", "h3e-writer-hops");
    hops.type = "number";
    hops.min = "1";
    hops.max = "24";
    hops.value = String(hopCount?.() || 3);
    hops.title = "How many hops to write.";
    row.appendChild(hops);

    const go = button("Write plan", "Generate a plan and repair it until the "
        + "node accepts it", () => run());
    row.appendChild(go);

    // In the main row on purpose. This is the button you reach for when a
    // render just OOMed, and a killswitch behind a disclosure is not one.
    const freeBtn = button("Free VRAM", "Unload whatever the writer is holding "
        + "in memory, right now -- not just the model configured here. Press "
        + "it before queueing if the card is full.", () => freeVram());
    row.appendChild(freeBtn);
    body.appendChild(row);

    const status = el("div", "h3e-note h3e-writer-status");
    status.style.display = "none";
    body.appendChild(status);

    /* -- settings ------------------------------------------------------ */
    const cog = el("details", "h3e-writer-conn");
    const cogSum = el("summary", null, "Settings");
    cog.appendChild(cogSum);

    const urlRow = el("div", "h3e-writer-row");
    const url = el("input", "h3e-writer-url");
    url.type = "text";
    url.placeholder = "http://127.0.0.1:1234";
    url.title = "Any OpenAI-compatible server: LM Studio, llama-server, "
        + "Ollama. The /v1 suffix is added for you.";
    urlRow.appendChild(url);
    const testBtn = button("Test", "Check the server and reload the model list",
        () => loadConn(true));
    urlRow.appendChild(testBtn);
    cog.appendChild(urlRow);

    const modelRow = el("div", "h3e-writer-row");
    const model = el("select", "h3e-select h3e-writer-model");
    model.title = "Which loaded model writes the plan.";
    modelRow.appendChild(model);
    cog.appendChild(modelRow);

    const optRow = el("div", "h3e-writer-row");
    const unloadWrap = el("label", "h3e-writer-check");
    const unload = el("input");
    unload.type = "checkbox";
    unloadWrap.appendChild(unload);
    unloadWrap.appendChild(el("span", null, "Unload after writing"));
    unloadWrap.title = "Hand the VRAM back when the plan is done. A 27B and an "
        + "H3 render do not fit on one card, so leaving this off usually means "
        + "the next queue runs out of memory.";
    optRow.appendChild(unloadWrap);
    optRow.appendChild(el("span", "h3e-spacer"));
    const saveBtn = button("Save", "Remember these settings on this machine",
        () => saveConn());
    optRow.appendChild(saveBtn);
    cog.appendChild(optRow);

    const connNote = el("div", "h3e-note h3e-note-hint");
    connNote.textContent = "Settings are saved on this machine only — they are "
        + "not part of the workflow, so a shared .json never points at your "
        + "server.";
    cog.appendChild(connNote);
    body.appendChild(cog);

    /* -- state --------------------------------------------------------- */
    let busy = false;
    let conn = null;

    function say(text, kind) {
        status.style.display = text ? "" : "none";
        status.textContent = text || "";
        status.classList.toggle("h3e-note-error", kind === "error");
        status.classList.toggle("h3e-note-hint", kind === "hint");
    }

    function setBusy(on) {
        busy = on;
        go.disabled = on;
        // Unloading mid-generation would evict the model answering the prompt.
        freeBtn.disabled = on;
        go.textContent = on ? "Writing…" : "Write plan";
    }

    async function freeVram() {
        if (busy) return;
        freeBtn.disabled = true;
        freeBtn.textContent = "Freeing…";
        try {
            const r = await fetch(UNLOAD_URL, { method: "POST" });
            const j = await r.json();
            // "Nothing was loaded" is a success with nothing to do, and saying
            // so beats a bare "done" that leaves the user wondering whether
            // the card is actually free.
            say(!j.ok
                ? (j.error || "could not reach the writer")
                : j.unloaded
                    ? `Freed ${j.unloaded} model(s): ${j.note}`
                    : `Nothing to free — ${j.note}`,
                j.ok ? "hint" : "error");
            if (j.ok && j.unloaded) await loadConn(false);
        } catch (e) {
            say(String(e), "error");
        } finally {
            freeBtn.disabled = false;
            freeBtn.textContent = "Free VRAM";
        }
    }

    async function loadConn(announce) {
        try {
            const r = await fetch(LLM_URL);
            conn = await r.json();
        } catch (e) {
            badge.textContent = "offline";
            if (announce) say(`Could not reach ComfyUI: ${e}`, "error");
            return;
        }
        if (!conn?.ok) {
            badge.textContent = "offline";
            if (announce) say(conn?.error || "settings unavailable", "error");
            return;
        }
        url.value = conn.server_url || "";
        unload.checked = conn.unload_after !== false;

        // Preselect the SAVED model rather than letting the list decide. If the
        // dropdown lands on option[0], the next Save quietly rewrites the
        // configured model to whatever LM Studio happens to list first.
        model.innerHTML = "";
        const list = conn.models || [];
        if (!list.length) {
            const o = el("option", null, "(no models — is the server started?)");
            o.value = "";
            model.appendChild(o);
        }
        const ids = list.map((m) => m.id);
        for (const m of list) {
            // LM Studio lists every INSTALLED model, so "in the list" and
            // "will answer" are different things. Say which, or the first
            // Write plan fails with a 400 the dropdown implied was impossible.
            const mark = m.loaded === true ? "● " : (m.loaded === false ? "○ " : "");
            const tail = m.loaded === false ? " — not loaded" : "";
            const o = el("option", null, `${mark}${m.id}${tail}`);
            o.value = m.id;
            if (m.id === conn.model) o.selected = true;
            model.appendChild(o);
        }
        if (conn.model && !ids.includes(conn.model)) {
            // The saved model is not on this server right now. Keep it visible
            // rather than dropping it, or Save would erase a working setting.
            const o = el("option", null, `${conn.model} — not found`);
            o.value = conn.model;
            o.selected = true;
            model.appendChild(o);
        }

        badge.textContent = conn.online
            ? (conn.model || "no model") : "offline";
        if (announce) {
            const loaded = list.filter((m) => m.loaded === true).length;
            say(!conn.online
                ? `No server at ${conn.server_url}. Start it in LM Studio’s `
                  + `Developer tab, or write the JSON by hand.`
                : conn.any_loaded === false
                    ? `Server is up, but none of its ${list.length} model(s) `
                      + `are loaded. Load one in LM Studio, or turn on `
                      + `Just-In-Time model loading.`
                    : `Server is up — ${loaded} of ${list.length} model(s) `
                      + `loaded (●).`,
                conn.online && conn.any_loaded !== false ? "hint" : "error");
        }
    }

    async function saveConn() {
        const patch = { server_url: url.value, unload_after: unload.checked };
        // Never post a blank model: the server skips blanks, but sending one
        // at all invites the same overwrite bug from the other direction.
        if (model.value) patch.model = model.value;
        try {
            const r = await fetch(LLM_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(patch),
            });
            const j = await r.json();
            if (!j.ok) throw new Error(j.error || "save failed");
            say("Saved.", "hint");
            await loadConn(false);
        } catch (e) {
            say(String(e), "error");
        }
    }

    async function run() {
        if (busy) return;                       // one generation at a time
        setBusy(true);
        say(`Writing a ${hops.value}-hop plan… this can take a minute on a `
            + `large model.`, "hint");
        try {
            const r = await fetch(PLAN_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    brief: brief.value,
                    hops: Number(hops.value) || 3,
                }),
            });
            const j = await r.json();
            if (!j.ok) {
                // A plan that would not converge still comes back, so the
                // errors name real shots. Showing them beats "generation
                // failed", which tells the user nothing they can act on.
                const errs = (j.errors || []).slice(0, 3)
                    .map((e) => `• ${e}`).join("\n");
                say(errs
                    ? `Gave up after ${j.attempts} attempts. The model could `
                      + `not fix:\n${errs}`
                    : (j.error || "the plan writer failed"), "error");
                return;
            }
            onWritten?.(j.shot_plan || "", j.ref_plan || "");
            const warn = (j.warnings || []).slice(0, 3)
                .map((w) => `• ${w}`).join("\n");
            say(`Wrote ${hops.value} hop(s) in ${j.attempts} attempt(s).`
                + (warn ? `\n\nWorth a look before you queue:\n${warn}` : ""),
                // Lints are shown but never auto-retried: a model asked to fix
                // a lint it disagrees with rewrites the parts that were fine.
                // "warn" is the absence of both modifiers -- a bare .h3e-note
                // is already the warning colour.
                warn ? "warn" : "hint");
        } catch (e) {
            say(String(e), "error");
        } finally {
            setBusy(false);
        }
    }

    // Only reach the network when the section is actually opened. A panel that
    // probes a server on every node creation would make every canvas load wait
    // on a timeout for people who never use this.
    let loaded = false;
    root.addEventListener("toggle", () => {
        if (root.open && !loaded) {
            loaded = true;
            loadConn(false);
        }
    });

    return {
        root,
        syncHops() { hops.value = String(hopCount?.() || hops.value || 3); },
    };
}
