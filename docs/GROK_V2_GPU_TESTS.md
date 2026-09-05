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
| 3 | restart hop 4, new seed | **not run — do this next** |
| 4 | restart master length | **not run — log-only if 3 ran** |
| 5 | last-frame guide `still` | **not run — lowest priority** |

Do not re-run 1 or 2 unless the tree moved. Remaining window: **3, then 4, then 5**. Nothing needs more than 4 hops. Do not start a 9-hop chain. The take on disk is 26 s; a long chain would pad mute.

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
