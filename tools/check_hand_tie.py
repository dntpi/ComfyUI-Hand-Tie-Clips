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
import re
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
   and HT.sections([{"beat": "x"}] * 7, [192] * 7) == [[0, 1, 2, 3], [4, 5, 6]])
ck("a run split by size alone is split evenly: 10 x 8 s is 5 + 5, every last hop closed",
   HT.sections([{"beat": "x"}] * 10, [192] * 10) == [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]]
   and all(HT.layout([192] * len(s))["closing"] for s in HT.sections([{"beat": "x"}] * 10, [192] * 10)))
_ex = [{"beat": "x"}] * 3 + [{"beat": "x", "anchor": "extend"}] + [{"beat": "x"}] * 2
ck("an authored extend or restart is never moved by the balancing",
   HT.sections(_ex, [192] * 6) == [[0, 1, 2], [3, 4, 5]], HT.sections(_ex, [192] * 6))
ck("the closing window has no moment: one action range per hop, the closing window a carry",
   HT.compile_previs([{"beat": "@a walks."}, {"beat": "@a sits."}], [192, 192])[0]
   == ("At 00:00.000-00:02.833, @a walks. At 00:02.833-00:03.750, " + HT.carry_text("@a")
       + " At 00:03.750-00:05.667, @a sits. At 00:05.667-00:06.583, " + HT.carry_text("@a")))
ck("run() ties a hop's end wherever a next window exists (the closing one included)",
   "[(-(_ht.WINDOW + _xtra), _k + 1)] if _k + 1 < len(_wp)" in src)
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
ck("one action range per beat, in order (plus carry ranges)", text.count(", <Subject 1> ") == 7, text)
ck("plain beats start right AFTER their window (window start + 22 f): 06.583, 09.417",
   all(s in text for s in ("At 00:00.000-00:01.833, <Subject 1> chops",
                           "At 00:06.583-00:08.500, <Subject 1> leans",
                           "At 00:09.417-00:11.333, <Subject 1> lifts")), text)
ck("a window between two plain beats is the earlier beat settling",
   "At 00:05.667-00:06.583, " + HT.carry_text("<Subject 1>") in text
   and "At 00:08.500-00:09.417, " + HT.carry_text("<Subject 1>") in text, text)
ck("a speaking moment is led in so the window lands mid-line (window B 2.833 -> 1.833), no carry range there",
   "At 00:01.833-00:05.667, <Subject 1> turns to the window" in text, text)
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
ck("plain beats: one stamp per window, each beat to its end; the last beat's own sound reaches no window",
   plain == ("At 00:00.000-00:02.833, @cook chops herbs. @cook turns to the window. "
             "At 00:02.833-00:03.750, " + HT.carry_text("@cook") + " "
             "At 00:03.750-00:05.667, @cook looks out of the window. @cook steps to the sink. "
             "At 00:05.667-00:06.583, " + HT.carry_text("@cook") + " "
             "At 00:06.583-00:08.500, @cook sips. "
             "At 00:08.500-00:09.417, " + HT.carry_text("@cook")), plain)
ck("_last_sentence still reads a pronoun ending (kept for moment_end's split)",
   HT._last_sentence("@dex steps out. He walks toward the camera.") == "@dex walks toward the camera.",
   HT._last_sentence("@dex steps out. He walks toward the camera."))
ck("without moments: each beat's first sentence, one per hop",
   no_moment == ("At 00:00.000-00:02.833, First thing. At 00:02.833-00:03.750, " + HT.carry_text("")
                 + " At 00:03.750-00:05.667, Third. At 00:05.667-00:06.583, " + HT.carry_text("")), no_moment)

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
   _off == ("At 00:00.000-00:02.833, @dex walks. At 00:02.833-00:03.750, " + HT.carry_text("@dex")
            + " At 00:03.750-00:05.667, @dex turns. @dex walks toward the camera down the street."
            + " At 00:05.667-00:06.583, " + HT.carry_text("@dex")), _off)
ck("and the default is the sound-carrying one", "The low hum of the refrigerator." in _on, _on)
_eat = HT.moments([{"beat": "@mina slows by a stall. A sizzling grill, the vendor calling out orders."},
                   {"beat": "@mina holds the skewer up. (S1) says, <d>[English] Here goes.</d> She bites. "
                            "The crunch of her chewing, traffic hum, footsteps."}])
ck("a window's sound is the PREVIOUS beat's closing sound (00154: chewing at the bite hop's opening)",
   _eat == ["@mina slows by a stall. A sizzling grill.",
            "@mina holds the skewer up. @mina bites. A sizzling grill."], _eat)
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
   _t.count("A sizzling grill and footsteps on wet pavement.") == 2
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

print("\nbody grade (a second before the cut against a second after)")
prev_g = torch.full((80, 16, 16, 3), 0.40)
prev_g[-HT.WINDOW:] = 0.20                                   # the tail window, which must not be the sample
inc_g = torch.full((80, 16, 16, 3), 0.50)                    # the next hop's body, 25.5/255 hotter
inc_g[:HT.WINDOW] = 0.70                                     # its head window, a third color
prev_g[:, :4, :4] = torch.rand(80, 4, 4, 3)
inc_g[:, :4, :4] = torch.rand(80, 4, 4, 3)
kept = prev_g.clone()
graded, hs, hd = HT.body_grade(prev_g, inc_g, HT.WINDOW)
ck("the hop shift is the body a second back minus the body a second on",
   hs is not None and abs(float(hs.mean()) * 255 + 25.5) < 0.3,
   None if hs is None else (hs * 255).tolist())
ck("the head window takes the extra shift onto that same color",
   hd is not None and abs(float(hd.mean()) * 255 + 51.0) < 0.3,
   None if hd is None else (hd * 255).tolist())
ck("a second after the cut matches a second before it",
   abs(float(graded[HT.FPS, 8, 8, 0]) - 0.40) * 255 < 0.3)
ck("the head window's grade matches that color and keeps a flat picture",
   abs(float(graded[5, 8, 8, 0]) - 0.40) * 255 < 0.3
   and abs(float(graded[5, 8, 8, 0] - graded[6, 10, 10, 0])) * 255 < 0.3)
ck("the previous hop is not modified", torch.equal(prev_g, kept))
ck("no still pixels leaves the hop alone",
   HT.body_grade(torch.rand(80, 16, 16, 3), torch.rand(80, 16, 16, 3), HT.WINDOW)[1] is None)

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

_ct = HT.carry_text("@x")
ck("a window's range carries the beat on: no settle/hold/still/pause/stop/stare (user 22:05: hop 1 tail froze)",
   not hasattr(HT, "settle_text") and _ct.startswith("@x carries on")
   and not any(wd in _ct.lower() for wd in ("settle", "hold", "still", "pause", "stop", "stare", "freez")), _ct)
