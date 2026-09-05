r"""Offline tests for the spilled master frame buffer -- no server, no GPU.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_master_spill.py

The master is allocated once at full chain length and then sits resident and
inactive through every sampling pass -- ~31 GB on an 8 x 15 s chain at 1280x736,
competing with the DiT, the VAE decode buffers, `imgs` and `prev_imgs`. Above
`MASTER_SPILL_BYTES` it is memory-mapped instead.

The win itself is not testable here: whether the OS actually evicts pages under
pressure only shows on a chain long enough to hurt, and that needs a GPU and an
hour. What IS testable offline is the part that would be a silent disaster --
that the spilled buffer behaves exactly like the RAM one for every operation
`run()` performs on it. The frames it hands back must be bit-identical, because
the alternative is a chain whose pixels depend on how much RAM the machine had.

So this exercises the real access pattern rather than a toy: the hop-0 write at
`[0:k]`, the later `[write_pos:write_pos+keep]` writes, the short-chain trim,
the `[-1]` preview read, and the shape reads around them.
"""
from __future__ import annotations

import gc
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


def _spills(root):
    return {n for n in os.listdir(root)
            if n.startswith("htc_master_") and n.endswith(".raw")}


def main():
    import torch

    load_pack()
    H3 = sys.modules["htcpack.h3_ref_chain"]

    # This checker makes real mappings, so it cleans up after itself rather
    # than leaving them for the next run's sweep. Anything already present
    # belongs to somebody else and is left alone.
    import folder_paths
    _root = folder_paths.get_temp_directory()
    os.makedirs(_root, exist_ok=True)
    _pre = _spills(_root)

    frames, h, w = 40, 16, 24
    overlap, keep = 6, 10

    def drive(buf):
        """Exactly what run() does to the master, in order."""
        torch.manual_seed(0)
        first = torch.rand(16, h, w, 3)
        buf[0:first.shape[0]] = first
        pos = first.shape[0]
        for _ in range(2):
            hop = torch.rand(overlap + keep, h, w, 3)
            buf[pos:pos + keep] = hop[overlap:]
            pos += keep
        return buf[:pos], pos

    print("RAM path and spilled path agree")
    # MASTER_DTYPE on both sides on purpose: this pair asks whether the mapping
    # behaves like RAM, not what the dtype is. That is asserted separately below.
    ram = torch.empty((frames, h, w, 3), dtype=H3.MASTER_DTYPE)
    a, pos_a = drive(ram)

    # The master is delivery-only, so it is fp16: prev_imgs is cloned from imgs
    # and nothing in the conditioning path reads the master back. fp16 and not
    # bf16 because the buffer is clamped to 0..1 -- mantissa bits are the whole
    # question and bf16's seven give exactly 8-bit output precision with nothing
    # in reserve.
    ck("the master is fp16, not fp32", H3.MASTER_DTYPE == torch.float16,
       str(H3.MASTER_DTYPE))
    ck("and not bf16, which would band the highlights",
       H3.MASTER_DTYPE != torch.bfloat16)
    # What fp16 actually costs at delivery, measured rather than asserted from
    # the mantissa argument. It is NOT bit-identical: the encode truncates
    # (`(x * 255).astype(uint8)`, core nodes.py), so a value that fp16 rounds
    # down across an integer boundary loses one 255th. Measured over 2M random
    # samples: ~2.1% of pixels move, every one of them by exactly 1, none by
    # more. That is a quarter of the h264 encode's own error and invisible; the
    # bound is what matters, so the bound is what is checked.
    torch.manual_seed(0)
    probe = torch.rand(200000, dtype=torch.float32)
    as_master = probe.to(H3.MASTER_DTYPE).float()
    to_byte = lambda t: (t * 255.0).clip(0, 255).to(torch.uint8)  # noqa: E731
    delta = (to_byte(probe).int() - to_byte(as_master).int()).abs()
    ck("fp16 never moves a delivered pixel by more than 1/255",
       int(delta.max()) <= 1, "max %d" % int(delta.max()))
    ck("and moves fewer than 5% of them at all",
       float((delta > 0).float().mean()) < 0.05,
       "%.2f%% by one 255th" % (100.0 * float((delta > 0).float().mean())))

    ck("a whole 8x15s 1280x736 master is under 16 GB",
       (2742 * 736 * 1280 * 3 * torch.finfo(H3.MASTER_DTYPE).bits // 8)
       / 2**30 < 16.0,
       "%.1f GB" % ((2742 * 736 * 1280 * 3
                     * torch.finfo(H3.MASTER_DTYPE).bits // 8) / 2**30))

    big = H3.MASTER_SPILL_BYTES
    try:
        H3.MASTER_SPILL_BYTES = 0          # force the mapping regardless of size
        spilled = H3._alloc_master(frames, h, w)
        ck("the spilled buffer matches the master dtype and shape",
           tuple(spilled.shape) == (frames, h, w, 3)
           and spilled.dtype == H3.MASTER_DTYPE,
           f"{tuple(spilled.shape)} {spilled.dtype}")
        b, pos_b = drive(spilled)
        ck("the same writes land at the same positions", pos_a == pos_b,
           f"{pos_a} vs {pos_b}")
        ck("delivered frames are bit-identical", torch.equal(a, b),
           "pixels must not depend on how much RAM the machine had")
        ck("the trim yields a real view", b.shape[0] == pos_b, str(b.shape[0]))
        ck("the preview read works on the mapping",
           torch.equal(b[-1], a[-1]), "master_imgs[-1] feeds the final preview")
        ck("a slice survives conversion for the encoder",
           torch.equal(b[:3].contiguous().cpu(), a[:3]))
    finally:
        H3.MASTER_SPILL_BYTES = big

    print("choosing a path")
    small = H3._alloc_master(4, 8, 8)
    ck("a small chain stays in RAM", small.numel() == 4 * 8 * 8 * 3,
       "under MASTER_SPILL_BYTES")
    ck("the threshold is a real number and not zero",
       isinstance(H3.MASTER_SPILL_BYTES, int) and H3.MASTER_SPILL_BYTES > 0,
       f"{H3.MASTER_SPILL_BYTES / 2**30:.0f} GiB")

    print("the spill file cleans up after itself")
    # The bug this exists to prevent: the first version wrote an ordinary file
    # and relied on the NEXT run sweeping it. ComfyUI holds the previous run's
    # IMAGE output, so the mapping was still open, os.remove raised, and the
    # handler passed silently -- 9 GB per render, unnoticed. Delete-on-close
    # makes the lifetime automatic, and this asserts BOTH halves of it.
    before = _spills(_root)
    H3.MASTER_SPILL_BYTES = 0
    try:
        live = H3._alloc_master(24, h, w)
        made = _spills(_root) - before
        ck("a spill file exists while the tensor is alive", len(made) == 1, str(made))
        live[0, 0, 0, 0] = 0.5
        ck("it is writable while mapped", float(live[0, 0, 0, 0]) == 0.5)
        mapping = getattr(live, "_htc_mmap", None)
        del live
        if mapping is not None:
            mapping._mmap.close()
        del mapping
        gc.collect()
        ck("it removes itself once nothing holds it",
           not (_spills(_root) & made),
           "no sweep required, and a killed process cleans up too")
    finally:
        H3.MASTER_SPILL_BYTES = big
        for n in _spills(_root) - before:
            try:
                os.remove(os.path.join(_root, n))
            except OSError:
                pass

    print("sweep, as a safety net for older builds")
    import folder_paths
    root = folder_paths.get_temp_directory()
    os.makedirs(root, exist_ok=True)
    stale = os.path.join(root, "htc_master_deadbeef.raw")
    with open(stale, "wb") as fh:
        fh.write(b"\0" * 1024)
    H3._sweep_master_spills(keep=os.path.join(root, "htc_master_current.raw"))
    ck("a stale spill from an earlier run is reclaimed",
       not os.path.exists(stale),
       "a killed render must not leave 31 GB behind")

    other = os.path.join(root, "something_else.bin")
    with open(other, "wb") as fh:
        fh.write(b"\0" * 16)
    H3._sweep_master_spills(keep="")
    ck("files that are not ours are left alone", os.path.exists(other))
    os.remove(other)

    # Drop every mapping before unlinking: on Windows a live mapping cannot be
    # removed, which is the same property _sweep_master_spills relies on.
    # Close the mapping before unlinking. A live mapping cannot be removed on
    # Windows -- the property _sweep_master_spills relies on -- so dropping
    # references is not enough and the memmap has to be closed by name.
    mapping = getattr(spilled, "_htc_mmap", None)
    ck("the spilled tensor carries a handle to its mapping", mapping is not None,
       "needed to release the file deterministically")
    del a, b, small, spilled, ram
    if mapping is not None:
        mapping._mmap.close()
    del mapping
    gc.collect()
    for name in _spills(_root) - _pre:
        try:
            os.remove(os.path.join(_root, name))
        except OSError:
            print("  note: could not remove", name)

    print()
    if FAIL:
        print("%d FAILURE(S): %s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
