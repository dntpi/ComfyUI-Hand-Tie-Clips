# Hand Tie Clips v2 — GPU test plan

For Claude and the human. Branch `v2` at
`D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips`, branch `v2` unpushed. Do not
push. Do not restart ComfyUI unless Python changed: startup **and**
shutdown `rmtree` temp/, which is the hop cache.

Log: `D:\ComfyUI\user\comfyui.log`. Force a re-run if ComfyUI serves a
cached graph with no `[HandTieClips]` lines: nudge `cache_budget_gb` by 1.

**This machine's graph is not the tester couch chain.** It is Mia at a
podcast desk, 576p 9:16, 8 steps `lcm`/`sgm_uniform`. Pass/fail below is
written for *that* graph. The tester-package "couch / identity portrait"
wording is the *kind* of failure, not the room they are in.

Defaults unless a test says otherwise: `quality=final`,
`last_frame_guide=off`, `master_audio_file=""` (empty), `pin_mech=auto`,
`tone_anchor_ref=hop1`, `cache_hops=on`.

---

## Status 2026-09-05

| # | test | result |
|---|---|---|
| 1 | audio lock on, 2 hops | **PASS.** Hop 1 `[0.00s-8.00s]`, hop 2 `[7.08s-15.08s]`, passthrough `[0.00s-15.08s]`, drift +0 ms. After xfade fix `d140c0d` + ComfyUI restart. |
| 2 | lock off (empty MEDIA) | **PASS.** No `audio locked`, generated voice heard, drift −40 ms. |
| 3 | restart hop 4, new seed | **PASS.** `chain_00094_`, 0.70 MP, seed 424242. Hop 4 (22.17-30.17 s) opens at the desk, mid-hop she is holding the mic in the same room, ends at the desk. `shot_probe`: edges 3.2-4.4, colours 1979-2483 across the whole hop, no collapse. The tester's hop-4 portrait does NOT reproduce. |
| 4 | restart master length | **PASS.** Log: `4 hops x 192f overlap 22 -> 724 frames (30.2s)` and `master length 724f (2 chain start(s), 2 overlap trim(s); old formula would have been 702f)`. |
| 5 | last-frame guide `still` | **PASS, with a measured cost.** `chain_00095_`, native 768x1344. The restart cut becomes a match cut -- hop 3 arrives at the still's framing instead of snapping to it. Cost: all four hop ENDINGS converge, 3.8/255 apart against 39.1/255 unguided, while mid-hop frames stay as varied as ever (65.8 vs 61.1). Two viewers watching in motion could not see it. |
| - | fp16 master | **PASS, live.** `master buffer: 3.0 GB spilled` at 0.70 MP and `4.2 GB` at native -- both fp16; fp32 would read double. SaveVideo accepted the fp16 IMAGE, which was the one thing offline work could not settle. |

Do not re-run 1-5 unless the tree moved.

### Reading test 5 correctly

The 3.8/255 number is real and my first reading of it was wrong. Four hop-ENDING
frames in a grid is the presentation that makes convergence obvious and motion
invisible; a hop's last frame is passed through in 1/24 s and the next hop
continues straight out of it. The user and the tester both watched it and saw
nothing. **Ship `last_frame_guide` as-is, off by default.**

What the number does support is a prediction, not a worry about that clip: the
run had CAMERA / FRAMING / PACE all unset, so nothing competed with the still.
The guide plants the photograph at `frame_idx=-1` on EVERY hop, so a shot
authored `framing: close` should be overridden at its own ending. That is the
open question test 6 exists for.

### False positive worth knowing about

`shot_probe` flagged 3.00-3.50 s on both runs. It is a push-in -- she fills the
frame and the background goes to bokeh -- not a set change. The shape is the
tell: a gradual 2.5 s descent and 3.5 s recovery, where a real cutaway is a
cliff (the tester's went 5.58 -> 1.59 in 1.5 s and snapped back instantly).
This desk set has median edges 3.85% against her 6.35%, so the dynamic range is
compressed and a lens move gets near the threshold. Left untuned deliberately:
a probe that never false-positives on a sparse set would miss a real event on
one.

## 6. Authored framing against the guide  (running)

**Proves:** whether an authored `framing` directive outvotes the last-frame
guide, or the guide overrides it.

Identical to test 5 -- native, same seeds, `last_frame_guide = still` -- except
shots 2 and 3 are authored to fight the still, which is a WIDE
(`004128_00001__3.jpg`: full body, desk, chair, plant).

- shot 2: `join continuous, camera push_in, framing close, pace slow`
- shot 3: `join continuous, camera hold, framing close`

`camera: push_in` is load-bearing: the planner warns when a framing change rides
a continuous join with no camera move to justify it. `push_in`+`close` is legal;
only `push_in`+`wide` and `pull_back`+`close` are rejected as contradictory.

**What to look at:** the last second of shots 2 and 3.

- **Ends close** -- the directive wins, the guide only nudges, no README
  constraint needed.
- **Ends wide** -- the guide overrides authored framing at every hop ending.
  README constraint, and the per-shot form moves from nice-to-have to needed.
- **Pops wide in the final frames** -- worst for a viewer, and the most likely
  honest outcome.

In `chain_00095` those two hops ended wide with NOTHING authored, so ending wide
again with `close` on both is decisive rather than ambiguous.

---

## 3. Restart hop 4, new seed  (next)

**Proves:** a restart stays in the shot. Hop 4 of the tester chain opened
on the couch then visited the identity still; we must not inherit that
seed (`…458`) and we must not debug the wrong start image.

**Cost:** 4 hops × 8 s × 8 steps. Abort after hop 4's first 3 seconds if
it already left the desk.

**Setup**

1. Same graph as tests 1–2 (Mia / podcast). `duration` = `8 s`. `steps` = `8`.
2. `start_image_file` set to the **shot's opening still** (the desk /
   podcast frame, not a face crop). Required for a restart.
