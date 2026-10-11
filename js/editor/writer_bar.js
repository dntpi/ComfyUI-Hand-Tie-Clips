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

/* Which backends have a server program this pack starts, filled from the
 * server's own list on every load. Empty until the first response, which is why
 * the path fields start hidden: a field for a backend with no binary would be a
 * field that cannot do anything. */
let BACKEND_PROGRAMS = [];
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

    /* -- the conversation (Phase B) ------------------------------------- */
    //
    // The WRITE tab is a CHAT THREAD, and the cards are its output. Propose,
    // Write beats and Write plan are shortcuts that post a canned message --
    // free text works at every point, and the model sees the thread, the
    // accepted proposal and the current plan every time (planner.build_thread).
    //
    // Nothing here writes the plan. Every turn produces a DRAFT CARD, and the
    // plan changes only when Accept is pressed on that card. A plan silently
    // rewritten under you is worse than no plan.
    const THREAD_PROP = "h3_writer_thread";
    const PROPOSAL_PROP = "h3_writer_proposal";
    // Bounded for the same reason as the server side: the thread rides the
    // saved workflow, so an unbounded one grows every file, and a long
    // conversation puts the brief so far back the model answers an old
    // question. The accepted proposal and the plan are resent every turn by
    // build_thread, so what this bounds is the wandering, not the state.
    const THREAD_MAX = 12;
    const THREAD_DROPPED_PROP = "h3_writer_dropped";

    const thread = el("div", "h3e-chat");
    const threadList = el("div", "h3e-chat-log");
    thread.appendChild(threadList);
    body.appendChild(thread);

    const stageRow = el("div", "h3e-writer-row");
    stageRow.appendChild(button("Propose", "Ask for the film in words first — "
        + "the look, who is in frame, how many hops and why. Writes nothing.",
        () => sendCanned("propose", "propose the film")));
    stageRow.appendChild(button("Write beats", "Write the beats for the "
        + "proposal you accepted.", () => sendCanned("develop",
            "write the beats for the accepted proposal")));
    stageRow.appendChild(button("Write plan", "Propose and write the beats in "
        + "one go.", () => sendCanned("develop", "propose the film, then write "
        + "the beats for it")));
    // MV mode's entry point, and only meaningful with a song attached. Kept on
    // the same row because it IS the same conversation -- the song card it
    // produces is a reply, not a separate mode.
    stageRow.appendChild(button("Song", "Measure the attached song — tempo, "
        + "bar grid, suggested sections, lyrics. Writes nothing.",
        () => sendCanned("song", "measure this song")));
    body.appendChild(stageRow);

    // The composer is a TEXTAREA, not an input. A single-line box cannot hold a
    // paragraph of brief, a piece of dialogue, or anything pasted -- and a
    // paste into a one-liner is either truncated or scrolls off with no way
    // to see what is there. Enter still sends, because that is what this box
    // has always done and changing it would be a surprise; Shift+Enter is the
    // newline, which is the convention every other chat box uses and the only
    // reason to make this a textarea at all.
    const chatRow = el("div", "h3e-writer-row h3e-writer-composer");
    const chat = el("textarea", "h3e-writer-chat");
    chat.rows = 3;
    chat.spellcheck = true;
    chat.placeholder = "talk to the writer — an idea, a change, a line of "
        + "dialogue… (Enter sends, Shift+Enter a new line)";
    chat.title = "Free text. The model sees this conversation, the proposal "
        + "you accepted and the plan as it stands.";
    chat.addEventListener("keydown", (e) => {
        // Enter sends, Shift+Enter breaks the line. Without the modifier test
        // this box is a one-line input with extra steps, and every newline
        // would post a turn the user did not mean to send.
        if (e.key !== "Enter" || e.shiftKey) return;
        // IME: a Japanese or Chinese keyboard's Enter COMMITS the candidate.
        // Sending there ate the composed character and posted an empty turn.
        if (e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        e.stopPropagation();
        sendCanned(composerStage(), chat.value);
    });
    chatRow.appendChild(chat);
    chatRow.appendChild(button("Send", "Send this to the writer",
        () => sendCanned(composerStage(), chat.value)));

    /* -- what Enter in the composer DOES ---------------------------------- */
    //
    // Free text went to `develop`, which is the route's else-branch and writes
    // a plan. So typing "test" produced a ten-beat film: correct, and not what
    // anyone typing one word into a chat box meant. There was no way to ASK a
    // question at all -- every turn was a commission.
    //
    // Two chips rather than a guess. A heuristic would be wrong in the
    // expensive direction: a misrouted turn costs a full generate on a 27B
    // that decodes at ~15 tok/s, and this pack's standing lesson is that you
    // cannot subtract a mistake with cfg 1.0. The chips also make the cost
    // legible before it is paid, which a heuristic never does.
    //
    // `develop` stays the default. Someone who types a sentence into this box
    // has almost always typed a brief, and silently changing what free text
    // MEANS would be the same surprise in the other direction.
    let composerStageId = "develop";
    const chipRow = el("div", "h3e-writer-row h3e-stage-chips");
    const chips = {};
    function composerStage() { return composerStageId; }
    for (const [id, label, tip] of [
        ["ask", "Ask", "Talk to the writer. It answers in prose and writes "
            + "nothing — the plan only changes when you accept a draft."],
        ["develop", "Write beats", "Treat this as a brief and write the beats. "
            + "A draft card appears; nothing reaches the plan until Accept."],
    ]) {
        const b = button(label, tip, () => {
            composerStageId = id;
            for (const [k, c] of Object.entries(chips)) c.classList.toggle("on", k === id);
            chat.focus();
        });
        chips[id] = b;
        chipRow.appendChild(b);
    }
    chips.develop.classList.add("on");
    body.appendChild(chipRow);

    body.appendChild(chatRow);

    // ---- the thread, in node.properties so it survives a pane switch -----
    function readThread() {
        const t = node.properties?.[THREAD_PROP];
        return Array.isArray(t) ? t.slice(-THREAD_MAX) : [];
    }
// Called after any turn, so it must NOT decide what the proposal is.
      // It did, once, by copying a variable that was never declared -- which
      // read as null and therefore wiped an accepted proposal on the very next
      // line that touched the thread. The proposal is owned by exactly one
      // place, its own property: the card's Accept writes it, Discard clears
      // it, and nothing that happens to the thread may touch it.
function writeThread(t) {
              node.properties ??= {};
              // Count BEFORE slicing, and carry the number, because after the
              // slice the length can never exceed THREAD_MAX again -- which is
              // why measuring it in paintThread() was permanently false and the
              // notice below could never fire.
              const over = t.length - THREAD_MAX;
              node.properties[THREAD_DROPPED_PROP] =
                  over > 0 ? (node.properties[THREAD_DROPPED_PROP] || 0) + over : 0;
              node.properties[THREAD_PROP] = t.slice(-THREAD_MAX);
          }
    function addTurn(role, content) {
        const t = readThread();
        t.push({ role, content: String(content || "") });
        writeThread(t);
        paintThread();
    }
    function paintThread() {
        threadList.textContent = "";
        const t = readThread();
        if (!t.length) {
            threadList.appendChild(el("div", "h3e-chat-empty",
                "Ask for the film, or just describe the scene."));
        }
        for (const m of t) {
            const row = el("div", "h3e-chat-msg " + (m.role === "user" ? "me" : "him"));
            row.appendChild(el("span", "h3e-chat-who",
                m.role === "user" ? "you" : "writer"));
            row.appendChild(el("span", "h3e-chat-text", m.content));
            threadList.appendChild(row);
        }
        const dropped = (node.properties?.[THREAD_DROPPED_PROP] || 0) > 0;
        if (dropped) {
            // Say it rather than silently truncating: a model that has
            // forgotten what you said three turns ago looks like it never
            // heard you.
            threadList.appendChild(el("div", "h3e-note h3e-note-hint",
                `Older turns dropped — the last ${THREAD_MAX} are kept. The `
                + `proposal and the plan are still sent every turn.`));
        }
        scrollerToEnd();
    }
    function scrollerToEnd() {
        const p = thread.closest(".h3e-scroll");
        if (p) p.scrollTop = p.scrollHeight;
    }

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

    // Which server. First, because it decides what the address below MEANS: a
    // user who types :8080 while the panel thinks it is LM Studio gets a green
    // Test and a confusing "model unloaded" later. Changing this refetches, so
    // the address and the model list below always belong to the backend shown.
    const backRow = el("div", "h3e-writer-row");
    const backend = el("select", "h3e-select h3e-writer-backend");
    backend.title = "The local server that writes your plans. Both speak the "
        + "same OpenAI API; each keeps its own address and model.";
    backend.addEventListener("change", async () => {
        // Save the switch on its own so the server keeps its pair even if the
        // address field is left mid-edit. Everything else here posts the pair
        // with the address, and switching with a half-typed address would
        // bank that half-typed address under the new name.
        await postConn({ backend: backend.value });
        loadConn(true);
    });
    backRow.appendChild(backend);
    cog.appendChild(backRow);

    const urlRow = el("div", "h3e-writer-row");
    const url = el("input", "h3e-writer-url");
    url.type = "text";
    url.placeholder = "http://127.0.0.1:1234";
    url.title = "Any OpenAI-compatible server: LM Studio, llama-server, "
        + "Ollama, nInfer. The /v1 suffix is added for you.";
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

    /* -- D1h: the server PROGRAM and the MODEL FILE, for backends that need
     * one. --------------------------------------------------------------
     *
     * Until this, picking nInfer on a machine that did not have it in
     * `C:\ninfer` was a dead end: `start_process` said "No nInfer binary
     * found. Put it in C:\ninfer, or set the path in Settings" -- and Settings
     * had no path field. The only escape hatch was hand-editing
     * `htc_llm.json`, which is exactly what a user will not find.
     *
     * Both fields are PER BACKEND and both are ordinary machine properties in
     * the same settings file, which is the design that was already there: the
     * server reads `servers[<name>].exe` and `servers[<name>].model_file`.
     * Nothing new to store, and nothing that could turn a saved path into "run
     * this instead" -- the override is only consulted for a name the module
     * already knows.
     *
     * Only shown for backends that HAVE a program. LM Studio loads its model
     * from a GUI and has no binary to point at, so showing a path field there
     * would be a field that cannot do anything. */
    const exeRow = el("div", "h3e-writer-row h3e-writer-pathrow");
    const exeField = el("label", "h3e-writer-pathfield");
    exeField.appendChild(el("span", "h3e-writer-pathlab", "server program"));
    const exeInput = el("input", "h3e-writer-path");
    exeInput.type = "text";
    exeInput.spellcheck = false;
    exeInput.placeholder = "ninfer-serve.exe";
    exeInput.title = "The server binary this pack starts. Leave blank to search "
        + "C:\\, D:\\, C:\\ninfer and D:\\ninfer. A full path wins over all of "
        + "them, which is the case on any machine where nInfer lives somewhere "
        + "else -- including every machine but this one.";
    exeField.appendChild(exeInput);
    exeRow.appendChild(exeField);

    const modelFileRow = el("div", "h3e-writer-row h3e-writer-pathrow");
    const modelFileField = el("label", "h3e-writer-pathfield");
    modelFileField.appendChild(el("span", "h3e-writer-pathlab", "model file"));
    const modelFileInput = el("input", "h3e-writer-path");
    modelFileInput.type = "text";
    modelFileInput.spellcheck = false;
    modelFileInput.placeholder = "qwen3_8_27b_nvfp4.ninfer";
    modelFileInput.title = "The weights to serve. Leave blank to look for a "
        + ".ninfer model beside the binary, in a `models` folder.";
    modelFileField.appendChild(modelFileInput);
    modelFileRow.appendChild(modelFileField);
    // Both persist on blur, not on input: a half-typed path should not become
    // the saved one, and the same reasoning as the settings SAVE button.
    for (const inp of [exeInput, modelFileInput]) {
        inp.addEventListener("change", () => { saveConn(); });
        inp.addEventListener("blur", () => { saveConn(); });
    }
    cog.appendChild(exeRow);
    cog.appendChild(modelFileRow);

    /** Is this backend started BY this pack, rather than by its own app? */
    function backendNeedsProgram(name) {
        // The server decides; the panel asks, so adding a backend in llm.py does
        // not mean remembering to hide a field here.
        return (BACKEND_PROGRAMS || []).includes(name);
    }
    function paintPathRows() {
        const need = backendNeedsProgram(backend.value);
        exeRow.style.display = need ? "" : "none";
        modelFileRow.style.display = need ? "" : "none";
    }

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

    // Start / Stop the writer. A backend that holds the card by PROCESS
    // (nInfer) cannot be freed by an unload call, so the loop with a render is
    // inevitably write -> render -> stopped -> write again. Making the second
    // write a console command makes the loop unusable, so it is a button here.
    //
    // Explicit, never automatic: nothing a settings save does starts a process,
    // and nothing a queued render can. Only this button, and only the explicit
    // `action: "start"` verb behind it.
    const serverRow = el("div", "h3e-writer-row");
    const startBtn = button("Start writer", "Start the writer server and load "
        + "the model. A 27B takes about 30 s. The card is unavailable until "
        + "you stop it or queue a render.",
        async () => {
            // D1h: bank the path fields FIRST. Someone who typed a path and
            // pressed Start in the same breath must not have to press Save in
            // between, or the first press fails with "no binary found" on a
            // machine where they can see the path they just typed.
            if (await saveConn()) return serverAction("start");
            say("could not save the settings, so the paths were not used",
                "error");
            return serverAction("start");
        });
    const stopBtn = button("Stop writer", "Stop the writer server and give "
        + "the card back, straight away.", () => serverAction("stop"));
    serverRow.appendChild(startBtn);
    serverRow.appendChild(stopBtn);
    cog.appendChild(serverRow);
    let serverUp = false;
    function paintServer() {
        startBtn.disabled = serverUp;
        stopBtn.disabled = !serverUp;
        startBtn.classList.toggle("h3e-on", serverUp);
    }
    paintServer();

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
        serverUp = Boolean(conn.running);
        paintServer();

        // The backend dropdown, rebuilt from the server's own list so adding a
        // backend in llm.py does not mean editing this file too. Never let the
        // list decide which one is selected: that is the same overwrite bug as
        // the model dropdown, one level up -- a switch that falls back to
        // option[0] would silently move the user off their server.
        backend.innerHTML = "";
        for (const b of (conn.backends || [])) {
            const o = el("option", null, b.label);
            o.value = b.id;
            backend.appendChild(o);
        }
        backend.value = conn.backend || "lmstudio";
          // D1h: which backends have a program this pack starts, and the paths
          // saved for the one shown. Both come from the server, so adding a
          // backend in llm.py does not mean remembering to hide a field here.
          BACKEND_PROGRAMS = (conn.backends || [])
              .filter((b) => b.program).map((b) => b.id);
          const paths = (conn.server_paths || {})[backend.value] || {};
          exeInput.value = paths.exe || "";
          modelFileInput.value = paths.model_file || "";
          // The search found a binary even though none was typed. Say so,
          // because "the field is blank" reads as "nothing is configured" and
          // the next question is always "where do I put it".
          const found = paths.found_exe || "";
          exeInput.title = exeInput.title
              + (found ? `\n\nCurrently starting: ${found}` : "");
          paintPathRows();
        // The address placeholder follows the backend, because :1234 typed into
        // a nInfer panel is the single most likely wrong thing here.
        url.placeholder = conn.backend === "ninfer"
            ? "http://127.0.0.1:8080" : "http://127.0.0.1:1234";

        // Preselect the SAVED model rather than letting the list decide. If the
        // dropdown lands on option[0], the next Save quietly rewrites the
        // configured model to whatever LM Studio happens to list first.
        model.innerHTML = "";
        const list = conn.models || [];
if (!list.length) {
              // The two backends disagree about what an empty list MEANS.
              // On LM Studio it means the server is not started. On nInfer it
              // also means no model is selected, which is a second, separate
              // thing to fix and the one people hit first -- so the message
              // names the backend's own rule rather than guessing which it is.
              const o = el("option", null, conn.model_optional
                  ? "(no models - is the server started?)"
                  : "(no models - this server needs one selected)");
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
            // Name the backend that is actually selected. This said "LM
            // Studio's Developer tab" for everyone, which is advice for a
            // server this panel does not even start -- nInfer is launched by
            // the Start button right here, and a person following the sentence
            // would go looking for a Developer tab that does not exist.
            const what = conn.backend_label
                || (conn.backend === "ninfer" ? "nInfer" : "LM Studio");
            const how = conn.backend === "ninfer"
                ? `Press Start writer below.`
                : `Start it in LM Studio's Developer tab.`;
            say(!conn.online
                ? `No server at ${conn.server_url}. ${how}`
                : conn.any_loaded === false
                    ? `Server is up, but none of its ${list.length} model(s) `
                      + `are loaded. Wait for ${what} to finish loading the `
                      + `model, or load one in LM Studio.`
                    : `Server is up — ${loaded} of ${list.length} model(s) `
                      + `loaded (●).`,
                conn.online && conn.any_loaded !== false ? "hint" : "error");
        }
    }

    async function postConn(patch) {
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
            return true;
        } catch (e) {
            say(String(e), "error");
            return false;
        }
    }

    async function serverAction(action) {
        const b = action === "start" ? startBtn : stopBtn;
        b.disabled = true;
        say(action === "start"
            ? "Starting the writer… a 27B takes about 30 s to load."
            : "Stopping the writer…", "hint");
        try {
            const r = await fetch(LLM_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action }),
            });
            const j = await r.json();
            serverUp = Boolean(j.running);
            say(j.note || (j.ok ? "" : (j.error || "failed")),
                j.ok ? "hint" : "error");
        } catch (e) {
            say(String(e), "error");
        } finally {
            paintServer();
        }
    }

    async function saveConn() {
const patch = { server_url: url.value, keep_warm: unload.checked };
          // Never post a blank model: the server skips blanks, but sending one
          // at all invites the same overwrite bug from the other direction.
          if (model.value) patch.model = model.value;
          // D1h: the per-backend paths, but ONLY for a backend that has them.
          // Posting empty strings for a backend with no program would write two
          // blank keys into its settings entry on every Save, which is noise in
          // a file people read when something goes wrong.
          const here = backend.value;
          if (here && BACKEND_PROGRAMS.includes(here)) {
              patch.servers = {
                  [here]: { exe: exeInput.value.trim(), model_file: modelFileInput.value.trim() },
              };
          }
          if (await postConn(patch)) await loadConn(false);
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
                // Same reason: the rail owns these and the writer never sees
                // them. Without them Accept turned every RefMod row back into
                // a still.
                source: r.source === "refmod" ? "refmod" : null,
                strength: r.strength == null || r.strength === ""
                    ? null : Number(r.strength),
                voice: r.voice ? String(r.voice) : null,
            }));
    }

    async function run() {
        // "Write plan" is the whole thing in one go: propose, then beats.
        await sendCanned("develop", "propose the film, then write the beats for it");
    }

    // The one call every stage button and every free-text line goes through.
    // Free text goes to `develop`, which is the route's own else-branch and the
    // stage that writes beats. It used to send "chat", on the theory that the
    // model would work out what was asked for -- but there has never been a
    // `chat` stage, so every plain sentence died on
    // `unknown stage 'chat': propose, develop, edit, song` before reaching the
    // model at all. Typing "a dynamic fight scene" is the most ordinary thing
    // this box is for, and it was the one input that could not be sent.
    async function sendCanned(stage, text) {
        if (busy) return;
        const msg = String(text || "").trim();
        if (!msg) return;
        setBusy(true);
        chat.value = "";
        addTurn("user", msg);
        say("Thinking… this can take a minute on a large model.", "hint");
        try {
            const r = await fetch(PLAN_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    stage: stage || "develop",
                    brief: msg,
                    // The song comes off the node, not from a field the user
                    // has to fill again here: MEDIA owns the soundtrack, and a
                    // second place to choose it is a second place to forget.
                    song_file: String(widgetByName(node, "master_audio_file")?.value || ""),
                    hops: Number(hops.value) || 3,
                    duration: String(widgetByName(node, "duration")?.value || ""),
                    refs: railRefs(),
                    thread: readThread(),
                    proposal: node.properties?.[PROPOSAL_PROP] || null,
                    // Edit reads the plan off the node, so a change never
                    // depends on the panel having re-sent a register the user
                    // has since hand-edited.
                    shot_plan: String(widgetByName(node, "shot_plan")?.value || ""),
                    ref_plan: String(widgetByName(node, "ref_plan")?.value || ""),
                    pin_mech: String(widgetByName(node, "pin_mech")?.value || ""),
                }),
            });
            const j = await r.json();
            const note = (j.note || j.warnings || []).join?.("\n")
                || (j.warnings || []).join("\n");
            if (j.song) {
                renderSongCard(j.song, note);
            } else if (j.proposal) {
                // A proposal is accepted by READING it, and Accepting it
                // stores the proposal and writes nothing to the plan.
                renderProposalCard(j.proposal, note);
            } else if (j.diffs) {
                renderEditCard(j, note);
            } else if (j.shot_plan) {
                showDraft(j.shot_plan || "", j.ref_plan || "");
                addTurn("assistant", note
                    || `Draft of ${j.shot_plan ? "a plan" : ""}. Accept to put it on the cards.`);
            } else if (typeof j.text === "string" && j.text) {
                addTurn("assistant", j.text);
            }
            if (!j.ok) {
                const errs = (j.errors || []).map((e) => `• ${e}`).join("\n");
                say(errs || (j.error || "the writer failed"), "error");
                if (!j.proposal && !j.shot_plan && !j.text) {
                    addTurn("assistant", j.error || "the writer failed");
                }
            } else if (j.warnings?.length) {
                say((j.warnings || []).map((w) => `• ${w}`).join("\n"), "warn");
            }
        } catch (e) {
            say(String(e), "error");
            addTurn("assistant", String(e));
        } finally {
            setBusy(false);
        }
    }

    // ---- the cards ------------------------------------------------------
    //
    // A card is a STRUCTURED thing the user reads and then acts on. Accept is
    // the only thing that writes the plan, and each card says plainly what its
    // own Accept does -- a proposal writes nothing, a plan replaces the cards,
    // an edit replaces only the shots it names.
    // The three card kinds are separate literals rather than one name built by
    // concatenation: check_ui.py's dead-class check reads the source for
    // literal class strings, and a class only ever assembled at runtime is a
    // class the check cannot see -- so a rule for it would look dead forever.
    function cardShell(kind, title, subtitle) {
        const box = el("div", "h3e-cardbox");
        if (kind === "proposal") box.classList.add("h3e-cardbox-proposal");
        if (kind === "edit") box.classList.add("h3e-cardbox-edit");
        if (kind === "plan") box.classList.add("h3e-cardbox-plan");
        const head = el("div", "h3e-cardbox-head");
        head.appendChild(el("span", "h3e-title", title));
        if (subtitle) head.appendChild(el("span", "h3e-count", subtitle));
        box.appendChild(head);
        const bodyEl = el("div", "h3e-cardbox-body");
        box.appendChild(bodyEl);
        const acts = el("div", "h3e-writer-row");
        box.appendChild(acts);
        draft.style.display = "";
        draftList.textContent = "";
        draftList.appendChild(box);
        return { box, bodyEl, acts };
    }
    function field(label, value) {
        if (value == null || value === "") return null;
        const row = el("div", "h3e-cardbox-row");
        row.appendChild(el("span", "h3e-cardbox-k", label));
        row.appendChild(el("span", "h3e-cardbox-v", String(value)));
        return row;
    }

    function renderProposalCard(p, note) {
        const c = cardShell("proposal", "PROPOSAL",
            p?.shots?.count ? `${p.shots.count} hops` : "");
        const b = c.bodyEl;
        const rows = [
            field("the film", p.one_line),
            field("how it looks", p.look),
            field("in frame", p.frames),
            field("hops", p.shots?.arithmetic || p.shots?.count),
            field("note", note),
        ].filter(Boolean);
        for (const r of rows) b.appendChild(r);
        if (Array.isArray(p.refs) && p.refs.length) {
            b.appendChild(el("div", "h3e-cardbox-k", "the pictures, read as"));
            for (const r of p.refs) {
                b.appendChild(el("div", "h3e-cardbox-row",
                    `@${r.tag || "?"} — ${r.read_as || r.why || "not said"}`));
            }
        }
        if (Array.isArray(p.sections) && p.sections.length > 1) {
            b.appendChild(el("div", "h3e-cardbox-k", "sections"));
            for (const s of p.sections) {
                b.appendChild(el("div", "h3e-cardbox-row",
                    `hops ${s.from ?? "?"}–${s.to ?? "?"}${s.why ? ` — ${s.why}` : ""}`));
            }
        }
        // Accepting a proposal stores it and writes NOTHING to the plan. That
        // is the point of the card: you agree to the shape before a beat is
        // written against it.
        c.acts.appendChild(button("Accept proposal",
            "Keep this as the agreement to write beats against. No plan is "
            + "written yet.", () => {
                node.properties ??= {};
                node.properties[PROPOSAL_PROP] = p;
                writeThread(readThread());
                say("Proposal accepted. Write beats writes to it.", "hint");
                addTurn("assistant", "Proposal accepted. Say 'write the beats'.");
                draft.style.display = "none";
                draftList.textContent = "";
            }));
// Discard means discard. It used to only hide the card, which left an
              // earlier accepted proposal in place -- so a card you had
              // already said no to still shaped the next "write the beats".
              c.acts.appendChild(button("Discard", "Throw this away",
                  () => {
                      node.properties ??= {};
                      node.properties[PROPOSAL_PROP] = null;
                      draft.style.display = "none";
                      draftList.textContent = "";
                  }));
    }

    function renderSongCard(s, note) {
        const c = cardShell("plan", "SONG",
            s.bpm ? `${Math.round(s.bpm)} BPM` : "");
        const b = c.bodyEl;
        const secs = Math.round(s.dur || 0);
        b.appendChild(field("length", secs ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}` : ""));
        b.appendChild(field("bar", s.bar_s
            ? `${s.bar_s.toFixed(3)}s (${s.beats_per_bar || 4}/bar)` : ""));
        // Steadiness first: it says whether the grid above can be trusted, and
        // a cut landing off the bar is otherwise the solver's fault by default.
        if (s.steady != null) {
            b.appendChild(field("steady", `${s.steady}${(s.grid_drift_f != null) ? `, drift ${s.grid_drift_f}f` : ""}`));
        }
        if ((s.sections || []).length) {
            b.appendChild(el("div", "h3e-cardbox-k", "sections — suggestions"));
            for (const x of s.sections) {
                b.appendChild(el("div", "h3e-cardbox-row",
                    `bar ${x.bar ?? "?"}${x.from != null ? ` (${x.from.toFixed(1)}s)` : ""}`
                    + `${x.why ? ` — ${x.why}` : ""}`));
            }
            b.appendChild(el("div", "h3e-note h3e-note-hint",
                s.sections_confidence
                || "Section detection is a suggestion — confirm or move them."));
        }
        if ((s.lines || []).length) {
            b.appendChild(el("div", "h3e-cardbox-k", "lyrics"));
            b.appendChild(el("div", "h3e-chat-text", s.lines.join("\n")));
        }
        // No Accept. A song card is a MEASUREMENT, and nothing about the plan
        // changes because you read it. The bar grid is the trustworthy part and
        // the user should see it before anything is written.
        c.acts.appendChild(button("Propose the film", "Ask for a film cut to "
            + "this song — hop lengths on bar lines, cuts at the sections you "
            + "confirm above.", () => {
                draft.style.display = "none";
                draftList.textContent = "";
                sendCanned("develop", `propose a music video cut to this song. `
                    + `Sections: ${(s.sections || []).map((x) => `bar ${x.bar}`).join(", ") || "none yet"}.`
                    + ` Nobody speaks: the song carries it.`);
            }));
        c.acts.appendChild(button("Close", "Dismiss this card",
            () => { draft.style.display = "none"; draftList.textContent = ""; }));
    }

    function renderEditCard(j, note) {
        const c = cardShell("edit", "EDIT",
            (j.diffs || []).length ? `${j.diffs.length} shot(s)` : "");
        const b = c.bodyEl;
        for (const d of (j.diffs || [])) {
            b.appendChild(el("div", "h3e-cardbox-k",
                `hop ${d.hop} · ${d.shot_id}`));
            // Word-level diff, so the user sees what the edit DID rather than
            // two paragraphs and a hunch.
            if (d.before) {
                const before = el("div", "h3e-diff-before", d.before);
                b.appendChild(before);
            }
            if (d.after) b.appendChild(el("div", "h3e-diff-after", d.after));
            const rem = (d.diff?.removed || []);
            const add = (d.diff?.added || []);
            if (rem.length || add.length) {
                const bits = [];
                if (rem.length) bits.push(`− ${rem.join(" ")}`);
                if (add.length) bits.push(`+ ${add.join(" ")}`);
                b.appendChild(el("div", "h3e-diff-words", bits.join("   ")));
            }
        }
        if (note) b.appendChild(el("div", "h3e-cardbox-row", note));
c.acts.appendChild(button("Accept edit",
              "Put these shots on the cards. Every other shot is untouched.",
              () => {
                  // showDraft RENDERS INTO draftList, so the two lines that
                  // used to follow this call wiped the edit on its way in --
                  // the button accepted a draft nobody could see. Showing it
                  // is the last thing that happens, and the draft panel's own
                  // Accept is what writes the cards.
                  draft.style.display = "block";
                  showDraft(j.shot_plan || "", j.ref_plan || "");
                  say("Edit ready. Review it, then Accept in the panel.",
                      "hint");
              }));
        c.acts.appendChild(button("Discard", "Keep the cards as they are",
            () => { draft.style.display = "none"; draftList.textContent = ""; }));
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
        // The thread and the accepted proposal. Properties arrive with the
        // workflow AFTER the mount, so this is the only place they can be put
        // back -- without it a reload drops the whole conversation and the
        // accepted proposal, and the writer starts from nothing.
        paintThread();
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
