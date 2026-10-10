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
# One window per hop, plus a closing window for the last hop when it fits
# (chain_00155/00156: the start-only last hop snapped at f22 on every take).
ck("4 x 8 s: one window per hop and a closing one, 0/68/136/204/272",
   lay["windows"] == [0, 68, 136, 204, 272] and lay["closing"], lay["windows"])
ck("4 x 8 s: a 294-frame previs (12.25 s)", lay["previs"] == 294, lay["previs"])
_l5, _l6 = HT.layout([192] * 5), HT.layout([192] * 6)
ck("5 x 8 s still close (362 = PREVIS_MAX); a full 6 x 8 s section's last hop is start-only",
   _l5["closing"] and _l5["previs"] == 362 and not _l6["closing"] and len(_l6["windows"]) == 6,
   (_l5, _l6))
ck("the closing window never splits a section: 6 x 8 s is still one",
   HT.sections([{"beat": "x"}] * 6, [192] * 6) == [[0, 1, 2, 3, 4, 5]]
   and HT.sections([{"beat": "x"}] * 7, [192] * 7) == [[0, 1, 2, 3, 4, 5], [6]])
ck("the closing window has no moment: the previs text keeps one stamp per hop",
   HT.compile_previs([{"beat": "@a walks."}, {"beat": "@a sits."}], [192, 192])[0].count("At ") == 2)
ck("run() ties a hop's end wherever a next window exists (the closing one included)",
   "[(-_ht.WINDOW, _k + 1)] if _k + 1 < len(_wp)" in src)
ck("4 x 8 s: a 702-frame film", HT.film_frames([192] * 4) == 702, HT.film_frames([192] * 4))
ck("the 22-frame window is the node's 0.9 s overlap", OVERLAP_FRAMES["0.9 s"] == HT.WINDOW)
ck("6 x 8 s fill the budget exactly", HT.layout([192] * 6)["previs"] == HT.PREVIS_MAX == 362)
ck("max_hops() at 8 s is 6", HT.max_hops(192) == 6, HT.max_hops(192))
ck("every section previs is 17q+5, a length H3 renders",
   all(HT.layout([192] * k)["previs"] % 17 == 5 for k in range(1, 7)))

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
twelve = [{"beat": "x"} for _ in range(12)]
ck("12 x 8 s split into two previs of 6, the second CHAINED (no cut)",
   HT.sections(twelve, [192] * 12) == [list(range(6)), list(range(6, 12))]
   and [HT.chained(twelve, s) for s in HT.sections(twelve, [192] * 12)] == [False, True],
   HT.sections(twelve, [192] * 12))
ext = [{"beat": "x"} for _ in range(4)] + [{"beat": "y", "anchor": "extend"}]
ck("anchor=extend opens a chained section, leaving the rendered one as it was",
   HT.sections(ext, [192] * 5) == [[0, 1, 2, 3], [4]]
   and [HT.chained(ext, s) for s in HT.sections(ext, [192] * 5)] == [False, True])
ck("a restart is never chained", not HT.chained(shots, [2, 3, 4]))

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
ck("one timed line per window, in order", text.count("At ") == 4, text)
ck("plain moments sit on their window: 00:00.000, 00:05.667, 00:08.500",
   all(s in text for s in ("At 00:00.000 <Subject 1> chops", "At 00:05.667 <Subject 1> leans",
                           "At 00:08.500 <Subject 1> lifts")))
ck("a speaking moment is led in so the window lands mid-line (window B 2.833 -> 1.833)",
   "At 00:01.833 <Subject 1> turns to the window" in text, text)
ck("there is no closing MOMENT (00150 cut to reach one): moment_end does not reach the previs",
   "chops again" not in text, text[-160:])
