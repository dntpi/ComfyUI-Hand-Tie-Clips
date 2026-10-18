r"""Generate the standalone ComfyUI-H3-Video-Upsampler pack from HTC's source.

    D:\ComfyUI\venv\Scripts\python.exe tools\build_upsampler_pack.py [OUT_DIR]

The pack is the H3 Video Upsampler (finished clip in, bigger clip out) with
the h3upscale x2 LoRA and one example workflow. Its three modules are copied
from this repo, never edited by hand: re-run this after changing
tiled_upsampler.py / upscale_guide.py / video_upsampler.py and the pack is in
sync again. (Every hand-mirrored copy of an HTC feature so far has drifted.)

Renames on the way, so both packs can be installed side by side:
  * node id HTCVideoUpsampler -> H3VideoUpsampler, its log tag likewise;
  * the HTCTiledUpsampler class in tiled_upsampler.py is NOT registered;
  * the PackedLayout patch keeps its `_htc_strided` marker on purpose: with
    both packs installed, whichever loads first installs it, the other skips.

Writes code, LICENSE, THIRD_PARTY_NOTICES, README (GitHub) and README_HF.md
(the Hugging Face model card), the workflow under example_workflows/, and the
LoRA + its training config under lora/. lora/*.safetensors is gitignored, so
the git history (GitHub) never carries the 620 MB file.

Publishing to Hugging Face, learned the hard way (2026-10-06):
  * this machine sets HF_HUB_DISABLE_IMPLICIT_TOKEN, so pass the token
    explicitly (`HfApi(token=get_token())`) or every write is a bare 401;
  * upload_folder AND the Hub honour .gitignore -- the local one and the
    repo's own -- so the LoRA must go up by upload_file, with no .gitignore
    in the HF repo (delete it there; it exists only for GitHub);
  * README_HF.md goes up as README.md (the model card).
"""
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
OUT = (sys.argv[1] if len(sys.argv) > 1
       else os.path.join(os.path.expanduser("~"), "Desktop", "ComfyUI-H3-Video-Upsampler"))
LORA_DIR = os.path.join(COMFY, "models", "loras", "h3upscale", "v2_x2")
LORA = "h3upscale_v2_x2.safetensors"
VERSION = "0.2.2"
GH = "https://github.com/dntpi/ComfyUI-H3-Video-Upsampler"
HF = "https://huggingface.co/sandpies/ComfyUI-H3-Video-Upsampler"
HTC_GH = "https://github.com/dntpi/ComfyUI-Hand-Tie-Clips"


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def write(rel, text):
    path = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def sub_once(text, old, new, what):
    if text.count(old) != 1:
        raise SystemExit(f"pack build: expected one '{what}' to rename, found {text.count(old)}")
    return text.replace(old, new)


HEADER = ("# Generated from ComfyUI-Hand-Tie-Clips ({}) by tools/build_upsampler_pack.py.\n"
          "# Edit it there, not here: the next build overwrites this file.\n").format(HTC_GH)


def code():
    tu = read("tiled_upsampler.py")
    tu = sub_once(tu, 'TAG = "HTCTiledUpsampler"', 'TAG = "H3VideoUpsampler"', "tiled TAG")
    ug = read("upscale_guide.py")
    ug = sub_once(ug, 'TAG = "HTC upscale guide"', 'TAG = "H3VideoUpsampler guide"', "guide TAG")
    vu = read("video_upsampler.py")
    vu = sub_once(vu, 'TAG = "HTCVideoUpsampler"', 'TAG = "H3VideoUpsampler"', "video TAG")
    vu = sub_once(vu, 'NODE_CLASS_MAPPINGS = {"HTCVideoUpsampler": HTCVideoUpsampler}',
                  'NODE_CLASS_MAPPINGS = {"H3VideoUpsampler": HTCVideoUpsampler}', "node id")
    vu = sub_once(vu, 'NODE_DISPLAY_NAME_MAPPINGS = {"HTCVideoUpsampler": "H3 Video Upsampler (finished clip)"}',
                  'NODE_DISPLAY_NAME_MAPPINGS = {"H3VideoUpsampler": "H3 Video Upsampler"}', "display name")
    vu = sub_once(vu, 'CATEGORY = "Hand Tie Clips"', 'CATEGORY = "video/upscale"', "category")
    for name, text in (("tiled_upsampler.py", tu), ("upscale_guide.py", ug),
                       ("video_upsampler.py", vu)):
        write(name, HEADER + text)
    write("__init__.py", HEADER + '''"""H3 Video Upsampler: a finished MiniMax H3 clip, upscaled with the H3 latent
upscaler and a light two-step re-draw in tiles. An h3upscale guide LoRA gets its
guide inside those steps."""
from .tiled_upsampler import register_folder as _register_folder
from .video_upsampler import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

_register_folder()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
''')


