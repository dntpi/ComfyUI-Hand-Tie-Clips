r"""Offline tests for the hop-cache latent sidecar -- no server, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_latent_sidecar.py

The sidecar used to be `torch.save`, so reading it back needed
`weights_only=False`: a pickle load of a file on disk. Nothing about this
payload requires that. `samples` is a `comfy.nested_tensor.NestedTensor`, and
`latents.parts()` already decomposes it into plain tensors because the pin
levers needed exactly that decomposition -- which is a safetensors payload
wearing a different name.

What has to hold for the swap to be safe is narrow and testable offline:

  * a restored latent is **bit-identical**, not merely close. A cached hop's
    latent becomes the next hop's Motion-Context pin, so a resumed chain that
    differs from an uninterrupted one would make the cache change the output,
    which is the one thing it must never do;
  * every member survives, including keys this file has never seen. Silently
    dropping one would restore a latent that is quietly not the one stored;
  * anything unrepresentable returns None rather than guessing, because the
    caller already has a correct answer for that -- cache the frames, skip the
    latent, fall back to the pixel pin;
  * the file on disk is really safetensors, with no pickle in it.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMFY = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, COMFY)

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-54s %s" % ("ok" if cond else "FAIL", name, detail))
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
    import torch
    import safetensors
    from safetensors.torch import save_file

    load_pack()
    store = sys.modules["htcpack.store"]
    to_flat, from_flat = store._latent_to_flat, store._latent_from_flat
    from comfy.nested_tensor import NestedTensor

    def roundtrip(latent, tmpdir):
        """Through a real file, not just in memory -- the format is the point."""
        packed = to_flat(latent)
        if packed is None:
            return None
        flat, meta = packed
        path = os.path.join(tmpdir, "s.safetensors")
        save_file(flat, path, metadata=meta)
        with safetensors.safe_open(path, framework="pt", device="cpu") as fh:
            got = {k: fh.get_tensor(k) for k in fh.keys()}
            hdr = fh.metadata()["htc"]
        return from_flat(got, hdr), path

    with tempfile.TemporaryDirectory() as td:
        print("nested samples (the shipped shape)")
        v = torch.randn(2, 4, 8, 8, dtype=torch.float32)
        a = torch.randn(2, 16, 40, dtype=torch.float32)
        out, path = roundtrip({"samples": NestedTensor([v, a])}, td)
        got = out["samples"].unbind()
        ck("container class is restored", type(out["samples"]) is NestedTensor,
           type(out["samples"]).__name__)
        ck("component count preserved", len(got) == 2, str(len(got)))
        ck("video component bit-identical", torch.equal(got[0], v),
           "a resumed chain must not diverge from an uninterrupted one")
        ck("audio component bit-identical", torch.equal(got[1], a))
        ck("dtype preserved", got[0].dtype == v.dtype and got[1].dtype == a.dtype)

        print("the file itself")
        head = open(path, "rb").read(4096)
        ck("no pickle opcode in the payload",
           b"ctorch" not in head and b"__reduce__" not in head
           and b"collections\nOrderedDict" not in head)
        with safetensors.safe_open(path, framework="pt", device="cpu") as fh:
            keys = sorted(fh.keys())
        ck("keys are flat and named", keys == ["samples.0", "samples.1"], str(keys))

        print("plain-tensor samples")
        t = torch.randn(1, 4, 8, 8)
        out2, _ = roundtrip({"samples": t}, td)
        ck("a bare tensor round-trips", isinstance(out2["samples"], torch.Tensor)
           and torch.equal(out2["samples"], t))

        print("other members of the latent dict")
        mask = torch.rand(1, 1, 8, 8)
        out3, _ = roundtrip(
            {"samples": NestedTensor([v, a]), "noise_mask": mask,
             "batch_index": 3, "label": "hop2", "flag": True, "nothing": None}, td)
        ck("a tensor member survives bit-identically",
           torch.equal(out3["noise_mask"], mask))
        ck("scalar members survive with their types",
           out3["batch_index"] == 3 and out3["label"] == "hop2"
           and out3["flag"] is True and out3["nothing"] is None,
           "int/str/bool/None")

        # The fixture above uses a PLAIN tensor mask. The code it guards makes a
        # NESTED one: a joint AV latent's mask carries a tensor per stream, so
        # master_audio_file sets NestedTensor((ones, zeros)). This checker passed
        # while every locked hop on a GPU logged "not representable without
        # pickling" and cached no latent at all. A fixture that does not match
        # what the code produces is not a test.
        print("a NESTED member -- the shape master_audio_file really sets")
        vm = torch.ones((1, 1) + tuple(v.shape[2:]))
        am = torch.zeros((1, 1) + tuple(a.shape[2:]))
        real = {"samples": NestedTensor([v, a]),
                "noise_mask": NestedTensor([vm, am])}
        ck("a locked latent is representable", to_flat(real) is not None,
           "master_audio_file + cache_hops=on")
        out4, _ = roundtrip(real, td)
        ck("the nested mask comes back nested",
           type(out4["noise_mask"]).__name__ == "NestedTensor")
        got = list(out4["noise_mask"].unbind())
        ck("both streams survive bit-identically",
           torch.equal(got[0], vm) and torch.equal(got[1], am),
           "ones on video, zeros on audio")
        ck("the samples beside it are still intact",
           torch.equal(list(out4["samples"].unbind())[0], v))

        # A cache carried across the 1.1 upgrade has a .latent.pt beside every
        # entry. It is never read -- that would be the pickle this pack retired
        # -- but the sweep has to SEE it, or it is invisible to the budget and
        # survives the eviction of the entry it belongs to. ~15 MB each,
        # forever. Exactly the shape of the master-spill leak.
        print("the pickle-era sidecar is accounted for, never read")
        ck("the legacy extension is still named",
           getattr(store, "LEGACY_LATENT_EXT", None) == ".latent.pt")
        with open(os.path.join(HERE, "store.py"), encoding="utf-8") as _fh:
            src = _fh.read()
        entries = src[src.index("def entries("):src.index("def sweep(")]
        sweep = src[src.index("def sweep("):]
        ck("the budget counts it", "LEGACY_LATENT_EXT" in entries,
           "or the cache silently exceeds its own budget")
        ck("eviction removes it", "LEGACY_LATENT_EXT" in sweep,
           "or it outlives the entry it belongs to")
        ck("nothing ever reads it",
           "LEGACY_LATENT_EXT" not in src[src.index("def get_latent("):
                                          src.index("def entries(")]
           if "def get_latent(" in src else True,
           "reading it back would need weights_only=False")

        print("refusals -- None means cache the frames, skip the latent")
        ck("a dict with no samples is refused", to_flat({"x": 1}) is None)
        ck("a non-dict is refused", to_flat(torch.randn(2)) is None)
        ck("an unrecognised samples container is refused",
           to_flat({"samples": object()}) is None,
           "parts() returns None and is not second-guessed")
        ck("an unrepresentable member is refused rather than dropped",
           to_flat({"samples": t, "cb": (lambda: None)}) is None,
           "restoring a latent minus a member is worse than not caching it")

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
