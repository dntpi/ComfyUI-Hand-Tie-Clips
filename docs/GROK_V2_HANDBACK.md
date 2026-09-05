# Hand Tie Clips v2 — handback

**Claude: start at `GROK_TO_CLAUDE.md` (Desktop and `docs/`). This file is
the writing record.** Remaining GPU work is tests **3–5**.

Branch `v2` at `D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips`. Committed,
**unpushed**. Head **`d0ba7af`**. `check_all.py`: 22 checks, all green after
the xfade commit. Grok never pushed, never published.

GPU plan: `GROK_V2_GPU_TESTS.md`. **Tests 1 and 2 passed 2026-09-05.**
Tests 3–5 are yours. This graph is Mia / podcast at 576p, not the tester
couch chain.

---

## 1. Commit list (v2 after `tone-anchor-lab` `774cadb`)

```
2130baa Merge docs/voice-and-dials into v2
6150e60 Merge cache-key-batch into v2
9c35cc1 Merge latent-sidecar-safetensors into v2
f67758d Merge master-spill into v2
4bd40c9 Merge texture-lab-findings into v2
25df349 Merge segment-reanchor into v2
5ab0db5 Merge media-3x3 into v2
52a1c78 Merge ref-mp-ceiling into v2
3721260 Add master_audio_file: lock every hop to one take
7b313c2 Add last_frame_guide: opt-in last-pixel AddGuide
ddd0fa2 Write full length on restart hops, no overlap trim
3bb5d7f Wire shot.refs: choose which stills ride a hop
c7ece81 2.0.0: version, changelog, README against the code
d140c0d Xfade locked hop-1 [C,T] against hop-2 [B,C,T]
d0ba7af DEVLOG: GPU tests 1 and 2 passed
```

`texture-lab` was not merged. DEVLOG is monotonic 8–62. Next free section **63**.

---

## 2. Per phase

### Phase 1 — v2 trunk

**Built.** Branched `v2` off `tone-anchor-lab`. Merged the eight real
branches in the documented order, then `ref-mp-ceiling`. DEVLOG
renumbered through §55 at merge, then §56–60 from later phases.
`media-3x3` extra widgets were appended **after** `pin_mech` /
`tone_anchor_ref` so v2 workflows keep those last-two values.

**Could not verify.** Whether three voices / three clips behave on a
GPU, and what three reference blocks cost per step.

### Phase 2 — `master_audio_file`

**Built.** `audio_lock.py` (no ComfyUI imports) + splice in `run()` +
MEDIA slot + both workflows `""` + `tools/check_audio_lock.py`. Empty
string does not enter the lock and does not add a `chain_salt` field.

**GPU 2026-09-05.** Test 1: hop 1 `[0.00s-8.00s]`, hop 2 `[7.08s-15.08s]`,
passthrough `[0.00s-15.08s]`, join after the xfade fix. Test 2: empty
slot, generated voice heard. Sampler honoured the freeze enough for the
take to be the delivered sound; empty is off.

**Still open:** beat `/ <d>` vs the take (Fail C — wooden mouth,
correct sound). Latent sidecar still will not pickle this NestedTensor
(`not representable without pickling`); hops render, later hits fall
back to the pixel pin.

### Phase 3 — last-frame guide

**Built.** Combo `off` / `still`, default `off`, widget last, JS in the
join & pin group, AddGuide `frame_idx=-1` (last **pixel** frame), per-hop
key only when not off, queue-fail without a start image.

**The latent-T trap.** AddGuide's index is pixel frames. `FRAME_PER_TOKEN`
is `(1,4,4,4,4)`. An 8 s hop is 192 px / latent T=57; `latent_T-1` is
pixel 56, about 2.3 s in, not the end. `_last_pixel_guide_idx` returns
`-1`. This is the thing that was easy to get wrong and that a GPU would
not have made obvious (a pin 2 s in still "does something").

**Could not verify.** Whether the DiT treats last-pixel AddGuide as a
bound; whether `-1` is the last *decoded* frame; chroma pulse at the
still/render gap.

