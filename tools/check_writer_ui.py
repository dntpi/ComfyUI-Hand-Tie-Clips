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
# D1n. The ASK stage lives in the planner, so its contract is checked there.
PLANNER = os.path.join(ROOT, "planner.py")
# D1l. The composer's size and the thread's growth are both CSS, so the
# checks for them cannot read the JS.
CSS = os.path.join(ROOT, "js", "h3_ref_chain.css")

fails = []


def src(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


js = src(JS)
py = src(PY)
llm = src(LLM)
planner = src(PLANNER)
css = src(CSS)
# Structural CSS checks read `css_code`, never `css`. A rule's own comment is
# allowed to NAME the value it replaced -- that is the most useful comment
# there is -- and a check that greps the raw file cannot tell a declaration
# from a sentence about one. The first version of the D1l floor check failed
# on its own comment saying "was max-height: 260px".
css_code = re.sub(r"/\*.*?\*/", "", css, flags=re.S)


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

# --- 10. D1k: a rejected schema is remembered, not re-learned every time -----
# On nInfer the propose burned 568 s and then said so:
#   [INFO] Prompt executed in 568.09 seconds
#   [HandTieClips] server rejected structured output; retrying without it
# The repair loop dropped response_format and re-posted the SAME prompt, so
# the cost was the failed attempt plus the real one -- and it would repeat on
# every single generate, because nothing remembered.
check("the body skips response_format on a server that refused it",
      re.search(r"if schema is not None and structured_supported\(\):", llm) is not None,
      "-- every generate pays a full wasted generation to relearn this")
check("the refusal is recorded, keyed by ADDRESS not by backend name",
      "_NO_STRUCTURED" in llm and "_active_base()" in llm,
      "-- two servers of the same kind can differ; backend_name would poison "
      "both from one 4xx")
check("an unknown server still gets the schema",
      re.search(r"return _active_base\(\) not in _NO_STRUCTURED", llm) is not None,
      "-- assuming the worst costs the retry we already had")
check("the log says the finding is banked, not just refused this once",
      "does not accept structured output; banked in" in llm,
      "-- a silent behaviour change looks like the panel misreading")

# And it must only be believed when the server NAMED the schema. A 404, a 401
# or a 400 for an unloaded model all land on the same branch, and any of them
# would permanently disable a capability the server does have.
#
# STRUCTURAL, not a token search. D1k's first version asserted
# `blames_schema =` exists, which passed when the line was there and nothing
# read it. It also has to survive the work moving into a helper, so these
# follow the CALL rather than the add.
_BS = llm.find("blames_schema = any(")
_GUARD = llm.find("if blames_schema:")
_CALL = llm.find("_remember_no_structured()", _GUARD) if _GUARD > 0 else -1
_RETRY = llm.find("schema=None", _GUARD) if _GUARD > 0 else -1
_HELPER = llm.find("def _remember_no_structured")
_ADD = llm.find("_NO_STRUCTURED.add(", _HELPER) if _HELPER > 0 else -1

check("a 4xx alone is not enough to remember it",
      _BS > 0 and _GUARD > _BS,
      "-- 'HTTP 4' also covers a wrong path, a refused key, and a model that "
      "is not loaded; any of those would downgrade every later generate")
check("...and the remembering actually sits INSIDE that guard",
      _GUARD > 0 and _CALL > _GUARD and llm[_GUARD:_CALL].count("\n") <= 10,
      "-- an assignment nobody branches on is a comment")
for _w in ("response_format", "json_schema", "unsupported"):
    check("...and it listens for %r" % _w, ('"%s"' % _w) in llm)
check("...but still retries without the schema when it is something else",
      _RETRY > _CALL > 0,
      "-- the retry must still happen; only the REMEMBERING is conditional")
check("the helper banks the ADDRESS it was learned against",
      _HELPER > 0 and _ADD > _HELPER
      and re.search(r"def _remember_no_structured\(\):.*?"
                    r"addr = _active_base\(\).*?_NO_STRUCTURED\.add\(addr\)",
                    llm, re.S) is not None,
      "-- keying it on anything else pins one server's answer onto another")
check("the key is the address, not the backend name",
      re.search(r"_NO_STRUCTURED\.add\((?:\"ninfer\"|'ninfer'|backend)", llm) is None,
      "-- one 4xx on one port would downgrade a second server on another")

check("a settings file that cannot be written is not a failed generate",
      re.search(r"could not bank the structured-output finding", llm) is not None,
      "-- the in-memory set already does the real work")

# The dropped-connection message used to assert a CAUSE it had no evidence for
# -- "it is probably still loading the model -- wait until the status line says
# a model is loaded" -- and it was wrong twice over: nInfer loads a 27B in
# seven seconds, and this panel cannot report load state on a backend that
# returns no `state` field. It cost a minute of chasing a model that was
# already loaded.
_dropped = llm[llm.index("if _dropped_connection(exc):"):][:1400]
# Read it with the full-line comments removed. The comment ABOVE this raise
# quotes the old wording in order to explain why it went -- and a check that
# greps prose finds its own explanation. Fourth time this phase that a file's
# comments were mistaken for its code.
_dropped = "\n".join(l for l in _dropped.splitlines()
                     if not l.lstrip().startswith("#"))
check("the dropped-connection message does not blame the model load",
      "still loading" not in _dropped and "wait until the status line" not in _dropped,
      "-- it names a cause it cannot see, and points at a status line that "
      "does not exist on a backend with no load state")
check("...and says what is actually true instead",
      "send it again" in _dropped
      and "server's own log" in _dropped,
      "-- usually transient, and the evidence is in the server's log, not here")

# --- 10b. D1m: banked on disk, because "restart and pay it again" is not a fix
# These were written once already and then silently deleted by a scripted
# block rewrite, which is why three of the negative tests below passed with
# the feature gone. The negative test is the only reason that was caught.
check("the finding is written to the settings file, not just remembered",
      re.search(r"def _remember_no_structured", llm) is not None
      and re.search(r"def _restore_no_structured", llm) is not None,
      "-- in memory means every ComfyUI restart pays the 568 s again")
check("...and reloaded at startup",
      # The CALL, indented inside load_conn. A bare `_restore_no_structured()`
      # search matches its own `def _restore_no_structured():` line, so deleting
      # the call left this green -- the negative test found that too.
      re.search(r"def load_conn.*?^\s{4}_restore_no_structured\(\)", llm,
                re.S | re.M) is not None)
check("the stored value is the ADDRESS, not a bare true",
      re.search(r'entry\["no_structured"\]\s*=\s*addr\b', llm) is not None,
      "-- the flag lives on an entry whose address is editable, so a boolean "
      "follows the server to a port it was never asked about")
check("...and only applies while the entry still points at that address",
      re.search(r"if addr and addr == normalise_base\(banked\)", llm) is not None)
check("...and a bare true from an older file is ignored, not honoured",
      re.search(r"if not banked or not isinstance\(banked, str\)", llm) is not None,
      "-- a bool is exactly what the address check exists to reject")

# --- 11. D1l: the composer takes a paragraph, the reply takes the pane ------
# Two boxes, opposite ends, and both were too small. The composer was a
# single-line <input>, which cannot hold a paragraph of brief or a piece of
# dialogue, and the thread was capped at max-height: 260px -- four scrolls to
# read a reply, on a tab whose whole purpose is the reply.
check("the composer is a textarea, not a one-line input",
      re.search(r'el\("textarea",\s*"h3e-writer-chat"\)', js) is not None
      and 'el("input", "h3e-writer-chat")' not in js,
      "-- a one-liner truncates a pasted brief with no way to see what is there")
check("and it has room to type into",
      re.search(r"\.h3e-writer-chat\s*\{[^}]*flex:\s*1 1 auto", css_code) is not None
      and re.search(r"\.h3e-writer-chat\s*\{[^}]*min-width:\s*0", css_code) is not None)
check("Send sits beside the last line, not the middle",
      re.search(r"\.h3e-writer-composer\s*\{\s*align-items:\s*flex-end", css)
      is not None,
      "-- a 3-line box with a vertically centred button reads as misaligned")

# Enter must still send -- this box has always done that, and changing it
# would be a surprise. Shift+Enter is the newline.
_kd = js[js.index('el("textarea", "h3e-writer-chat")'):]
_kd = _kd[:_kd.index("chatRow.appendChild")]
check("Enter still sends",
      "e.key !== \"Enter\"" in _kd
      and "sendCanned(composerStage(), chat.value)" in _kd,
      "-- the box has always sent on Enter, and D1n changed WHICH stage it "
      "sends, not whether it sends")
check("Shift+Enter is a newline, not a turn",
      re.search(r"e\.key\s*!==\s*\"Enter\"\s*\|\|\s*e\.shiftKey", _kd) is not None,
      "-- without the modifier test the textarea posts an empty turn per line")
check("the newline is not swallowed either",
      "e.preventDefault()" in _kd,
      "-- a bare `return` above it would still let Enter submit the form")
# IME: on a Japanese or Chinese keyboard Enter COMMITS the candidate. Without
# this, composing a sentence ate the character and posted a blank turn -- and
# the character is lost, not just unsent.
check("Enter does not fire while an IME candidate is being composed",
      "e.isComposing" in _kd and "229" in _kd,
      "-- a CJK keyboard loses the composed character and sends an empty turn")

# The thread grows into the pane instead of being capped.
_cs = css_code[css_code.index(".h3e-chat {"):]
_cs = _cs[:_cs.index("}")]
check("the thread has a floor, not a ceiling",
      re.search(r"min-height:\s*\d+px", _cs) is not None
      and "max-height" not in _cs,
      "-- max-height: 260px was a number nobody had measured against a reply")
check("and it is the thing that grows",
      re.search(r"flex:\s*1 1 auto", _cs) is not None)
check("the body is what lets it grow",
      re.search(r"\.h3e-writer-body\s*\{[^}]*display:\s*flex", css_code) is not None
      and re.search(r"\.h3e-writer-body\s*\{[^}]*flex:\s*1 1 auto", css_code) is not None,
      "-- flex:1 on a child of a static parent buys nothing")
check("a short pane keeps a readable thread rather than crushing it",
      re.search(r"\.h3e-chat\s*\{[^}]*min-height:\s*200px", css_code) is not None)

# --- 11b. D1l regression, found by LOOKING at it: the draft box overflowed ---
# Making the body a flex column gave every child `flex: 0 1 auto`. When a draft
# appeared next to a thread holding `flex: 1`, the draft -- which wraps a 46vh
# scrolling list -- was squeezed smaller than its own content, and having no
# `overflow` of its own it painted outside its own border.
check("the draft box cannot be shrunk below its content",
      re.search(r"\.h3e-writer-draft\s*\{[^}]*flex:\s*0 0 auto", css_code)
      is not None,
      "-- a squeezed box with no overflow draws its content on the panel "
      "behind it, which is what the overlap was")
check("the invariant is stated for every sibling, not just the draft",
      re.search(r"\.h3e-writer-body\s*>\s*\.h3e-writer-draft[^{]*\{[^}]*"
                r"flex:\s*0 0 auto", css_code) is not None,
      "-- patching one box leaves the next tall sibling to fail the same way")
# --- 11c. one level up, found by the user on the Layout card (2026-10-11) ---
# Every child of the WRITE pane kept its height, but the PANE itself was
# `flex: 1 1 auto; min-height: 0` in the scroller, so it shrank to the
# scroller's height and its content painted below its own background.
check("a tab pane grows to fill the scroller but never shrinks below its content",
      re.search(r"\.h3e-pane\s*\{[^}]*flex:\s*1 0 auto", css_code) is not None,
      "-- a shrinkable pane draws a tall tab (a Layout card) past its own "
      "background")
check("and the thread is still the only thing that shrinks",
   re.search(r"\.h3e-chat\s*\{[^}]*flex:\s*1 1 auto", css_code) is not None)

# --- 12. D1n: ASK -- a conversation that cannot commit to a plan ------------
# Free text went to `develop`, the route's else-branch, which writes a plan.
# Typing "test" produced a ten-beat film: correct behaviour, and not what
# anyone typing one word into a chat box meant. There was no way to ASK.
check("the route accepts an `ask` stage",
      re.search(r"if stage not in \([^)]*[\"']ask[\"']", py) is not None,
      "-- without it the panel posts a stage the route rejects, which is "
      "exactly the D1j failure")
# Not the whole tuple: pinning it meant adding `solve` broke a check about
# `ask`, which is the brittleness this pack keeps paying for. What matters is
# that every stage the PANEL sends is accepted by the ROUTE.
check("...and dispatches it first among the model stages",
      py.index('if stage == "ask"') < py.index('elif stage == "propose"')
      if 'if stage == "ask"' in py and 'elif stage == "propose"' in py else False)

# The panel and the route must agree, in BOTH directions. A stage the panel
# sends that the route rejects is the D1j bug (`chat`); a stage the route
# accepts that no branch handles is the same bug wearing a different hat.
_SENT = set(re.findall(r'sendCanned\("(\w+)"', js)) | set(
    re.findall(r'stage:\s*"(\w+)"', js))
_WL = py[py.index("if stage not in ("):]
_TUP = _WL[:_WL.index(")")]
_ACCEPTED = set(re.findall(r'"(\w+)"', _TUP))
# `develop` has no `stage == "develop"` branch BY DESIGN -- it is the else,
# so an unknown-looking stage lands on it. Looking only for explicit branches
# reports it as unhandled, which is the check being wrong.
_EXPLICIT = set(re.findall(r'stage == "(\w+)"', py))
# Song and solve are dispatched together, ahead of the writer (no model call):
# `if stage in ("song", "solve"):` with solve explicit and song its else.
_EARLY = re.search(r'if stage in \(([^)]*)\):', py)
if _EARLY:
    _EXPLICIT |= set(re.findall(r'"(\w+)"', _EARLY.group(1)))
_HANDLED = _EXPLICIT | ({"develop"} if re.search(r"else:\s*\n\s*out = await "
                                                         r"_planner\.write_plan\(", py)
                        else set())
check("every stage the panel sends, the route accepts",
      bool(_SENT) and _SENT <= _ACCEPTED,
      "panel sends %s, route accepts %s" % (sorted(_SENT), sorted(_ACCEPTED)))
check("and every stage the route accepts, a branch handles",
      _ACCEPTED <= _HANDLED,
      "accepted but never dispatched: %s" % sorted(_ACCEPTED - _HANDLED))
check("`develop` really is the else-branch, not a missing case",
      "develop" in _HANDLED and 'stage == "develop"' not in py,
      "-- an absent explicit branch is only correct if something else is")
check("the composer offers the choice rather than guessing",
      re.search(r'const chipRow = el\("div", "h3e-writer-row h3e-stage-chips"\)',
                js) is not None,
      "-- a heuristic is wrong in the expensive direction: a misrouted turn "
      "costs a full generate at ~15 tok/s")
check("Enter sends whatever the chip says",
      "sendCanned(composerStage(), chat.value)" in js
      and js.count("sendCanned(composerStage(), chat.value)") == 2,
      "-- both the keydown and the Send button, or one of them keeps writing "
      "a plan behind the chips' back")
check("NO composer path still defaults to a plan",
      "sendCanned(null, chat.value)" not in js,
      "-- the D1j bug in a new place: a stage the route has to reinterpret")
check("`develop` stays the default, not `ask`",
      re.search(r'let composerStageId = "develop"', js) is not None,
      "-- someone typing a sentence has almost always typed a brief; "
      "silently changing what free text MEANS is the same surprise")

# The stage is the one thing that must not fall through to a plan.
_send = js[js.index("async function sendCanned"):]
_send = _send[:_send.index("\n    }") if "\n    }" in _send else 4000]
check("the stage is passed through, never defaulted away",
      "stage: stage || " in _send,
      "-- an empty stage must not silently become a plan")

# --- 12b. the ASK stage itself
check("planner exposes write_ask",
      re.search(r"async def write_ask\(", planner) is not None)
check("it asks for no schema, so no JSON can come back",
      re.search(r"def write_ask\(.*?complete_fn\(messages, schema=None\)", planner,
                re.S) is not None,
      "-- a conversational turn has nothing to validate")
_ASK = planner[planner.index("async def write_ask("):
               planner.index("async def describe_frame(")]
check("it never runs the repair loop",
      "validate(" not in _ASK
      and not re.search(r"schema\s*=\s*(?!None)\w", _ASK),
      "-- there is no right answer to converge on, and a non-None schema here "
      "would be a structure this stage is defined not to produce")
check("it resends the proposal and the plan every turn",
      re.search(r"def write_ask\(.*?shot_text=shot_text", planner, re.S) is not None
      and "ref_text=ref_text" in planner,
      "-- the plan can be accepted or reverted between two turns of talking")
check("it returns prose, and no plan key at all",
      re.search(r'return \{"ok": True, "text": text', planner) is not None
      and '"shot_plan"' not in planner[planner.index("async def write_ask("):
                                      planner.index("async def describe_frame(")],
      "-- a stage that can return a plan is a stage that can surprise")
check("and a long answer is cut, with a warning",
      "max_chars" in planner and "was cut at" in planner,
      "-- at 15 tok/s a 2,000-word reply is four minutes of scrolling, and a "
      "length instruction the model ignores is only a suggestion")

# --- nInfer's Test (user, 2026-10-11: "the test never turns green") ---
# nInfer reports no load state, so every model's `loaded` is None and
# `any(...)` read that as "nothing loaded": the red "wait for nInfer" on
# every Test, which this file's own section 6b-ii calls a shipped mistake.
print("\nTest on a server that cannot report load state")
import ast as _ast
_llm_tree = _ast.parse(llm)
_fn = next(n for n in _llm_tree.body
           if isinstance(n, _ast.FunctionDef) and n.name == "any_loaded")
_ns = {}
exec(_ast.get_source_segment(llm, _fn), _ns)
check("the route reports the tri-state, not any() over Nones",
      '"any_loaded": _llm.any_loaded(found)' in py)
check("all states unknown (nInfer) is UNKNOWN, not 'nothing loaded'",
      _ns["any_loaded"]([{"loaded": None}, {"loaded": None}]) is None,
      repr(_ns["any_loaded"]([{"loaded": None}])))
check("one loaded is loaded; all reported unloaded is unloaded",
      _ns["any_loaded"]([{"loaded": False}, {"loaded": True}]) is True
      and _ns["any_loaded"]([{"loaded": False}]) is False)
check("Test turns green when the server answers and nothing says unloaded",
      re.search(r'testBtn\.classList\.toggle\("h3e-test-ok",\s*'
                r'Boolean\(conn\.online\) && conn\.any_loaded !== false\)', js)
      is not None and re.search(r"\.h3e-btn\.h3e-test-ok\s*\{[^}]*--h3-ok",
                                css_code) is not None,
      "-- Start promises it; nothing turned it green before")
check("an unknown load state gets its own message, not the red one",
      "does not report load state" in js
      and "conn.any_loaded === false" in js)

print()
if fails:
    print("WRITER UI: %d check(s) failed." % len(fails))
    sys.exit(1)
print("WRITER UI: all passed.")