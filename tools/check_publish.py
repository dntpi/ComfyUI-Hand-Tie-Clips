"""Scan the files that would actually be PUBLISHED for scanner-trigger tokens.

The Comfy registry runs a YARA scan on every uploaded version and Flags what it
matches. It has no taint analysis and does not care whether a match is a call, a
comment, or a sentence in a changelog -- it matches strings in files. That last
part is the one that keeps catching us out. Around 1.0.2 the same mistake was
made three times in one day:

  1. A hostname lookup was removed from a function and then named twice in the
     docstring explaining the removal.
  2. The aiohttp client class was quoted in docs/DEVLOG.md while writing up the
     finding, which produced a SECOND finding, in a markdown file.
  3. The replacement implementation's own docstring quoted the two patterns
     that had just flagged it.

Each was caught by reading. That is not a control. This is: it reads the same
file set `comfy node publish` uploads -- git-tracked, minus .comfyignore -- and
fails on any token the registry is known to match, wherever it appears.

It is deliberately NOT a copy of the registry's rules. It carries only patterns
this pack has actually been Flagged for, each with the version that earned it,
because a checker asserting things nobody has observed goes stale silently.

The aiohttp client class is the exception: it is expected in llm.py and allowed
there, because that is the LLM client and it is not going anywhere. It is
refused everywhere else, which is exactly the DEVLOG case.
"""
from __future__ import annotations

import fnmatch
import re
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The tokens are assembled rather than written out, so this file does not itself
# become the thing it is checking for. It ships nowhere -- tools/ is in
# .comfyignore -- but a checker that would fail itself is a bad checker.
_AIOHTTP = "aiohttp.Client" + "Session"
_SOCK = "socket.sock" + "et("
_HOSTNAME = "gethost" + "name"
_HOSTBYNAME = "gethost" + "byname"

# token, why it is here, files where it is tolerated
PATTERNS = [
    (_HOSTNAME, "host enumeration; removed in 1.0.3", ()),
    (_HOSTBYNAME, "host enumeration", ()),
    (_SOCK, "matched $socket1 on 1.0.2", ()),
    (".bind(", "matched $socket4 on 1.0.2", ()),
# Process launch. Allowed in llm.py and refused everywhere else, for the
      # same reason the client class below is: it is the WRITER, it is the only
      # file that starts or stops a server, and the argv is a fixed list built
      # from a validated path -- no shell, so nothing in a settings file can be
      # interpolated into a command line.
      #
      # It is here because the writer is launchable from the node (user,
      # 2026-10-10). The panel can already stop the server, so it should be
      # able to start it; otherwise the second plan of a session begins with
      # alt-tab, because a 27B on a 32 GB card cannot coexist with a render and
      # is therefore stopped for every one. `comfy node publish` was ALREADY
      # skipped over the client class, so this costs no new release.
      #
      # The bare token is listed as well as the two call forms, because the scan
      # reads prose the same way it reads code (see 1.0.2) and shipped modules
      # have carried this word in comments explaining why the thing it names was
      # removed. One entry per token, in place: a second entry for the same token
      # is shadowed by the first, so the exemption would silently do nothing.
      ("subprocess.Popen", "process launch, for the writer server only", ("llm.py",)),
      ("subprocess.run", "process launch, for the writer server only", ("llm.py",)),
      ("subprocess", "process launch, for the writer server only", ("llm.py",)),
    # The BARE token as well as the two call forms. The scan reads prose the
    # same way it reads code -- 1.0.2 was Flagged for a class name quoted in a
    # markdown file while explaining why it had been Flagged -- and two shipped
    # modules carried this word in comments describing why the thing it names
    # was REMOVED. Describe the mechanism, never spell it. tools/ is
    # comfyignored, so this file naming it is not in the scanned set.
    # OS PROBING. Never flagged here, and checked because a scanner reading a
    # pack that asks the machine who it is has every reason to look harder --
    # host name, user name, MAC address and the environment are what
    # fingerprinting looks like, whatever they were meant for. This pack has
    # never needed any of it: paths come from folder_paths, media from
    # media.resolve, and the one legitimate absolute path in the tree is
    # sheet.py's Windows font fallback, which names no user and no machine.
    #
    # Listed as a class rather than waiting to be Flagged for one of them.
    ("platform.system", "OS probing: not needed, reads as fingerprinting", ()),
    ("platform.machine", "OS probing", ()),
    ("platform.node", "OS probing: the host name", ()),
    ("platform.uname", "OS probing", ()),
    ("socket.gethostname", "OS probing: the host name", ()),
    ("getpass.getuser", "OS probing: the user name", ()),
    ("uuid.getnode", "OS probing: the MAC address", ()),
    ("winreg", "registry access", ()),
    ("os.environ", "reads the environment", ()),
    ("os.getenv", "reads the environment", ()),
    ("importlib.import_module", "python_bytecode_manipulation on 1.0.0", ()),
(_AIOHTTP, "matched $http5 on 1.0.1 and 1.0.2", ("llm.py",)),
      # Process launch, allowed in llm.py for the same reason as the client
      # above and with the same shape of argument: it is the WRITER, it is the
      # only file that starts or stops a server, and the argv is a fixed list
      # built from a validated path -- no shell, so nothing in a settings file
      # can be interpolated into a command line.
      #
      # It is here because the user asked for the writer to be launchable from
      # the node (2026-10-10), and the alternative is a workflow that cannot
      # use the writer it is written for: the panel can stop the server, so it
      # should be able to start it. `comfy node publish` was ALREADY skipped
      # over the client class above, so this costs no new release.
      #
      # Anything else in the tree that spawns a process is still a FAIL, which
      # is the half that matters.
  ]


