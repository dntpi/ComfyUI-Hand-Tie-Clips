"""Pre-ship sanity: walk both shipped workflows' plans through the real code.

Not a unit test -- it drives the same helpers `run()` drives, hop by hop, and
asserts the invariants that have actually broken before:

  * every `@tag` resolves, and nothing literal survives into the prompt;
  * every `<Picture N>` on a hop is either the live frame or a plate that is
    genuinely scheduled onto that hop;
  * identity text is present on every hop 2+ of a chain with a register (the
    chain_00057 failure);
  * shot 1 carries subject definitions when there is a register, and no later
    shot is a full H3 block;
  * the last shot closes on settle/hold.

It also REPORTS, without failing, any `<Subject N>` that reaches a continuation
hop. `subject_definitions` is hop-1 material and each hop is an independent text
encode, so such a token has no antecedent in its own conditioning.
"""
import io
import json
import os
import re
import sys
import types

PACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(PACK))
sys.path.insert(0, ROOT)
pkg = types.ModuleType("h3p")
pkg.__path__ = [PACK]
sys.modules["h3p"] = pkg

from h3p import refs as R          # noqa: E402
from h3p import plan as PL         # noqa: E402
from h3p import directives as D    # noqa: E402
from h3p import h3_ref_chain as H3  # noqa: E402

FAIL = []
NOTE = []


