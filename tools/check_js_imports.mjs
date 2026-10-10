/**
 * Every name a js/editor module EXPORTS must be imported (or defined) by any
 * file that calls it.
 *
 * Why this exists: Phase A shipped `h3_ref_chain_ui.js` calling `button(...)`
 * with no import. `node --check` cannot see that -- the name parses fine, it
 * just does not exist at runtime -- and the string pins in check_all read other
 * files entirely. So every workflow holding the node failed to load with
 * "ReferenceError: button is not defined", and that was the user's first
 * contact with the new UI.
 *
 * The first scratch version of this scan found `button` and then produced two
 * false positives on the clean tree, both of which are fixed here:
 *
 *   1. `refresh() { ... }` inside an object literal -- a METHOD DEFINITION, not
 *      a call. The old version matched `\(([^()]*)\)\s*(?:=>|\{)` and treated
 *      the parameter list as a set of defined names, which invented a call.
 *   2. Names defined and used in their OWN file (`files`, `onFilesChanged` in
 *      media_picker.js). The old version built one global "exported" set and
 *      never asked whether the file being scanned was itself the definer.
 *
 * Method definitions are now excluded properly, and the defined-set is
 * per-file.
 *
 * Run: node tools/check_js_imports.mjs [repo-root]
 * Exit 0 clean, 1 on a missing import.
 */
import fs from "fs";
import path from "path";

const ROOT = process.argv[2]
  || path.resolve(path.dirname(new URL(import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, "$1"), "..");

const EDITOR = path.join(ROOT, "js", "editor");
const ENTRY = path.join(ROOT, "js", "h3_ref_chain_ui.js");

const files = [
  ENTRY,
  ...fs.readdirSync(EDITOR).filter((f) => f.endsWith(".js"))
    .map((f) => path.join(EDITOR, f)),
].filter((f) => fs.existsSync(f));

/** Strip comments and string/template literals so a name inside prose is not a call. */
function codeOnly(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "")
    .replace(/`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, '""');
}

/** Names this file imports (local alias when `as` is used). */
function importedNames(src) {
  const out = new Set();
  for (const m of src.matchAll(/import\s*\{([\s\S]*?)\}\s*from/g)) {
    for (const part of m[1].split(",")) {
      const bits = part.trim().split(/\s+as\s+/);
      const name = (bits[1] || bits[0] || "").trim();
      if (name) out.add(name);
    }
  }
  for (const m of src.matchAll(/import\s+(\w+)\s*(?:,|from)/g)) out.add(m[1]);
  return out;
}

/** Names this file declares at any scope: functions, classes, consts, params. */
function definedNames(code) {
  const out = new Set();
  for (const m of code.matchAll(/(?:^|[^\w$.])(?:function\s*\*?|class)\s+([A-Za-z_$][\w$]*)/g)) {
    out.add(m[1]);
  }
  for (const m of code.matchAll(/(?:^|[^\w$.])(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) {
    out.add(m[1]);
  }
  // Destructuring, both declaration forms.
  for (const m of code.matchAll(/(?:const|let|var)\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(",")) {
      const n = part.split(":").pop().split("=")[0].trim();
      if (n) out.add(n);
    }
  }
  for (const m of code.matchAll(/(?:const|let|var)\s*\[([^\]]*)\]/g)) {
    for (const part of m[1].split(",")) {
      const n = part.split("=")[0].trim();
      if (n) out.add(n);
    }
  }
  // Function PARAMETERS are definitions -- but a parameter list must belong to
  // a declaration, not to a call. `function f(a)`, `(a) =>`, and `async (a) =>`
  // are declarations; `foo(a)` is not. Anchor on the keyword, not on `(`.
  for (const m of code.matchAll(/(?:function\s*\*?\s*\w*|\w+\s*)?\(([^()]*)\)\s*(?:=>|\{)/g)) {
    const before = code.slice(Math.max(0, m.index), m.index + 60);
    const isDeclaration =
      /(?:function\s*\*?\s*\w*|\bcatch|for)\s*$/.test(before.slice(0, before.indexOf("(")))
      || /^\s*(?:async\s+)?\w*\s*$/.test(before.slice(0, before.indexOf("(")));
    if (!isDeclaration) continue;
    for (const part of m[1].split(",")) {
      const n = part.replace(/[{}\[\].]/g, "").split(/[=:]/)[0].trim();
      if (n && /^[A-Za-z_$][\w$]*$/.test(n)) out.add(n);
    }
  }
  return out;
}

/**
 * Index of the `)` matching the `(` at position `open`, or -1.
 *
 * `open` is the index OF the open paren, not the text after it: starting the
 * count at the wrong place finds a later paren and reads a call's arguments as
 * a method's parameter list.
 */
function balancedParenEnd(s, open) {
  let depth = 0;
  for (let i = open; i < s.length; i++) {
    const c = s[i];
    if (c === "(") depth++;
    else if (c === ")") {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

/**
 * Every `name(` in the file that is NOT a method definition.
 * `name(` preceded by `.` or `?.` is a property access, and `name() {` with a
 * brace body inside an object literal is a method DEFINITION.
 */
function calledNames(code) {
  const out = new Map(); // name -> line
  for (const m of code.matchAll(/(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = m[1];
    // A method DEFINITION -- `refresh() { ... }` in an object literal or class
    // body. Walk the balanced parens and ask what follows the `)`. `{` means a
    // definition; anything else means this really is a call.
    const open = m.index + m[0].length - 1;   // index OF the `(`
    const close = balancedParenEnd(code, open);
    if (close >= 0 && /^\s*\{/.test(code.slice(close + 1))) continue;
    if (!out.has(name)) {
      out.set(name, code.slice(0, m.index).split("\n").length);
    }
  }
  return out;
}

const exported = new Map(); // name -> defining file
for (const f of files) {
  const src = fs.readFileSync(f, "utf8");
  for (const m of src.matchAll(
    /export\s+(?:async\s+)?(?:function\s*\*?|class|const|let|var)\s+([A-Za-z_$][\w$]*)/g)) {
    exported.set(m[1], f);
  }
  for (const m of src.matchAll(/export\s*\{([^}]*)\}/g)) {
    for (const part of m[1].split(",")) {
      const n = part.trim().split(/\s+as\s+/).pop().trim();
      if (n) exported.set(n, f);
    }
  }
}

const problems = [];
for (const f of files) {
  const src = fs.readFileSync(f, "utf8");
  const code = codeOnly(src);
  const have = new Set([...importedNames(src), ...definedNames(code)]);
  for (const [name, line] of calledNames(code)) {
    if (!exported.has(name)) continue;      // not a shared name -- not our business
    if (have.has(name)) continue;           // imported or defined here
    // Defined in this very file? `exported` maps it here, and `have` missed it,
    // so treat that as a scan bug rather than a real gap ONLY if the file
    // actually declares it. definedNames is the authority; reaching here with
    // the definer === this file means definedNames missed a form.
    if (exported.get(name) === f) continue;
    problems.push({
      file: path.relative(ROOT, f),
      line,
      name,
      from: path.relative(ROOT, exported.get(name)),
    });
  }
}

if (problems.length) {
  console.log(`JS IMPORTS: ${problems.length} missing import(s).`);
  for (const p of problems) {
    console.log(`  FAIL ${p.file}:${p.line} calls ${p.name}() -- exported by `
      + `${p.from}, not imported here`);
  }
  process.exit(1);
}
console.log(`JS IMPORTS: all passed. ${files.length} files, ${exported.size} `
  + `exported names, every call bound.`);