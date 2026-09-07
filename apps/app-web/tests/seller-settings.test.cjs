const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const otherSellerId = "a1000000-0000-4000-8000-000000000002";
const fixture = () => ({ seller_id: sellerId, name: "Negozio demo", legal_name: "Azienda demo", email: "", can_manage: true,
  marketplace_accounts: [{ id: accountId, marketplace: "kaufland", account_name: "Kaufland principale", active: true, credentials_configured: true, client_key_masked: "••••••••abcd" }] });
const body = () => ({ name: "Nuovo nome", legal_name: "", email: "testo libero" });
const account = () => ({ account_name: " Secondo account ", client_key: " test-client ", secret_key: "" });
const plain = (value) => JSON.parse(JSON.stringify(value));

function load(relativePath, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, setTimeout, clearTimeout, URL, ...context });
  return exports;
}
const types = load("../app/lib/seller-settings-types.ts");

function proxy(fetchImpl) {
  class NextResponse extends Response {
    static json(value, options) { return new NextResponse(JSON.stringify(value), options); }
  }
  return load("../app/lib/seller-settings-proxy.ts", {
    fetch: fetchImpl,
    require(name) {
      if (name === "next/server") return { NextResponse };
      if (name === "./api-url") return { apiUrl: "http://api.test" };
      if (name === "./seller-settings-types") return types;
      throw new Error(`Unexpected dependency ${name}`);
    },
  }).sellerSettingsProxy;
}
const request = (payload, headers = {}) => ({ url: "https://app.test/api/sellers/settings", headers: new Headers({ cookie: "mh_session=existing", ...headers }), json: async () => payload });

test("settings input requires a name, preserves free text and discards retired profit split fields", () => {
  assert.equal(types.readSettingsInput(body()).email, "testo libero");
  assert.deepEqual(plain(types.readSettingsInput({ ...body(), name: " N ", our_profit_pct: 40, partner_profit_pct: 40 })), { ...body(), name: "N" });
  for (const values of [{ name: " " }, { name: null }, { legal_name: null }, { email: null }]) {
    assert.equal(types.readSettingsInput({ ...body(), ...values }), null);
  }
});

test("account input accepts either key as in Streamlit and refuses two empty keys", () => {
  assert.deepEqual(plain(types.readAccountInput(account())), { account_name: "Secondo account", client_key: "test-client", secret_key: "" });
  assert.ok(types.readAccountInput({ account_name: "Solo secret", client_key: "", secret_key: "secret" }));
  assert.equal(types.readAccountInput({ ...account(), client_key: " ", secret_key: " " }), null);
});

test("public DTO verifies identity and nested accounts while discarding raw credentials", () => {
  const value = fixture();
  value.client_key = "never-expose";
  value.our_profit_pct = 40;
  value.partner_profit_pct = 60;
  value.marketplace_accounts[0].secret_key = "never-expose";
  const parsed = types.readSellerSettings(value, sellerId);
  assert.ok(parsed);
  assert.equal(JSON.stringify(parsed).includes("never-expose"), false);
  assert.equal("our_profit_pct" in parsed, false);
  assert.equal("partner_profit_pct" in parsed, false);
  for (const bad of [{ ...value, seller_id: otherSellerId }, { ...value, can_manage: "true" },
    { ...value, marketplace_accounts: [{ ...value.marketplace_accounts[0], client_key_masked: "raw-secret" }] },
    { ...value, marketplace_accounts: [value.marketplace_accounts[0], value.marketplace_accounts[0]] }]) {
    assert.equal(types.readSellerSettings(bad, sellerId), null);
  }
});

test("masked client tails preserve original characters including a newline without exposing more than four", () => {
  const value = fixture();
  value.marketplace_accounts[0].client_key_masked = "••••••••a\nbc";
  assert.equal(types.readSellerSettings(value, sellerId).marketplace_accounts[0].client_key_masked, "••••••••a\nbc");
  value.marketplace_accounts[0].client_key_masked = "••••••••abcde";
  assert.equal(types.readSellerSettings(value, sellerId), null);
});

