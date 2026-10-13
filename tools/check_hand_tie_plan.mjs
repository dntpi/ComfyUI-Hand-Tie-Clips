// Round-trip of pin_mech=hand_tie's shot fields through the editor's plan model.
//
//     node tools/check_hand_tie_plan.mjs
//
// parsePlan + planToJson rebuild every shot from a fixed field list, so a field
// they do not name is DELETED by the next edit of any card (tone/anchor/refs were,
// once). plan_editor.js imports the ComfyUI app, which plain Node cannot load, so
// the two pure functions are lifted out of the source and evaluated alone.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "..", "js", "editor", "plan_editor.js"), "utf8");
const grab = (name) => {
    const a = src.indexOf(`export function ${name}(`);
    let depth = 0, i = src.indexOf("{", a);
    for (; i < src.length; i++) {
        if (src[i] === "{") depth++;
        else if (src[i] === "}" && --depth === 0) break;
    }
    return src.slice(a + "export ".length, i + 1);
};
const { parsePlan, planToJson } = new Function(
    `${grab("parsePlan")}\n${grab("planToJson")}\nreturn { parsePlan, planToJson };`)();

let fails = 0;
const ck = (label, ok, detail = "") => {
    console.log(`  ${ok ? "ok  " : "FAIL"} ${label}${detail ? " -- " + detail : ""}`);
    if (!ok) fails++;
};

const plan = JSON.stringify({ shots: [
    { id: "a", beat: "one" },
    { id: "b", beat: "two", moment: "she turns and (S1) says, <d>[English] hi there.</d>" },
    { id: "c", beat: "three", moment_end: "she smiles." },
] });
const once = parsePlan(plan);
const back = JSON.parse(planToJson(once));
ck("moment survives a parse + write", back.shots[1].moment === "she turns and (S1) says, <d>[English] hi there.</d>");
ck("moment_end survives a parse + write", back.shots[2].moment_end === "she smiles.");
ck("an unset moment is not written", !("moment" in back.shots[0]) && !("moment_end" in back.shots[0]));
once[0].beat = "edited";
const edited = JSON.parse(planToJson(once));
ck("editing another card keeps them", edited.shots[1].moment && edited.shots[2].moment_end);
ck("the card no longer shows moment boxes (the previs fields are round-tripped, not edited)",
   !src.includes("momentBox") && !src.includes("h3e-moment"));

console.log(fails ? `\n${fails} FAILED` : "\nall ok");
process.exit(fails ? 1 : 0);