no_moment = HT.compile_previs([{"beat": "First thing. Second thing."}, {"beat": "Third. Fourth."}], [192, 192])[0]
plain = HT.compile_previs([
    {"beat": "@cook chops herbs. She turns to the window."},
    {"beat": "@cook looks out of the window and (S1) says, <d>[English] I keep thinking the snow will "
             "stop by noon.</d> She steps to the sink."},
    {"beat": "@cook sips, and (S1) says, <d>[English] Right.</d> The low hum of the refrigerator."}],
    [192] * 3)[0]
ck("a talking cue never reaches the previs (it invents speech + burned-in captions)",
   HT._first_sentence("@mina walks down the street, holding her phone out and talking into it. x")
   == "@mina walks down the street, holding her phone out."
   and HT._first_sentence("@mina slows by a stall, still talking to the phone. x") == "@mina slows by a stall."
   and HT._first_sentence("@mina keeps walking with the phone held out. x")
   == "@mina keeps walking with the phone held out.")
ck("a plain beat's spoken line never reaches the previs (no dangling '(S1) says,')",
   "(S1)" not in plain and "<d>" not in plain and "says" not in plain, plain)
# Changed by PREVIS_SOUND. The old text was
#   "... At 00:05.667 @cook sips. At 00:08.500 @cook sips."
# and the beat that ends "The low hum of the refrigerator." lost it -- which is
# the bug _sound_sentence exists for: a moment is a picture, and a previs whose
# text names only pictures gives its audio branch nothing to do but invent a
# voice (chain_00147: 6 invented words, all in the first 4 s).
ck("plain beats: first sentence per window; the last beat's own closing sound reaches no window",
   plain == ("At 00:00.000 @cook chops herbs. At 00:02.833 @cook looks out of the window. "
             "At 00:05.667 @cook sips."), plain)
ck("_last_sentence still reads a pronoun ending (kept for moment_end's split)",
   HT._last_sentence("@dex steps out. He walks toward the camera.") == "@dex walks toward the camera.",
   HT._last_sentence("@dex steps out. He walks toward the camera."))
ck("without moments: each beat's first sentence, one per hop",
   no_moment == "At 00:00.000 First thing. At 00:02.833 Third.", no_moment)

print("\nthe moment carries its beat's sound, so the audio branch has a target")
# chain_00147's kept previs (seed 219378) invented "Looks to be I'm hard." (5
# words, window 0) and "Yeah." (1 word, window 1) from a previs whose text named
# pictures only. audio_cut hid it: that run's window 3 -- the club bass, mean
# -10.1 dB -- had zero points under AUDIO_QUIET_DB in 88 probes, so on one window
# in six the invention would have reached the film.
snd = HT._sound_sentence("@dex walks down a busy night street. A sizzling grill, crowd chatter, "
                         "footsteps on wet pavement.")
ck("a beat ending on a sound names it", snd == "A sizzling grill, crowd chatter, footsteps on wet pavement.", snd)
ck("a beat ending on an action does not",
   HT._sound_sentence("@dex walks down the street. He turns to the window.") == "",
   HT._sound_sentence("@dex walks down the street. He turns to the window."))
ck("a beat ending on a person does not",
   HT._sound_sentence("@dex walks to the cart. He leans on it.") == "",
   HT._sound_sentence("@dex walks to the cart. He leans on it."))
ck("a one-sentence beat has nothing to add",
   HT._sound_sentence("@dex walks down the street.") == "")
ck("a two-word tail is a clause, not a sound bed",
   HT._sound_sentence("@dex walks down the street. Fourth.") == "",
   HT._sound_sentence("@dex walks down the street. Fourth."))
ck("the sound carries no dialogue of its own",
   not any(x in HT._sound_sentence(b) for b in (
       "@dex walks. (S1) says, <d>[English] hello.</d> The low hum of the refrigerator.",
   ) for x in ("<d>", "(S1)", "says")))
_off = HT.compile_previs([{"beat": "@dex walks. The low hum of the refrigerator."},
                          {"beat": "@dex turns. He walks toward the camera down the street."}],
                         [192, 192], sound=False)[0]
