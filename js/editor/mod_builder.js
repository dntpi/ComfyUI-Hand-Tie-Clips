/**
 * The RefMod builder's panel: sources on the left, dials on the right.
 *
 * `mod_plan` is the only source of truth for what goes into the mod, exactly
 * as `shot_plan` / `ref_plan` are for the chain node. The panel reads that
 * widget and writes straight back, so a builder configured here and one typed
 * by hand are the same file.
 *
 * The stills list is repeatable rather than a fixed set of slots. Fixed slots
 * are why the other packs ship a second node whose whole job is toggling
 * inputs on and off: a socket cannot be empty gracefully, and a gap in a dense
 * numbering renames everything after it -- the ordinal instability `refs.py`
 * exists to prevent. A list has no gaps to express.
 *
 * Dials are drawn with `run_panel`'s own `fieldFor`, not a second copy of it,
 * so they keep reading `widgetType()` / `widgetOptions()` and keep their
 * tooltips from `INPUT_TYPES`.
 */
import {
    el, button, widgetByName, widgetType, tooltipFor, isolateEvents,
} from "./widget_utils.js";
import { fieldFor, labelFor } from "./run_panel.js";
import { createPicker, onFilesChanged, extKind } from "./media_picker.js";
import { createTrimBar } from "./trim_bar.js";

const ESTIMATE_URL = "/h3_ref_chain/refmod_estimate";
const PLAN_URL = "/h3_ref_chain/refmod_plan";

/** Dials the panel draws, so the mount hides exactly these. */
const DIALS = ["name", "mode", "pool", "latent_frames", "refine_steps",
    "max_tokens", "subfolder", "save", "build"];

/** Drawn only when mode is training; pooling is what they configure. */
const POOL_ONLY = new Set(["pool", "latent_frames", "refine_steps"]);

export const BUILDER_WIDGETS = ["mod_plan"].concat(DIALS);

function parseModPlan(text) {
    const raw = String(text || "").trim();
    if (!raw) return { stills: [], clip: null, voice: "" };
    let data;
    try {
        data = JSON.parse(raw);
    } catch (err) {
        return null;
    }
    if (Array.isArray(data)) data = { stills: data };
    if (!data || typeof data !== "object") return null;
    const stills = (Array.isArray(data.stills) ? data.stills : [])
        .map((s) => (typeof s === "string" ? s : String(s?.file || s?.name || "")))
        .filter(Boolean);
    let clip = null;
    const c = data.clip;
    if (c) {
        const file = typeof c === "string" ? c : String(c.file || c.name || "");
        if (file) {
            clip = {
                file,
                start: Number(c.start) || 0,
                end: Number(c.end) || 0,
                frames: Number(c.frames) || 0,
            };
        }
    }
    let voice = typeof data.voice === "string" ? data.voice : "";
    // A sound file in the clip slot is what a voice looked like before
    // this slot existed. Move it rather than dead-ending on the parser,
    // which now refuses it: the author's intent is not in doubt.
    if (clip && extKind(clip.file) === "audio") {
        if (!voice) voice = clip.file;
        clip = null;
        console.log("[HandTieClips] moved a sound file from the RefMod clip"
                    + " slot to its voice slot");
    }
    return { stills, clip, voice };
}

function modPlanToJson(plan) {
    if (!plan.stills.length && !plan.clip && !plan.voice) return "";
    const out = {};
    if (plan.stills.length) out.stills = plan.stills.map((f) => ({ file: f }));
    if (plan.clip) {
        const c = { file: plan.clip.file };
        if (plan.clip.start) c.start = Math.round(plan.clip.start * 1000) / 1000;
        if (plan.clip.end) c.end = Math.round(plan.clip.end * 1000) / 1000;
        if (plan.clip.frames) c.frames = plan.clip.frames;
        out.clip = c;
    }
    // Omitted when empty, so a mod_plan written before the voice
    // slot existed round-trips through the panel byte for byte.
    if (plan.voice) out.voice = plan.voice;
    return JSON.stringify(out, null, 2);
}

