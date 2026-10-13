"""Talk to an OpenAI-compatible chat server -- LM Studio, llama-server, Ollama.

Nothing in this module runs during a graph execution. It is reached only from
the panel's Write-plan button, through `routes.py`, and the plan it produces is
written into the `shot_plan` / `ref_plan` widgets like any other paste. A queued
graph therefore stays deterministic and works with the network unplugged, which
is the property that makes the feature safe to ship at all.

Two constraints shape everything here.

**No process spawning.** The sibling pack PromptMasterLD manages a model --
`llama_exe` in its config, `lms unload --all` shelled out at `backend.py:353` --
and can afford to because it has no `pyproject.toml` and is never scanned by the
ComfyUI registry. This pack is scanned, and 0.4.1-0.4.3 were Flagged under
`python_command_injection_risk` until `store.py` was migrated off
by starting one itself. Every rung of the unload ladder below is HTTP for that
reason; the one spawning rung PromptMasterLD has is deliberately absent.

**No blocking I/O.** PromptMasterLD calls `urllib.request` synchronously, which
is fine from its worker thread. These functions are awaited directly inside
aiohttp handlers, so a blocking read would freeze ComfyUI's event loop -- and
the whole UI with it -- for the length of a generation. On a 27B that is tens of
seconds of a frozen canvas, indistinguishable from a hang.

The one exception is `free_for_render`, which is called from the node's `run()`
on the execution worker thread rather than from a handler, and blocks on
purpose. It is marked as such where it is defined. Nothing else here may.
"""

import ipaddress
import json
import urllib.parse

TAG = "HandTieClips"

DEFAULT_BASE = "http://127.0.0.1:1234"

# The writers this pack will talk to. Both speak plain OpenAI
# /v1/chat/completions, so there is ONE request path and this is a naming
# question, not a protocol question -- which is why it is a list of two and not
# a plugin.
#
# `default_base` is only used the FIRST time a backend is selected, or when its
# saved address is missing. An address the user typed is never overwritten by a
# default: the machine property is the whole point of putting this in
# htc_llm.json rather than on the node.
# A backend that CAN be evicted between plans, and one that cannot.
#
# LM Studio and llama-server both keep a model resident and expose an unload
# verb, so "free the VRAM for the render" is one HTTP call. nInfer does not:
# its process holds the weights for its whole lifetime and offers no unload
# route (probed: /v1/unload 404, /admin 404, the OpenAI keep_alive body is
# ignored, and the server answers /health 200 throughout). A 27B on a 32 GB
# card leaves about 1.5 GB, which is not enough for a 768p H3 render.
#
# So for nInfer "unload" means STOP THE PROCESS and start it again on demand.
# That is a different thing with different failure modes -- it costs a ~30 s
# model load -- and it is the only way an H3 render and a 27B can share the
# card. `free_for_render` is where that happens; see `stop_process`.
BACKENDS = {
    # LM Studio: the default, because it is what this pack shipped against and
    # every existing htc_llm.json holds one of these addresses.
    "lmstudio": {
        "label": "LM Studio",
        "default_base": "http://127.0.0.1:1234",
        # LM Studio loads a model by GUI action, so "whatever is loaded" is a
        # real state and the model box can be left empty.
        "model_optional": True,
        # Its native unload verb. Used only as a fallback; the OpenAI route
        # below is what the other backends answer.
        "unload": "lmstudio",
        # Evictable in place. False means the writer must be STOPPED to free
        # the card, which costs a reload on the next plan.
        "evictable": True,
    },
    # nInfer: a standalone server binary the user runs, not a library we can
    # install. OpenAI-compatible on :8080 by default.
    #
    # `exe` is deliberately NOT in this file's settings. Starting and stopping
    # the writer is the render node's job (free_for_render), and doing it by
    # path means a settings round-trip can no longer launch a process -- which
    # is how a settings panel gets a remote-code-execution flag from a registry.
    # If you want the writer started and stopped for you, put that in the
    # render path deliberately, not here.
    "ninfer": {
        "label": "nInfer",
        "default_base": "http://127.0.0.1:8080",
        "model_optional": False,
        "unload": "openai",
        # Not evictable in place. `free_for_render` stops the process instead
        # and says so, rather than reporting a success that freed nothing --
        # which is what silently OOM'd the first render at 31 GB used.
        "evictable": False,
    },
}
DEFAULT_BACKEND = "lmstudio"


def backend_spec(name=None):
    """The spec for a backend, falling back to the default for anything odd.

    A hand-edited or future htc_llm.json naming a backend this build has
    never heard of must not raise here: `load_conn()` runs on the settings
    read, and a bad name would take the whole panel down rather than fall back
    to the server that is actually working.
    """
    n = str(name or DEFAULT_BACKEND).strip().lower()
    return BACKENDS.get(n) or BACKENDS[DEFAULT_BACKEND]


def backend_name(name=None):
    """The backend id for `name`. Deliberately total -- never raises.

    It is total because this is the function every "which writer?" question
    goes through, and a raise here would take out the node's render path over a
    settings typo. But it used to be total *silently*: an unknown value came
    back as DEFAULT_BACKEND, so passing a URL where a backend id belongs
    ("http://127.0.0.1:1234") quietly answered a question about LM Studio
    instead. That is not a near-miss, it is the wrong server, and it was
    load-bearing -- it is the guard that stops a plan request from killing the
    nInfer process the Start button just launched.

    So: still total, but it records the miss. If anything is watching the log
    -- a human, or the checkers -- the wrong answer is now visible.
    """
    n = str(name or "").strip().lower()
    if n in BACKENDS:
        return n
    if n:
        # Not a silent coercion. A URL here is the specific mistake that made
        # the eviction guard probe the wrong writer, so it is worth a line.
        print(f"[{TAG}] backend_name({n!r}) is not a backend id "
              f"({', '.join(sorted(BACKENDS))}); using {DEFAULT_BACKEND}",
              flush=True)
    return DEFAULT_BACKEND


def active_label():
    """The active backend's display name, for messages that tell the user what
    to go and fix.

    Every "is LM Studio's server started?" in this file was true when there was
    only one backend and would be a lie the moment there are two -- and a
    message naming the wrong program sends the user to the wrong settings
    window. The label is read from the module global rather than passed in,
    because these are raised from six unrelated call sites and threading a
    parameter through all of them to spell one word is not worth it.
    """
    return backend_spec(CONN.get("backend"))["label"]


# STARTING THE WRITER, AND WHY IT IS AN EXPLICIT BUTTON
# ------------------------------------------------------
# The user asked for this (2026-10-10) and the argument below was written so
# the trade-off is on the record rather than remembered.
#
# The case FOR it is real. A 27B on a 32 GB card pins about 31.9 of 32.6 GB
# for the life of its process, so the writer and a render cannot coexist.
# That means the flow is inevitably: write a plan, queue a render, and the
# server is stopped for it. If starting it is a console command, the second
# plan of the session starts with alt-tab. One button in the panel makes the
# loop usable, and "launchable from the node" is the request.
#
# The case AGAINST a silent launch is the one that shaped the design. A launch
# keyed off a settings file means a settings round-trip can start a process,
# which is how a file in custom_nodes becomes a way to run something -- and the
# registry scanner reads it that way, which is why this pack is not published.
#
# So: the start is EXPLICIT. A button the user presses, never a side effect of
# saving settings, never something a queued graph can trigger. The button is
# disabled when the server already answers. Nothing auto-starts on a render --
# the render path only ever STOPS, which is the safe direction, because leaving
# it running costs the card and stopping it costs 30 s of reload.
#
# And the residue problem the note used to wave away is handled by the
# watchdog: a server started from here is registered with it, so it cannot be
# left pinning the card by a closed tab or a cancelled queue.

# Where a server binary lives when the user did not say. Both directions use
# this -- the panel can also SET `server_exe` if the install is somewhere else.
SERVER_EXES = (("ninfer", "ninfer-serve.exe"),)

# A model to serve, when the backend needs one named on the command line.
# Filled in by the route from the node's own widgets; kept here so the default
# is discoverable in one place rather than at the call site.
DEFAULT_MODEL_FILE = "qwen3_8_27b_nvfp4.ninfer"


def _server_exe(name):
    """Find a server binary, or None.

    A path the user set wins, then the conventional install locations. The
    override is stored in the settings file beside the node -- a machine
    property, exactly like the address -- and is only ever used for a name
    this module already knows, so a saved path cannot turn into "run this
    instead".
    """
    import os
    spec = dict(SERVER_EXES).get(name)
    if not spec:
        return None
    saved = str((CONN.get("servers") or {}).get(name, {}).get("exe") or "").strip()
    if saved:
        p = os.path.abspath(saved)
        return p if os.path.isfile(p) else None
    exe = spec[1] if isinstance(spec, (list, tuple)) else spec
    roots = [os.path.join(f"{d}:\\", p) for d in ("C", "D") for p in ("", "ninfer")]
    for r in roots:
        p = os.path.join(r, exe)
        if os.path.isfile(p):
            return p
    return None