_on = HT.compile_previs([{"beat": "@dex walks. The low hum of the refrigerator."},
                         {"beat": "@dex turns. He walks toward the camera down the street."}],
                        [192, 192])[0]
ck("sound=False restores the exact old text (the A/B lever)",
   _off == "At 00:00.000 @dex walks. At 00:02.833 @dex turns.", _off)
ck("and the default is the sound-carrying one", "The low hum of the refrigerator." in _on, _on)
_eat = HT.moments([{"beat": "@mina slows by a stall. A sizzling grill, the vendor calling out orders."},
                   {"beat": "@mina holds the skewer up. (S1) says, <d>[English] Here goes.</d> She bites. "
                            "The crunch of her chewing, traffic hum, footsteps."}])
ck("a window's sound is the PREVIOUS beat's closing sound (00154: chewing at the bite hop's opening)",
   _eat == ["@mina slows by a stall. A sizzling grill, the vendor calling out orders.",
            "@mina holds the skewer up. A sizzling grill, the vendor calling out orders."], _eat)
_carry = HT.moments([{"beat": "@cook chops. The low hum of the refrigerator."}, {"beat": "@cook turns. She sips."},
                     {"beat": "@cook sits. She reads."}])
_ws = HT.window_speaks(plan, 5)
ck("only a window with a planned line pins the previs's sound (00159: 'Right,' from an invented one)",
   _ws == [False, True, False, False, False], _ws)
ck("a plain beats-only plan pins no sound at all; the closing window never does",
   HT.window_speaks([{"beat": "@a walks. (S1) says, <d>[English] Hi there.</d> Hum."}] * 3, 4)
   == [False] * 4)
ck("run() pins sound only where window_speaks says, picture everywhere",
   "if not ht_speaks[_si][_wk]:" in src and "snds.append(None)" in src)
ck("a beat that names no sound passes the last named one on (a soundless window invents a voice)",
   all(m.endswith("The low hum of the refrigerator.") for m in _carry), _carry)
ck("PREVIS_SOUND is on by default, so a plain plan needs no extra input",
   HT.PREVIS_SOUND is True)
# the movement test that produced the finding: every beat ends on a real sound
_move = [{"beat": "@dex walks down a busy night street of neon shop signs, rolling his shoulders. "
                  "He slows at a street food cart, leans on its counter. "
                  "A sizzling grill, crowd chatter, footsteps on wet pavement."},
         {"beat": "@dex lowers his arms as rain starts to fall, looks up and laughs. "
                  "He jogs under a striped shop awning, shakes the water from his sleeves. "
                  "Rain pattering on the canvas awning, the bass beat muffled behind him."},
         {"beat": "@dex watches the rain from under the awning, grins and steps back out into it. "
                  "He walks toward the camera down the glistening street. "
                  "Steady rain, distant traffic, footsteps splashing."}]
_t = HT.compile_previs(_move, [192] * 3)[0]
ck("each window carries the sound the hop before it ends on (window 0: its own)",
   _t.count("A sizzling grill, crowd chatter, footsteps on wet pavement.") == 2
   and "Rain pattering on the canvas awning, the bass beat muffled behind him." in _t
   and "Steady rain" not in _t, _t)
ck("and the bass line the sound vocabulary misses is still carried",
   "A thumping bass beat fills the street, with cheers from inside the club."
   in HT.compile_previs(
       [{"beat": "@dex hears a bass beat from the doorway. He steps into the light. "
                 "A thumping bass beat fills the street, with cheers from inside the club."}],
       [192])[0],
   HT.compile_previs(
       [{"beat": "@dex hears a bass beat from the doorway. He steps into the light. "
                 "A thumping bass beat fills the street, with cheers from inside the club."}],
       [192])[0])

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

