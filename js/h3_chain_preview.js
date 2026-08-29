/**
 * H3 Chain Preview -- the chain's progress panel, on its own node.
 *
 * Structure follows ComfyUI-KJNodes' preview_override panel (image area that
 * absorbs slack, a fixed-height stats panel, a drag grip between them). The
 * grip itself follows the sibling MiniMaxH3-Contex-Loop pack's review node
 * instead: it uses pointer capture and reads the UNSCALED offsetHeight, because
 * getBoundingClientRect() includes ComfyUI's canvas zoom and every release then
 * compounds a smaller screen-space height into the saved value.
 *
 * Deliberately NOT overriding computeSize/computeLayoutSize the way the editor
 * panel does. Frontend 1.49.6 distributes a node's spare vertical space across
 * its widgets by growing each from minHeight toward maxHeight; pinning both to
 * the same value opts the widget out of that entirely, which is why the editor
 * panel does not respond to node resize. Here we let the frontend size it and
 * do the internal layout in CSS.
 *
 * There is no JS runtime on the machine this was written on, so this file is
 * static-checked only.
 */

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

/* Both ids. The pack registers the pre-rename id as a deprecated subclass so
 * workflows saved before 2026-08-29 still load. If this check knew only the
 * new id those nodes would come up with NO editor at all, which looks exactly
 * like the rename having broken the pack. */
const NODE_TYPES = new Set(["HTCChainPreview", "H3ChainPreview"]);
const CHAIN_TYPES = new Set(["HandTieClips", "H3RefChain"]);
const STYLE_ID = "h3rc-style";
const PANEL_PROP = "h3rcPanelH";
const _cssUrl = new URL("./h3_ref_chain.css", import.meta.url).href;

function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const link = document.createElement("link");
    link.id = STYLE_ID;
    link.rel = "stylesheet";
    link.href = _cssUrl;
    document.head.appendChild(link);
}

function el(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text != null) n.textContent = text;
    return n;
}

/** Mirrors getNodeByExecutionId (not exported); subgraph-aware. */
function findNodeByQualifiedId(rootGraph, qid) {
    if (!rootGraph || qid == null) return null;
    const parts = String(qid).split(":");
    let graph = rootGraph;
    for (let i = 0; i < parts.length - 1; i++) {
        const parentId = parseInt(parts[i], 10);
        if (!Number.isFinite(parentId)) return null;
        const parentNode = graph?.getNodeById?.(parentId);
        if (!parentNode?.subgraph) return null;
        graph = parentNode.subgraph;
    }
    const leafId = parseInt(parts[parts.length - 1], 10);
    if (!Number.isFinite(leafId)) return null;
    return graph?.getNodeById?.(leafId) || null;
}

/**
 * Walk back from this node's `images` input to the chain node feeding it.
 *
 * Live progress events carry the *chain's* node id, so a graph with two chains
 * needs to know which one belongs to this panel. Returns null when the input is
 * unwired or the upstream is something else, in which case the panel falls back
 * to accepting any chain's events -- correct for the common single-chain graph,
 * and labelled as such so it cannot silently mislead.
 */
function upstreamChainId(node) {
    let hops = 0;
    let current = node;
    let slotName = "images";
    while (current && hops < 12) {
        const input = (current.inputs || []).find((s) => s.name === slotName);
        if (!input || input.link == null) return null;
        const link = current.graph?.links?.[input.link];
        if (!link) return null;
        const src = current.graph.getNodeById?.(link.origin_id);
        if (!src) return null;
        if (CHAIN_TYPES.has(src.type)) return String(src.id);
        // Pass through anything that forwards images (reroutes, other previews).
        const forwarded = (src.inputs || []).find((s) => s.type === "IMAGE");
        if (!forwarded) return null;
        current = src;
        slotName = forwarded.name;
        hops += 1;
    }
    return null;
}

function clock(seconds) {
    if (!Number.isFinite(seconds)) return "–";
    const m = Math.floor(seconds / 60);
    const s = seconds - m * 60;
    return m ? `${m}:${s.toFixed(1).padStart(4, "0")}` : `${s.toFixed(1)}s`;
}

