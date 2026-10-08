r"""Offline checks for hand_tie.py -- no GPU, no H3 weights, no server.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_hand_tie.py

Hand-tie ties every hop to one previs: windows cut from it guide each hop at both
ends, and no hop reads another. The geometry has to reproduce what the HTC-Lab
runs measured (lab name "bridge clips", 2026-10-07/08), so the numbers below are
the lab's, not new choices:

* 4 hops of 8 s -> windows 0/68/136/204/272, a 294-frame previs, a 702-frame film;
* every window on H3's 17-frame grid, and the hop's tail guide (-22) on it too;
* 5 hops of 8 s fill the 362-frame budget exactly; 6 are refused with a fix;
* window B's sound is 2.833 s + 0.917 s of the previs track;
* the sound test's previs text: moments at the window times, the crossing line led in;
* the tone ramp removes a ramped bias (the lab's HTCLabBridgeTone numbers);
* settings refused: overlap other than 22, last_frame_guide on, an oversize section;
* the key carries the windows, not prev_key;
* no ComfyUI import in the module.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys

import torch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("hand_tie", os.path.join(HERE, "hand_tie.py"))
HT = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HT)

FAILS = []


def ck(label, ok, detail=""):
    print("  %s %s%s" % ("ok  " if ok else "FAIL", label, (" -- " + str(detail)) if detail != "" else ""))
    if not ok:
        FAILS.append(label)


# DURATION_FRAMES from the node, read as text so this stays importable without ComfyUI
src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
_a = src.index("DURATION_FRAMES = {")
DURATION_FRAMES = ast.literal_eval(src[_a + len("DURATION_FRAMES = "):src.index("}", _a) + 1])
OVERLAP_FRAMES = ast.literal_eval(src[src.index("OVERLAP_FRAMES = {") + len("OVERLAP_FRAMES = "):
                                      src.index("}", src.index("OVERLAP_FRAMES = {")) + 1])

print("geometry (the lab's numbers)")
lay = HT.layout([192] * 4)
ck("4 x 8 s: windows 0/68/136/204/272", lay["windows"] == [0, 68, 136, 204, 272], lay["windows"])
ck("4 x 8 s: a 294-frame previs (12.25 s)", lay["previs"] == 294, lay["previs"])
ck("4 x 8 s: a 702-frame film", HT.film_frames([192] * 4) == 702, HT.film_frames([192] * 4))
ck("the 22-frame window is the node's 0.9 s overlap", OVERLAP_FRAMES["0.9 s"] == HT.WINDOW)
ck("5 x 8 s fill the budget exactly", HT.layout([192] * 5)["previs"] == HT.PREVIS_MAX == 362)
ck("max_hops() at 8 s is 5", HT.max_hops(192) == 5, HT.max_hops(192))

print("\nthe 17-frame grid, for every hop length the node offers up to 15 s")
lengths = sorted({v for k, v in DURATION_FRAMES.items() if 124 <= v <= 362})
ck("every spacing is a multiple of 17 and at least 34",
   all(HT.spacing(f) % 17 == 0 and HT.spacing(f) >= 34 for f in lengths),
   {f: HT.spacing(f) for f in lengths})
ck("8 s -> 68, 5 s -> 51, 10 s -> 85 (the design doc's rule)",
   (HT.spacing(192), HT.spacing(124), HT.spacing(243)) == (68, 51, 85))
ck("every hop length is 17q+5, so the tail guide at -22 starts on 17q",
   all(f % 17 == 5 and (f - HT.WINDOW) % 17 == 0 for f in lengths))
mixed = HT.layout([124, 192, 243, 175])
ck("mixed lengths: windows stay on the grid and never overlap",
   all(w % 17 == 0 for w in mixed["windows"])
   and all(b - a >= HT.WINDOW for a, b in zip(mixed["windows"], mixed["windows"][1:])), mixed)

print("\nsections")
shots = [{"beat": "a"}, {"beat": "b"}, {"beat": "c", "anchor": "restart"}, {"beat": "d"}, {"beat": "e"}]
ck("hop 1 and every restart open a section", HT.sections(shots) == [[0, 1], [2, 3, 4]], HT.sections(shots))
ck("one shot is one section", HT.sections([{"beat": "x"}]) == [[0]])

print("\nwindow sound")
a, d = HT.window_audio(68)
ck("window B: 2.833 s + 0.917 s of the previs track", (round(a, 4), round(d, 4)) == (2.8333, 0.9167), (a, d))

print("\nthe previs text (the lab's sound-test scene)")
plan = [
    {"beat": "She chops herbs at the counter. She sets the knife down.",
     "moment": "<Subject 1> chops herbs at the counter."},
    {"beat": "She leans on the sill and the tap runs.",
     "moment": "<Subject 1> turns to the window and (S1) says, <d>[English] I keep thinking the snow will stop by noon.</d>"},
    {"beat": "She turns off the tap and sips.", "moment": "<Subject 1> leans on the sill, the tap running."},
    {"beat": "She sips in the quiet.", "moment": "<Subject 1> lifts the blue mug and sips."},
]
plan[-1]["moment_end"] = "<Subject 1> chops again and (S1) says, <d>[English] Right. Soup won't make itself.</d>"
text, lay = HT.compile_previs(plan, [192] * 4)
ck("one timed line per window, in order", text.count("At ") == 5, text)
ck("plain moments sit on their window: 00:00.000, 00:05.667, 00:08.500",
   all(s in text for s in ("At 00:00.000 <Subject 1> chops", "At 00:05.667 <Subject 1> leans",
                           "At 00:08.500 <Subject 1> lifts")))
ck("a speaking moment is led in so the window lands mid-line (window B 2.833 -> 1.833)",
   "At 00:01.833 <Subject 1> turns to the window" in text, text)
ck("the closing line is placed to end with the previs (11.333 + 0.917 - 2.0 = 10.250)",
   "At 00:10.250 <Subject 1> chops again" in text, text[-160:])
no_moment = HT.compile_previs([{"beat": "First thing. Second thing."}, {"beat": "Third. Fourth."}], [192, 192])[0]
ck("without moments: each beat's first sentence, and the last beat's last sentence",
   no_moment == "At 00:00.000 First thing. At 00:02.833 Third. At 00:05.667 Fourth.", no_moment)

print("\nthe crossing-line lint (the lab's join-1 repeat)")
rep = [dict(plan[0]), dict(plan[1])]
rep[1]["beat"] = "(S1) finishes, <d>[English] the snow will stop by noon.</d> She leans on the sill."
ck("a beat repeating its head window's line is flagged", len(HT.crossing_lints(rep)) == 1, HT.crossing_lints(rep))
ck("the plan that writes the line once is clean", HT.crossing_lints(plan) == [])

print("\nthe crossing line, split between the two hops that share its window")
split = HT.split_crossing(plan)
ck("hop 1 ends on the line's opening up to the window, cut by core's <|cutoff|> token",
   split[0]["beat"].endswith("<d>[English] I keep thinking the<|cutoff|></d>"), split[0]["beat"][-90:])
ck("hop 2 picks the same sentence up from the start of the window",
   split[1]["beat"].startswith("<Subject 1> (S1) continues the same sentence, <d>[English] thinking the snow "
                               "will stop by noon.</d>"), split[1]["beat"][:110])
ck("the last hop gets the moment_end line whole",
   split[3]["beat"].endswith("<d>[English] Right. Soup won't make itself.</d>"), split[3]["beat"][-80:])
ck("the input shots are not modified", "<d>" not in plan[0]["beat"] and "continues" not in plan[1]["beat"])
ck("a moment without a line changes nothing",
   HT.split_crossing([{"beat": "a"}, {"beat": "b", "moment": "she turns."}])[0]["beat"] == "a")
ck("<|cutoff|> is a special token in core's H3 tokenizer",
   '"<|cutoff|>"' in open(os.path.join(os.path.dirname(os.path.dirname(HERE)), "comfy", "text_encoders",
                                       "minimax.py"), encoding="utf-8").read())

import shutil, subprocess  # noqa: E401,E402
node = shutil.which("node")
if node:
    r = subprocess.run([node, os.path.join(HERE, "tools", "check_hand_tie_plan.mjs")],
                       capture_output=True, text=True)
    _out = (r.stdout + r.stderr).strip().splitlines()
    ck("the editor round-trips moment / moment_end (tools/check_hand_tie_plan.mjs)", r.returncode == 0,
       _out[-1] if _out else "")
else:
    print("  skip the editor round-trip: Node is not on PATH")

print("\ntone match")
torch.manual_seed(0)
N, F = 192, 22
content = torch.rand(N, 8, 12, 3) * 0.6 + 0.2
hb, tb = torch.tensor([3.0, 2.0, 1.0]) / 255, torch.tensor([-2.0, -2.5, -1.0]) / 255
hop = content + HT.tone_offsets(N, hb, tb, F).view(N, 1, 1, 3)
out, (ho, to) = HT.tone_match(hop, content[:F], content[-F:], F)
ck("a 3/255 ramped bias is removed to under 0.15/255 (the lab node's figure)",
   float((out - content).abs().max()) * 255 < 0.15, "%.3f/255" % (float((out - content).abs().max()) * 255))
ck("offsets come back with the right sign", float(ho[0]) < 0 and float(to[0]) > 0, (ho * 255, to * 255))
ck("the input is not modified", torch.allclose(hop, content + HT.tone_offsets(N, hb, tb, F).view(N, 1, 1, 3)))
try:
    HT.tone_match(hop[:30], content[:F], content[-F:], F)
    ck("a hop too short for two spans is refused", False, "accepted")
except ValueError as e:
    ck("a hop too short for two spans is refused", True, str(e)[:50])

print("\nrefusals")
ck("overlap 22 + last_frame_guide off is fine", HT.settings_errors(22, "off") == [])
ck("overlap 5 is refused", len(HT.settings_errors(5)) == 1)
ck("last_frame_guide is refused", len(HT.settings_errors(22, "before_restart")) == 1)
big = [{"beat": "x"} for _ in range(6)]
e = HT.settings_errors(22, "off", big, [192] * 6)
ck("6 hops of 8 s in one section are refused, naming the fix", len(e) == 1 and "anchor=restart" in e[0], e)
big[3]["anchor"] = "restart"
ck("the same 6 hops split by a restart are fine", HT.settings_errors(22, "off", big, [192] * 6) == [])

print("\ncache key")
k = HT.hop_key_fields("aa", "bb")
ck("the key names its two windows and no prev_key", k == {"pin": "hand_tie", "head": "aa", "tail": "bb"}, k)
ck("window sound digests join only when sound is pinned",
   set(HT.hop_key_fields("aa", "bb", "ca", "cb")) == {"pin", "head", "tail", "head_audio", "tail_audio"})

print("\nthe module stays ComfyUI-free")
tree = ast.parse(open(os.path.join(HERE, "hand_tie.py"), encoding="utf-8").read())
mods = {n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])}
ck("imports only stdlib + torch", mods <= {"re", "torch"}, sorted(m for m in mods if m))

print("\nall ok" if not FAILS else "\n%d FAILED: %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
