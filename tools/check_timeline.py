r"""Offline checks for the retake timeline -- no server, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_timeline.py

What has to hold:

  * A cached hop records the predecessor it was generated against
    (`prev_key` in its meta), and the store hands that back without decoding
    frames. That is what lets `run()` say, exactly, when a locked hop is
    served next to a hop it was never generated with -- the join the pack
    otherwise has no way to warn about.
  * The timeline's pictures and playback (`strip.py`) are built from the
    cache alone: a scrub sheet of evenly spaced frames that starts and ends
    on the join frames, and a join clip trimmed exactly the way the master
    is -- the previous hop's tail, then the next hop past its overlap.
  * The forecast (`js/editor/timeline_model.js`) follows the key's shape:
    a change runs on, stops before a restart, and stops at a locked hop.
    Its rules run under Node when Node is on PATH, and are skipped, loudly,
    when it is not.
  * Each hop's run record reaches the timeline on the chain's own preview
    event, with the full key; the routes accept hex keys and nothing else.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import importlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def pack(mod):
    pkg = types.ModuleType("htc_tl")
    pkg.__path__ = [HERE]
    sys.modules.setdefault("htc_tl", pkg)
    return importlib.import_module("htc_tl." + mod)


def test_prev_key_meta():
    import torch
    store = pack("store")
    with tempfile.TemporaryDirectory() as root:
        st = store.HopStore(root)
        imgs = torch.rand(5, 32, 32, 3)
        wav = torch.zeros(1, 2, 1600)
        st.put("k2", imgs, wav, 16000, {"hop": 2, "prev_key": "k1"})
        st.put("k3", imgs, wav, 16000, {"hop": 3, "prev_key": None})
        ck("meta hands back the predecessor", st.meta("k2").get("prev_key") == "k1")
        ck("a restart's predecessor is recorded as None",
           "prev_key" in st.meta("k3") and st.meta("k3")["prev_key"] is None)
        ck("a missing entry has no meta", st.meta("nope") is None)


def test_run_records_and_compares():
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    ck("run() stores prev_key with every cached hop",
       '"prev_key": None if hop_restart else prev_key' in src)
    ck("the locked branch compares it against this run's predecessor",
       '_pm["prev_key"] != prev_key' in src and "unpaired_joins.append(i + 1)" in src)
    ck("info names the unpaired joins", '"unpaired joins: "' in src)


def test_strip():
    import av
    import math
    import torch
    strip = pack("strip")
    frames = torch.zeros(100, 72, 128, 3)
    for i in range(100):
        frames[i] = i / 99.0          # brightness encodes the frame index
    jpg, cells = strip.sprite(frames, n=10, height=36)
    ck("a sheet is a JPEG of the asked cells", jpg[:2] == bytes([0xFF, 0xD8]) and cells == 10)
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(jpg)).convert("L")
    w, h = img.size
    ck("cells are even-sized and scaled down", h == 36 and w == 10 * 64, f"{w}x{h}")
    first = img.getpixel((32, 18))
    last = img.getpixel((w - 32, 18))
    ck("the sheet opens on frame 0 and ends on the last frame",
       first < 10 and last > 245, f"{first} {last}")

    sr = 24000
    per = sr // 24
    prev = torch.zeros(48, 8, 8, 3)
    nxt = torch.ones(60, 8, 8, 3)
    pw = torch.zeros(1, 2, 48 * per)
    nw = torch.arange(60 * per, dtype=torch.float32).repeat(2, 1)[None]
    ia, ib = strip.seam_indices(48, 60, 22, seconds=1.0)
    ck("a join shows the previous hop's tail and the next past its trim",
       ia == list(range(24, 48)) and ib == list(range(22, 46)), f"{ia[:2]} {ib[:2]}")
    imgs, wav = strip.seam(prev[ia], pw, nxt[ib], nw, sr, 22)
    ck("its frames are those windows, in order",
       imgs.shape == (48, 8, 8, 3) and imgs[:24].max() == 0 and imgs[24:].min() == 255,
       str(imgs.shape))
    ck("its audio drops the same overlap the master does",
       wav.shape == (2, 48 * per) and int(wav[0, 24 * per]) == 22 * per,
       f"{wav.shape} {wav[0, 24 * per]}")
    ia, ib = strip.seam_indices(48, 60, 0, seconds=1.0)
    ck("a restart join drops nothing", ib[0] == 0 and len(ib) == 24)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "c.mp4")
        t = torch.arange(sr * 2) / sr
        tone = torch.sin(2 * math.pi * 330 * t)[None, None].repeat(1, 2, 1)
        strip.encode_clip(torch.rand(48, 72, 128, 3), tone, sr, path)
        c = av.open(path)
        kinds = sorted(st.type for st in c.streams)
        secs = float(c.duration or 0) / 1e6
        c.close()
        ck("a clip is H.264 + AAC, the length of its frames",
           kinds == ["audio", "video"] and abs(secs - 2.0) < 0.15, f"{kinds} {secs:.2f}s")
        ck("no half-written file is left behind", not os.path.exists(path + ".part"))


def test_routes_path():
    """The routes' build path, end to end, on a real store: small frames only."""
    import av
    import torch
    store = pack("store")
    strip = pack("strip")
    with tempfile.TemporaryDirectory() as root:
        hs = store.HopStore(root)
        sr = 24000
        a = torch.zeros(60, 144, 256, 3)
        b = torch.ones(60, 144, 256, 3)
        hs.put("aa11", a, torch.zeros(1, 2, 60 * 1000), sr, {"hop": 1})
        hs.put("bb22", b, torch.zeros(1, 2, 60 * 1000), sr, {"hop": 2, "prev_key": "aa11"})
        few = hs.frames("aa11", 36, [0, 30, 59])
        ck("frames() decodes only what is asked, scaled, as uint8",
           few.shape == (3, 36, 64, 3) and few.dtype.name == "uint8", str(few.shape))
        ck("frames() of a missing key is None", hs.frames("nope", 36) is None)
        wav, got_sr = hs.audio("bb22")
        ck("audio() hands back the stored waveform and rate",
           got_sr == sr and wav.shape[-1] == 60 * 1000)
        jpg, cells = strip.sprite(hs.frames("aa11", 96, list(range(0, 60, 5))), 12)
        ck("a sheet from store frames", cells == 12 and len(jpg) > 100)
        ia, ib = strip.seam_indices(60, 60, 22, fps=hs.fps)
        imgs, sw = strip.seam(hs.frames("aa11", 360, ia), hs.audio("aa11")[0],
                              hs.frames("bb22", 360, ib), hs.audio("bb22")[0], sr, 22)
        path = os.path.join(root, "seam.mp4")
        strip.encode_clip(imgs, sw, sr, path, fps=hs.fps)
        c = av.open(path)
        secs = float(c.duration or 0) / 1e6
        c.close()
        want = (len(ia) + len(ib)) / 24.0
        ck("a join clip plays both windows", abs(secs - want) < 0.15, f"{secs:.2f}s vs {want:.2f}s")