function mountPanel(node) {
    ensureStyles();

    const root = el("div", "h3rc-preview");

    const frame = el("div", "h3rc-frame");
    const imgA = el("img", "h3rc-img");
    const imgB = el("img", "h3rc-img h3rc-seam-b");
    const placeholder = el("div", "h3rc-placeholder", "waiting for sample…");
    const seamLine = el("div", "h3rc-seam-line");
    const tagA = el("div", "h3rc-seam-tag h3rc-tag-a", "prev");
    const tagB = el("div", "h3rc-seam-tag h3rc-tag-b", "this hop");
    frame.append(imgA, imgB, seamLine, tagA, tagB, placeholder);
    root.appendChild(frame);

    const bar = el("div", "h3rc-bar");
    const fill = el("div", "h3rc-bar-fill");
    bar.appendChild(fill);
    root.appendChild(bar);

    const grip = el("div", "h3rc-grip");
    grip.setAttribute("role", "separator");
    grip.setAttribute("aria-orientation", "horizontal");
    grip.title = "Drag to resize · double-click to reset";
    root.appendChild(grip);

    const panel = el("div", "h3rc-panel");
    const line = el("div", "h3rc-line");
    line.appendChild(el("span", "h3rc-status-label", "status"));
    const statusValue = el("span", "h3rc-status-value", "idle");
    line.appendChild(statusValue);
    const mech = el("span", "h3rc-mech");
    mech.style.display = "none";
    line.appendChild(mech);
    panel.appendChild(line);
    const meta = el("div", "h3rc-meta");
    panel.appendChild(meta);
    root.appendChild(panel);

    /* -- resize grip ----------------------------------------------------- */

    const DEFAULT_PANEL_H = 62;
    const applyPanelH = (px) => { panel.style.height = `${Math.max(40, px)}px`; };
    if (typeof node.properties?.[PANEL_PROP] === "number") {
        applyPanelH(node.properties[PANEL_PROP]);
    }

    let dragFrom = null;
    grip.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        grip.setPointerCapture(e.pointerId);
        dragFrom = { y: e.clientY, h: panel.offsetHeight };
    });
    grip.addEventListener("pointermove", (e) => {
        if (!dragFrom) return;
        // Divide by canvas zoom: clientY is screen space, offsetHeight is not.
        const scale = app.canvas?.ds?.scale || 1;
        const dy = (e.clientY - dragFrom.y) / scale;
        const max = Math.max(40, root.clientHeight - 90);
        applyPanelH(Math.min(max, dragFrom.h - dy));
    });
    const endDrag = (e) => {
        if (!dragFrom) return;
        dragFrom = null;
        try { grip.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
        // offsetHeight, not getBoundingClientRect().height -- the latter is
        // zoom-scaled and would shrink the saved height on every release.
        if (!node.properties) node.properties = {};
        node.properties[PANEL_PROP] = panel.offsetHeight;
        node.graph?.change?.();
    };
    grip.addEventListener("pointerup", endDrag);
    grip.addEventListener("pointercancel", endDrag);
    grip.addEventListener("dblclick", (e) => {
        e.preventDefault();
        e.stopPropagation();
        applyPanelH(DEFAULT_PANEL_H);
        if (!node.properties) node.properties = {};
        node.properties[PANEL_PROP] = DEFAULT_PANEL_H;
    });

    // Keep canvas gestures out of the panel, but let the wheel through so the
    // graph still zooms with the pointer over the preview.
    for (const name of ["pointerdown", "mousedown", "click", "dblclick", "contextmenu"]) {
        root.addEventListener(name, (e) => e.stopPropagation());
    }

    /* -- state ------------------------------------------------------------ */

    const setImage = (target, b64) => {
        target.src = `data:image/jpeg;base64,${b64}`;
        target.style.display = "block";
        placeholder.style.display = "none";
    };

    const api_ = {
        set(data) {
            if (data?.status != null) {
                const s = String(data.status).replace(/\s+/g, " ").trim();
                // Hard clip: this strip is one line, and passing the prompt
                // dump through it once already turned the panel into the prompt.
                statusValue.textContent = s.length > 80 ? `${s.slice(0, 77)}…` : s;
                statusValue.title = s;
            }
            if (typeof data?.image === "string") setImage(imgA, data.image);
            if (typeof data?.seam_image === "string") {
                setImage(imgB, data.seam_image);
                frame.classList.add("h3rc-showing-seam");
            } else if (data?.image) {
                imgB.style.display = "none";
                frame.classList.remove("h3rc-showing-seam");
            }

            if (typeof data?.frac === "number") {
                fill.style.width = `${Math.round(data.frac * 100)}%`;
                fill.classList.toggle("h3rc-done", data.frac >= 1);
            } else if (data?.hop && data?.total) {
                fill.style.width = `${Math.round((data.hop / data.total) * 100)}%`;
            }

            if (data?.pin_mech) {
                mech.style.display = "";
                mech.className = `h3rc-mech h3rc-mech-${data.pin_mech}`;
                mech.textContent = data.pin_mech === "motion_context"
                    ? "latent pin" : "pixel pin";
                mech.title = data.pin_mech === "motion_context"
                    ? "Motion-Context pinned the previous hop's sampler latent — the working join."
                    : "AddGuide pinned decoded pixels. This is the fallback: Motion-Context was "
                      + "unavailable, the overlap is not one of its context lengths, or the previous "
                      + "hop was cached by a build older than the latent sidecar.";
            }

            const bits = [];
            if (data?.hop && data?.total) bits.push(["hop", `${data.hop}/${data.total}`]);
            if (data?.frames != null && data?.of_frames) bits.push(["frames", `${data.frames}/${data.of_frames}`]);
            else if (data?.frames != null) bits.push(["frames", String(data.frames)]);
            if (data?.cached) bits.push(["cache", `hit ${data.key || ""}`.trim()]);
            if (data?.seed != null) bits.push(["seed", String(data.seed)]);
            if (data?.steps != null) bits.push(["steps", String(data.steps)]);
            if (data?.tone) bits.push(["tone", String(data.tone)]);
            if (data?.video_s != null) bits.push(["video", clock(data.video_s)]);
            if (data?.audio_s != null) bits.push(["audio", clock(data.audio_s)]);
            if (data?.drift_ms != null) {
                bits.push(["A/V drift", `${data.drift_ms > 0 ? "+" : ""}${data.drift_ms} ms`]);
            }
            if (data?.width && data?.height) bits.push(["size", `${data.width}x${data.height}`]);
            if (bits.length) {
                meta.textContent = "";
                for (const [k, v] of bits) {
                    const cell = el("span", null, `${k} `);
                    cell.appendChild(el("b", null, v));
                    meta.appendChild(cell);
                }
            }
        },
        reset() {
            imgA.style.display = "none";
            imgB.style.display = "none";
            frame.classList.remove("h3rc-showing-seam");
            placeholder.style.display = "";
            placeholder.textContent = "waiting for sample…";
            statusValue.textContent = "queued";
            statusValue.title = "";
            mech.style.display = "none";
            meta.textContent = "";
            fill.style.width = "0";
            fill.classList.remove("h3rc-done");
        },
        error(msg) {
            placeholder.style.display = "";
            placeholder.textContent = msg;
        },
    };

    node._h3ChainPreview = api_;
    const widget = node.addDOMWidget("preview", "h3rc_chain_preview", root, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 180,
        getMaxHeight: () => 4000,
    });
    return widget;
}