_pt = HT.compile_previs([{"beat": "@x walks."}, {"beat": "@x sits."}, {"beat": "@x grabs a skewer."}], [68, 68, 68], 24)[0]
ck("the compiled previs carries no settle/hold words, and each beat still starts after its window",
   not any(wd in _pt.lower() for wd in ("settle", "holds", "pause", "stare")) and _pt.count("carries on") >= 2
   and "At 00:03.750-" in _pt and "@x grabs a skewer." in _pt, _pt)

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
ck("the previs is ONE take: no retake loop, no seed stride, no same_cut (2026-10-14)",
   "PREVIS_TRIES" not in src and not hasattr(HT, "PREVIS_TRIES") and not hasattr(HT, "same_cut")
   and "for _try in range(" not in src and "_p_seed = _p_seed0\n" in src
   and "_p_seed0 + _try" not in src and "rendering it again on the next seed" not in src)
ck("the cut detector only logs: WARNING for a cut inside a window, info between windows",
   "_ht.previs_cuts(_p_imgs[:_p_len])" in src
   and "if w <= _c < w + _ht.WINDOW), None)" in src
   and "WARNING: hand-tie previs {_si + 1}: cut at f{_c}" in src
   and "between windows (no hop is tied there)" in src
   and "ht_previs_warn.append((_si + 1, _p_seed, _in_win))" in src and "check the film there" in src)
_w = HT.layout([192] * 6)["windows"]
ck("window_cuts finds the cuts inside a window",
   HT.window_cuts([314, 321, 200, 191], _w) == [] and HT.window_cuts([145, 210], _w) == [145, 210]
   and HT.window_cuts([136, 158], _w) == [], HT.window_cuts([314, 321, 200, 191, 145, 210, 136, 158], _w))
_walk = torch.stack([torch.roll(scene_a, shifts=6 * t, dims=1) for t in range(60)])     # a fast handheld walk
_walk += 0.03 * torch.randn_like(_walk)
_wcut = _walk.clone()
_wcut[30:] = torch.stack([torch.roll(scene_b, shifts=6 * t, dims=1) for t in range(30)])
ck("a fast walk is not a cut", HT.previs_cuts(_walk) == [], HT.previs_cuts(_walk))
ck("a cut inside a fast walk is found (00154 f66: 3.6x under the old median test)",
   HT.previs_cuts(_wcut) == [30], HT.previs_cuts(_wcut))
_w5 = HT.layout([192] * 5)["windows"]

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
   "_ht.tone_match(imgs, _tone_head, None)" in src
   and "_tone_head = _wp[_k]" in src)
