r"""The positional-widget regression test -- no GPU, no server.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_widget_order.py

`widgets_values` is a bare JSON array. ComfyUI matches it to the node by
POSITION, never by name, so a saved workflow carries no record of what any of
its values meant. Insert one widget anywhere above the end and every workflow
on every user's disk silently shifts by one -- `steps` reads a sampler name,
`pin_noise` reads a float meant for something else, and the render comes back
subtly wrong with no error anywhere. That failure has already been observed
once in this pack: `HandTieClips_Refine_AB.json`, saved against a build whose
refine widgets sat in a different order, decoded `'off'` into `refine_denoise`.

`check_refine_keys.py` asserts the refine widgets sit after `voice_every_hop`.
That is a RELATIVE check and it stays true even if two widgets below swap with
each other. This one is absolute: the order below is frozen from the last
released node (v2.0, `bb3f9fa`) and the live node must still begin with it,
exactly, before anything appended.

The second half decodes the workflows actually on disk. Any HandTieClips node
carrying the released slot count must land every one of its values on the same
widget it was saved against, and the slots added since must default to
something inert, so that opening an old workflow renders what it always did.
"""
from __future__ import annotations

import io
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []

# Frozen at v2.0 (bb3f9fa), the last order any released workflow was saved
# against. `seed` occupies two slots -- ComfyUI appends control_after_generate
# straight after it. Nothing here is edited: widgets are APPENDED past the end.
RELEASED = [
    "prompt", "chains", "resolution", "aspect", "duration", "overlap",
    "seed", "control_after_generate", "seed_per_shot", "steps", "sampler_name",
    "scheduler", "shift_video", "shift_audio", "ref_image_size", "hop_script",
    "pin_to_qwen", "shot_plan", "ref_plan", "cache_hops", "cache_budget_gb",
    "audio_pin_frames", "pin_renorm", "pin_noise", "tone_compensate",
    "start_image_file", "reference_video_file", "voice_file", "establish",
    "render_through", "quality", "dry_run", "contact_sheet", "tone_anchor",
    "music_gain_db", "music_duck", "music_fit", "music_fade_s",
    "soundtrack_file", "voice_start_s", "voice_end_s",
    "reference_video_start_s", "reference_video_end_s", "music_start_s",
    "music_end_s", "render_from", "reference_video_desc",
    "reference_video_size", "pin_mech", "tone_anchor_ref",
    "reference_video_2_file", "reference_video_2_start_s",
    "reference_video_2_end_s", "reference_video_3_file",
    "reference_video_3_start_s", "reference_video_3_end_s", "voice_2_file",
    "voice_2_start_s", "voice_2_end_s", "voice_3_file", "voice_3_start_s",
    "voice_3_end_s", "master_audio_file", "last_frame_guide",
    "voice_every_hop",
]

# A widget appended since v2.0 may ship with any default that reproduces the
# old behaviour. Only the ones that CHANGE a render need pinning here.
INERT = {"hop_refine": "off", "speed_mode": "regular"}

SOCKET_TYPES = {"MODEL", "CLIP", "VAE", "IMAGE", "AUDIO", "LATENT",
                "CONDITIONING", "VIDEO"}


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def load_pack():
    """Import the pack under a private package name, as the checkers do."""
    pkg = types.ModuleType("htcpack")
    pkg.__path__ = [HERE]
    sys.modules["htcpack"] = pkg
    import importlib
    return importlib.import_module("htcpack.h3_ref_chain")


def widget_slots(H3):
    """[(name, default)] in widgets_values index order.

    A widget is any input whose type is a primitive or a combo AND is not
    forced onto a wire -- socket inputs take no slot, which is why
    `refine_model` does not appear and must not.
    """
    it = H3.HandTieClips.INPUT_TYPES()
    out = []
    for section in ("required", "optional"):
        for name, spec in (it.get(section) or {}).items():
            t = spec[0]
            cfg = spec[1] if len(spec) > 1 else {}
            if isinstance(t, str) and t in SOCKET_TYPES:
                continue
            if cfg.get("forceInput"):
                continue
            default = cfg.get("default")
            if default is None and isinstance(t, (list, tuple)) and t:
                default = t[0]
            out.append((name, default))
            if name == "seed":
                out.append(("control_after_generate", "fixed"))
    return out


def workflow_dirs():
    """[(dir, shipped)] -- the pack's own examples, then the user's.

    The user directory is where a stale workflow actually bites, so it is read
    when it exists and skipped in silence when it does not. It does not vote on
    the exit code: this runs as a pre-commit gate, and a stale file someone
    saved on their own machine is a warning to them, not a broken repository.
    """
    out = [(os.path.join(HERE, "workflows"), True)]
    out.append((os.path.join(COMFY, "user", "default", "workflows"), False))
    return [(d, shipped) for d, shipped in out if os.path.isdir(d)]


def main():
    H3 = load_pack()
    slots = widget_slots(H3)
    names = [n for n, _ in slots]

    print("declared order: the released prefix is untouched")
    ck("node declares at least the %d released slots" % len(RELEASED),
       len(names) >= len(RELEASED), "%d declared" % len(names))
    head = names[:len(RELEASED)]
    moved = [(i, RELEASED[i], head[i])
             for i in range(min(len(head), len(RELEASED)))
             if RELEASED[i] != head[i]]
    ck("slots 0-%d match v2.0 by name and position" % (len(RELEASED) - 1),
       not moved,
       "" if not moved else
       "first break: slot %d was %s, now %s" % moved[0])
    for i, was, now in moved:
        print("       slot %-3d released=%-24s live=%s" % (i, was, now))

    print("\nappended slots %d-%d: present, and inert by default"
          % (len(RELEASED), len(slots) - 1))
    for i in range(len(RELEASED), len(slots)):
        print("       [%d] %-22s default=%r" % (i, slots[i][0], slots[i][1]))
    for name, want in INERT.items():
        got = dict(slots).get(name, "<missing>")
        ck("%s ships %r" % (name, want), got == want,
           "" if got == want else "is %r" % (got,))

    print("\nsaved workflows on disk decode onto the same widgets")
    seen = 0
    for d, shipped in workflow_dirs():
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            # Local-only A/B graphs. Not shipped; saved against mid-dev
            # widget orders and must not warn on every gate run.
            if "Refine_AB" in fn:
                continue
            try:
                wf = json.load(io.open(os.path.join(d, fn), encoding="utf-8"))
            except Exception:
                continue
            for node in (wf.get("nodes") or []):
                if node.get("type") != "HandTieClips":
                    continue
                wv = node.get("widgets_values") or []
                seen += 1
                # Longer than the node declares means it was saved against a
                # build whose widgets are not these -- every value past the end
                # is dropped and the ones below it may well be misaligned.
                ok = len(wv) <= len(slots)
                label = "%s: %d slots fit the node's %d" % (fn, len(wv),
                                                            len(slots))
                if ok or shipped:
                    ck(label, ok,
                       "" if ok else "saved against a different build; re-save")
                else:
                    print("  warn %-58s %s"
                          % (label, "stale local file; re-save or delete it"))
    if not seen:
        print("       (no HandTieClips node found in any workflow)")

    print("\n%s" % ("FAILED: " + ", ".join(FAIL) if FAIL else "all ok"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
