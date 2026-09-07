const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "../app/lib/session.ts"), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const session = { user_id: "seller-user", login: "rionero", display_name: "Rionero", realm: "seller", expires_at: "2026-09-08T00:00:00Z" };

function setup(fetchImpl) {
  const exports = {};
  const requests = [];
  const redirects = [];
  vm.runInNewContext(compiled, {
    exports, AbortSignal,
    fetch: async (url, options) => { requests.push({ url, options }); return fetchImpl(url, options); },
    require: (name) => {
      if (name === "next/headers") return { cookies: async () => ({ toString: () => "mh_session=existing-session" }) };
      if (name === "next/navigation") return { redirect: (target) => { redirects.push(target); throw new Error(`REDIRECT:${target}`); } };
      if (name === "./api-url") return { apiUrl: "http://api.test" };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  return { requireRealm: exports.requireRealm, requests, redirects };
}

test("valid existing session is forwarded without a new login and uses bounded uncached request", async () => {
  const helper = setup(async () => ({ ok: true, status: 200, json: async () => session }));
  assert.equal(await helper.requireRealm("seller", "/login/seller"), session);
  assert.deepEqual(helper.redirects, []);
  assert.equal(helper.requests[0].options.headers.cookie, "mh_session=existing-session");
  assert.equal(helper.requests[0].options.cache, "no-store");
  assert.ok(helper.requests[0].options.signal instanceof AbortSignal);
});

for (const status of [401, 403]) {
  test(`confirmed ${status} redirects to the corresponding login`, async () => {
    const helper = setup(async () => ({ ok: false, status }));
    await assert.rejects(helper.requireRealm("agency", "/login/agency"), /REDIRECT:\/login\/agency/);
    assert.deepEqual(helper.redirects, ["/login/agency"]);
  });
}

for (const status of [429, 500, 502, 503]) {
  test(`temporary ${status} offers recoverable state and does not redirect existing session`, async () => {
    const helper = setup(async () => ({ ok: false, status }));
    assert.equal(await helper.requireRealm("seller", "/login/seller"), null);
    assert.deepEqual(helper.redirects, []);
  });
}

test("network timeout preserves the recoverable state", async () => {
  const helper = setup(async () => { throw new Error("Connection timed out"); });
  assert.equal(await helper.requireRealm("seller", "/login/seller"), null);
  assert.deepEqual(helper.redirects, []);
});

test("invalid JSON or incomplete session does not grant access or trigger a false logout", async () => {
  for (const json of [async () => { throw new SyntaxError("Unexpected response"); }, async () => ({ realm: "seller" })]) {
    const helper = setup(async () => ({ ok: true, status: 200, json }));
    assert.equal(await helper.requireRealm("seller", "/login/seller"), null);
    assert.deepEqual(helper.redirects, []);
  }
});

test("a valid session for a different realm cannot open the requested portal", async () => {
  const helper = setup(async () => ({ ok: true, status: 200, json: async () => session }));
  await assert.rejects(helper.requireRealm("platform", "/system-admin/login"), /REDIRECT:\/system-admin\/login/);
});