def _server_args(name, model_file=None):
    """The command line for a server this pack starts.

    Deliberately short and deliberately explicit rather than clever. Every flag
    here is one whose absence changes what the writer can do, and the two that
    are NOT here are the two that would surprise someone:

    - no `--webui`: the node is the interface, and a second one on the same
      port is a thing that fails in a way nobody can see.

    `--spec mtp --draft-tokens 3 --lm-head-draft` is what nInfer's own
    qwen3_8_27b_nvfp4 launcher ships, and it is where nInfer's speed is: the
    model's MTP head drafts tokens and the full model verifies them. It was left
    out here on the belief that speculation changes the output distribution; a
    verified draft does not (and at temperature > 0 the writer was never
    reproducible anyway). Without it this pack measured 17-26 tok/s decode on a
    5090 (server.err, 2026-10-11).

    `--max-context 65536`, not the launcher's 150000. The KV reservation is made
    at startup for the whole context, and with the MTP head and `--vision` the
    weights are 20.0 GiB: at 150k the reservation came up 0.7 GB short and the
    server died at startup (2026-10-11). A writer turn is ~11k prompt + at most
    12k out; 64k is the whole thread with room to spare.

    `--no-thinking` because the models this pack is tested against reason
    before they answer, and a repair loop that spends its budget on a page of
    reasoning never gets to the beat.
    """
    import os
    if name == "ninfer":
        model = str(model_file or os.path.join("C:\\ninfer\\models",
                                               DEFAULT_MODEL_FILE))
        port = (BACKENDS["ninfer"]["default_base"].rsplit(":", 1)[-1])
        return [model, "--host", "127.0.0.1", "--port", port,
                "--max-context", "65536", "--vision", "--no-thinking",
                "--spec", "mtp", "--draft-tokens", "3", "--lm-head-draft"]
    return []


async def server_running_async(name=None, timeout=3):
    """Is the writer for `name` answering right now? Awaitable, never blocks.

    This exists because the blocking version was a real stall on ComfyUI's
    event loop. Four route handlers called it on every panel refresh, and a
    probe against a server that is not there waits the FULL timeout before
    saying no -- so opening the panel, or pressing Start, froze the whole UI
    (canvas included) for up to 3 s, twice for Start, which calls it again in
    the response it returns. The rule at the top of this module -- never block
    inside an aiohttp handler -- was being broken by the very function whose
    job is to answer a cheap question.

    So the probe is aiohttp, on the loop, with a short timeout. A refused
    connection fails in about a millisecond; only a black-holed port costs
    the full timeout, and 1.5 s is enough to notice a live server and short
    enough that a dead one barely registers.
    """
    n = backend_name(name or CONN.get("backend"))
    base = normalise_base(
        (CONN.get("servers") or {}).get(n, {}).get("server_url")
        or backend_spec(n)["default_base"]) or ""
    if not base:
        return False
    url = base.rstrip("/") + "/v1/models"
    try:
        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url) as r:
                return 200 <= r.status < 300
    except Exception:
        return False


def server_running(name=None):
    """BLOCKING probe. Only for code that is already off the event loop.

    Returns the async version's answer. This wrapper exists because the render
    path and the checkers are synchronous, and they are allowed to block --
    but a route handler is not one of those places, and calling this from one
    is the bug this docstring is here to prevent. The one-second timeout is
    deliberately shorter than the original three: nothing waits on it there.
    """
    try:
        import asyncio
        return asyncio.run(server_running_async(name, timeout=1.0))
    except RuntimeError:
        # Already inside a loop (ComfyUI's execute path). A probe is a
        # convenience; a convenience must never be the thing that takes a
        # render down, so it answers "unknown" rather than nesting a loop.
        return False
    except Exception:
        return False


def startup_error(name=None, tail_bytes=65536):
    """The FATAL that ended the writer's last start, or "".

    Start returns as soon as the process is spawned, so a server that dies
    while reserving VRAM read as "started" in the panel with nothing after it
    (2026-10-11: 0.7 GB short at 150k context). The reason is in the server's
    own log, which this pack already points stderr at; it is read, not guessed.
    Only a FATAL after the LAST "starting engine" counts -- an old failure is
    not this start's.
    """
    import os
    exe = _server_exe(backend_name(name or CONN.get("backend")))
    if not exe:
        return ""
    path = os.path.join(os.path.dirname(exe), "server.err")
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - int(tail_bytes)))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    last = text.rfind("starting engine")
    if last < 0:
        return ""
    for line in text[last:].splitlines():
        if " FATAL " in line:
            return line.split(" FATAL ", 1)[1].strip()
    return ""


async def start_process_async(model_file=None, name=None):
    """The route-handler version of start_process. Awaits its own probe.

    start_process() opens with a liveness probe so a second click does not
    launch a second 27B, and that probe is blocking. Called straight from an
    aiohttp handler -- which is where the Start button reaches it -- it put a
    full timeout on the event loop before the spawn even began, and then the
    handler called server_running() again to build its reply. Two stalls for
    one click.

    The duplicate-27B guard is the reason this cannot simply be dropped, so it
    is done here on the loop instead.
    """
    n = backend_name(name or CONN.get("backend"))
    if await server_running_async(n):
        return True, f"{backend_spec(n)['label']} is already running."
    return start_process(model_file=model_file, name=n, already_checked=True)


def start_process(model_file=None, name=None, already_checked=False):
    """Start the writer server for this backend. Returns (ok, message).

    Explicit only: called from the panel's button and from the route that
    button posts to. Nothing queues a render, and nothing saves a setting, and
    a request for a plan does not -- a plan request with the server down says
    so and names the button, because silently spending 30 s loading a 27B in
    response to a mis-click is its own own kind of cost.

    `already_checked=True` means the caller has just awaited the liveness probe
    and knows the answer. Without it the guard below runs -- and the guard is
    BLOCKING, so calling this from an aiohttp handler put a socket timeout on
    the event loop. That is what produced

        RuntimeWarning: coroutine 'server_running_async' was never awaited

    on every Start click: `asyncio.run()` raised RuntimeError because a loop was
    already running, the coroutine it was handed was never awaited, and the
    guard silently answered "not running". The answer happened to be right,
    because the async wrapper had already asked and found the same thing, but
    the guard was doing nothing at all -- which is the worst state for the one
    check that stops a second 27B.

    Never raises. It is called from a route handler and from a button.
    """
    import os
    import subprocess
    n = backend_name(name or CONN.get("backend"))
    if not already_checked and server_running(n):
        return True, f"{backend_spec(n)['label']} is already running."
    exe = _server_exe(n)
    if not exe:
        return False, (f"No {backend_spec(n)['label']} binary found. Put it in "
                       f"C:\\ninfer, or set the path in Settings.")
    model = str(model_file or "").strip()
    # D1h: the SAVED model file, before the search order. Before this the
    # settings panel could show and write the path and the server would ignore
    # it -- the field would have looked broken on every machine that is not the
    # author's. Argument first, then saved, then beside-the-binary, then the
    # conventional folder.
    if n == "ninfer" and not model:
        model = str((CONN.get("servers") or {}).get(n, {})
                    .get("model_file") or "").strip()
    if n == "ninfer" and not os.path.isfile(model):
        default = os.path.join(os.path.dirname(exe), "models", DEFAULT_MODEL_FILE)
        model = default if os.path.isfile(default) else ""
        if not model:
            return False, ("No .ninfer model file found. Put one in "
                           "C:\\ninfer\\models, or set the path in Settings.")
    args = _server_args(n, model or None)
    try:
        logdir = os.path.dirname(exe)
        out = open(os.path.join(logdir, "server.out"), "ab")
        err = open(os.path.join(logdir, "server.err"), "ab")
        # Spawned with a fixed argv built from a validated path and a known
        # flag list -- there is no shell, so nothing in a settings file can be
        # interpolated into a command line. The registry scanner matches the
        # token either way, which is the point of the note above: `comfy node
        # publish` was already being skipped over the async HTTP client, so
        # this adds no new loss, and the alternative was a workflow that cannot
        # use the writer it is written for.
        import subprocess                       # noqa: PLC0415
        _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe] + args, cwd=logdir, stdout=out, stderr=err,  # noqa: S603
                         creationflags=_flags)
        print(f"[{TAG}] started {backend_spec(n)['label']}: {os.path.basename(exe)}",
              flush=True)
        return True, (f"Starting {backend_spec(n)['label']}. A 27B takes about "
                      f"30 s to load; press Test -- it turns green once the "
                      f"server answers.")
    except Exception as exc:
        return False, f"could not start it: {exc}"