def workflow():
    w = json.loads(read(os.path.join("workflows", "HandTieClips_Upsample_Video.json")))
    for n in w["nodes"]:
        if n["type"] == "HTCVideoUpsampler":
            n["type"] = "H3VideoUpsampler"
            n["properties"]["Node name for S&R"] = "H3VideoUpsampler"
            n["widgets_values"][4] = LORA          # lora: the shipped x2 LoRA
        if n["type"] == "SaveVideo":
            n["widgets_values"][0] = "video/H3_upsampled"
        if n["type"] == "MarkdownNote":
            note = n["widgets_values"][0]
            note = note.replace("`video/HANDTIECLIPS/upsampled`", "`video/H3_upsampled`")
            note = note.replace("an **h3upscale** LoRA is detected",
                                f"set to `{LORA}` (put it in `models/loras`); an **h3upscale** LoRA is detected")
            n["widgets_values"][0] = note
    w["id"] = "h3-video-upsampler"
    write(os.path.join("example_workflows", "H3_Video_Upsampler.json"), json.dumps(w, indent=1))


def lora():
    os.makedirs(os.path.join(OUT, "lora"), exist_ok=True)
    src = os.path.join(LORA_DIR, LORA)
    dst = os.path.join(OUT, "lora", LORA)
    if not (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src)):
        shutil.copyfile(src, dst)                   # byte for byte; never re-saved
    shutil.copyfile(os.path.join(LORA_DIR, "config.yaml"), os.path.join(OUT, "lora", "training_config.yaml"))


