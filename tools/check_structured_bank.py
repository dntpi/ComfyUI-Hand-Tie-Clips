"""D1m: a refused response_format is banked on disk, against the right address.

D1k remembered it in memory, which means "restart ComfyUI and pay the 568 s
again". That is a poor trade for a flag -- ComfyUI is restarted for all sorts
of reasons that have nothing to do with the writer, and the person has already
restarted once to get the fix.

So it is written to htc_llm.json, beside the other per-server settings, in the
file that is already machine-local and never travels with a shared workflow.

The interesting half is WHERE it is written. The obvious thing -- a boolean on
the backend entry -- is wrong: the entry's address is editable, so the flag
follows the server to a port it was never asked about. The stored value is the
ADDRESS that refused, and it only applies while the entry still points there.

Run: python tools/check_structured_bank.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import types

ROOT = r"D:\ComfyUI\custom_nodes\ComfyUI-Hand-Tie-Clips"
sys.path.insert(0, os.path.dirname(ROOT))
pkg = types.ModuleType("htc")
pkg.__path__ = [ROOT]
sys.modules["htc"] = pkg
spec = importlib.util.spec_from_file_location("htc.llm", os.path.join(ROOT, "llm.py"))
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
fails = []
_real_file, _real_conn = llm.CONN_FILE, dict(llm.CONN)


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else "  " + detail))
    if not ok:
        fails.append(name)


def offers_schema():
    return "response_format" in llm._chat_body(
        "m", [{"role": "user", "content": "hi"}], schema=SCHEMA,
        temperature=0.7, max_tokens=128)


def fresh(file_body, backend="ninfer", servers=None):
    """A brand new settings file, loaded into a cleared CONN.

    Each scenario gets its OWN file and its OWN module state. Sharing one CONN
    between sub-tests is how the first version of this passed a wrong answer:
    a later sub-test inherited an earlier one's servers dict.
    """
    d = tempfile.mkdtemp()
    llm.CONN_FILE = os.path.join(d, "htc_llm.json")
    body = {"backend": backend,
            "server_url": "http://127.0.0.1:1234",
            "model": "qwen3_8_27b"}
    if servers is not None:
        body["servers"] = servers
    with open(llm.CONN_FILE, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    llm.CONN.clear()
    llm.CONN.update({"backend": backend, "server_url": "http://127.0.0.1:1234",
                     "model": "qwen3_8_27b", "servers": {}})
    llm._NO_STRUCTURED.clear()
    llm.load_conn()
    return d


NINFER = {"server_url": "http://127.0.0.1:8080",
          "exe": "C:/ninfer/ninfer-serve.exe",
          "model_file": "C:/ninfer/models/m.ninfer"}

print("\nSTRUCTURED BANK")
print("\nA. a server that has never been asked")
d = fresh(None, servers={"ninfer": dict(NINFER)})
ck("offers the schema", offers_schema())
llm._remember_no_structured()
ck("and stops offering it once refused", not offers_schema())
ck("the file carries the finding",
   ((json.load(open(llm.CONN_FILE, encoding="utf-8"))["servers"]["ninfer"])
    .get("no_structured")) == "http://127.0.0.1:8080")
ck("the saved paths are untouched",
   json.load(open(llm.CONN_FILE, encoding="utf-8"))["servers"]["ninfer"]["exe"]
   .endswith("ninfer-serve.exe"))
for f in os.listdir(d):
    os.remove(os.path.join(d, f))
os.rmdir(d)

print("\nB. the restart that matters")
d = fresh(None, servers={"ninfer": dict(NINFER)})
llm._remember_no_structured()
# Now the module is thrown away and the file is read again -- the state a
# ComfyUI restart produces.
banks = json.load(open(llm.CONN_FILE, encoding="utf-8"))["servers"]
d2 = tempfile.mkdtemp()
llm.CONN_FILE = os.path.join(d2, "htc_llm.json")
with open(llm.CONN_FILE, "w", encoding="utf-8") as fh:
    json.dump({"backend": "ninfer", "server_url": "http://127.0.0.1:1234",
               "model": "qwen3_8_27b", "servers": banks}, fh)
llm.CONN.clear()
llm.CONN.update({"backend": "ninfer", "server_url": "http://127.0.0.1:1234",
                 "model": "qwen3_8_27b", "servers": {}})
llm._NO_STRUCTURED.clear()
llm.load_conn()
ck("still not offered after a restart", not offers_schema())
ck("and the saved model file came back too",
   ((llm.CONN.get("servers") or {}).get("ninfer") or {}).get("model_file", "")
   .endswith("m.ninfer"))

print("\nC. the flag follows the ADDRESS, not the backend name")
d = fresh(None, servers={"ninfer": {"server_url": "http://127.0.0.1:8099",
                                    "no_structured": "http://127.0.0.1:8080"}})
ck("the SAME backend on a different port is unknown again", offers_schema(),
   "-- a flag keyed by name would follow it to the wrong address")

print("\nD. one refused server does not disable another")
d = fresh(None, backend="lmstudio",
       servers={"ninfer": dict(NINFER, no_structured="http://127.0.0.1:8080")})
ck("LM Studio still gets its schema", offers_schema())
ck("and nInfer on :8080 is remembered",
   llm._NO_STRUCTURED == {"http://127.0.0.1:8080"}, repr(llm._NO_STRUCTURED))

print("\nE. a file with no servers key at all still works")
d = fresh(None, servers=None)
ck("offers the schema", offers_schema())
ck("and nothing was invented",
   llm._NO_STRUCTURED == set(), repr(llm._NO_STRUCTURED))

for d in (d, d2):
    try:
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        os.rmdir(d)
    except OSError:
        pass
llm.CONN_FILE, _ = _real_file, None
llm.CONN.clear()
llm.CONN.update(_real_conn)

print()
if fails:
    print("%d check(s) failed." % len(fails))
    sys.exit(1)
print("STRUCTURED BANK: all passed.")