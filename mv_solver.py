"""Hop lengths for an HTC music video: cuts on the downbeat grid.

`solve(song_map, sections)` turns a tempo grid plus a list of sections into the
hop skeleton a plan is written from. It is the generalised form of the scratch
script that planned Headliner by hand, and it keeps that script's two decisions:

**A cut error is measured in frames, not seconds.** Every hop length is a
`17k+5` frame count, so a bar of 44.9 frames cannot be hit exactly and the best
possible error is whatever the length grid leaves. The solver minimises the
WORST cut error over a search of `master_audio_start_s`, because that offset
moves every cut at once and is the only free variable that can absorb a grid
mismatch. `master_audio_start_s` is quantised to 0.01 s before the errors are
computed, because that is the widget's `round`, so the number reported is the
one that renders.

**Fewer hops wins, and long hops win, but only after the cuts are placed.** The
objective is lexicographic: worst cut error first, then a penalty for short hops
and for each extra hop in a section. A solver that traded a cut for a shorter
hop would produce a tidy plan that cuts on the wrong beat.

A section is `R`, `R C` or `R C C`. `R` is a restart (a hard cut that re-anchors
on the reference plates), `C` a continuation pinned to the previous hop's last
`overlap` frames, so it adds `duration - overlap` frames to the film. There is no
continuation at the 5 s length: 124 frames is under the airlock budget.

No ComfyUI imports, for the same reason `plan.py`, `refs.py`, `music.py` and
`songmap.py` have none: `tools/check_songmap.py` runs this on the CPU with no
server. `DURATION_FRAMES` and the frame rate are arguments with the node's
values as defaults, read here rather than imported so the checker needs no
package.
"""

from __future__ import annotations

# HandTieClips' own numbers, duplicated rather than imported: h3_ref_chain pulls
# in ComfyUI, and this module has to stay importable without a server. Kept in
# step by tools/check_songmap.py, which reads the real dict and compares.
DURATION_FRAMES = {
    "5 s": 124, "6 s": 141, "7 s": 175, "8 s": 192, "9 s": 209,
    "10 s": 243, "11 s": 260, "12 s": 294, "13 s": 311, "14 s": 328,
    "15 s": 362, "16 s": 379, "17 s": 413, "18 s": 430, "19 s": 464,
    "20 s": 481, "21 s": 498, "22 s": 532, "23 s": 549, "24 s": 583,
    "25 s": 600, "26 s": 617, "27 s": 651, "28 s": 668, "29 s": 702,
    "30 s": 719,
}

FPS = 24
OVERLAP_FRAMES = 22
# `master_audio_start_s` in h3_ref_chain declares step 0.01 AND round 0.01, so a
# stored 4.03 is quantised to 4.03 but a stored 0.4167 lands on 0.42. The solver
# searches frame offsets and then reports the value the widget will actually
# hold, because a cut error computed against an offset the widget cannot store is
# a cut error that is not what renders.
OFFSET_QUANTUM_S = 0.01
OFFSET_SEARCH_F = 16           # the search band, in frames, at 24 fps

# A continuation shorter than this drops the airlock (CLAUDE.md: 5 s is not a
# join-validation canvas, and a 22-frame overlap on a 124-frame hop is 18%).
MIN_CONTINUATION = "6 s"
SHORT_HOP_AT = 175             # under this, a hop is "short" and costs penalty
EXTRA_HOP_PENALTY = 40

# An MV hop is 5-10 s, and that range is a decision rather than a subset: the
# node has labels out to 30 s, and letting the solver reach for a 15 s hop makes
# it place four cuts in a 60 s verse instead of nine. Four cuts is a music video
# with three shots. Headliner's solved plan is 17 hops over 119 s, which is a 7 s
# average, and that is what the cut count has to be free to reach.
DEFAULT_PATTERN = ("R", "RC", "RCC")

MV_DURATION_LABELS = ("5 s", "6 s", "7 s", "8 s", "9 s", "10 s")