test("BFF uses fixed seller paths, authenticated cookies, timeout and no-store for each operation", async () => {
  for (const [operation, method, suffix, payload] of [["read", "GET", "settings"], ["save", "PUT", "settings", body()],
    ["add-account", "POST", "kaufland-accounts", account()], ["delete-account", "DELETE", `kaufland-accounts/${accountId}`, { confirmation: "ELIMINA" }]]) {
    const run = proxy(async (url, options) => {
      assert.equal(url, `http://api.test/v1/sellers/${sellerId}/${suffix}`);
      assert.equal(options.method, method);
      assert.equal(options.headers.cookie, "mh_session=existing");
      assert.equal(options.cache, "no-store");
      assert.ok(options.signal instanceof AbortSignal);
      if (operation === "delete-account") assert.deepEqual(JSON.parse(options.body), { confirmation: "ELIMINA" });
      return Response.json(fixture());
    });
    const response = await run(request(payload, { origin: "https://app.test" }), sellerId, operation, accountId);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("cache-control"), "no-store");
  }
});

test("BFF rejects path injection, unauthenticated and cross-origin writes without contacting API", async () => {
  let calls = 0;
  const run = proxy(async () => { calls++; throw new Error("must not call"); });
  assert.equal((await run(request(body()), "../another-seller", "save")).status, 422);
  assert.equal((await run(request({ confirmation: "ELIMINA" }), sellerId, "delete-account", "../../orders")).status, 422);
  assert.equal((await run(request(body(), { cookie: "" }), sellerId, "save")).status, 401);
  assert.equal((await run(request(body(), { origin: "https://other.test" }), sellerId, "save")).status, 403);
  assert.equal(calls, 0);
});

test("BFF requires exact deletion confirmation and does not send unknown input fields", async () => {
  const inputs = [];
  const run = proxy(async (_url, options) => { inputs.push(JSON.parse(options.body)); return Response.json(fixture()); });
  for (const payload of [null, {}, { confirmation: "elimina" }, { confirmation: " ELIMINA " }]) {
    assert.equal((await run(request(payload), sellerId, "delete-account", accountId)).status, 422);
  }
  assert.equal(inputs.length, 0);
  await run(request({ ...body(), seller_id: otherSellerId, can_manage: true, url: "http://other.test", our_profit_pct: 40, partner_profit_pct: 40 }), sellerId, "save");
  assert.deepEqual(inputs[0], body());
});

test("BFF accepts public Host behind Next's internal URL while rejecting a forged forwarded host", async () => {
  let calls = 0;
  const run = proxy(async () => { calls++; return Response.json(fixture()); });
  const forwarded = { ...request(body(), { host: "app.test", origin: "https://app.test" }), url: "https://localhost:10000/api/sellers/settings" };
  assert.equal((await run(forwarded, sellerId, "save")).status, 200);
  const forged = { ...request(body(), { host: "app.test", origin: "https://attacker.test", "x-forwarded-host": "attacker.test" }), url: forwarded.url };
  assert.equal((await run(forged, sellerId, "save")).status, 403);
  assert.equal(calls, 1);
});

test("upstream errors and malformed responses are sanitized and never echo credential values", async () => {
  for (const [result, expected] of [[() => Response.json({ detail: "secret-from-body" }, { status: 422 }), 422],
    [() => Response.json({ detail: "secret-from-body" }, { status: 403 }), 403], [() => new Response(null, { status: 500 }), 503],
    [() => Response.json({ ...fixture(), seller_id: otherSellerId }), 502], [() => { throw new Error("secret-from-body"); }, 503]]) {
    const response = await proxy(async () => result())(request(account()), sellerId, "add-account");
    assert.equal(response.status, expected);
    assert.equal((await response.text()).includes("secret-from-body"), false);
  }
});

