r"""Grade a language model's shot_plan/ref_plan reply against the real node.

Point it at a file containing a model's raw answer -- the prose plus its two
json blocks -- and it runs the plan through the same parsers, validators and
lints `run()` uses, then reports what a human still has to judge.

    D:\ComfyUI\venv\Scripts\python.exe tools\grade_plan.py reply.md --hops 6

Three severities, and the split is the whole point:

  FAIL   the node would reject or mis-render this. Mechanical, no judgment.
  WARN   a lint the node itself prints as a note. Costs a render, not a queue.
  LOOK   a rule from SYSTEM_PROMPT.md that no parser can decide. Quoted for a
         human to rule on. These are where two models that both parse cleanly
         actually differ.

Run it with ComfyUI's interpreter -- importing the node pulls in torch.
"""
import io
import json
import os
import re
import sys
import types

# Located from this file, like the other checkers: the pack moves, gets
# cloned to a second machine, or is run from a different drive letter.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
pkg = types.ModuleType("h3p")
pkg.__path__ = [HERE]
sys.modules["h3p"] = pkg

from h3p import refs as R          # noqa: E402
from h3p import plan as PL         # noqa: E402
from h3p import directives as D    # noqa: E402
from h3p import h3_ref_chain as H3  # noqa: E402

# `parse_plan` stores a duration label without looking at it; `run()` rejects an
# unknown one at h3_ref_chain.py:1399. So "10s" and "10 seconds" both survive
# every parser in this file and then kill the queue. Checked here because a
# local model getting the label format wrong is a likely, cheap-to-catch fault.
DUR = H3.DURATION_FRAMES


# ---------------------------------------------------------------- rule tables

# Rule 2's explicit list. The node's compile path does not reject these --
# `tools/check_prompts.py` does, and only for the two shipped workflows.
HARD_NEG = (" no ", " not ", " never ", "n't", "without ", "none of ",
            "stops ", "stop ", " silent", "silence")

# Rule 2's *idea*, which is the part a word list cannot catch. Each of these
# names a thing that has finished happening, so at cfg 1.0 it adds the very
# thing it means to remove. Quoted, never auto-failed: "she passes the knife"
# and "the storm passes" share a verb and only a reader can tell them apart.
SOFT_NEG = ("fade", "fades", "fading", "subside", "subsides", "subsiding",
            "dies down", "dying down", "die down", "eases off", "ease off",
            "easing off", "recede", "recedes", "receding", "wane", "wanes",
            "waning", "grows quiet", "growing quiet", "falls quiet",
            "falling quiet", "drops away", "dropping away", "drop away",
            "trails off", "trailing off", "cease", "ceases", "ceasing",
            "halt", "halts", "abate", "abates", "diminish", "diminishes",
            "lessen", "lessens", "taper", "tapers", "tapering",
            "quieten", "quietens", "quiets", "slows to a", "at last",
            "finally", "no longer", "has passed", "passes", "is over",
            "dwindle", "dwindles", "dwindling", "settles into")

# Rule 4. Broadband ambience renders as five seconds of hiss. "a distant car"
# is endorsed by the prompt itself, so `distant` alone is not the tell -- the
# tell is a category noun standing in for a discrete event.
BROADBAND = ("street noise", "city sounds", "sounds of the city",
             "background noise", "ambient noise", "ambient sound",
             "general noise", "muffled sounds", "faint noise", "faint sounds",
             "traffic noise", "crowd noise", "the sounds of", "noises of",
             "atmospheric", "soundscape")


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def quote(text, needle, width=100):
    """The sentence a needle landed in, so a reader can rule on it."""
    for s in sentences(text):
        if needle in s.lower():
            return s if len(s) <= width else s[:width - 3] + "..."
    i = (text or "").lower().find(needle)
    return (text or "")[max(0, i - 30):i + 60]


# ---------------------------------------------------------------- extraction

def blocks(raw):
    """The fenced json blocks in a model reply, in order."""
    out = re.findall(r"```(?:json)?\s*\n(.*?)```", raw, re.S)
    return [b.strip() for b in out if b.strip()]


def split_reply(raw):
    """-> (shot_plan_text, ref_plan_text, notes).

    Tolerant on purpose: the job is to grade what the model planned, not to
    reward fence discipline. Every leniency is recorded as a note, so a model
    that needed one does not silently score the same as one that did not.
    """
    notes = []
    bs = blocks(raw)
    if len(bs) < 2:
        for b in (bs or [raw]):
            try:
                d = json.loads(b)
            except Exception:                                  # noqa: BLE001
                continue
            if isinstance(d, dict) and "shots" in d and "refs" in d:
                notes.append("emitted ONE json object, not two documents")
                return (json.dumps({"shots": d["shots"]}),
                        json.dumps({k: v for k, v in d.items()
                                    if k != "shots"}), notes)
        if len(bs) == 1:
            notes.append("only one json block found")
            return bs[0], "", notes
        notes.append("no fenced json block found at all")
        return "", "", notes
    if len(bs) > 2:
        notes.append("%d json blocks; graded the first two" % len(bs))
    shot, ref = bs[0], bs[1]
    try:                                    # order is not guaranteed; sniff it
        if "refs" in json.loads(shot):
            shot, ref = ref, shot
            notes.append("blocks came back ref-then-shot")
    except Exception:                                          # noqa: BLE001
        pass
    return shot, ref, notes