print("\nthe previs checks itself for hard cuts (chain_00146: medium to wide at 13.08 s)")
torch.manual_seed(3)
def _scene(seed):                                                     # a 512p-sized frame (16 x 8 cells)
    g = torch.Generator().manual_seed(seed)
    coarse = torch.rand(1, 3, 6, 4, generator=g)                       # big shapes: a composition
    big = torch.nn.functional.interpolate(coarse, size=(512, 288), mode="bilinear", align_corners=False)
    return (0.8 * big + 0.2 * torch.rand(1, 3, 512, 288, generator=g))[0].permute(1, 2, 0)
scene_a, scene_b = _scene(10), _scene(11)
pan = torch.stack([torch.roll(scene_a, shifts=t, dims=1) for t in range(60)])          # a moving camera
pan += 0.03 * torch.randn_like(pan)                                                   # and rain
ck("a panning, rainy shot is not a cut", HT.previs_cuts(pan) == [], HT.previs_cuts(pan))
cut = pan.clone()
cut[37:] = torch.stack([torch.roll(scene_b, shifts=t, dims=1) for t in range(23)])
ck("a hard cut to another shot is found at its first new frame", HT.previs_cuts(cut) == [37], HT.previs_cuts(cut))
ck("run() renders the previs again on a cut, PREVIS_TRIES at most, keeping the least-cut take",
   "for _try in range(_ht.PREVIS_TRIES):" in src and "_ht.previs_cuts(_p_imgs[:_p_len])" in src
   and "_rank < _best[5]" in src)
ck("a previs that cuts on every try is a warning in the log and in info",
   "ht_previs_warn.append(" in src and "check the film there" in src)
ck("a cut the text asks for (same place twice) stops the re-rolls (chain_00150: 13.38/13.42/13.62 s)",
   HT.same_cut([321], [322]) and HT.same_cut([314], [327]) and "if _again:" in src)
_w = HT.layout([192] * 6)["windows"]
ck("window_cuts finds the cuts inside a window",
   HT.window_cuts([314, 321, 200, 191], _w) == [] and HT.window_cuts([145, 210], _w) == [145, 210]
   and HT.window_cuts([136, 158], _w) == [], HT.window_cuts([314, 321, 200, 191, 145, 210, 136, 158], _w))
ck("run() re-rolls on EVERY cut (00154's hop 1 copied one between windows), window cuts rank the takes",
   "_cuts = _ht.previs_cuts(_p_imgs[:_p_len])" in src and "_rank < _best[5]" in src
   and "window_cuts(_cuts" in src)
_walk = torch.stack([torch.roll(scene_a, shifts=6 * t, dims=1) for t in range(60)])     # a fast handheld walk
_walk += 0.03 * torch.randn_like(_walk)
_wcut = _walk.clone()
_wcut[30:] = torch.stack([torch.roll(scene_b, shifts=6 * t, dims=1) for t in range(30)])
ck("a fast walk is not a cut", HT.previs_cuts(_walk) == [], HT.previs_cuts(_walk))
ck("a cut inside a fast walk is found (00154 f66: 3.6x under the old median test)",
   HT.previs_cuts(_wcut) == [30], HT.previs_cuts(_wcut))
ck("a cut that moved, or a new one, is not the same cut",
   not HT.same_cut([321], [261, 327]) and not HT.same_cut([100], [200]) and not HT.same_cut([], [321]))

print("\nthe cut sits at the window START (the previous hop's tail span is dropped)")
# raw hops of chain_00142: the tail span, guided at -22, stepped -11/255; the head,
# guided at 0, -3 at worst. run() rewinds the master by the overlap and writes each
# later hop whole; the outgoing hop is bent to meet it, and is toned on its head only.
ck("run() rewinds the master and writes a hand-tie hop whole",
   "write_pos -= overlap_n" in src and "master_imgs[write_pos:write_pos + keep_n] = imgs" in src)
# chain_00153: 11 joins x a 40 ms overlapping crossfade = sound 440 ms ahead of
# the lips by hop 12. The fade now borrows from the dropped tail; replay the
# arithmetic with run()'s own expression over 12 hops of 8 s.
import math
def _xfade(l, r, sr, ms=40):
    k = min(max(1, int(sr * ms / 1000.0)), l.shape[-1], r.shape[-1])
    t = torch.linspace(0, 1, k)
    return torch.cat([l[..., :-k], l[..., -k:] * torch.cos(t * math.pi / 2) + r[..., :k] * torch.sin(t * math.pi / 2), r[..., k:]], -1)
