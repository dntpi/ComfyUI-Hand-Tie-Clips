// The timeline forecast's rules, under Node. Run by tools/check_timeline.py.
import { forecastPlan, shotSig, tieSections, framesOf } from "../js/editor/timeline_model.js";

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

// Hand-tie: one previs per section, so a change re-renders the section.
fc = forecastPlan({ shots: edited, rec, now: snapOf(edited), tie: true });
ck("hand-tie: editing shot 2 re-renders its whole section, shot 1 included",
   fc.state.join() === "render,render,render,render", fc.state.join());
ck("hand-tie: the cause says the previs changes",
   String(fc.cause[0]).startsWith("the section's previs changes (shot 2 edited"), String(fc.cause[0]));
const tieExt = [...base, shot("s5", "e")];
fc = forecastPlan({ shots: tieExt, rec, now: snapOf(tieExt), tie: true });
ck("hand-tie: extending re-renders the whole section, not just the new hop",
   fc.state.every((st) => st === "render"), fc.state.join());
const tieR = base.map((s) => ({ ...s }));
tieR[2].anchor = "restart";
const tieRe = tieR.map((s) => ({ ...s }));
tieRe[3].beat = "d2";
fc = forecastPlan({ shots: tieRe, rec: recOf(tieR, snapOf(tieR)), now: snapOf(tieRe), tie: true });
ck("hand-tie: a change stays in its own section (stops at the restart)",
   fc.state.join() === "keeps,keeps,render,render", fc.state.join());
fc = forecastPlan({ shots: base, rec, now, tie: true });
ck("hand-tie: nothing edited, nothing re-renders", fc.state.every((s) => s === "keeps"), fc.state.join());

ck("framesOf matches DURATION_FRAMES (8 s 192, 5 s 124, 15 s 362, 30 s 719)",
   [framesOf("8 s"), framesOf("5 s"), framesOf("15 s"), framesOf("30 s")].join() === "192,124,362,719");
const twelve = Array.from({ length: 12 }, (_, i) => shot(`t${i}`, "x"));
const secs = tieSections(twelve, twelve.map(() => 192));
ck("hand-tie: 12 x 8 s is two sections of 6, the second chained",
   secs.map((s) => s.hops.length).join() === "6,6" && secs.map((s) => s.chained).join() === "false,true",
   JSON.stringify(secs));
const ten = tieSections(twelve.slice(0, 10), twelve.slice(0, 10).map(() => 192));
ck("hand-tie: 10 x 8 s is 5 + 5, as hand_tie.sections (balanced)",
   ten.map((s) => s.hops.length).join() === "5,5" && ten.map((s) => s.chained).join() === "false,true",
   JSON.stringify(ten));
const sevn = tieSections(twelve.slice(0, 7), twelve.slice(0, 7).map(() => 192));
ck("hand-tie: 7 x 8 s is 4 + 3", sevn.map((s) => s.hops.length).join() === "4,3", JSON.stringify(sevn));
// "+ extend" after a render: only the new shot renders.
const extd = [...base, shot("s5", "e", { anchor: "extend" })];
fc = forecastPlan({ shots: extd, rec, now: snapOf(extd), tie: true, frames: extd.map(() => 192) });
ck("hand-tie: + extend (anchor=extend) renders only the new shot",
   fc.state.join() === "keeps,keeps,keeps,keeps,render", fc.state.join());
// Editing a shot in section 1 moves the chained section 2 as well.
const recT = recOf(twelve, snapOf(twelve));
const twelveE = twelve.map((s) => ({ ...s }));
twelveE[2].beat = "x2";
fc = forecastPlan({ shots: twelveE, rec: recT, now: snapOf(twelveE), tie: true,
                    frames: twelve.map(() => 192) });
ck("hand-tie: an edit in section 1 moves the chained section 2 too",
   fc.state.every((s) => s === "render") && String(fc.cause[8]).includes("the section before it moved"),
   fc.state.join() + " " + fc.cause[8]);
const twelveL = twelve.map((s) => ({ ...s }));
twelveL[9].beat = "x9";
fc = forecastPlan({ shots: twelveL, rec: recT, now: snapOf(twelveL), tie: true,
                    frames: twelve.map(() => 192) });
ck("hand-tie: an edit in section 2 leaves section 1 cached",
   fc.state.join() === "keeps,keeps,keeps,keeps,keeps,keeps,render,render,render,render,render,render",
   fc.state.join());

process.exit(failed ? 1 : 0);