def _quantise(offset_s, quantum=OFFSET_QUANTUM_S):
    """The value the node's widget would actually hold.

    `master_audio_start_s` is a FLOAT widget with step 0.01, so an offset the
    solver liked at 0.4237 s is not an offset the graph can run -- the node snaps
    it to 0.42 and every cut error is recomputed against the snapped value. The
    search therefore quantises BEFORE measuring, not after choosing.
    """
    return round(float(offset_s) / quantum) * quantum


def _lengths(frames=DURATION_FRAMES, labels=None):
    """[(label, n)] sorted by n, shortest first. The sorted order is what makes
    the dynamic programme below deterministic."""
    if labels:
        frames = {k: v for k, v in frames.items() if k in set(labels)}
    return sorted(((k, int(v)) for k, v in frames.items()), key=lambda kv: kv[1])


def _hop_options(lengths, overlap, pattern, min_continuation):
    """Every (kinds, labels, frames, penalty) a section may take.

    `pattern` is the list of kind-strings to try per section, e.g.
    ("R", "RC", "RCC"). For each, every duration combination is enumerated --
    216 for the longest pattern, a few hundred entries overall.

    The penalty is computed HERE, once per option, rather than inside the search
    where it would be recomputed once per (state, option) pair. That is the
    difference between a solve that takes a second and one that takes minutes,
    and it changes nothing about the answer.
    """
    import itertools  # noqa: PLC0415

    order = [n for _lab, n in lengths]
    labs = [lab for lab, _n in lengths]
    min_c = dict(lengths).get(min_continuation, 0)
    out = []
    for kinds in pattern:
        for combo in itertools.product(order, repeat=len(kinds)):
            if any(k == "C" and v < min_c for v, k in zip(combo, kinds)):
                continue
            total = sum(v if k == "R" else v - int(overlap)
                        for v, k in zip(combo, kinds))
            names = [labs[order.index(v)] for v in combo]
            pen = (sum(max(0, SHORT_HOP_AT - dict(zip(labs, order))[nm])
                        for nm in names)
                   + EXTRA_HOP_PENALTY * (len(kinds) - 1))
            out.append((kinds, names, total, int(pen)))
    # shortest first, so a tie in the objective prefers the tidy skeleton
    out.sort(key=lambda t: (t[2], len(t[0])))
    return out


def bar_time_s(song_map, bar):
    """Bar `bar` (1-based) in seconds from the file's beginning."""
    return (float(song_map["first_downbeat_s"])
            + (int(bar) - 1) * float(song_map["bar_s"]))


def _section_bars(section):
    """The bars a cut may land on for one section.

    A section is `{"bar": n}` for a structural cut, or `{"bar": n, "flex": [a, b,
    c]}` for one that may slide. `flex` wins when present: the point of listing
    a range is that the section boundary is not certain, and a solver that ignores
    the range will confidently land on the wrong bar.
    """
    flex = section.get("flex") or section.get("bars")
    if flex:
        return [int(b) for b in flex]
    return [int(section.get("bar", 1))]


