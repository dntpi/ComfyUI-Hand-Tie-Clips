r"""Offline checks for the retake timeline -- no server, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_timeline.py

What has to hold:

  * A cached hop records the predecessor it was generated against
    (`prev_key` in its meta), and the store hands that back without decoding
    frames. That is what lets `run()` say, exactly, when a locked hop is
    served next to a hop it was never generated with -- the join the pack
    otherwise has no way to warn about.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import importlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-58s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def pack(mod):
    pkg = types.ModuleType("htc_tl")
    pkg.__path__ = [HERE]
    sys.modules.setdefault("htc_tl", pkg)
    return importlib.import_module("htc_tl." + mod)


def test_prev_key_meta():
    import torch
    store = pack("store")
    with tempfile.TemporaryDirectory() as root:
        st = store.HopStore(root)
        imgs = torch.rand(5, 32, 32, 3)
        wav = torch.zeros(1, 2, 1600)
        st.put("k2", imgs, wav, 16000, {"hop": 2, "prev_key": "k1"})
        st.put("k3", imgs, wav, 16000, {"hop": 3, "prev_key": None})
        ck("meta hands back the predecessor", st.meta("k2").get("prev_key") == "k1")
        ck("a restart's predecessor is recorded as None",
           "prev_key" in st.meta("k3") and st.meta("k3")["prev_key"] is None)
        ck("a missing entry has no meta", st.meta("nope") is None)


def test_run_records_and_compares():
    src = open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    ck("run() stores prev_key with every cached hop",
       '"prev_key": None if hop_restart else prev_key' in src)
    ck("the locked branch compares it against this run's predecessor",
       '_pm["prev_key"] != prev_key' in src and "unpaired_joins.append(i + 1)" in src)
    ck("info names the unpaired joins", '"unpaired joins: "' in src)


def main():
    print("check_timeline")
    test_prev_key_meta()
    test_run_records_and_compares()
    if FAIL:
        print(f"{len(FAIL)} check(s) failed")
        sys.exit(1)
    print("all timeline checks passed")


if __name__ == "__main__":
    main()