ck("run() does not repaint the incoming hop, and does not bend that join",
   "_ht.body_grade(" not in src
   and "_ht.seam_bend(master_imgs, ht_hop_start, write_pos, imgs)" not in src
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
   "_ht.split_crossing(_ht.time_lines(" in src and "_ht.open_lips(_ht.quiet_sounds(shots)), hop_frames=_tl_frames," in src
   and "else _ht.LINE_START_S)))]" in src
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
ck("the previs seed is the run seed, never a shot's own (00164: hop 1 could not be retaken alone)",
   "_p_seed0 = (((int(seed) + i) if seed_per_shot else int(seed))" in src
   and '_p_seed0 = ((int(_sh["seed"])' not in src)

print("\nrefusals")
ck("overlap 22 + last_frame_guide off is fine", HT.settings_errors(22, "off") == [])
ck("overlap 5 is refused", len(HT.settings_errors(5)) == 1)
ck("last_frame_guide is refused", len(HT.settings_errors(22, "before_restart")) == 1)
big = [{"beat": "x"} for _ in range(7)]
e = HT.settings_errors(22, "off", big, [192] * 7)
ck("7 hops of 8 s are not refused: they chain into two sections", e == [], e)
ck("run() pins a chained previs on the previous hop's real frames and uses them as window 0",
   "MiniMaxH3AddGuide, \"the chained previs pin\"" in src and "image=prev_imgs, audio=None if _mute else prev_audio" in src
   and "if not _ht.SEAM_SOUND_ONLY:\n                    pics[0] = prev_imgs[-_ht.WINDOW:].clone()" in src and '**({"from": _from} if _from else {})' in src)
ck("run() ties a section's last hop at its start only",
   "if _k + 1 < len(_wp) else []" in src and "_store.tensor_digest(_wp[_k + 1]) if _tl else None" in src)
ck("anchor=extend on shot 1 is refused (nothing to continue)",
   len(HT.settings_errors(22, "off", [{"beat": "x", "anchor": "extend"}], [192])) == 1)

print("\ncache key")
k = HT.hop_key_fields("aa", "bb")
ck("the key names its two windows and no prev_key", k == {"pin": "hand_tie", "head": "aa", "tail": "bb"}, k)
ck("window sound digests join only when sound is pinned",
   set(HT.hop_key_fields("aa", "bb", "ca", "cb")) == {"pin", "head", "tail", "head_audio", "tail_audio"})

print("\nthe voice rides a hand-tie hop only where it speaks")
_vt = ast.parse(src)
_vns = {"_planner": type("P", (), {"spoken_spans": staticmethod(lambda t: [])})}
for _fn in _vt.body:
    if isinstance(_fn, ast.FunctionDef) and _fn.name in ("_voice_rides_hop", "_start_carries_voice"):
        exec(compile(ast.Module([_fn], []), "h3_ref_chain.py", "exec"), _vns)
_scv = _vns.get("_start_carries_voice")
ck("_start_carries_voice exists in h3_ref_chain.py", _scv is not None)
if _scv:
    _said = "@mina waves. (S1) says, <d>[English] Hi.</d> Footsteps."
    _quiet = "@mina bites the skewer and chews with her lips closed. Crunching, footsteps."
    ck("a hand-tie hop with a line carries the voice", _scv(True, True, "off", _said))
    ck("a quiet hand-tie hop does not (chain_00038)", not _scv(True, True, "off", _quiet))
    ck("voice_every_hop=on still rides a quiet hand-tie hop", _scv(True, True, "on", _quiet))
    ck("a plain chain start carries it, line or not", _scv(True, False, "off", _quiet))
    ck("a continuation is not a start", not _scv(False, True, "on", _said))
ck("run() gates hop and row voices on it",
   "_voice_start = _start_carries_voice(hop_is_start, ht, voice_every_hop, block)" in src
   and "_voice_start or str(hop_script) != " in src
   and "_row_voice_on = (_voice_start or" in src)

print("\nthe previs is one take, played to each beat's end (the Carolyn run, 2026-10-13)")
_car = [
    {"beat": "@carolyn walks down a busy shopping street, holding her phone out. (S1) says, "
             "<d>[English] Welcome back.</d> She grins with her lips closed. "
             "Traffic hum, footsteps, the chatter of the crowd."},
    {"beat": "@carolyn keeps walking with the phone held out. (S1) says, <d>[English] Look.</d> "
             "She smiles. Shop doors chiming, footsteps, a bus pulling away."},
    {"beat": "@carolyn slows beside a street food stall and takes a skewer. (S1) says, "
             "<d>[English] Here goes.</d> She raises the skewer. A sizzling grill, the vendor "
             "calling out orders."},
    {"beat": "@carolyn walks on and takes a big bite of the skewer, then chews slowly with her lips "
             "closed. She swallows and gives the camera a thumbs up. The crunch of the skewer, "
             "traffic hum and footsteps."},
    {"beat": "@carolyn walks on, holding the half-eaten skewer. (S1) says, <d>[English] So good.</d> "
             "She nods. Footsteps, traffic hum, a car horn far off."},
]
_ct, _cl = HT.compile_previs(_car, [192] * 5)
ck("one shot: no bare stamp anywhere, every clock is a range",
   _ct.startswith("At 00:00.000-") and not re.search(r"At \d\d:\d\d\.\d{3}(?!-)", _ct), _ct)
ck("the taste beat plays to its swallow inside its own moment, with no stamp of its own",
   "chews slowly with her lips closed. @carolyn swallows" in _ct
   and _ct.count("At ") == 5 + 5, _ct)
ck("an ending is a person's action, never the beat's sound",
   HT._end_sentence("@a walks. He sits. Traffic hum, footsteps.") == "@a sits."
   and HT._end_sentence("@a walks. Traffic hum, footsteps.") == ""
   and HT._end_sentence("@a walks.") == "", HT._end_sentence("@a walks. He sits. Traffic hum, footsteps."))
ck("an authored moment gets no ending of its own",
   "@x sits" not in HT.compile_previs([{"beat": "@x walks. @x sits.", "moment": "@x stands."},
                                       {"beat": "@x runs."}], [192, 192])[0])
_full = HT.compile_previs([{"beat": "@x walks. @x sits."}] * 6, [192] * 6)
ck("one stamp per window and no more (each extra stamp was a cut: eeebcad, 4.83/7.54/10.50 s)",
   _full[0].count(", @x walks") == 6 and _full[0].count("@x sits") == 6
   and _full[0].count(HT.carry_text("@x")) == 4
   and "At 00:14.167-00:15.083, @x walks" in _full[0], _full[0])

print("\nprevis law (2026-10-14): one shot of contiguous ranges, beats after their windows")
_RG = re.compile(r"At (\d\d):(\d\d\.\d{3})-(\d\d):(\d\d\.\d{3}), ")
def _ranges(txt):
    return [(int(a) * 60 + float(b), int(c) * 60 + float(d)) for a, b, c, d in _RG.findall(txt)]
for _nm, _tx, _ly in (("Carolyn 5", _ct, _cl), ("lab 4", text, lay), ("x6", _full[0], _full[1])):
    _r = _ranges(_tx)
    ck("%s: ranges start at 0, touch end to start and end at the previs end" % _nm,
       bool(_r) and _r[0][0] == 0.0 and all(abs(_r[i][1] - _r[i + 1][0]) < 1e-9 for i in range(len(_r) - 1))
       and abs(_r[-1][1] - _ly["previs"] / float(HT.FPS)) < 1e-3 and len(_r) == _tx.count("At "), _r)
    ck("%s: no bare single stamp (a bare At is a camera cut)" % _nm,
       not re.search(r"At \d\d:\d\d\.\d{3}(?!-)", _tx))
_r = _ranges(_ct)
_starts = {round(a * HT.FPS) for a, _ in _r}
ck("Carolyn 5: every plain beat k>0 starts at window k + 22",
   all((w + HT.WINDOW) in _starts for w in _cl["windows"][1:4]), (sorted(_starts), _cl["windows"]))
ck("the stamp grammar is gone from the previs (no 'The same take continues as')",
   not hasattr(HT, "CONTINUE") and "The same take continues" not in _ct)
ck("no voice reaches the previs (gibberish from 'the chatter of the crowd')",
   not re.search(r"chatter|calling out", _ct), _ct)

print("\nvoices leave the sound sentences")
ck("a voice item is taken out, the rest kept",
   HT.quiet_sound("Traffic hum, footsteps, the chatter of the crowd.")
   == ("Traffic hum and footsteps.", ["the chatter of the crowd"]),
   HT.quiet_sound("Traffic hum, footsteps, the chatter of the crowd."))
ck("a sound with no voice is returned untouched",
   HT.quiet_sound("A sizzling grill, footsteps, a car horn far off.")
   == ("A sizzling grill, footsteps, a car horn far off.", []))
ck("non-verbal sounds stay (laughter, cheers, a refrigerator's hum)",
   HT.quiet_sound("Laughter, cheers from the club, the low hum of the refrigerator.")[1] == [])
ck("every item a voice: nothing left", HT.quiet_sound("Crowd chatter, voices.")[0] == "")
_qs = HT.quiet_sounds(_car)
ck("a hop's beat loses the voice from its sound sentence only",
   _qs[0]["beat"].endswith("She grins with her lips closed. Traffic hum and footsteps.")
   and _qs[2]["beat"].endswith("She raises the skewer. A sizzling grill.")
   and _qs[1]["beat"] == _car[1]["beat"], [q["beat"][-60:] for q in _qs])
ck("a beat ending on a person, or a line, is not a sound sentence",
   HT.quiet_sounds([{"beat": "@a walks. She talks to the vendor."},
                    {"beat": "@a waves. (S1) says, <d>[English] Stop talking.</d>"}])
   == [{"beat": "@a walks. She talks to the vendor."},
       {"beat": "@a waves. (S1) says, <d>[English] Stop talking.</d>"}])
ck("the input shots are not modified", "chatter" in _car[0]["beat"])
_lv = [w for w in HT.line_lints(_car, [192] * 5) if "names a voice" in w]
ck("the lint names each voice it leaves out", len(_lv) == 2 and "the vendor calling out orders" in _lv[1], _lv)
ck("run() compiles the hops from quiet_sounds",
   "_ht.open_lips(_ht.quiet_sounds(shots))," in src)

print("\na fresh section's first hop opens free (hop 1's closed mouth, its f171 cut)")
ck("hop 1 of a fresh section, no start image: free",
   HT.free_head(0, False) and HT.free_head(0, False, False))
ck("a chained section's first hop keeps its head; so does any later hop; so does a start image",
   not HT.free_head(0, True) and not HT.free_head(1, False) and not HT.free_head(0, False, True))
ck("run() drops the head tie for it, and the head from its key",
   "_free = _ht.free_head(_k, ht_chained[_si], start_image is not None)" in src
   and "_ties = ([] if _free else [(0, _k)])" in src
   and "_store.tensor_digest(_wp[_k]) if _hd else None" in src
   and "_store.audio_digest(_ws[_k]) if _hd else None" in src)
ck("run() does not tone-match a free hop to a window it was never tied to",
   "or _ht.free_head(_k, ht_chained[_si], start_image is not None)):" + chr(10)
   + "                    # No head window to match" in src)

print("\nthe sound first: each hop drafts its sound, the picture renders to it")
_blk = ("detailed_description: At 00:01.000 <Subject 1> says, <d>[English] What's up, "
        "everybody, welcome back.</d> Footsteps. <d>[English] Okay here<|cutoff|></d>")
ck("a hop's planned words: every line, the language tag and <|cutoff|> left out",
   HT.hop_words(_blk) == ["what's", "up", "everybody", "welcome", "back", "okay", "here"],
   HT.hop_words(_blk))
ck("a quiet hop plans no words", HT.hop_words("@a bites the skewer. Crunching.") == [])
ck("[English] or no tag is 'en'; another language is detected",
   HT.hop_language(_blk) == "en" and HT.hop_language("<d>hi</d>") == "en"
   and HT.hop_language("<d>[Japanese] konnichiwa</d>") is None)
_line = HT.hop_words(_blk)
ck("the line, said: passes",
   HT.judge_sound(_line, " What's up everybody, welcome back. Okay, here.")[0])
ck("most of the line plus a stray word: passes",
   HT.judge_sound(_line, "What's up, everybody! Welcome back, okay?")[0])
ck("a different sentence (00165 hop 4's 'quick brown fox'): fails",
   not HT.judge_sound(_line, "makes a man please point up every time the quick brown fox jump")[0])
ck("nothing said on a hop with a line: fails", not HT.judge_sound(_line, "")[0])
ck("the line buried in invented speech: fails",
   not HT.judge_sound(_line, "What's up everybody welcome back okay here and then the man said "
                             "we should go down to the river before the rain comes")[0])
ck("a quiet hop with a hum Whisper reads as 'Thank you.': passes",
   HT.judge_sound([], " Thank you.")[0])
ck("a quiet hop that talks: fails", not HT.judge_sound([], "so this is the best one on the street")[0])
ck("a better draft scores higher",
   HT.judge_sound(_line, "What's up everybody welcome back okay here")[1]
   > HT.judge_sound(_line, "What's up everybody")[1])
ck("v3: the sound first is OFF by default (opt-in), the previs is smaller and muted",
   (not HT.SOUND_FIRST) and HT.PREVIS_MUTE and HT.SOUND_TRIES >= 2
   and HT.PREVIS_RESOLUTION.startswith("512p") and HT.SOUND_DRAFT_RESOLUTION.startswith("448p"))
_t0 = torch.full((48, 8, 6, 3), 0.5)
_ts = torch.full((22, 4, 3, 3), 0.6)
_tm, (_th, _) = HT.tone_match(_t0, _ts, None)
ck("tone_match takes a window cut from a smaller previs",
   abs(float(_th[0]) - 0.1) < 1e-5 and abs(float(_tm[0, 0, 0, 0]) - 0.6) < 1e-5, (_th, _tm[0, 0, 0]))
_d0 = src.find("if ht and sound_first:")
_lk = src.find("latent = _splice_locked_audio(" + chr(10) + "                        latent, _kept[0], carry=_carry,")
_sm = src.find('SamplerCustomAdvanced, "the sampler",')
ck("run() drafts after the windows are tied and before the hop samples, then freezes the kept sound",
   -1 not in (_d0, _lk, _sm) and src.find('"the hand-tie window"') < _d0 < _lk < _sm)
ck("the draft carries the hop's own windows, and stops at the first pass",
   'MiniMaxH3AddGuide, "the sound draft window"' in src
   and "for _t in range(_ht.SOUND_TRIES):" in src and "if _ok:" + chr(10) + "                            break" in src)
ck("run() keys the hop on the sound draft only when it runs",
   'hop_payload["sound_first"] = [sd_w, sd_h, _ht.SOUND_TRIES,' in src
   and "if sound_first:" + chr(10) + "                        # Which draft is kept" in src)
ck("the sound first stands down under master audio and the boost",
   "sound_first = bool(_ht.SOUND_FIRST) and locked is None and boost_model is None" in src)
ck("the previs renders on its own canvas, keyed on it",
   "width=int(pv_w), height=int(pv_h), length=_p_len," in src
   and '**({"canvas": [pv_w, pv_h]} if (pv_w, pv_h) != (int(width), int(height))' in src)
ck("the previs is muted unless a window speaks, keyed on it, and pins no sound when muted",
   "_mute = bool(_ht.PREVIS_MUTE) and not any(ht_speaks[_si])" in src
   and '**({"mute": True} if _mute else {}),' in src
   and "audio=None if _mute else prev_audio" in src
   and "_pl = _splice_locked_audio(_pl, _encode_locked_slice(" in src)
_pc = HT.previs_canvas
ck("768p 9:16 hops of 8 s: the previs takes the 512p tier (512x896), under one hop",
   _pc(768, 1344, [192] * 5, 362, (512, 896)) == (512, 896))
ck("512p hops of 8 s: the previs shrinks to fit one hop's pixels x frames (352x640)",
   _pc(512, 896, [192] * 5, 362, (512, 896)) == (352, 640)
   and 352 * 640 * 362 <= 512 * 896 * 192)
ck("every previs costs no more than one of its hops (above the floor)",
   all(pw * ph * pf <= w * h * max(fr) for (w, h) in ((768, 1344), (1344, 768), (640, 1152), (896, 512))
       for fr, pf in (([192] * 5, 362), ([243] * 4, 362), ([192] * 3, 226), ([124] * 6, 362))
       for pw, ph in [_pc(w, h, fr, pf, (512, 896) if h > w else (896, 512))]
       if min(pw, ph) > HT.PREVIS_MIN_SHORT))
ck("a previs no longer than its hop keeps the hop canvas under no cap",
   _pc(512, 896, [362], 362) == (512, 896))
ck("no short edge under the floor, on the 32 px grid",
   min(_pc(448, 800, [124] * 6, 362, None)) >= HT.PREVIS_MIN_SHORT
   and all(v % 32 == 0 for v in _pc(448, 800, [124] * 6, 362, None)))
ck("run() sizes each section's previs with previs_canvas",
   'ht_pv.append(_ht.previs_canvas(width, height, _frames, _lay["previs"], pv_cap))' in src
   and "pv_w, pv_h = ht_pv[_si]" in src)
_hs = open(os.path.join(HERE, "hear.py"), encoding="utf-8").read()
ck("hear.py never downloads: local cache only, on the CPU",
   "local_files_only=True" in _hs and "device=_device()" in _hs and "snapshot_download(repo," in _hs)
# ht_seam_fix (recover, 2026-10-14)
ck("hear.py takes the GPU when CUDA is up, else the CPU",
   "return 0 if torch.cuda.is_available() else -1" in _hs and 'return_timestamps": "word"' in _hs)
ck("early_words flags only the NEW line inside the overlap",
   HT.early_words(["hi", "there"], [("so", 0.1, 0.3), ("Hi", 0.5, 0.7), ("there", 1.2, 1.4)], 0.917)
   == [("hi", 0.5)] and HT.early_words(["hi"], [("Hi", 1.0, 1.2)], 0.917) == [])
ck("ht_seam_fix is off by default, and unique_id stays last after the audio widgets",
   '"ht_seam_fix": (["off", "on"], {' in src and 'ht_seam_fix="off"' in src
   and "audio_model=None,\n            unique_id=None):" in src)
ck("ht_seam_fix keys every hop, version 8 (seam source / gap order / guide carry removed)",
   'if _sfix:' + chr(10) + '                    hop_payload["ht_seam_fix"] = [8,' in src)
ck("ht_seam_fix v4: tone match and mean anchor off by default, tail discard kept, keyed",
   HT.SEAMFIX_TONE_MATCH is False and HT.SEAMFIX_ANCHOR is False and HT.SEAMFIX_TAIL_EXTRA == 34
   and "bool(_ht.SEAMFIX_TONE_MATCH), bool(_ht.SEAMFIX_ANCHOR)]" in src
   and "if _sfix and _ht.SEAMFIX_ANCHOR:" in src and "if (_tone_head is None\n                        or _ht.free_head" in src)
ck("startup line no longer claims a x0.75 anchor",
   'latent mean anchor "' + chr(10) + '                  f"x{_ht.SEAMFIX_ANCHOR_STRENGTH})"' not in src)
ck("every hop logs which voice ref it carries", '"[{TAG}] hop {i + 1}: voice ref "' in src.replace("f\"", "\""))
_wfp = os.path.join(HERE, "workflows", "HandTieClips_HandTie_SeamFix_v2.json")
_sla = [n for n in __import__("json").load(open(_wfp, encoding="utf-8"))["nodes"] if n.get("type") == "H3SLAAttention"]
ck("SeamFix_v2: SLA protect_audio on, so the voice ref's audio tokens survive 65% sparsity",
   bool(_sla) and all(n["widgets_values"][4] is True for n in _sla))
# ht_seam_fix v2 (recover, 2026-10-14)
ck("v2: the tail discard is whole 17-frame blocks and covers the 22-frame tail",
   HT.SEAMFIX_TAIL_EXTRA % 17 == 0 and HT.SEAMFIX_TAIL_EXTRA >= HT.WINDOW and "_dsteps = int(frames) // 17 * 5" in src
   and "render_length = hop_length + _xtra" in src and "sampled = _drop_latent_tail(sampled, _xtra)" in src)
ck("v3: the line starts at 1.0 s with seam fix on or off (v1 timing)",
   HT.SEAMFIX_LINE_START_S == 1.0 and HT.LINE_START_S == 1.0)
ck("v2: invented_words flags any word after the overlap",
   HT.invented_words([("so", 0.1, 0.3), ("Thanks", 1.5, 1.8)], 0.917) == [("Thanks", 1.5)]
   and HT.invented_words([("so", 0.1, 0.3)], 0.917) == [])
ck("v3: anchor 0.3 kept; no hop opens on another (_seam_prev is always False); draft head lock off",
   HT.SEAMFIX_ANCHOR_STRENGTH_V2 == 0.3 and "_seam_prev = False" in src
   and "_pin_continue(" not in src[src.find("                if ht:\n                    # Window k at frame 0"):src.find("pin_mech_used = \"hand_tie\"")]
   and not HT.SEAMFIX_PICTURE_ONLY and not HT.SEAMFIX_DRAFT_LOCK)
ck("v3: the single-pass hop carries the voice ref on its own conditioning",
   "ref_audios=hop_ref_audios," in src[src.find('"the reference conditioning"'):src.find('"the reference conditioning"') + 1200])
ck("v3: the hop key records sound_first and the v2 sound switches",
   "bool(sound_first), bool(_ht.SEAMFIX_PICTURE_ONLY)," in src)
ck("a one-hop chain skips the previs and ties to no None window",
   "if len(_shots_full) <= 1 and not previs_only:" in src and "if _wp[_w] is not None]" in src)
import torch as _tt
import importlib as _il
_rc = sys.modules.get(HT.__name__.rsplit(".", 1)[0] + ".h3_ref_chain")
if _rc is not None and hasattr(_rc, "_mean_anchor"):
    _v = _tt.zeros(1, 2, 10, 2, 2); _v[:, :, 0::5] += 1.0
    _ref = {"samples": _v.clone()}
    _, _tab = _rc._mean_anchor(_ref, None, 0.0)
    _w = {"samples": _v.clone() - 0.5}
    _o, _ = _rc._mean_anchor(_w, _tab, 1.0)
    ck("_mean_anchor puts each block position back on hop 1's level",
       float((_o["samples"] - _v).abs().max()) < 1e-5)

print("\nclosed lips leave a hop that speaks (the push run: 'Today...' said with lips closed)")
_ol = HT.open_lips([
    {"beat": "@c walks. @c says, <d>[English] Hi there.</d> She grins with her lips closed and "
             "glances at the crowd. Footsteps."},
    {"beat": "@c chews slowly with her lips closed, eyes shut. The crunch of the skewer."},
    {"beat": "@c says, <d>[English] Yes.</d> She gives a thumbs up with her lips pressed together "
             "in a smile. She smiles at the camera, lips closed."},
])
ck("a speaking hop loses 'with her lips closed'",
   _ol[0]["beat"].endswith("She grins and glances at the crowd. Footsteps."), _ol[0]["beat"])
ck("a quiet hop keeps it (chewing with lips closed is the picture)",
   "chews slowly with her lips closed" in _ol[1]["beat"])
ck("'lips pressed together in a smile' and a bare ', lips closed' go too",
   _ol[2]["beat"].endswith("She gives a thumbs up. She smiles at the camera.")
   and "lips" not in _ol[2]["beat"], _ol[2]["beat"])
_psrc = open(os.path.join(HERE, "planner.py"), encoding="utf-8").read()
_rules = _psrc[_psrc.find("def hand_tie_rules"):_psrc.find("# A music video renders over the song")]
_lint = " ".join(HT.line_lints([{"beat": "@a says, <d>[English] " + "word " * 30 + "</d>"}], [192]))
ck("the writer no longer asks for closed lips after a line, nor does the lint",
   "lips closed" not in _rules and "closed-mouth" not in _rules and "def hand_tie_rules" in _rules
   and "closed-mouth" not in _lint and "fills the rest with mouthing" in _lint, _lint)
ck("run() compiles the hops through open_lips",
   "_ht.open_lips(_ht.quiet_sounds(shots)), hop_frames=_tl_frames," in src)

print("\nsound carry: hop k's sound crosses the join, its picture does not")
ck("SEAM_SOUND_ONLY is on and SAVE_WINDOWS is off by default (no disk bloat)", HT.SEAM_SOUND_ONLY is True and HT.SAVE_WINDOWS is False)
def _grab(name):
    a = src.find("def " + name + "(")
    b = src.find("\ndef ", a + 1)
    return src[a:b]
_ns = {"_HTC_PRIOR": "_htc_prior_kf"}
exec(_grab("_mark_prior_keyframes") + "\n" + _grab("_strip_video_keyframes"), _ns)
_prior = [["e", {"minimax_keyframes": [{"resolved_frame_index": 100, "latent": "anchor"}]}]]
_m = _ns["_mark_prior_keyframes"](_prior)
# what Motion-Context.apply returns: copies of the prior keyframes, then its picture
# keyframes (one per latent step), then its sound keyframe (nodes.py:649)
_after = [["e", {"minimax_keyframes": [dict(kf) for kf in _m[0][1]["minimax_keyframes"]]
               + [{"resolved_frame_index": j, "latent": "hopk_pic%d" % j} for j in range(6)]
               + [{"resolved_frame_index": -0.4, "audio_latent": "hopk_sound"}]}]]
_c, _nv, _na = _ns["_strip_video_keyframes"](_after)
_kf = _c[0][1]["minimax_keyframes"]
ck("no picture keyframe from hop k survives the strip",
   _nv == 6 and not any(str(k.get("latent", "")).startswith("hopk_pic") for k in _kf), _kf)
ck("hop k's sound keyframe is kept", _na == 1 and any(k.get("audio_latent") == "hopk_sound" for k in _kf), _kf)
ck("a picture keyframe that was on the conditioning before stays, untagged",
   any(k.get("latent") == "anchor" for k in _kf) and not any("_htc_prior_kf" in k for k in _kf), _kf)
ck("_pin_continue strips the picture in sound-only mode and refuses a missing sound guide",
   "if sound_only:\n                cond = _mark_prior_keyframes(cond)" in src
   and "cond, _nv, _na = _strip_video_keyframes(cond)" in src
   and "Motion-Context added no sound keyframe" in src)
ck("every tie is picture only off a muted previs (voice fix v7 kept)",
   "_pv_muted = bool(_ht.PREVIS_MUTE) and not any(ht_speaks[_si])" in src
   and "_wa = None if _pv_muted else _ws[_w]" in src and "_wimg = _wp[_w]" in src and "image=_wimg, audio=_wa," in src)
ck("hop keys no longer chain the previous hop under hand-tie",
   "None if (hop_restart or ht) else prev_key," in src and '"prev_hop_sound_v1"' not in src)

print("\nties: every hand-tie hop opens on its previs window and ends on the next (end tie at -56)")
_a = src.find("                    _ties = ([] if _free else")
_b = src.find("_ties = [(_fi, _w) for _fi, _w in _ties if _wp[_w] is not None]", _a)
_b = src.find("\n", _b)
import textwrap as _tw
_blk = _tw.dedent(src[_a:_b])
def _ties_for(k, n_win, free=False):
    class _H: pass
    _h = _H(); _h.WINDOW = 22
    ns = {"_free": free, "_k": k, "_xtra": 34, "_wp": ["w"] * n_win, "_ht": _h}
    exec(_blk, ns)
    return ns["_ties"]
_got = {k: _ties_for(k, 5) for k in range(1, 3)}
ck("hops 2..N-1 get a head tie at 0 and a tail tie at -(22+34) on window k+1",
   all(_got[k] == [(0, k), (-56, k + 1)] for k in _got), _got)
ck("hop 1 (free head) keeps its tail tie only", _ties_for(0, 5, free=True) == [(-56, 1)])
ck("a section's last hop (no window k+1) is head-only", _ties_for(3, 4) == [(0, 3)])
ck("the hand-tie log names head-only / tail-only, not 'the section's last hop'",
   "(head only, at its start)" in src and "(tail only, at its end)" in src
   and "its start; the section's last hop" not in src)
ck("a tail tie is saved as a tail window (SAVE_WINDOWS covers every tie)",
   '_role = "head" if _fi >= 0 else "tail"' in src)
ck("each tie's guide window is saved as AddGuide encodes it",
   "if _ht.SAVE_WINDOWS:" in src and "_guide_frames_as_used(_wimg, latent)" in src
   and '"lanczos", "center"' in _grab("_guide_frames_as_used") and "while n % 17 != 5:" in _grab("_guide_frames_as_used")
   and '"HANDTIECLIPS_windows"' in src)

print("\nprevis cut lint: the Storyboard4 previs of the 17:26 run (seed 13069 cut f62/f126/f191, 117798 f195)")
# Windows at f0/68/136/204: f62 sits before window 2's stamp, f126 before window 3's,
# f191/f195 before window 4's. Each of those windows must be flagged.
_sb4 = [{"beat": b, "duration": "8 s"} for b in ["@carolyn walks down a busy shopping street lined with colorful shop windows in the late afternoon sun, holding her phone out. @carolyn says, <d>[English] What's up, everybody, welcome back. Today is all about street food.</d> She grins at the camera and glances at the passing crowd. Traffic hum and footsteps.", '@carolyn keeps walking with the phone held out, the colorful shop windows sliding past behind her. @carolyn says, <d>[English] Look at this block, every window is a different color. I love it.</d> She slows to a stop beside a street food stall and smiles at the camera. Shop doors chiming, footsteps, a bus pulling away from the curb.', '@carolyn stands at the street food stall on the same street and takes a skewer from the vendor with her free hand. @carolyn says, <d>[English] This is the stall everyone talks about. Okay, here goes the first bite.</d> She raises the skewer toward the camera with a grin, the stall behind her. A sizzling grill and the clink of metal tongs.', '@carolyn steps back from the stall and takes a big bite of the skewer, then chews slowly with her lips closed, eyes shut, nodding at the camera. She swallows, opens her eyes wide and gives the camera a thumbs up beside the stall. The crunch of the skewer, traffic hum and footsteps.']]
ck("PREVIS_CUT_LINT is off by default (wording cannot close a previs cut)", HT.PREVIS_CUT_LINT is False)
_lint_default = HT.PREVIS_CUT_LINT
HT.PREVIS_CUT_LINT = True  # the lint tests below exercise it switched on
_ms = HT.moments(_sb4)
_tr = HT.cut_triggers(_ms)
ck("window 1 (no stamp before it) is not flagged", _tr[0] == [], _tr[0])
ck("window 2 (f62 cut) flags the arrival at the stall",
   any(k == "location" and "slows to a stop beside a street food stall" in p for k, p, _ in _tr[1]), _tr[1])
ck("window 3 (f126 cut) flags the stall re-established and the stall behind her",
   any(k == "location" and "stands at the street food stall" in p for k, p, _ in _tr[2])
   and any(k == "framing" and "the stall behind her" in p for k, p, _ in _tr[2]), _tr[2])
ck("window 4 (f191/f195 cut, both takes) flags stepping back from the stall",
   any(k == "framing" and "steps back from the stall" in p for k, p, _ in _tr[3]), _tr[3])
ck("the skewer taken in window 3 is not a state jump in window 4",
   not any(k == "state" for k, _, _ in _tr[3]), _tr[3])
_sm, _ch = HT.smooth_moments(_ms)
ck("rewrites windows 2, 3 and 4 and nothing else", [c[0] for c in _ch] == [1, 2, 3], [c[0] for c in _ch])
ck("window 2: the stall is walked up to", "walks up to a street food stall, stops beside it," in _sm[1], _sm[1])
ck("window 3: she is still at the stall, not re-established on the same street",
   _sm[2].startswith("@carolyn is still at the street food stall and takes a skewer") and "same street" not in _sm[2], _sm[2])
ck("window 4: she stays at the stall", _sm[3].startswith("@carolyn leans back a little, still at the stall, and takes a big bite"), _sm[3])
_af = HT.cut_triggers(_sm)
ck("after the rewrite no location trigger is left at the stamps of windows 3-4 and no step-away",
   not any(k == "location" for k, _, _ in _af[2] + _af[3]) and not any("steps back" in p for _, p, _ in _af[3]), _af)
ck("the previs text carries the rewrites", "is still at the street food stall" in HT.compile_previs(_sb4, [192] * 4)[0])
_save = HT.PREVIS_CUT_LINT
HT.PREVIS_CUT_LINT = False
ck("PREVIS_CUT_LINT=False leaves the previs text as before",
   "stands at the street food stall on the same street" in HT.compile_previs(_sb4, [192] * 4)[0]
   and HT.cut_lint_report(_sb4) == [])
HT.PREVIS_CUT_LINT = _save
ck("a held object with nothing that gave it is a state jump",
   any(k == "state" for k, _, _ in HT.cut_triggers(["She walks down the street.", "She raises the cup to the camera."])[1]))
ck("camera and time words are flagged",
   {k for k, _, _ in HT.cut_triggers(["She walks.", "A wide shot reveals the street. Later she sits."])[1]} >= {"framing", "time"})
_rep = HT.cut_lint_report(_sb4)
ck("the log report names each window and the rewrite", any(l.startswith("previs window 4: framing") for l in _rep)
   and any(l.startswith("previs window 3 rewritten for one take") for l in _rep), _rep)
HT.PREVIS_CUT_LINT = _lint_default
ck("run() logs the report per section", "for _w in _ht.cut_lint_report([_pshots[j] for j in _sec]):" in src)

print("\nremoved 2026-10-14: ht_seam_source, gap_order, guide_carry")
_it = ast.parse(src)
_opt = None
for _n in ast.walk(_it):
    if isinstance(_n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "swap_refs" for k in _n.keys):
        _opt = [k.value for k in _n.keys if isinstance(k, ast.Constant)]
ck("audio_model is the last optional input, and swap_refs still precedes the audio widgets",
   bool(_opt) and _opt[-1] == "audio_model"
   and _opt[_opt.index("swap_refs") + 1] == "audio_refine", (_opt or [])[-4:])
ck("no ht_seam_source / gap_order / guide_carry widget",
   bool(_opt) and not ({"ht_seam_source", "gap_order", "guide_carry"} & set(_opt)))
ck("no gap order or guide carry code left in run()",
   not any(t in src for t in ("_gap_plan", "_gap_out", "_gap_bridge", "gap_render_order", "gap_role(",
                              "_ht_guide_carry", "_gcarry", "_ht_seam_prev_hop", "_seam_src", "_snd_carry")))
ck("hand_tie.py has no GAP_ORDER / gap_render_order / gap_role",
   not any(hasattr(HT, a) for a in ("GAP_ORDER", "gap_render_order", "gap_role")))

print("\nthe module stays ComfyUI-free")
tree = ast.parse(open(os.path.join(HERE, "hand_tie.py"), encoding="utf-8").read())
mods = {n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])}
ck("imports only stdlib + torch", mods <= {"re", "torch", "torch.nn.functional"}, sorted(m for m in mods if m))

