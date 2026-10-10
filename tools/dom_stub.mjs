/**
 * A DOM stub small enough to read, big enough to run the node's creation path.
 *
 * This exists because `node --check` and the string pins cannot see a
 * ReferenceError at RUNTIME, which is the only failure that made the user's
 * first contact with the new UI: "ReferenceError: button is not defined", on
 * every workflow holding the node. The name parses. It is simply not there.
 *
 * So this is deliberately shallow -- no layout, no CSS engine, no event
 * dispatch semantics beyond recording -- and deliberately STRICT. Every method
 * the panel calls is a real function that does the cheap thing, and anything
 * not in here is simply absent, which is how a missing helper surfaces as a
 * TypeError at the point of use rather than being silently tolerated.
 */
class StubEvent {
  constructor(type, init = {}) {
    this.type = type;
    Object.assign(this, init);
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this._stopped = true; }
  stopImmediatePropagation() { this._stopped = true; }
}

class StubClassList {
  constructor(node) { this.node = node; this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  toggle(c, on) { (on === undefined ? !this.set.has(c) : on) ? this.set.add(c) : this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  get value() { return [...this.set].join(" "); }
  set value(v) { this.set = new Set(String(v).split(/\s+/).filter(Boolean)); }
}

let NODE_SEQ = 0;

class StubElement {
  constructor(tag) {
    this.tagName = String(tag || "div").toUpperCase();
    this.children = [];
    this.childNodes = this.children;
    this.parentNode = null;
    this.attributes = {};
    this.style = new Proxy({}, { get: (t, k) => t[k] ?? "", set: (t, k, v) => ((t[k] = v), true) });
    this.dataset = {};
    this._listeners = new Map();
    this.classList = new StubClassList(this);
    // `textContent = ""` REMOVES THE CHILDREN. This was a plain field, so every
    // `list.textContent = ""` in the panel silently did nothing and the strip
    // accumulated a second copy of itself on each re-render -- which presented
    // as a selection bug and was not one. A stub that is more permissive than
    // the real DOM hides real bugs rather than surfacing them.
    this._text = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.id = "";
    this.title = "";
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollHeight = 1000;
    this.scrollWidth = 1000;
    this.clientHeight = 500;
    this.clientWidth = 560;
    this.offsetHeight = 20;
    this.offsetWidth = 100;
    this._uid = ++NODE_SEQ;
  }

  // --- tree ---------------------------------------------------------------
  appendChild(child) {
    if (!child) throw new TypeError("appendChild(null)");
    child.parentNode = this;
    this.children.push(child);
    this._dirty = true;
    return child;
  }
  append(...kids) { kids.forEach((k) => this.appendChild(k)); }
  prependChild(child) { child.parentNode = this; this.children.unshift(child); return child; }
  insertBefore(child) { return this.appendChild(child); }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i >= 0) this.children.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  remove() { this.parentNode?.removeChild(this); }
  replaceChildren(...kids) { this.children.length = 0; kids.forEach((k) => this.appendChild(k)); }
  contains(n) {
    for (let p = n; p; p = p.parentNode) if (p === this) return true;
    return false;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() {
    const p = this.parentNode;
    if (!p) return null;
    return p.children[p.children.indexOf(this) + 1] || null;
  }
  get previousSibling() {
    const p = this.parentNode;
    if (!p) return null;
    return p.children[p.children.indexOf(this) - 1] || null;
  }

  // --- attributes ---------------------------------------------------------
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  hasAttribute(k) { return k in this.attributes; }

  // --- class / style ------------------------------------------------------
  get textContent() { return this._text || ""; }
  set textContent(v) {
    this._text = v == null ? "" : String(v);
    // Assigning text replaces the children. Every `x.textContent = ""` in the
    // panel relies on this to clear a container, so a stub that kept the
    // children would make re-render append forever -- which is exactly the
    // class of bug this stub exists to catch.
    if (this._text === "") {
      for (const c of this.children) c.parentNode = null;
      this.children.length = 0;
    }
  }
  get className() { return this.classList.value; }
  set className(v) { this.classList.value = v; }
  get innerHTML() { return ""; }
  set innerHTML(v) {
    if (v) this.replaceChildren();
    else this.replaceChildren();
  }
  get outerHTML() { return `<${this.tagName.toLowerCase()}>`; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  matches() { return false; }
  closest() { return null; }

  // --- events -------------------------------------------------------------
  addEventListener(type, fn) {
    if (typeof fn !== "function") throw new TypeError(`addEventListener(${type}) got ${typeof fn}`);
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const l = this._listeners.get(type);
    if (l) this._listeners.set(type, l.filter((f) => f !== fn));
  }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    let node = this;
    while (node) {
      for (const fn of (node._listeners.get(ev.type) || [])) fn.call(node, ev);
      node = ev.bubbles === false ? null : node.parentNode;
    }
    return true;
  }
  /** Test helper: fire a listener without building an event. */
  fire(type, ev = {}) {
    return this.dispatchEvent(new StubEvent(type, ev));
  }
  click() { return this.fire("click"); }

  // --- layout (arithmetic only -- no layout engine) ----------------------
  getBoundingClientRect() {
    return { x: 0, y: 0, top: 0, left: 0, right: this.clientWidth,
             bottom: this.clientHeight, width: this.clientWidth,
             height: this.clientHeight };
  }
  getClientRects() { return [this.getBoundingClientRect()]; }
  focus() { StubElement._focused = this; }
  blur() { if (StubElement._focused === this) StubElement._focused = null; }
  scrollIntoView() {}
  setPointerCapture() {}
  releasePointerCapture() {}
  cloneNode() { return new StubElement(this.tagName); }

  // --- inputs -------------------------------------------------------------
  select() {}
  setSelectionRange() {}
}

/**
 * Install globals. Returns a teardown so a test file can run more than one
 * case in the same process without one stub leaking into the next.
 */
export function installDom() {
  const doc = {
    createElement: (t) => new StubElement(t),
    createElementNS: (_ns, t) => new StubElement(t),
    createTextNode: (t) => ({ nodeValue: t, textContent: t }),
    createDocumentFragment: () => new StubElement("fragment"),
    body: new StubElement("body"),
    documentElement: new StubElement("html"),
    head: new StubElement("head"),
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    addEventListener() {}, removeEventListener() {},
  };
  const g = globalThis;
  const saved = {
    document: g.document, window: g.window, HTMLElement: g.HTMLElement,
    Node: g.Node, Event: g.Event, CustomEvent: g.CustomEvent,
    requestAnimationFrame: g.requestAnimationFrame,
    cancelAnimationFrame: g.cancelAnimationFrame,
    getComputedStyle: g.getComputedStyle,
  };
  g.document = doc;
  g.window = g.window || { document: doc, getComputedStyle: () => ({ getPropertyValue: () => "" }), addEventListener() {}, removeEventListener() {}, devicePixelRatio: 1, matchMedia: () => ({ matches: false, addEventListener() {} }) };
  g.window.document = doc;
  g.HTMLElement = StubElement;
  g.Element = StubElement;
  g.Node = StubElement;
  g.Event = StubEvent;
  g.CustomEvent = StubEvent;
  g.MutationObserver = class { observe() {} disconnect() {} };
  g.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  g.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
  // Synchronous rAF: the panel uses it to settle the height guard, and an
  // async stub would let a test finish before the panel ever laid out.
  g.requestAnimationFrame = (fn) => { try { fn(0); } catch (e) { /* surfaced by the caller */ } return 0; };
  g.cancelAnimationFrame = () => {};
  g.getComputedStyle = () => ({ getPropertyValue: () => "", fontSize: "12px" });
  return () => { Object.assign(g, saved); };
}

export { StubElement, StubEvent };