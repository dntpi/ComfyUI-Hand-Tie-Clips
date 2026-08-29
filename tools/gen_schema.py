"""Generate `prompt_pack/SCHEMA.json` from the node's own constants.

A hand-written schema is a second source of truth that starts drifting the day
someone adds a camera move. This one reads `directives.VOCAB`, `refs.RETENTION`,
`plan._SHOT_KEYS` and the duration table directly, so it can only ever describe
the node that is actually installed.

    python tools/gen_schema.py            # write prompt_pack/SCHEMA.json
    python tools/gen_schema.py --check    # exit 1 if it is out of date (CI)

Run it with ComfyUI's interpreter -- importing the node pulls in torch.
"""
import argparse
import io
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "prompt_pack", "SCHEMA.json")


def _load():
    """Import the pack as a package without needing it to be installed."""
    root = os.path.dirname(os.path.dirname(HERE))   # .../ComfyUI
    if root not in sys.path:
        sys.path.insert(0, root)
    name = "_h3schema"
    pkg = types.ModuleType(name)
    pkg.__path__ = [HERE]
    sys.modules[name] = pkg
    mod = __import__(name + ".directives", fromlist=["directives"])
    d = sys.modules[name + ".directives"]
    r = __import__(name + ".refs", fromlist=["refs"])
    r = sys.modules[name + ".refs"]
    p = __import__(name + ".plan", fromlist=["plan"])
    p = sys.modules[name + ".plan"]
    return d, r, p, mod