def solve(song_map, sections, *, durations=DURATION_FRAMES, overlap=OVERLAP_FRAMES,
          fps=FPS, pattern=None, end_s=None, offset_search_f=OFFSET_SEARCH_F,
          quantise=True, last_event_s=None, tol_min=2, tol_max=32,
          labels=None):
    """Place the cuts. -> dict

    Keys: hops, master_audio_start_s, offset_f, worst_cut_f, cuts, total_frames,
    end_f, total_s, sections, pattern.
    """
    lengths = _lengths(durations, labels or MV_DURATION_LABELS)
    ov = int(overlap)
    by_label = dict(lengths)
    pattern = tuple(pattern or DEFAULT_PATTERN)
    dur_s = float(song_map.get("dur") or (end_s if end_s is not None else 0.0))
    if end_s is not None:
        dur_s = min(dur_s, float(end_s)) if dur_s else float(end_s)
    file_f = int(dur_s * float(fps))
    opts = _hop_options(lengths, ov, pattern, MIN_CONTINUATION)
    if not opts:
        raise ValueError(f"mv_solver: no hop options for pattern {pattern!r}")

    # Where the film is allowed to finish: at the last audible moment of the
    # music, and not a moment before it.
    #
    # The floor used to be the last bar line minus one bar, which is a
    # principled guess and the wrong answer: on Headliner it put the film 51
    # frames (2.1 s) short of where the track stops sounding, and a film that
    # ends before the music does is a video with the last verse cut off. The map
    # now carries `ends_on_sound` from an RMS envelope, so that is the floor.
    #
    # The ceiling is the same moment: the film must not outlive the music either,
    # or the last hop plays over silence. Both ends are the same instant, which
    # makes the window as narrow as one frame, so the search is given a little
    # slack at each end -- see WINDOW_SLACK_F.
    #
    # `last_event_s` still overrides it, for a caller that knows better than
    # either (a track with a long outro fade, say).
    WINDOW_SLACK_F = 24.0
    WINDOW_STEP_F = 24.0
    WINDOW_MAX_F = 288.0          # 12 s; past this the film is not ending with the song
    bars = song_map.get("downbeats") or []
    end_of_sound = song_map.get("ends_on_sound")
    if last_event_s is not None:
        target = float(last_event_s)
    elif end_of_sound is not None:
        target = float(end_of_sound)
    elif bars:
        target = float(bars[-1]) + float(song_map.get("bar_s") or 0.0)
    else:
        target = float(dur_s)
    target_f = int(target * float(fps))
    totals = sorted({int(o[2]) for o in opts})

    sec_bars = [_section_bars(s) for s in sections]
    if not sec_bars:
        raise ValueError("mv_solver: no sections to cut on")

    # Sections arrive 8 bars apart, which is ~15 s -- and the longest hop is 10 s.
    # A boundary the solver cannot reach is a boundary it crosses badly: measured
    # worst-cut errors went from 7.9 frames to 16.8 on Headliner purely because the
    # section list was too sparse to place cuts on.
    #
    # So the gaps are subdivided first, on the bar grid, until every one is within
    # reach. The extra points inherit their parent's kind, because they are inside
    # that section: a cut inside a chorus is still a chorus. This adds no new
    # musical claims, only places to put a cut that was going to be forced through
    # a bar line anyway.
    raw_bars = [_section_bars(s) for s in sections]
    dens = _densify(raw_bars, song_map, opts, ov, fps)
    if len(dens) != len(raw_bars):
        # `_densify` returned more pieces than sections, because some gap was too
        # wide for a single hop to cross and had to be split. Each piece is a hop
        # slot and each slot needs the section that describes it, so the section
        # list is expanded to match by bar position. Without this the two lists
        # desynchronise and `zip` below silently drops the film's tail.
        pi = 0
        grown = []
        for bars in dens:
            while pi + 1 < len(raw_bars) and raw_bars[pi + 1][0] <= bars[0]:
                pi += 1
            grown.append(sections[min(pi, len(sections) - 1)])
        sections = grown
    sec_bars = dens
    max_gap = _max_gap_s(sec_bars, song_map, fps)
    if max_gap is not None and max_gap > max(4.0 * float(fps),
                                             float(max(o[2] for o in opts))):
        raise ValueError(
            "mv_solver: sections are %.1f s apart and no hop is long enough to "
            "reach between them." % max_gap)

    # The window has to be wide enough for a hop to land in it, and hop totals are
    # ~17 f apart near the top of the range, so a window narrower than that gap can
    # contain no reachable film end AT ALL. Headliner at +/-1 s found none: its
    # last section needed a hop of 146-170 f and the nearest totals are 141 and
    # 175.
    #
    # So the window is widened until a section can be trimmed to one that a hop can
    # finish in, and the slack that was needed is REPORTED. The first, narrowest
    # window that works is used, so a track whose sections happen to line up gets
    # the exact end and only an awkward one pays the extra.
    slack = int(WINDOW_SLACK_F)
    kept = None
    lo_end_f = hi_end_f = target_f
    while True:
        lo_end_f = max(1, target_f - slack)
        hi_end_f = min(file_f, target_f + slack)
        hi_end_f = max(lo_end_f, hi_end_f)
        kept = _trim_to_end(sections, sec_bars, song_map, lo_end_f, hi_end_f,
                            totals, fps)
        if kept is not None or slack >= int(WINDOW_MAX_F):
            break
        slack += int(WINDOW_STEP_F)
    if kept is None:
        raise ValueError(
            "mv_solver: no arrangement of hops reaches the end of the song within "
            "%.1f s of where it stops sounding. Check the tempo grid, or shorten "
            "the song." % (WINDOW_MAX_F / float(fps)))
    sections = [s for s, _b in kept]
    sec_bars = [b for _s, b in kept]
    end_slack_f = int(round(max(0.0, target_f - hi_end_f, lo_end_f - target_f)))

    # A section that OPENS after the film is allowed to end can never be reached:
    # the film would have to run past its own last frame to get there. Trimming the
    # tail here rather than in the caller is deliberate -- a caller that lists one
    # section per 4 bars up to the end of the file always overshoots, because the
    # film stops inside the last bar, not after it. The section that survives as
    # the last one is then the section the film ends inside.
    #
    # There is a second condition, and it is the one that bit `lora_daddy_cut403`
    # (52 s, 176.5 BPM, sections every 4 bars): the last section must be able to
    # ADD a hop and still land in the end window. Its section started 32 frames
    # before the window, so it needed a hop of 32-79 frames and the shortest hop
    # is 124 -- no plan exists, and not because the grid is wrong.
    #
    # The test is exact rather than a heuristic: the reachable hop totals are
    # known, so a section is dropped only while no total fits the window it would
    # have to fill. Trimming stops at one section, so a one-section song still
    # reports itself unsolved instead of silently losing its only section.


    best_all = None
    for off_f in range(0, int(offset_search_f) + 1):
        off_s = _quantise(off_f / float(fps)) if quantise else off_f / float(fps)
        off_real_f = off_s * float(fps)
        got = None
        for tol in range(int(tol_min), int(tol_max) + 1):
            got = _search(song_map, sec_bars, opts, ov, fps, off_real_f,
                          lo_end_f - off_real_f, hi_end_f - off_real_f, tol)
            if got is not None:
                break
        if got is None:
            continue
        worst, pen, path = got
        key = (round(worst, 3), pen)
        if best_all is None or key < (round(best_all[1][0], 3), best_all[1][1]):
            best_all = (off_s, got, off_real_f)
        # the tolerance sweep already found this offset's optimum, so if the
        # error cannot beat what we have there is no reason to look further out
        if worst <= best_all[1][0] and best_all[1][0] <= float(tol_min):
            break
    if best_all is None:
        raise ValueError(
            "mv_solver: no arrangement of hops reaches the end of the song on a "
            "downbeat. Widen the sections, allow a longer pattern, or check the "
            "tempo grid.")
    off_s, (worst, pen, path), off_real_f = best_all

    hops, cuts, pos = [], [], 0
    for i, ((kinds, labs), sec) in enumerate(zip(path, sections)):
        start = pos
        # `cuts[i]` is the cut that OPENS this section, so it is measured against
        # this section's own bars. The cut that closes it is `cuts[i+1]`. Keeping
        # that the way round is what lets a reported error be read off as "this
        # picture starts N frames from a bar line" rather than "the picture before
        # it ended N frames from one".
        cuts.append({"section": i, "bar": sec.get("bar"), "film_f": start,
                     "err_f": None if i == 0 else _cut_err(
                         start, sec_bars[i], song_map, off_real_f, fps)})
        for k, lab in zip(kinds, labs):
            n = by_label[lab]
            hop = {"id": "%02d_%s" % (len(hops) + 1, lab.replace(" ", "")),
                   "kind": k, "duration": lab, "frames": int(n),
                   "section": i}
            hop.update(_times(hop, start, ov, off_real_f, fps))
            hop["section_bar"] = sec.get("bar")
            hop["section_kind"] = sec.get("kind")
            hops.append(hop)
            start += n if k == "R" else n - ov
        pos = start
    # the film's own end is a cut too, and the reference measures it
    cuts.append({"section": len(sections), "bar": None, "film_f": pos,
                 "err_f": None, "end": True})
    end_tol = None
    if song_map.get("downbeats"):
        end_tol = min(abs(pos - _bar_f(b, song_map, off_real_f, fps))
                      for b in song_map["downbeats"])

    out = {
        "hops": hops,
        "master_audio_start_s": round(float(off_s), 2),
        "offset_f": round(off_real_f, 3),
        "worst_cut_f": round(float(worst), 2),
        "worst_cut_incl_end_f": round(float(max(worst, end_tol or 0.0)), 2),
        "cuts": cuts,
        "total_frames": int(pos),
        "end_f": int(pos),
        "end_s": round(pos / float(fps), 3),
        "overlap": ov,
        "pattern": list(pattern),
        "n_sections": len(sections),
        "end_target_s": round(float(target), 3),
        # how far the film's LAST SONG MOMENT sits from the music's last sound,
        # in song frames. NOT `total_frames - target_f`: the film's own length and
        # the song's end are different quantities once `master_audio_start_s` is
        # non-zero, because the film is the song shifted by the offset. Comparing
        # them directly reads 17 frames of error on a solution that is exact.
        "end_slack_f": round(float(max(0.0, abs(pos + off_real_f - target_f))), 2),
        "labels": [l for l, _n in lengths],
    }
    out["per_cut_err_f"] = [c["err_f"] for c in cuts if not c.get("end")]
    # the film's head against bar 1: not a cut, but the audio lead-in, so it is
    # reported rather than folded into the cut error
    out["head_offset_f"] = round(_bar_f(sec_bars[0][0], song_map,
                                        off_real_f, fps), 2)
    _attach_singing(out, song_map, fps)
    return out


