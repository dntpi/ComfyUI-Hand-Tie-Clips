"""D1i: uploads dedup by CONTENT, and clearing a slot clears its dropdown.

Two behaviours a person notices and no existing check covered.

The pile-up: `unique_path` only ever appended `_1`, `_2`, -- so dropping the
same picture twice left `photo.png` AND `photo_1.png`, forever. Nothing here
looked at the bytes.

The stale dropdown: clearing a slot repainted the thumbnail but not the file
list, so the row said `empty` on the left and still offered the old filename on
the right -- and the next Save wrote that filename back into the plan.

Run: python tools/check_dedup.py
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILS: list[str] = []


def ck(name, ok, detail=""):
    if ok:
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


print("\ndedup by content")
media = load("h3media_dedup_probe", os.path.join(ROOT, "media.py"))

BLOB = b"the same picture, dropped twice"
HEX = hashlib.sha256(BLOB).hexdigest()

with tempfile.TemporaryDirectory() as d:
    # First drop lands under the name it asked for.
    tmp = os.path.join(d, ".upload.part")
    with open(tmp, "wb") as fh:
        fh.write(BLOB)
    ck("an unknown file is not a duplicate", media.find_by_hash(d, HEX) is None)
    p1 = media.claim_for_hash(d, "photo.png", HEX)
    os.replace(tmp, p1)
    ck("the first drop lands under its own name",
       os.path.basename(p1) == "photo.png", os.path.basename(p1))

    # Second drop of the same bytes is recognised.
    ck("the same bytes are found on disk", media.find_by_hash(d, HEX) == "photo.png",
       repr(media.find_by_hash(d, HEX)))

    # A DIFFERENT file under the same name must not be overwritten.
    other = b"a different picture entirely"
    hex2 = hashlib.sha256(other).hexdigest()
    p2 = media.claim_for_hash(d, "photo.png", hex2)
    ck("different bytes never take an existing name",
       os.path.basename(p2) != "photo.png", os.path.basename(p2))
    with open(p2, "wb") as fh:
        fh.write(other)
    ck("and the original is untouched",
       open(os.path.join(d, "photo.png"), "rb").read() == BLOB)

    # An in-flight upload is not a duplicate candidate.
    inflight = os.path.join(d, ".upload_99_0.part")
    with open(inflight, "wb") as fh:
        fh.write(BLOB)
    ck("an upload in flight is ignored, not matched",
       media.find_by_hash(d, HEX) == "photo.png")
    os.remove(inflight)

    # The temp suffix is never reported as the stored name.
    with open(os.path.join(d, "junk.part"), "wb") as fh:
        fh.write(b"junk")
    ck("a .part file is never offered as a name",
       media.find_by_hash(d, hashlib.sha256(b"junk").hexdigest()) is None)

print("\ndedup wiring in the upload route")
routes = open(os.path.join(ROOT, "routes.py"), encoding="utf-8").read()
try:
    ast.parse(routes)
    ck("routes.py parses", True)
except SyntaxError as exc:
    ck("routes.py parses", False, str(exc))

ck("the route hashes while it streams", "digest.update(chunk)" in routes)
ck("the route asks before it copies", "find_by_hash(dest, hexed)" in routes)
ck("a duplicate reuses the existing name",
   re.search(r'if same:.*?"name": same', routes, re.S) is not None)
ck("the temp file is renamed to its final name",
   re.search(r"_os\.replace\(temp, path\)", routes) is not None)
ck("and there is still a partial-file cleanup",
   re.search(r"except Exception:.*?_os\.remove\(temp\)", routes, re.S) is not None)
ck("a duplicate is logged, not silent",
   "not copied" in routes,
   "-- a silent dedup looks like an upload that failed")

# Rewriting the write block dropped `w = h = 0` once already, and nothing in
# the suite noticed: only IMAGES go through shrink_image, so w/h were left
# unbound and a video or audio upload died on NameError at saved.append.
# A route this far from a test is worth one static guard.
ck("width/height are bound for every kind, not just images",
   re.search(r"w = h = 0\n\s+if kind == \"image\":", routes) is not None,
   "-- a video/audio upload would raise NameError in saved.append")
ck("and shrink_image still only runs for images",
   re.search(r"if kind == \"image\":\n\s+path, w, h = _media\.shrink_image", routes)
   is not None)

ck("media.py exposes both helpers",
   "def find_by_hash(" in open(os.path.join(ROOT, "media.py"), encoding="utf-8").read()
   and "def claim_for_hash(" in open(os.path.join(ROOT, "media.py"), encoding="utf-8").read())

print("\nclearing a slot clears its dropdown")
picker = io.open(os.path.join(ROOT, "js", "editor", "media_picker.js"),
                 encoding="utf-8").read()
# Anchor on the CLASS the clear button carries, not on its label text. The
# first version sliced from `button("×"` and got an empty window -- `rfind`
# returned -1, the slice was empty, and the check passed without ever looking
# at the code. A check that can pass on an empty string is worse than none.
_at = picker.find("h3e-thumb-x")
ck("the clear button is present at all", _at > 0)
# The exact call, with its arguments. A bare `paintList(` is satisfied by
# `onFilesChanged(paintList)` a few lines below, so removing the clear button's
# own call left this check green -- which is why the negative test had to be
# run before trusting it.
clear = picker[max(0, _at - 400):_at + 200] if _at > 0 else ""
ck("clearing repaints the file list, not just the thumbnail",
   "paintList(_files || [])" in clear,
   "-- the dropdown kept the old filename and the next Save wrote it back")
ck("and the empty name is still not re-added as a fallback",
   re.search(r"if \(name && !found\)", picker) is not None,
   "-- a name the widget no longer holds must not be offered")

print()
if FAILS:
    print("DEDUP: %d check(s) failed." % len(FAILS))
    sys.exit(1)
print("DEDUP: all passed.")