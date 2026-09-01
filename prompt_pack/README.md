# prompt_pack

Everything needed to have a language model write plans for this node.

| file | what it is |
|---|---|
| **`AUTHORING_PROMPT.md`** | Paste into any chat model, describe your scene, paste the two JSON blocks it returns into the node. Start here. |
| **`EXAMPLE_6_HOP.md`** | A worked six-hop plan with the reasoning behind its reference schedule. Useful as a second message to the model when you want it to match a shape. |
| **`SYSTEM_PROMPT.md`** | The same thing with the human preamble stripped, for pasting into a *System Prompt* box. Select all, paste, done. |
| **`SCHEMA.json`** | JSON Schema for both documents, for anyone wiring this into their own tooling. Carries the rule set under `x-rules` and the duration/frame table under `x-duration-frames`. |

## Using it in LM Studio (or any local chat app)

1. Load a model and set its **context length to 16384 or more** — and to
   **32768 if the model reasons**. The system prompt is ~4,700 tokens and the
   reply is another 1,000-2,000; a small window truncates the rules and you get
   invented directive names. A reasoning model adds far more than that on top:
   gemma4-26b measured 8,010 tokens of thinking before 858 tokens of JSON, and
   at a 4,096 reply budget it produced *no answer at all* — 4,093 tokens of
   reasoning and an empty message, which looks like a refusal and is really a
   budget that ran out mid-thought.
2. Open `SYSTEM_PROMPT.md`, select all, paste it into the **System Prompt** box
   in LM Studio's right-hand sidebar. Nothing else goes in that box.
3. Set **temperature 0.3-0.5**. Higher and the JSON starts growing trailing
   commas and smart quotes.
4. In the chat, describe your scene in plain language, and say **how many hops**
   and **what pictures you have**:

   > Six hops. A cook in a kitchen; she says one line, walks out into a hallway,
   > waits by a window, then comes back. I have a face photo, a photo of her
   > apron, and a photo of the kitchen.

5. It answers with a paragraph of reasoning and two ```json``` blocks. Each
   section of the panel has its own **JSON** disclosure at the bottom:
   - the first block goes in the **JSON** box under **SCRIPT** (`shot_plan`);
   - the second goes in the **JSON** box under **REFERENCES** (`ref_plan`).

   Both parse as you type. Bad JSON leaves the cards and rows showing the last
   good version and says so, rather than throwing your paste away.
6. Put your pictures in `ComfyUI/input/h3_refs` under the filenames the model
   used, or drop them onto the reference thumbnails and fix the names.
7. Queue. **If the node rejects the plan, paste the error straight back into the
   chat** — every message names the shot or reference it came from, and one
   round trip usually fixes it.

Want it to match a particular shape? Paste `EXAMPLE_6_HOP.md` as a second
message before describing your scene.

## Or let the panel do it — ALPHA

The steps above are the supported route and are not going anywhere. They work
with any chat model, hosted or local, and need nothing running.

The **WRITE** section at the top of the node's panel automates them against a
local OpenAI-compatible server. Open it, open **Settings**, point it at LM
Studio (`http://127.0.0.1:1234`), pick a **loaded** model — the dropdown marks
loaded ones with `●`, because LM Studio lists everything installed — and Save.
Then type the scene in one line, set the hop count, and press **Write plan**.

What it does that the copy-paste route cannot: it runs step 7 for you. The plan
comes back, the node's own validators check it, and any error is fed back to the
model for another attempt — up to three. The status line names each repair as it
happens, so a fixed `@tag` mismatch reads as

    attempt 2/3 — fixing: shot 1: the beat uses @kitchen but the reference
    register does not declare it.

rather than happening invisibly. Lints that do not stop a queue are shown but
never retried; read them before you queue.

Notes and limits:

* **Nothing here runs during a render.** The button fills the two JSON boxes and
  stops. Queueing reads the boxes, so a graph still renders with the network
  unplugged and an API-submitted workflow is unaffected — it also means a
  headless run gets no plan writer.
* **Settings live on this machine**, in a gitignored `htc_llm.json` beside the
  node, never in the workflow. A shared `.json` cannot point at your server.
* **Unload after writing** is on by default. A 27B and an H3 render do not fit
  on one card. It is skipped when the server is on another machine.
* **Free VRAM** unloads everything the writer is holding, on demand. Reach for
  it before queueing if a render just ran out of memory: the automatic unload
  only hands back the model configured here, and LM Studio may have loaded a
  different one on its own.
* No API keys, no cloud providers, no model downloading — local servers only.
* This is **alpha**. The manual recipe above is the one to fall back on.

Small local models (7B-8B) hold the JSON schema fine but drift on the prose
rules — they will write negations. Read the plan before queueing; a beat that
says "she stops talking" costs you a render.

`SCHEMA.json`, `EXAMPLE_6_HOP.md` and `SYSTEM_PROMPT.md` are **generated**, not
written:

```
python tools/gen_schema.py           # regenerate the schema from the node
python tools/gen_schema.py --check   # exit 1 if it is out of date
python tools/gen_example.py          # regenerate the example from the workflow
sed -n '14,$p' prompt_pack/AUTHORING_PROMPT.md \n    > prompt_pack/SYSTEM_PROMPT.md   # re-strip the preamble
```

Both read the installed node and the shipped workflow, so they cannot describe a
vocabulary or a plan that does not exist. Run `gen_schema.py` with ComfyUI's
interpreter — importing the node pulls in torch. Re-run both after changing
`directives.VOCAB`, `refs.RETENTION`, the shot fields, or the duration table;
`gen_schema.py` asserts against `plan._SHOT_KEYS`, `refs.REF_FIELDS` and
`refs.SUBJECT_FIELDS` and will fail loudly rather than emit a stale schema.

The node validates every plan it is handed, so a model that gets this wrong is
caught rather than obeyed. Errors name the shot or the reference they came from
— pasting one back to the model is usually enough to fix it.

The prose guide for humans is [`../PROMPTING.md`](../PROMPTING.md), and the
Starter workflow carries a condensed version of it as a card board on its own
canvas — including the LM Studio recipe above.