import io as _io
_rp = _io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "js", "editor", "run_panel.js"), encoding="utf-8").read()
ck("run_panel.js no longer lists ht_seam_source, gap_order or guide_carry",
   not any(t in _rp for t in ('"ht_seam_source"', '"gap_order"', '"guide_carry"')))

print("\nsolo re-render keeps the full previs (render_through/render_from, 2026-10-14)")
# 19:38:04 log: render_through=3 gave "hops 1-3, previs 226f at 480x896" vs the full
# run's "hops 1-4, previs 294f at 448x800", so the previs key missed. run() now plans
# every section from the full plan (_shots_full/_lengths_full); this replays that plan.
def _pv_plan(shots, lens, W=736, H=1280):
    out = []
    for sec in HT.sections(shots, lens):
        fr = [lens[j] for j in sec]
        txt, lay = HT.compile_previs([shots[j] for j in sec], fr, HT.FPS)
        out.append((sec, txt, lay["previs"], lay["windows"],
                    HT.previs_canvas(W, H, fr, lay["previs"], (448, 800)),
                    HT.window_speaks([shots[j] for j in sec], len(lay["windows"]))))
    return out
_sb = [{"beat": "@a walks."}, {"beat": "@a stops."}, {"beat": "@a sits."}, {"beat": "@a waves."}]
_full = _pv_plan(_sb, [124] * 4)
_trunc = _pv_plan(_sb[:3], [124] * 3)
ck("the old truncated plan really differs (the bug this guards)", _full != _trunc,
   [(p[2], p[4]) for p in _full + _trunc])