SR, HOP = 32000, int(round(192 / 24 * 32000))
DROP = int(round(22 / 24 * SR))
mw = torch.zeros(1, 2, HOP)
for _ in range(11):
    late = 0
    xf = min(max(1, int(SR * 40 / 1000.0)), DROP - late)
    mw = _xfade(mw[..., :mw.shape[-1] - DROP + late + xf], torch.zeros(1, 2, HOP)[..., late:], SR)
want = (192 + 11 * 170) / 24 * SR
ck("12 hand-tie hops keep the soundtrack exactly as long as the film (no 40 ms per join)",
   abs(mw.shape[-1] - want) <= 11, (mw.shape[-1], want))   # one sample of rounding per join; the old fade lost 1280
ck("run() borrows the crossfade from the dropped tail", "- dropped + _late + _xf]" in src)
ck("run() switches the sound inside the window at audio_cut's point (picture still cuts at its start)",
   "_ht.audio_cut(master_wav[..., -dropped:], _in[..., :dropped], sr)" in src)
SR = 32000
quiet = 0.001 * torch.randn(1, 2, 29333)                        # room tone, about -60 dB
word = quiet.clone(); word[..., :3500] += 0.3 * torch.randn(1, 2, 3500)    # a word ending 0.11 s in
echo = quiet.clone(); echo[..., :11000] += 0.2 * torch.randn(1, 2, 11000)  # the previs's word, 0.34 s
p1 = HT.audio_cut(word, quiet, SR)
ck("the sound waits for the outgoing word to end (chain_00147: 'tonight')", 3500 <= p1 <= 3500 + 0.03 * SR, p1 / SR)
p2 = HT.audio_cut(word, echo, SR)
ck("... and skips the incoming hop's echo of a previs word ('air')", 11000 <= p2 <= 11000 + 0.03 * SR, p2 / SR)
rain = 0.1 * torch.randn(1, 2, 29333)
ck("one continuous sound across the window switches at the picture cut", HT.audio_cut(rain, rain, SR) == 0)
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

print("\na hop's line sits between its windows (chain_00153: every dialogue error was at a join)")
ck("8 s: 14 words fit between the head window and the dropped tail (6.08 s at 2.4/s)",
   HT.line_budget(192) == 14, HT.line_budget(192))
ck("10 s leaves more room, 5 s less", HT.line_budget(243) > HT.line_budget(192) > HT.line_budget(124))
_b = ("@mina walks down the street, holding her phone out. (S1) says, <d>[English] What's up, "
      "everybody.</d> She grins. Traffic hum, footsteps.")
_t = HT.time_lines([{"beat": _b}])[0]["beat"]
ck("the line's sentence is stamped At 00:01.000, the opening At 00:00.000",
   _t.startswith("At 00:00.000 @mina walks down the street, holding her phone out. At 00:01.000 (S1) says,"), _t)
_one = HT.time_lines([{"beat": "@cook looks out and (S1) says, <d>[English] Snow.</d> The hum."}])[0]["beat"]
ck("a line in the first sentence stamps the beat's start at 00:01.000",
   _one.startswith("At 00:01.000 @cook looks out"), _one)
for label, beat in (("no line", "@mina walks. Traffic hum."),
                    ("already timed", "At 00:02.000 (S1) says, <d>[English] Hi.</d>"),
                    ("a full H3 block", "subject_definitions: x detailed_description: (S1) says, <d>[English] Hi.</d>")):
    ck("left alone: " + label, HT.time_lines([{"beat": beat}])[0]["beat"] == beat)
