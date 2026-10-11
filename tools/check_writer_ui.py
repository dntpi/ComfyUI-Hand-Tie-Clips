"""Regression pins for four Phase B defects, all found by reading rather than testing.

Each of these was shipped in a commit and then found by inspection, so nothing
in the suite would have caught them. They are cheap string pins over the panel
source -- deliberately not a refactor into a testable unit, because the bugs
were never in the logic, they were in the ORDER of operations against a DOM
node that cannot be constructed outside LiteGraph.

Run: python tools/check_writer_ui.py
"""
from __future__ import annotations

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JS = os.path.join(ROOT, "js", "editor", "writer_bar.js")
PY = os.path.join(ROOT, "routes.py")
LLM = os.path.join(ROOT, "llm.py")

fails = []


def src(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


js = src(JS)
py = src(PY)
llm = src(LLM)


def check(name, cond, why=""):
    if cond:
        print("  ok   %s" % name)
    else:
        fails.append(name)
        print("  FAIL %s %s" % (name, why))


# --- 1. the undeclared `proposal` ------------------------------------------
# writeThread() used to end with `node.properties[PROPOSAL_PROP] = proposal ||
# null`, where `proposal` was never declared anywhere in the file. It read as
# null and therefore ERASED an accepted proposal on the next line that touched
# the thread -- including the Accept button's own writeThread() call.
check(
    "writeThread does not assign PROPOSAL_PROP",
    "PROPOSAL_PROP] = proposal" not in js,
    "-- restores the wipe of an accepted proposal",
)
check(
    "no bare `proposal` identifier is read",
    not re.search(r"(?<![\w.\"'])proposal(?![\w\"'])\s*\|\|", js),
    "-- an undeclared global",
)
# The proposal is owned by exactly two places: Accept and Discard.
owners = re.findall(r"properties\[PROPOSAL_PROP\]\s*=", js)
check(
    "exactly two owners write PROPOSAL_PROP",
    len(owners) == 2,
    "-- got %d; Accept writes, Discard clears, nothing else may" % len(owners),
)

# --- 2. Discard has to actually discard ------------------------------------
# The PROPOSAL card's Discard only hid the card, so a proposal you had already
# refused still shaped the next "write the beats". There are two Discard
# buttons (proposal, edit) and only the proposal one owns this property, so
# this must look at the one preceded by the proposal accept text.
prop_card = js[js.rfind('button("Accept proposal"'):]
# Look at the whole Discard button body, not a `})`-truncated prefix: the
# first version cut at the `??=` line and missed the clear entirely.
discard = prop_card[:prop_card.find("});") + 400]
check(
    "proposal Discard clears the stored proposal",
    "PROPOSAL_PROP] = null" in discard,
    "-- it hides the card and leaves the agreement in place",
)

# --- 3. Accept edit rendered, then wiped -----------------------------------
# showDraft() RENDERS INTO draftList, so the two statements that followed it
# cleared the very draft it had just built. Nothing appeared and the edit could
# not be reviewed.
accept_edit = js[js.rfind('button("Accept edit"'):]
accept_edit = accept_edit[:accept_edit.find("})")]
si = accept_edit.find("showDraft(")
check(
    "Accept edit calls showDraft", si >= 0)
check(
    "Accept edit does not clear the draft list after showing it",
    "draftList.textContent" not in accept_edit[si:],
    "-- it wipes the draft on the way in",
)

# --- 4. the dropped-turns notice could never fire --------------------------
# writeThread() sliced to THREAD_MAX and then paintThread() measured the stored
# length against THREAD_MAX, so the comparison was permanently false.
check(
    "dropped count is captured before slicing",
    re.search(r"const over = t\.length - THREAD_MAX", js) is not None,
    "-- measuring after the slice is always false",
)
check(
    "paintThread reads the counter, not the sliced length",
    "THREAD_DROPPED_PROP] || 0) > 0" in js,
    "-- a length that can never exceed THREAD_MAX",
)

# --- 5. the server must not evict itself mid-request ----------------------
# free_for_render() ran on EVERY plan request when the writer shares the GPU,
# so Start -> Write beats stopped the server Start had just launched.
#
# This check was WRONG the first time and that is the point of it now. It only
# asked whether the token `server_running` appeared near the guard, so it passed
# a guard that passed conn["server_url"] into a function whose parameter is a
# BACKEND ID. backend_name() is total -- an unknown value returns
# DEFAULT_BACKEND -- so the URL silently became "lmstudio", the guard probed the
# wrong writer, concluded it was down, and called free_for_render(), which stops
# nInfer. The bug the guard existed to prevent, shipped inside the guard.
#
# So the check is now on the ARGUMENT, not the presence of a name.
guard = re.search(
    r"_writer_down\s*=\s*not\s+await\s+_llm\.server_running_async\("
    r"(.+?)\)\s*\n", py, re.S)
check(
    "the eviction guard is guarded on the writer being down",
    re.search(r"if _llm\.shares_this_gpu\([^)]*\)\s+and\s+_writer_down:", py)
    is not None,
    "-- a serving writer is stopped by the request that is about to use it",
)
arg = (guard.group(1) if guard else "").replace("\n", " ").strip()
check(
    "the guard probes a BACKEND ID, never a URL",
    'conn.get("backend")' in arg,
    "-- it passes %r; backend_name() turns an unknown value into lmstudio and "
    "silently answers about the wrong server" % arg,
)

# --- 6. nothing in an aiohttp handler may block ---------------------------
# server_running() waits on a socket. Called from a route handler it stalls
# ComfyUI's event loop -- the whole UI, canvas included -- for the length of
# its timeout, and a probe against a dead port always costs the FULL timeout.
check(
    "no route handler calls the blocking probe",
    re.search(r"_llm\.server_running\(", py) is None,
    "-- an event-loop stall behind every panel refresh and every Start click",
)
check(
    "start_process is not called synchronously from a route",
    re.search(r"_llm\.start_process\(", py) is None,
    "-- its own duplicate-27B probe blocks before the spawn",
)
for _m in re.finditer(r'"running":\s*(await\s+)?_llm\.server_running', py):
    check(
        "the %s probe at L%d is awaited"
        % ("async" if _m.group(1) else "BLOCKING", py[:_m.start()].count("\n") + 1),
        _m.group(1) is not None,
        "-- a 3 s socket wait on the event loop",
    )

# --- 7. D1h: the per-backend paths are settable FROM THE PANEL -------------
# The Start button used to fail with "set the path in Settings" and Settings had
# no path field. The only escape hatch was hand-editing htc_llm.json, so nInfer
# worked on the author's machine and nowhere else. Three halves, and all three
# have to hold: the panel offers the field, the panel posts it, and the SERVER
# reads it -- a field the server ignores looks broken on every other machine.
check("the panel offers a server-program field",
      '"server program"' in js,
      "-- the error message points at a Settings path that does not exist")
check("and a model-file field", '"model file"' in js)
check("saveConn posts them under the active backend",
      re.search(r"patch\.servers\s*=\s*\{", js) is not None,
      "-- a field that is never saved is a field that does nothing")
check("Start banks them before launching",
      re.search(r"async\s*\(\)\s*=>\s*\{[^}]{0,400}?saveConn\(\)"
                r"[^}]{0,400}?serverAction\(\"start\"\)", js, re.S) is not None,
      "-- typing a path and pressing Start must not need a Save in between")
# The half that is easy to get wrong and invisible until someone else's machine:
# a panel field the SERVER never reads. It would save cleanly, display
# correctly, and be ignored -- which looks exactly like a broken button.
check("the server reads the saved model_file when starting",
      re.search(r"get\(\"model_file\"\)\s*or\s*\"\"\)\.strip\(\)", llm) is not None,
      "-- a field the server ignores is worse than no field: it looks broken")
check("the server merges the per-backend patch instead of replacing it",
      "_merge_servers" in llm,
      "-- replacing drops the other backends' paths on the next Save")
check("the /llm route publishes which backends have a program",
      re.search(r'"program"\s*:', py) is not None,
      "-- the panel cannot hide a field it is not told about")
check("and publishes the saved paths",
      re.search(r'"server_paths"\s*:', py) is not None)

# --- 8. the paths actually SURVIVE a save --------------------------------
# The source-text checks above all passed against a build where the panel
# offered the fields, posted them, and the server read model_file -- and where
# save_conn then REPLACED the whole per-backend entry with {server_url, model},
# erasing exe and model_file on the very next Save. Ten checks, all green, and
# nothing round-tripped.
#
# So this one is behavioural: it drives save_conn/load_conn through a temp
# settings file and asserts the values come back. Nothing above it can catch
# that class of bug; only a value surviving a round trip can.
print("\nD1h: the paths survive a save (behavioural)")

import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

_spec = importlib.util.spec_from_file_location("h3llm_probe", LLM)
_llm = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_llm)
except Exception as _exc:  # noqa: BLE001
    _llm = None
    ck("llm.py imports under the checker", False, str(_exc)[:120])

