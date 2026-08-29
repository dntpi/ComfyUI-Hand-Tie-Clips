"""Hand Tie Clips — native MiniMax H3 Ref2VA continuation for ComfyUI."""
from .h3_ref_chain import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .preview_node import (
    NODE_CLASS_MAPPINGS as _PREVIEW_NODES,
    NODE_DISPLAY_NAME_MAPPINGS as _PREVIEW_NAMES,
)
from .tone import (
    NODE_CLASS_MAPPINGS as _TONE_NODES,
    NODE_DISPLAY_NAME_MAPPINGS as _TONE_NAMES,
)

NODE_CLASS_MAPPINGS.update(_PREVIEW_NODES)
NODE_DISPLAY_NAME_MAPPINGS.update(_PREVIEW_NAMES)
NODE_CLASS_MAPPINGS.update(_TONE_NODES)
NODE_DISPLAY_NAME_MAPPINGS.update(_TONE_NAMES)

# The editor's dropdowns are served from directives.VOCAB rather than copied
# into JS. A failure here costs the dropdowns their tooltips, not the node, so
# it must never stop the pack from loading.
try:
    from . import routes as _routes
    _routes.register()
except Exception as _exc:  # noqa: BLE001
    print(f"[HandTieClips] vocab route not registered: {_exc!r}", flush=True)

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
