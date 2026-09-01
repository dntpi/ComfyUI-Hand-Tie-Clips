/**
 * Pick a window inside a media file, by dragging it.
 *
 * A 173 s track under a 9.4 s chain takes the first 9.4 s, which for a mastered
 * track is the intro. Two number boxes would technically fix that and nobody
 * would ever use them, because you cannot type the timestamp of a downbeat you
 * have not found yet. So: a picture of the file, and two grips.
 *
 * **The peaks come from the server.** The obvious build fetches the file and
 * runs `decodeAudioData`, which for this track is ~66 MB of Float32 held in the
 * tab, per control, to draw something 240 pixels wide. `/h3_ref_chain/peaks`
 * returns 240 numbers instead. PromptMasterLD has four separate trim controls
 * and not one `decodeAudioData` between them; it reached the same conclusion.
 *
 * Same rendering-layer contract as the rest of the editor: `get()` and `set()`
 * are the store, this owns nothing, and the JSON tab keeps working.
 */
import { el } from "./widget_utils.js";

const PEAKS_URL = "/h3_ref_chain/peaks";
const BUCKETS = 240;

/** The shortest window the control will commit. Mirrors media.MIN_WINDOW_S. */
const MIN_WINDOW_S = 0.05;

/* -- the peak cache, shared by every bar on the canvas -------------------- */

// Keyed by filename, NOT stored on the caller's object: 240 floats per
// reference inside a widget value is exactly the bloat that broke workflow
// saving when thumbnails went in there.
const _peaks = new Map();
const _inflight = new Set();
const _waiters = new Set();

/** Fetch once per file, however many bars ask. -> {peaks, seconds} | null */
function peaksFor(name) {
    if (!name) return Promise.resolve(null);
    const hit = _peaks.get(name);
    if (hit) return Promise.resolve(hit);
    if (_inflight.has(name)) {
        // render() runs on every keystroke elsewhere in the editor. Without
        // this guard a panel with three bars open would refetch on each one.
        return new Promise((resolve) => {
            const tick = () => {
                if (_peaks.has(name)) resolve(_peaks.get(name));
                else if (_inflight.has(name)) _waiters.add(setTimeout(tick, 60));
                else resolve(null);
            };
            tick();
        });
    }
    _inflight.add(name);
    return fetch(PEAKS_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, n: BUCKETS }),
    })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((j) => {
            const got = { peaks: j?.peaks || [], seconds: Number(j?.seconds) || 0 };
            _peaks.set(name, got);
            return got;
        })
        .catch((err) => {
            console.error(`[HandTieClips] peaks for ${name} unavailable:`, err);
            return null;
        })
        .finally(() => _inflight.delete(name));
}

/** Drop a file's cached peaks, so a replaced file redraws. */
export function forgetPeaks(name) {
    if (name) _peaks.delete(name);
    else _peaks.clear();
}

/* -- helpers ------------------------------------------------------------- */

function fmt(s) {
    const v = Math.max(0, Number(s) || 0);
    const m = Math.floor(v / 60);
    return m ? `${m}:${(v % 60).toFixed(2).padStart(5, "0")}` : `${v.toFixed(2)} s`;
}

function viewUrl(name) {
    return `/view?filename=${encodeURIComponent(name)}`
        + `&subfolder=h3_refs&type=input&t=${Date.now()}`;
}

/* -- the control --------------------------------------------------------- */

/**
 * @param kind      "audio" | "video" -- which element to build.
 * @param name      () => basename in the reference folder, or "".
 * @param get       () => ({start, end}) in seconds. end 0 = to the end.
 * @param set       (start, end) => void.
 * @param onChange  called after a committed drag.
 */