def _densify(sec_bars, song_map, opts, ov, fps):
    """Split section gaps that no hop can cross. -> [bars] per section

    A section boundary is a PLACE A CUT MAY LAND, not a claim about the music. When
    two boundaries are further apart than the longest hop, the solver has to put a
    cut somewhere that is not a boundary, and it does so as badly as the arithmetic
    allows -- 16.8 frames of error against 7.9 on Headliner, from nothing but a
    sparse section list.

    Subdividing fixes that without inventing structure: the inserted bars are
    ordinary downbeats between two known boundaries, and they carry no kind of
    their own. `sec_bars` is a list of LISTS, so inserting into a section's own
    `flex` list gives the search more bars to place a cut on without adding a
    section boundary, which is the more conservative of the two options.

    BUT `_search` spends exactly ONE hop per section (see its loop), so filling one
    section's flex list does not shorten the span it has to cross -- it only gives
    the cut a finer choice of bar. A gap wider than the longest hop therefore still
    has no solution, and `_max_gap_s` reports the *filled* gap and so no longer
    sees it either. Measured: `sandpies_master.wav` bars 17->41 are 1082.9 f
    against a 685 f reach, `Track 2 master test.mp3` bars 33->73 are 1645.6 f,
    and all three tracks raised "no arrangement of hops reaches the end of the song
    on a downbeat" from deep inside the search, which names the wrong cause.

    So a gap too wide to cross is SPLIT into several sections, which is what the
    search actually consumes: one hop per section means more sections means more
    hops. Each piece gets a short flex window of real downbeats, so the cut still
    lands on a bar line and the invented boundaries stay ordinary bars. The kind
    is not carried here -- `solve` re-attaches each piece to its parent's section.
    """
    reach = float(max((o[2] for o in opts), default=0))
    if reach <= 0.0:
        return [list(b) for b in sec_bars]
    all_bars = song_map.get("downbeats") or []
    out = [list(b) for b in sec_bars]
    for i in range(len(out) - 1):
        a_bar = out[i][-1]
        b_bar = out[i + 1][0]
        if b_bar - a_bar <= 1:
            continue
        # every bar strictly between these two, in frame terms
        gap_f = (_bar_f(b_bar, song_map, 0.0, fps)
                 - _bar_f(a_bar, song_map, 0.0, fps))
        if gap_f <= reach:
            continue
        inner = [k for k in range(int(a_bar) + 1, int(b_bar))
                 if not all_bars or k <= len(all_bars)]
        if not inner:
            continue
        out[i].extend(inner)
        # Still too wide to cross in one hop: split the section itself, into as
        # many pieces as the reach needs, each ending on a real downbeat.
        pieces = _split_wide(out[i], song_map, reach, fps)
        if len(pieces) > 1:
            out[i:i + 1] = pieces
    return out