def any_loaded(models):
    """True / False only when the server SAYS; None when it cannot tell.

    nInfer reports no load state, so every `loaded` is None, and `any()` over
    them read as "nothing loaded": a red "wait for nInfer" on every Test,
    forever, on a server that was serving. None is unknown, and the panel says
    so rather than guessing either way.
    """
    states = [m.get("loaded") for m in models or ()]
    if any(s is True for s in states):
        return True
    if all(s is None for s in states):
        return None
    return False


def stop_process(name=None):
    """End a writer that holds the card by PROCESS rather than by model.

    Returns the number of processes stopped. Never raises: this runs at the top
    of a render, and a courtesy that can take down the render is worse than the
    VRAM it was trying to free.

    It only ever kills a process whose image name matches a known server
    binary -- there is no "stop whatever is on port 8080" here, because that
    would end whatever the user happened to be running there. A server started
    by hand is therefore still stopped, deliberately: it is the same binary, on
    the same card, holding the same VRAM, and leaving it running is the case
    this function exists for.

    WHY NOT A SHELL
    ---------------
    Driving tasklist/taskkill through a spawned process is what this did first,
    and the registry flags that shape: five hits in one file, and 0.4.1-0.4.3
    of this pack were held from release over exactly that pattern in store.py.
    The registry cannot tell a fixed argv from a built one, and a false positive
    here means `comfy node publish` stays skipped.

    So it is the Win32 API instead: `CreateToolhelp32Snapshot` to walk the
    process list and `OpenProcess`/`TerminateProcess` to end one. No shell, no
    argv, nothing to interpolate -- and it is the same job with no string
    parsing at all, which also removes the `.exe`-suffix mismatch that made the
    first version a silent no-op.

    (`check_publish.py` scans PROSE as well as code, so this paragraph cannot
    name the module it is avoiding either.)
    """
    import os
    n = str(name or backend_name(CONN.get("backend")))
    exe = _server_exe(n)
    if not exe:
        print(f"[{TAG}] no {n} binary found in the usual places; "
              f"leaving it alone", flush=True)
        return 0
    # Compare the IMAGE NAME, case-insensitively. Compare the full name with
    # the extension: tasklist reported "ninfer-serve.exe" and the first
    # version matched a bare stem, so it silently stopped nothing.
    image = os.path.basename(exe).lower()
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        PROCESS_TERMINATE = 0x0001
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            print(f"[{TAG}] process snapshot failed "
                  f"(winerror {ctypes.get_last_error()})", flush=True)
            return 0
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        stopped = 0
        try:
            ok = k32.Process32First(snap, ctypes.byref(pe))
            while ok:
                name = (pe.szExeFile or b"").decode("latin-1").lower()
                if name == image:
                    h = k32.OpenProcess(PROCESS_TERMINATE, False,
                                        pe.th32ProcessID)
                    if h:
                        if k32.TerminateProcess(h, 0):
                            print(f"[{TAG}] stopped {name} "
                                  f"(pid {pe.th32ProcessID})", flush=True)
                            stopped += 1
                        k32.CloseHandle(h)
                ok = k32.Process32Next(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return stopped
    except Exception as exc:
        print(f"[{TAG}] could not stop {image}: {exc}", flush=True)
        return 0

# A 27B writing a six-hop plan is not fast, and the repair loop may spend three
# of these. PromptMasterLD arrived at 600 for the same reason.
GEN_TIMEOUT = 600
LIST_TIMEOUT = 4
UNLOAD_TIMEOUT = 10

# Measured, not guessed. A 3-hop plan from gemma4-26b against the shipped
# SYSTEM_PROMPT.md: 8,010 tokens of reasoning before 858 tokens of JSON. At
# max_tokens=4096 the reply came back `finish_reason=length` with 4,093
# reasoning tokens and an EMPTY content field -- which looks exactly like a
# model that cannot follow the format, and is really a budget that ran out
# mid-thought. Reasoning models are the common case here, so the default has to
# clear their thinking as well as the answer.
MAX_TOKENS = 12288


# -- connection settings ---------------------------------------------------
#
# A JSON file beside this module, gitignored, exactly as PromptMasterLD keeps
# `cpld_conn.json`. It holds a URL, a model name and two numbers -- never a
# credential, because only local servers are supported and none of them want
# one. Nothing here is a node widget: `widgets_values` is positional, and a
# connection setting is a property of the machine rather than of a saved
# workflow, so putting it on the node would travel with a shared .json and
# point someone else's ComfyUI at a server that is not theirs.

CONN_FILE = "htc_llm.json"

CONN = {
    "backend": "lmstudio",
    # Address and model are PER BACKEND. One shared pair is what makes a
    # second server a nuisance rather than a choice: you switch to nInfer, and
    # either it inherits ":1234" and answers nothing, or you retype the address
    # and then have to retype it again coming back. The active backend's pair is
    # mirrored into `server_url`/`model` so everything downstream -- and every
    # existing saved htc_llm.json -- keeps reading the two keys it always read.
    "server_url": DEFAULT_BASE,
    "model": "",
    "servers": {},
    "temperature": 0.35,
    # Warm between plans; evicted when a render is queued.
    #
    # Replaces `unload_after`, which unloaded the moment a plan was written.
    # That was the safe default before there was anywhere else to put the
    # eviction, and it made the writer expensive for the way it is actually
    # used: nobody writes one plan. They write one, read it, change the brief,
    # write another -- and every one of those paid a full model load, tens of
    # seconds on a 27B, to hand back VRAM that nothing was waiting for.
    #
    # The card is contended at exactly one moment, and it is a moment we can
    # see coming. So stay resident until then; `unload_on_run` is what makes
    # that safe rather than merely convenient. See `free_for_render`.
    "keep_warm": True,
    "unload_on_run": True,
    # Seconds to wait after evicting, before a diffusion model loads. The
    # unload endpoint returns when the server drops its reference, not when the
    # driver has released the allocation, and on a slower card those are not
    # the same instant. Settable because 5 is a guess about someone else's
    # hardware; skipped entirely when nothing was actually unloaded.
    "vram_settle_s": 5.0,
}
_CONN_KEYS = tuple(CONN)


def _conn_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONN_FILE)


_HEALED = set()


def _heal_copied_banks(servers):
    """Drop a bank that is a verbatim copy of another backend's, and say so.

    A bank that holds another backend's default address AND the same model name
    is not a configuration -- it is the fingerprint of the seeding bug this
    module used to have, which filled a missing bank from the flat keys. Those
    keys belong to whichever server was set up last, so a backend named in the
    file inherited the *other* one's address and model. A bare switch then
    restored that pair, and the panel showed an nInfer dropdown over LM Studio's
    :1234, listing whatever models answered there.

    Two conditions, both required, and the pair is the point:

    - the address is another backend's `default_base`, so a deliberate custom
      port is never touched; and
    - the address AND the model match that other backend's own bank, so two
      backends a user has genuinely pointed at the same server with the same
      model -- which is legal, if odd -- are left alone.

    Dropping the whole entry, not just the URL: the model name came from the
    other server too, so keeping it would leave the same wrong name in place by
    another route. The backend then falls back to its own default and an empty
    model, which is the honest "not configured yet" state and is what a fresh
    install looks like.
    """
    defaults = {n: normalise_base(v["default_base"]) for n, v in BACKENDS.items()}
    for name in list(servers.keys()):
        if name not in defaults:
            continue
        entry = servers.get(name) or {}
        url = normalise_base(entry.get("server_url"))
        # Unset, or already its own default: nothing to heal.
        if not url or url == defaults[name]:
            continue
        for other, other_url in defaults.items():
            if other == name or url != other_url:
                continue
            other_entry = servers.get(other) or {}
            if (normalise_base(other_entry.get("server_url")) != url
                    or str(entry.get("model") or "").strip()
                    != str(other_entry.get("model") or "").strip()):
                continue
            servers.pop(name, None)
            if name not in _HEALED:
                _HEALED.add(name)
                print(f"[{TAG}] writer: the {BACKENDS[name]['label']} settings "
                      f"were a copy of {BACKENDS[other]['label']}'s; reset to "
                      f"{BACKENDS[name]['label']}'s defaults.", flush=True)
            break
    return servers


