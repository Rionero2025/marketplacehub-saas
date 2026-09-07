const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

function load(relativePath, context) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortSignal, ...context });
  return exports;
}
const navigation = load("../app/lib/seller-navigation.ts", {});

function component(fetchImpl) {
  const state = [];
  const navigations = [];
  const buttons = [];
  let refreshCount = 0;
  const element = (type, props) => {
    if (type === "button") buttons.push(props);
    return { type, props };
  };
  const { PortalShell } = load("../app/components/PortalShell.tsx", {
    fetch: fetchImpl,
    require(name) {
      if (name === "react") return {
        useState: (initial) => { const index = state.length; state.push(initial); return [initial, (value) => { state[index] = value; }]; },
        useRef: (current) => ({ current }), useEffect: () => {},
      };
      if (name === "react/jsx-runtime") return { jsx: element, jsxs: element };
      if (name === "next/navigation") return { useRouter: () => ({ replace: (target) => navigations.push(target), refresh: () => { refreshCount += 1; } }) };
      if (name === "next/link") return { default: "a" };
      if (name === "../lib/seller-navigation") return navigation;
      if (name === "./DashboardIcon") return { DashboardIcon: () => null };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  PortalShell({ portal: "SELLER", displayName: "Seller", children: null });
  return { logout: buttons.find((button) => button.id === "hub-logout").onClick, state, navigations, refreshCount: () => refreshCount };
}

test("confirmed logout navigates home, invalidates the router and releases pending", async () => {
  const shell = component(async (_url, options) => { assert.ok(options.signal instanceof AbortSignal); return { ok: true, status: 204 }; });
  await shell.logout();
  assert.deepEqual(shell.navigations, ["/"]);
  assert.equal(shell.refreshCount(), 1);
  assert.deepEqual(shell.state.slice(0, 2), [false, ""]);
});

test("rejected logout keeps the current portal and offers a retry", async () => {
  const shell = component(async () => ({ ok: false, status: 503 }));
  await shell.logout();
  assert.deepEqual(shell.navigations, []);
  assert.equal(shell.refreshCount(), 0);
  assert.equal(shell.state[0], false);
  assert.match(shell.state[1], /Riprova/);
});

test("network failure releases pending and does not pretend logout succeeded", async () => {
  const shell = component(async () => { throw new Error("Network unavailable"); });
  await shell.logout();
  assert.deepEqual(shell.navigations, []);
  assert.equal(shell.state[0], false);
  assert.match(shell.state[1], /connessione/);
});

function mobileMenu() {
  const state = [], refs = [], effects = [];
  let stateIndex, refIndex, effectIndex, pendingEffects, tree;
  const document = {
    activeElement: null, listeners: new Map(),
    addEventListener(type, listener) { this.listeners.set(type, listener); },
    removeEventListener(type, listener) { if (this.listeners.get(type) === listener) this.listeners.delete(type); },
  };
  const viewport = {
    matches: true, listener: null,
    addEventListener(_type, listener) { this.listener = listener; },
    removeEventListener(_type, listener) { if (this.listener === listener) this.listener = null; },
  };
  const descendants = (node) => {
    if (!node || typeof node !== "object") return [];
    if (Array.isArray(node)) return node.flatMap(descendants);
    return [node, ...descendants(node.props?.children)];
  };
  const element = (type, props = {}) => {
    const node = { type, props, focus() { document.activeElement = this; }, getClientRects: () => [1] };
    node.contains = (candidate) => descendants(node).includes(candidate);
    node.querySelectorAll = () => descendants(node).filter((item) => item.type === "a" || (item.type === "button" && !item.props.disabled));
    node.querySelector = (selector) => descendants(node).find((item) => selector === ".hub-menu-close" ? item.props.className === "hub-menu-close" : item.type === "a" && item.props["aria-current"]);
    if (props.ref) props.ref.current = node;
    return node;
  };
  const { PortalShell } = load("../app/components/PortalShell.tsx", {
    document, window: { location: { hash: "" }, addEventListener() {}, removeEventListener() {}, matchMedia: (query) => { assert.equal(query, "(max-width: 900px)"); return viewport; } },
    require(name) {
      if (name === "react") return {
        useState(initial) { const index = stateIndex++; if (!(index in state)) state[index] = initial; return [state[index], (value) => { state[index] = value; }]; },
        useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
        useEffect(create, dependencies) {
          const index = effectIndex++;
          if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) {
            pendingEffects.push(() => { effects[index]?.cleanup?.(); effects[index] = { dependencies, cleanup: create() }; });
          }
        },
      };
      if (name === "react/jsx-runtime") return { jsx: element, jsxs: element };
      if (name === "next/navigation") return { useRouter: () => ({ replace() {}, refresh() {} }) };
      if (name === "next/link") return { default: "a" };
      if (name === "../lib/seller-navigation") return navigation;
      if (name === "./DashboardIcon") return { DashboardIcon: () => null };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  function render() {
    stateIndex = refIndex = effectIndex = 0; pendingEffects = [];
    tree = PortalShell({ portal: "SELLER", displayName: "Seller", children: null });
    pendingEffects.forEach((effect) => effect());
  }
  const byClass = (className) => descendants(tree).find((node) => node.props?.className === className);
  render();
  return {
    document, viewport, render, byClass,
    open() { byClass("hub-menu-toggle").props.onClick(); render(); },
    press(key, shiftKey = false) {
      const event = { key, shiftKey, prevented: false, preventDefault() { this.prevented = true; } };
      document.listeners.get("keydown")?.(event);
      return event;
    },
  };
}

test("mobile drawer takes focus, wraps Tab and excludes the underlying page", () => {
  const menu = mobileMenu();
  menu.open();
  const sidebar = menu.byClass("hub-sidebar");
  const controls = sidebar.querySelectorAll();
  assert.equal(menu.document.activeElement, menu.byClass("hub-menu-close"));
  assert.equal(menu.byClass("hub-main").props.inert, true);
  controls[0].focus();
  assert.equal(menu.press("Tab", true).prevented, true);
  assert.equal(menu.document.activeElement, controls.at(-1));
  assert.equal(menu.press("Tab").prevented, true);
  assert.equal(menu.document.activeElement, controls[0]);
});

test("Escape, close button and backdrop restore focus to the mobile trigger", () => {
  for (const closeWith of ["Escape", "hub-menu-close", "hub-menu-backdrop"]) {
    const menu = mobileMenu();
    menu.open();
    if (closeWith === "Escape") assert.equal(menu.press("Escape").prevented, true);
    else menu.byClass(closeWith).props.onClick();
    menu.render();
    assert.equal(menu.byClass("hub-main").props.inert, false);
    assert.equal(menu.document.activeElement, menu.byClass("hub-menu-toggle"));
    assert.equal(menu.document.listeners.has("keydown"), false);
    assert.equal(menu.viewport.listener, null);
  }
});

test("resizing to desktop closes the mobile drawer without leaving the page inert", () => {
  const menu = mobileMenu();
  menu.open();
  menu.viewport.matches = false;
  menu.viewport.listener();
  menu.render();
  assert.equal(menu.byClass("hub-main").props.inert, false);
  assert.equal(menu.document.activeElement.props["aria-current"], "location");
  assert.equal(menu.document.listeners.has("keydown"), false);
});

function route(fetchImpl) {
  class NextResponse extends Response {
    static json(value, options) { return new NextResponse(JSON.stringify(value), options); }
  }
  return load("../app/api/auth/logout/route.ts", {
    fetch: fetchImpl,
    require(name) {
      if (name === "next/server") return { NextResponse };
      if (name === "../../../lib/api-url") return { apiUrl: "http://api.test" };
      throw new Error(`Unexpected dependency ${name}`);
    },
  }).POST;
}

test("BFF forwards the session and clears the cookie only after confirmed logout", async () => {
  const post = route(async (_url, options) => {
    assert.equal(options.headers.cookie, "mh_session=existing-session");
    assert.ok(options.signal instanceof AbortSignal);
    return new Response(null, { status: 204, headers: { "set-cookie": "mh_session=; Max-Age=0; HttpOnly" } });
  });
  const response = await post({ headers: new Headers({ cookie: "mh_session=existing-session" }) });
  assert.equal(response.status, 204);
  assert.match(response.headers.get("set-cookie"), /Max-Age=0/);
});

test("BFF upstream failure does not clear the existing session cookie", async () => {
  for (const upstream of [async () => new Response(null, { status: 503 }), async () => { throw new Error("Timed out"); }]) {
    const post = route(upstream);
    const response = await post({ headers: new Headers() });
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("set-cookie"), null);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
});