3. Add shots 3 and 4 if the plan is still 2 hops. Shot 4:
   `"anchor": "restart"`, `"directives": {"join": "hard_cut"}`.
   Beat names the **desk / microphone / room**, not a portrait sitting.
4. Shot 4 `"seed"` = something other than `991122334458`. e.g. `424242`.
5. `last_frame_guide` stays `off`. Shot 4 `refs` stays **omitted**
   (register default — identity stills still ride).
6. `render_through` = `4`. `master_audio_file` empty (the take is only
   26 s; 4×8 s with a restart is longer than the take and is not this
   test).

**What to look at**

- Log, hop 4: `ANCHOR RESTART -- start image is frame 0`.
- Log, hop 4: `restart, wrote all 192 frames (no overlap trim)`.
- First 1.5 s of hop 4: she is at the desk, not a face on seamless /
  the identity still (`cafe_floral_face.jpg`).
- Seconds 2–6 of hop 4: does she stay in the room, or cut away to that
  still?

**Pass:** opens on the desk **and stays**.

**Fail A — opens on the identity still:** wrong start image. Check
`start_image_file` is the room/desk, not the face crop.

**Fail B — opens on the desk, wanders to the identity still mid-hop:**
the known attractor. **Not a v2 regression.** Why `last_frame_guide` and
`shot.refs` exist. Optionally test 5, or set shot 4 `refs` to `none`
and re-queue hop 4 only (`render_from=4`, `render_through=4`, cache on).

**Abort at:** hop 4 preview, first 3 seconds.

---

## 4. Restart overlap trim — length  (log only if 3 ran)

**Proves:** the master is the right duration. A restart used to drop 22
frames (0.9 s).

**Cost:** if test 3 ran, **read the log**. No extra render.

**What to look at** (log only)

Four hops × 192, one restart at hop 4: n_trims = 2 (hops 2 and 3),
total = 192+170+170+192 = **724**. Old formula would have been 702
(`4*192 - 22*3`).

- At start: `master length 724f (2 chain start(s), 2 overlap trim(s); old
  formula would have been 702f)` — numbers must match this arithmetic.
  The "old formula would have been" aside is not the actual length.
- Hop 4: `restart, wrote all 192 frames (no overlap trim)`.
  **Fail** if hop 4 still says `dropped 22 frames`.
- End: wrote == planned (724 of 724). `note: wrote X of Y planned` with
  X ≠ Y is a fail.
- Preview duration ≈ 724/24 = **30.17 s**.

**Pass:** start line, hop-4 "wrote all", wrote == planned.

**Abort at:** the master-length line before hop 1 samples.

---

## 5. Last-frame guide on  (lowest priority)

**Proves:** `still` pins the end of the hop to `start_image`. Opt-in,
default off, so a bad result costs nothing shipped.

**Cost:** 2 hops × 8 s × 8 steps. Only if 3 (and 4) landed.

**Setup**

1. Back to 2 hops, `render_through=2`.
2. `start_image_file` = the desk/room still.
3. RUN → join & pin → `last_frame_guide` = `still`.
4. `master_audio_file` empty.

**What to look at**

- Log every hop: `last-frame guide (still at pixel frame_idx=-1)`.
- Last 0.5 s of hop 1 and hop 2: pulled toward that still. A chroma gap
  can pulse — named in the tooltip. Ugly pulse = known cost, not a crash.
- Hop 2 frame 0 is **not** the still. It is the continuation pin.

**Pass:** last frames resemble the still more than an `off` run of the
same seed; hop 2 still continues from hop 1.

**Fail A — queue error about no start image:** `start_image_file` empty.
Expected.

**Fail B — last frames ignore the still:** DiT did not treat last-pixel
AddGuide as a bound. **Leave default `off`.** Widget can still ship.

**Fail C — hop 2 opens on the still:** we accidentally made it next hop's
frame 0. **Do not ship `still`.** Default `off` is still safe.

**Abort at:** hop 1's last second in the preview.

---

## Optional extra, only after 3 Fail B

Shot 4 advanced → refs → type `none`. Re-queue hop 4 only
(`render_from=4`, `render_through=4`, `cache_hops=on`). If she stays at
the desk, the attractor was the identity stills.

---

## Already done (do not re-run)

### 1. Audio lock — PASS

Hop 1 `audio locked [0.00s-8.00s]`. Hop 2 `audio locked [7.08s-15.08s]`.
`final audio: passthrough of master_audio_file [0.00s-15.08s]`. Drift +0 ms.
Take: the Sakura ElevenLabs file under `h3_refs` (26.36 s). First attempt
died at `_xfade_audio` (`got 2 and 3`); `d140c0d` + restart fixed it.

Fail C (mouth vs take words) was **not** scored — beats were not matched
to the take.

### 2. Empty master_audio_file — PASS

Same graph, MEDIA master audio empty. No `master_audio_file: loaded`, no
`audio locked`, no passthrough. Generated voice heard. Drift −40 ms.
Cache miss vs the locked run is expected (digest in `chain_salt`).
