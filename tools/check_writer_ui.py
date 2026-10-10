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

fails = []


def src(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


js = src(JS)
py = src(PY)


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
# The guard must exist AND the flag it consults must come from a liveness
# probe -- a bare `and False` would satisfy the first half.
down = re.search(r"_writer_down\s*=\s*([^\n]+)", py)
check(
    "the eviction branch is guarded on the writer being down",
    re.search(r"if _llm\.shares_this_gpu\([^)]*\)\s+and\s+_writer_down:", py)
    is not None,
    "-- a serving writer is stopped by the request that is about to use it",
)
check(
    "_writer_down is derived from a liveness probe",
    down is not None and "server_running" in down.group(1),
    "-- the guard must not be a constant",
)

print()
if fails:
    print("WRITER UI: %d check(s) failed." % len(fails))
    sys.exit(1)
print("WRITER UI: all passed.")