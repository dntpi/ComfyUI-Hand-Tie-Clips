"""No statement follows a return / raise / continue / break in the same block.

D1i (b3abcb7) indented the whole upload save block one level too deep, under
the `continue` that skips an unsupported file. Every statement after it was
unreachable, so every SUPPORTED upload reached `shrink_image(path)` with
`path` never assigned ("cannot access local variable 'path'"), and no checker
ran the route. Python accepts the shape silently; this does not.

Run: python tools/check_unreachable.py
"""
from __future__ import annotations

import ast
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUMPS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def dead_code(tree):
    """[(line, jump_line)] for every statement after a jump in its block."""
    out = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody", "handlers"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block[:-1]):
                if isinstance(stmt, JUMPS):
                    out.append((block[i + 1].lineno, stmt.lineno))
    return out


def main():
    fails = []
    files = sorted(f for f in os.listdir(ROOT) if f.endswith(".py"))
    for name in files:
        src = io.open(os.path.join(ROOT, name), encoding="utf-8").read()
        for line, jump in dead_code(ast.parse(src, name)):
            fails.append(f"{name}:{line} is unreachable (after the jump on line {jump})")
    # The detector itself, both ways: the D1i shape is caught, a plain loop is not.
    d1i = ("for p in parts:\n    if bad(p):\n        skip(p)\n        continue\n"
           "        save(p)\n    use(p)\n")
    ok = "for p in parts:\n    if bad(p):\n        continue\n    save(p)\n"
    if dead_code(ast.parse(d1i)) != [(5, 4)]:
        fails.append("the detector misses the D1i shape")
    if dead_code(ast.parse(ok)):
        fails.append("the detector flags reachable code")
    for f in fails:
        print("  FAIL " + f)
    print(f"UNREACHABLE: {len(files)} modules, "
          + (f"{len(fails)} problem(s)." if fails else "none."))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