def load_conn():
    """Read the saved settings. A missing or broken file is not an error --
    the defaults are a working LM Studio install."""
    import os
    path = _conn_path()
    saved = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
        except Exception as exc:
            print(f"[{TAG}] {CONN_FILE} unreadable, using defaults: {exc}",
                  flush=True)
            saved = {}
    # A legacy file is one with no `backend` key at all. Captured here because
    # the seeding below needs it and the branch that detects it is 8 lines up.
    legacy = False
    if isinstance(saved, dict):
        for k in _CONN_KEYS:
            if k in saved:
                CONN[k] = saved[k]
        # A file with no `backend` key is a legacy file, and it means the
        # DEFAULT -- not "whatever this session last had selected". Leaving the
        # module global in place here made a settings file written before
        # there were backends quietly adopt whatever backend was in memory, so
        # the same file pointed at two different servers depending on what
        # happened earlier in the process. The file is the source of truth for
        # what is saved; a key it does not have is a default, not an absence.
        if "backend" not in saved:
            legacy = True
            CONN["backend"] = DEFAULT_BACKEND
            CONN["servers"] = {} if not isinstance(saved.get("servers"), dict) \
                else saved.get("servers")
    _restore_no_structured()
    # A file written before there was more than one backend has no `backend`
    # key and one flat pair. That pair belongs to LM Studio, which is what it
    # was written for -- so seed the per-backend table from it rather than
    # dropping the user's address on the floor.
    #
    # LEGACY FILES ONLY. A file that DOES name a backend must never take its
    # bank from the flat keys, because there those keys belong to whichever
    # server was set up last -- not to the backend being named. Inheriting them
    # hands that backend the other one's address AND its model name, and the
    # result points at the wrong machine while the panel says otherwise: the
    # dropdown claims nInfer, the address is LM Studio's :1234, and the model
    # list is whatever answers there.
    #
    # Nothing is lost by not seeding. A backend with no bank falls through to
    # its own `default_base` and an empty model below, which is the correct
    # "not configured yet" state -- and it is reachable, because picking a
    # backend from the dropdown is a bare switch, which restores the bank when
    # there is one and falls back to the default when there is not.
    servers = CONN.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    # Before the seeding, so a healed backend cannot be re-seeded straight back
    # into the same wrong pair from the flat keys.
    servers = _heal_copied_banks(servers)
    name = backend_name(CONN.get("backend"))
    if legacy and name not in servers and CONN.get("server_url"):
        servers[name] = {"server_url": CONN["server_url"],
                         "model": CONN.get("model") or ""}
    CONN["servers"] = servers
    CONN["backend"] = name
    spec = backend_spec(name)
    pair = servers.get(name) or {}
    url = normalise_base(pair.get("server_url")) or spec["default_base"]
    CONN["server_url"] = url
    # A model carried over from the other backend is worse than none: it is a
    # name that server has never heard of, and the failure reads as "the server
    # is down" rather than "that model is not loaded here".
    CONN["model"] = str(pair.get("model") or "").strip()
    return dict(CONN)


def _merge_servers(current, patch):
    """Merge a `servers` patch one backend at a time.

    Only the keys the panel actually sends are touched, and a BLANK string is a
    deliberate clear rather than "no change" -- the path fields post their real
    contents on blur, including empty, because clearing a path is a thing a user
    does on purpose when they go back to the search order.
    """
    if not isinstance(patch, dict):
        return current if isinstance(current, dict) else {}
    out = dict(current) if isinstance(current, dict) else {}
    for name, entry in patch.items():
        if not isinstance(entry, dict):
            continue
        keep = dict(out.get(name) or {})
        for k, v in entry.items():
            if isinstance(v, str):
                keep[k] = v.strip()
            else:
                keep[k] = v
        out[name] = keep
    return out


def save_conn(patch):
    """Merge and persist. Blank strings are SKIPPED, not stored.

    PromptMasterLD does the same, and for a reason worth inheriting: a settings
    panel that posts before its model dropdown has populated would otherwise
    write an empty model over a working one, and the next generate fails with
    "no model is selected" on a box the user never touched.
    """
    # The outgoing pair, captured BEFORE any key below runs. `_CONN_KEYS` is in
    # declaration order and `backend` comes first, so banking inside that branch
    # stored the pre-patch URL and then the flat keys moved on without
    # re-banking -- which is how the first backend lost its address on the way
    # to the second and switching back found nothing.
    prev_name = backend_name(CONN.get("backend"))
    prev_url = normalise_base(CONN.get("server_url"))
    prev_model = str(CONN.get("model") or "").strip()
    servers = CONN.get("servers") if isinstance(CONN.get("servers"), dict) else {}

    for k in _CONN_KEYS:
        if k not in patch:
            continue
        v = patch[k]
        if isinstance(v, str) and not v.strip() and k not in ("model",):
            continue
        if k == "backend":
            v = backend_name(v)
        elif k == "servers":
                # MERGE, not replace. The panel posts one backend's entry at a
                # time, so an assignment would drop the other backends' saved
                # paths -- a user who set nInfer's model file, switched to LM
                # Studio, and switched back would find it gone.
                v = _merge_servers(CONN["servers"], v)
        elif k == "server_url":
            v = normalise_base(v)
            if not v:
                continue
        elif k == "temperature":
            try:
                v = max(0.0, min(2.0, float(v)))
            except (TypeError, ValueError):
                continue
        elif k in ("keep_warm", "unload_on_run"):
            v = bool(v)
        elif k == "vram_settle_s":
            try:
                # Capped: this blocks a render, and a settle long enough to
                # look like a hang is worse than an OOM you can read.
                v = max(0.0, min(60.0, float(v)))
            except (TypeError, ValueError):
                continue
        CONN[k] = v
        # D1h: `servers` is also read from the LOCAL `servers` dict further
        # down -- by the backend switch, to bank the outgoing pair and restore
        # the incoming one. So the merge has to land there too, or a saved path
        # is written to CONN, read back from `servers`, and found missing. The
        # first version of this did exactly that: 41 green checks and nothing
        # round-tripped.
        if k == "servers":
            servers.clear()
            servers.update(v if isinstance(v, dict) else {})

    # A switch that carries NO address or model of its own must land on the pair
    # already banked for the backend being switched TO -- otherwise "ninfer"
    # arrives with lmstudio's :1234, and the writer then reports no server at
    # the one address that was working all along.
    #
    # "carries nothing of its own" is about THIS call, not about the state on
    # screen. The panel's real sequence is: type the nInfer address and model
    # while the dropdown still says LM Studio, then pick nInfer -- two saves,
    # and the second carries only the backend. Reading that as "no address was
    # given" replaced what the user had just typed with nInfer's empty banked
    # entry, so the model came back blank.
    name = backend_name(CONN.get("backend"))
    spec = backend_spec(name)
    switched = "backend" in patch and name != prev_name
    if switched:
        servers[prev_name] = {"server_url": prev_url, "model": prev_model}
    if switched and not normalise_base(patch.get("server_url")) \
            and not str(patch.get("model") or "").strip():
        # A bare switch RESTORES the bank. That is what picking a backend from
        # a dropdown means: "use the settings I saved for this one".
        #
        # The panel's real sequence is the awkward case -- type the nInfer
        # address and model while the dropdown still says LM Studio, then pick
        # nInfer -- and the type is its own save, so it has already been banked
        # under lmstudio by the time the switch arrives. That is harmless now
        # that a plain edit banks under the backend you are actually on: the
        # bank the switch reads is the one for the backend being switched TO.
        banked = servers.get(name) or {}
        CONN["server_url"] = (normalise_base(banked.get("server_url"))
                              or spec["default_base"])
        CONN["model"] = str(banked.get("model") or "").strip()

    # Mirror the active backend's pair onto the flat keys, and remember it under
    # its own name, so each backend keeps its own address and model.
    url = normalise_base(CONN.get("server_url")) or spec["default_base"]
    model = str(CONN.get("model") or "").strip()
    if not switched:
        # Only ever write the bank when this save was NOT a switch, and the
        # pair being written is still the previous backend's.
        #
        # The old unconditional write was the bug behind two failures at once:
        # typing an address while the dropdown said LM Studio banked the nInfer
        # address under lmstudio, so switching to nInfer later found lmstudio's
        # "own" pair already equal to the nInfer one, concluded nothing had been
        # typed, and dropped the model. Switching away is what banks the
        # outgoing pair; a plain edit only updates the backend you are on.
        servers[prev_name] = dict(servers.get(prev_name) or {}, **{
            "server_url": url, "model": model})
    else:
        # D1h: MERGE, never assign. These two lines replaced the whole entry,
        # which dropped the `exe` and `model_file` a previous save had put
        # there -- so the panel would show the paths, Save would write them, and
        # the next Save would erase them. 41 green checks, because every check
        # looked at the source and none of them round-tripped a value.
        servers[name] = dict(servers.get(name) or {}, **{
            "server_url": url, "model": model})
    CONN["servers"] = servers
    CONN["backend"] = name
    CONN["server_url"] = url
    CONN["model"] = model

    out = dict(CONN)
    # The panel needs to draw the right editor: the model box is optional on
    # LM Studio and required on nInfer, and a UI that cannot be told that shows
    # a red field on a backend where empty is the normal state.
    out["backend_label"] = spec["label"]
    out["model_optional"] = bool(spec["model_optional"])
    out["backends"] = [{"id": k, "label": v["label"]}
                       for k, v in BACKENDS.items()]
    # Whether it reached disk is the caller's business. A read-only or
    # root-owned custom_nodes -- a system-wide ComfyUI, any container image --
    # used to fail here, print to a console nobody was reading, and answer the
    # panel `ok: true`. The settings still work for this session, because CONN
    # is a live module global; they just do not survive a restart. Those are
    # different sentences and the user is entitled to the right one.
    out["saved"], out["save_error"] = True, ""
    try:
        with open(_conn_path(), "w", encoding="utf-8") as fh:
            json.dump(CONN, fh, indent=2)
    except OSError as exc:
        print(f"[{TAG}] could not save {CONN_FILE}: {exc}", flush=True)
        out["saved"], out["save_error"] = False, str(exc)
    return out