def test_forecast_under_node():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        print("  skip forecast rules: Node is not on PATH (tools/timeline_model_test.mjs)")
        return
    got = subprocess.run([node, os.path.join(HERE, "tools", "timeline_model_test.mjs")],
                         capture_output=True, text=True, timeout=60)
    for line in got.stdout.splitlines():
        print("  " + line.strip() if line.strip() else "")
    ck("the forecast rules pass under Node", got.returncode == 0, got.stderr[-300:])


def test_wiring():
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    ck("each hop's preview event carries its full key and status",
       '"timeline": {' in src and '"key": hop_key,' in src and '"locked" if locked_served' in src)
    routes = open(os.path.join(HERE, "routes.py"), encoding="utf-8").read()
    ck("the timeline routes accept hex keys only",
       'any(c not in "0123456789abcdef" for c in key)' in routes)
    ck("they decode in an executor, never on the event loop",
       "run_in_executor(None, work)" in routes and "await _run(" in routes)
    ui = open(os.path.join(HERE, "js", "h3_ref_chain_ui.js"), encoding="utf-8").read()
    ck("the tab sits after Refs", ui.index('id: "refs"') < ui.index('id: "timeline"') < ui.index('id: "media"'))
    tl = open(os.path.join(HERE, "js", "editor", "timeline.js"), encoding="utf-8").read()
    ck("solo and lock go through the plan editor's own functions",
       "editor.toggleSolo(" in tl and "editor.toggleLock(" in tl)
    ck("geometry comes from editor.timing()", "editor.timing()" in tl)
    pe = open(os.path.join(HERE, "js", "editor", "plan_editor.js"), encoding="utf-8").read()
    ck("extend appends through the editor, with a free id",
       "function appendShot()" in pe and "shot.id = freeId();" in pe
       and "editor.appendShot()" in tl)
    ck("a block opens the SCRIPT card itself, not a copy",
       "function cardFor(index)" in pe and "editor.cardFor(idx)" in tl)
    ck("a shot is removed through the editor, the card's × included",
       "function removeShot(index)" in pe and "() => removeShot(index)" in pe
       and "editor.removeShot(idx)" in tl)
    ck("removing a rendered shot takes two clicks", "h3e-tl-armed" in tl)
    ck("the inspector is not rebuilt under the caret",
       "inspector.contains(document.activeElement)" in tl)
    ck("pin_only's skipped last-hop refine is in that hop's key",
       'hop_payload["refine"].append("skipped:last")' in src
       and 'str(hop_refine) == "pin_only" and i >= n - 1' in src)


def main():
    print("check_timeline")
    test_prev_key_meta()
    test_run_records_and_compares()
    test_strip()
    test_routes_path()
    test_forecast_under_node()
    test_wiring()
    if FAIL:
        print(f"{len(FAIL)} check(s) failed")
        sys.exit(1)
    print("all timeline checks passed")


if __name__ == "__main__":
    main()