if _llm is not None:
    _tmp = os.path.join(ROOT, "htc_llm_checkwriterui.json")
    _saved_file, _saved_conn = _llm.CONN_FILE, dict(_llm.CONN)
    try:
        if os.path.isfile(_tmp):
            os.remove(_tmp)
        _llm.CONN_FILE = "htc_llm_checkwriterui.json"
        _llm.CONN.update({"backend": "ninfer", "servers": {}})
        # The panel's REAL shape: the paths arrive in the same patch as the
        # address and the keep-warm flag. A first version of this check sent the
        # paths alone, which does not reach the branch that was broken, so the
        # negative test passed against a build that lost them.
        _llm.save_conn({
            "servers": {"ninfer": {
                "exe": "E:/tools/ninfer-serve.exe",
                "model_file": "E:/models/qwen3_8_27b_nvfp4.ninfer"}},
            "server_url": "http://127.0.0.1:8080",
            "keep_warm": True,
        })
        _llm.CONN.update({"servers": {}})
        _llm.load_conn()
        _got = (_llm.CONN.get("servers") or {}).get("ninfer") or {}
        check("a saved server program survives the round trip",
              _got.get("exe") == "E:/tools/ninfer-serve.exe", f"got {_got.get('exe')!r}")
        check("a saved model file survives the round trip",
              _got.get("model_file") == "E:/models/qwen3_8_27b_nvfp4.ninfer",
              f"got {_got.get('model_file')!r}")
        # And a second backend's save must not take them with it.
        _llm.save_conn({"servers": {"lmstudio": {"exe": "", "model_file": ""}}})
        _llm.CONN.update({"servers": {}})
        _llm.load_conn()
        _kept = ((_llm.CONN.get("servers") or {}).get("ninfer") or {}).get("model_file")
        check("and survives a save for ANOTHER backend",
              _kept == "E:/models/qwen3_8_27b_nvfp4.ninfer", f"got {_kept!r}")
    except Exception as _exc:  # noqa: BLE001
        check("the settings round trip runs", False, str(_exc)[:140])
    finally:
        if os.path.isfile(_tmp):
            os.remove(_tmp)
        _llm.CONN_FILE = _saved_file
        _llm.CONN.clear()
        _llm.CONN.update(_saved_conn)