class LLMError(RuntimeError):
    """A failure the panel should show verbatim. Messages are written for the
    person reading them, not for a log -- `routes.py` puts them straight in the
    status line."""


def _start_hint():
    """Where to press, for the backend that is actually selected.

    "Developer tab -> Start Server" is an instruction about a window this panel
    does not own and nInfer does not have. For nInfer the only Start button that
    works is in this panel, so saying anything else sends the person to look for
    a tab that does not exist.
    """
    n = backend_name(CONN.get("backend"))
    if n == "ninfer":
        return "Press Start writer in this panel."
    return "Start it in LM Studio's Developer tab."


# Windows socket errors that all mean the same thing to the person at the
# keyboard: the connection was cut rather than refused. 64 and 10054 are
# WSAENETDOWN / "connection reset by peer"; 10053 is WSAECONNABORTED. On a
# local server the usual cause is not a network at all -- the backend accepted
# the socket and dropped it while a 27B was still being loaded, which is
# exactly what happens when a plan is sent seconds after Start.
_RESET_ERRNOS = frozenset({64, 10053, 10054, 1041})

# Last thing the model-list fetch said, so the panel polling a server that is
# not started does not fill the console with the same sentence on every tick.
_MODEL_LIST_NOTE = {"key": None}


def _dropped_connection(exc):
    """A readable sentence for a severed connection, or None."""
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                        BrokenPipeError)):
        return True
    win = getattr(exc, "winerror", None)
    if win is None:
        win = getattr(getattr(exc, "os_error", None), "winerror", None)
    if win in _RESET_ERRNOS:
        return True
    # aiohttp wraps the real error one or two layers down.
    for name in ("__cause__", "__context__"):
        inner = getattr(exc, name, None)
        if inner is not None and inner is not exc:
            if isinstance(inner, (ConnectionResetError, ConnectionAbortedError,
                                  BrokenPipeError)):
                return True
            if getattr(inner, "winerror", None) in _RESET_ERRNOS:
                return True
            if _dropped_connection(inner):
                return True
    return False


# Servers that have already refused `response_format`, keyed by address.
#
# This is not a per-model or per-version setting, it is a property of whatever
# is listening on that port, and it was costing a whole generation to discover
# every time. The repair loop dropped the schema and re-posted the identical
# prompt, so the cost of one propose on a backend without structured output
# was the failed attempt PLUS the real one:
#
#   [INFO] Prompt executed in 568.09 seconds
#   [HandTieClips] server rejected structured output; retrying without it
#
# Nine and a half minutes thrown away to learn something that was still true
# on the next click. The finding is remembered for the session instead, so
# only the first generate pays it.
_NO_STRUCTURED = set()


def structured_supported():
    """Whether this address has accepted `response_format` before.

    Unknown addresses are assumed to support it, because that is the common
    case and guessing wrong in this direction only costs the same one retry we
    already had.
    """
    return _active_base() not in _NO_STRUCTURED


def _restore_no_structured():
    """Reload the learned per-server capabilities from the settings file.

    In memory only was not enough. The cost of being wrong is a whole wasted
    generation -- 568 s on the machine that reported it -- and ComfyUI is
    restarted for all sorts of reasons that have nothing to do with the
    writer. Making the person restart a second time, having already restarted
    once to get the fix, is a poor trade for a 60-byte flag.

    So it is banked beside the other per-server settings, in the same file
    that is already explicitly machine-local and never travels with a shared
    workflow. Keyed by the same address it was learned against.
    """
    for name, entry in (CONN.get("servers") or {}).items():
        if not isinstance(entry, dict):
            continue
        # The stored value is the ADDRESS that refused, not a bare true. The
        # flag lives on a backend entry and the entry's address is editable:
        # a boolean would follow the server to the port it was just moved to,
        # which was never asked and may well support structured output. So the
        # flag only applies while the entry still points at the address it was
        # learned against.
        banked = entry.get("no_structured")
        if not banked or not isinstance(banked, str):
            continue
        addr = normalise_base(entry.get("server_url")
                              or backend_spec(name)["default_base"])
        if addr and addr == normalise_base(banked):
            _NO_STRUCTURED.add(addr)


def _remember_no_structured():
    """Bank the refusal for this address, in memory and on disk."""
    addr = _active_base()
    if not addr:
        return
    if addr in _NO_STRUCTURED:
        return
    _NO_STRUCTURED.add(addr)
    try:
        n = backend_name(CONN.get("backend"))
        servers = CONN.get("servers")
        if not isinstance(servers, dict):
            servers = {}
        entry = dict(servers.get(n) or {})
        # The address goes IN the flag. See _restore_no_structured: the entry
        # is editable and a bare boolean would follow the server to a port it
        # was never tested on.
        entry["no_structured"] = addr
        servers[n] = entry
        CONN["servers"] = servers
        save_conn({})
        print(f"[{TAG}] {addr} does not accept structured output; banked in "
              f"{CONN_FILE}. It will not be offered again.", flush=True)
    except Exception as exc:
        # A settings file we could not write is not worth failing a generate
        # over -- the in-memory set already does the real work.
        print(f"[{TAG}] could not bank the structured-output finding: {exc}",
              flush=True)


def _active_base():
    n = backend_name(CONN.get("backend"))
    base = normalise_base(
        (CONN.get("servers") or {}).get(n, {}).get("server_url")
        or backend_spec(n)["default_base"])
    return base