### Phase 4a — restart overlap trim

**Built.** Write path keys on `hop_is_start`, not `i==0`. Preallocation
`master_frame_count`: 9×192 / 22 overlap / restarts at hops 4 and 7 =
**1596**, not 1552. Audio-lock window follows the master head, so a
restart does not lock lips 0.9 s early. `overlap` leaves the hop key on
a restart.

**Near miss.** I first named the start-flag list `start_at`, which is
already `render_from`. Caught in the same edit; the checker asserts
`hop_starts`.

**Could not verify.** 40 ms audio xfade at a restart cut. Length itself
is arithmetic and is checked; hearing the click is not.

### Phase 4b — shot.refs

**Built.** Omitted = register default. `[]` = none. A list = those tags
in that order. Editor round-trips `[]`. Unknown tags fail on the queue.
Default is current behaviour on purpose: dropping identity stills on
restart was the old accident and is not obviously right.

**Could not verify.** Whether `refs: []` on hop 4 keeps the couch. The
diagnosis is from pictures, not from this code path.

### Phase 5 — 2.0.0

**Built.** `pyproject.toml` 2.0.0, editor `VERSION` v2.0.0, `CHANGELOG.md`,
README against the code. Stale claims fixed: "no shot-level refs",
`join: cut` in an example, "four nodes" (there are five in search),
console `v1.5.0` (JS was `v1.5.3`, now `v2.0.0`), MEDIA strip still
describing one clip and one voice. `HANDOVER_*.md` not rewritten;
`BETA_NOTES.md` marked superseded. `CLAUDE.md` shot.refs paragraph
updated so the authority file does not say the field is dead.

**Could not verify.** Registry listing, Manager install, a live
`comfy node publish`. Not asked, not done.

---

## 3. Decisions taken instead of asking

### The seven audio questions

1. **0-based hop index.** Hop 0 at t=0. Stride = hop_frames − overlap_frames
   when every hop after 0 trims. Failure if 1-based: lips lead by one hop,
   obvious at hop 2.
2. **noise_mask 1=denoise video, 0=freeze audio.** Follows `song_lock.py`.
   Asserted. Failure if inverted: "lip-sync died and she stopped moving".
3. **Pad with zeros at 32 kHz** if the take runs out. ElevenLabs has no
   room tone to steal. Failure: mute last hop, not a wrong voice.
4. **Splice AFTER pin, BEFORE sampler.** Failure if first: hop 2+ window
   is the previous hop's tail.
5. **Freeze covers the full hop including overlap.** Unlocking the overlap
   would mix generated voice into a locked hop.
6. **No auto-inject transcript into beats.** The beat already has `<d>`.
   A TSV cutter is a second clock. Failure: wooden mouth, correct sound
   — recoverable.
7. **Mono duplicated to stereo.** song_lock behaviour. Cannot invent a
   second channel of content.

**After Phase 4:** the uniform stride is wrong the moment a restart
writes full length. Window then starts at the master head for a start
hop, and overlap_frames before the master head for a continuation.
Leaving the uniform stride in place would lock lips 0.9 s early per
restart, compounding. That was not one of the seven; it is the eighth.

**Empty `master_audio_file`:** digest omitted from `chain_salt`. A None
field would bust every existing cache key.

### Other decisions

- **Last-frame index is pixel `-1`, not latent T-1.** See Phase 3. Not
  asked; the brief said "last latent index" and that would have been
  wrong.
- **Last-frame guide does not become next hop's frame 0.** Conservative
  half, as specified.
- **shot.refs default = omitted = current behaviour**, not "drop stills
  on restart". Empty list is the drop. Why: the old drop was an accident;
  a restart that needs the face still needs the face.
- **Restart audio xfade is the same 40 ms helper as a continuation.**
  A hard concat might click. Unheard.
- **media-3x3 widgets after pin_mech / tone_anchor_ref**, not before.
  Positional `widgets_values`.

---

## 4. Falsification evidence

Each new checker was run against `HEAD` of the file it guards (stash
the implementation, run, restore).

