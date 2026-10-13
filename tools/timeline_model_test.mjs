// The timeline forecast's rules, under Node. Run by tools/check_timeline.py.
import { contentSig, forecastPlan, shotSig, framesOf } from "../js/editor/timeline_model.js";

let failed = 0;
const ck = (name, ok, detail = "") => {
    console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
    if (!ok) failed += 1;
};

const shot = (id, beat, extra = {}) => ({ id, beat, seed: 1, ...extra });
const snapOf = (shots, extra = {}) => ({
    ids: shots.map((s) => s.id),
    shots: Object.fromEntries(shots.map((s) => [s.id, shotSig(s)])),
    refs: {}, subjects: "{}", widgets: { steps: 8, overlap: "22 f" }, seed: 5,
    ...extra,
});
const recOf = (shots, snap) => ({
    snapshot: snap,
    hops: shots.map((s, i) => ({ key: `k${i + 1}`, status: "rendered" })),
});

const base = [shot("s1", "a"), shot("s2", "b"), shot("s3", "c"), shot("s4", "d")];
const rec = recOf(base, snapOf(base));
const now = snapOf(base);

let fc = forecastPlan({ shots: base, rec, now });
ck("nothing edited, nothing re-renders", fc.state.every((s) => s === "keeps"), fc.state.join());

const edited = base.map((s) => ({ ...s }));
edited[1].beat = "b2";
fc = forecastPlan({ shots: edited, rec, now: snapOf(edited) });
ck("editing shot 2 re-renders 2..end",
   fc.state.join() === "keeps,render,render,render", fc.state.join());
ck("the cause is named", fc.cause[1] === "shot 2 edited" && fc.cause[3] === "shot 2 edited",
   String(fc.cause));

const restart = edited.map((s) => ({ ...s }));
restart[2].anchor = "restart";
const recR = recOf(restart, snapOf(restart.map((s, i) => (i === 1 ? { ...s, beat: "b" } : s))));
fc = forecastPlan({ shots: restart, rec: recR, now: snapOf(restart) });
ck("a change stops before a restart",
   fc.state.join() === "keeps,render,keeps,keeps", fc.state.join());

const locked = edited.map((s) => ({ ...s }));
locked[2].locked = true;
fc = forecastPlan({ shots: locked, rec, now: snapOf(locked) });
ck("a locked hop is served, and the chain stops at it",
   fc.state.join() === "keeps,render,locked,keeps", fc.state.join());
ck("and its join with the re-rendered hop is flagged unpaired",
   fc.unpaired.join() === "2", fc.unpaired.join());
ck("toggling lock alone moves nothing",
   forecastPlan({ shots: base.map((s, i) => (i === 2 ? { ...s, locked: true } : s)),
                  rec, now }).state.every((s) => s !== "render"));

fc = forecastPlan({ shots: locked, rec, now: snapOf(locked), missing: new Set(["k3"]) });
ck("a locked hop gone from the cache renders once",
   fc.state[2] === "render" && fc.unpaired.length === 0, fc.state.join());

fc = forecastPlan({ shots: base, rec, now: snapOf(base, { widgets: { steps: 10, overlap: "22 f" } }) });
ck("a chain-wide setting re-renders everything",
   fc.state.every((s) => s === "render") && fc.cause[0] === "steps changed", fc.state.join());

fc = forecastPlan({ shots: base, rec, now: snapOf(base, { widgets: { steps: 8, overlap: "39 f" } }) });
ck("overlap leaves hop 1 alone",
   fc.state.join() === "keeps,render,render,render", fc.state.join());

const unseeded = base.map((s) => ({ ...s, seed: null }));
const recU = recOf(unseeded, snapOf(unseeded));
fc = forecastPlan({ shots: unseeded, rec: recU, now: snapOf(unseeded), seedCtl: "randomize" });
ck("a seed set to change each queue re-renders hops without their own seed",
   fc.state.every((s) => s === "render"), fc.state.join());
fc = forecastPlan({ shots: base, rec, now, seedCtl: "randomize" });
ck("hops with their own seed ignore it", fc.state.every((s) => s === "keeps"), fc.state.join());

