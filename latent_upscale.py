"""Learned latent upscaling for H3 video latents -- HTC's own runner.

WHAT IT RUNS. The weights are LBH-123-AI's
(https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler, Apache-2.0):
a 24-channel conv stack, twelve residual blocks on each side of a trilinear
resize, with a depthwise temporal conv after every other block. A network is
only loadable by the module layout it was trained in, so the layout and the
key names below are theirs, and so are the 48 normalisation numbers -- the
H3 latent statistics the weights were trained against, which are data, not
something to re-derive. See THIRD_PARTY_NOTICES.md. The runner is this pack's.

WHERE IT DIFFERS FROM THEIR NODE, AND WHY.

* **ComfyUI owns the VRAM.** Their node calls `unload_all_models()` first, so
  inside a chain the H3 model was thrown out and reloaded on every hop. Here
  `free_memory` asks for exactly what the pass needs, and the H3 weights stay
  wherever ComfyUI decides.
* **Whole clip when it fits.** Every conv and GroupNorm sees the whole clip,
  and the temporal receptive field (~70 latent frames) is longer than an 8 s
  hop (49). Any window is therefore an approximation stitched with a
  crossfade. At HTC's sizes the whole clip is ~1 GB of activations, so it
  runs whole, and windows are the fallback, not the default.
* **The block list is read from the file.** Which slots hold a residual block
  and which hold a temporal conv comes from the keys, not from an
  `every=2` rule, so a checkpoint with another rhythm loads or refuses
  strictly instead of loading wrong.
* **safetensors only.** No pickle, the same rule as the hop cache's latent
  sidecar.
* **The size is decided by the caller**, on HTC's 32 px canvas grid, so the
  chain can preallocate the master before the first hop renders.
"""
from __future__ import annotations

import os

import torch
import torch.nn as nn
import torch.nn.functional as F

TAG = "HandTieClips"
FOLDER = "latent_upscale_models"
SPATIAL = 16            # H3 video VAE: pixels per latent cell
CANVAS_GRID = 32        # pixel grid the upscaled canvas snaps to

# The statistics LBH-123-AI's weights were trained against (24 channels).
LATENT_MEAN = (
    0.858090341091156, -0.9606591463088989, 1.0661640167236328,
    -0.5090325474739075, -0.2727581858634949, -1.3675414323806763,
    -0.2553254961967468, -0.26907554268836975, -0.5376840829849243,
    -0.0464097298681736, 0.6657370328903198, 0.19690127670764923,
    -0.5460608005523682, -0.4035342037677765, -0.23683024942874908,
    0.25928452610969543, -0.30133944749832153, 0.211341992020607,
    -1.1206848621368408, 0.3581933379173279, -0.04225143790245056,
    0.2604829967021942, 0.22864092886447906, 0.7056031823158264,
)
LATENT_STD = (
    1.2223774194717407, 1.2767263650894165, 1.6831774711608887,
    1.7549455165863037, 1.5636216402053833, 2.194143533706665,
    0.9653137922286987, 1.0569885969161987, 0.841948926448822,
    0.7729952931404114, 1.8955937623977661, 0.946841835975647,
    0.7996809482574463, 0.44988900423049927, 0.7197399735450745,
    0.6936293244361877, 2.961095094680786, 2.7694199085235596,
    3.0496184825897217, 2.1088054180145264, 3.276226282119751,
    3.1627357006073, 2.2816812992095947, 2.6127843856811523,
)
# Temporal window for the fallback, and the frames each side it borrows.
WINDOW, WINDOW_PAD = 24, 5

_CACHE = {}


def register_folder():
    """Make `models/latent_upscale_models` known even on a Core without it."""
    try:
        import folder_paths
        if FOLDER not in folder_paths.folder_names_and_paths:
            folder_paths.add_model_folder_path(
                FOLDER, os.path.join(folder_paths.models_dir, FOLDER))
    except Exception:  # noqa: BLE001 -- never stop the pack loading
        pass


def choices():
    """["None", *.safetensors in the folder]. INPUT_TYPES must never raise."""
    try:
        import folder_paths
        return ["None"] + [f for f in folder_paths.get_filename_list(FOLDER)
                           if f.endswith(".safetensors")]
    except Exception:  # noqa: BLE001
        return ["None"]


def upscaled_canvas(width, height, factor):
    """(w, h) in pixels: `factor` times the canvas, snapped to the 32 px grid."""
    f = float(factor)
    return (max(CANVAS_GRID, round(width * f / CANVAS_GRID) * CANVAS_GRID),
            max(CANVAS_GRID, round(height * f / CANVAS_GRID) * CANVAS_GRID))


def shrink(x, h, w):
    """A video latent [B, C, T, H, W] area-averaged down to (h, w) cells.

    For a pin that has to sit on a smaller canvas than the one it was sampled
    on. Area, not bilinear: every source cell contributes, so the shrunk pin
    keeps the frame's average content instead of sampling a sparse grid.
    """
    if tuple(x.shape[-2:]) == (int(h), int(w)):
        return x
    return F.interpolate(x.float(), size=(int(x.shape[2]), int(h), int(w)),
                         mode="area").to(x.dtype)