print()
# --- 9. D1j: three defects found by pressing the buttons -------------------
# All three were reported from one sitting at the panel, and all three were
# invisible to the suite: one only fires when a plan is sent seconds after
# Start, one only on free text, and one only on a backend that is not
# LM Studio.

# 9a. Free text sent a stage the route rejects. `sendCanned(null, ...)` posted
# stage:"chat", there has never been a `chat` stage, and the route whitelists
# propose/develop/edit/song -- so every ordinary typed sentence came back
# "unknown stage 'chat'" without reaching the model.
check("free text does not post a stage the route rejects",
      re.search(r"stage:\s*stage\s*\|\|\s*[\"']develop[\"']", js) is not None
      and not re.search(r"stage:\s*stage\s*\|\|\s*[\"']chat[\"']", js),
      "-- a typed sentence is the most ordinary input there is")
check("and the route's whitelist matches what the panel sends",
      all(s in py for s in ('"propose"', '"develop"', '"edit"', '"song"')))

# 9b. The blocking duplicate-27B guard ran from inside the event loop.
# start_process_async probed correctly, then delegated to start_process, which
# probed AGAIN through server_running -> asyncio.run() -> RuntimeError, and the
# coroutine was never awaited. The guard answered "not running" while doing
# nothing at all, which is the worst state for the one check that stops a
# second 27B -- and it printed a RuntimeWarning on every Start click.
check("the Start route does not fall through to a second blocking probe",
      re.search(r"start_process\([^)]*already_checked\s*=\s*True", llm) is not None,
      "-- asyncio.run() inside a running loop raises, and the guard silently "
      "answers 'not running' instead of guarding anything")
check("start_process can still guard itself when called directly",
      re.search(r"if not already_checked and server_running\(n\)", llm) is not None,
      "-- a checker or a button calls it without the flag")

# 9c. The console said the opposite of what was happening. With nothing
# listening, `model list from ... failed` printed twice per page load in the
# same red as a fault, directly above `started nInfer`. And when a connection
# was cut mid-request the panel showed the raw
# "[WinError 64] The specified network name is no longer available".
check("a refused model list is not reported as a failure",
      "no server listening at" in llm,
      "-- before Start nothing is listening, and that is the normal state")
check("and it is said once per address, not once per poll",
      "_MODEL_LIST_NOTE" in llm)
check("a dropped connection becomes a sentence a person can act on",
      re.search(r"closed the connection before it answered", llm) is not None,
      "-- WinError 64 is the socket layer talking to itself")
check("WinError 64 is classified as a dropped connection",
      "64" in llm.split("_RESET_ERRNOS")[1][:80],
      "-- it is the exact error the panel showed")
check("an ordinary error is NOT called a dropped connection",
      re.search(r"if _dropped_connection\(exc\):", llm) is not None
      and re.search(r"_dropped_connection.*\n.*?raise LLMError\(f\"request failed",
                    llm, re.S) is not None,
      "-- a JSON parse error is not a severed socket")

# 9d. The offline copy told nInfer users to open an LM Studio tab.
check("the offline message names the panel's own Start button for nInfer",
      re.search(r"Press Start writer", js) is not None,
      "-- nInfer has no Developer tab; the sentence sends people looking for "
      "a window that does not exist")

print()
if fails:
    print("WRITER UI: %d check(s) failed." % len(fails))
    sys.exit(1)
print("WRITER UI: all passed.")