def _split_wide(bars, song_map, reach, fps, slack=0.85):
    """Cut `bars` into consecutive chunks, each spanning no more than `reach`.

    `slack` leaves headroom against the longest hop: a span of exactly `reach`
    would need the single longest available hop, which the shorter-hop preference
    inside `_search` would then have to fight for. Only bars already in the list
    are used, so a piece always ends on a boundary the caller supplied or that
    `_densify` inserted above.
    """
    if not bars:
        return [list(bars)]
    budget = max(1.0, reach * float(slack))
    pieces = [[bars[0]]]
    for b in bars[1:]:
        span = (_bar_f(b, song_map, 0.0, fps)
                - _bar_f(pieces[-1][0], song_map, 0.0, fps))
        if span > budget:
            pieces.append([b])
        else:
            pieces[-1].append(b)
    return pieces


def _max_gap_s(sec_bars, song_map, fps):
    """The largest film-frame gap between consecutive cut points, or None."""
    pts = []
    for bars in sec_bars:
        for b in bars:
            pts.append(_bar_f(b, song_map, 0.0, fps))
    pts = sorted(set(pts))
    if len(pts) < 2:
        return None
    return float(max(b - a for a, b in zip(pts, pts[1:])))


def _trim_to_end(sections, sec_bars, song_map, lo_end_f, hi_end_f, totals, fps):
    """Drop trailing sections until the last one can be finished by a hop.

    -> [(section, bars)] or None when no tail survives.

    Two conditions, both needed:

    - a section that OPENS after the film is allowed to end can never be reached,
      because the film would have to run past its own last frame to get there;
    - the last section must be able to ADD a hop and still land inside the window.

    The second is what bit `lora_daddy_cut403` (52 s, 176.5 BPM): its final section
    started 32 frames before the window, so it needed a hop of 32-79 frames and the
    shortest hop is 124. No plan existed, and not because the grid was wrong.

    The test is exact rather than a heuristic -- the reachable hop totals are known,
    so a section is dropped only while none of them fits the window it would have
    to fill. Trimming stops at one section, so a one-section song reports itself
    unsolved instead of silently losing its only section.
    """
    kept = list(zip(sections, sec_bars))
    while len(kept) > 1:
        start_last = _bar_f(kept[-1][1][0], song_map, 0.0, fps)
        if start_last > lo_end_f:
            kept.pop()
            continue
        need0 = int(lo_end_f - start_last)
        need1 = int(hi_end_f - start_last)
        if any(need0 <= t <= need1 for t in totals):
            return kept
        kept.pop()
    if len(kept) == 1:
        start_last = _bar_f(kept[0][1][0], song_map, 0.0, fps)
        need0, need1 = int(lo_end_f - start_last), int(hi_end_f - start_last)
        if need0 <= 0 and any(t <= need1 for t in totals):
            return kept
    return None