export function createModBuilder(node, { onChange } = {}) {
    const root = el("div", "h3e-root h3e-mod");
    isolateEvents(root);

    const planWidget = widgetByName(node, "mod_plan");

    // A mod_plan that does not parse is NOT silently replaced with an empty
    // one: the panel would then write "" over it on the next edit and destroy
    // whatever the author was in the middle of repairing.
    let planBad = false;
    const readPlan = () => {
        const parsed = parseModPlan(planWidget?.value);
        planBad = parsed === null;
        return parsed || { stills: [], clip: null, voice: "" };
    };
    let plan = readPlan();

    const writePlan = () => {
        if (!planWidget) return;
        const text = modPlanToJson(plan);
        planWidget.value = text;
        // Setting `.value` alone leaves anything bound to this widget unaware
        // that it changed -- the same reason the media strip and the run panel
        // both fire the callback rather than assigning.
        try {
            planWidget.callback?.(text, node.graph?.canvas, node, undefined, undefined);
        } catch (err) {
            console.error("[HandTieClips] mod_plan callback failed:", err);
        }
        planBad = false;
        node.graph?.setDirtyCanvas(true, true);
        onChange?.();
        estimate();
    };

    /* -- sources ---------------------------------------------------------- */

    const left = el("div", "h3e-mod-left");

    const stillsSec = el("div", "h3e-section h3e-mod-stills");
    const stillsHead = el("div", "h3e-mod-head");
    stillsHead.appendChild(el("span", "h3e-title", "STILLS"));
    const stillsCount = el("span", "h3e-count");
    stillsHead.appendChild(stillsCount);
    stillsHead.appendChild(button("+ add", "Add another still. Each one becomes a latent frame of the mod.", () => {
        plan.stills.push("");
        writePlan();
        renderStills();
    }));
    stillsSec.appendChild(stillsHead);
    const stillsGrid = el("div", "h3e-mod-grid");
    stillsSec.appendChild(stillsGrid);
    stillsSec.appendChild(el("div", "h3e-note h3e-note-hint",
        "Several views of one character: face, upper body, profile, outfit. "
        + "Order is the order they occupy latent frames."));
    left.appendChild(stillsSec);

    function renderStills() {
        stillsGrid.textContent = "";
        stillsCount.textContent = plan.stills.length
            ? `${plan.stills.length}` : "none yet";
        plan.stills.forEach((name, i) => {
            const cell = el("div", "h3e-mod-cell");
            const pick = createPicker({
                kind: "image",
                title: "Drop a still here, or click to browse.",
                get: () => plan.stills[i] || "",
                set: (v) => { plan.stills[i] = v; },
                onChange: () => { writePlan(); renderStills(); },
            });
            cell.appendChild(pick.root);
            pick.render();
            const bar = el("div", "h3e-mod-cellbar");
            bar.appendChild(button("◀", "Move earlier", () => {
                if (i === 0) return;
                [plan.stills[i - 1], plan.stills[i]] = [plan.stills[i], plan.stills[i - 1]];
                writePlan();
                renderStills();
            }));
            bar.appendChild(el("span", "h3e-count", `${i + 1}`));
            bar.appendChild(button("▶", "Move later", () => {
                if (i >= plan.stills.length - 1) return;
                [plan.stills[i + 1], plan.stills[i]] = [plan.stills[i], plan.stills[i + 1]];
                writePlan();
                renderStills();
            }));
            bar.appendChild(button("remove", "Drop this still from the mod", () => {
                plan.stills.splice(i, 1);
                writePlan();
                renderStills();
            }, "h3e-btn h3e-x"));
            cell.appendChild(bar);
            stillsGrid.appendChild(cell);
        });
        if (!plan.stills.length) {
            stillsGrid.appendChild(el("div", "h3e-note h3e-note-hint",
                "No stills yet. A single face still is the minimum for an identity mod."));
        }
    }

    const clipSec = el("div", "h3e-section h3e-mod-clip");
    const clipHead = el("div", "h3e-mod-head");
    clipHead.appendChild(el("span", "h3e-title", "CLIP"));
    const clipCount = el("span", "h3e-count", "optional");
    clipHead.appendChild(clipCount);
    clipSec.appendChild(clipHead);

    const clipRow = el("div", "h3e-mod-cliprow");
    const clipPick = createPicker({
        kind: "video",
        title: "Drop a clip here. Its frames follow the stills.",
        get: () => plan.clip?.file || "",
        set: (v) => {
            plan.clip = v ? { file: v, start: 0, end: 0, frames: 0 } : null;
        },
        onChange: () => { writePlan(); renderClip(); },
    });
    clipRow.appendChild(clipPick.root);
    clipPick.render();
    const clipTrim = el("div", "h3e-mod-trim");
    clipRow.appendChild(clipTrim);
    clipSec.appendChild(clipRow);
    clipSec.appendChild(el("div", "h3e-note h3e-note-hint",
        "Motion and a range of angles, from one take. Frames are snapped to "
        + "the H3 17k+5 grid; how many latent frames they become is the VAE's call."));
    left.appendChild(clipSec);

    /* The voice. Not encoded into the latent -- a RefMod holds pictures --
     * but recorded in the file, so a reference row citing this mod speaks
     * with it without naming it a second time. That is the difference
     * between a face and a character. */
    const voiceSec = el("div", "h3e-mod-sec");
    const voiceHead = el("div", "h3e-mod-head", "VOICE");
    const voiceCount = el("span", "h3e-count", "optional");
    voiceHead.appendChild(voiceCount);
    voiceSec.appendChild(voiceHead);
    const voicePick = createPicker({
        kind: "audio",
        title: "Drop a voice here. It is stored by name, not encoded.",
        get: () => plan.voice || "",
        set: (v) => { plan.voice = v || ""; },
        onChange: () => { writePlan(); renderVoice(); },
    });
    voiceSec.appendChild(voicePick.root);
    voicePick.render();
    voiceSec.appendChild(el("div", "h3e-note h3e-note-hint",
        "A timbre reference. It rides the hops the reference row rides, "
        + "hop 2+ only as voice_every_hop allows. The sound file "
        + "stays in h3_refs -- the mod names it, it is not inside it."));
    left.appendChild(voiceSec);

    function renderVoice() {
        voiceCount.textContent = plan.voice || "optional";
        voicePick.render();
    }

    // The live bar, so a repaint can be told to let go of its <video> element.
    let clipBar = null;

    function renderClip() {
        clipBar?.destroy?.();
        clipBar = null;
        clipTrim.textContent = "";
        clipCount.textContent = plan.clip ? plan.clip.file : "optional";
        if (!plan.clip) return;
        clipBar = createTrimBar({
            kind: "video",
            name: () => plan.clip?.file || "",
            get: () => ({ start: plan.clip.start, end: plan.clip.end }),
            set: (s, e) => {
                plan.clip.start = Number(s) || 0;
                plan.clip.end = Number(e) || 0;
            },
            onChange: () => { writePlan(); },
        });
        clipTrim.appendChild(clipBar.root);
        clipBar.render();
    }

    /* -- dials ------------------------------------------------------------ */

    const right = el("div", "h3e-mod-right");
    const dialSec = el("div", "h3e-section h3e-mod-dials");
    dialSec.appendChild(el("span", "h3e-title", "BUILD"));
    const dialGrid = el("div", "h3e-grid");
    dialSec.appendChild(dialGrid);
    right.appendChild(dialSec);

    const owned = [];
    const readers = [];

    function modeValue() {
        return String(widgetByName(node, "mode")?.value || "encode");
    }

    function renderDials() {
        dialGrid.textContent = "";
        owned.length = 0;
        readers.length = 0;
        const pooled = modeValue() === "training";
        const rejected = [];
        for (const name of DIALS) {
            if (!pooled && POOL_ONLY.has(name)) continue;
            const w = widgetByName(node, name);
            if (!w) continue;
            const f = fieldFor(node, w, () => {
                // mode decides whether the pooling dials exist at all, so a
                // change to it rebuilds this column rather than leaving three
                // inert controls on screen.
                if (name === "mode") renderDials();
                paintSummary();
                onChange?.();
            });
            if (!f) {
                rejected.push(`${name}:${widgetType(w) || "?"}`);
                continue;
            }
            const l = el("label", "h3e-field");
            l.appendChild(el("span", null, labelFor(w)));
            l.appendChild(f.input);
            const tip = tooltipFor(node, w);
            if (tip) { l.title = tip; f.input.title = tip; }
            dialGrid.appendChild(l);
            owned.push(name);
            if (f.read) readers.push(f.read);
        }
        if (rejected.length) {
            console.warn("[HandTieClips] RefMod dials left native:", rejected.join(", "));
        }
        // Reload the sources a saved mod was built from. The header carries
        // the mod_plan it was written with, so adding a fourth still to a
        // mod built last month does not mean finding the graph that built
        // it. Header read only -- no tensor, no VAE.
        const load = button("load sources",
            "Read back the stills and clip this name was last built from, "
            + "and put them in the panel. Replaces what is here now.",
            () => loadStoredPlan());
        load.classList.add("h3e-run-wide");
        dialGrid.appendChild(load);
        if (!pooled) {
            dialGrid.appendChild(el("div", "h3e-note h3e-note-hint h3e-run-wide",
                "encode keeps the full VAE latent. pool, latent_frames and "
                + "refine_steps belong to training and are hidden here because "
                + "they would not be applied."));
        }
    }

    /* -- summary ---------------------------------------------------------- */

    const summary = el("div", "h3e-note h3e-mod-summary");
    right.appendChild(summary);

    async function loadStoredPlan() {
        const name = String(widgetByName(node, "name")?.value || "").trim();
        if (!name) { setSummary("error", "Name a mod first."); return; }
        try {
            const res = await fetch(PLAN_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            const data = await res.json();
            if (!data.ok) { setSummary("error", data.error || "read failed"); return; }
            const stored = data.plan || {};
            const stills = Array.isArray(stored.stills) ? stored.stills : [];
            if (!stills.length && !stored.clip && !stored.voice) {
                // Not an error. A mod built through the sockets, or before
                // the header carried this, has nothing to give back.
                setSummary("hint", `${name} carries no source list.`);
                return;
            }
            plan.stills = stills.slice();
            plan.clip = stored.clip ? { ...stored.clip } : null;
            plan.voice = stored.voice || "";
            writePlan();
            renderStills();
            renderClip();
            renderVoice();
        } catch (err) {
            console.warn("[HandTieClips] RefMod plan read failed:", err);
            setSummary("error", "could not reach the server");
        }
    }

    let est = null;
    let estSeq = 0;

    function estimate() {
        const seq = ++estSeq;
        if (!plan.stills.filter(Boolean).length && !plan.clip) {
            est = null;
            paintSummary();
            return;
        }
        fetch(ESTIMATE_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                stills: plan.stills.filter(Boolean),
                clip: plan.clip,
                voice: plan.voice || "",
            }),
        })
            .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
            .then((j) => {
                // A slow answer to a plan the user has already changed must not
                // overwrite the fresh one.
                if (seq !== estSeq) return;
                est = j && j.ok ? j : null;
                paintSummary(j && !j.ok ? j.error : "");
            })
            .catch((err) => {
                if (seq !== estSeq) return;
                est = null;
                paintSummary();
                console.warn("[HandTieClips] RefMod estimate unavailable:", err);
            });
    }

    // hint / warn / error are three different notes and the stylesheet keys
    // them off a modifier on the note itself, not on anything inside it.
    function setSummary(level, text) {
        summary.className = "h3e-note h3e-mod-summary"
            + (level ? ` h3e-note-${level}` : "");
        summary.textContent = text;
    }

    function paintSummary(error) {
        if (planBad) {
            setSummary("error",
                "mod_plan is not valid JSON. It is left exactly as written; "
                + "fix it in the widget and the panel will pick it up.");
            return;
        }
        if (error) {
            setSummary("error", String(error));
            return;
        }
        const nStills = plan.stills.filter(Boolean).length;
        if (!nStills && !plan.clip) {
            setSummary("hint",
                "Add a still to begin. Build writes models/refmods/<name>.safetensors; "
                + "put that name in a reference row's file field.");
            return;
        }
        const bits = [];
        if (nStills) bits.push(`${nStills} still${nStills === 1 ? "" : "s"}`);
        if (est?.clip_frames) bits.push(`${est.clip_frames}f clip`);
        else if (plan.clip) bits.push("clip");
        let line = bits.join(" + ");
        if (est?.canvas) {
            line += ` → ${est.canvas[0]}×${est.canvas[1]} canvas, `
                + `${est.per_frame_tokens} tokens a frame`;
            if (modeValue() === "encode" && nStills && !plan.clip) {
                line += `, ${est.still_tokens} total`;
            }
        }
        if (plan.voice) {
            line += `, voice ${plan.voice}`;
        }
        if (est?.clip_dropped) {
            line += ` — ${est.clip_dropped} frame(s) trimmed by the 17k+5 snap.`;
        }
        // The server phrases these: a name can be absent, the wrong kind,
        // or unreadable, and "not in h3_refs" was wrong for two of the three.
        if (est?.problems?.length) {
            setSummary("error", `${line} — ${est.problems.join("; ")}`);
            return;
        }
        if (modeValue() === "training") {
            setSummary("", `${line} — training pools this down; a face needs encode.`);
            return;
        }
        setSummary("hint", line);
    }

    root.appendChild(left);
    root.appendChild(right);

    const stopFiles = onFilesChanged(
        () => { renderStills(); renderClip(); renderVoice(); });

    function refresh() {
        plan = readPlan();
        renderStills();
        renderClip();
        renderVoice();
        renderDials();
        estimate();
    }

    refresh();

    return {
        root,
        refresh,
        /** Widget names the panel drew, so the mount hides exactly those. */
        ownedNames: () => ["mod_plan"].concat(owned),
        dispose: () => { stopFiles?.(); clipBar?.destroy?.(); },
    };
}
