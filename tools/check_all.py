r"""Run every offline checker, report one summary, exit non-zero if any failed.

    D:\ComfyUI\venv\Scripts\python.exe tools\check_all.py

There is no test runner in this pack and no dependency to add one, so the
pre-commit sweep has been a hand-written shell loop -- retyped per session, in
whichever shell was open, and wrong at least twice: bash `for ... do` and `||`
are parse errors in PowerShell, and `/d/ComfyUI/...` resolves to `C:\d\...`.

Each child runs in its own process, because these import ComfyUI internals and
one checker's half-initialised torch state must not decide another's result.
`--quiet` prints only the summary table; without it each checker's own output
is passed through, which is what you want when one of them fails.

The schema check is in the list on purpose: `planner.py` feeds `SCHEMA.json` to
a live server as structured output, so drift there stopped being a docs problem
and became a runtime one.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# (script, extra args). Order is cheapest-first so an obvious break surfaces
# before the slow ones have finished importing torch.
CHECKS = [
    ("check_templates.py", []),
    ("check_prompts.py", []),
    ("check_workflows.py", []),
    ("check_features.py", []),
    ("check_planner.py", []),
    ("gen_schema.py", ["--check"]),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quiet", action="store_true",
                    help="only print the summary table")
    args = ap.parse_args()

    results = []
    for script, extra in CHECKS:
        path = os.path.join(HERE, script)
        if not os.path.isfile(path):
            results.append((script, None, 0.0, "missing"))
            continue
        if not args.quiet:
            print("=" * 70)
            print("  %s %s" % (script, " ".join(extra)))
            print("=" * 70)
        t0 = time.time()
        proc = subprocess.run([sys.executable, path] + extra,
                              cwd=os.path.dirname(HERE),
                              capture_output=args.quiet, text=True)
        dt = time.time() - t0
        tail = ""
        if args.quiet and proc.stdout:
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            tail = lines[-1].strip() if lines else ""
        results.append((script, proc.returncode, dt, tail))

    print()
    print("=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    bad = 0
    for script, code, dt, tail in results:
        if code is None:
            state = "MISSING"
            bad += 1
        elif code == 0:
            state = "ok"
        else:
            state = "FAIL(%d)" % code
            bad += 1
        print("  %-8s %-22s %5.1fs  %s" % (state, script, dt, tail))

    print()
    if bad:
        print("%d of %d checks did not pass." % (bad, len(results)))
        return 1
    print("All %d checks passed." % len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