def docs():
    write("LICENSE", read("LICENSE"))
    notices = read("THIRD_PARTY_NOTICES.md")
    tiled = notices[notices.index("## `tiled_upsampler.py`"):]
    tiled = tiled.split("\n## ", 1)[0].rstrip() + "\n"
    write("THIRD_PARTY_NOTICES.md", f"""# Third-party notices

The code here is generated from [ComfyUI-Hand-Tie-Clips]({HTC_GH}) (MIT).

{tiled}
## `upscale_guide.py` -- the h3upscale guide layout

The guide geometry (`target_crop_v2` / `target_grid_stride_v1`: the source
reduced by the LoRA's factor, packed on every factor-th patch of the target's
grid) is the layout of [Alissonerdx](https://huggingface.co/Alissonerdx)'s
ai-toolkit fork, branch `minimax-h3-latent-guides` (ai-toolkit: MIT). No code
was copied; this pack's checker compared the two layouts row for row.

## `lora/{LORA}`

Trained by Sandpies with that fork on Alissonerdx's
[h3upscale-4k](https://huggingface.co/datasets/Alissonerdx/h3upscale-4k)
dataset, on the MiniMax H3 Ref2VA base. Read both of their terms before
redistributing or using it commercially.
""")
    write(".gitignore", "__pycache__/\n*.pyc\nlora/*.safetensors\n")
    write(".gitattributes", "*.safetensors filter=lfs diff=lfs merge=lfs -text\n")
    write("pyproject.toml", f"""[project]
name = "comfyui-h3-video-upsampler"
version = "{VERSION}"
description = "Upscale a finished MiniMax H3 clip: H3 latent upscaler + a light two-step re-draw in tiles, with an h3upscale guide LoRA."
license = {{ file = "LICENSE" }}
dependencies = []

[project.urls]
Repository = "{GH}"

[tool.comfy]
PublisherId = "sandpies"
DisplayName = "H3 Video Upsampler"
""")
    body = f"""Upscale a **finished** MiniMax H3 clip (or any clip) in ComfyUI: the H3 latent
upscaler enlarges it, then a light two-step re-draw in tiles restores detail. With the
**h3upscale x2 LoRA** those steps also get the clip itself, at half the output size,
as a guide -- so it restores the clip's own detail instead of inventing texture.
Mouths, motion and the soundtrack come from your clip.

## Install

1. Clone into `ComfyUI/custom_nodes` and restart ComfyUI.
2. Put the latent upscaler `minimax_h3_latent_upscaler_3d_bf16.safetensors` in
   `ComfyUI/models/latent_upscale_models`
   ([LBH-123-AI](https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler)).
3. Put `{LORA}` in `ComfyUI/models/loras` (from [Hugging Face]({HF}/tree/main/lora)).
4. Load `example_workflows/H3_Video_Upsampler.json`, pick a clip, queue.

Needs a ComfyUI with MiniMax H3 support and the H3 models (DiT, text encoder,
video and audio VAE).

## The node: H3 Video Upsampler

| widget | what it does |
|---|---|
| `megapixels` | output size, aspect kept (2 = about 1920x1080) |
| `denoise` | fraction of the schedule the two re-draw steps cover; **0.1** is the setting that held, 0 only enlarges |
| `lora` | `{LORA}` = guided re-draw (detected from its header). Any other LoRA just sits on the re-draw |
| `tile_tokens` | **VRAM.** Largest sequence per tile; lower = more, smaller tiles, slower, less memory. 70000 is the default; try 40000 or 25000 on smaller cards |
| `prompt` | defaults to the caption the LoRA was trained on; keep it with that LoRA |

- Wire the **plain** diffusion model loader as `model` when using the LoRA: it was
  trained on the bare DiT, and extra LoRAs under it re-add their own texture.
- `audio_vae` wired: the re-draw hears the soundtrack (best at 24 fps). Unwired:
  it hears silence. Either way you get your original soundtrack back.
- **Low VRAM / RAM:** with [ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes)
  installed, the re-draw model gets its MiniMax low-VRAM attention and chunked
  feed-forward (same maths, less memory); without it they are skipped. Other
  models are unloaded before each stage, the latent upscale falls back to CPU and
  the re-draw to smaller tiles if the card runs out, and the result is decoded
  in slices so a long clip never sits in RAM as one tensor.
- Any frame size and length: frames are fitted to 32 px and padded to a valid H3
  length, then the padding is trimmed off again.

## What the LoRA does and does not do

Tested by eye on H3 renders: guided at denoise 0.1, the 2 MP result beat the native
render in a blind comparison (0.1.0, one step). 0.2.0 runs the re-draw exactly as
Hand-Tie-Clips' chain upsampler does: two steps on the denoise schedule, 192 px
tile overlap. It is an **upscaler**: generating a video from noise
with it flickers every 17 frames (it was trained on 5-frame clips); refining an
existing clip, as this node does, does not.

## Credits

Built in [ComfyUI-Hand-Tie-Clips]({HTC_GH}), where the same upsampler runs over a
whole chain. Latent upscaler network: LBH-123-AI. Upscale recipe: bbaudio-2025's
MMH3 Ultimate Upscale. Guide layout and dataset: Alissonerdx. See
`THIRD_PARTY_NOTICES.md`.
"""
    write("README.md", f"# ComfyUI H3 Video Upsampler\n\n"
          f"> The LoRA and a mirror of this code: [Hugging Face]({HF})\n\n" + body)
    write("README_HF.md", f"""---
license: other
license_name: see-third-party-notices
base_model: Comfy-Org/MiniMax-H3
library_name: comfyui
tags:
- minimax-h3
- comfyui
- lora
- video-upscale
---

# H3 Video Upsampler + h3upscale x2 LoRA

> Code: [GitHub]({GH}). The LoRA is in [`lora/`](./lora).

""" + body)


def main():
    if not os.path.isfile(os.path.join(LORA_DIR, LORA)):
        raise SystemExit(f"pack build: {LORA} not found in {LORA_DIR}")
    os.makedirs(OUT, exist_ok=True)
    code()
    workflow()
    lora()
    docs()
    print(f"pack written to {OUT}")
    for root, _, files in os.walk(OUT):
        if ".git" in root.split(os.sep):
            continue
        for f in sorted(files):
            p = os.path.join(root, f)
            print(f"  {os.path.relpath(p, OUT):48s} {os.path.getsize(p):>12,d}")


if __name__ == "__main__":
    main()
