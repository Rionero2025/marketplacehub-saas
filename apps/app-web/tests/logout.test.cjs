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
      if (name === "react") return { useState: (initial) => { const index = state.length; state.push(initial); return [initial, (value) => { state[index] = value; }]; } };
      if (name === "react/jsx-runtime") return { jsx: element, jsxs: element };
      if (name === "next/navigation") return { useRouter: () => ({ replace: (target) => navigations.push(target), refresh: () => { refreshCount += 1; } }) };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  PortalShell({ portal: "SELLER", displayName: "Seller", children: null });
  return { logout: buttons[0].onClick, state, navigations, refreshCount: () => refreshCount };
}

test("confirmed logout navigates home, invalidates the router and releases pending", async () => {
  const shell = component(async (_url, options) => { assert.ok(options.signal instanceof AbortSignal); return { ok: true, status: 204 }; });
  await shell.logout();
  assert.deepEqual(shell.navigations, ["/"]);
  assert.equal(shell.refreshCount(), 1);
  assert.deepEqual(shell.state, [false, ""]);
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
