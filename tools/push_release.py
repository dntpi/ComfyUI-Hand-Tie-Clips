"""Push a branch to both sites, in the order that works, and prove it landed.

`origin` carries two push URLs, so one `git push origin main` keeps GitHub and
HuggingFace at the same commit. That is worth keeping -- two remotes pushed by
hand drift the moment somebody runs one and not the other, and drift is silent,
where a half-pushed mirror at least fails loudly.

What the mirror does not do is carry the LFS objects. git-lfs uploads to the
first push URL only, so the objects have to go up explicitly, per site, BEFORE
the branch that references them:

    merge  ->  objects  ->  branch  ->  verify

The middle step is per-branch as well as per-site: `git lfs push <url> <branch>`
covers only the objects reachable from that branch, so a picture added on a
feature branch is not covered by a `main` push until the merge has landed. That
is the ordering that put two images on `v2` for a release cycle while every
local check said the tree was fine.

The verify step is the point. Nothing on this disk can tell you an object was
uploaded -- the tree, the pointer and the working copy all read correct either
way. So this ends by comparing both remotes' SHA against the local one and
handing off to check_lfs_urls.py, which fetches every published image.

    python tools/push_release.py            # current branch
    python tools/push_release.py main
    python tools/push_release.py main --dry-run

Terminal prompting is disabled: a missing credential fails in a second instead
of hanging on a password prompt nobody can see.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

GITHUB = "https://github.com/dntpi/ComfyUI-Hand-Tie-Clips.git"
HF = "https://huggingface.co/sandpies/ComfyUI-Hand-Tie-Clips"


def git(*args, **kw):
    import subprocess
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git"] + list(args), cwd=HERE, env=env,
                          capture_output=True, text=True, **kw)


def run(label, *args, dry=False):
    print("\n$ git %s" % " ".join(args))
    if dry:
        print("  (dry run, not executed)")
        return True
    r = git(*args)
    out = (r.stdout + r.stderr).strip()
    if out:
        print("\n".join("  " + l for l in out.split("\n")))
    if r.returncode:
        print("\nFAILED at: %s" % label)
    return r.returncode == 0


def remote_sha(remote, branch):
    r = git("ls-remote", remote, "refs/heads/" + branch)
    line = r.stdout.strip().split("\n")[0] if r.stdout.strip() else ""
    return line.split("\t")[0] if line else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("branch", nargs="?", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    branch = a.branch or git("branch", "--show-current").stdout.strip()
    dirty = git("status", "--porcelain").stdout.strip()
    if dirty:
        print("working tree is not clean -- commit or stash first:")
        print("\n".join("  " + l for l in dirty.split("\n")))
        return 1

    local = git("rev-parse", branch).stdout.strip()
    if not local:
        print("no such branch: %s" % branch)
        return 1
    print("pushing %s (%s) to both sites" % (branch, local[:10]))

    # Objects first, to each site by URL. Pushing to `origin` here would send
    # them to one site only, which is the whole failure this exists to stop.
    for url in (GITHUB, HF):
        if not run("lfs push " + url, "lfs", "push", url, branch,
                   dry=a.dry_run):
            return 1

    if not run("push", "push", "origin", branch + ":" + branch,
               dry=a.dry_run):
        return 1

    if a.dry_run:
        print("\ndry run: nothing pushed, nothing verified")
        return 0

    print("\nverifying both sites carry %s" % local[:10])
    bad = False
    for name, remote in (("github", "origin"), ("hf", "hf")):
        got = remote_sha(remote, branch)
        ok = got == local
        bad = bad or not ok
        print("  %-4s %-8s %s" % ("ok" if ok else "FAIL", name,
                                  got[:10] if got else "no such branch"))
    if bad:
        print("\nthe sites disagree -- do not publish")
        return 1

    print("\nnow the images, which is the part no local check can see:")
    import check_lfs_urls
    return check_lfs_urls.main()


if __name__ == "__main__":
    sys.exit(main())
