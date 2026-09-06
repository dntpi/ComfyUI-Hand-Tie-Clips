"""Every image the published page and the registry listing point at, fetched.

This is the one check that has to leave the machine, which is why it is not in
check_all.py: run it after the push, before `comfy node publish`.

Two failures it exists for, both silent:

  1. **An LFS object that was never uploaded.** `origin` carries two push URLs
     and git-lfs uploads to the first only, so objects can be missing on GitHub
     while the tree that references them is not. `git lfs push <github-url>
     <branch>` fixes it -- but only for objects reachable from THAT branch, so
     a picture added on a feature branch needs the merge to land first. Get the
     order wrong and the page renders a broken image with no error anywhere.

  2. **A pointer served as 200 OK.** `raw.githubusercontent.com` does not
     resolve LFS: it returns the 131-byte pointer file, as text/plain, with a
     success status. Nothing in a browser, a linter or a link checker calls
     that an error. Only the size gives it away.

So status is not enough. Each URL must come back the same number of bytes as
the file on disk, and must not begin with a pointer header.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pointer file's first line. Assembled, so a hit on this file's own text
# cannot be mistaken for the thing it looks for.
POINTER = "version " + "https://git-lfs"

MEDIA = "https://media.githubusercontent.com/media/"
FAILS = []


def ck(label, ok, detail=""):
    print("  %-4s %-52s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        FAILS.append(label)


def urls_referenced():
    """Every remote image the two published faces of this pack point at."""
    out = []
    readme = io.open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    for m in re.finditer(r"!\[[^\]]*\]\((https?://[^)]+)\)", readme):
        out.append(("README.md", m.group(1)))
    toml = io.open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8").read()
    for field in ("Icon", "Banner"):
        m = re.search(r"^%s\s*=\s*\"([^\"]+)\"" % field, toml, re.M)
        if m:
            out.append(("pyproject.toml %s" % field, m.group(1)))
    return out


def local_path(url):
    """docs/img/x.png, out of .../media/<owner>/<repo>/<ref>/docs/img/x.png."""
    if not url.startswith(MEDIA):
        return None
    rest = url[len(MEDIA):].split("/")
    return "/".join(rest[3:]) if len(rest) > 3 else None


def main():
    refs = urls_referenced()
    print("checking %d referenced image(s)\n" % len(refs))

    tracked = subprocess.run(["git", "lfs", "ls-files", "-n"], cwd=HERE,
                             capture_output=True, text=True).stdout.split("\n")
    tracked = {p.strip() for p in tracked if p.strip()}

    for where, url in refs:
        name = url.rsplit("/", 1)[-1]
        # The whole point of media. is that raw. lies about LFS.
        if "raw.githubusercontent.com" in url or "/raw/" in url:
            ck("%s: not a raw. URL" % name, False,
               "%s -- raw. serves the pointer as 200 OK" % where)
            continue

        rel = local_path(url)
        if rel is None:
            ck("%s: recognised host" % name, True, "not LFS, skipped")
            continue

        disk = os.path.join(HERE, rel.replace("/", os.sep))
        if not os.path.exists(disk):
            ck("%s: exists in this tree" % name, False, rel)
            continue
        size = os.path.getsize(disk)

        # An image referenced from the page but not LFS-tracked pushes fine to
        # GitHub and is refused by the HuggingFace mirror, which takes no plain
        # binaries at any size.
        ck("%s: LFS-tracked" % name, rel in tracked, rel)

        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            ck("%s: served" % name, False,
               "HTTP %d -- the object is not on GitHub; `git lfs push "
               "<github-url> <branch>` for the branch that has it" % e.code)
            continue
        except OSError as e:
            ck("%s: served" % name, False, "unreachable: %s" % e)
            continue

        ck("%s: served" % name, status == 200, "HTTP %d" % status)
        ck("%s: content, not a pointer" % name,
           not body[:40].decode("utf-8", "replace").startswith(POINTER),
           "%d bytes" % len(body))
        ck("%s: matches the file on disk" % name, len(body) == size,
           "%d served vs %d on disk" % (len(body), size))

    if FAILS:
        print("\nLFS URL CHECK: %d failure(s). The page renders these broken."
              % len(FAILS))
        return 1
    print("\nLFS URL CHECK: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