app.registerExtension({
    name: "HandTieClips.ChainPreview",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (!NODE_TYPES.has(nodeData?.name)) return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            mountPanel(this);
            this.setSize([
                Math.max(this.size?.[0] ?? 360, 360),
                Math.max(this.size?.[1] ?? 420, 420),
            ]);
            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            delete this._h3ChainPreview;
            return onRemoved?.apply(this, arguments);
        };
    },
});

/* Live events from the chain node carry the CHAIN's node id, so each panel takes
 * the ones from the chain feeding it. With no resolvable upstream a lone panel
 * still shows the only chain running rather than sitting blank. */
function previewNodes(graph, out) {
    for (const n of graph?._nodes || []) {
        if (NODE_TYPES.has(n.type) && n._h3ChainPreview) out.push(n);
        if (n.subgraph) previewNodes(n.subgraph, out);
    }
    return out;
}

api.addEventListener("h3_refchain_preview", (e) => {
    const data = e.detail;
    if (!data) return;
    const panels = previewNodes(app.graph, []);
    if (!panels.length) return;
    const chainId = data.node_id == null ? null : String(data.node_id);
    const matched = panels.filter((n) => upstreamChainId(n) === chainId);
    const targets = matched.length ? matched
        : (panels.length === 1 && upstreamChainId(panels[0]) === null ? panels : []);
    for (const n of targets) n._h3ChainPreview.set(data);
});

/* The preview node's own execution: final frame count, size and A/V drift. */
api.addEventListener("h3_chain_preview", (e) => {
    const data = e.detail;
    if (!data || data.node_id == null) return;
    const node = findNodeByQualifiedId(app.graph, data.node_id);
    node?._h3ChainPreview?.set(data);
});

/* A new run starts blank rather than showing the previous run's last frame --
 * the old panel defined reset() and never called it. */
api.addEventListener("execution_start", () => {
    for (const n of previewNodes(app.graph, [])) n._h3ChainPreview.reset();
});
