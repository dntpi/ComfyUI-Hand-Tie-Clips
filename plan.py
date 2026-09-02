"""Shot-plan schema for H3 Ref Chain.

A plan is the authored script for one chain: an ordered list of shots, one per
hop. It is carried as a single JSON string so the whole plan lives in one widget
and serializes with the workflow -- the DOM editor renders cards from this and
writes back to the same string, so the JSON stays the source of truth whether it
was typed by hand or clicked together.

The shot count IS the hop count. There is no separate `chains` number to keep in
sync, which removes a whole class of "3 blocks but chains=4" mismatches.

Shot fields (all optional except `beat`):
    beat        what happens this hop
    directives  {join, camera, framing, pace, tail} -- see directives.VOCAB
    prose       free text appended verbatim, for anything the vocabulary lacks
    seed        int override, else the chain seed
    steps       int override
    duration    str override, e.g. "8 s"
    locked      bool -- reuse this hop's cached render (hop store, step 4)
    tone        "" | "free" | "rebase" -- opt out of tone_compensate=anchor's
                chain-wide pull for this hop. "free" skips the pull once;
                "rebase" also moves the anchor to this hop, for a scene that is
                deliberately darker (or brighter) from here on.
    id          stable identifier, generated if absent
"""

import json

from . import directives as _d

TAG = "HandTieClips"

# No "refs" here. A shot never activated a reference: activation is the ref's
# own `shots` list in refs.py. The field was parsed, normalised and printed but
# read by nothing -- and the editor neither loads nor writes it, so a
# hand-authored shot_plan lost it the first time anyone touched a card. Leaving
# it out means _norm_shot's unknown-field error names it and points at `shots`.
_SHOT_KEYS = {"id", "beat", "directives", "prose",
              "seed", "steps", "duration", "locked", "tone"}


TONE_VALUES = ("", "free", "rebase")


def _tone_field(v, where):
    """Validate a shot's `tone` opt-out. -> "" | "free" | "rebase"."""
    if v in (None, "", False):
        return ""
    v = str(v).strip().lower()
    if v not in TONE_VALUES:
        raise ValueError(
            f"{TAG}: {where}tone must be one of {[x for x in TONE_VALUES if x]} "
            f"(or omitted), got {v!r}"
        )
    return v


def _norm_shot(raw, i):
    where = f"shot {i + 1}: "
    if isinstance(raw, str):
        raw = {"beat": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"{TAG}: {where}each shot must be an object or a string")

    unknown = set(raw) - _SHOT_KEYS
    if unknown:
        raise ValueError(
            f"{TAG}: {where}unknown field(s) {sorted(unknown)}. "
            f"Valid: {sorted(_SHOT_KEYS)}"
        )

    d_raw = raw.get("directives") or {}
    if not isinstance(d_raw, dict):
        raise ValueError(f"{TAG}: {where}directives must be an object")
    bad = set(d_raw) - set(_d.AXES)
    if bad:
        raise ValueError(
            f"{TAG}: {where}unknown directive axis/axes {sorted(bad)}. "
            f"Valid: {list(_d.AXES)}"
        )
    dirs = {ax: _d.validate(ax, d_raw.get(ax), where=where) for ax in _d.AXES}
    dirs = {k: v for k, v in dirs.items() if v}

    def _int(name):
        v = raw.get(name)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{TAG}: {where}{name} must be a whole number, got {v!r}")

    return {
        "id": str(raw.get("id") or f"s{i + 1}"),
        "beat": str(raw.get("beat") or "").strip(),
        "directives": dirs,
        "prose": str(raw.get("prose") or "").strip(),
        "seed": _int("seed"),
        "steps": _int("steps"),
        "duration": (str(raw["duration"]).strip() or None) if raw.get("duration") else None,
        "locked": bool(raw.get("locked")),
        "tone": _tone_field(raw.get("tone"), where),
    }


