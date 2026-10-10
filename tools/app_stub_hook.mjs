// Resolve hook for the Node tests (see app_stub_loader.mjs, which registers
// this). Rewrites ComfyUI's frontend-only app.js to a stub, so a module that
// imports it can still be loaded outside the browser.
//
// The stub app RECORDS what it is asked to register, on globalThis.__appStub.
// The height guard never touches app at all, so an empty object was enough
// there; the mount test is the opposite case -- it needs to reach the node's
// real beforeRegisterNodeDef, which the entry only ever exposes by calling
// app.registerExtension. An empty object makes that call fail on a property
// that does not exist, which is the opposite of what that test is for.
const APP_RECORDER = `
const rec = { extensions: [], sidebarTabs: [], listeners: {} };
globalThis.__appStub = rec;
export const app = {
  registerExtension(ext) { rec.extensions.push(ext); return ext; },
  registerSidebarTab(t) { rec.sidebarTabs.push(t); return t; },
  extensionManager: {
    registerSidebarTab(t) { rec.sidebarTabs.push(t); return t; },
    sidebarTab: {},
  },
};
export default app;
`;

// api.js is the frontend's HTTP client. The node uses exactly two members of
// it -- addEventListener / removeEventListener for "promptQueued" -- so the
// stub records listeners rather than inventing a fetch.
const API_STUB = `
const rec = globalThis.__appStub || (globalThis.__appStub = { listeners: {} });
export const api = {
  addEventListener(name, fn) { (rec.listeners[name] ||= []).push(fn); },
  removeEventListener(name, fn) {
    const l = rec.listeners[name];
    if (l) rec.listeners[name] = l.filter((f) => f !== fn);
  },
  fetchApi() { return Promise.resolve({}); },
};
export default api;
`;

export async function resolve(specifier, context, nextResolve) {
  if (/scripts[\\/]app(\.js)?$/.test(specifier)) {
    return {
      url: "data:text/javascript," + encodeURIComponent(APP_RECORDER),
      shortCircuit: true,
    };
  }
  if (/scripts[\\/]api(\.js)?$/.test(specifier)) {
    return {
      url: "data:text/javascript," + encodeURIComponent(API_STUB),
      shortCircuit: true,
    };
  }
  return nextResolve(specifier, context);
}