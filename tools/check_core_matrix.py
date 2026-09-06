"""Prove every Core call works on EVERY ComfyUI in the range we advertise.

check_core_calls.py checks the Core installed on this machine. That proves the
pack does not depend on parameter order, which is what broke a user once, but it
cannot prove the call works on the build somebody else is running -- and
everybody is running something different. pyproject.toml claims
`requires-comfyui = ">=0.34.0"` with no upper bound. This is what makes that
claim testable rather than aspirational.

For every ComfyUI release at or above the declared floor, plus today's master,
it fetches the modules that define the nodes this pack calls, reads their
`execute` signatures, and checks each call site against them:

  * the node exists in that version at all
  * nothing is passed positionally
  * every keyword passed is a real parameter there
  * every parameter that has no default there is one we supply

Signatures are read with `ast`, never imported -- this downloads code from the
internet, and parsing it is not the same as running it.

Network, so it is not in check_all.py. Run it when Core moves, when the floor
changes, or before a release.

    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\check_core_matrix.py
    ... --refresh     ignore the cache and re-fetch
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# comfyanonymous/ComfyUI redirected to Comfy-Org/ComfyUI; the numeric id is
# stable across the rename and does not 301.
REPO = "https://api.github.com/repositories/589831718"
RAW = "https://raw.githubusercontent.com/Comfy-Org/ComfyUI/{ref}/{path}"
CACHE = os.path.join(HERE, ".core_matrix_cache")

MODULES = {
    "comfy_extras.nodes_minimax_h3": "comfy_extras/nodes_minimax_h3.py",
    "comfy_extras.nodes_custom_sampler": "comfy_extras/nodes_custom_sampler.py",
}

FAILS = []


def ck(label, ok, detail=""):
    print("  %-4s %-46s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        FAILS.append(label)


def fetch(url, cache_key, refresh=False):
    path = os.path.join(CACHE, cache_key)
    if not refresh and os.path.exists(path):
        return io.open(path, encoding="utf-8").read()
    with urllib.request.urlopen(url, timeout=60) as r:
        body = r.read().decode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="").write(body)
    return body


def parse_version(tag):
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def floor_from_pyproject():
    s = io.open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r'^requires-comfyui\s*=\s*"([^"]+)"', s, re.M)
    if not m:
        return None, ""
    spec = m.group(1)
    return parse_version(spec.lstrip(">=<!~ ")), spec


def signatures(src):
    """{name: (param_names, required_names)} read without importing."""
    out = {}
    tree = ast.parse(src)
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in cls.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "execute"):
                continue
            a = fn.args
            names = [x.arg for x in (a.posonlyargs + a.args)
                     if x.arg not in ("cls", "self")]
            ndef = len(a.defaults)
            req = names[:len(names) - ndef] if ndef else list(names)
            names += [x.arg for x in a.kwonlyargs]
            req += [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults)
                    if d is None]
            # Indexed by the class name, because that is what this pack imports.
            out[cls.name] = (names, req)
    return out


def call_sites():
    """Every watched Core call in the pack, with the keywords it passes."""
    import check_core_calls as C
    src = io.open(os.path.join(HERE, "h3_ref_chain.py"), encoding="utf-8").read()
    sites = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f, owner, args = node.func, None, None
        if (isinstance(f, ast.Attribute) and f.attr == "execute"
                and isinstance(f.value, ast.Name)):
            owner, args = f.value.id, node.args
        elif isinstance(f, ast.Name) and f.id == "_core_call" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name):
                # _core_call(Node, "what", **kw) -- the first two are the
                # wrapper's own, everything after would be positional to Core.
                owner, args = first.id, node.args[2:]
        if owner in C.WATCHED:
            sites.append((owner, node.lineno, len(args or []),
                          [k.arg for k in node.keywords if k.arg]))
    return sites


def main():
    refresh = "--refresh" in sys.argv
    floor, spec = floor_from_pyproject()
    if floor is None:
        print("pyproject.toml declares no requires-comfyui; nothing to verify")
        return 1
    print("pyproject declares requires-comfyui = %r\n" % spec)

    sites = call_sites()
    print("%d call site(s) across %d node(s)"
          % (len(sites), len({s[0] for s in sites})))

    rels = json.loads(fetch(REPO + "/releases?per_page=100", "releases.json",
                            refresh=True))
    tags = sorted({r["tag_name"] for r in rels
                   if parse_version(r["tag_name"])
                   and parse_version(r["tag_name"]) >= floor},
                  key=parse_version)
    refs = tags + ["master"]
    print("%d version(s) at or above the floor, plus master\n" % len(tags))

    for ref in refs:
        sigs, missing = {}, None
        for path in MODULES.values():
            try:
                src = fetch(RAW.format(ref=ref, path=path),
                            os.path.join(ref, path.replace("/", "_")),
                            refresh=refresh or ref == "master")
            except urllib.error.HTTPError as e:
                missing = "%s (HTTP %d)" % (path, e.code)
                continue
            sigs.update(signatures(src))
        if missing:
            ck("%s: has the modules this pack imports" % ref, False, missing)
            continue

        bad = []
        for owner, line, npos, kws in sites:
            if owner not in sigs:
                bad.append("%s @:%d does not exist here" % (owner, line))
                continue
            names, req = sigs[owner]
            if npos:
                bad.append("%s @:%d passes %d positionally" % (owner, line, npos))
            unknown = [k for k in kws if k not in names]
            if unknown:
                bad.append("%s @:%d passes %s; this version takes %s"
                           % (owner, line, unknown, names))
            unfilled = [r for r in req if r not in kws]
            if unfilled:
                bad.append("%s @:%d omits required %s" % (owner, line, unfilled))
        ck("%s: every call binds" % ref, not bad,
           "%d site(s)" % len(sites) if not bad else "")
        for b in bad:
            print("       %s" % b)

    if FAILS:
        print("\nCORE MATRIX: %d version(s) this pack claims to support and "
              "does not." % len(FAILS))
        return 1
    print("\nCORE MATRIX: every call site binds on every version at or above "
          "the declared floor, and on master.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
