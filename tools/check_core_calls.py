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
WATCHED = {"MiniMaxH3ReferenceToVideo", "MiniMaxH3AddGuide", "MiniMaxH3ImageToVideo"}


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def core_params(cls_name):
    from comfy_extras import nodes_minimax_h3 as core
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
        if not (isinstance(f, ast.Attribute) and f.attr == "execute"):
            continue
        owner = f.value.id if isinstance(f.value, ast.Name) else None
        if owner in WATCHED:
            calls.append((owner, node))

    ck("every watched Core node is actually called", len(calls) >= 3,
       f"{len(calls)} call site(s)")

    for owner, node in calls:
        line = node.lineno
        ck(f"{owner} @ :{line} passes nothing positionally",
           not node.args,
           f"{len(node.args)} positional arg(s)" if node.args
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