def hygiene(raw_json, label, rep):
    """The JSON faults prompt_pack/README predicts above temperature 0.5."""
    if re.search(r"[\u201c\u201d\u2018\u2019]", raw_json):
        rep.warn("%s contains smart quotes" % label)
    if re.search(r",\s*[}\]]", raw_json):
        rep.warn("%s contains a trailing comma" % label)


# ------------------------------------------------------------------- report

class Report(object):
    def __init__(self, name):
        self.name = name
        self.fails, self.warns, self.looks, self.oks = [], [], [], []
        self.shape = ""

    def fail(self, m, d=""):
        self.fails.append((m, d))

    def warn(self, m, d=""):
        self.warns.append((m, d))

    def look(self, m, d=""):
        self.looks.append((m, d))

    def ok(self, m):
        self.oks.append(m)

    def ck(self, m, cond, d=""):
        self.ok(m) if cond else self.fail(m, d)
        return cond

    def render(self):
        print("\n" + "=" * 74)
        print("  %s" % self.name)
        print("=" * 74)
        if self.shape:
            print("  %s\n" % self.shape)
        for tag, rows in (("FAIL", self.fails), ("WARN", self.warns),
                          ("LOOK", self.looks)):
            for m, d in rows:
                print("  %-5s %s" % (tag, m))
                for line in str(d).split("\n"):
                    if line.strip():
                        print("          %s" % line.strip())
        print("  ---- %d passed, %d FAIL, %d WARN, %d LOOK"
              % (len(self.oks), len(self.fails), len(self.warns),
                 len(self.looks)))
        return self


# -------------------------------------------------------------------- grade

