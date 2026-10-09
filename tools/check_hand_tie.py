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
plain = HT.compile_previs([
    {"beat": "@cook chops herbs. She turns to the window."},
    {"beat": "@cook looks out of the window and (S1) says, <d>[English] I keep thinking the snow will "
             "stop by noon.</d> She steps to the sink."},
    {"beat": "@cook sips, and (S1) says, <d>[English] Right.</d> The low hum of the refrigerator."}],
    [192] * 3)[0]
ck("a plain beat's spoken line never reaches the previs (no dangling '(S1) says,')",
   "(S1)" not in plain and "<d>" not in plain and "says" not in plain, plain)
ck("plain beats: first sentence per window, and the last beat's last sentence that names a person",
   plain == ("At 00:00.000 @cook chops herbs. At 00:02.833 @cook looks out of the window. "
             "At 00:05.667 @cook sips. At 00:08.500 @cook sips."), plain)
ending = HT.compile_previs([
    {"beat": "@dex walks under the awning."},
    {"beat": "@dex steps out into the rain. He walks toward the camera down the street, and (S1) says, "
             "<d>[English] Worth it.</d> Steady rain."}], [192, 192])[0]
ck("a closing sentence that opens on a pronoun is the ending, with the beat's person in its place",
   ending.endswith("At 00:05.667 @dex walks toward the camera down the street."), ending)
ck("without moments: each beat's first sentence, and the last beat's last sentence",
   no_moment == "At 00:00.000 First thing. At 00:02.833 Third. At 00:05.667 Fourth.", no_moment)

print("\nthe crossing-line lint (the lab's join-1 repeat)")
rep = [dict(plan[0]), dict(plan[1])]
rep[1]["beat"] = "(S1) finishes, <d>[English] the snow will stop by noon.</d> She leans on the sill."
ck("a beat repeating its head window's line is flagged", len(HT.crossing_lints(rep)) == 1, HT.crossing_lints(rep))
ck("the plan that writes the line once is clean", HT.crossing_lints(plan) == [])

print("\nthe crossing line, split between the two hops that share its window")
split = HT.split_crossing(plan)
# chain_00140 (2026-10-09): by the end of window B the previs had said "I keep thinking
# the snow will stop" (7 words, ~3.5 a second); the film cuts at the window's END, so hop
# 2 gets only what follows. Starting hop 2 at the window's start said "will stop" twice.
ck("hop 1 says the line through the end of the shared window, cut by core's <|cutoff|>",
   split[0]["beat"].endswith("<d>[English] I keep thinking the snow will stop<|cutoff|></d>"),
   split[0]["beat"][-90:])
ck("hop 2 says only what follows the window: no word twice across the cut",
   split[1]["beat"].startswith("<Subject 1> (S1) finishes the same sentence, <d>[English] by noon.</d>"),
   split[1]["beat"][:110])
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

print("\nseam bend (chain_00144: the flash after each join)")
torch.manual_seed(1)
H, W = 24, 32
bg = torch.rand(1, H, W, 3) * 0.5 + 0.2
master = bg.repeat(60, 1, 1, 1).clone()                         # the outgoing hop, frames 10..49
nxt = (bg + 4.0 / 255).repeat(40, 1, 1, 1).clone()             # the next hop sits 4/255 brighter
nxt[:, :8, :8] = torch.rand(40, 8, 8, 3)                       # and something moves in one corner
st, share = HT.seam_step(master[48:50], nxt, 0)
ck("the step is measured on still pixels, ignoring the moving corner",
   abs(float(st.mean()) * 255 + 4.0) < 0.05 and share > 0.8, "%s, %.0f%% still" % ((st * 255).tolist(), share * 100))
before = master.clone()
st2 = HT.seam_bend(master, 10, 50, nxt)
ck("after the bend the cut has no level step", abs(float((nxt[0] - master[49])[8:, 8:].mean()) * 255) < 0.15,
   "%.2f/255" % (float((nxt[0] - master[49])[8:, 8:].mean()) * 255))
ck("the bend starts at nothing on the outgoing hop's first frame", torch.equal(master[10], before[10]))
ck("it never steps more than step/length per frame",
   float((master[11:50] - master[10:49]).abs().max()) * 255 < 4.0 / 39 + 0.01)
ck("frames before the hop and the next hop are untouched",
   torch.equal(master[:10], before[:10]) and torch.equal(master[50:], before[50:]))
