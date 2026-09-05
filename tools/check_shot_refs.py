r"""Offline tests for shot-level refs -- no server, no model, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_shot_refs.py

Identity stills on a pin-less hop can win the middle of it (restart hop 4
of the tester chain). Silently dropping them on restart was the old
accident and is not obviously right. shot.refs is the choice:

  * omitted  -- register default (unscheduled stills on starts, off
                continuations under hop_script=next)
  * []       -- none, including on a restart
  * [tags]   -- those stills only, in that order

The editor used to destroy this field on any card edit. parsePlan and
planToJson must round-trip both a filled list and the empty list.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


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


def raises(fn, needle):
    try:
        fn()
    except ValueError as e:
        return needle.lower() in str(e).lower()
    except Exception:
        return False
    return False


def main():
    load_pack()
    P = sys.modules["htcpack.plan"]
    R = sys.modules["htcpack.refs"]

    print("the field parses")
    omitted = P.parse_plan(json.dumps({"shots": [{"beat": "x"}]}))
    ck("omitted becomes None, not []", omitted[0]["refs"] is None)
    empty = P.parse_plan(json.dumps({"shots": [{"beat": "x", "refs": []}]}))
    ck("[] survives as []", empty[0]["refs"] == [])
    filled = P.parse_plan(json.dumps(
        {"shots": [{"beat": "x", "refs": ["@her_front", "studio"]}]}))
    ck("@ stripped, order kept", filled[0]["refs"] == ["her_front", "studio"])
    ck("unknown field is no longer how this is refused",
       "refs" in P._SHOT_KEYS)

    print("queue-fail")
    rail = [{"tag": "her_front", "file": "a.png", "shots": None},
            {"tag": "studio", "file": "b.png", "shots": None}]
    ck("omitted with no register is fine",
       P.validate_shot_refs([{"refs": None}], []) is None)
    ck("[] with no register is fine",
       P.validate_shot_refs([{"refs": []}], []) is None)
    ck("a tag with no register is refused",
       raises(lambda: P.validate_shot_refs(
           [{"refs": ["her_front"]}], []), "register"))
    ck("an unknown tag is refused by name",
       raises(lambda: P.validate_shot_refs(
           [{"refs": ["ghost"]}], rail), "ghost"))
    ck("a known tag is fine",
       P.validate_shot_refs([{"refs": ["studio"]}], rail) is None)

    print("select_for_shot is shot order, not rail order")
    refs = [
        {"tag": "her_front", "slot": 1, "shots": None},
        {"tag": "studio", "slot": 2, "shots": None},
    ]
    got = R.select_for_shot(refs, ["studio", "her_front"], {1, 2})
    ck("studio can be Picture 1",
       [r["tag"] for r in got] == ["studio", "her_front"])
    ck("[] selects nothing",
       R.select_for_shot(refs, [], {1, 2}) == [])
    ck("a missing file is skipped, not invented",
       [r["tag"] for r in R.select_for_shot(refs, ["studio"], {1})] == [])

    print("default path is unchanged")
    # Unscheduled stills ride hop 0 (a start) and are dropped on hop 1
    # under hop_script=next -- that is active_refs plus the continue
    # filter, which is the omitted-field behaviour.
    hop0 = R.active_refs(refs, 0, {1, 2})
    ck("unscheduled stills ride hop 1",
       [r["tag"] for r in hop0] == ["her_front", "studio"])

    print("frontend round-trips [] and a filled list")
    js = io.open(os.path.join(HERE, "js", "editor", "plan_editor.js"),
                 encoding="utf-8").read()
    ck("parsePlan reads refs", "s.refs" in js and "Array.isArray(s.refs)" in js)
    ck("planToJson writes an empty list, not only a filled one",
       "if (Array.isArray(s.refs)) o.refs = s.refs" in js)
    ck("the card has a refs field", 'el("span", null, "refs")' in js)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