# --- the network, in the layout the weights were trained in ----------------

def _norm(c):
    return nn.GroupNorm(32, c)


class _Res(nn.Module):
    def __init__(self, c, emb):
        super().__init__()
        self.in_layers = nn.Sequential(_norm(c), nn.SiLU(),
                                       nn.Conv3d(c, c, 3, padding=1))
        self.emb_layers = nn.Sequential(nn.SiLU(), nn.Linear(emb, 2 * c))
        self.out_norm = _norm(c)
        self.out_layers = nn.Sequential(nn.SiLU(), nn.Dropout(0.0),
                                        nn.Conv3d(c, c, 3, padding=1))

    def forward(self, x, emb):
        h = self.in_layers(x)
        scale, shift = self.emb_layers(emb).to(h.dtype)[..., None, None, None] \
            .chunk(2, dim=1)
        return x + self.out_layers(self.out_norm(h) * (1 + scale) + shift)


class _Temporal(nn.Module):
    def __init__(self, c, k):
        super().__init__()
        self.norm = _norm(c)
        self.dwconv = nn.Conv3d(c, c, (k, 1, 1), padding=(k // 2, 0, 0),
                                groups=c)
        self.pwconv = nn.Conv3d(c, c, 1)

    def forward(self, x):
        return x + self.pwconv(self.dwconv(F.silu(self.norm(x))))


class LatentUpscaler(nn.Module):
    def __init__(self, sd):
        super().__init__()
        c = int(sd["conv_in.weight"].shape[0])
        cin = int(sd["conv_in.weight"].shape[1])
        emb = int(sd["embed.0.weight"].shape[0])
        self.conv_in = nn.Conv3d(cin, c, 3, padding=1)
        self.embed = nn.Sequential(nn.Linear(1, emb), nn.SiLU(),
                                   nn.Linear(emb, emb))
        self.in_blocks = self._blocks(sd, "in_blocks", c, emb)
        self.out_blocks = self._blocks(sd, "out_blocks", c, emb)
        self.norm_out = _norm(c)
        self.conv_out = nn.Conv3d(c, cin, 3, padding=1)

    @staticmethod
    def _blocks(sd, name, c, emb):
        slots = {}
        for k in sd:
            if k.startswith(name + "."):
                i = int(k.split(".")[1])
                if k.endswith(".dwconv.weight"):
                    slots[i] = _Temporal(c, int(sd[k].shape[2]))
                elif ".in_layers." in k:
                    slots.setdefault(i, None)
        for i in [i for i, m in slots.items() if m is None]:
            slots[i] = _Res(c, emb)
        if sorted(slots) != list(range(len(slots))):
            raise RuntimeError(f"{TAG}: latent upscaler {name} has gaps in "
                               "its block list; not a file this runner reads.")
        return nn.ModuleList(slots[i] for i in range(len(slots)))

    def forward(self, x, size, scale):
        emb = self.embed(torch.tensor([[float(scale) - 1.0]],
                                      device=x.device, dtype=x.dtype))
        emb = emb.expand(x.shape[0], -1)
        x = self.conv_in(x)
        for b in self.in_blocks:
            x = b(x, emb) if isinstance(b, _Res) else b(x)
        x = F.interpolate(x, size=size, mode="trilinear", align_corners=False)
        for b in self.out_blocks:
            x = b(x, emb) if isinstance(b, _Res) else b(x)
        return self.conv_out(F.silu(self.norm_out(x)))


def load(name):
    """The network for `name`, cached on the CPU in bf16. Refuses loudly."""
    if name in _CACHE:
        return _CACHE[name]
    import folder_paths
    from safetensors.torch import load_file
    if not str(name).endswith(".safetensors"):
        raise RuntimeError(f"{TAG}: latent upscaler {name!r}: only "
                           ".safetensors files are read (no pickle).")
    path = folder_paths.get_full_path(FOLDER, name)
    if not path:
        raise RuntimeError(f"{TAG}: latent upscaler {name!r} is not in "
                           f"models/{FOLDER}.")
    sd = load_file(path, device="cpu")
    if any(k.startswith("upscaler.") for k in sd):
        sd = {k[len("upscaler."):]: v for k, v in sd.items()
              if k.startswith("upscaler.")}
    if "conv_in.weight" not in sd or "embed.0.weight" not in sd:
        raise RuntimeError(f"{TAG}: {name!r} is not an H3 latent upscaler "
                           "(no conv_in / embed weights).")
    net = LatentUpscaler(sd)
    net.load_state_dict({k: v.float() for k, v in sd.items()}, strict=True)
    net = net.to(torch.bfloat16).eval().requires_grad_(False)
    _CACHE.clear()          # one resident file is enough; they are 0.7-1.3 GB
    _CACHE[name] = net
    print(f"[{TAG}] latent upscaler {name}: "
          f"{sum(p.numel() for p in net.parameters()) / 1e6:.0f}M params, "
          f"{len(net.in_blocks)}+{len(net.out_blocks)} blocks", flush=True)
    return net


def _peak_bytes(net, t, h, w):
    c = int(net.conv_in.weight.shape[0])
    return 8 * c * int(t) * int(h) * int(w) * 2


def upscale(x, name, h, w, factor):
    """Video latent [B, 24, T, H, W] -> [B, 24, T, h, w], same dtype/device.

    `h`, `w` are latent cells. The network is told `factor`, the multiplier
    asked for, not the ratio left after snapping to the grid -- the value
    their "scale by multiplier" mode passes, so the recipe's x1.25 conditions
    it identically.
    """
    import comfy.model_management as mm
    net = load(name)
    t = int(x.shape[2])
    size = (t, int(h), int(w))
    if tuple(x.shape[-3:]) == size:
        return x
    scale = float(factor)
    dev = mm.get_torch_device()
    weights = sum(p.numel() for p in net.parameters()) * 2
    whole = _peak_bytes(net, t, h, w)
    mm.free_memory(weights + whole, dev)
    free = mm.get_free_memory(dev) if dev.type != "cpu" else whole * 2
    windowed = whole > free
    mean = torch.tensor(LATENT_MEAN).view(1, -1, 1, 1, 1)
    std = torch.tensor(LATENT_STD).view(1, -1, 1, 1, 1)
    src = ((x.detach().float().cpu() - mean) / std).to(torch.bfloat16)
    net.to(dev)
    try:
        with torch.inference_mode():
            if not windowed:
                out = net(src.to(dev), size, scale).float().cpu()
            else:
                print(f"[{TAG}] latent upscale: whole clip needs "
                      f"{whole / 2**30:.1f} GB, {free / 2**30:.1f} GB free; "
                      f"windows of {WINDOW} frames", flush=True)
                out = _windowed(net, src, size, scale, dev)
    finally:
        net.to("cpu")
        mm.soft_empty_cache()
    return (out * std + mean).to(device=x.device, dtype=x.dtype)


def _windowed(net, src, size, scale, dev):
    """The fallback: overlapping temporal windows, crossfaded where they meet."""
    t = int(src.shape[2])
    out = torch.zeros(src.shape[0], src.shape[1], t, size[1], size[2])
    wsum = torch.zeros(1, 1, t, 1, 1)
    start = 0
    while start < t:
        a, b = max(0, start - WINDOW_PAD), min(t, start + WINDOW + WINDOW_PAD)
        seg = net(src[:, :, a:b].to(dev), (b - a, size[1], size[2]), scale)
        ramp = torch.ones(b - a)
        lead, tail = start - a, b - min(t, start + WINDOW)
        if lead:
            ramp[:lead] = torch.arange(1, lead + 1) / (lead + 1)
        if tail:
            ramp[-tail:] = torch.arange(tail, 0, -1) / (tail + 1)
        ramp = ramp.view(1, 1, -1, 1, 1)
        out[:, :, a:b] += seg.float().cpu() * ramp
        wsum[:, :, a:b] += ramp
        start += WINDOW
    return out / wsum.clamp(min=1e-8)


class HTCLatentUpscale:
    """The same runner as a node, for any H3 latent outside the chain."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT", {
                    "tooltip": "An H3 video or AV latent. Audio passes through untouched.",
                }),
                "upscale_model": (choices(), {
                    "tooltip": "From models/latent_upscale_models.",
                }),
                "scale": ("FLOAT", {
                    "default": 1.25, "min": 1.0, "max": 4.0, "step": 0.05,
                    "tooltip": "How much bigger. Snapped to a 32 px grid. 1.0 passes the latent through.",
                }),
            },
        }

    RETURN_TYPES = ("LATENT",)
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = ("H3 learned latent upscale (LBH-123-AI's weights, HTC's "
                   "runner): no decode, no re-encode.")

    def run(self, latent, upscale_model, scale):
        from . import latents as _latents
        parts = _latents.from_dict(latent)
        if not parts or parts[0].ndim != 5 or parts[0].shape[1] != 24:
            raise RuntimeError(f"{TAG}: HTCLatentUpscale takes an H3 video "
                               "latent (24 channels, 5-D).")
        if upscale_model == "None" or float(scale) <= 1.0:
            return (latent,)
        v = parts[0]
        w, h = upscaled_canvas(int(v.shape[-1]) * SPATIAL,
                               int(v.shape[-2]) * SPATIAL, float(scale))
        up = upscale(v, str(upscale_model), h // SPATIAL, w // SPATIAL,
                     float(scale))
        out = {k: val for k, val in latent.items() if k != "noise_mask"}
        out["samples"] = _latents.rebuild(latent["samples"], [up] + parts[1:])
        return (out,)


NODE_CLASS_MAPPINGS = {"HTCLatentUpscale": HTCLatentUpscale}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCLatentUpscale": "H3 Latent Upscale"}
