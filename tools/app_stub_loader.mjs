// Preload for the Node tests: stand in for ComfyUI's browser-only app.js so
// widget_utils.js can be imported outside the browser. installHeightGuard never
// touches `app` -- widget_utils.js imports it only for a canvas helper in a
// different function -- so an empty object is enough to satisfy the graph
// without pulling in the whole frontend.
import { register } from "node:module";
register(new URL("./app_stub_hook.mjs", import.meta.url));