_pshots, _plens = list(_sb), [124] * 4      # what run() plans from at render_through=3
ck("previs length, windows, canvas, text identical with and without render_through/render_from",
   _pv_plan(_pshots, _plens) == _full)
_src_n = src.replace("\r\n", "\n")
ck("run() plans previs sections from the full plan, not the render range",
   "_shots_full = list(shots)" in _src_n and "_lengths_full = list(lengths)" in _src_n
   and "_pshots, _plens = _shots_full, _lengths_full" in _src_n
   and "enumerate(_ht.sections(_pshots, _plens))" in _src_n
   and "_ht.compile_previs([_pshots[j] for j in _sec], _frames, FPS)" in _src_n
   and "_ht.window_speaks([_pshots[j] for j in _sec]" in _src_n
   and "if len(_shots_full) <= 1 and not previs_only:" in _src_n)
ck("the previs seed is per section start hop, not the render range",
   "_p_seed0 = (((int(seed) + i) if seed_per_shot else int(seed))" in _src_n)
ck("the last rendered hop keeps its tail discard as in the full run (same hop key)",
   "if (_sfix and i + 1 < len(_shots_full) and locked is None) else 0)" in _src_n)

print("\nqwen3-vl previs compiler + lead_clause + seam_feather (2026-10-16)")
_lc = HT.lead_clause("@carolyn steps back from the stall and takes a big bite of the skewer, then chews slowly.")
ck("lead_clause strips compound mid-hop payoff ('and takes a big bite..., then chews')",
   _lc == "@carolyn steps back from the stall.", _lc)
