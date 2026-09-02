/* WRITE -- have a local chat model draft both JSON boxes.
 *
 * Optional in the strongest sense: with no server configured this section is a
 * one-line hint pointing at the manual recipe in `prompt_pack/README.md`, and
 * nothing it does can affect a render. A successful draft is held here until
 * Accept, which writes into `shot_plan` and `ref_plan` exactly as a paste
 * would. Discard leaves the cards alone. A plan silently rewritten under you
 * is worse than no plan; the model is a drafting aid, not the store.
 * Queueing reads the widgets; the model is never in the execution path.
 *
 * The status line is the point of the design, not decoration. The server-side
 * loop generates, validates with the node's own checkers, and feeds any error
 * back for another attempt -- and a repair that happens silently is
 * indistinguishable from a model that got it right first time. Showing
 * "attempt 2/3 -- fixing: shot 1: unknown reference '@kitchen'" is what makes
 * the feature legible rather than magic, and it is also the thing that teaches
 * the format to someone reading it.
 */
import { el, button, widgetByName } from "./widget_utils.js";
import { parseRefPlan } from "./ref_rail.js";

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
    brief.title = "Describe the scene in plain language. Pictures already in "
        + "the REFERENCES rail are the ones that get used; the model looks at "
        + "them. With the rail empty, files in h3_refs are listed by name.";
    row.appendChild(brief);

    const hops = el("input", "h3e-writer-hops");
    hops.type = "number";
    hops.min = "1";
    hops.max = "24";
    hops.value = String(hopCount?.() || 3);
    hops.title = "How many hops to write.";
    row.appendChild(hops);

    const go = button("Write plan", "Draft a plan and repair it until the "
        + "node accepts it. Your cards do not change until you press Accept.",
        () => run());
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

    const draft = el("div", "h3e-writer-draft");
    draft.style.display = "none";
    const draftList = el("div", "h3e-writer-draft-list");
    draft.appendChild(draftList);
    const draftBtns = el("div", "h3e-writer-row");
    const acceptBtn = button("Accept",
        "Put this draft on the shot and reference cards, replacing what is there.",
        () => acceptDraft());
    const discardBtn = button("Discard",
        "Throw the draft away. The cards stay as they are.",
        () => discardDraft());
    draftBtns.appendChild(acceptBtn);
    draftBtns.appendChild(discardBtn);
    draft.appendChild(draftBtns);
    body.appendChild(draft);

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
    // Persist on pick, because Write plan sends only the brief and the hop
    // count -- the server uses the SAVED model. Without this the dropdown
    // looks like it selects the writer and does not: choosing a model and
    // pressing Write plan would quietly run the previous one, and the only
    // evidence would be a plan in the wrong house style.
    model.addEventListener("change", () => { if (model.value) saveConn(); });
    modelRow.appendChild(model);
    cog.appendChild(modelRow);

    const optRow = el("div", "h3e-writer-row");
    const unloadWrap = el("label", "h3e-writer-check");
    const unload = el("input");
    unload.type = "checkbox";
    unloadWrap.appendChild(unload);
    unloadWrap.appendChild(el("span", null, "Keep the writer loaded"));
    unloadWrap.title = "Stay resident between plans, so writing a second one "
        + "does not pay a full model load. The VRAM is handed back "
        + "automatically when you queue a render — a 27B and an H3 render do "
        + "not fit on one card. Turn this off to unload the moment each plan is "
        + "written, on a machine too tight to hold the writer at all.";
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
    let pending = null;     // {shot, ref} waiting on Accept, or null

    /* Everything in this section is DOM, and the panel is mounted with
     * `serialize: false` -- so the brief, the hop count and an un-accepted
     * draft lived nowhere that survived a pane switch or a page reload. You
     * could describe a scene, wait a minute for a 27B to write it, glance at
     * another tab, and come back to an empty box.
     *
     * Node properties are the fix: they ride the saved workflow and ComfyUI's
     * restored graph, and they are what `h3_editor_mode` and `h3_plan_folded`
     * already use for exactly this. The draft is held here too -- it is the
     * expensive thing on the screen, and discarding it silently is the worst of
     * the three losses.
     *
     * The connection settings deliberately do NOT go here: they are per-machine
     * and live in htc_llm.json, so a shared .json never points at your server. */
    const BRIEF_PROP = "h3_writer_brief";
    const HOPS_PROP = "h3_writer_hops";
    const DRAFT_PROP = "h3_writer_draft";

    const props = () => (node.properties ??= {});

    function remember(key, value) {
        try {
            props()[key] = value;
        } catch (err) {
            console.warn("[HandTieClips] could not persist writer state:", err);
        }
    }

    function rememberDraft() {
        remember(DRAFT_PROP, pending
            ? { shot: pending.shot || "", ref: pending.ref || "" }
            : null);
    }

    function summarise(shotJson, refJson) {
        let shots = [];
        try {
            const p = JSON.parse(shotJson || "null");
            shots = Array.isArray(p) ? p : (p?.shots || []);
        } catch (_) { shots = []; }
        let refs = [];
        try {
            const r = JSON.parse(refJson || "null");
            refs = Array.isArray(r?.refs) ? r.refs : [];
        } catch (_) { refs = []; }
        return { shots, refs };
    }

    /* The draft, in full.
     *
     * This used to show one line per shot, clipped at 110 characters, with
     * everything except `beat` and the tag list thrown away before it was even
     * rendered -- so the thing you were being asked to Accept or Discard was a
     * row of sentences ending in an ellipsis. You cannot judge a plan you
     * cannot read. Nothing is truncated here: the pane scrolls, and a beat that
     * runs long is a beat worth seeing before it goes on the cards.
     */
    function showDraft(shotJson, refJson) {
        pending = { shot: shotJson || "", ref: refJson || "" };
        rememberDraft();
        draftList.textContent = "";
        const { shots, refs } = summarise(pending.shot, pending.ref);

        if (!shots.length) {
            draftList.appendChild(el("div", "h3e-note",
                "The draft has no shots. Discard it and try a different brief."));
        }
        shots.forEach((s, i) => {
            const card = el("div", "h3e-draft-shot");
            const head = el("div", "h3e-draft-head");
            head.appendChild(el("span", "h3e-draft-no", String(i + 1)));
            // The dials the old summary discarded. A per-shot duration or seed
            // is exactly the kind of thing you want to catch BEFORE accepting.
            for (const [label, value] of [
                ["", s.duration], ["seed ", s.seed], ["steps ", s.steps],
            ]) {
                if (value !== undefined && value !== null && value !== "") {
                    head.appendChild(el("span", "h3e-draft-chip",
                                        `${label}${value}`));
                }
            }
            if (s.locked) head.appendChild(el("span", "h3e-draft-chip", "locked"));
            card.appendChild(head);

            const beat = String(s.beat || "").replace(/\s+/g, " ").trim();
            card.appendChild(el("div", "h3e-draft-beat",
                                beat || "(empty beat)"));

            const dirs = s.directives && typeof s.directives === "object"
                ? Object.entries(s.directives)
                    .filter(([, v]) => v)
                    .map(([k, v]) => `${k}: ${v}`)
                : [];
            if (dirs.length) {
                card.appendChild(el("div", "h3e-draft-meta", dirs.join("   ")));
            }
            // Which pictures ride this shot, per shot rather than as one
            // undifferentiated list at the bottom. `shots` is 1-based.
            const riding = refs
                .filter((r) => !Array.isArray(r.shots) || r.shots.includes(i + 1))
                .map((r) => "@" + String(r.tag || "").replace(/^@/, ""));
            if (riding.length) {
                card.appendChild(el("div", "h3e-draft-meta", riding.join("  ")));
            }
            draftList.appendChild(card);
        });

        if (refs.length) {
            draftList.appendChild(el("div", "h3e-draft-head",
                                     `${refs.length} reference(s)`));
            for (const r of refs) {
                const bits = [
                    "@" + String(r.tag || "").replace(/^@/, ""),
                    r.file || "(no file)",
                    r.subject != null ? `subject ${r.subject}` : "",
                    r.retention || "",
                    Array.isArray(r.shots) ? `shots ${r.shots.join(",")}`
                                           : "every shot",
                ].filter(Boolean);
                draftList.appendChild(
                    el("div", "h3e-writer-draft-line", bits.join("   ")));
                if (r.desc) {
                    draftList.appendChild(el("div", "h3e-draft-meta", r.desc));
                }
            }
        }

        // The literal text Accept would write, for anyone who would rather read
        // the JSON than the rendering of it. Same escape hatch the SCRIPT and
        // REFERENCES sections already offer, shut by default.
        const wrap = el("details", "h3e-json-wrap");
        wrap.appendChild(el("summary", null, "JSON"));
        const area = el("textarea", "h3e-json");
        area.readOnly = true;
        area.value = [pending.shot, pending.ref].filter(Boolean).join("\n\n");
        wrap.appendChild(area);
        draftList.appendChild(wrap);

        draft.style.display = "";
    }

    function discardDraft() {
        pending = null;
        rememberDraft();
        draft.style.display = "none";
        draftList.textContent = "";
    }

    function acceptDraft() {
        if (!pending) return;
        const shot = pending.shot;
        const ref = pending.ref;
        discardDraft();
        onWritten?.(shot, ref);
        say("Accepted. On the cards now.", "hint");
    }

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
        acceptBtn.disabled = on;
        discardBtn.disabled = on;
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
        unload.checked = conn.keep_warm !== false;

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

        // A fresh install has no saved model, so nothing above matched and
        // the browser is left showing option[0] having fired no `change` --
        // and `change` is the only thing that saves. The panel then displays a
        // model the server does not have: WRITE answers "no model is
        // selected", nothing is ever asked of the server so JIT never fires,
        // and Free VRAM still works because unload_all() ignores the
        // configured model by design. All three, one empty string.
        //
        // So adopt what is on screen -- but ONLY when the stored value is
        // empty. The guard above is still right that option[0] must not
        // overwrite a working setting; it had just over-corrected into never
        // writing one at all.
        if (!conn.model && model.value) {
            const adopted = model.value;
            try {
                const r = await fetch(LLM_URL, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ model: adopted }),
                });
                const j = await r.json();
                if (j?.ok) conn.model = j.model || adopted;
                if (j?.saved === false) {
                    console.warn("[HandTieClips] model set for this session "
                        + "but not written to disk: " + (j.save_error || ""));
                }
            } catch (e) {
                // Leave conn.model empty: the badge below then reads
                // "no model", which is the truth.
                console.warn("[HandTieClips] could not adopt model: " + e);
            }
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
        const patch = { server_url: url.value, keep_warm: unload.checked };
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
            say(j.saved === false
                ? `Set for this session, but htc_llm.json could not be `
                  + `written: ${j.save_error || "unknown error"}. The setting `
                  + `is lost when ComfyUI restarts -- make the pack folder `
                  + `writable.`
                : "Saved.",
                j.saved === false ? "error" : "hint");
            await loadConn(false);
        } catch (e) {
            say(String(e), "error");
        }
    }

    function railRefs() {
        const plan = parseRefPlan(widgetByName(node, "ref_plan")?.value);
        if (!plan) return [];
        return (plan.refs || [])
            .filter((r) => String(r.file || "").trim())
            .map((r) => ({
                tag: String(r.tag || "").replace(/^@/, ""),
                file: String(r.file || "").trim(),
                subject: r.subject == null ? null : Number(r.subject),
                retention: String(r.retention || ""),
                desc: String(r.desc || ""),
                // The writer never sees `mp` and cannot author one, but Accept
                // replaces the whole register with what comes back. Send it up
                // so the server can put it back, or every write silently
                // resets every row to "full".
                mp: r.mp ? Number(r.mp) : null,
            }));
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
                    // The node's hop length. The prompt's length table has a
                    // row per duration and the model could not tell which row
                    // it was writing to -- it is told to ask, and a button
                    // cannot answer.
                    duration: String(widgetByName(node, "duration")?.value || ""),
                    refs: railRefs(),
                }),
            });
            const j = await r.json();
            // A rejected loop can still hold a usable script. Put it in the
            // draft so Accept is not behind a red wall that threw the work away.
            if (j.shot_plan) showDraft(j.shot_plan || "", j.ref_plan || "");
            if (!j.ok) {
                // A plan that would not converge still comes back, so the
                // errors name real shots. Showing them beats "generation
                // failed", which tells the user nothing they can act on.
                // All of them, not the first three. The status box scrolls.
                // Capping diagnostics is how a repair loop spending an attempt
                // on a filename mismatch stayed invisible through 1.0.0 and
                // 1.0.1 -- the error that explained it was the fourth one.
                const errs = (j.errors || []).map((e) => `• ${e}`).join("\n");
                say(errs
                    ? `Gave up after ${j.attempts} attempts. The model could `
                      + `not fix:\n${errs}`
                      + (j.shot_plan
                          ? "\n\nA partial draft is below; Accept still writes it."
                          : "")
                    : (j.error || "the plan writer failed"), "error");
                return;
            }
            const warn = (j.warnings || []).map((w) => `• ${w}`).join("\n");
            say(`Draft of ${hops.value} hop(s) in ${j.attempts} attempt(s). `
                + `Accept to put it on the cards, or Discard to keep what you have.`
                + (warn ? `\n\nWorth a look before you accept:\n${warn}` : ""),
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

    brief.addEventListener("input", () => remember(BRIEF_PROP, brief.value));
    hops.addEventListener("input", () => remember(HOPS_PROP, hops.value));

    /** Put back whatever this node was holding. Safe to call more than once. */
    function restore() {
        const p = props();
        if (typeof p[BRIEF_PROP] === "string" && !brief.value) {
            brief.value = p[BRIEF_PROP];
        }
        const d = p[DRAFT_PROP];
        if (!pending && d && (d.shot || d.ref)) showDraft(d.shot, d.ref);
    }
    restore();

    return {
        root,
        restore,
        syncHops() {
            // A hop count the user typed is an instruction, not a stale value:
            // writing four hops against a six-shot plan is a normal thing to
            // want. Only follow the plan when nothing has been chosen here.
            const saved = props()[HOPS_PROP];
            if (saved) {
                hops.value = String(saved);
                return;
            }
            hops.value = String(hopCount?.() || hops.value || 3);
        },
    };
}
