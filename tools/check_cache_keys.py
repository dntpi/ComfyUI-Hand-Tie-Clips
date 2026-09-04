r"""Offline tests for hop-cache invalidation -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_cache_keys.py

A wrong cache key is the one defect class in this pack with no natural
detector. It does not crash, it does not fail a check, and it does not print
anything: the run simply serves frames rendered under settings that are no
longer the ones on the graph. The docstring on `_model_fingerprint` says it
plainly -- "silently wrong output, which is worse than no cache at all" -- and
the class has now bitten three times.

  * SLA sparsity. H3-SLA-Attention installs its config by closure, and a
    callable hashed as `type(fn).__name__` collapsed to the bare string
    "function". Changing sparsity 0.90 -> 0.50 left the fingerprint unmoved.
    Fixed by `_closure_scalars`.
  * Object-configured nodes. A node that installs a configured *instance*
    through `set_model_patch_replace` has no closure at all, and an instance
    inherits neither `__qualname__` nor `__name__`, so it collapsed to the
    constant "fn()". Presence was detected; configuration was not. Fixed by
    `_object_scalars`.
  * The base checkpoint. The fingerprint described what was patched ONTO the
    model and never the model itself, so an int8 build and a bf16 build under
    the same LoRA stack produced identical keys.

Each of those was found by reading, and each could return the moment someone
adds a branch to `_scalars`. So they are asserted here instead, by
construction: build two graphs that differ in exactly one thing and require the
digests to disagree -- and, just as importantly, require two identical graphs
to agree, because a fingerprint that always changes is a cache that never hits.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-52s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    spec = importlib.util.spec_from_file_location(
        "htcpack", os.path.join(HERE, "__init__.py"),
        submodule_search_locations=[HERE])
    m = importlib.util.module_from_spec(spec)
    sys.modules["htcpack"] = m
    spec.loader.exec_module(m)
    return m


class _DiffusionModel:
    """Enough of a diffusion model to be counted and asked its dtype."""

    def __init__(self, n_params=2, dtype="bf16"):
        import torch
        self.dtype = dtype
        self._ps = [torch.zeros(4) for _ in range(n_params)]

    def parameters(self):
        return iter(self._ps)


class _BaseModel:
    def __init__(self, dtype="bf16", n_params=2):
        self.diffusion_model = _DiffusionModel(n_params, dtype)

    def get_dtype(self):
        return self.diffusion_model.dtype


class _Patcher:
    """The shape of ModelPatcher that `_model_fingerprint` actually reads."""

    def __init__(self, dtype="bf16", n_params=2, transformer=None, patches=None):
        self.model = _BaseModel(dtype, n_params)
        self.patches = patches or {}
        self.model_options = {"transformer_options": transformer or {}}

    def model_dtype(self):
        if hasattr(self.model, "get_dtype"):
            return self.model.get_dtype()


class _ObjectConfigured:
    """A node that carries its settings on an instance, not in a closure.

    Callable on purpose: that is what sent it down the closure path, where an
    instance produces no name and no cells and every setting disappeared.
    """

    def __init__(self, reuse_threshold=0.05, max_steps=2):
        self.reuse_threshold = reuse_threshold
        self.max_steps = max_steps

    def __call__(self, *a, **kw):
        return None


def _closure_configured(sparsity):
    """The SLA shape: settings captured in cells."""
    def _override(*a, **kw):
        return sparsity
    return _override


def main():
    load_pack()
    fp = sys.modules["htcpack.h3_ref_chain"]._model_fingerprint

    def dit(obj):
        return {"patches_replace": {"dit": {("block_loop", 0): obj}}}

    print("base checkpoint")
    ck("dtype alone moves the key",
       fp(_Patcher(dtype="bf16")) != fp(_Patcher(dtype="int8")),
       "int8 vs bf16 under an identical LoRA stack")
    ck("parameter count alone moves the key",
       fp(_Patcher(n_params=2)) != fp(_Patcher(n_params=3)))
    ck("two identical models agree",
       fp(_Patcher()) == fp(_Patcher()),
       "a key that always moves is a cache that never hits")

    print("settings carried on an object")
    a = dit(_ObjectConfigured(reuse_threshold=0.05))
    b = dit(_ObjectConfigured(reuse_threshold=0.20))
    c = dit(_ObjectConfigured(reuse_threshold=0.05))
    ck("changing a setting moves the key",
       fp(_Patcher(transformer=a)) != fp(_Patcher(transformer=b)),
       "reuse_threshold 0.05 -> 0.20")
    ck("the same setting agrees",
       fp(_Patcher(transformer=a)) == fp(_Patcher(transformer=c)))
    ck("installing the node at all moves the key",
       fp(_Patcher()) != fp(_Patcher(transformer=a)))

    print("settings carried in a closure")
    ck("changing a closed-over setting moves the key",
       fp(_Patcher(transformer=dit(_closure_configured(0.90))))
       != fp(_Patcher(transformer=dit(_closure_configured(0.50)))),
       "the SLA sparsity regression")

    print("mutable run state is still excluded")
    shared = {"calls": 0}

    def _with_state():
        def _override(*a, **kw):
            shared["calls"] += 1
        return _override
    before = fp(_Patcher(transformer=dit(_with_state())))
    shared["calls"] += 17
    ck("a counter the sampler advances does not move the key",
       before == fp(_Patcher(transformer=dit(_with_state()))),
       "hashing it would miss the cache on every queue")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