export function createTrimBar({ kind = "audio", name, get, set, onChange } = {}) {
    const root = el("div", "h3e-trimwrap");

    const bar = el("div", "h3e-trim");
    const wave = el("canvas", "h3e-trim-wave");
    bar.appendChild(wave);
    const maskL = el("div", "h3e-trim-mask l");
    const maskR = el("div", "h3e-trim-mask r");
    const head = el("div", "h3e-trim-head");
    const gripI = el("div", "h3e-trim-grip in");
    const gripO = el("div", "h3e-trim-grip out");
    gripI.title = "Drag: where the window starts.";
    gripO.title = "Drag: where the window ends.";
    bar.append(maskL, maskR, head, gripI, gripO);
    root.appendChild(bar);

    const media = el(kind === "video" ? "video" : "audio", "h3e-trim-media");
    media.controls = true;
    media.preload = "metadata";
    root.appendChild(media);

    const readout = el("div", "h3e-note h3e-trim-readout");
    root.appendChild(readout);

    let secs = 0;            // duration, from the route (see loadedmetadata)
    let a = 0;               // IN, seconds
    let b = 0;               // OUT, seconds -- 0 means "to the end"
    let loaded = "";         // which file the element currently holds
    let raf = 0;
    let dragging = null;

    const outOf = () => (b > 0 ? Math.min(b, secs) : secs);

    function pct(t) {
        return secs > 0 ? Math.max(0, Math.min(100, (t / secs) * 100)) : 0;
    }

    /** Move the overlay only. Called on every pointermove, so it touches no DOM
     *  structure -- rebuilding mid-drag would destroy the captured element and
     *  the drag would die halfway. */
    function paintGrips() {
        const pa = pct(a);
        const pb = pct(outOf());
        maskL.style.width = `${pa}%`;
        maskR.style.width = `${100 - pb}%`;
        gripI.style.left = `${pa}%`;
        gripO.style.left = `${pb}%`;
        const span = Math.max(0, outOf() - a);
        readout.textContent = secs > 0
            ? `in ${fmt(a)}   out ${b > 0 ? fmt(b) : "end"}   (${fmt(span)} of ${fmt(secs)})`
            : "";
    }

    let lastPeaks = null;
    let pending = 0;

    function paintWave(peaks) {
        lastPeaks = peaks === undefined ? lastPeaks : peaks;
        const w = bar.clientWidth;
        const h = bar.clientHeight;
        // The bar can have no box yet: peaks served from the module cache
        // resolve in a microtask, which on a second node using the same file
        // beats first layout. Painting into a zero-width canvas silently draws
        // nothing and never retries, so the waveform would just be missing.
        if (!w || !h) {
            if (!pending) pending = requestAnimationFrame(() => {
                pending = 0;
                paintWave(undefined);
            });
            return;
        }
        peaks = lastPeaks;
        const dpr = window.devicePixelRatio || 1;
        wave.width = Math.round(w * dpr);
        wave.height = Math.round(h * dpr);
        wave.style.width = `${w}px`;
        wave.style.height = `${h}px`;
        const g = wave.getContext("2d");
        if (!g) return;
        g.setTransform(1, 0, 0, 1, 0, 0);
        g.scale(dpr, dpr);
        g.clearRect(0, 0, w, h);
        if (!peaks || !peaks.length) return;
        const mid = h / 2;
        const step = w / peaks.length;
        g.fillStyle = getComputedStyle(bar).getPropertyValue("--h3-wave")
            || "rgba(150,190,255,0.75)";
        for (let i = 0; i < peaks.length; i += 1) {
            // A floor of 1 px so silence still reads as a file rather than as a
            // control that failed to load.
            const v = Math.max(1, peaks[i] * (h - 2));
            g.fillRect(i * step, mid - v / 2, Math.max(1, step - 0.5), v);
        }
    }

    function seek(clientX) {
        const r = bar.getBoundingClientRect();
        if (!r.width || secs <= 0) return 0;
        return Math.max(0, Math.min(secs, ((clientX - r.left) / r.width) * secs));
    }

    function onDown(which) {
        return (e) => {
            if (secs <= 0) return;
            e.preventDefault();
            e.stopPropagation();
            dragging = which;
            e.target.setPointerCapture?.(e.pointerId);
        };
    }

    function onMove(e) {
        if (!dragging) return;
        const t = seek(e.clientX);
        if (dragging === "in") a = Math.min(t, outOf() - MIN_WINDOW_S);
        else b = Math.max(t, a + MIN_WINDOW_S);
        a = Math.max(0, a);
        paintGrips();
    }

    function onUp(e) {
        if (!dragging) return;
        const was = dragging;
        dragging = null;
        e.target.releasePointerCapture?.(e.pointerId);
        // Snap OUT back to the sentinel when it is at the end, so replacing the
        // file with a longer one still plays to ITS end rather than being
        // silently truncated at the old file's length.
        if (was === "out" && b >= secs - 0.05) b = 0;
        set(Math.round(a * 100) / 100, b > 0 ? Math.round(b * 100) / 100 : 0);
        paintGrips();
        onChange?.();
    }

    gripI.addEventListener("pointerdown", onDown("in"));
    gripO.addEventListener("pointerdown", onDown("out"));
    for (const g of [gripI, gripO]) {
        g.addEventListener("pointermove", onMove);
        g.addEventListener("pointerup", onUp);
        g.addEventListener("pointercancel", onUp);
    }

    // Clicking the bar scrubs, so you can find the downbeat before deciding
    // where to put a grip.
    bar.addEventListener("pointerdown", (e) => {
        if (dragging || e.target !== bar && e.target !== wave) return;
        const t = seek(e.clientX);
        try { media.currentTime = t; } catch { /* not seekable yet */ }
        head.style.left = `${pct(t)}%`;
    });

    function tick() {
        if (secs > 0 && !media.paused) head.style.left = `${pct(media.currentTime)}%`;
        raf = requestAnimationFrame(tick);
    }

    function render() {
        const file = String(name?.() || "");
        const win = get?.() || {};
        a = Number(win.start) || 0;
        b = Number(win.end) || 0;

        root.style.display = file ? "" : "none";
        if (!file) {
            if (loaded) { media.pause(); media.removeAttribute("src"); media.load(); }
            loaded = "";
            return;
        }
        if (file !== loaded) {
            loaded = file;
            media.src = viewUrl(file);
            secs = 0;
            paintWave(null);
            peaksFor(file).then((got) => {
                if (loaded !== file) return;      // switched away mid-flight
                // Duration from the route, not from media.duration: MP3
                // Xing/LAME headers routinely report double, and a duration
                // that lies makes every position on this bar lie with it.
                if (got?.seconds > 0) secs = got.seconds;
                paintWave(got?.peaks);
                paintGrips();
            });
        }
        paintGrips();
    }

    // A video with no audio track has no peaks, so the element's own metadata
    // is the only duration there is. Never let it overwrite a decoded one.
    media.addEventListener("loadedmetadata", () => {
        if (secs <= 0 && Number.isFinite(media.duration)) {
            secs = media.duration;
            paintGrips();
        }
    });
    media.addEventListener("timeupdate", () => {
        // Stop at OUT so pressing play auditions the WINDOW, which is the only
        // thing you are actually choosing here.
        if (secs > 0 && media.currentTime >= outOf() - 0.02) {
            media.pause();
            try { media.currentTime = a; } catch { /* ignore */ }
        }
    });
    media.addEventListener("play", () => {
        if (secs > 0 && (media.currentTime < a || media.currentTime >= outOf())) {
            try { media.currentTime = a; } catch { /* ignore */ }
        }
    });

    raf = requestAnimationFrame(tick);
    render();

    function destroy() {
        cancelAnimationFrame(raf);
        if (pending) cancelAnimationFrame(pending);
        for (const t of _waiters) clearTimeout(t);
        _waiters.clear();
        media.pause();
        media.removeAttribute("src");
        // Without load() the browser keeps the connection open and the decoder
        // alive for a file nothing is showing any more.
        media.load();
        media.remove();
    }

    return { root, render, destroy, repaint: () => paintWave(undefined) };
}