_parsed = HT.parse_previs_llm_moments(
    "1: @carolyn walks down the street.\n2: She continues past the shopfronts.\n",
    [{"beat": "@carolyn walks down the street."},
     {"beat": "@carolyn continues past the shopfronts and waves."},
     {"beat": "@carolyn steps back from the stall and takes a big bite of the skewer."}],
)
ck("parse_previs_llm_moments restores @tag on pronoun lines and falls back to lead_clause on missing lines",
   _parsed[0]["moment"] == "@carolyn walks down the street."
   and _parsed[1]["moment"] == "@carolyn continues past the shopfronts."
   and _parsed[2]["moment"] == "@carolyn steps back from the stall.",
   [p.get("moment") for p in _parsed])
_parsed_props = HT.parse_previs_llm_moments(
    "1: @carolyn walks down the street, holding her phone out.\n"
    "2: @carolyn stands at the street food stall on the same street and takes a skewer.\n"
    "3: @carolyn stands beside the street musician playing guitar on the corner, holding the phone out so he is behind her.\n"
    "4: @carolyn stands at the end of the block, holding the phone close with the cone in her other hand.\n",
    [{"beat": "@carolyn walks down the street, holding her phone out."},
     {"beat": "@carolyn stands at the street food stall on the same street and takes a skewer."},
     {"beat": "@carolyn stands beside the street musician playing guitar on the corner, holding the phone out so he is behind her."},
     {"beat": "@carolyn stands at the end of the block, holding the phone close with the cone in her other hand."}],
)
ck("parse_previs_llm_moments strips mid-chain props (skewer, cone) and crowd clutter while keeping Shot 1 phone and continuous motion",
   _parsed_props[1]["moment"] == "@carolyn walks up to the street food stall."
   and _parsed_props[2]["moment"] == "@carolyn walks past the street musician, holding the phone out."
   and _parsed_props[3]["moment"] == "@carolyn walks up to the end of the block, holding the phone close.",
   [p.get("moment") for p in _parsed_props])