def published_files():
    """git-tracked, minus .comfyignore -- what `comfy node publish` zips."""
    out = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True,
                         text=True, check=True).stdout
    tracked = [f.strip().replace("\\", "/") for f in out.splitlines() if f.strip()]

    rules = []
    ci = os.path.join(HERE, ".comfyignore")
    if os.path.exists(ci):
        with open(ci, encoding="utf-8") as fh:
            rules = [ln.strip() for ln in fh
                     if ln.strip() and not ln.lstrip().startswith("#")]

    def ignored(path):
        for rule in rules:
            r = rule.rstrip("/")
            if path == r or path.startswith(r + "/") or fnmatch.fnmatch(path, r):
                return True
        return False

    return [f for f in tracked if not ignored(f)]


# Absolute paths that name THIS machine rather than any machine. The font
# fallback list in sheet.py is a legitimate Windows path and is not this.
_LOCAL_PATH = re.compile(r"[A-Za-z]:\\(?:Users|ComfyUI)\\|/[a-z]/ComfyUI/")


def check_no_local_paths(files):
    """Nothing in the published zip may name this disk.

    A session document is written for whoever picks the work up next, and is
    useful on GitHub for exactly that reason. In the published artifact it is
    a leak: the handoff notes carry this pack's absolute path, a Desktop path,
    the ComfyUI log, the venv interpreter, and the filename of a voice take
    belonging to somebody who is not the installer. They were tracked and NOT
    in .comfyignore, so they would have shipped.

    Enforced rather than remembered. The .comfyignore entries are the fix;
    this is what stops the next session document arriving without one.
    """
    out = []
    for rel in files:
        if os.path.basename(rel) == "sheet.py":
            continue                     # Windows font fallbacks, deliberate
        try:
            with open(os.path.join(HERE, rel), encoding="utf-8",
                      errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if _LOCAL_PATH.search(line):
                        out.append((rel, i, line.strip()[:100]))
        except (OSError, UnicodeError):
            continue
    return out


def main():
    files = published_files()

    leaks = check_no_local_paths(files)
    if leaks:
        print("PUBLISH CHECK: %d line(s) in the published zip name this "
              "machine:" % len(leaks))
        for rel, i, line in leaks[:12]:
            print("  FAIL %s:%d  %s" % (rel, i, line))
        print("       Add the file to .comfyignore, or take the path out.")
        return 1
    print("  ok   no published file names this disk       "
          "%d file(s) scanned" % len(files))
    # The guard that matters. A checker which silently checks nothing is the
    # failure mode every checker has; check_ui.py shipped with exactly that bug
    # and reported success against zero imports.
    if len(files) < 10:
        print("PUBLISH CHECK: only %d file(s) resolved. The file list is wrong, "
              "not the pack." % len(files))
        return 1

    hits = []
    for rel in files:
        try:
            with open(os.path.join(HERE, rel), encoding="utf-8",
                      errors="ignore") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for token, why, allowed in PATTERNS:
            if rel in allowed:
                continue
            for i, line in enumerate(lines, 1):
                if token in line:
                    hits.append((rel, i, token, why, line.strip()[:70]))

    print("scanning %d files that would be published\n" % len(files))
    if not hits:
        for token, why, allowed in PATTERNS:
            note = "  (allowed in %s)" % ", ".join(allowed) if allowed else ""
            print("  ok   absent: %-26s %s%s" % (token, why, note))
        print("\nPUBLISH CHECK: all passed")
        return 0

    for rel, i, token, why, line in hits:
        print("  FAIL %s:%d  %r" % (rel, i, token))
        print("       %s" % why)
        print("       %s" % line)
    print("\nPUBLISH CHECK: %d occurrence(s). These reach the registry scanner "
          "wherever they appear -- prose counts." % len(hits))
    return 1


if __name__ == "__main__":
    sys.exit(main())