test("Next routes pass UUID parameters and operation to shared BFF without selecting arbitrary upstream URLs", async () => {
  const calls = [];
  const routes = [
    ["../app/api/sellers/[sellerId]/settings/route.ts", "GET", "read"],
    ["../app/api/sellers/[sellerId]/settings/route.ts", "PUT", "save"],
    ["../app/api/sellers/[sellerId]/kaufland-accounts/route.ts", "POST", "add-account"],
    ["../app/api/sellers/[sellerId]/kaufland-accounts/[accountId]/route.ts", "DELETE", "delete-account"],
  ];
  for (const [file, method, operation] of routes) {
    const route = load(file, { require: () => ({ sellerSettingsProxy: (...args) => { calls.push(args); return "ok"; } }) });
    assert.equal(await route[method]("request", { params: Promise.resolve({ sellerId, accountId }) }), "ok");
    assert.equal(calls.at(-1)[1], sellerId);
    assert.equal(calls.at(-1)[2], operation);
  }
  assert.equal(calls.at(-1)[3], accountId);
});

function component(fetchImpl, id = sellerId) {
  const states = [], refs = [], effects = [], requests = [], navigations = [];
  let stateIndex, refIndex, effectIndex, pendingEffects, tree, saved = 0;
  const elements = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(elements) : [node, ...elements(node.props?.children)];
  const { SellerSettingsPanel } = load("../app/components/SellerSettingsPanel.tsx", {
    fetch: async (...args) => { requests.push(args); return fetchImpl(...args); },
    require(name) {
      if (name === "react") return {
        useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = initial; return [states[index], (value) => { states[index] = value; }]; },
        useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
        useEffect(create, dependencies) { const index = effectIndex++; if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) pendingEffects.push(() => { effects[index]?.cleanup?.(); effects[index] = { dependencies, cleanup: create() }; }); },
      };
      if (name === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
      if (name === "next/navigation") return { useRouter: () => ({ replace: (url) => navigations.push(url), refresh() {} }) };
      if (name === "../lib/seller-settings-types") return types;
      if (name === "./DashboardIcon") return { DashboardIcon: () => null };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  const text = (node) => typeof node === "string" ? node : typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  function render() { stateIndex = refIndex = effectIndex = 0; pendingEffects = []; tree = SellerSettingsPanel({ sellerId: id, loginPath: "/login/seller", onSaved: () => { saved++; } }); pendingEffects.forEach((effect) => effect()); }
  const nodes = () => elements(tree);
  const byId = (suffix) => nodes().find((node) => node.props?.id === `seller-settings-${id}-${suffix}`);
  const button = (label) => nodes().find((node) => node.type === "button" && text(node) === label);
  render();
  return { render, nodes, requests, navigations, byId, button, text: () => text(tree), saved: () => saved,
    async settle() { await new Promise(setImmediate); render(); },
    async open() { button("Gestisci negozio").props.onClick(); render(); await new Promise(setImmediate); render(); },
    change(suffix, value) { byId(suffix).props.onChange({ target: { value } }); render(); },
    async submit(className) { const form = nodes().find((node) => node.type === "form" && node.props.className === className); await form.props.onSubmit({ preventDefault() {} }); render(); },
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}

test("editor loads lazily, saves only profile fields and refreshes the overview only after confirmation", async () => {
  const panel = component(async (_url, options) => Response.json(options.method === "PUT" ? { ...fixture(), ...JSON.parse(options.body) } : fixture()));
  assert.equal(panel.requests.length, 0);
  await panel.open();
  assert.equal(panel.byId("ours"), undefined);
  assert.equal(panel.byId("partner"), undefined);
  panel.change("name", "Negozio aggiornato");
  await panel.submit("settings-form");
  assert.equal(panel.saved(), 1);
  assert.equal(panel.byId("name").props.value, "Negozio aggiornato");
  assert.deepEqual(JSON.parse(panel.requests.at(-1)[1].body), { name: "Negozio aggiornato", legal_name: "Azienda demo", email: "" });
  assert.match(panel.text(), /Dati del negozio salvati/);
  panel.unmount();
});

test("read-only editor disables profile changes and offers no account mutation controls", async () => {
  const panel = component(async () => Response.json({ ...fixture(), can_manage: false }));
  await panel.open();
  assert.ok(panel.nodes().filter((node) => node.type === "fieldset").every((node) => node.props.disabled));
  assert.equal(panel.button("Salva dati negozio"), undefined);
  assert.equal(panel.button("Elimina"), undefined);
  assert.equal(panel.byId("client"), undefined);
  await panel.submit("settings-form");
  assert.equal(panel.requests.length, 1);
  panel.unmount();
});

test("account keys clear after confirmed save and the UI never claims an API connection check", async () => {
  const panel = component(async () => Response.json(fixture()));
  await panel.open(); panel.change("client", "synthetic-key");
  assert.equal(panel.byId("client").props.type, "password");
  await panel.submit("settings-form settings-add-account");
  assert.equal(panel.byId("client").props.value, "");
  assert.equal(panel.byId("secret").props.value, "");
  assert.match(panel.text(), /connessione API non è stata verificata/);
  assert.equal(panel.saved(), 1);
  panel.unmount();
});

test("ambiguous save failures block further writes until a successful fresh read", async () => {
  const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture()) : new Response(null, { status: 503 }));
  await panel.open(); panel.change("client", "synthetic-key");
  await panel.submit("settings-form settings-add-account");
  assert.equal(panel.saved(), 0);
  assert.match(panel.text(), /Operazione non confermata/);
  assert.equal(panel.button("Salva account Kaufland").props.disabled, true);
  await panel.submit("settings-form settings-add-account");
  assert.equal(panel.requests.length, 2);
  await panel.button("Aggiorna dati").props.onClick(); panel.render();
  assert.equal(panel.byId("client").props.value, "");
  assert.equal(panel.button("Salva dati negozio").props.disabled, false);
  panel.unmount();
});

test("invalid Seller identity and revoked access never turn into successful mutations", async () => {
  for (const response of [() => Response.json({ ...fixture(), seller_id: otherSellerId }), () => new Response(null, { status: 403 })]) {
    const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture()) : response());
    await panel.open(); await panel.submit("settings-form");
    assert.equal(panel.saved(), 0);
    assert.ok(panel.nodes().find((node) => node.props?.role === "alert"));
    if (panel.button("Salva dati negozio")) assert.equal(panel.button("Salva dati negozio").props.disabled, true);
    panel.unmount();
  }
});