_vlog = {"beat": "@mina waits at a crosswalk. (S1) says, <d>[English] Quick question for you guys, what "
                 "should I eat next? Drop it in the comments, I'm serious.</d> Beeping."}
ck("the vlog's 16-word line is linted at 8 s, a 9-word one is not (cap 14)",
   len(HT.line_lints([_vlog], [192])) == 1
   and HT.line_lints([{"beat": "(S1) says, <d>[English] One two three four five six seven eight nine.</d>"}], [192]) == [])
ck("run() stamps lines (with the hop lengths) before splitting crossing lines, and lints after",
   "_ht.split_crossing(_ht.time_lines(shots, hop_frames=_tl_frames))" in src
   and "_ht.line_lints(shots, lengths)" in src)
_c = HT.time_lines([{"beat": _b}], hop_frames=[192])[0]["beat"]
ck("the sentence after the line is stamped where the line ends at 3 words/s (00155: mouthing after it)",
   "everybody.</d> At 00:02.000 She grins." in _c, _c)
_long = {"beat": "@mina walks. (S1) says, <d>[English] " + " ".join(["word"] * 30) + ".</d> She smiles. Hum."}
_cl = HT.time_lines([_long], hop_frames=[192])[0]["beat"]
ck("a long line's close is held 0.5 s before the dropped tail (6.58 s at 8 s)",
   ".</d> At 00:06.600 She smiles." in _cl, _cl[-60:])
ck("without hop lengths nothing after the line is stamped (the old call)",
   HT.time_lines([{"beat": _b}])[0]["beat"].count("At ") == 2)
ck("a line that ends the beat has nothing to stamp after it",
   HT.time_lines([{"beat": "@mina waves. (S1) says, <d>[English] Bye.</d>"}], hop_frames=[192])[0]["beat"]
   .count("At ") == 2)
_eat = HT.line_lints([{"beat": "@mina holds the skewer up. (S1) says, <d>[English] Here goes.</d> "
                                 "She takes a big bite."}], [192])
ck("a line and a bite in one hop is linted (00153, 00154, 00160: the line over the eating)",
   len(_eat) == 1 and "bite" in _eat[0], _eat)
ck("eating without a line, or a line that only TALKS about food, is not",
   HT.line_lints([{"beat": "@mina takes a big bite and chews with her lips closed. Crunching."}], [192]) == []
   and HT.line_lints([{"beat": "@mina grins. (S1) says, <d>[English] I could eat ten of these.</d> "
                               "She laughs."}], [192]) == [])
ck("run() re-renders a hand-tie hop that cuts, once, on a seed clear of every other",
   "_hcuts = _ht.previs_cuts(imgs)" in src and "shot_seed + _ht.PREVIS_RETRY_STRIDE" in src
   and "len(_c2) < len(_hcuts)" in src)

print("\nrefusals")
ck("overlap 22 + last_frame_guide off is fine", HT.settings_errors(22, "off") == [])
ck("overlap 5 is refused", len(HT.settings_errors(5)) == 1)
ck("last_frame_guide is refused", len(HT.settings_errors(22, "before_restart")) == 1)
big = [{"beat": "x"} for _ in range(7)]
e = HT.settings_errors(22, "off", big, [192] * 7)
ck("7 hops of 8 s are not refused: they chain into two sections", e == [], e)
ck("run() pins a chained previs on the previous hop's real frames and uses them as window 0",
   "MiniMaxH3AddGuide, \"the chained previs pin\"" in src and "image=prev_imgs, audio=prev_audio" in src
   and "pics[0] = prev_imgs[-_ht.WINDOW:].clone()" in src and '**({"from": _from} if _from else {})' in src)
ck("run() ties a section's last hop at its start only",
   "if _k + 1 < len(_wp) else []" in src and "_store.tensor_digest(_wp[_k + 1]) if _tl else None" in src)
ck("anchor=extend on shot 1 is refused (nothing to continue)",
   len(HT.settings_errors(22, "off", [{"beat": "x", "anchor": "extend"}], [192])) == 1)

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
