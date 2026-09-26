"""The H3 Cache node.

Hand Tie Clips' own wrapper (MIT) around the vendored cache in ``h3_cache.py``.
The implementation there is silveroxides' work, improved and ported by
PlagueKind, redistributed with permission; read that file's header before
touching either.

WHY THIS IS IN THE PACK AT ALL. The non-turbo base is the configuration this
pack recommends -- two 9x8s chains held identity end to end, where the turbo
merge was visibly gone by hop 4. Non-turbo only stays *shippable* if it runs at
a usable speed, and this cache is what buys that. Shipping the chain node
without it meant telling every user to install a second pack and hand-patch it,
which is not a recommendation anyone follows.

WIRING. Drop it on the MODEL wire anywhere before the sampler. It patches a
cloned patcher only, so it composes with SLA attention and with LoRA loaders in
either order. Feed its output to HandTieClips' ``model`` input -- and to
``refine_model`` too if you run a separate refine model, since the refine pass
is its own sampler call and gets no caching otherwise.
"""
from __future__ import annotations

from .h3_cache import patch_h3_minimax_cache

TAG = "HTCH3Cache"

# PlagueKind's own running settings, not his node's declared defaults -- the two
# disagree on three of four. His node ships 0.15/0.90/2; what he actually runs,
# and told us to ship, is a narrower reuse window and a single skip. Both changes
# point the same way: less caching than the widget defaults invite. That is the
# right bias for this pack, because cache error is not a per-hop cost here -- it
# feeds the next hop's guide and compounds, which is the failure mode the whole
# project exists to fight.
#
# Source: PlagueKind, 2026-09-15. Not measured here. If a sweep ever contradicts
# these, the sweep wins and this comment gets rewritten with the numbers.
DEFAULTS = dict(reuse_threshold=0.05, start_percent=0.20, end_percent=0.80,
                max_steps=1)

DEVICE_MODES = ("auto", "cuda", "cpu")


class HTCH3Cache:
    """Reuse MiniMax-H3's whole-block-stack residual across similar steps."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL", {
                    "tooltip": "The MiniMax H3 model.",
                }),
                "reuse_threshold": ("FLOAT", {
                    "default": DEFAULTS["reuse_threshold"], "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": ("How much may change before a cached step is refused. Higher is faster and "
                                "less exact."),
                }),
                "start_percent": ("FLOAT", {
                    "default": DEFAULTS["start_percent"], "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "No reuse before this point in sampling. Early steps shape the picture.",
                }),
                "end_percent": ("FLOAT", {
                    "default": DEFAULTS["end_percent"], "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "No reuse after this point in sampling. Late steps add detail.",
                }),
                "max_steps": ("INT", {
                    "default": DEFAULTS["max_steps"], "min": 1, "max": 10, "step": 1,
                    "tooltip": "Most skipped steps in a row. 1 is safest; higher is faster.",
                }),
                "device": (list(DEVICE_MODES), {
                    "default": "auto",
                    "tooltip": "Where the cache lives. cpu saves video memory.",
                }),
                "verbose": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Log each step's cache decision.",
                }),
            },
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "run"
    CATEGORY = "Hand Tie Clips"
    DESCRIPTION = (
        "Block-stack residual cache for MiniMax-H3: skips re-running the "
        "transformer stack on steps whose features have barely moved. Patches a "
        "cloned model only. This is what makes the non-turbo base fast enough "
        "to recommend. Implementation by silveroxides (ComfyUI-UtilsCollection), "
        "improved and ported by PlagueKind, redistributed with permission -- "
        "see THIRD_PARTY_NOTICES.md."
    )

    def run(self, model, reuse_threshold, start_percent, end_percent,
            max_steps, device, verbose):
        # No try/except here, and that is the deliberate difference from
        # upstream: his node catches everything and passes the model through
        # unpatched, so a broken patch costs you a full-price render that looks
        # fine and no clue why it took twice as long. Every failure this can
        # raise is a wiring error with an actionable message -- wrong model
        # class, a second cache already on the wire, start past end -- and a
        # wiring error should stop the queue, not quietly bill you for it.
        patched = patch_h3_minimax_cache(
            model,
            reuse_threshold=float(reuse_threshold),
            start_percent=float(start_percent),
            end_percent=float(end_percent),
            max_steps=int(max_steps),
            device=str(device),
            verbose=bool(verbose),
        )
        if verbose:
            print("[%s] cache armed: threshold %.3f, window %.2f-%.2f, "
                  "max %d consecutive skips, residual on %s"
                  % (TAG, float(reuse_threshold), float(start_percent),
                     float(end_percent), int(max_steps), device), flush=True)
        return (patched,)


NODE_CLASS_MAPPINGS = {"HTCH3Cache": HTCH3Cache}
NODE_DISPLAY_NAME_MAPPINGS = {"HTCH3Cache": "H3 Cache"}
