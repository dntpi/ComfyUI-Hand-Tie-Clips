# Pickup for Claude — Hand Tie Clips v2

Grok finished the **writing** of v2. You are taking over **review + remaining
GPU tests**. The human continues the GPU window with you. Do not push. Do
not publish. Do not zip.

Read this file first, then `GROK_V2_GPU_TESTS.md` (same folder, remaining
tests 3–5). Engineering detail: `GROK_V2_HANDBACK.md`. Repo authority:
`CLAUDE.md` (outranks this). Copies also live in the pack under `docs/`.

---

## Where the tree is

- Repo: `D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips`
- Branch: **`v2`**, unpushed
- Head: **`d0ba7af`** (`DEVLOG: GPU tests 1 and 2 passed`)
- Parent writing: `d140c0d` xfade rank fix, `c7ece81` 2.0.0 bump
- Python: `D:\ComfyUI\venv\Scripts\python.exe`
- Checkers: `D:\ComfyUI\venv\Scripts\python.exe tools\check_all.py` (22, all
  green after the xfade commit)
- ComfyUI log: `D:\ComfyUI\user\comfyui.log`
- ComfyUI was restarted after `d140c0d`. Python on disk is loaded.

`texture-lab` was **not** merged. Do not merge it.

---

## GPU status (2026-09-05, this machine)

This is **not** the tester's 9-hop couch chain. The live graph is Mia at a
podcast desk, 576p 9:16, 8 steps `lcm`/`sgm_uniform`, 2 hops × 8 s, overlap
0.9 s, `cache_hops=on`, budget 19 GB.

Take on disk (26.36 s, stereo, 44100 Hz):

`ElevenLabs_2026-09-05T10_38_22_Sakura - Sweet, Gentle_pvc_sp100_s50_sb75_se49_b_m2.mp3`

under `ComfyUI/input/h3_refs/`.

| test | result |
|---|---|
| 1 audio lock on | **PASS.** Hop 1 `[0.00s-8.00s]`, hop 2 `[7.08s-15.08s]`, passthrough `[0.00s-15.08s]`, drift +0 ms. First attempt crashed at hop-2 xfade (2-D take vs 3-D hop audio). Fixed `d140c0d`, ComfyUI restarted, second attempt joined. |
| 2 lock off | **PASS.** Empty MEDIA slot. No `master_audio_file: loaded`, no `audio locked`, no passthrough. Generated voice **heard**. Drift −40 ms (generated xfade). |
| 3 restart hop 4 | **not run** |
| 4 restart master length | **not run** (log-only if 3 runs) |
| 5 last-frame guide | **not run** |

Fail C (wooden mouth, correct take) was not scored. Beats were not rewritten
to match the take's words.

**Latent sidecar (not a test-1/2 blocker):** every hop logs
`latent not cached (ValueError('latent is not representable without pickling
(unrecognised samples container, or a non-scalar non-tensor member)'))`.
Hops still render. A later cache hit will fall back to the AddGuide pixel
pin. Motion-Context still ran on hop 2 of these tests because the live
sampler latent was in RAM. Worth a look after 3–5, not instead of them.

---

## What you do next

1. **GPU tests 3, then 4, then 5** — verbatim in
   `C:\Users\User\Desktop\GROK_V2_GPU_TESTS.md`. Adapted to *this* graph
   (desk/podcast, not the tester couch). If the window is short: **3 and 4**.
   4 is a log read if 3 already ran.
2. Record results in that file and in `docs/DEVLOG.md` (next free section
   is **63**).
3. Do not push, tag, or `comfy node publish` unless the human asks.

If you write code: `check_all.py` green before every commit; one behaviour
per commit; append widgets last; hop-2+ levers in the per-hop key not
`chain_salt`; restart hops pass `None` prev_key; Core calls keyword-only
via `_core_call`; no pickle; no `texture-lab`; do not rewrite `tone.py`
seam guarantee.

---

## Hard facts that already bit this session

- **AddGuide `frame_idx` is pixel frames.** `FRAME_PER_TOKEN` is
  `(1,4,4,4,4)`. `latent_T-1` on an 8 s hop is ~2.3 s in. Last-frame guide
  uses `-1`.
- **`start_at` is `render_from`.** Restart flags are `hop_starts`.
- **A restart hop writes full length.** 4×192, restart at hop 4 → **724**
  frames, not 702. Log: `restart, wrote all 192 frames (no overlap trim)`.
- **Python changes need a ComfyUI process restart.** Browser refresh is JS
  only. Restart wipes `temp/` (hop cache).
- **Log line numbers in a traceback tell you if the process reloaded.**
  The xfade `torch.cat` is at **1474** on current `h3_ref_chain.py`. A
  crash at 1457 is the pre-fix process.

---

## Files

| path | what |
|---|---|
| `C:\Users\User\Desktop\GROK_TO_CLAUDE.md` | this file |
| `C:\Users\User\Desktop\GROK_V2_GPU_TESTS.md` | remaining tests, exact widgets |
| `C:\Users\User\Desktop\GROK_V2_HANDBACK.md` | commits, decisions, falsification |
| pack `docs/` | copies of the three, committed on `v2` |
| pack `CLAUDE.md` | authority; live-queue pointer at the end |
| pack `docs/DEVLOG.md` | §§56–62 are this v2 work |
| pack `CHANGELOG.md` | user-facing 2.0.0 |

Original takeover brief (historical): `C:\Users\User\Desktop\GROK_TAKEOVER_v2.md`.
Reviews: `GROK_VIDEO_REVIEW.md`, `GROK_REVIEW_2.md` on the Desktop.