def parse_plan(text):
    """Parse a shot-plan JSON string. Blank -> [] (caller falls back to `prompt`)."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{TAG}: shot_plan does not parse as JSON ({e})") from e

    if isinstance(data, dict):
        shots = data.get("shots")
        if shots is None:
            raise ValueError(f'{TAG}: shot_plan object needs a "shots" array')
    elif isinstance(data, list):
        shots = data
    else:
        raise ValueError(f'{TAG}: shot_plan must be an object with "shots", or an array')

    if not isinstance(shots, list) or not shots:
        raise ValueError(f"{TAG}: shot_plan has no shots")

    out = [_norm_shot(s, i) for i, s in enumerate(shots)]
    if not any(s["beat"] or s["prose"] for s in out):
        raise ValueError(f"{TAG}: shot_plan has no beat text in any shot")
    return out


def check_coherence(shots):
    """Warn (never raise) about directive combinations that fight each other.

    A framing change asks the audience to be somewhere new. With a moving camera
    the move earns it; with a held camera the only way to get there is a cut, so
    `join=continuous` and the framing change are asking for opposite things and
    the model will pick one. Warn rather than raise -- it is a legitimate thing
    to want, it just rarely reads as continuous.

    Also warns when the camera move and the framing point opposite ways
    (push_in + wide, pull_back + close), which is a contradiction regardless of
    how the hop joins.
    """
    warnings = []
    # push_in narrows the frame, pull_back opens it. Asking for one and naming
    # the opposite destination is a physical contradiction at any join value,
    # and nothing warned about it before.
    _opposed = {("push_in", "wide"), ("pull_back", "close")}
    # The framing this shot INHERITS. `keep` and an unset axis both leave it
    # alone, so the last named value carries forward. Empty means no shot has
    # named one yet, and a first mention is not a change.
    prev_framing = ""
    for i, s in enumerate(shots):
        d = s.get("directives") or {}
        framing = d.get("framing", "")
        camera = d.get("camera", "")
        # Hop 1 has no previous hop, and directive_prose does not emit `join`
        # there, so a join warning on shot 1 points at a sentence that is never
        # compiled. The JS mirror already skips index 0.
        #
        # `framing != prev_framing` is what this check is FOR and it was
        # missing: the docstring says a framing CHANGE, but the test read
        # "framing is named and is not `keep`". Models restate the framing on
        # every shot -- the axis describes the shot, not a transition -- so a
        # plan reading medium/medium/medium tripped this on every hop after the
        # first while the framing never moved. chain_00052 was exactly that
        # shape and seamed at -0.31/255, one of the cleanest joins measured.
        if (i > 0 and d.get("join") == "continuous"
                and framing not in ("", "keep")
                and prev_framing and framing != prev_framing
                and camera in ("", "hold")):
            warnings.append(
                f"shot {i + 1}: join=continuous changes framing from "
                f"{prev_framing} to {framing} with a held camera, which implies "
                f"a cut. Use camera=push_in/pull_back/pan_follow to reach that "
                f"framing on the move, or framing=keep."
            )
        if (camera, framing) in _opposed:
            warnings.append(
                f"shot {i + 1}: camera={camera} moves the opposite way from "
                f"framing={framing}. Pick the framing the move actually lands on."
            )
        if framing not in ("", "keep"):
            prev_framing = framing
    return warnings


def check_place_handoff(shots, ref_plan=None):
    """Warn when a hop changes location without the previous beat arriving there.

    This is the failure that put the one visible cut in the 8x15 s anime chain:
    shot 3 ended "ahead the trunks begin to thin toward open ground" and shot 4
    opened "Across the flat moonlit stone of @arena_clearing the two of them
    square off". Hop 4 was handed a live frame of a man among trees and a beat
    asserting he was already standing on open stone. It held the forest for
    3.25 s and then reset the scene -- a hard cut 78 frames into the hop, the
    single largest frame-to-frame jump in 114 seconds of film.

    Nothing warned. `check_coherence` sees only directives, and the plan was
    clean by every other check the pack has.

    The rule the warning encodes: a beat must be true from ANY plausible ending
    of the hop before it. When shot N names a place tag that shot N-1 never
    mentions, shot N-1 has to do the arriving, or the model has to cut.

    Two shapes are accepted as an arrival, because both work in practice:
    shot N-1 naming the new place tag itself, or shot N's own beat carrying the
    journey ("reaches the top of the stairs and pushes open the door"), which is
    satisfiable from a live frame that is still on the stairs.

    `ref_plan` is the parsed dict from refs.parse_ref_plan. Without it there is
    no way to tell a place tag from a face tag, so the check no-ops.
    """
    warnings = []
    if not ref_plan or len(shots) < 2:
        return warnings
    # A ref with no subject is a place plate. Faces ride people, not rooms.
    places = {r["tag"] for r in (ref_plan.get("refs") or []) if not r.get("subject")}
    if not places:
        return warnings

    # Phrases that mean the beat itself carries the journey, so it is
    # satisfiable from a live frame still in the old location. The pack's own
    # Showcase is the case to keep clean: shot 5 leaves her at a window in the
    # hallway and shot 6 reads "She walks back along the hallway and through
    # the doorway to the counter in @kitchen" -- the travelling is right there
    # in the beat, and warning about it would be noise.
    #
    # Kept as travel language rather than a list of rooms: what makes a beat
    # safe is that it starts where the last hop ended and moves, not which
    # place it moves to.
    _ARRIVES = (
        "reach", "arriv", "enter", "emerg",
        "step into", "steps into", "step through", "steps through",
        "walk into", "walks into", "walk back", "walks back", "walking back",
        "walk through", "walks through", "go back", "goes back",
        "return", "returns", "returning", "head back", "heads back",
        "head toward", "heads toward", "back along", "back through",
        "push open", "pushes open", "through the door", "through the doorway",
        "through the gate", "through the entrance", "across into",
        "come out", "comes out", "break out", "breaks out",
        "cross into", "crosses into", "climb", "climbs", "descend", "descends",
        "makes her way", "makes his way", "makes their way",
    )

    # Where the film currently IS, carried forward across beats that name no
    # place at all. Comparing only against shot N-1 made a place the film never
    # left read as newly arrived the moment one beat in the middle did not
    # happen to name it: platform / (unnamed) / platform warned on shot 3, for
    # a chain that spends every hop on one platform. What matters is whether
    # this beat names somewhere OTHER than where the previous hop ended, which
    # is what the live frame at its first frame actually shows.
    current = set()
    for i, s in enumerate(shots):
        low = (s.get("beat") or "").lower()
        named = {t for t in places if ("@" + t).lower() in low}
        if i and current:
            for tag in sorted(named - current):
                if any(v in low for v in _ARRIVES):
                    continue
                warnings.append(
                    f"shot {i + 1}: @{tag} is a new place and shot {i} is "
                    f"somewhere else. The hop opens on a live frame of the old "
                    f"location, so the only way to obey is a cut. End shot {i} "
                    f"with the arrival, or have shot {i + 1} do the travelling."
                )
        if named:
            current = named

    # The same defect seen from the other side. Both LM Studio models plated
    # the opening location, moved the story somewhere else, and gave the new
    # place no plate at all -- then justified it with a rule that does not
    # exist ("to avoid conflicting with the frame pin of the new space"). In
    # the lighthouse plan that left the lamp room, the main setting of four of
    # six hops, carried by beat text alone.
    #
    # The test is ABANDONMENT, not gaps. A plan is free to leave a transitional
    # space unplated and come back: the pack's own Showcase plates the kitchen
    # on shots 1-3, walks her down an unplated hallway for 4-5, and returns the
    # plate on 6. What is always wrong is plating the opening and then letting
    # the plan END with no plate riding, because that is the destination -- the
    # place the film spends its last hops in -- with nothing holding it.
    #
    # A plan with no place plates at all is a legitimate shape and says nothing
    # here: the Starter ships that way so it runs before any pictures exist.
    #
    # Neither does a plan that never LEAVES. The warning above is about a
    # destination the film moves to and then holds with nothing; a chain that
    # spends every hop in one room has no destination to lose, and dropping the
    # plate after hop 1 there is the pin-only recipe the renderer is built for
    # -- `active_refs` plus the `next`-mode drop exist to take unscheduled
    # stills OFF a continue so "pin carries wardrobe and room", and
    # ref_rail.js:424 calls it by name. Firing on a single-location chain told
    # the author to undo the one thing the pack asks them to do, on every
    # 2-hop kitchen plan written today.
    #
    # Distinct places CITED IN BEATS is the test, not places in the register: a
    # plate nothing names is not a location the film goes to.
    cited = {t for t in places
             if any(("@" + t).lower() in (s.get("beat") or "").lower()
                    for s in shots)}
    if len(cited) < 2:
        return warnings

    covered = set()
    for r in (ref_plan.get("refs") or []):
        if r.get("subject"):
            continue
        covered |= set(r.get("shots") or range(1, len(shots) + 1))
    n = len(shots)
    if covered and n not in covered:
        first_bare = n
        while first_bare - 1 >= 1 and (first_bare - 1) not in covered:
            first_bare -= 1
        span = (f"shot {first_bare}" if first_bare == n
                else f"shots {first_bare}-{n}")
        warnings.append(
            f"{span}: the plan's place plates stop riding and never resume, so "
            f"the film ends somewhere no picture describes. A location "
            f"introduced part way through needs its own plate on the hop it "
            f"arrives and every hop after -- tightening a plate to its own "
            f"shots does not mean the next location goes without one."
        )
    return warnings



# ---------------------------------------------------------------------------
# Over-delivery: the defect class that survives every other check.
#
# A beat has to be true from ANY plausible ending of the previous hop, because
# the model decides where that hop actually lands. `tail=settle` and
# `tail=hold` are the two directives that promise the opposite of motion --
# settle brings the subject to rest, hold freezes the frame -- so a following
# beat that opens as though the action never stopped is asking hop N+1 to
# continue something hop N was told to end.
#
# This is what produced the one hard cut in the 8x15s chain (a 7.1-sigma jump
# at f1098) from a plan that passed coherence, place-handoff and every template
# check. Nothing structural distinguishes it: both shots are individually
# well-formed and the directives are individually legal. Only the JOIN between
# them is wrong, which is why it needs its own pass.
#
# Heuristic, and deliberately narrow. The vocabularies below are words that
# explicitly assert continuation; a beat can open mid-action without using any
# of them and this will miss it. It warns, like its neighbours -- there are
# legitimate reasons to write "she continues" after a settle, and a false
# positive that blocked a render would be worse than the defect.
# ---------------------------------------------------------------------------

# Phrases that assert the previous action is STILL RUNNING. Trailing spaces are
# load-bearing: "keeps " must not fire on "keepsake", "still " not on "stillness".
_MID_ACTION = (
    "continues", "continuing", "carries on", "carrying on",
    "keeps ", "keeping ", "still ", "goes on ", "going on ",
    "resumes", "resuming", "finishes", "finishing",
    "without stopping", "without pausing", "without breaking",
    "without looking up", "mid-sentence", "mid sentence",
    "mid-step", "mid-stride", "mid-turn", "mid-word", "mid-gesture",
)

# A beat OPENING on one of these reads as an action already in progress. Only
# checked at the very start of the beat, where a gerund is the subject of the
# sentence rather than a description of something else in the room -- which is
# why "morning", "evening" and "nothing" cannot trip it.
_MID_ACTION_LEAD = (
    "walking", "turning", "holding", "speaking", "talking", "moving",
    "reaching", "stepping", "running", "pouring", "writing", "carrying",
    "leaning", "pulling", "pushing", "climbing", "crossing", "gesturing",
    "nodding", "shaking", "waving", "pacing", "wiping", "stirring",
)

# How much of the beat counts as "the opening".
_OPEN_WINDOW = 110


def check_over_delivery(shots):
    """Warn when a beat opens mid-action after a hop told to come to rest.

    Returns a list of warning strings; never raises.
    """
    warnings = []
    for i in range(1, len(shots)):
        prev = shots[i - 1] or {}
        cur = shots[i] or {}
        tail = ((prev.get("directives") or {}).get("tail") or "").strip()
        if tail not in ("settle", "hold"):
            continue
        beat = (cur.get("beat") or "").strip()
        if not beat:
            continue
        head = beat[:_OPEN_WINDOW].lower()
        first = head.split()[0].strip(",.;:!?") if head.split() else ""

        hit = None
        if first in _MID_ACTION_LEAD:
            hit = f"opens on '{first}'"
        else:
            for phrase in _MID_ACTION:
                if phrase in head:
                    hit = f"opens with '{phrase.strip()}'"
                    break
        if not hit:
            continue

        warnings.append(
            f"shot {i + 1}: shot {i} ends on tail={tail}, which delivers a "
            f"subject at rest -- but shot {i + 1} {hit}, as if the action never "
            f"stopped. A beat has to be true from ANY ending the model picks "
            f"for the hop before it. Either set shot {i}'s tail to `ongoing`, "
            f"or rewrite this opening so it also reads from a standstill."
        )
    return warnings


def compile_blocks(shots, establish=None, ref_plan=None):
    """Compile a plan into one body string per hop, ready for the chain loop."""
    for w in check_coherence(shots):
        print(f"[{TAG}] note: {w}", flush=True)
    for w in check_place_handoff(shots, ref_plan):
        print(f"[{TAG}] note: {w}", flush=True)
    for w in check_over_delivery(shots):
        print(f"[{TAG}] note: {w}", flush=True)
    return [_d.compile_shot(s, i, establish) for i, s in enumerate(shots)]


def describe(shots):
    """One-line-per-shot summary for the console, so the plan is auditable."""
    rows = []
    for i, s in enumerate(shots):
        bits = [f"{k}={v}" for k, v in s["directives"].items()]
        extra = []
        if s["seed"] is not None:
            extra.append(f"seed={s['seed']}")
        if s["steps"] is not None:
            extra.append(f"steps={s['steps']}")
        if s["duration"]:
            extra.append(f"duration={s['duration']}")
        if s["locked"]:
            extra.append("locked")
        if s.get("tone"):
            extra.append(f"tone={s['tone']}")
        beat = (s["beat"] or "").replace(chr(10), " ")
        if len(beat) > 60:
            beat = beat[:57] + "..."
        rows.append(
            f"  shot {i + 1} [{s['id']}] {' '.join(bits + extra) or '(no directives)'}"
            + chr(10) + f"      beat: {beat or '(continues)'}"
        )
    return chr(10).join(rows)
