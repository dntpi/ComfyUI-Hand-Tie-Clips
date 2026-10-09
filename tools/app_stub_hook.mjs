// Resolve hook for the Node tests (see app_stub_loader.mjs, which registers
// this). Rewrites ComfyUI's frontend-only app.js to a stub, so a module that
// imports it can still be loaded outside the browser.
export async function resolve(specifier, context, nextResolve) {
    if (/scripts[\\/]app(\.js)?$/.test(specifier)) {
        return {
            url: "data:text/javascript,export const app={};export default app;",
            shortCircuit: true,
        };
    }
    return nextResolve(specifier, context);
}