const refSnap = snapOf(base, { refs: { kitchen: JSON.stringify({ tag: "kitchen", file: "a.png", shots: [3] }) } });
const recRef = recOf(base, refSnap);
fc = forecastPlan({
    shots: base, rec: recRef,
    now: snapOf(base, { refs: { kitchen: JSON.stringify({ tag: "kitchen", file: "b.png", shots: [3] }) } }),
    refsNow: [{ tag: "kitchen", file: "b.png", shots: [3] }],
});
ck("a changed reference re-renders from the first hop it rides",
   fc.state.join() === "keeps,keeps,render,render" && fc.cause[2] === "@kitchen changed",
   fc.state.join());

fc = forecastPlan({ shots: edited, rec, now: snapOf(edited), range: { from: 2, through: 2 } });
ck("a solo renders its hop and skips the rest after it",
   fc.state.join() === "replay,render,skip,skip", fc.state.join());
fc = forecastPlan({ shots: edited, rec, now: snapOf(edited), range: { from: 3, through: 0 } });
ck("render_from past an edited hop needs a replay the cache will not match",
   fc.state[1] === "replay-miss", fc.state.join());

fc = forecastPlan({ shots: base, rec: null, now });
ck("no run recorded: everything renders", fc.state.every((s) => s === "render"));

const extended = [...base, shot("s5", "e", { directives: { join: "continuous" } })];
fc = forecastPlan({ shots: extended, rec, now: snapOf(extended) });
ck("extend renders only the new shot",
   fc.state.join() === "keeps,keeps,keeps,keeps,render" && fc.cause[4] === "not in the last run",
   fc.state.join());
const unseededExt = [...unseeded, { id: "s5", beat: "e", seed: null }];
fc = forecastPlan({ shots: unseededExt, rec: recU, now: snapOf(unseededExt), seedCtl: "increment" });
ck("extend under a moving seed re-renders every unseeded shot",
   fc.state.every((st) => st === "render"), fc.state.join());

ck("the old section tie is gone from the model (no `tie` input, no tieSections)",
   !/tie\s*=/.test(String(forecastPlan)) && !String(forecastPlan).includes("tieSections"));

ck("framesOf matches DURATION_FRAMES (8 s 192, 5 s 124, 15 s 362, 30 s 719)",
   [framesOf("8 s"), framesOf("5 s"), framesOf("15 s"), framesOf("30 s")].join() === "192,124,362,719");
// "+ extend" after a render: only the new shot renders.
const extd = [...base, shot("s5", "e", { anchor: "extend" })];
fc = forecastPlan({ shots: extd, rec, now: snapOf(extd) });
ck("+ extend (anchor=extend) renders only the new shot",
   fc.state.join() === "keeps,keeps,keeps,keeps,render", fc.state.join());
// A shot's own seed retakes that hop, and the chain runs on from it.
const reseed = base.map((s, i) => (i === 2 ? { ...s, seed: 99 } : s));
fc = forecastPlan({ shots: reseed, rec, now: snapOf(reseed) });
ck("a new seed on shot 3 re-renders shots 3-4 and keeps 1-2",
   fc.state.join() === "keeps,keeps,render,render", fc.state.join());

// hand_tie: a continuation's window cites the previous window's still, a
// restart's cites still 1, and a shot seed does not move a window.
const htSnap = (shots, extra = {}) => snapOf(shots, {
    widgets: { steps: 8, overlap: "22 f", pin_mech: "hand_tie" },
    content: Object.fromEntries(shots.map((s) => [s.id, contentSig(s)])),
    ...extra,
});
const htRec = (shots) => recOf(shots, htSnap(shots));
const beat = (shots, index, text) => shots.map((s, i) => (i === index ? { ...s, beat: text } : s));

const htEdit2 = beat(base, 1, "b2");
fc = forecastPlan({ shots: htEdit2, rec: htRec(base), now: htSnap(htEdit2) });
ck("hand_tie: editing shot 2 re-renders 2 onward (the windows chain) and keeps 1",
   fc.state.join() === "keeps,render,render,render"
   && fc.cause[1] === "shot 2 edited" && fc.cause[3] === "shot 2 edited" && fc.cause[0] == null,
   fc.state.join() + " " + String(fc.cause));