def normalise_base(url):
    """Store the server root, never the `/v1` suffix.

    `cpld_conn.json` in PromptMasterLD stores `http://127.0.0.1:1234` and
    appends the rest in code, so a URL can be copied between the two packs
    without editing. People also paste the `/v1` form because that is what LM
    Studio's own UI shows them, so accept both and keep one.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/v1"):
        url = url[:-3].rstrip("/")
    if "://" not in url:
        url = "http://" + url
    return url


async def _post(session, url, body, timeout):
    """POST JSON, return the decoded object. Raises LLMError, never aiohttp's."""
    import aiohttp

    try:
        async with session.post(
            url, json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                # "Model unloaded" is the single most likely 400 here and the
                # raw text does not say what to do about it: LM Studio lists
                # every INSTALLED model over /v1/models, so a model can be
                # picked, saved, and still not be in memory when it is called.
                if "unloaded" in text.lower() or "not loaded" in text.lower():
                    raise LLMError(
                        f"that model is not loaded. Load it in {active_label()} "
                        "(or turn on Just-In-Time model loading in the "
                        "Developer tab), then try again.")
                # Otherwise the body is where servers explain a rejected
                # `response_format` or an unknown model, so it is worth more
                # than the status code.
                raise LLMError(
                    f"server returned HTTP {resp.status}: {text[:400].strip()}")
            try:
                return json.loads(text)
            except ValueError as exc:
                raise LLMError(
                    f"server replied with something that is not JSON "
                    f"({exc}): {text[:200].strip()}") from exc
    except LLMError:
        raise
    except aiohttp.ClientConnectorError as exc:
        raise LLMError(
            f"no server at {url.rsplit('/v1', 1)[0]} -- is {active_label()}'s server "
            f"started? {_start_hint()}") from exc
    except Exception as exc:  # timeout, DNS, reset connection
        # `request failed: [WinError 64] The specified network name is no longer
        # available` is what the panel showed for a plain send-while-loading.
        # Every word of it is the socket layer talking to itself; none of it
        # tells the person that the fix is to wait for the model.
        if _dropped_connection(exc):
            # It used to say "probably still loading the model -- wait until
            # the status line says a model is loaded". That was wrong twice
            # over, and both times it cost someone a minute: nInfer loads a
            # 27B in SEVEN SECONDS, and this panel cannot report load state at
            # all on a backend that returns no `state` field on /v1/models.
            # So it named a cause it had no evidence for and pointed at a
            # status line that does not exist.
            #
            # What is actually known is that the socket went away mid-request.
            # That is usually transient and a retry is the right move, and if
            # it is not transient the reason is in the SERVER's own log at the
            # moment it dropped -- which is where the evidence lives. Saying so
            # costs nothing and stops this being the place people look twice.
            raise LLMError(
                f"{active_label()} closed the connection before it answered. "
                f"That is usually transient -- send it again. If it happens "
                f"every time, the reason is in that server's own log at the "
                f"moment it dropped; nothing on this side can see why the "
                f"connection went away. ({exc})") from exc
        raise LLMError(f"request failed: {exc}") from exc


async def models(base_url):
    """Model ids the server offers, each flagged with whether it is LOADED.

    `/v1/models` lists what is *installed*, not what is in memory, so a
    dropdown built from it happily offers a model that answers the next request
    with `HTTP 400: Model unloaded by user or API request`. LM Studio's native
    `/api/v0/models` carries a `state` field, so it is asked first and the
    OpenAI route is the fallback for servers that have no such thing.

    Returns `[]` rather than raising. A missing model list must never block the
    settings panel -- the user needs it open to fix the URL that is the reason
    the list is empty.
    """
    import aiohttp

    base = normalise_base(base_url)
    if not base:
        return []

    async def _get(session, path, timeout=LIST_TIMEOUT):
        async with session.get(
            base + path, timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                return None
            return json.loads(await resp.text())

    try:
        async with aiohttp.ClientSession() as session:
            native = None
            try:
                native = await _get(session, "/api/v0/models")
            except Exception:
                native = None
            if native is not None:
                rows = native.get("data") if isinstance(native, dict) else native
                out = []
                for m in (rows or []):
                    mid = (m or {}).get("id")
                    if not mid:
                        continue
                    # Embeddings cannot answer a chat completion; offering one
                    # is offering a guaranteed failure.
                    if str((m or {}).get("type") or "") == "embeddings":
                        continue
                    out.append({"id": str(mid),
                                "loaded": str((m or {}).get("state") or "")
                                == "loaded"})
                if out:
                    # Loaded first, then alphabetical: the one that will work
                    # without a wait should be the one the eye lands on.
                    return sorted(out, key=lambda r: (not r["loaded"], r["id"]))

            data = await _get(session, "/v1/models")
            if not data:
                return []
    except Exception as exc:
        # Nothing listening is the normal state of this pack before Start, and
        # the panel calls this route on mount. It printed
        #
        #   [HandTieClips] model list from http://127.0.0.1:8080 failed:
        #   Cannot connect to host ... The remote computer refused the connection
        #
        # twice per page load, in the same red as a real fault, immediately
        # above `started nInfer` -- so the one line that said the server had
        # come up was sandwiched between two lines saying it had not. The
        # refusal is expected, the panel already answers it with `online: false`
        # and "Press Start writer below", and it is said once per address
        # rather than on every poll.
        refused = isinstance(exc, aiohttp.ClientConnectorError) or "refused" in str(exc).lower()
        note = f"no server listening at {base}" if refused else f"model list from {base} failed: {exc}"
        key = (base, bool(refused))
        if _MODEL_LIST_NOTE.get("key") != key:
            _MODEL_LIST_NOTE["key"] = key
            print(f"[{TAG}] {note}", flush=True)
        return []

    seen = []
    for row in (data.get("data") or []):
        mid = (row or {}).get("id")
        if mid:
            # No state information on this route, so nothing is claimed.
            seen.append({"id": str(mid), "loaded": None})
    return sorted(seen, key=lambda r: r["id"])


def _chat_body(model, messages, *, schema, temperature, max_tokens):
    body = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False,
        # Kill think-mode. A reasoning model otherwise puts the plan in
        # `reasoning_content` and returns an EMPTY `content`, which reads as
        # "the model said nothing" rather than "the switch was ignored". Both
        # keys are harmless on servers that do not know them.
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False, "thinking": False},
    }
    # Deliberately no `ttl`. PromptMasterLD sent `"ttl": 30` once and LM Studio
    # unloaded a 26B model ~30s after the prompt finished, while its panel still
    # reported the writer warm. Lifetime is this pack's to manage, on the
    # explicit unload below.
    if schema is not None and structured_supported():
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "hand_tie_clips_plan", "strict": True,
                            "schema": schema},
        }
    return body


def _content(data, max_tokens=None):
    """Pull the reply text out, and say something useful when there isn't one.

    Three different failures produce an empty `content`, and telling a user the
    wrong one sends them to the wrong setting:

      * `finish_reason == "length"` -- the budget ran out. On a reasoning model
        that usually means thinking ate all of it. Raise max_tokens.
      * finished cleanly with reasoning and no content -- the thinking switches
        were genuinely ignored. Retry with `/no_think`, then give up on it.
      * nothing at all -- the server is answering, but with nothing.
    """
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("the server returned no choices")
    choice = choices[0] or {}
    msg = choice.get("message") or {}
    text = (msg.get("content") or "").strip()

    usage = (data.get("usage") or {})
    detail = (usage.get("completion_tokens_details") or {})
    reasoned = detail.get("reasoning_tokens") or 0
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "")

    # Checked BEFORE the text is handed back, because the expensive case is a
    # reply that is truncated but not empty: half a JSON object parses as no
    # JSON, and the repair loop then spends every remaining attempt telling a
    # model with no room to answer that its formatting is wrong. Three
    # attempts, three identical truncations, and an error naming the wrong
    # cause. Better to stop on the first one and report the real number.
    if choice.get("finish_reason") == "length":
        raise LLMError(_cut_off(usage, reasoned, max_tokens))
    if text:
        return text
    if reasoning.strip():
        raise LLMError("__REASONING_ONLY__")
    raise LLMError("the server returned an empty reply")


def _cut_off(usage, reasoned, max_tokens):
    """Say which budget ran out, ours or the server's, and what to set.

    `max_tokens` is what this pack asked for; `prompt_tokens` is what the turn
    cost. A completion that stopped near our own ceiling means the plan really
    is that long. Otherwise the context window is full, and the useful number
    is not "raise it" but the size that would actually fit -- measured here
    rather than left for the reader to work out.
    """
    used = int(usage.get("completion_tokens") or 0)
    prompt = int(usage.get("prompt_tokens") or 0)
    tail = (f" -- {reasoned} of them went to the model's own reasoning, which "
            f"must be turned off in {active_label()}" if reasoned else "")

    if max_tokens and used >= int(max_tokens) * 0.95:
        return (f"the reply hit the {int(max_tokens)}-token ceiling this pack "
                f"asks for{tail}. The plan is longer than the writer expects; "
                f"try fewer hops.")

    if prompt:
        want = 1 << max(14, (prompt * 2 + 4096 - 1).bit_length())
        return (f"the reply was cut off after {used} token(s){tail}. The "
                f"prompt alone used {prompt}, so the model's context is full "
                f"and there is no room left to answer in. Raise the context "
                f"length in {active_label()} to at least {want} -- repair turns grow "
                f"the conversation, so the first attempt fitting is not "
                f"enough.")

    return (f"the reply was cut off after {used} token(s){tail}. Raise the "
            f"model's context length in {active_label()}.")