def _search(song_map, sec_bars, opts, ov, fps, off_f, lo_end_f, hi_end_f, tol):
    """The dynamic programme at one cut-error tolerance. -> (worst, pen, path) or None

    State is (film position, bar this section starts on). Keeping the bar in the
    key rather than the section index is what lets two ways of reaching the same
    film position collapse: the cheaper penalty survives.

    `tol` is what makes this fast, and dropping it is not a simplification. A
    transition whose next cut is worse than `tol` can never be part of a solution
    whose WORST cut is within `tol`, so it is discarded on the spot; and because
    `tol` is swept upward from a small value, the first tolerance that admits any
    solution is the optimum. Without it the search fans out over every reachable
    film position and takes minutes rather than under a second.
    """
    targets = [[_bar_f(b, song_map, off_f, fps) for b in bars]
               for bars in sec_bars]
    states = {(0, sec_bars[0][0]): (0.0, 0, [])}
    n = len(sec_bars)
    for i in range(n):
        last = i + 1 == n
        tgt = None if last else targets[i + 1]
        key_bar = sec_bars[i + 1][0] if not last else 0
        new = {}
        for (pos, _bar), (err, pen, path) in states.items():
            for kinds, labs, total, op_pen in opts:
                p = pos + total
                if last:
                    if not (lo_end_f <= p <= hi_end_f):
                        continue
                    e = err
                else:
                    e = min(abs(p - t) for t in tgt)
                    if e > tol:
                        continue
                    if e < err:
                        e = err
                q2 = pen + op_pen
                key = (p, key_bar)
                cand = (e, q2, path + [(kinds, labs)])
                old = new.get(key)
                if old is None or (old[1], old[0]) > (q2, e):
                    new[key] = cand
        states = new
        if not states:
            return None
    best = min(states.values(), key=lambda v: (v[1], v[0]))
    return (best[0], best[1], best[2])