const htEdit1 = beat(base, 0, "a2");
fc = forecastPlan({ shots: htEdit1, rec: htRec(base), now: htSnap(htEdit1) });
ck("hand_tie: editing shot 1 re-renders the film",
   fc.state.join() === "render,render,render,render" && fc.cause[3] === "shot 1 edited",
   fc.state.join());

const htRestart = base.map((s, i) => (i === 2 ? { ...s, anchor: "restart" } : s));
fc = forecastPlan({
    shots: beat(htRestart, 0, "a2"), rec: htRec(htRestart), now: htSnap(beat(htRestart, 0, "a2")),
});
ck("hand_tie: editing shot 1 runs through a restart",
   fc.state.join() === "render,render,render,render", fc.state.join());
fc = forecastPlan({
    shots: beat(htRestart, 1, "b2"), rec: htRec(htRestart), now: htSnap(beat(htRestart, 1, "b2")),
});
ck("hand_tie: a later edit does not cross a restart",
   fc.state.join() === "keeps,render,keeps,keeps", fc.state.join());

const htSeed = base.map((s, i) => (i === 2 ? { ...s, seed: 99 } : s));
fc = forecastPlan({ shots: htSeed, rec: htRec(base), now: htSnap(htSeed) });
ck("hand_tie: a shot seed retakes that hop only",
   fc.state.join() === "keeps,keeps,render,keeps", fc.state.join());

fc = forecastPlan({ shots: base, rec: htRec(base), now: htSnap(base, { seed: 6 }) });
ck("hand_tie: a run-seed change re-renders every hop",
   fc.state.every((s) => s === "render"), fc.state.join());
fc = forecastPlan({ shots: base, rec: htRec(base), now: htSnap(base), seedCtl: "randomize" });
ck("hand_tie: a moving run seed re-renders hops that have their own seed",
   fc.state.every((s) => s === "render"), fc.state.join());

const htLockedBeat = htEdit2.map((s, i) => (i === 2 ? { ...s, locked: true } : s));
fc = forecastPlan({ shots: htLockedBeat, rec: htRec(base), now: htSnap(htLockedBeat) });
ck("hand_tie: a locked hop after a beat edit is unpaired on both joins; the hop after renders",
   fc.state.join() === "keeps,render,locked,render" && fc.unpaired.join() === "2,3",
   fc.state.join() + " unpaired " + fc.unpaired.join());
const htLockedRestart = htLockedBeat.map((s, i) => (i === 3 ? { ...s, anchor: "restart" } : s));
fc = forecastPlan({ shots: htLockedRestart, rec: htRec(htLockedRestart.map((s) => ({ ...s, beat: s.id === "s2" ? base[1].beat : s.beat }))),
                    now: htSnap(htLockedRestart) });
ck("hand_tie: a restart after the locked hop is not unpaired, and keeps",
   fc.state.join() === "keeps,render,locked,keeps" && fc.unpaired.join() === "2",
   fc.state.join() + " unpaired " + fc.unpaired.join());
const htLockedSeed = base.map((s, i) => (i === 1 ? { ...s, seed: 99 } : i === 2 ? { ...s, locked: true } : s));
fc = forecastPlan({ shots: htLockedSeed, rec: htRec(base), now: htSnap(htLockedSeed) });
ck("hand_tie: a locked hop after a seed retake is not unpaired",
   fc.state.join() === "keeps,render,locked,keeps" && fc.unpaired.length === 0,
   fc.state.join() + " unpaired " + fc.unpaired.join());

const oldSnap = snapOf(base, { widgets: { steps: 8, overlap: "22 f", pin_mech: "hand_tie" } });
fc = forecastPlan({ shots: htSeed, rec: recOf(base, oldSnap), now: htSnap(htSeed) });
ck("hand_tie: a snapshot without a content map treats a seed retake as content",
   fc.state.join() === "keeps,keeps,render,render", fc.state.join());

process.exit(failed ? 1 : 0);
