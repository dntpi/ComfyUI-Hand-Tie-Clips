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

import functools
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

    print("settings reached through a binding")
    # A node hands the model `self.method` rather than `self`, or a
    # functools.partial rather than a closure. Both are callable, both have an
    # empty __dict__ of their own, and neither has cells -- so before they were
    # unwrapped, both collapsed to a constant and every setting behind them was
    # invisible. These are the object bug and the SLA bug in their third and
    # fourth binding shapes.
    class _Bound:
        def __init__(self, reuse_threshold=0.05):
            self.reuse_threshold = reuse_threshold

        def patch(self, *a, **kw):
            return None

    ck("a bound method carries its instance's settings",
       fp(_Patcher(transformer=dit(_Bound(0.05).patch)))
       != fp(_Patcher(transformer=dit(_Bound(0.20).patch))),
       "vars() on a bound method sees the function, not the instance")
    ck("two identical bound methods agree",
       fp(_Patcher(transformer=dit(_Bound(0.05).patch)))
       == fp(_Patcher(transformer=dit(_Bound(0.05).patch))))

    def _plain_override(*a, **kw):
        return None

    ck("a functools.partial carries its keywords",
       fp(_Patcher(transformer=dit(functools.partial(_plain_override, sparsity=0.90))))
       != fp(_Patcher(transformer=dit(functools.partial(_plain_override, sparsity=0.50)))),
       "no __name__, no cells, empty __dict__")
    ck("a functools.partial carries its positional args",
       fp(_Patcher(transformer=dit(functools.partial(_plain_override, 0.90))))
       != fp(_Patcher(transformer=dit(functools.partial(_plain_override, 0.50)))))

    print("mutable run state")
    shared = {"calls": 0}

    def _with_state():
        def _override(*a, **kw):
            shared["calls"] += 1
        return _override
    before = fp(_Patcher(transformer=dit(_with_state())))
    shared["calls"] += 17
    ck("a counter in a closure cell does not move the key",
       before == fp(_Patcher(transformer=dit(_with_state()))),
       "hashing a mutable container would miss the cache on every queue")

    # The other half of that tradeoff, asserted rather than left implicit: a
    # PUBLIC SCALAR attribute is hashed even when the node mutates it, so a
    # node carrying a step counter on itself moves the fingerprint between runs
    # and the cache stops hitting while it is installed. That is deliberate --
    # this pack renders twice rather than serving the wrong frames once -- but
    # it is a real cost, and the run log prints the fingerprint so it can be
    # seen rather than guessed at. If this assertion ever flips, the tradeoff
    # was changed and the docstring on `_object_scalars` needs to change with it.
    class _Counting:
        def __init__(self):
            self.reuse_threshold = 0.05
            self.cnt = 0

        def __call__(self, *a, **kw):
            return None

    counting = _Counting()
    seen = fp(_Patcher(transformer=dit(counting)))
    counting.cnt += 7
    ck("a public scalar counter DOES move the key (documented cost)",
       seen != fp(_Patcher(transformer=dit(counting))),
       "wasteful, not wrong -- see _object_scalars")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