busy = torch.rand(40, H, W, 3)
ck("a seam with no still pixels is left alone", HT.seam_bend(before.clone(), 10, 50, busy) is None)

print("\ndeflicker (the frame-17 dip every guided head has)")
film = bg.repeat(80, 1, 1, 1).clone()
film[:, :8, :8] = torch.rand(80, 8, 8, 3)                      # motion in one corner
film[40:] += 6.0 / 255                                         # a real level change that lasts
film[17] -= 3.0 / 255                                          # a one-frame blink
film[60:62] += 2.0 / 255                                       # a two-frame blink
ref = film.clone()
ref[17] += 3.0 / 255
ref[60:62] -= 2.0 / 255
fix = HT.deflicker(film)
ck("one- and two-frame blinks are removed", float((film - ref)[:, 8:, 8:].abs().max()) * 255 < 0.3,
   "%.2f/255" % (float((film - ref)[:, 8:, 8:].abs().max()) * 255))
ck("a level change that lasts is kept", abs(float((film[45] - film[35])[8:, 8:].mean()) * 255 - 6.0) < 0.3)
ck("the largest correction is reported", abs(fix * 255 - 3.0) < 0.3, "%.2f" % (fix * 255))

print("\ndetail settle (chain_00144: detail swelled +12% after join 1)")
torch.manual_seed(2)
H2, W2 = 32, 48
tex = torch.rand(1, H2, W2, 3)
smooth = HT._detail_split(tex[0])[0][None]
film = smooth.repeat(80, 1, 1, 1) + (tex - smooth).repeat(80, 1, 1, 1)
swell = torch.ones(80)
swell[40:70] = 1.0 + 0.15 * torch.sin(torch.linspace(0, 3.14159, 30))   # the swell after a cut at 40
film = smooth + swell.view(-1, 1, 1, 1) * (tex - smooth)
before = film.clone()
g = HT.detail_settle(film, 40)
e = [HT._detail_split(film[t])[1] for t in range(80)]
ck("the swell is flattened to within 2% of the level", max(e[40:70]) / e[30] < 1.02,
   "%.3f" % (max(e[40:70]) / e[30]))
ck("it only softens, never sharpens", min(g) >= HT.DETAIL_FLOOR and max(g) <= 1.0, (min(g), max(g)))
ck("frames outside the span are untouched",
   torch.equal(film[:40], before[:40]) and torch.equal(film[70:], before[70:]))
ck("a cut too close to the start is left alone", HT.detail_settle(before.clone(), 5) is None)

print("\nthe cut sits at the window START (the previous hop's tail span is dropped)")
# raw hops of chain_00142: the tail span, guided at -22, stepped -11/255; the head,
# guided at 0, -3 at worst. run() rewinds the master by the overlap and writes each
# later hop whole; the outgoing hop is bent to meet it, and is toned on its head only.
ck("run() rewinds the master and writes a hand-tie hop whole",
   "write_pos -= overlap_n" in src and "master_imgs[write_pos:write_pos + keep_n] = imgs" in src)
ck("run() drops the previous hop's tail audio, not this hop's head",
   "master_wav[..., :-dropped]" in src)
ck("run() tones a hop on its head window only (its tail span is not delivered)",
   "_ht.tone_match(imgs, _wp[_k], None)" in src)
ck("run() bends the outgoing hop to the incoming one, then deflickers each section",
   "_ht.seam_bend(master_imgs, ht_hop_start, write_pos, imgs)" in src
   and "_ht.deflicker(master_imgs[a:b])" in src)
ck("run() settles detail at each tied cut, never at a restart",
   "ht_cuts.append(int(write_pos))" in src and "_ht.detail_settle(master_imgs, _c)" in src
   and src.index("ht_cuts.append") > src.index("elif ht:"))
_pos, _cuts = 0, []
for _k in range(4):
    if _k:
        _pos -= HT.WINDOW
        _cuts.append(_pos)
    _pos += 192
ck("4 x 8 s: cuts at 170/340/510, the film still 702 frames",
   _cuts == [170, 340, 510] and _pos == HT.film_frames([192] * 4), (_cuts, _pos))

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
ck("imports only stdlib + torch", mods <= {"re", "torch", "torch.nn.functional"}, sorted(m for m in mods if m))

print("\nall ok" if not FAILS else "\n%d FAILED: %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
