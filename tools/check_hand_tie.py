r"""Offline checks for hand_tie.py and its wiring in run() -- no GPU, no server.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_hand_tie.py

Hand-tie shares a clean 22-frame window at each join. The window is rendered
from the refs and still 1, never from a neighbour. Hop k ends on that window,
hop k+1 opens on its latent (video only), and both delivered sides are written
with it. What is checked here:

* the text: a still shows the beat's opening and ending with no line, no talking
  cue and no sound; voices leave the sound sentences; closed lips leave a hop
  that speaks; a line sits after the overlap and fits the hop;
* the arithmetic: 22 frames are 7 latent steps, a legal hop is at least 56
  frames, and the tail write is not the head write;
* audio-only keyframes are dropped and a mixed keyframe stays;
* the cut detector on synthetic motion and a synthetic cut;
* the sound check's verdict;
* run(): still 1 is the picture ref, the window latent is cached, the hop key
  does not chain, the end guide is at -22 after the pin, and both writes sit
  before the decode, including the retake.
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


src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
_rs = src.index("    def run(self")
run_src = src[_rs:]

print("the still: how the beat ends, and where")
_beat = ("@carolyn walks down a busy shopping street, holding her phone out. (S1) says, "
         "<d>[English] What's up, everybody.</d> She grins with her lips closed and glances "
         "at the crowd. Traffic hum and footsteps.")
_st = HT.still_text({"beat": _beat})
ck("opening and ending, the person named in place of the pronoun",
   _st == "@carolyn walks down a busy shopping street, holding her phone out. "
          "@carolyn grins with her lips closed and glances at the crowd.", _st)
ck("no line, no speaker tag, no sound sentence in a still",
   "<d>" not in _st and "(S1)" not in _st and "says" not in _st and "Traffic" not in _st)
_taste = ("@carolyn takes a big bite of the skewer, then chews slowly with her lips closed. "
          "She swallows and gives the camera a thumbs up. The crunch of the skewer, traffic hum.")
ck("the taste beat's still is the swallow, not the chewing (the Carolyn run)",
   HT.still_text({"beat": _taste}).endswith("@carolyn swallows and gives the camera a thumbs up."),
   HT.still_text({"beat": _taste}))
ck("a one-sentence beat is its own still",
   HT.still_text({"beat": "@a sits on the bench."}) == "@a sits on the bench.")
_talk = ("@mina walks down the street, holding her phone out and talking into it. She smiles.")
ck("a talking cue does not reach a still (it draws captions and an open mouth)",
   HT.still_text({"beat": _talk}) == "@mina walks down the street, holding her phone out. @mina smiles.",
   HT.still_text({"beat": _talk}))
ck("an ending is a person's action, never the beat's sound",
   HT._end_sentence("@a walks. He sits. Traffic hum, footsteps.") == "@a sits."
   and HT._end_sentence("@a walks. Traffic hum, footsteps.") == ""
   and HT._end_sentence("@a walks.") == "")
ck("a still's seed is the run seed's plus its offset, in range",
   HT.still_seed(3001) == (3001 + HT.STILL_SEED_OFFSET) and HT.still_seed(0x7FFFFFFF) >= 0)

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
_qs = HT.quiet_sounds([{"beat": "@a walks. She smiles. Traffic hum, the vendor calling out orders."},
                       {"beat": "@a walks. She talks to the vendor."}])
ck("a beat loses the voice from its sound sentence only",
   _qs[0]["beat"] == "@a walks. She smiles. Traffic hum." and _qs[1]["beat"] == "@a walks. She talks to the vendor.",
   [q["beat"] for q in _qs])
_lv = [w for w in HT.line_lints([{"beat": "@a walks. She smiles. Traffic hum, crowd chatter."}], [192])
       if "names a voice" in w]
ck("the lint names the voice it leaves out", len(_lv) == 1 and "crowd chatter" in _lv[0], _lv)

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
_rules_src = open(os.path.join(HERE, "planner.py"), encoding="utf-8").read()
_rules = _rules_src[_rules_src.find("def hand_tie_rules"):_rules_src.find("# A music video renders over the song")]
ck("the writer's hand-tie rules ask for no closed lips and describe the storyboard",
   "def hand_tie_rules" in _rules and "closed-mouth" not in _rules and "lips closed" not in _rules
   and "ends on a still" in _rules and "preview render" not in _rules)

print("\na hop's line sits after the overlap and fits the hop")
ck("8 s fits 14 words (LINE_WPS 2.4 over 8 s less the overlap and the last second)",
   HT.line_budget(192) == 14, HT.line_budget(192))
_w14 = " ".join("w%d" % k for k in range(14))
_tl = HT.time_lines([{"beat": "@a walks. @a says, <d>[English] %s</d> She smiles. Footsteps." % _w14}],
                    hop_frames=[192])[0]["beat"]
ck("the opening at 0, the line at 1 s, the sentence after it where a 3 wps line ends",
   _tl.startswith("At 00:00.000 @a walks. At 00:01.000 @a says,") and "At 00:05.700 She smiles." in _tl, _tl)
_long = " ".join("w%d" % k for k in range(30))
ck("an over-long line is linted, with no closed-mouth advice",
   any("speaks 30 words" in w and "closed-mouth" not in w
       for w in HT.line_lints([{"beat": "@a says, <d>[English] %s</d>" % _long}], [192])))
ck("a line and a bite in one hop is linted (00153/00154/00160)",
   any("bite" in w for w in HT.line_lints([{"beat": "@a takes a bite. @a says, <d>[English] Good.</d>"}], [192])))

print("\nthe sound check")
_blk = ("detailed_description: At 00:01.000 <Subject 1> says, <d>[English] What's up, "
        "everybody, welcome back.</d> Footsteps. <d>[English] Okay here<|cutoff|></d>")
_line = HT.hop_words(_blk)
ck("a hop's planned words: every line, the language tag and <|cutoff|> left out",
   _line == ["what's", "up", "everybody", "welcome", "back", "okay", "here"], _line)
ck("a quiet hop plans no words", HT.hop_words("@a bites the skewer. Crunching.") == [])
ck("[English] or no tag is 'en'; another language is detected",
   HT.hop_language(_blk) == "en" and HT.hop_language("<d>hi</d>") == "en"
   and HT.hop_language("<d>[Japanese] konnichiwa</d>") is None)
ck("the line, said: passes", HT.judge_sound(_line, " What's up everybody, welcome back. Okay, here.")[0])
ck("most of the line plus a stray word: passes",
   HT.judge_sound(_line, "What's up, everybody! Welcome back, okay?")[0])
ck("a different sentence (00165 hop 4's 'quick brown fox'): fails",
   not HT.judge_sound(_line, "makes a man please point up every time the quick brown fox jump")[0])
ck("the line buried in invented speech: fails",
   not HT.judge_sound(_line, "What's up everybody welcome back okay here and then the man said "
                             "we should go down to the river before the rain comes")[0])
ck("nothing said on a hop with a line: fails", not HT.judge_sound(_line, "")[0])
ck("a quiet hop with a hum Whisper reads as 'Thank you.': passes", HT.judge_sound([], " Thank you.")[0])
ck("a quiet hop that talks: fails", not HT.judge_sound([], "so this is the best one on the street")[0])
_hs = open(os.path.join(HERE, "hear.py"), encoding="utf-8").read()
ck("hear.py never downloads: local cache only, on the CPU",
   "local_files_only=True" in _hs and "device=-1" in _hs and "snapshot_download(repo," in _hs)

print("\nthe join: 22 frames are 7 latent steps, written at the head or the tail")


def _latent_t(f):   # core's video_latent_t (comfy_extras/nodes_minimax_h3.py), restated
    return 2 if f <= 5 else ((f - 5) // 17) * 5 + 2


ck("22 frames are 7 steps; 5 are 2; 39 are 12 (core's arithmetic)",
   [HT.overlap_rows(f) for f in (22, 5, 39)] == [7, 2, 12] == [_latent_t(f) for f in (22, 5, 39)])
ck("a legal hop's last 7 steps open on a key token",
   all((_latent_t(f) - 7) % 5 == 0 for f in (56, 124, 192, 243, 362)))
ck("that window is not the hop's first step, and step 1 is off the token grid",
   all((_latent_t(f) - 7) != 0 and (1 % 5) != 0 for f in (56, 124, 192)))
ck("a hop under 56 frames is refused, and 56 is not",
   HT.hop_fit_errors([39]) and HT.hop_fit_errors([56, 124]) == []
   and not HT.hop_fit_errors([56]))
ck("the short-hop error names the hop and the minimum",
   "hop 2" in HT.hop_fit_errors([124, 39])[0] and "56" in HT.hop_fit_errors([39])[0])
_prev = torch.randn(1, 24, 57, 4, 6)
_new = torch.randn(1, 24, 57, 4, 6)
_wb = HT.write_back(_new, _prev, 7)
ck("write_back puts the previous hop's last 7 steps at this hop's start, exactly",
   torch.equal(_wb[:, :, :7], _prev[:, :, -7:]) and torch.equal(_wb[:, :, 7:], _new[:, :, 7:]))
ck("...and leaves its input alone", not torch.equal(_new[:, :, :7], _prev[:, :, -7:]))
ck("a canvas mismatch or no rows: unchanged (never a wrong splice)",
   HT.write_back(_new, torch.randn(1, 24, 57, 4, 8), 7) is _new and HT.write_back(_new, _prev, 0) is _new)
_tail = HT.write_rows(_new, _prev, 7, tail=True)
ck("the tail write puts the window's first 7 steps at this hop's end",
   torch.equal(_tail[:, :, -7:], _prev[:, :, :7]) and torch.equal(_tail[:, :, :-7], _new[:, :, :-7]))
ck("the head write is not the tail write",
   not torch.equal(HT.write_rows(_new, _prev, 7, tail=False)[:, :, :7], _prev[:, :, :7]))
_short = torch.randn(1, 24, 3, 4, 6)
ck("a short window, or a hop no longer than it, is returned unchanged",
   HT.write_rows(_new, _short, 7, tail=True) is _new
   and HT.write_rows(_short, _prev, 7) is _short)
ck("audio-only keyframes are dropped and a mixed one stays",
   HT.video_only_keyframes([
       {"audio_latent": 1}, {"latent": 2, "audio_latent": 3}, {"latent": 4},
   ]) == [{"latent": 2, "audio_latent": 3}, {"latent": 4}])
ck("no keyframes passes through",
   HT.video_only_keyframes(None) is None and HT.video_only_keyframes([]) == [])

print("\nthe cut detector (a hop that cuts to reach its still)")
g = torch.Generator().manual_seed(5)
scene_a = torch.rand(1, 3, 6, 4, generator=g)
scene_b = torch.rand(1, 3, 6, 4, generator=g)
_big = lambda sc: torch.nn.functional.interpolate(sc, size=(96, 64), mode="bilinear",  # noqa: E731
                                                   align_corners=False)[0].permute(1, 2, 0)
_walk = torch.stack([torch.roll(_big(scene_a), shifts=2 * t, dims=1) for t in range(60)])
_walk = _walk + 0.02 * torch.randn(_walk.shape, generator=g)
_cut = _walk.clone()
_cut[30:] = torch.stack([torch.roll(_big(scene_b), shifts=2 * t, dims=1) for t in range(30)])
ck("a handheld walk is not a cut", HT.hard_cuts(_walk) == [], HT.hard_cuts(_walk))
ck("a cut inside the walk is found, at its first new frame", HT.hard_cuts(_cut) == [30], HT.hard_cuts(_cut))

print("\nrefusals")
ck("overlap other than 0.9 s is refused", any("overlap 0.9 s" in e for e in HT.settings_errors(5, "off")))
ck("last_frame_guide is refused (the still is the end guide)",
   any("last_frame_guide" in e for e in HT.settings_errors(22, "before_restart")))
ck("master_audio_file is refused", any("master_audio_file" in e for e in HT.settings_errors(22, "off", "x.wav")))
ck("22 frames, guide off, no master audio: nothing refused", HT.settings_errors(22, "off", "") == [])

print("\nrun(): the window")
_st0 = run_src.find("        def _ht_still(i):")
_st1 = run_src.find("        def _ht_digest(lat):")
_still_src = run_src[_st0:_st1]
ck("_ht_still is defined in run()", _st0 > 0 and _st1 > _st0)
ck("the window is a STILL_FRAMES clip on the hop canvas, and its last frame is kept",
   "s_len = align_frame_count(_ht.STILL_FRAMES)" in _still_src
   and "width=int(width), height=int(height), length=s_len," in _still_src
   and "ht_stills[i] = _si[-1:].clone()" in _still_src)
ck("its text is still_text; its seed the run seed, never a shot's",
   "_ht.still_text(_sh)" in _still_src
   and "s_seed = _ht.still_seed((int(seed) + i) if seed_per_shot else int(seed))" in _still_src
   and '_sh.get("seed")' not in _still_src)
ck("it is muted: frozen silence spliced into its latent, no voice",
   "_sl = _splice_locked_audio(_sl, _encode_locked_slice(" in _still_src and "ref_audios=None" in _still_src)
ck("later windows take still 1, never the previous window",
   "ht_stills.get(0) if i > 0" in _still_src
   and 's_images[f"ref_image_{_n}"] = _anchor' in _still_src
   and '_line = f"<Picture {_n}> ({_ht.STILL_CONTEXT})."' in _still_src
   and "weak_reference" in HT.STILL_CONTEXT
   and "_prev = ht_stills.get(i - 1)" not in run_src)
ck("the window cache stores the sampler latent, and a hit without one re-renders",
   "latent=_win" in _still_src
   and "if _hit is not None and _hit[3] is None:" in _still_src
   and "if _hit is not None and _hit[3] is not None:" in _still_src
   and "_si, _sw = _store.as_stored(_si, _sw)" in _still_src)
ck("it is cached: keyed on its text, refs, seed and length, and served by key",
   "s_key = _store.hop_key(None, {" in _still_src and '"still": sblock' in _still_src
   and "_hit = hop_store.get(s_key)" in _still_src and "hop_store.put(s_key, _si, _sw," in _still_src)
_key_at = run_src.find('hop_payload["hand_tie"] = [')
ck("run() renders the window before the hop key", 
   0 < run_src.find("_still = _ht_still(i) if ht else None") < _key_at)
ck("the key records the two window digests, not the old storyboard still",
   '"window", _open_digest, _ht_digest(ht_latents.get(i)),' in run_src
   and '"hand_tie": ["storyboard"' not in run_src
   and "tensor_digest(_still)" not in run_src)

print("\nrun(): the hop")
ck("a hand-tie hop does not chain prev_key", 
   "None if (hop_restart or ht) else prev_key, hop_payload)" in run_src
   and "None if hop_restart else prev_key, hop_payload)" not in run_src)
ck("the cache meta still records prev_key for the other pin modes",
   '"prev_key": None if hop_restart else prev_key' in run_src)
ck("the opening pin is the previous window, and there is no auto pixel fallback",
   "context_latent=_wlat" in run_src
   and "ht_latents.get(i - 1)" in run_src
   and "_ht.video_only_keyframes(" in run_src
   and 'pin_mech_used = "window"' in run_src
   and 'mode="auto" if ht' not in run_src)
ck("a continuation is the window; a restart opens free",
   'pin_mech_pred = "none" if (i == 0 or hop_restart) else "window"' in run_src)
_eg = run_src.find('MiniMaxH3AddGuide, "the storyboard window",')
_pin = run_src.find("context_latent=_wlat")
_smp = run_src.find('SamplerCustomAdvanced, "the sampler",')
ck("the end guide is the 22-frame window after the pin and before the sampler",
   -1 not in (_eg, _pin, _smp) and _pin < _eg < _smp
   and "frame_idx=-_ht.OVERLAP" in run_src[_eg:_eg + 400]
   and "frame_idx=_last_pixel_guide_idx()" not in run_src[_eg:_eg + 400]
   and "audio=None" in run_src[_eg:_eg + 400])
_wb0 = run_src.find("sampled = _ht_apply_windows(sampled)")
_dec = run_src.find("imgs, audio = _decode_av(vae, audio_vae, sampled)")
ck("the window write runs before the decode, and not after it",
   -1 not in (_wb0, _dec) and _wb0 < _dec
   and run_src.find("sampled = _ht_apply_windows(sampled)", _dec) == -1
   and 'if ht and pin_mech_used == "motion_context" and prev_sampled is not None:' not in run_src)
_rt = run_src.find("_s2 = _ht_apply_windows(_s2)")
_rtd = run_src.find("_i2, _a2 = _decode_av(vae, audio_vae, _s2)")
ck("the cut retake is written before its decode, and uses the cut detector",
   -1 not in (_rt, _rtd) and _rt < _rtd
   and "_s2 = _ht_write_back(_s2)" not in run_src
   and "_hcuts = _ht.hard_cuts(imgs)" in run_src
   and "_c2 = _ht.hard_cuts(_i2)" in run_src and "_ht.HOP_RETRY_STRIDE" in run_src)
_ap = run_src.find("        def _ht_apply_windows(lat):")
_ap_src = run_src[_ap:run_src.find("\n        assembled = []", _ap)]
ck("the write uses overlap_rows, video only, and a restart has no head write",
   "_ht.write_rows(" in _ap_src and "_ht.overlap_rows(overlap_n)" in _ap_src
   and "parts[0] = spliced" in _ap_src and "parts[1]" not in _ap_src
   and "if i > 0 and not hop_restart:" in _ap_src
   and "parts[0] = _ht.write_back(" not in run_src)
ck("a locked hand-tie hop is unpaired on its opening window",
   '_pm.get("window_open") != _open_digest' in run_src
   and '_meta["window_open"] = _open_digest' in run_src)
ck("a short hop and a missing Motion-Context fail the queue",
   "_ht.hop_fit_errors(lengths)" in run_src
   and "there is no pixel-pin fallback." in run_src)
ck("the sound check is a verdict on the kept part, logged and in info",
   "_txt = _hear.transcribe(wav[..., _cut:], sr, _ht.hop_language(block))" in run_src
   and "ht_heard.append((i + 1," in run_src and 'info += chr(10) + f"hop {_h} sound: {_v}"' in run_src)
ck("a hand-tie hop joins like any chain: no window cut, no bend, no deflicker",
   "            elif ht:" not in run_src and "seam_bend" not in run_src and "deflicker" not in run_src
   and "tone_match" not in run_src)
ck("the compile path is time_lines over open_lips over quiet_sounds",
   "_ht.open_lips(_ht.quiet_sounds(shots)), hop_frames=_tl_frames)]" in src)
ck("nothing of the previs or the sound drafts is left in run()",
   not re.search(r"ht_previs|ht_windows|ht_pos\b|_ht_previs|sound_first|compile_previs|"
                 r"hand-tie section|split_crossing|free_head", src))
ck("dry_run=previs outputs the stills, and the clips are dropped once unused",
   'head = (f"STORYBOARD ONLY - {int(_imgs.shape[0])} still(s) at "' in run_src
   and "_imgs = torch.cat([ht_stills[k] for k in sorted(ht_stills)], dim=0)" in run_src
   and run_src.count("ht_clips.pop(i, None)") >= 3)

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
    _quiet = "@mina bites the skewer and chews. Crunching, footsteps."
    ck("a hand-tie hop with a line carries the voice", _scv(True, True, "off", _said))
    ck("a quiet hand-tie hop does not (chain_00038)", not _scv(True, True, "off", _quiet))
    ck("voice_every_hop=on still rides a quiet hand-tie hop", _scv(True, True, "on", _quiet))
    ck("a plain chain start carries it, line or not", _scv(True, False, "off", _quiet))
    ck("a continuation is not a start", not _scv(False, True, "on", _said))

print("\nthe module stays ComfyUI-free")
tree = ast.parse(open(os.path.join(HERE, "hand_tie.py"), encoding="utf-8").read())
mods = {n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [n])}
ck("imports only stdlib + torch", mods <= {"re", "torch", "torch.nn.functional"}, sorted(m for m in mods if m))

print("\nall ok" if not FAILS else "\n%d FAILED: %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