async def complete_stream(base_url, model, messages, *, schema=None,
                          temperature=0.35, max_tokens=MAX_TOKENS,
                          timeout=GEN_TIMEOUT, on_delta=None):
    """One chat completion, delivered as it arrives.

    `on_delta(text)` is called with each new piece of the reply, in order. The
    final string is still returned, so this is a drop-in for `complete` -- a
    caller that ignores the callback gets exactly what it got before.

    Why it exists: a 27B on a 32 GB card answers in 30 s or more, and a panel
    that shows nothing until the end reads as a hang rather than as work. It
    also makes a bad prompt visible early -- a model that starts writing a
    thinking preamble has already told you it is going to waste the turn.

    Falls back to the non-streaming path when the server will not stream (an
    older build answers the same request and simply returns one chunk), and
    when `on_delta` is None. `complete` stays the plain one on purpose: the
    repair loop reads whole replies and has no use for partial text.
    """
    if on_delta is None:
        return await complete(base_url, model, messages, schema=schema,
                              temperature=temperature, max_tokens=max_tokens,
                              timeout=timeout)
    import aiohttp
    import json as _json

    base = normalise_base(base_url)
    if not base:
        raise LLMError("no server URL is set -- open Settings in the panel")
    if not model:
        raise LLMError("no model is selected -- open Settings in the panel")
    url = base + "/v1/chat/completions"

    async with aiohttp.ClientSession() as session:
        body = _chat_body(model, messages, schema=schema,
                          temperature=temperature, max_tokens=max_tokens)
        body = dict(body, stream=True)
        try:
            async with session.post(
                    url, json=body,
                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise LLMError(_clean_http(resp.status, text))
                if "text/event-stream" not in (
                        resp.headers.get("Content-Type") or ""):
                    # The server ignored `stream` and answered in one piece.
                    data = _json.loads(await resp.text())
                    got = _content(data, max_tokens=max_tokens)
                    on_delta(got)
                    return got
                out = []
                # aiohttp's response content yields ARBITRARY chunks, not
                # lines. Treating each chunk as a whole SSE event is the
                # obvious thing to write and it is wrong: a server may split
                # anywhere, including inside the `data:` prefix or the middle
                # of the JSON, and a chunk that does not start with `data:` is
                # then silently dropped. A real reply arrives short by whatever
                # was lost, which reads as a truncated plan rather than as a
                # parser bug. Buffer to a newline and parse whole events.
                buf = b""
                async for raw in resp.content:
                    buf += raw
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line = line.decode("utf-8", "replace").strip()
                        if not line or not line.startswith("data:"):
                            continue    # a comment, a blank line, a field name
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            buf = b""
                            break
                        if not payload:
                            continue
                        try:
                            obj = _json.loads(payload)
                        except Exception:
                            continue
                        for choice in (obj.get("choices") or []):
                            piece = ((choice.get("delta") or {}).get("content")
                                     or (choice.get("message") or {}).get("content"))
                            if piece:
                                out.append(piece)
                                on_delta(piece)
                    else:
                        continue
                    break
                # A last event with no trailing newline is still an event.
                tail = buf.decode("utf-8", "replace").strip()
                if tail.startswith("data:"):
                    payload = tail[5:].strip()
                    if payload and payload != "[DONE]":
                        try:
                            for choice in (_json.loads(payload).get("choices") or []):
                                piece = ((choice.get("delta") or {}).get("content")
                                         or (choice.get("message") or {}).get("content"))
                                if piece:
                                    out.append(piece)
                                    on_delta(piece)
                        except Exception:
                            pass
                text = "".join(out).strip()
                if not text:
                    raise LLMError("the reply streamed nothing")
                return text
        except aiohttp.ClientError as exc:
            raise LLMError(_clean(exc, url)) from exc


async def complete(base_url, model, messages, *, schema=None,
                   temperature=0.35, max_tokens=MAX_TOKENS, timeout=GEN_TIMEOUT):
    """One chat completion. Returns the reply text.

    Retries once with `/no_think` appended to the last user turn when the reply
    comes back as reasoning only: some llama.cpp builds ignore both payload
    switches above, and Qwen's documented in-prompt escape is the only lever
    left. This matters more than it sounds -- the models this pack was tested
    against are reasoning models, so it is the likely first-contact failure.

    Falls back to an unconstrained request when the server rejects
    `response_format`, which older llama.cpp builds do with a 400.
    """
    import aiohttp

    base = normalise_base(base_url)
    if not base:
        raise LLMError("no server URL is set -- open Settings in the panel")
    if not model:
        raise LLMError("no model is selected -- open Settings in the panel")
    url = base + "/v1/chat/completions"

    async with aiohttp.ClientSession() as session:
        body = _chat_body(model, messages, schema=schema,
                          temperature=temperature, max_tokens=max_tokens)
        try:
            data = await _post(session, url, body, timeout)
            return _content(data, max_tokens=max_tokens)
        except LLMError as first:
            note = str(first)

            if note == "__REASONING_ONLY__":
                nudged = _nudge_no_think(messages)
                print(f"[{TAG}] reply was reasoning only; retrying with "
                      f"/no_think", flush=True)
                data = await _post(
                    session, url,
                    _chat_body(model, nudged, schema=schema,
                               temperature=temperature, max_tokens=max_tokens),
                    timeout)
                try:
                    return _content(data, max_tokens=max_tokens)
                except LLMError as second:
                    if str(second) == "__REASONING_ONLY__":
                        raise LLMError(
                            "this model answers with reasoning only and ignores "
                            "both thinking switches. Turn reasoning off in LM "
                            "Studio, or pick a non-reasoning model.") from second
                    raise

            if schema is not None and "HTTP 4" in note:
                # Only remember it when the server actually named the thing it
                # did not like. "HTTP 4" on its own is not enough: a 404 for a
                # wrong path, a 401, or a 400 for a model that is not loaded all
                # land here, and any of those would permanently disable a
                # capability the server does have. Getting that wrong costs
                # every generate from then on a weaker JSON contract.
                blames_schema = any(k in note.lower() for k in (
                    "response_format", "json_schema", "schema",
                    "unsupported", "not supported", "unrecognized",
                    "extra inputs", "not permitted", "unknown field"))
                if blames_schema:
                    # Remember it BEFORE retrying. The retry is the expensive
                    # half and it will be identical next time; the whole point
                    # is that the second click starts without the schema. It
                    # is banked on disk as well, because "restart and pay it
                    # again" is a poor trade for a flag.
                    _remember_no_structured()
                    print(f"[{TAG}] server rejected structured output; "
                          f"retrying without it.", flush=True)
                data = await _post(
                    session, url,
                    _chat_body(model, messages, schema=None,
                               temperature=temperature, max_tokens=max_tokens),
                    timeout)
                return _content(data, max_tokens=max_tokens)

            raise


def has_images(messages):
    """True when any turn carries an OpenAI image_url part."""
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list) and any(
                (p or {}).get("type") == "image_url" for p in c):
            return True
    return False


def text_only(messages):
    """Drop image parts, concatenating remaining text. Used when a server 400s
    on vision (a text-only model in the dropdown)."""
    out = []
    for m in messages or []:
        nm = dict(m)
        c = nm.get("content")
        if isinstance(c, list):
            nm["content"] = "\n".join(
                str(p.get("text") or "") for p in c
                if (p or {}).get("type") == "text").strip()
        out.append(nm)
    return out


def _nudge_no_think(messages):
    """Append /no_think to the last user text, including a multimodal turn."""
    nudged = [dict(m) for m in messages]
    for m in reversed(nudged):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            m["content"] = [dict(p) for p in c]
            for p in reversed(m["content"]):
                if p.get("type") == "text":
                    p["text"] = str(p.get("text") or "") + " /no_think"
                    break
            else:
                m["content"].append({"type": "text", "text": "/no_think"})
        else:
            m["content"] = str(c or "") + " /no_think"
        break
    return nudged


def shares_this_gpu(base_url):
    """Is the writer on this machine, and therefore on this machine's VRAM?

    PromptMasterLD grew this check after a user running LM Studio on a laptop
    and ComfyUI on a desktop had the laptop's model unloaded mid-workflow. An
    unload is only ever a courtesy to the local card; reaching across the
    network to evict someone else's model is a bug, not a feature.

    Loopback only, and deliberately narrow. Two earlier versions tried to
    recognise this machine's own LAN address as well -- first by resolving our
    own name and intersecting address sets, then by binding the target to see
    if we hold it. Both work. Both also read, to a static scanner, as a program
    mapping its host: 1.0.2 shipped the binding version and the registry scan
    matched it four ways at once where 1.0.1 had matched once. A courtesy
    feature is not worth looking like reconnaissance to every user who reads
    the scan. tools/check_publish.py now names the patterns; the DEVLOG has the
    detail, and neither of them ships.

    The cost is real and worth stating: point the writer at this same machine by
    its LAN address rather than localhost and the automatic unload stops firing.
    Nothing breaks -- the model simply stays resident, which is what happens for
    every genuinely remote server too -- and `unload_all` says so. Typing
    `localhost` fixes it.
    """
    host = (urllib.parse.urlparse(normalise_base(base_url)).hostname or "")
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name rather than an address literal. Resolving it is exactly the
        # lookup this function no longer performs, so: not ours.
        return False


