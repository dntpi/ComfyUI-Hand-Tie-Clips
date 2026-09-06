r"""Every call into a Core node is by NAME, and every name is real.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_core_calls.py

A user on a different ComfyUI build hit
`MiniMaxH3ReferenceToVideo.execute() got multiple values for argument
'ref_image_size'` on hop 1, before anything sampled. The pack was passing the
first seven arguments POSITIONALLY; their Core orders those parameters
differently, so the seventh positional landed on `ref_image_size` and the
keyword we also passed collided with it.

Core's parameter ORDER is not a contract. Its parameter NAMES are. So the rule
is: no positional arguments into a Core node, ever, and every keyword must
exist in the signature actually installed.

This checks both halves against the Core that is really on this machine, which
is the only version whose signature can be inspected -- it cannot prove the
call works on someone else's build, but it does prove the pack is not relying
on order, which is what broke.
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []
# Every Core node this pack calls, and the module that defines it. The H3 three
# are the ones whose order actually moved; the sampler five are the same failure
# class waiting to happen, and cost nothing to watch.
WATCHED = {
    "MiniMaxH3ReferenceToVideo": "comfy_extras.nodes_minimax_h3",
    "MiniMaxH3AddGuide": "comfy_extras.nodes_minimax_h3",
    "MiniMaxH3ImageToVideo": "comfy_extras.nodes_minimax_h3",
    "MiniMaxH3SigmaShift": "comfy_extras.nodes_minimax_h3",
    "KSamplerSelect": "comfy_extras.nodes_custom_sampler",
    "BasicScheduler": "comfy_extras.nodes_custom_sampler",
    "BasicGuider": "comfy_extras.nodes_custom_sampler",
    "RandomNoise": "comfy_extras.nodes_custom_sampler",
    "SamplerCustomAdvanced": "comfy_extras.nodes_custom_sampler",
}


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def core_params(cls_name):
    import importlib
    try:
        core = importlib.import_module(WATCHED[cls_name])
    except (ImportError, KeyError):
        return None
    cls = getattr(core, cls_name, None)
    if cls is None:
        return None
    fn = getattr(cls, "execute", None)
    if fn is None:
        return None
    p = list(inspect.signature(fn).parameters)
    return [x for x in p if x not in ("cls", "self")]


def main():
    src = io.open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # Direct: MiniMaxH3AddGuide.execute(...)
        if isinstance(f, ast.Attribute) and f.attr == "execute":
            owner = f.value.id if isinstance(f.value, ast.Name) else None
            if owner in WATCHED:
                calls.append((owner, node, node.args))
            continue
        # Wrapped: _core_call(MiniMaxH3AddGuide, "what", **kw). The first two
        # positionals are the wrapper's own; everything for Core is keyword.
        if isinstance(f, ast.Name) and f.id == "_core_call" and node.args:
            first = node.args[0]
            owner = first.id if isinstance(first, ast.Name) else None
            if owner in WATCHED:
                calls.append((owner, node, node.args[2:]))

    ck("every watched Core node is actually called", len(calls) >= 3,
       f"{len(calls)} call site(s)")

    for owner, node, core_args in calls:
        line = node.lineno
        ck(f"{owner} @ :{line} passes nothing positionally",
           not core_args,
           f"{len(core_args)} positional arg(s)" if core_args
           else "order-independent")

        params = core_params(owner)
        if params is None:
            ck(f"{owner} exists in the installed Core", False, "class or execute missing")
            continue
        names = [kw.arg for kw in node.keywords if kw.arg]
        unknown = [n for n in names if n not in params]
        ck(f"{owner} @ :{line} uses only real parameter names",
           not unknown, f"unknown: {unknown}" if unknown else f"{len(names)} checked")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
