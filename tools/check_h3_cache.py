r"""The vendored H3 cache still agrees with the Core forward it copies.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_h3_cache.py

`h3_cache.py` is not this pack's design -- it is silveroxides' cache, taken by
way of PlagueKind's port and redistributed with permission (THIRD_PARTY_
NOTICES.md). Its `h3_cache_forward` is a reimplementation of Core's
`MiniMaxH3Model._forward` with one added block-loop boundary, because Core
exposes no replacement point around the complete block stack.

That is the whole reason this checker exists. A copied forward does not break
loudly when Core moves underneath it: it keeps running and returns something
subtly wrong. The specific failure has already happened once in the wild --
PlagueKind's port dropped the `cond_audio` segment kind, and every audio hop
died on `KeyError: 'cond_audio'`. That one at least raised. A new kind added to
Core, or a changed modality tag, would not.

So this compares the two segment tables that decide which timestep and which
modality tag every row of the packed sequence gets, against the Core installed
on this machine. It cannot prove the forward is correct on someone else's
build; it proves the pack is not silently running a stale copy of this one.

Attribution is checked too, in all three places the permission requires it.
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

VENDORED = os.path.join(HERE, "h3_cache.py")
NODE = os.path.join(HERE, "h3_cache_node.py")
NOTICES = os.path.join(HERE, "THIRD_PARTY_NOTICES.md")

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def read(path):
    return io.open(path, encoding="utf-8").read()


def dict_assign(tree, var):
    """The last `var = {...}` literal in a tree, as a plain dict.

    Last rather than first on purpose: Core builds `seg_t` once and then
    mutates individual entries, so an earlier partial literal would be the
    wrong thing to compare.
    """
    found = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == var:
                out = {}
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        out[k.value] = (v.value if isinstance(v, ast.Constant) else None)
                found = out
    return found


def appended_kinds(tree):
    """Every literal kind Core pushes into its segment table."""
    kinds = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "segments"
                and node.args
                and isinstance(node.args[0], ast.Tuple)
                and node.args[0].elts
                and isinstance(node.args[0].elts[0], ast.Constant)
                and isinstance(node.args[0].elts[0].value, str)):
            kinds.add(node.args[0].elts[0].value)
    return kinds


def main():
    print(__doc__.strip().splitlines()[0])
    print()

    try:
        from comfy.ldm.minimax import model as core_model
    except Exception as exc:                              # noqa: BLE001
        ck("Core's MiniMax H3 model imports", False, repr(exc))
        print("\n1 FAILURE(S): cannot compare without Core")
        return 1

    ck("Core still defines MiniMaxH3Model._forward", True,
       "the method this file reimplements")
    core_src = inspect.getsource(core_model)
    core_tree = ast.parse(core_src)
    vend_tree = ast.parse(read(VENDORED))

    print("\n  segment tables")
    core_t, vend_t = dict_assign(core_tree, "seg_t"), dict_assign(vend_tree, "seg_t")
    core_tag, vend_tag = dict_assign(core_tree, "seg_tag"), dict_assign(vend_tree, "seg_tag")

    ck("both files still declare a seg_t literal",
       core_t is not None and vend_t is not None,
       "core=%s vendored=%s" % (core_t is not None, vend_t is not None))
    ck("both files still declare a seg_tag literal",
       core_tag is not None and vend_tag is not None,
       "core=%s vendored=%s" % (core_tag is not None, vend_tag is not None))

    if core_t and vend_t:
        missing = sorted(set(core_t) - set(vend_t))
        extra = sorted(set(vend_t) - set(core_t))
        ck("seg_t handles every kind Core handles", not missing,
           "missing: %s" % missing if missing else "%d kinds" % len(core_t))
        ck("seg_t invents no kind Core does not have", not extra,
           "extra: %s" % extra if extra else "")

    if core_tag and vend_tag:
        # Values matter here, not just keys: the tag picks the modality row in
        # the t_emb table, so a wrong-but-present tag is silent corruption --
        # exactly the failure mode a copied forward has and Core does not.
        bad = sorted(k for k in core_tag if vend_tag.get(k) != core_tag[k])
        ck("seg_tag matches Core key for key", not bad,
           "differs: %s" % [(k, core_tag[k], vend_tag.get(k)) for k in bad]
           if bad else "%d tags identical" % len(core_tag))

    kinds = appended_kinds(core_tree)
    if kinds and vend_t:
        unknown = sorted(kinds - set(vend_t))
        ck("every kind Core emits has a vendored entry", not unknown,
           "unhandled: %s" % unknown if unknown else "%d emitted" % len(kinds))

    print("\n  the regression this pack already had to fix")
    src = read(VENDORED)
    ck("cond_audio is in seg_t", bool(vend_t) and "cond_audio" in vend_t)
    ck("cond_audio is in seg_tag", bool(vend_tag) and "cond_audio" in vend_tag)
    # The assignment, not the first mention -- the file's own header names
    # `has_aud_cond` a hundred lines earlier and a substring search finds that.
    aud = [ln for ln in src.splitlines() if ln.strip().startswith("has_aud_cond =")]
    ck("cond_audio is in the has_aud_cond test",
       bool(aud) and "cond_audio" in aud[0],
       "" if aud else "no `has_aud_cond =` assignment found")

    print("\n  double-patch guard")
    ck("the guard reads PlagueKind's owner key", "plaguekind_h3_minimax_cache" in src)
    ck("the guard reads UtilsCollection's owner key",
       "utilscollection_minimax_h3_cache" in src)
    ck("this pack writes its own owner key",
       'OWNER_KEY = "handtieclips_h3_minimax_cache"' in src)

    print("\n  attribution, which is the condition of the grant")
    node_src = read(NODE)
    ck("h3_cache.py header credits silveroxides", "silveroxides" in src[:4000])
    ck("THIRD_PARTY_NOTICES.md exists and credits silveroxides",
       os.path.exists(NOTICES) and "silveroxides" in read(NOTICES))
    ck("the node's own DESCRIPTION credits silveroxides",
       "silveroxides" in node_src)
    ck("the node is registered", "h3_cache_node" in read(os.path.join(HERE, "__init__.py")))

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
