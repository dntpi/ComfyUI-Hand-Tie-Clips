"""A watchdog for the writer server, so it cannot be left holding the card.

    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\llm_watchdog.py start
    ... run whatever you were testing ...
    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\llm_watchdog.py check
    D:\\ComfyUI\\venv\\Scripts\\python.exe tools\\llm_watchdog.py stop

WHY
---
nInfer is a 27B on a 32 GB card and it pins about 31.9 of 32.6 GB for as long
as the process lives. It has no unload endpoint -- probed /v1/unload 404,
/admin 404, the OpenAI keep_alive body ignored, and the GPU unchanged after
every attempt -- so the only thing that gives the card back is ending the
process.

That makes it very easy to leave the machine unusable by accident. Three times
during this work the server was started for a test, the test finished, and the
process stayed up holding the card while the next thing tried to run. The
safeguard has to be automatic, because a rule that depends on remembering is
not a safeguard.

So: `start` writes a deadline and leaves a timer thread. When the deadline
passes with the server still running, it stops it and says so. `check` tells
you what is currently held and how long is left -- run it between steps, or
from the shell, without waiting to be asked.

The idle timeout is deliberately SHORT (default 20 minutes). A writer server
is not something you want resident across an afternoon of work, and the cost of
getting it back is about 30 s of model load. Erring towards stopping it.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "llm_watchdog.json")
SERVER_EXES = (("ninfer", "ninfer-serve.exe"),)

# Long enough for a slow plan (a 27B repair loop can run minutes) and short
# enough that nobody comes back to a locked card.
DEFAULT_IDLE_MIN = 20.0


# -- process helpers, shared with llm.py's stop_process ---------------------

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def find(image):
    """PIDs whose image name matches `image` exactly (case-insensitive)."""
    image = os.path.basename(str(image or "")).lower()
    if not image:
        return []
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    pe = _PE()
    pe.dwSize = ctypes.sizeof(_PE)
    found = []
    try:
        ok = k32.Process32First(snap, ctypes.byref(pe))
        while ok:
            if (pe.szExeFile or b"").decode("latin-1").lower() == image:
                found.append(pe.th32ProcessID)
            ok = k32.Process32Next(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return found


def stop(image):
    """Terminate matching processes. Returns the count stopped."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    n = 0
    for pid in find(image):
        h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            if k32.TerminateProcess(h, 0):
                n += 1
            k32.CloseHandle(h)
    return n


def held():
    """[(label, image, pids, GiB-ish count)] for every known writer running."""
    out = []
    for label, image in SERVER_EXES:
        pids = find(image)
        if pids:
            out.append((label, image, pids))
    return out


# -- the watchdog itself ----------------------------------------------------

def _read():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _write(d):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    os.replace(tmp, STATE)


def _guard(stop_after, label, image):
    """Wait, then stop the server if it is still up. Prints when it acts."""
    deadline = time.time() + stop_after
    try:
        while time.time() < deadline:
            time.sleep(2.0)
            if not find(image):
                return          # already gone; nothing to do
        n = stop(image)
        print("[htc-watchdog] %s idle for %.0f min -- stopped %d process(es), "
              "card released." % (label, stop_after / 60.0, n), flush=True)
        try:
            os.remove(STATE)
        except Exception:
            pass
    except Exception as exc:
        print("[htc-watchdog] guard failed: %r" % (exc,), flush=True)


def cmd_start(args):
    label, image = next(((l, i) for l, i in SERVER_EXES
                         if l == args.backend), SERVER_EXES[0])
    _write({"label": label, "image": image, "at": time.time(),
            "stop_after": float(args.minutes), "armed": True})
    t = threading.Thread(target=_guard, args=(float(args.minutes), label, image),
                         daemon=True)
    t.start()
    print("[htc-watchdog] %s armed: stops itself in %.0f min if left running. "
          "`check` any time." % (label, float(args.minutes)), flush=True)
    return 0


def cmd_check(_args):
    rows = held()
    if not rows:
        print("  no writer server running")
        try:
            os.remove(STATE)
        except Exception:
            pass
        return 0
    st = _read()
    for label, image, pids in rows:
        left = ""
        if st.get("image") == image and st.get("armed"):
            left = "  armed, stops in %.0f min" % st.get("stop_after", 0)
        print("  %s running (pid %s)%s" % (label, ", ".join(map(str, pids)), left))
    print("  -> tools\\llm_watchdog.py stop   to release the card now")
    return 0


def cmd_stop(_args):
    n = 0
    for label, image in SERVER_EXES:
        n += stop(image)
    try:
        os.remove(STATE)
    except Exception:
        pass
    print("  stopped %d writer process(es)" % n)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="arm the watchdog")
    s.add_argument("--backend", default="ninfer", choices=[l for l, _ in SERVER_EXES])
    s.add_argument("--minutes", type=float, default=DEFAULT_IDLE_MIN)
    s.set_defaults(fn=cmd_start)
    sub.add_parser("check", help="is a writer running?").set_defaults(fn=cmd_check)
    sub.add_parser("stop", help="release the card now").set_defaults(fn=cmd_stop)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())