_sf_m = torch.full((80, 16, 16, 3), 0.40)
_sf_m[40:] = 0.44  # +10.2 / 255 step at cut=40
_sf_pw = torch.full((22, 16, 16, 3), 0.40)
_sf_step = HT.seam_feather(_sf_m, 40, _sf_pw, span=10, blend=16)
ck("seam_feather closes the 1-frame step across the cut and leaves the hop bodies untouched",
   _sf_step is not None
   and float((_sf_m[40] - _sf_m[39]).abs().max()) * 255 < 1.5
   and float((_sf_m[10] - 0.40).abs().max()) < 1e-6
   and float((_sf_m[70] - 0.44).abs().max()) < 1e-6)
_tf_m = torch.full((80, 8, 8, 3), 0.40)
_tf_m[-22:] = 0.46  # +15.3 / 255 closing tail-guide flash
HT.tail_feather(_tf_m, 80, window=22)
ck("tail_feather flattens the closing -22 tail-guide flash on the final hop",
   float((_tf_m[-1] - 0.40).abs().max()) * 255 < 0.5)
ck("hand-tie v2.6: mod5-calibrated 3-step latent handoff + 17f ViT3D warm prefix + post-deflicker seam_feather (no sampler noise_mask)",
   'hop_payload["ht_inpaint"] = "v2.6"' in src
   and 'ref_k = src_5[:, :, k % 5:k % 5 + 1]' in src
   and 'out["_vae_prefix"] = src_5' in src
   and 'for _c in ht_cuts:\n                _ht.seam_feather(master_imgs, _c)' in src)