test("account delete needs exact typed confirmation and sends the selected account only", async () => {
  const panel = component(async () => Response.json(fixture()));
  await panel.open(); panel.button("Elimina").props.onClick(); panel.render();
  assert.equal(panel.button("Elimina definitivamente").props.disabled, true);
  panel.change("delete", "elimina"); assert.equal(panel.button("Elimina definitivamente").props.disabled, true);
  panel.change("delete", "ELIMINA"); assert.equal(panel.button("Elimina definitivamente").props.disabled, false);
  panel.button("Elimina definitivamente").props.onClick(); await panel.settle();
  assert.equal(panel.requests.at(-1)[0], `/api/sellers/${sellerId}/kaufland-accounts/${accountId}`);
  assert.deepEqual(JSON.parse(panel.requests.at(-1)[1].body), { confirmation: "ELIMINA" });
  panel.unmount();
});

test("switching Seller unmounts pending settings, aborts the request and ignores late success", async () => {
  let resolveSave;
  const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture()) : new Promise((resolve) => { resolveSave = resolve; }));
  await panel.open();
  const save = panel.submit("settings-form");
  panel.unmount();
  assert.equal(panel.requests.at(-1)[1].signal.aborted, true);
  resolveSave(Response.json(fixture())); await save;
  assert.equal(panel.saved(), 0);
  assert.deepEqual(panel.navigations, []);
});

test("expired settings session clears editor and navigates to the current portal login", async () => {
  const panel = component(async () => new Response(null, { status: 401 }));
  await panel.open();
  assert.deepEqual(panel.navigations, ["/login/seller"]);
  assert.equal(panel.byId("client"), undefined);
  panel.unmount();
});
