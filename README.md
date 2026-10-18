---
license: mit
tags:
  - comfyui
  - comfyui-nodes
  - custom-nodes
  - video
  - video-generation
  - text-to-video
  - image-to-video
  - minimax-h3
---

# Hand Tie Clips — Multi-Shot Studio for MiniMax H3

**Multi-hop MiniMax H3 video from one ComfyUI node.** Author shot cards, manage reference stills and voices on a visual rail, lock or solo-retake individual hops from a lossless cache, run guided character/head swaps, and upscale to 2 MP.

![Six consecutive frames across a join](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/seam-frames.png)

*Six consecutive frames crossing the join between Hop 1 and Hop 2 — no cut, flash, or identity jump.*

> 🚀 **[Launch the Interactive Node UI Demo in Your Browser (`docs/demo.html`)](https://dntpi.github.io/ComfyUI-Hand-Tie-Clips/demo.html)** — Click through **Script · Refs · Timeline · Media · Write · Swap** and the **`⚙` (`RUN`)** render-controls page live, toggle shot locks, solo-retake hops, and inspect the 10-hop `hand_tie` setup (compatible with both Classic LiteGraph and ComfyUI Vue Nodes 2.0).

<div align="center">
  <p><strong>Hand-Tie 10x8s example</strong></p>
  <video controls preload="metadata" width="50%" style="max-width:480px; border-radius:8px;" src="https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips/resolve/main/docs/img/hand-tie-10hop-00138.mp4">
    <source src="https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips/resolve/main/docs/img/hand-tie-10hop-00138.mp4" type="video/mp4">
    <source src="https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/hand-tie-10hop-00138.mp4" type="video/mp4">
  </video>
</div>

---

## What You Can Do

| Tab / Surface | What It Does |
|---|---|
| **🎬 SCRIPT** (`Simple` & `Shots`) | Switch to **`Simple`** mode (`chains = 1`) to paste standard **MiniMax H3 `ref2va` / `t2va` 6-field prompts** (passed through untouched!) or plain prose for single-clip generations with **`realism_boost: on`** + **`audio_refine: on`** — or switch to **`Shots`** mode to author per-hop cards (5 s – 15 s each) with camera, framing, pace, and join controls. |
| **🖼️ REFS** (`@tags` & `RefMod`) | Drop up to 9 character/setting stills straight onto the node. Assign stable `@tags`, subject numbers, per-photo megapixel caps, per-hop schedules (`rides hops`), and per-character voice clips without wiring `Load Image` chains. |
| **🎞️ TIMELINE** (Retake Board) | Scrub every rendered hop, inspect the exact join frames, **lock** the shots you want to keep, or click `⏵` on Shot 3 to re-roll *only* Shot 3 while Shots 1–2 load from the lossless cache. |
| **🎵 MEDIA** (Clips, Voices & Lip-Sync) | Load a starting frame, up to 3 reference videos, 3 voice timbre clips, a background music bed (with auto-ducking), or a **Master Audio File** that locks lip-sync across the entire film. |
| **✍️ WRITE** (AI Draft Assistant — *WIP*) | Connect a local OpenAI-compatible server (nInfer / LM Studio / llama.cpp) to propose films, draft shot cards, surgically edit individual hops (`Edit shots`), or lay out bar-synced music videos (`Song` → `Lay out`). **Note:** WRITE is an experimental work-in-progress drafting tool, not a 100% hands-off autopilot — always review the generated beats and photo descriptions before rendering. |
| **🎭 SWAP** (Guided Video Swap) | Standalone one-hop character or head swap driven by a reference video's motion and audio (`replace_person`, `head_swap`, `face_only`, `keep_person`) with a free-text wardrobe override box and automatic dual Swap LoRA loading. Leaves your multi-shot `SCRIPT` and `REFS` untouched. |

---

## Two Ways to Join Shots (`pin_mech`)

| Mode | Status & Best Use Case | How It Works & Honest Tradeoffs |
|---|---|---|
| **`motion_context`** *(Default — Built-In)* | **Stable** — Continuous relays (~5–6 hops on Turbo, ~7–8 hops on non-Turbo with Refine) & multi-scene `restart` cuts | Built directly into Hand Tie Clips (no separate node install required; credit to [NikoDemon80's ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context) for the latent-slice + end-aligned audio timeline design). Relays the previous hop's sampler AV latent into the next hop with in-latent head inpainting and distribution locking — and resets cleanly on `restart` cuts. |
| **`hand_tie`** *(Single-Take Previs)* | **Public Experimental** — Continuous one-location walking vlogs & tracking shots ([🎬 10-Hop 72 s Example](https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/hand-tie-10hop-00138.mp4)) | Renders one low-res single-take bridge previs first and ties each full-res hop's opening and closing 0.9 s to those clean windows via 7 per-step Motion-Context keyframes (with muted-window audio stripping and ambient floor locking) so hops never copy texture from each other.<br><br>**Previs Compiler Model (Recommended in `ComfyUI/models/text_encoders/`):** Drop any `qwen3vl_4b*.safetensors` or `qwen3vl_8b*.safetensors` into `models/text_encoders/` — e.g. [`qwen3vl_4b_fp8_scaled.safetensors` (Comfy-Org)](https://huggingface.co/Comfy-Org/Krea-2/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors), [`qwen3vl_4b` (silveroxides/K2Q)](https://huggingface.co/silveroxides/K2Q/tree/main/qwen3vl_4b), or [`qwen3vl_8b_int8_convrot.safetensors` (Comfy-Org)](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors). It loads at the start of the run to compile your beats into continuous bridge moments and unloads from VRAM before H3 starts.<br><br>**Caveat:** Because every hop must meet its bridge window, beats that ask for a sudden location change, hard camera angle jump, or fast prop action across a join can cause the previs to cut or morph. It works best when the camera and subject flow continuously in one setting, and may occasionally need a seed re-roll if a beat moves too far. |

---

## Built-In Finishing (`⚙ RUN` Page)

### 1. Realism Boost + Audio Refine (Built for `Simple` Mode / Standard `ref2va` & `t2va` Clips)
- **Using HTC for standard single generations (`ref2va` / `t2va`):** You don't need a multi-hop chain to use Hand Tie Clips — switch **`SCRIPT`** to **`Simple`** mode (`chains = 1` in `⚙ RUN`) and paste your standard **6-field MiniMax H3 `ref2va` / `t2va` prompt** (`subject_definitions:`, `summary:`, `detailed_description:`, etc.) or plain prose. HTC detects structured H3 prompts and passes them through untouched while giving you the visual **`REFS`** rail, **`realism_boost`**, and **`upsample`** on a single clip — which is what **`realism_boost: on`** is built for!
- **`realism_boost: on`** runs a 2-stage split schedule inside the sampler (at `steps = 6`: **2 base steps** set composition, then **10 realism LoRA steps** paint skin and lighting with *no fresh noise*).
- **Important Audio Note:** Because stage 2 adds no noise, the audio stream only gets those first 2 noisy steps and **will sound thin or under-converged on its own**. When using `realism_boost`, turn on **`audio_refine: on`** (under the **Soundtrack** group, `4–5` steps) — it re-samples the soundtrack cleanly while keeping your finished video 100% frozen.

<div align="center">
  <p><strong>Simple Mode (15s · Realism Boost + 2 MP Upsample) example</strong></p>
  <video controls preload="metadata" width="50%" style="max-width:480px; border-radius:8px;" src="https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips/resolve/main/docs/img/simple-realism-boost-2mp-00008.mp4">
    <source src="https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips/resolve/main/docs/img/simple-realism-boost-2mp-00008.mp4" type="video/mp4">
    <source src="https://media.githubusercontent.com/media/dntpi/ComfyUI-Hand-Tie-Clips/main/docs/img/simple-realism-boost-2mp-00008.mp4" type="video/mp4">
  </video>
</div>

### 2. Refine Pass (`hop_refine: full`) — Honest Limitations
- Runs a gentle low-sigma detail pass per hop (supports wiring a stock 30-step H3 DiT to `refine_model` while your main wire runs a Turbo model).
- **The honest truth on chain length:** `Refine` holds back relay degradation and keeps joins clean, pushing continuous `motion_context` chains to **5–6 clips on Turbo** before very visible texture changes appear, and **7–8 clips on non-Turbo**. Under `motion_context`, plan to transition to a cut (`anchor: "restart"`) every 4–5 clips or so to maintain the integrity of your textures (or use `pin_mech: hand_tie` for continuous single-take sequences).

### 3. Guided Tiled Upsampler (`upsample: on`) & Standalone Node
Enlarges the finished film to 1.5–2.0 MP in latent chunks and runs a 2-step tiled redraw. With the **`h3upscale` x2 LoRA**, the redraw is guided by your actual canvas frames at half the output size so it restores real detail instead of inventing texture. Toggling `upsample` on a cached chain skips hop generation and runs only the upscaler.

**Required Files for Upsampling:**
1. **Latent Upscaler (`ComfyUI/models/latent_upscale_models/`):**
   Download `minimax_h3_latent_upscaler_3d_bf16.safetensors` from [LBH-123-AI/Minimax_h3_latent_Upscaler](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler) (or `h3_upscaler_lms_v0.1.safetensors`).
2. **Guided Upscale LoRA (`ComfyUI/models/loras/`):**
   Download `h3upscale_v2_x2.safetensors` from **[sandpies/ComfyUI-H3-Video-Upsampler (Hugging Face)](https://huggingface.co/sandpies/ComfyUI-H3-Video-Upsampler/tree/main/lora)**.
3. **`upsample_dit` wire:** When using `h3upscale_v2_x2`, wire a **plain, unpatched H3 UNETLoader** into `upsample_dit` (the LoRA was trained on the bare DiT without turbo/realism LoRAs underneath).

*Want to upscale any existing video outside a chain? Use the included **H3 Video Upsampler (finished clip)** node (`workflows/HandTieClips_Upsample_Video.json`) or the standalone repo: [dntpi/ComfyUI-H3-Video-Upsampler](https://github.com/dntpi/ComfyUI-H3-Video-Upsampler).*

---

## Quick Start (60 Seconds)

From your `ComfyUI/custom_nodes/` folder:

```bash
git clone https://github.com/dntpi/ComfyUI-Hand-Tie-Clips.git
```

1. **Restart ComfyUI** and hard-refresh your browser (`Ctrl+Shift+R`). Zero pip dependencies.
2. Drag **`workflows/HandTieClips_Starter.json`** onto ComfyUI and point the loaders at your MiniMax H3 ref2va checkpoint, text encoder, and VAEs.
3. In **REFS**, drop your character photo onto Row 1 and give it a tag like `@maya`.
4. In **SCRIPT**, write one beat per shot card and press **Queue**.

<details>
<summary>Recommended Companion Packs & Optional Models</summary>

| Component | Where to Get It |
|---|---|
| **Turbo / SLA / AdaLN Nodes** | [ComfyUI-PlagueKind-Nodes](https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes) & [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes) |
| **Upscaler LoRA & Standalone Pack** | [sandpies/ComfyUI-H3-Video-Upsampler (HF)](https://huggingface.co/sandpies/ComfyUI-H3-Video-Upsampler) · [GitHub](https://github.com/dntpi/ComfyUI-H3-Video-Upsampler) |
| **3D Latent Upscaler Weights** | [LBH-123-AI/Minimax_h3_latent_Upscaler](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler) (`models/latent_upscale_models/`) |
| **Hand-Tie Previs Compiler (`models/text_encoders/`)** | [`qwen3vl_4b_fp8_scaled.safetensors` (5.2 GB)](https://huggingface.co/Comfy-Org/Krea-2/resolve/main/text_encoders/qwen3vl_4b_fp8_scaled.safetensors) · [`silveroxides/K2Q (4B)`](https://huggingface.co/silveroxides/K2Q/tree/main/qwen3vl_4b) · [`qwen3vl_8b_int8_convrot.safetensors` (9.3 GB)](https://huggingface.co/Comfy-Org/Qwen-Image-2.1/resolve/main/text_encoders/qwen3vl_8b_int8_convrot.safetensors) |

</details>

---

## Prompting Cheat-Sheet (The 5 Golden Rules)

MiniMax H3 samples at **CFG 1.0 with no negative prompt** — every word you write is *added* to the scene.

| ❌ Don't Write | ✅ Write Instead |
|---|---|
| *"She stops talking."* (Names talking → keeps her talking) | *"She smiles with her lips closed. The low hum of the refrigerator."* |
| *"No background music, silent room."* (Silence = model invents speech) | Always name a specific room tone: *"Soft rain on the windowpane, distant traffic hum."* |
| Re-describing hair, face, and clothes on Shots 2, 3, 4 | Describe **only the new action** on Shots 2+ — the reference rail and join pin carry her look. |
| Ending Shot 2 mid-sentence when Shot 3 should be quiet | Finish the action/sentence at the **end of Shot 2** so Shot 3 inherits a settled state. |
| Leaving the final shot's `tail` on `ongoing` | Set the last shot's `tail` to `settle` or `hold` so the film lands cleanly. |

Full authoring guide: **[PROMPTING.md](PROMPTING.md)** · Release history: **[CHANGELOG.md](CHANGELOG.md)** · Credits & Notices: **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)**

<details>
<summary>Included Nodes (Category: "Hand Tie Clips")</summary>

- **H3 Ref2VA Chain (`HandTieClips`):** The main studio node (`images`, `audio`, `info`, `contact_sheet`).
- **H3 Chain Preview (`HTCChainPreview`):** Live progress, seam side-by-side viewer, and end-of-run A/V drift monitor.
- **H3 Video Upsampler (`HTCVideoUpsampler`):** Standalone node to upscale any finished video clip using the tiled latent upsampler + `h3upscale` guide.
- **H3 Tiled Upsampler (`HTCTiledUpsampler`):** Latent-input tiled upsampler for custom graphs.
- **H3 RefMod Builder (`HTCRefModBuilder`):** Builds reusable visual/voice reference modules for the REFS rail.
- **H3 Cache (`HTCH3Cache`):** Residual step cache on the MODEL wire ([silveroxides](https://github.com/silveroxides) / [PlagueKind](https://github.com/PlagueKind); see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)).

</details>