def ck(label, ok, detail=""):
    print("    %s %s%s" % ("ok  " if ok else "FAIL", label,
                           ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(label)


def widget_map(node):
    names = [i["name"] for i in node["inputs"] if i.get("widget")]
    names.insert(names.index("seed") + 1, "control_after_generate")
    return dict(zip(names, node["widgets_values"]))


for fn in ("HandTieClips_Starter.json", "HandTieClips_Showcase.json"):
    p = os.path.join(PACK, "workflows", fn)
    wf = json.load(io.open(p, encoding="utf-8"))
    chain = next(n for n in wf["nodes"] if n["type"] == "HandTieClips")
    wv = widget_map(chain)
    print("\n%s" % fn)

    plan = PL.parse_plan(wv["shot_plan"])
    shots = plan["shots"] if isinstance(plan, dict) else plan
    rp = R.parse_ref_plan(wv["ref_plan"])
    refs, subs = rp["refs"], rp["subjects"]
    wired = {r["slot"] for r in refs if r["file"]}
    has_people = any(r["subject"] is not None for r in refs)

    ck("shot count == chains", len(shots) == int(wv["chains"]),
       "%d vs %s" % (len(shots), wv["chains"]))
    warn = PL.check_coherence(shots)
    ck("coherence clean", not warn, " | ".join(warn))
    last_tail = (shots[-1].get("directives") or {}).get("tail")
    ck("last shot closes", last_tail in ("settle", "hold"), repr(last_tail))

    known = {r["tag"] for r in refs}
    for i, shot in enumerate(shots):
        beat = shot.get("beat") or ""
        used = set(re.findall(r"@([A-Za-z0-9_]+)", beat))
        active = R.active_refs(refs, i, wired)
        ords = R.ordinals(active)
        shift = 1 if i > 0 else 0            # live frame takes ordinal 1
        hop_ords = {t: n + shift for t, n in ords.items()}
        legal_pics = set(hop_ords.values()) | ({1} if i > 0 else set())

        ck("hop %d every tag declared" % (i + 1), used <= known,
           "unknown=%s" % sorted(used - known) if used - known else "")
        try:
            # Mirrors the node: names, not ordinals, from hop 2 on.
            resolved = R.resolve_tags(
                beat, hop_ords, R.subjects(refs), where="shot %d" % (i + 1),
                declared=known,
                subject_names=({k: (v or {}).get("name")
                                for k, v in (subs or {}).items()}
                               if i > 0 else None))
        except Exception as exc:                      # noqa: BLE001
            ck("hop %d tags resolve" % (i + 1), False, str(exc))
            continue
        ck("hop %d leaves no literal @tag" % (i + 1),
           not re.search(r"@[A-Za-z0-9_]+", resolved))

        # Same list tools/check_templates.py enforces. At cfg 1.0 with no
        # negative branch every named concept is ADDED, so "None of the kitchen
        # is visible" puts a kitchen in the encoder, and "she stops" is law 2.
        hits = [w for w in (" no ", " not ", " never ", "n't", "without ",
                            "stops ", "stop ", "silent", "silence", "none of ")
                if w in (" " + beat.lower() + " ")]
        ck("hop %d names nothing it wants absent" % (i + 1), not hits,
           str(hits))

        if i == 0:
            prose = R.subject_prose(active, subs)
            block = (prose + "\n\n" + resolved) if prose else resolved
            if refs:
                ck("hop 1 defines its subjects",
                   ("subject_definitions:" in prose) if has_people else True,
                   "%d chars of prose" % len(prose))
        else:
            id_ords = None
            n_subj = None
            if active:
                id_ords = [hop_ords[r["tag"]] for r in active
                           if r["subject"] is not None]
                n_subj = len({r["subject"] for r in active
                              if r["subject"] is not None}) or None
            elif refs:
                id_ords = []
            cont = R.continuity_line(
                subs, {r["subject"] for r in refs
                       if r["subject"] is not None}) if refs else ""
            block = H3._assemble_next(
                resolved, live_picture=1, live_video=1,
                n_stills=len(active),
                identity_ordinals=id_ords, n_subjects=n_subj,
                tail=(shot.get("directives") or {}).get("tail"),
                continuity=cont)

            pics = {int(m) for m in re.findall(r"<Picture (\d+)>", block)}
            ck("hop %d cites only scheduled plates" % (i + 1),
               pics <= legal_pics,
               "cited=%s legal=%s" % (sorted(pics), sorted(legal_pics)))
            ck("hop %d is not a full H3 block" % (i + 1),
               not D.is_full_h3_prompt(resolved))
            if has_people:
                ck("hop %d carries identity text" % (i + 1), bool(cont.strip()),
                   "%d chars" % len(cont))
            dangling = sorted(set(re.findall(r"<Subject (\d+)>", block)))
            if dangling:
                NOTE.append("%s hop %d: <Subject %s> with no definitions block "
                            "on this hop" % (fn, i + 1, ">, <Subject ".join(dangling)))

# -- the two prompt documents must not drift apart -----------------------------
# AUTHORING_PROMPT.md is SYSTEM_PROMPT.md wrapped in paste-me framing, and the
# rules in the middle are meant to be the same text. Nothing enforced that, and
# both files carry ~400 lines. DEVLOG 23 is what this looks like when it goes
# wrong: five documents claimed ffmpeg was required, long after it was not.
# The user-facing one is the AUTHORING copy, so a rule fixed only in
# SYSTEM_PROMPT is a rule fixed in the file nobody pastes.
print()
print("  prompt_pack: the two documents agree")


def _shared_blocks(text):
    """The parts the two files are supposed to hold in common."""
    out = {}
    # Every numbered rule. Matched on the OPENER alone: several rules carry a
    # bold header that wraps across a line, and requiring the closing `**` on
    # the same line silently skipped rules 1 and 9 -- a coverage hole that
    # looks exactly like a passing check.
    for m in re.finditer(r"^(\d+)\. \*\*", text, re.M):
        a = m.start()
        nxt = re.search(r"^\d+\. \*\*", text[a + 1:], re.M)
        end = re.search(r"^## ", text[a + 1:], re.M)
        stop = min([x.start() for x in (nxt, end) if x] or [len(text)])
        out["rule " + m.group(1)] = text[a:a + 1 + stop]
    blank = chr(10) + chr(10)
    m = re.search(r"^\| hop \|.*?(?=" + blank + ")", text, re.M | re.S)
    if m:
        out["length table"] = m.group(0)
    items = re.findall(r"^- \[ \] .+?(?=" + chr(10) + r"- \[ \]|" + blank + ")",
                       text, re.M | re.S)
    out["checklist"] = chr(10).join(" ".join(i.split()) for i in items)
    return out


_docs = {}
for _fn in ("SYSTEM_PROMPT.md", "AUTHORING_PROMPT.md"):
    _path = os.path.join(PACK, "prompt_pack", _fn)
    _docs[_fn] = _shared_blocks(io.open(_path, encoding="utf-8").read())

_a, _b = _docs["SYSTEM_PROMPT.md"], _docs["AUTHORING_PROMPT.md"]
ck("both documents define the same blocks", set(_a) == set(_b),
   "only in one: %s" % sorted(set(_a) ^ set(_b)))
# A comparison that quietly stops finding things passes just as loudly as one
# that finds everything and matches. Pin the count.
_rules = [k for k in _a if k.startswith("rule ")]
# 14 as of rule 14, the light and air vocabulary. Bump this in the SAME commit
# that adds a rule: the count is here so that a rule which fails to match the
# block pattern is loud rather than silently absent, and it can only do that if
# it is kept honest.
ck("all 14 numbered rules were found", len(_rules) == 14,
   "found %d: %s" % (len(_rules),
                     sorted(int(r.split()[1]) for r in _rules)))
for _key in sorted(set(_a) & set(_b)):
    _x = " ".join(_a[_key].split())
    _y = " ".join(_b[_key].split())
ck("%s matches" % _key, _x == _y,
   "" if _x == _y else "SYSTEM %d chars vs AUTHORING %d" % (len(_x), len(_y)))

# ------------------------------------------------------- beat_table (A7's band)
# The shot cards print "47 / 35-60 words" under every beat, and that band is
# this table -- read out of the prompt rather than restated, so the number the
# user is judged against is the number the model was asked for. If the table
# stops parsing, the cards quietly fall back to a bare count with no band, which
# looks like the UI working. Pin that it parses, and pin the 8 s row exactly:
# a silent edit to the prompt table must fail here rather than quietly widen
# every card's band.
from h3p import planner as PLANNER   # noqa: E402

_bt = PLANNER.beat_table()
ck("beat_table parses rows out of the prompt", bool(_bt), repr(_bt))
ck("every duration has a band", len(_bt) >= 5,
   "%d: %s" % (len(_bt), sorted(_bt)))
# A band is (min_words, max_words, min_lines, max_lines) with min <= max; a row
# that parses with min > max would make every beat read as "over".
ck("every band is ordered and positive",
   all(0 < w0 <= w1 and 0 < l0 <= l1 for (w0, w1, l0, l1) in _bt.values()),
   repr(_bt))
ck("the 8 s band is 35-60 words, 1-2 lines",
   _bt.get("8 s") == (35, 60, 1, 2), repr(_bt.get("8 s")))
# The routes publish these labels and the cards look them up by the duration
# widget's own value, so a label with a stray double space ("8  s") would look
# up nothing and every card would silently lose its band.
ck("labels are whitespace-normalised for the JS lookup",
   all("  " not in k and k == k.strip() for k in _bt),
   repr([k for k in _bt if "  " in k or k != k.strip()]))

# ---------------------------------------------------- the light and air tables
# Rule 14's two tables are the one vocabulary that is NOT an axis, so nothing
# else parses them and a row that stops matching would be dropped in silence.
# The pack's four phrasing rules are the test, and they are load-bearing: cfg
# 1.0 with no negative branch means anything named is additive, so a negated
# row ("no hard shadow") puts `hard shadow` in front of the encoder.
_doc = io.open(os.path.join(PACK, "prompt_pack", "SYSTEM_PROMPT.md"),
               encoding="utf-8").read()
# Scoped to rule 14's OWN two tables. A loose match also catches the directive
# value table above it (`join` | `continuous`, `match_cut`...) and the hop
# length table, and those rows are lists rather than sentences -- which is what
# the first version of this check reported.
_r14 = _doc[_doc.index("14. **Name the light and the air"):]
_r14 = _r14[:_r14.index("\n## ")] if "\n## " in _r14 else _r14
# The rows are indented four spaces -- they sit inside the numbered rule, like
# every other continuation line in this document -- so the leading space is
# part of the pattern, not incidental.
_rows = re.findall(r"^\s*\|\s*`([a-z_]+)`\s*\|\s*(.+?)\s*\|\s*$", _r14, re.M)
ck("rule 14 has a table of light and air options", len(_rows) >= 15,
   "%d rows" % len(_rows))
_mine = [s for _k, s in _rows if s.strip()]
ck("every option is a full sentence, not a name",
   all(s.strip().endswith(".") for s in _mine),
   repr([s for s in _mine if not s.strip().endswith(".")][:3]))
# Affirmative only. "no hard shadow" is the exact failure mode the pack was
# built on: with no negative branch the words after `no` reach the encoder.
_neg = [s for s in _mine
        if re.search(r"\b(no|not|never|without|avoid|lacks?|isn't|aren't)\b",
                     s, re.I)]
ck("no option names a negative", not _neg, repr(_neg[:2]))
# No pronouns: plural "they" has been observed rendering two people from one
# reference.
_pron = [s for s in _mine if re.search(r"\b(they|them|their|he|she|his|her)\b",
                                       s, re.I)]
ck("no option names a person or a pronoun", not _pron, repr(_pron[:2]))
# One sentence per entry: these concatenate into a beat with a word budget.
_long = [s for s in _mine if s.count(".") > 1]
ck("every option is one sentence", not _long, repr(_long[:2]))
# The shortest beat band is 30 words, so a table row that cannot fit in the
# eight words the rule budgets would push the beat out of band.
_long_words = [s for s in _mine if len(s.split()) > 22]
ck("no option is long enough to break the beat budget", not _long_words,
   repr([s[:40] for s in _long_words[:2]]))
# The prompt table carries bands for the lengths it documents (5-15 s). The
# node's duration combo offers more than that, and a card at a length with no
# row is supposed to fall back to a bare count -- that is a supported state,
# not a defect. What must hold is that the DOCUMENTED lengths are covered, so
# the common hops are the ones that get a target.
ck("every documented length has a band",
   {"5 s", "7 s", "8 s", "10 s", "15 s"} <= set(_bt),
   "missing: %s" % sorted({"5 s", "7 s", "8 s", "10 s", "15 s"} - set(_bt)))

print()
for n in NOTE:
    print("NOTE  " + n)
print()
if FAIL:
    print("%d FAILURE(S):\n  %s" % (len(FAIL), "\n  ".join(FAIL)))
    raise SystemExit(1)
print("SHIP CHECK: all clear (%d note%s)" % (len(NOTE), "" if len(NOTE) == 1 else "s"))