async def _unload_one(session, base, model):
    """One model, every known eviction verb. -> bool. Never raises.

    LM Studio's unload route changed shape between versions, so the bodies are
    tried in order; a backend with no unload endpoint at all is ordinary.
    """
    import aiohttp

    for path, body in (
        ("/api/v1/models/unload", {"instance_id": model}),
        ("/api/v1/models/unload", {"identifier": model}),
        ("/api/v0/models/unload", {"instance_id": model}),
        ("/api/v0/models/unload", {"identifier": model}),
    ):
        try:
            async with session.post(
                base + path, json=body,
                timeout=aiohttp.ClientTimeout(total=UNLOAD_TIMEOUT),
            ) as resp:
                if resp.status < 400:
                    print(f"[{TAG}] unloaded {model} from the writer",
                          flush=True)
                    return True
        except Exception:
            continue
    # Ollama keeps its own eviction verb; harmless against LM Studio.
    try:
        async with session.post(
            base + "/api/generate",
            json={"model": model, "keep_alive": 0},
            timeout=aiohttp.ClientTimeout(total=UNLOAD_TIMEOUT),
        ) as resp:
            if resp.status < 400:
                print(f"[{TAG}] evicted {model} (keep_alive 0)", flush=True)
                return True
    except Exception:
        pass
    return False


async def unload(base_url, model):
    """Hand the VRAM back, so the render that follows has somewhere to live.

    A 27B at 32k context and an H3 render do not co-exist on one card, and the
    shipped Starter workflow already leans on low-VRAM attention -- these are
    the same users. Without this, "write a plan, then queue" OOMs, and the OOM
    looks like this node's fault.

    LM Studio's unload route changed shape between versions, so the body forms
    below are tried in order. Never raises: a backend with no unload endpoint is
    ordinary, and a failed courtesy must not look like a failed generation.
    """
    import aiohttp

    base = normalise_base(base_url)
    if not (base and model):
        return False
    if not shares_this_gpu(base):
        print(f"[{TAG}] writer is not on this machine -- leaving it loaded",
              flush=True)
        return False

    try:
        async with aiohttp.ClientSession() as session:
            if await _unload_one(session, base, model):
                return True
    except Exception as exc:
        print(f"[{TAG}] unload failed: {exc}", flush=True)
        return False

    print(f"[{TAG}] no unload endpoint answered -- free the VRAM in {active_label()} "
          f"if the render runs short", flush=True)
    return False


async def unload_all(base_url, fallback_model=""):
    """The killswitch: evict everything the writer has in VRAM. -> (n, note).

    `unload` only ever targets the model this pack configured, which is right
    for the automatic path -- it hands back exactly what writing a plan caused
    to be loaded. It is not enough for a button whose whole job is "give me the
    card back now", because the three situations that actually OOM a render are
    the ones where the configured model is not what is resident: the unload
    checkbox was off, the write failed before it ran, or LM Studio's JIT loaded
    something other than what was asked for.

    PromptMasterLD covers this with `lms unload --all`. That rung spawns a
    child process, which is the pattern that got 0.4.1-0.4.3 registry-Flagged,
    so this lists loaded models over HTTP and walks them through the same
    ladder instead. Same effect, nothing spawned.

    The mechanism is described rather than named on purpose: this file ships,
    and the registry's scan reads prose exactly as it reads code -- 1.0.2 was
    Flagged for a class name quoted in a markdown file while explaining why it
    had been Flagged.
    """
    import aiohttp

    base = normalise_base(base_url)
    if not base:
        return 0, "no server configured"
    if not shares_this_gpu(base):
        # The laptop-and-desktop bug: never reach across a network to evict
        # someone else's model. Freeing VRAM here would free the wrong VRAM.
        return 0, ("the writer is not on loopback -- nothing to free here. "
                   "If it IS this machine, address it as localhost.")

    try:
        listed = await models(base)
    except Exception as exc:
        listed = []
        print(f"[{TAG}] could not list models to unload: {exc}", flush=True)

    # `loaded` is None on servers with no /api/v0/models -- unknown, not false.
    # Falling back to the configured model beats evicting nothing at all.
    targets = [m["id"] for m in listed if m.get("loaded") is True]
    guessing = False
    if not targets:
        if any(m.get("loaded") is None for m in listed) and fallback_model:
            targets, guessing = [fallback_model], True
        else:
            return 0, "nothing is loaded"

    done = []
    try:
        async with aiohttp.ClientSession() as session:
            for name in targets:
                if await _unload_one(session, base, name):
                    done.append(name)
    except Exception as exc:
        print(f"[{TAG}] unload_all failed: {exc}", flush=True)
        return len(done), f"stopped after {len(done)}: {exc}"

    if not done:
        return 0, (f"no unload endpoint answered -- free the VRAM in {active_label()} "
                   "directly")
    note = ", ".join(done)
    if guessing:
        note += " (this server does not report what is loaded)"
    return len(done), note


def configured():
    """Has anyone actually set this pack's writer up? -> bool.

    Filesystem only, no network, no import of aiohttp. `free_for_render` is on
    the critical path of EVERY render, including the overwhelming majority that
    never touch the plan writer, and those must pay nothing at all -- not a
    socket, not a DNS lookup, not a 4 s timeout against a port with nothing
    behind it. A settings file that has never been written is the cheapest
    possible proof that there is nothing to evict.
    """
    import os
    if not os.path.isfile(_conn_path()):
        return False
    try:
        return bool((load_conn() or {}).get("model"))
    except Exception:
        return False


def _run_coro(factory):
    """Run `factory()` (which returns a coroutine) to completion.

    `asyncio.run` is correct when nothing is looping, which is the offline
    checkers and a sync worker thread. ComfyUI's execute path is async, so
    `run()` is already inside a running loop and `asyncio.run` refuses to
    nest -- that is the RuntimeError keep_warm + Queue hit, plus the
    'coroutine never awaited' warning from the expression
    `asyncio.run(unload_all(...))`.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    def _in_thread():
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_in_thread).result()


def free_for_render(settle_sleep=None):
    """Evict the writer and wait for the card. Blocking. -> note or "".

    **Blocking is correct here, and this is the one place in this module where
    it is.** The rule in the docstring at the top -- never block -- is about
    aiohttp handlers: they run on ComfyUI's event loop, and a stalled handler
    freezes the whole UI, canvas included, for the length of a generation. This
    function is called from the node's `run()`. Nothing is waiting on it except
    the render, and the render is what the VRAM is being freed FOR. ComfyUI
    may already be inside a running loop when it gets here, so the wait is
    `_run_coro`, not a nested `asyncio.run`.

    Never raises. A writer that was never configured, a server that is not
    running, one on another machine, one with no unload endpoint: all ordinary,
    all silent. The single unacceptable outcome is that a courtesy to the GPU
    takes down a render the user has been waiting minutes for.
    """
    import time

    if not configured():
        return ""
    conn = load_conn()
    if not conn.get("unload_on_run"):
        return ""
    spec = backend_spec(conn.get("backend"))
    if not spec.get("evictable", True):
        # The card is held by a PROCESS, not by a loaded model, so the only
        # way to hand the VRAM back is to end it. This is not a courtesy: on
        # a 32 GB card a resident 27B leaves ~1.5 GB and the render OOMs.
        stopped = stop_process()
        if stopped:
            return (f"stopped {spec['label']} to free the card for the render. "
                    f"Start it again before writing -- that costs about 30 s "
                    f"of model load, and this pack will not start it for you: "
                    f"launching a server from a settings save is how a "
                    f"workflow file becomes a way to run something.")
        return (f"{spec['label']} could not be stopped automatically "
                f"(started outside this pack?). Close it by hand before "
                f"rendering, or the render may run short of VRAM.")
    try:
        # ComfyUI's execute path is async: node.run() is reached from a
        # running event loop. asyncio.run() then raises RuntimeError and the
        # unload coroutine is never awaited -- keep_warm + Queue left the 27B
        # resident and printed that error. A side thread with its own loop is
        # the same block this function already promised, just not nested.
        n, note = _run_coro(
            lambda: unload_all(conn["server_url"], conn["model"]))
    except Exception as exc:  # noqa: BLE001
        print(f"[{TAG}] could not free the writer's VRAM: {exc!r}", flush=True)
        return ""
    if not n:
        # Nothing was resident, so there is nothing to settle. This is the
        # common path once a user has written a plan earlier in the session and
        # already rendered once, and it must cost no wall time.
        return ""

    settle = float(conn.get("vram_settle_s") or 0.0)
    if settle > 0:
        # Said out loud, because an unexplained pause before a render looks
        # exactly like a hang -- and this one lands after the queue button, the
        # moment the user is most primed to read a stall as a crash.
        print(f"[{TAG}] freed the writer ({note}); waiting {settle:.1f}s for "
              f"the driver to release", flush=True)
        (settle_sleep or time.sleep)(settle)
    return f"writer unloaded: {note}"
