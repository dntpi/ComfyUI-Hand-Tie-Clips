r"""Offline structural checks for the editor's JavaScript and stylesheet.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_ui.py

There is no Node in this environment and no build step in this pack, so until
now the ~4000 lines under `js/` had NOTHING standing between an edit and a
browser -- and the browser reports a bad import as a blank node, which looks
exactly like the pack failing to load. This is not a parser and does not
pretend to be one. It checks the four things that have actually gone wrong or
could go wrong silently:

  * delimiters balance, per file -- the stray-brace class of typo;
  * every named import resolves to a real export in the file it names;
  * every `var(--h3-*)` the stylesheet reads is declared in it. Two were not:
    `--h3-line` made the trim bar's border resolve to nothing for months, and
    nothing anywhere would have said so;
  * class names the JS sets and the stylesheet styles do not drift apart, in
    the direction that matters -- a rule for a class nobody sets is dead code,
    which is worth knowing before it is inherited as gospel.
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(HERE, "js")
CSS = os.path.join(JS_DIR, "h3_ref_chain.css")

FAIL = []


def ck(name, cond, detail=""):
    print("  %-4s %-56s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAIL.append(name)


def read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def js_files():
    out = []
    for base, _dirs, names in os.walk(JS_DIR):
        for n in sorted(names):
            if n.endswith(".js"):
                out.append(os.path.join(base, n))
    return sorted(out)


def strip_js(src):
    """Blank out strings, template literals and comments, keeping length.

    Regex literals are deliberately NOT handled: distinguishing `/` as division
    from `/` as a regex needs the parser this file is explicitly not. Every
    regex in this pack is brace-free, which the balance check below would fail
    loudly on if it stopped being true.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "'\"`":
            quote, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    break
                j += 1
            for k in range(i, min(j + 1, n)):
                if src[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if src[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def balance(src):
    """-> (ok, message). Reports the line of the first offending delimiter."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    line = 1
    for ch in src:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                return False, f"closing {ch!r} with nothing open, line {line}"
            got, at = stack.pop()
            if got != pairs[ch]:
                return False, f"{got!r} at line {at} closed by {ch!r} at {line}"
    if stack:
        got, at = stack[-1]
        return False, f"{got!r} at line {at} is never closed"
    return True, ""


def main():
    files = js_files()
    print("delimiters")
    sources = {}
    for path in files:
        src = strip_js(read(path))
        sources[path] = src
        ok, why = balance(src)
        ck(os.path.relpath(path, HERE).replace(os.sep, "/"), ok, why)

    print("\nimports resolve to real exports")
    imp = re.compile(r"import\s*\{([^}]*)\}\s*from\s*[\"']([^\"']+)[\"']")
    exp = re.compile(r"export\s+(?:async\s+)?(?:function|const|let|class)\s+"
                     r"([A-Za-z0-9_$]+)")
    exports = {p: set(exp.findall(read(p))) for p in files}
    checked = host = 0
    for path in files:
        # The RAW source, not `sources[path]`: stripping blanks out string
        # literals, and the module specifier of an import is a string literal.
        # Scanned against the stripped text this found zero imports and said so
        # -- which is why the "something was actually imported" guard is here.
        for names, spec in imp.findall(read(path)):
            target = os.path.normpath(
                os.path.join(os.path.dirname(path), spec))
            rel = os.path.relpath(path, HERE).replace(os.sep, "/")
            # `../../scripts/app.js` is ComfyUI's, served by its web server and
            # not on disk anywhere near the pack. Anything resolving outside the
            # pack is the host's to provide; only our own files are checkable.
            if not target.startswith(HERE + os.sep):
                host += 1
                continue
            if not os.path.isfile(target):
                ck(f"{rel} -> {spec}", False, "no such file")
                continue
            wanted = {w.strip().split(" as ")[0].strip()
                      for w in names.split(",") if w.strip()}
            missing = sorted(wanted - exports.get(target, set()))
            ck(f"{rel} -> {spec}", not missing,
               f"{len(wanted)} name(s)" if not missing
               else "not exported: " + ", ".join(missing))
            checked += len(wanted)
    ck("something was actually imported", checked > 0,
       f"{checked} names, plus {host} import(s) of ComfyUI's own scripts")

    print("\nstylesheet")
    css = read(CSS)
    css_nc = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    declared = set(re.findall(r"(--h3-[A-Za-z0-9-]+)\s*:", css_nc))
    used = set(re.findall(r"var\(\s*(--h3-[A-Za-z0-9-]+)", css_nc))
    undeclared = sorted(used - declared)
    ck("every --h3-* the sheet reads is declared in it",
       not undeclared, f"{len(declared)} declared" if not undeclared
       else "undeclared: " + ", ".join(undeclared))

    # Classes the JS sets, vs classes the sheet has a rule for. Only one
    # direction is a defect: a rule for a class nothing sets is dead weight.
    # The other direction is normal -- plenty of classes are pure layout hooks.
    js_all = "\n".join(read(p) for p in files)
    js_classes = set()
    for m in re.findall(r"[\"']([a-z0-9 _-]*h3e-[a-z0-9 _-]*)[\"']", js_all):
        js_classes.update(w for w in m.split() if w.startswith("h3e-"))
    css_classes = set(re.findall(r"\.(h3e-[A-Za-z0-9_-]+)", css_nc))
    dead = sorted(css_classes - js_classes)
    ck("no stylesheet rule targets a class the JS never sets",
       not dead, f"{len(css_classes)} styled" if not dead
       else f"{len(dead)} dead: " + ", ".join(dead[:6]))

    print()
    if FAIL:
        print(f"UI CHECK: {len(FAIL)} FAILURE(S)")
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("UI CHECK: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