print("\ndirect previs-to-previs spine + 3 lossless previs optimizations + hand_tie_mc (2026-10-17)")
ck("chained previs pins directly onto previous section's closing window W_end + 17f warm prefix",
   "nonlocal prev_imgs" in src
   and "_chain_pin_imgs = _prev_w_end if _prev_w_end is not None else prev_imgs" in src
   and "pics[0] = _prev_w_end.clone()" in src
   and "warm_pics[0] = _prev_w_warm.clone()" in src
   and "_calibrate_overlap_latent(\n                                _ps, _ht_pv_lat[_si - 1], _ht.WINDOW" in src
   and "cap_high=True, high_rms_ref=_ht_pv_hi_ref[0]" in src)
ck("previs optimizations: 0.50 MP ref cap, ref_audios=None + skip_audio when muted, and pre_encode_hop batching",
   '_pv_ref_size = ("0.50 MP"' in src
   and "ref_audios=(None if _mute else hop_ref_audios)," in src
   and "skip_audio=bool(_mute and not previs_only)" in src
   and "_ht_previs(i, pre_encode_hop=_hop_ref2va)" in src
   and "_ht_pre_ref2va.pop(i)" in src)
ck("hand_tie and hand_tie_mc route Hops 2+ through _ht_mc_inpaint_head (v3)",
   '"pin_mech": (["auto", "motion_context", "addguide", "hand_tie", "hand_tie_mc"]' in src
   and 'ht_mc = (str(pin_mech) in ("hand_tie", "hand_tie_mc")) and not swap' in src
   and 'hop_payload["ht_mc"] = "v3"' in src
   and "_ht_mc_inpaint_head(" in src)
_ch_txt, _ch_lay = HT.compile_previs([{"beat": "@a walks."}, {"beat": "@a stops."}], [192, 192])
_ch_out = HT.chain_previs_text(_ch_txt, [{"beat": "@a walks."}, {"beat": "@a stops."}], _ch_lay)
ck("chain_previs_text clocks Window 0 (00:00.000-00:00.917) with carry_text on chained Previs 2+",
   "At 00:00.000-00:00.917, @a carries on naturally with that motion" in _ch_out
   and "At 00:00.917-00:02.833, @a walks." in _ch_out, _ch_out)
_test_wav = torch.ones((1, 2, 16000 * 4), dtype=torch.float32) * 0.010
_test_wav[:, :, 16000:24000] = 0.180  # simulated hallucinated speech burst on a quiet hop
_calmed_q = HT.calm_hop_audio(_test_wav, 16000, has_line=False)
ck("calm_hop_audio suppresses hallucinated speech bursts on non-speaking hops",
   float(_calmed_q[:, :, 16000:24000].pow(2).mean().sqrt()) < 0.025,
   float(_calmed_q[:, :, 16000:24000].pow(2).mean().sqrt()))
ck("locked or replayed sections recover their cached previs via ht_previs_key / window digests",
   "_sec_has_kept = any(" in src
   and '"ht_previs_key": ht_previs_keys.get(ht_pos[i][0])' in src
   and 'hop_store.set_pointer(f"__previs_{_si + 1}", _p_key)' in src)

print("\nall ok" if not FAILS else "\n%d FAILED: %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
