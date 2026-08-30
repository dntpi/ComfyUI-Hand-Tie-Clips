"""Talk to an OpenAI-compatible chat server -- LM Studio, llama-server, Ollama.

Nothing in this module runs during a graph execution. It is reached only from
the panel's Write-plan button, through `routes.py`, and the plan it produces is
written into the `shot_plan` / `ref_plan` widgets like any other paste. A queued
graph therefore stays deterministic and works with the network unplugged, which
is the property that makes the feature safe to ship at all.

Two constraints shape everything here.

**No subprocess.** The sibling pack PromptMasterLD manages a model process --
`llama_exe` in its config, `lms unload --all` shelled out at `backend.py:353` --
and can afford to because it has no `pyproject.toml` and is never scanned by the
ComfyUI registry. This pack is scanned, and 0.4.1-0.4.3 were Flagged under
`python_command_injection_risk` until `store.py` was migrated off
`subprocess.Popen`. Every rung of the unload ladder below is HTTP for that
reason; the one subprocess rung PromptMasterLD has is deliberately absent.

**No blocking I/O.** PromptMasterLD calls `urllib.request` synchronously, which
is fine from its worker thread. These functions are awaited directly inside
aiohttp handlers, so a blocking read would freeze ComfyUI's event loop -- and
the whole UI with it -- for the length of a generation. On a 27B that is tens of
seconds of a frozen canvas, indistinguishable from a hang.
"""

import json
import socket
import urllib.parse

TAG = "HandTieClips"

DEFAULT_BASE = "http://127.0.0.1:1234"

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
    "server_url": DEFAULT_BASE,
    "model": "",
    "temperature": 0.35,
    "unload_after": True,
}
_CONN_KEYS = tuple(CONN)


def _conn_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONN_FILE)


def load_conn():
    """Read the saved settings. A missing or broken file is not an error --
    the defaults are a working LM Studio install."""
    import os
    path = _conn_path()
    if not os.path.isfile(path):
        return dict(CONN)
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        for k in _CONN_KEYS:
            if k in saved:
                CONN[k] = saved[k]
    except Exception as exc:
        print(f"[{TAG}] {CONN_FILE} unreadable, using defaults: {exc}",
              flush=True)
    CONN["server_url"] = normalise_base(CONN.get("server_url")) or DEFAULT_BASE
    return dict(CONN)


def save_conn(patch):
    """Merge and persist. Blank strings are SKIPPED, not stored.

    PromptMasterLD does the same, and for a reason worth inheriting: a settings
    panel that posts before its model dropdown has populated would otherwise
    write an empty model over a working one, and the next generate fails with
    "no model is selected" on a box the user never touched.
    """
    for k in _CONN_KEYS:
        if k not in patch:
            continue
        v = patch[k]
        if isinstance(v, str) and not v.strip():
            continue
        if k == "server_url":
            v = normalise_base(v)
            if not v:
                continue
        elif k == "temperature":
            try:
                v = max(0.0, min(2.0, float(v)))
            except (TypeError, ValueError):
                continue
        elif k == "unload_after":
            v = bool(v)
        CONN[k] = v
    try:
        with open(_conn_path(), "w", encoding="utf-8") as fh:
            json.dump(CONN, fh, indent=2)
    except OSError as exc:
        print(f"[{TAG}] could not save {CONN_FILE}: {exc}", flush=True)
    return dict(CONN)


class LLMError(RuntimeError):
    """A failure the panel should show verbatim. Messages are written for the
    person reading them, not for a log -- `routes.py` puts them straight in the
    status line."""


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
                        "that model is not loaded. Load it in LM Studio "
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
            f"no server at {url.rsplit('/v1', 1)[0]} -- is LM Studio's server "
            f"started? (Developer tab -> Start Server)") from exc
    except Exception as exc:  # timeout, DNS, reset connection
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
        print(f"[{TAG}] model list from {base} failed: {exc}", flush=True)
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
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "hand_tie_clips_plan", "strict": True,
                            "schema": schema},
        }
    return body


def _content(data):
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
    if text:
        return text

    usage = (data.get("usage") or {})
    detail = (usage.get("completion_tokens_details") or {})
    reasoned = detail.get("reasoning_tokens") or 0
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "")

    if choice.get("finish_reason") == "length":
        raise LLMError(
            f"the reply was cut off at {usage.get('completion_tokens', '?')} "
            f"tokens before any JSON was written"
            + (f" -- {reasoned} of them went to the model's own reasoning"
               if reasoned else "")
            + ". Raise the model's context length in LM Studio, or turn its "
              "reasoning off.")
    if reasoning.strip():
        raise LLMError("__REASONING_ONLY__")
    raise LLMError("the server returned an empty reply")


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
            return _content(data)
        except LLMError as first:
            note = str(first)

            if note == "__REASONING_ONLY__":
                nudged = [dict(m) for m in messages]
                for m in reversed(nudged):
                    if m.get("role") == "user":
                        m["content"] = str(m.get("content") or "") + " /no_think"
                        break
                print(f"[{TAG}] reply was reasoning only; retrying with "
                      f"/no_think", flush=True)
                data = await _post(
                    session, url,
                    _chat_body(model, nudged, schema=schema,
                               temperature=temperature, max_tokens=max_tokens),
                    timeout)
                try:
                    return _content(data)
                except LLMError as second:
                    if str(second) == "__REASONING_ONLY__":
                        raise LLMError(
                            "this model answers with reasoning only and ignores "
                            "both thinking switches. Turn reasoning off in LM "
                            "Studio, or pick a non-reasoning model.") from second
                    raise

            if schema is not None and "HTTP 4" in note:
                print(f"[{TAG}] server rejected structured output; retrying "
                      f"without it", flush=True)
                data = await _post(
                    session, url,
                    _chat_body(model, messages, schema=None,
                               temperature=temperature, max_tokens=max_tokens),
                    timeout)
                return _content(data)

            raise


def shares_this_gpu(base_url):
    """Is the writer on this machine, and therefore on this machine's VRAM?

    PromptMasterLD grew this check after a user running LM Studio on a laptop
    and ComfyUI on a desktop had the laptop's model unloaded mid-workflow. An
    unload is only ever a courtesy to the local card; reaching across the
    network to evict someone else's model is a bug, not a feature.
    """
    host = (urllib.parse.urlparse(normalise_base(base_url)).hostname or "")
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    try:
        here = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)}
        there = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(here & there)


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

    print(f"[{TAG}] no unload endpoint answered -- free the VRAM in LM Studio "
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

    PromptMasterLD covers this with `lms unload --all`. That rung is a
    subprocess, which is what got 0.4.1-0.4.3 registry-Flagged under
    `python_command_injection_risk`, so this lists loaded models over HTTP and
    walks them through the same ladder instead. Same effect, nothing spawned.
    """
    import aiohttp

    base = normalise_base(base_url)
    if not base:
        return 0, "no server configured"
    if not shares_this_gpu(base):
        # The laptop-and-desktop bug: never reach across a network to evict
        # someone else's model. Freeing VRAM here would free the wrong VRAM.
        return 0, "the writer is on another machine -- nothing to free here"

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
        return 0, ("no unload endpoint answered -- free the VRAM in LM Studio "
                   "directly")
    note = ", ".join(done)
    if guessing:
        note += " (this server does not report what is loaded)"
    return len(done), note