def _bar_f(bar, song_map, off_f, fps):
    """Where bar `bar` falls on the FILM's frame line.

    Read out of the map's own `downbeats` list when it is there, because that list
    is not necessarily an even grid: a track whose tempo drifts is given TRACKED
    downbeats, and arithmetic from `first_downbeat_s + (bar-1)*bar_s` then places
    every cut in the second half of the song progressively early -- by 16 frames on
    Headliner, which is two hops' worth of error in a search that measures cuts to
    a third of a frame.
    """
    bars = song_map.get("downbeats") or []
    k = int(bar) - 1
    if 0 <= k < len(bars):
        return float(bars[k]) * float(fps) - float(off_f)
    return (float(song_map["first_downbeat_s"]) * float(fps)
            + k * float(song_map["bar_s"]) * float(fps) - float(off_f))


def _cut_err(pos_f, bars, song_map, off_f, fps):
    if not bars:
        return None
    return round(min(abs(pos_f - _bar_f(b, song_map, off_f, fps)) for b in bars), 2)


def _times(hop, pos_f, ov, off_f, fps):
    """A hop's film span and the song time it plays.

    The song time is the film's own frame count plus the master offset, because
    `master_audio_start_s` moves the take, not the film: film frame `a` is song
    time `(a + offset)/fps`. A continuation's delivered span is `n - overlap`,
    because the overlap frames are the previous hop's and are thrown away.
    """
    n = int(hop["frames"])
    a = int(pos_f)
    b = int(pos_f + (n if hop["kind"] == "R" else n - ov))
    hop["film_a"] = a
    hop["film_b"] = b
    hop["song_a"] = round((a + off_f) / float(fps), 3)
    hop["song_b"] = round((b + off_f) / float(fps), 3)
    return hop


def _attach_singing(out, song_map, fps):
    """Per-hop sung flag and the lyric lines that fall inside it.

    Overlap, not containment: a line that starts in one hop and is still being
    sung when the next cut lands is the strongest thing an MV can have at a
    seam, and dropping it would make the plan avoid exactly the moments worth
    cutting on. Only possible when the map came from the listening path.
    """
    sung = song_map.get("sung")
    lines = song_map.get("lines") or []
    for hop in out["hops"]:
        a, b = hop["song_a"], hop["song_b"]
        if sung:
            hit = [k for k in range(len(song_map["downbeats"]) - 1)
                   if _bar_overlaps(song_map, k, a, b)]
            hop["sung"] = bool(hit) and any(
                song_map["sung"][k] >= 0.35 for k in hit)
            hop["sung_bars"] = [k + 1 for k in hit]
        else:
            hop["sung"] = None
            hop["sung_bars"] = None
        hop["lyric"] = [ln["text"] for ln in lines
                        if ln.get("b", 0.0) > a and ln.get("a", 0.0) < b]


def _bar_overlaps(song_map, k, a, b):
    bars = song_map["downbeats"]
    t0 = float(bars[k])
    t1 = float(bars[k + 1]) if k + 1 < len(bars) else t0 + float(song_map["bar_s"])
    return t1 > a and t0 < b


def solve_from_song_map(path, sections, **kw):
    """Convenience: build the map (tempo only) and solve it in one call."""
    from . import songmap as _sm  # noqa: PLC0415

    return solve(_sm.song_map(path), sections, **kw)