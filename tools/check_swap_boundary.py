r"""SWAP is an implant. This file is the artefact that keeps it one.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_swap_boundary.py

CLAUDE.md: SWAP may write shot_plan (exactly one shot) and
reference_video_desc, may bind MEDIA clip slot 1, and may set that shot's
refs to tags that already exist on the rail. It must not write ref_plan,
must not add, remove or rewrite rail rows, must not share WRITE's system
prompt, and must not Accept over a multi-shot plan without an explicit
replace.

Clauses this file can enforce statically or with a scripted fake:

  * SWAP-owned files do not import or name the render-path modules.
  * write_swap_plan never returns a ref_plan key, even when the model emits one.
  * validate_swap refuses a two-shot plan.
  * video_swap.js never writes the rail and refuses a draft that is not one shot.

Clauses it cannot: Accept-over-multi-shot is a runtime confirm in the DOM
(the SCRIPT widget's current JSON); LLM conn sharing with WRITE; MEDIA
display desync (stage 5, 2.1). Those gaps are named so they are not assumed
covered.

A checker that passes both with and without the fix is not a checker. This
file's forbidden-import assertion is the one to falsify: add `tone` to a
SWAP-owned file, watch it fail, revert.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []

SWAP_OWNED = [
    os.path.join("js", "editor", "video_swap.js"),
    os.path.join("prompt_pack", "SWAP_PROMPT.md"),
]

# Render-path modules. If a SWAP change needs one of these, it is not a SWAP
# change.
# Filenames / imports, not English words: SWAP_PROMPT says "room tone".
FORBIDDEN = (
    "h3_ref_chain.py", "audio_lock.py", "tone.py", "store.py", "latents.py",
    "seam.py", "sheet.py",
    "from . import tone", "from . import audio_lock", "from . import store",
    "from . import latents", "from . import seam", "from . import sheet",
    "from . import h3_ref_chain",
)


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
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


def main():
    print("SWAP-owned files stay off the render path")
    for rel in SWAP_OWNED:
        path = os.path.join(HERE, rel)
        ck(rel + " exists", os.path.isfile(path), path)
        if not os.path.isfile(path):
            continue
        text = io.open(path, encoding="utf-8").read()
        hits = [n for n in FORBIDDEN if n in text]
        ck(rel + " names no render-path module",
           not hits,
           ("SWAP may not import %s; if this change is about that "
            "module it is not a SWAP change" % ", ".join(hits)) if hits else "")

    print("writer never emits ref_plan")
    load_pack()
    import htcpack.planner as P  # noqa: E402

    async def fake_with_register(messages, schema=None):
        return json.dumps({
            "shot_plan": {"shots": [{
                "beat": "She stands in the room like @her_face and turns.",
                "directives": {"camera": "hold", "framing": "medium",
                               "tail": "settle"},
                "refs": ["her_face"],
            }]},
            "ref_plan": {"refs": [{"tag": "ghost", "file": "ghost.png"}]},
        })

    out = asyncio.run(P.write_swap_plan(
        "", complete_fn=fake_with_register, identity_tag="her_face",
        rail_tags=["her_face"]))
    ck("write_swap_plan has no ref_plan key",
       "ref_plan" not in out,
       "SWAP must not write the rail; drop the key, do not merge it")
    ck("the model's register document was discarded",
       "ghost" not in json.dumps(out.get("shot_plan") or ""),
       "a ref_plan in the reply is not an invitation to keep it")
    ck("the one hop survived",
       out.get("ok") is True and "her_face" in (out.get("shot_plan") or ""))

    two = json.dumps({"shots": [
        {"beat": "A cites @her_face."},
        {"beat": "B cites @her_face."},
    ]})
    errs, _ = P.validate_swap(two, rail_tags=["her_face"], identity_tag="her_face")
    ck("two shots are refused",
       any("one hop" in e.lower() or "length 1" in e.lower() for e in errs),
       "; ".join(errs) or "validate_swap accepted a chain")

    print("video_swap.js never writes the rail and Accepts one shot")
    js = io.open(os.path.join(HERE, "js", "editor", "video_swap.js"),
                 encoding="utf-8").read()
    ck("video_swap.js reads the rail, it does not write it",
       "parseRefPlan" in js and "refPlanToJson" not in js
       and "refWidget" not in js,
       "parseRefPlan is how SWAP lists identity tags. refPlanToJson is the "
       "rail's write -- SWAP must not call it.")
    ck("video_swap.js does not call refPlanToJson",
       "refPlanToJson" not in js,
       "that is the rail's write/commit. SWAP Accepts a shot, not a register.")
    ck("Accept requires shots.length === 1",
       "shots.length !== 1" in js or "shots.length != 1" in js,
       "SWAP Accept must refuse a draft that is not one shot")
    ck("Accept refuses a multi-shot SCRIPT",
       "already has" in js and "one hop" in js,
       "SWAP must not silently replace a chain")

    ui = io.open(os.path.join(HERE, "js", "h3_ref_chain_ui.js"),
                 encoding="utf-8").read()
    # The SWAP onWritten is the createVideoSwap callback. It must not assign
    # refWidget -- that assignment is WRITE's.
    swap_cb = ui.split("const videoSwap = createVideoSwap")[-1].split("const tabs")[0] if "const videoSwap = createVideoSwap" in ui else ""
    ck("SWAP onWritten does not assign refWidget",
       "refWidget" not in swap_cb,
       "WRITE owns ref_plan. SWAP's Accept callback writes shot_plan only.")

    src = inspect.getsource(P.write_swap_plan)
    ck("write_swap_plan does not put a ref_plan field on the result",
       '"ref_plan":' not in src and "'ref_plan':" not in src,
       "do not add a ref_plan key to SWAP's return; the rail is not SWAP's")

    print("SWAP prompt is not WRITE's")
    ck("swap_prompt is not system_prompt",
       P.swap_prompt() != P.system_prompt())
    ck("SWAP instruct forbids a register document",
       "ref_plan" in P.swap_prompt() and "Do not emit" in P.swap_prompt())

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