def build():
    d, r, p, _ = _load()

    # The duration labels, read from the node so the two cannot disagree. The
    # import is late and guarded because it pulls torch in.
    try:
        from _h3schema import h3_ref_chain as node   # noqa: F401
        durations = list(node.DURATION_FRAMES)
        frames = dict(node.DURATION_FRAMES)
    except Exception:                                # pragma: no cover
        durations, frames = [], {}

    directives = {
        axis: {
            "type": "string",
            "enum": sorted(vals),
            "description": " | ".join(f"{k}: {v}" for k, v in vals.items()),
        }
        for axis, vals in d.VOCAB.items()
    }
    for axis, spec in directives.items():
        if axis in d.DEFAULTS:
            spec["default"] = d.DEFAULTS[axis]

    shot = {
        "type": "object",
        "required": ["beat"],
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string",
                   "description": "Stable name. Generated if absent."},
            "beat": {"type": "string",
                     "description": "What happens THIS hop. Shot 1 is the whole "
                                    "opening; every later shot is only the new "
                                    "beat."},
            "directives": {
                "type": "object", "additionalProperties": False,
                "properties": directives,
                "description": "Compiled in the order " + ", ".join(d.AXES) +
                               ". `join` is ignored on shot 1.",
            },
            "prose": {"type": "string",
                      "description": "Appended verbatim, for anything the "
                                     "vocabulary lacks. Phrase it affirmatively."},
            "seed": {"type": "integer"},
            "steps": {"type": "integer"},
            "duration": {"type": "string", "enum": durations},
            "locked": {"type": "boolean",
                       "description": "Reuse this hop's cached render even when "
                                      "its inputs changed. Needs cache_hops=on."},
            "tone": {"type": "string", "enum": ["free", "rebase"],
                     "description": "Opt out of tone_compensate=anchor's pull "
                                    "toward hop 1's exposure. 'free' skips it "
                                    "for this hop; 'rebase' also moves the "
                                    "anchor onto this hop, for a scene that is "
                                    "deliberately darker or brighter from here "
                                    "on. Omit it unless that is the case."},
        },
    }
    assert set(shot["properties"]) == set(p._SHOT_KEYS), (
        "schema and plan._SHOT_KEYS disagree: "
        f"{sorted(set(shot['properties']) ^ set(p._SHOT_KEYS))}")

    ref = {
        "type": "object",
        "required": ["tag"],
        "additionalProperties": False,
        "properties": {
            "tag": {"type": "string", "pattern": "^[A-Za-z0-9_]+$",
                    "description": "Unique. Written into beats as @tag."},
            "file": {"type": "string",
                     "description": "Basename under ComfyUI/input/h3_refs. "
                                    "Never a path."},
            "slot": {"type": "integer",
                     "description": "Derived from list position. Do not author "
                                    "it; present only for legacy plans."},
            "subject": {"type": "integer", "minimum": 1,
                        "description": "Groups pictures OF THE SAME PERSON. Two "
                                       "different people under one number makes "
                                       "the model render the average of their "
                                       "faces."},
            "retention": {"type": "string", "enum": sorted(r.RETENTION),
                          "description": " | ".join(f"{k}: {v}" for k, v
                                                    in r.RETENTION.items())},
            "desc": {"type": "string"},
            "shots": {
                "type": "array", "items": {"type": "integer", "minimum": 1},
                "minItems": 1,
                "description": "1-based hops this picture rides on. OMITTING IT "
                               "MEANS HOP 1 ONLY on a continuation chain -- a "
                               "plate shot elsewhere riding a later hop beats "
                               "the frame pin.",
            },
        },
    }
    assert set(ref["properties"]) == set(r.REF_FIELDS), (
        "schema and refs.REF_FIELDS disagree: "
        f"{sorted(set(ref['properties']) ^ set(r.REF_FIELDS))}")

    subject = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "locked": {"type": "string",
                       "description": "What must not change. This is what "
                                      "survives on a hop where the photograph "
                                      "is absent."},
            "context": {"type": "string",
                        "description": "Situational state that should persist "
                                       "-- wardrobe, what they are holding."},
        },
    }
    assert set(subject["properties"]) == set(r.SUBJECT_FIELDS)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "H3 Ref Chain plans",
        "description":
            "Two documents. `shot_plan` goes in the shot_plan widget, `ref_plan` "
            "in the ref_plan widget. Generated by tools/gen_schema.py from the "
            "installed node -- do not edit by hand.",
        "type": "object",
        "properties": {
            "shot_plan": {
                "type": "object",
                "required": ["shots"],
                "additionalProperties": False,
                "properties": {
                    "shots": {"type": "array", "minItems": 1, "items": shot,
                              "description": "One shot per hop. The shot count "
                                             "IS the hop count."},
                },
            },
            "ref_plan": {
                "type": "object",
                "required": ["refs"],
                "additionalProperties": False,
                "properties": {
                    "refs": {"type": "array", "items": ref,
                             "maxItems": r.MAX_REF_IMAGES,
                             "description": f"At most {r.MAX_REF_IMAGES} "
                                            f"pictures on any one hop."},
                    "subjects": {
                        "type": "object",
                        "patternProperties": {"^[0-9]+$": subject},
                        "additionalProperties": False,
                        "description": "Keyed by subject number, as a STRING. "
                                       "Every subject here must be claimed by "
                                       "at least one ref, or the plan is "
                                       "rejected.",
                    },
                },
            },
        },
        "x-duration-frames": frames,
        "x-rules": [
            "cfg is 1.0 with no negative branch: the prompt is ADDITIVE. "
            "Anything named is added; nothing can be removed by mentioning it. "
            "Never write a negation.",
            "Never name the thing you want to end. 'stops talking' keeps them "
            "talking. Write the state as a pose plus a sound.",
            "H3 always generates audio. Silence must be written AS a sound -- "
            "room tone, a refrigerator, a single click.",
            "Ambience must be narrowband. 'faint street noise' renders as hiss; "
            "'the low hum of the refrigerator' renders as a refrigerator.",
            "A state change belongs at the END of the previous shot. Every hop "
            "opens holding the frames it was handed.",
            "Shot 1 establishes everything. Later shots carry ONLY the new "
            "beat -- never re-describe the face, the clothes or the room.",
            "Write @tags into the action line. Describing a reference in the "
            "register does not make the model use it.",
            "Set `tail` to settle or hold on the FINAL shot, or the model is "
            "told action is still underway at the last frame and invents "
            "something to satisfy it.",
        ],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the file on disk is out of date")
    args = ap.parse_args()

    text = json.dumps(build(), indent=2, ensure_ascii=False) + "\n"

    if args.check:
        try:
            have = io.open(OUT, encoding="utf-8").read()
        except OSError:
            print("SCHEMA.json is missing")
            return 1
        if have != text:
            print("SCHEMA.json is out of date; run tools/gen_schema.py")
            return 1
        print("SCHEMA.json is current")
        return 0

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(text)
    print("wrote %s (%d bytes)" % (OUT, len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
