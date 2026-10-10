"""complete_stream's branches, with no server and no GPU.

The live-server run proved the happy path (13 deltas, joined == final). What
it cannot reach is the three ways a stream goes wrong, and those are the ones
worth a checker: a server that ignores `stream` and answers in one piece, a
stream that carries nothing at all, and the no-callback path the repair loop
uses. All three are ordinary states, not exotic ones.
"""
import asyncio
import importlib.util
import json
import sys
import types

HERE = r"D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips"
sys.path.insert(0, r"D:\ComfyUI")
spec = importlib.util.spec_from_file_location(
    "htcpack", HERE + r"\__init__.py", submodule_search_locations=[HERE])
_m = importlib.util.module_from_spec(spec)
sys.modules["htcpack"] = _m
spec.loader.exec_module(_m)
LM = importlib.util.module_from_spec(  # noqa: E402
    importlib.util.spec_from_file_location("htcpack.llm", HERE + r"\llm.py"))
sys.modules["htcpack.llm"] = LM
importlib.util.spec_from_file_location(
    "htcpack.llm", HERE + r"\llm.py").loader.exec_module(LM)

FAILED = []


def ck(name, cond, detail=""):
    print("  %-4s %-46s %s" % ("ok" if cond else "FAIL", name, detail))
    if not cond:
        FAILED.append(name)


class _Resp:
    def __init__(self, status=200, body=b"", ctype="application/json", chunks=None):
        self.status = status
        self.headers = {"Content-Type": ctype}
        self._body = body
        self.content = self._iter(chunks or [])

    async def _iter(self, chunks):
        for c in chunks:
            yield c

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def install(resp):
    ai = types.ModuleType("aiohttp")

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            return resp

    ai.ClientSession = _Session
    ai.ClientTimeout = lambda **k: None
    ai.ClientError = type("ClientError", (Exception,), {})
    sys.modules["aiohttp"] = ai


MSG = [{"role": "user", "content": "x"}]
URL = "http://127.0.0.1:1"

sse = b"".join(
    b"data: " + json.dumps({"choices": [{"delta": {"content": c}}]}).encode() + b"\n\n"
    for c in ("Hello", " there")) + b"data: [DONE]\n\n"

print("\nllm.complete_stream -- the branches a live server cannot reach")

got = []
install(_Resp(ctype="text/event-stream", chunks=[sse]))
out = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got.append))
ck("a real SSE stream delivers each piece", len(got) == 2, str(got))
ck("and returns the whole reply", out == "Hello there", repr(out))
ck("joining the pieces gives the same text", "".join(got).strip() == out.strip())

one = json.dumps({"choices": [{"message": {"content": "all at once"}}]}).encode()
got2 = []
install(_Resp(ctype="application/json", body=one))
out2 = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got2.append))
ck("a server that ignores stream still works", out2 == "all at once", repr(out2))
ck("and the callback gets it once", len(got2) == 1, str(got2))

install(_Resp(ctype="text/event-stream", chunks=[b"data: [DONE]\n\n"]))
try:
    asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=lambda t: None))
    ck("an empty stream raises rather than returning ''", False, "no error")
except LM.LLMError as exc:
    ck("an empty stream raises rather than returning ''", True, str(exc)[:44])

install(_Resp(ctype="application/json", body=one))
out4 = asyncio.run(LM.complete_stream(URL, "m", MSG))
ck("no callback takes the plain path", out4 == "all at once", repr(out4))

# Malformed chunks must be skipped, not fatal: a keep-alive or a comment line
# in the middle of a stream is ordinary, and killing the reply over one would
# turn a cosmetic thing into a failed plan.
mixed = (b": keep-alive\n\n"
         + b"data: not json at all\n\n"
         + sse)
got5 = []
install(_Resp(ctype="text/event-stream", chunks=[mixed]))
out5 = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got5.append))
ck("a comment line and a malformed chunk are skipped",
   out5 == "Hello there", repr(out5))

# THE case a real server produces and a naive parser loses. aiohttp yields
# arbitrary chunks: one event split across three reads, and the `data:` prefix
# itself split in half. Anything that treats a chunk as a whole event drops
# the fragments and returns a short reply -- which looks like a truncated plan
# rather than like a parser bug, so it can ship unnoticed.
split = b"".join((
    b'data: {"choices":[{"del',                 # cut mid-key
    b'ta":{"content":"Hello"}}]}\n',             # rest of the event
    b"\n",
    b'data: {"choices":[{"delta":{"content":" ther',   # cut mid-value
    b'e"}}]}\n\n',
    b"data: [DONE]\n\n"))
got6 = []
install(_Resp(ctype="text/event-stream", chunks=[split]))
out6 = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got6.append))
ck("an event split across chunks is reassembled", out6 == "Hello there",
   repr(out6))
ck("and every piece reached the callback", len(got6) == 2, str(got6))

# The prefix itself split in half -- the worst case, and one no amount of
# "skip anything not starting with data:" survives.
tiny = b"".join((b"dat", b"a: {\"choices\":[{\"delta\":{\"content\":",
                b"\"yes\"}}]}\n\ndata: [DONE]\n\n"))
got7 = []
install(_Resp(ctype="text/event-stream", chunks=[tiny]))
out7 = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got7.append))
ck("a 'data:' prefix split mid-word is reassembled", out7 == "yes", repr(out7))

# One byte at a time is the worst a server can do, and costs nothing to test.
byt = b"".join(bytes([c]) for c in sse)
got8 = []
install(_Resp(ctype="text/event-stream", chunks=[byt]))
out8 = asyncio.run(LM.complete_stream(URL, "m", MSG, on_delta=got8.append))
ck("a stream arriving one byte at a time still parses",
   out8 == "Hello there", repr(out8))

print()
if FAILED:
    print("STREAM: %d FAILURE(S)\n  %s" % (len(FAILED), "\n  ".join(FAILED)))
    raise SystemExit(1)
print("STREAM: all passed")