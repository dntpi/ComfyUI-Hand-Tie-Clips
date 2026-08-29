/* Skeleton shot patterns, appended into the card list.
 *
 * These are not "example scenes" -- each one exists to demonstrate a rule from
 * PROMPTING.md that a first-time author reliably gets wrong, in a form they can
 * edit rather than read:
 *
 *   - an ending written as a pose plus a sound, never as "stops talking";
 *   - a state change landing at the END of the shot before the one that needs
 *     it, because every hop opens holding the frames it was handed;
 *   - `tail` set to settle/hold wherever a clip actually finishes;
 *   - a framing change earned on a camera move, so `join: continuous` and the
 *     framing are not asking for opposite things;
 *   - a spoken line landing MID-hop with a non-verbal action running into the
 *     seam, and a named sound on every hop without dialogue, because the audio
 *     pin otherwise carries speech down the whole chain (chain_00057);
 *   - `match_cut` on a hop that changes location, because a continuous join
 *     across two rooms morphs one into the other (chain_00057).
 *
 * No beat carries an `@tag`. A template that referred to a reference the user
 * has not created would turn a helpful click into a hard error at run time.
 * The `note` on each pattern is where reference scheduling is explained
 * instead.
 */

import { el, button } from "./widget_utils.js";

function shot(beat, directives) {
    return {
        id: "", beat, directives,
        prose: "", seed: null, steps: null, duration: null, locked: false,
    };
}

export const TEMPLATES = [
    {
        name: "Dialogue, held",
        hint: "Someone speaks, then finishes. Two hops.",
        note: "The line lands mid-hop and a non-verbal action runs into the "
            + "seam, so the audio pin does not carry speech into the next hop.",
        shots: [
            shot("A person stands in the room, looks up at someone off-frame "
                 + "and says, 'You are earlier than I expected.' They set both "
                 + "hands on the counter, the refrigerator humming behind them.",
                 { camera: "hold", framing: "medium", pace: "steady", tail: "ongoing" }),
            shot("Their weight shifts onto one hip and their eyes move slowly "
                 + "across the room. The refrigerator hums and a car passes "
                 + "outside.",
                 { join: "continuous", camera: "hold", framing: "keep", pace: "slow", tail: "settle" }),
        ],
    },
    {
        name: "Cross the room",
        hint: "Movement across the space, camera following. Two hops.",
        note: "The framing change is earned on the move -- a framing change "
            + "with a held camera and a continuous join implies a cut.",
        shots: [
            shot("They set down what they are holding and walk the length of "
                 + "the room to the window, looking out at the street. Their "
                 + "steps are soft on the floor.",
                 { join: "continuous", camera: "pan_follow", framing: "medium", pace: "steady", tail: "ongoing" }),
            shot("They arrive at the window and rest one hand on the frame, "
                 + "their weight settling onto one hip. Traffic passes faintly "
                 + "on the other side of the glass.",
                 { join: "continuous", camera: "push_in", framing: "close", pace: "slow", tail: "settle" }),
        ],
    },
    {
        name: "Leave for an unseen space",
        hint: "Exit the referenced room into somewhere with no picture. Two hops.",
        note: "Take the room reference OFF these hops in the register. A plate "
            + "of the room riding them drags the character back into it.",
        shots: [
            shot("They turn from the window, cross the room and push through "
                 + "the doorway into the corridor beyond, the room falling "
                 + "away behind them, their footsteps carrying on the floor.",
                 { join: "continuous", camera: "pan_follow", framing: "wide", pace: "brisk", tail: "ongoing" }),
            shot("They walk down a narrow corridor hung with coats, one hand "
                 + "trailing along the wall, their footsteps muffled on the "
                 + "runner.",
                 { join: "continuous", camera: "handheld", framing: "medium", pace: "steady", tail: "ongoing" }),
        ],
    },
    {
        name: "Return to the room",
        hint: "Come back to an established space. One hop.",
        note: "Put the room reference back on this hop, and join on "
            + "match_cut -- a continuous join across a real location "
            + "change morphs one room into the other.",
        shots: [
            shot("They walk back through the doorway to where they started, "
                 + "picking up what they set down. The room is quiet apart "
                 + "from the hum of the refrigerator.",
                 { join: "match_cut", camera: "pull_back", framing: "wide", pace: "steady", tail: "hold" }),
        ],
    },
    {
        name: "Quiet close",
        hint: "End a chain without a stray gesture. One hop.",
        note: "A discrete sound rather than a continuous bed, and `tail: hold` "
            + "so the model is not told action is still underway at the last "
            + "frame.",
        shots: [
            shot("They lean back with their lips closed and let their eyes "
                 + "move slowly across the room. A single click from the "
                 + "refrigerator, then stillness.",
                 { join: "continuous", camera: "hold", framing: "keep", pace: "slow", tail: "hold" }),
        ],
    },
];

/* A panel of one row per pattern. Hidden until the SCRIPT header's button asks
 * for it, so it costs nothing on a node the author already knows their way
 * around. `onPick` receives a fresh deep copy -- the caller renumbers ids, and
 * handing out the module-level object would let one insertion mutate the
 * template for the rest of the session. */
export function createTemplatePanel({ onPick }) {
    const root = el("div", "h3e-tpl");
    root.style.display = "none";

    root.appendChild(el("div", "h3e-note",
        "Appended to the end of your script. Every beat is a starting point to "
        + "rewrite, not a finished shot."));

    for (const t of TEMPLATES) {
        const row = el("div", "h3e-tpl-row");
        const text = el("div", "h3e-tpl-text");
        text.appendChild(el("div", "h3e-tpl-name", t.name));
        text.appendChild(el("div", "h3e-tpl-hint", t.hint));
        text.appendChild(el("div", "h3e-tpl-note", t.note));
        row.appendChild(text);
        row.appendChild(button(
            `+ ${t.shots.length}`,
            `Append ${t.shots.length} shot${t.shots.length > 1 ? "s" : ""}: ${t.note}`,
            () => onPick(JSON.parse(JSON.stringify(t.shots))),
        ));
        root.appendChild(row);
    }

    return {
        root,
        toggle() {
            root.style.display = root.style.display === "none" ? "" : "none";
            return root.style.display !== "none";
        },
        hide() { root.style.display = "none"; },
    };
}