| checker | without the fix | with the fix |
|---|---|---|
| `check_audio_lock.py` | 1-based window: 27 FAIL on the hand-computed table (Phase 2 session). Empty digest and polarity also asserted. | ALL PASS |
| `check_last_frame_guide.py` | `git stash -- h3_ref_chain.py`: **19 FAIL** (widget missing, helpers missing, workflows unmapped). | ALL PASS |
| `check_restart_trim.py` | stash `h3_ref_chain.py` + `audio_lock.py`: **12 FAIL** (`master_frame_count` missing; `lengths=` TypeError on the old window; write path still `i==0`). Uniform hop 3 at 510/24 still passed — that is the old function. | ALL PASS |
| `check_shot_refs.py` | stash `plan.py` + `refs.py` + `h3_ref_chain.py` + `plan_editor.js`: **KeyError `refs`** then FAIL_EXIT=1 (field not on the shot; `{"refs":[]}` would have been "unknown field"). | ALL PASS |

`check_all.py` after every behaviour commit: 19 → 20 → 21 → 22 checks,
all green.

---

## 5. Where this brief was wrong, or incomplete

- **DEVLOG has no §51 on `tone-anchor-lab`.** Lighting-response lives in
  `BETA_NOTES.md`. (Carried from review 2.)
- **"Last latent index"** for AddGuide is the wrong unit. Core
  `frame_idx` is pixel frames. See Phase 3. I did not build what the
  sentence said; I built what Core does.
- **Audio window as `hop_index * stride`** is only true when every hop
  after 0 trims. Phase 4 restarts break it. The brief's Phase 2 table
  is still the no-restart case and is still checked.
- **`start_at` is `render_from`.** A restart-flags list must not reuse
  that name.
- **README said there is no shot-level `refs` field**, and `join: cut`
  in an example, and four nodes, and editor `v1.5.0`. All contradicted
  the code. Fixed in the 2.0.0 commit.
- **CLAUDE.md said shot.refs is parsed and not consumed.** That was
  the "wire it up or drop it" fork. Wired. CLAUDE.md updated so it
  does not outrank the code with a stale sentence.
- **Widget count.** Brief said 50 at start of work. After media-3x3,
  master_audio, last_frame_guide it is higher; `check_workflows.py`
  is the authority, not the brief's number.
- **Identity stills on restart hops already ride** in current
  behaviour (`hop_is_start` skips the "stills stay off this continue"
  filter). The hop-4 portrait is that default, not a silent drop. The
  drop was the *older* accident, before restart-is-a-start.

---

## 6. What I would not ship, if anything

This goes to strangers. I would ship **2.0.0 with the GPU tests 1 and 2
run**, and I would not ship audio lock as a advertised feature if test 1
is Fail A or B.

Specifically:

- **Ship** empty-default `master_audio_file` only after test 2 (empty
  is really off). The widget can exist either way; claiming "one take,
  every hop" in the README without test 1 is the thing I would not do.
- **Ship** `last_frame_guide` default `off` even if test 5 never runs.
  Opt-in, off, named untested. If test 5 Fail C (hop 2 opens on the
  still), strip `still` before tag. If Fail B (does nothing), leave
  it, say so.
- **Ship** the restart trim. The length arithmetic is checked. The
  unheard 40 ms xfade is the same helper already on every continuation.
- **Ship** `shot.refs` omitted-default. I would not ship a silent drop
  of identity stills on restart as a "fix" for hop 4.
- **I would not ship** a 9-hop "v2 is fine" from this window. Texture
  ratchet is still the README limit (3–5 hops). `texture-lab` stayed
  scratch. Tests 1–2 used 2 hops at 576p.

Tests 1 and 2 **did** happen. Audio lock may be advertised. Leave
`last_frame_guide` default off until test 5. Leave restart trim as
shipped; test 4 is the length check.

---

## 7. GPU tests

`GROK_V2_GPU_TESTS.md` (Desktop and `docs/`). Tests 1–2 done. Claude
runs 3–5. Pickup: `GROK_TO_CLAUDE.md`.