def grade(path, want_hops=None):
    raw = io.open(path, encoding="utf-8", errors="replace").read()
    rep = Report(os.path.basename(path))
    sp_text, rp_text, notes = split_reply(raw)
    for n in notes:
        rep.warn(n)
    if not sp_text:
        rep.fail("no shot_plan to grade")
        return rep.render()

    hygiene(sp_text, "shot_plan", rep)
    if rp_text:
        hygiene(rp_text, "ref_plan", rep)

    try:
        shots = PL.parse_plan(sp_text)
        rep.ok("shot_plan parses and validates")
    except Exception as exc:                                   # noqa: BLE001
        rep.fail("shot_plan rejected by the node", exc)
        return rep.render()

    refs, subs, rp = [], {}, None
    if rp_text:
        try:
            rp = R.parse_ref_plan(rp_text)
            refs, subs = rp["refs"], rp["subjects"]
            rep.ok("ref_plan parses and validates")
        except Exception as exc:                               # noqa: BLE001
            rep.fail("ref_plan rejected by the node", exc)
    else:
        rep.warn("no ref_plan block")

    # Graded as if the user wired every slot the model asked for.
    wired = {r["slot"] for r in refs if r["file"]}
    known = {r["tag"] for r in refs}
    has_people = any(r["subject"] is not None for r in refs)

    if want_hops:
        rep.ck("shot count == %d requested hops" % want_hops,
               len(shots) == want_hops, "got %d" % len(shots))

    # Beat length is not a rule, but it is the cheapest tell for a model that
    # padded its way through the brief or ran out of scene halfway.
    rep.shape = ("%d shots, beats %s chars | %d refs, %d subject(s)"
                 % (len(shots), "/".join(str(len(s.get("beat") or ""))
                                         for s in shots), len(refs), len(subs)))

    for w in PL.check_coherence(shots):
        rep.warn("coherence", w)
    if rp:
        for w in PL.check_place_handoff(shots, rp):
            rep.warn("place handoff", w)
    for w in PL.check_over_delivery(shots):
        rep.warn("over-delivery", w)

    last_tail = (shots[-1].get("directives") or {}).get("tail")
    rep.ck("last shot closes on settle/hold", last_tail in ("settle", "hold"),
           "tail=%r" % last_tail)

    bad_dur = [(i + 1, s["duration"]) for i, s in enumerate(shots)
               if s.get("duration") and str(s["duration"]) not in DUR]
    rep.ck("every duration label is one the node accepts", not bad_dur,
           "%s -- must be one of: %s"
           % (", ".join("shot %d: %r" % b for b in bad_dur), ", ".join(DUR))
           if bad_dur else "")

    for i, shot in enumerate(shots):
        h = i + 1
        beat = shot.get("beat") or ""
        low = " " + beat.lower() + " "

        hits = [x.strip() for x in HARD_NEG if x in low]
        rep.ck("hop %d names nothing it wants absent" % h, not hits,
               ("banned: %s\n%s" % (", ".join(hits), quote(beat, hits[0])))
               if hits else "")

        for s in [x for x in SOFT_NEG
                  if re.search(r"\b%s\b" % re.escape(x), low)][:3]:
            rep.look("hop %d: '%s' may name a thing that has FINISHED "
                     "happening (rule 2)" % (h, s), quote(beat, s))

        for b in [x for x in BROADBAND if x in low]:
            rep.look("hop %d: '%s' is broadband ambience (rule 4)" % (h, b),
                     quote(beat, b))

        # Rule 6: speech at the end of a hop rides the audio pin into the next.
        tail_txt = beat[-120:]
        if i < len(shots) - 1 and re.search(
                r"['\u2019\"][^'\u2019\"]{8,}['\u2019\"]\s*\.?\s*$", tail_txt):
            rep.look("hop %d ends ON dialogue; the audio pin carries it into "
                     "hop %d (rule 6)" % (h, h + 1), tail_txt.strip())

        if '"' in beat:
            rep.look("hop %d uses double quotes for speech" % h,
                     "the pack's example uses single quotes; doubles inside "
                     "JSON are an escaping hazard")

        if not refs:
            continue

        used = set(re.findall(r"@([A-Za-z0-9_]+)", beat))
        rep.ck("hop %d every tag declared" % h, used <= known,
               "unknown=%s" % sorted(used - known))
        active = R.active_refs(refs, i, wired)
        ords = R.ordinals(active)
        shift = 1 if i > 0 else 0            # live frame takes ordinal 1
        hop_ords = {t: n + shift for t, n in ords.items()}
        legal = set(hop_ords.values()) | ({1} if i > 0 else set())

        try:
            resolved = R.resolve_tags(
                beat, hop_ords, R.subjects(refs), where="shot %d" % h,
                declared=known,
                subject_names=({k: (v or {}).get("name")
                                for k, v in (subs or {}).items()}
                               if i > 0 else None))
        except Exception as exc:                               # noqa: BLE001
            rep.fail("hop %d tags resolve" % h, exc)
            continue
        rep.ck("hop %d leaves no literal @tag" % h,
               not re.search(r"@[A-Za-z0-9_]+", resolved))

        if i == 0:
            prose = R.subject_prose(active, subs)
            rep.ck("hop 1 defines its subjects",
                   ("subject_definitions:" in prose) if has_people else True,
                   "%d chars of prose" % len(prose))
            continue

        id_ords = ([hop_ords[r["tag"]] for r in active
                    if r["subject"] is not None] if active else [])
        n_subj = (len({r["subject"] for r in active
                       if r["subject"] is not None}) or None) if active else None
        cont = R.continuity_line(
            subs, {r["subject"] for r in refs if r["subject"] is not None})
        block = H3._assemble_next(
            resolved, live_picture=1, live_video=1, n_stills=len(active),
            state_header="", identity_ordinals=id_ords, n_subjects=n_subj,
            tail=(shot.get("directives") or {}).get("tail"), continuity=cont)

        pics = {int(m) for m in re.findall(r"<Picture (\d+)>", block)}
        rep.ck("hop %d cites only scheduled plates" % h, pics <= legal,
               "cited=%s legal=%s" % (sorted(pics), sorted(legal)))
        rep.ck("hop %d is not a full H3 block" % h,
               not D.is_full_h3_prompt(resolved))
        if has_people:
            rep.ck("hop %d carries identity text" % h, bool(cont.strip()))
        dang = sorted(set(re.findall(r"<Subject (\d+)>", block)))
        if dang:
            rep.warn("hop %d: <Subject %s> has no definitions block on this hop"
                     % (h, ">, <Subject ".join(dang)))

    return rep.render()


if __name__ == "__main__":
    argv = sys.argv[1:]
    hops = None
    paths = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--hops"):
            if "=" in a:
                hops = int(a.split("=", 1)[1])
            else:
                i += 1
                hops = int(argv[i])
        else:
            paths.append(a)
        i += 1
    reps = [grade(p, hops) for p in paths]
    print("\n" + "=" * 74)
    print("  SUMMARY")
    print("=" * 74)
    for r in reps:
        print("  %-30s %2d pass  %d FAIL  %d WARN  %d LOOK"
              % (r.name, len(r.oks), len(r.fails), len(r.warns), len(r.looks)))
