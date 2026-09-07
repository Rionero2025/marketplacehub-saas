const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const otherSellerId = "a1000000-0000-4000-8000-000000000002";
const entry = () => ({ id: accountId, marketplace: "kaufland", account_name: "Kaufland principale", active: true,
  connection_status: "unverified", last_checked_at: null, public_name: null, external_shop_id: null, storefronts: [], credential_mask: "••••••••abcd", error_code: null });
const fixture = (accounts = [entry()]) => ({ seller_id: sellerId, can_manage: true, accounts });
const input = () => ({ marketplace: "kaufland", account_name: "Kaufland principale", credentials: { client_key: "client-test", secret_key: "secret-test" } });
const plain = (value) => JSON.parse(JSON.stringify(value));
function load(file, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, setTimeout, clearTimeout, URL, ...context });
  return exports;
}
const settings = load("../app/lib/seller-settings-types.ts");
const types = load("../app/lib/marketplace-connections-types.ts", { require: () => settings });
const catalog = load("../app/lib/marketplace-catalog.ts");
const request = (payload, headers = {}) => ({ url: "https://app.test/api/connections", headers: new Headers({ cookie: "mh_session=existing", ...headers }), json: async () => payload });
function proxy(fetchImpl) {
  class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
  return load("../app/lib/marketplace-connections-proxy.ts", { fetch: fetchImpl, require(name) {
    if (name === "next/server") return { NextResponse };
    if (name === "./api-url") return { apiUrl: "http://api.test" };
    if (name === "./seller-settings-types") return settings;
    if (name === "./marketplace-connections-types") return types;
    throw new Error(name);
  } }).marketplaceConnectionsProxy;
}

test("directory separates implemented connectors from planned marketplaces and filters their names/categories", () => {
  assert.equal(catalog.marketplaceCatalog.length, 28);
  assert.equal(new Set(catalog.marketplaceCatalog.map((item) => item.code)).size, 28);
  assert.deepEqual(plain(catalog.filterMarketplaces("", "available", "all").map((item) => item.code)), ["kaufland", "worten"]);
  assert.equal(catalog.filterMarketplaces(" AMAZON ", "planned", "all")[0].code, "amazon");
  assert.equal(catalog.filterMarketplaces("amazon", "available", "all").length, 0);
  assert.equal(catalog.filterMarketplaces("", "available", "Elettronica")[0].code, "worten");
});

test("credentials have connector-specific requirements, fixed Worten endpoint and no arbitrary fields", () => {
  assert.equal(types.readConnectionInput({ ...input(), credentials: { client_key: "x", secret_key: " " } }), null);
  assert.equal(types.readConnectionInput({ ...input(), marketplace: "amazon" }), null);
  const worten = { marketplace: "worten", account_name: " Worten ", credentials: { api_key: " key ", shop_id: " 123 ", api_url: types.WORTEN_API_URL, secret_key: "drop" }, seller_id: otherSellerId };
  assert.deepEqual(plain(types.readConnectionInput(worten)), { marketplace: "worten", account_name: "Worten", credentials: { api_key: "key", shop_id: "123", api_url: types.WORTEN_API_URL } });
  assert.equal(types.readConnectionInput({ ...worten, credentials: { ...worten.credentials, api_url: "https://attacker.test/api" } }), null);
});

test("public connection DTO validates Seller, dates, masks, statuses and strips every unknown credential field", () => {
  const source = fixture([{ ...entry(), api_key: "never-echo", credentials: { secret_key: "never-echo" } }]);
  assert.equal(JSON.stringify(types.readConnections(source, sellerId)).includes("never-echo"), false);
  for (const account of [{ ...entry(), credential_mask: "raw-secret" }, { ...entry(), connection_status: "maybe" }, { ...entry(), last_checked_at: "yesterday" }, { ...entry(), error_code: "raw-secret" }]) assert.equal(types.readConnections(fixture([account]), sellerId), null);
  assert.equal(types.readConnections({ ...source, seller_id: otherSellerId }, sellerId), null);
  assert.equal(types.readConnections(fixture([entry(), entry()]), sellerId), null);
});

test("BFF scopes each operation to fixed Seller/account routes, forwards session and uses no-store", async () => {
  for (const [operation, method, suffix, payload, status] of [["read", "GET", "", undefined, 200], ["connect", "POST", "", input(), 201], ["verify", "POST", `/${accountId}/verify`, undefined, 200], ["delete", "DELETE", `/${accountId}`, { confirmation: "ELIMINA" }, 200]]) {
    const run = proxy(async (url, options) => {
      assert.equal(url, `http://api.test/v1/sellers/${sellerId}/marketplace-connections${suffix}`);
      assert.equal(options.method, method); assert.equal(options.headers.cookie, "mh_session=existing"); assert.equal(options.cache, "no-store"); assert.ok(options.signal instanceof AbortSignal);
      return Response.json(fixture());
    });
    const response = await run(request(payload), sellerId, operation, suffix ? accountId : undefined);
    assert.equal(response.status, status); assert.equal(response.headers.get("cache-control"), "no-store");
  }
});

test("BFF blocks path injection, unauthenticated writes, cross-origin, unavailable connector and loose deletion", async () => {
  let calls = 0; const run = proxy(async () => { calls++; throw new Error("must not call"); });
  assert.equal((await run(request(input()), "../foreign", "connect")).status, 422);
  assert.equal((await run(request(), sellerId, "verify", "../../account")).status, 422);
  assert.equal((await run(request(input(), { cookie: "" }), sellerId, "connect")).status, 401);
  assert.equal((await run(request(input(), { origin: "https://other.test" }), sellerId, "connect")).status, 403);
  assert.equal((await run(request({ ...input(), marketplace: "amazon" }), sellerId, "connect")).status, 422);
  assert.equal((await run(request({ confirmation: " elimina " }), sellerId, "delete", accountId)).status, 422);
  assert.equal(calls, 0);
});

test("BFF handles Render's public Host and cannot be redirected by forwarded-host or credential URL", async () => {
  const calls = []; const run = proxy(async (url, options) => { calls.push([url, options]); return Response.json(fixture()); });
  const proxied = { ...request({ ...input(), api_url: "https://attacker.test", credentials: { ...input().credentials, api_url: "https://attacker.test" } }, { host: "app.test", origin: "https://app.test" }), url: "https://localhost:10000/api/connections" };
  assert.equal((await run(proxied, sellerId, "connect")).status, 201);
  assert.equal(calls[0][0], `http://api.test/v1/sellers/${sellerId}/marketplace-connections`);
  assert.equal(calls[0][1].body.includes("attacker"), false);
  assert.equal((await run({ ...proxied, headers: new Headers({ cookie: "yes", host: "app.test", origin: "https://attacker.test", "x-forwarded-host": "attacker.test" }) }, sellerId, "connect")).status, 403);
});

test("BFF exposes only known error codes and sanitized text, including malformed success DTOs", async () => {
  for (const [make, status, code] of [[() => Response.json({ detail: { code: "invalid_credentials", message: "secret-test" } }, { status: 422 }), 422, "invalid_credentials"], [() => Response.json({ detail: "secret-test" }, { status: 500 }), 503], [() => Response.json({ ...fixture(), seller_id: otherSellerId }), 502], [() => { throw new Error("secret-test"); }, 503]]) {
    const response = await proxy(async () => make())(request(input()), sellerId, "connect");
    const value = await response.json(); assert.equal(response.status, status); assert.equal(JSON.stringify(value).includes("secret-test"), false); assert.equal(value.error_code, code);
  }
});

test("Next routes resolve asynchronous params and route connect, read, verify and delete to the shared proxy", async () => {
  const calls = [];
  for (const [file, method, operation] of [["../app/api/sellers/[sellerId]/marketplace-connections/route.ts", "GET", "read"], ["../app/api/sellers/[sellerId]/marketplace-connections/route.ts", "POST", "connect"], ["../app/api/sellers/[sellerId]/marketplace-connections/[accountId]/verify/route.ts", "POST", "verify"], ["../app/api/sellers/[sellerId]/marketplace-connections/[accountId]/route.ts", "DELETE", "delete"]]) {
    const route = load(file, { require: () => ({ marketplaceConnectionsProxy: (...args) => { calls.push(args); return "ok"; } }) });
    assert.equal(await route[method]("request", { params: Promise.resolve({ sellerId, accountId }) }), "ok");
    assert.equal(calls.at(-1)[1], sellerId); assert.equal(calls.at(-1)[2], operation);
  }
});

function component(fetchImpl) {
  const states = [], refs = [], effects = [], requests = [], navigations = [];
  let stateIndex, refIndex, effectIndex, pendingEffects, tree;
  const elements = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(elements) : [node, ...elements(node.props?.children)];
  const text = (node) => typeof node === "string" ? node : typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  const module = load("../app/components/MarketplaceConnectionsPanel.tsx", { fetch: async (...args) => { requests.push(args); return fetchImpl(...args); }, require(name) {
    if (name === "react") return {
      useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = initial; return [states[index], (value) => { states[index] = value; }]; },
      useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
      useEffect(create, dependencies) { const index = effectIndex++; if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) pendingEffects.push(() => { effects[index]?.cleanup?.(); effects[index] = { dependencies, cleanup: create() }; }); },
      useTransition: () => [false, (fn) => fn()],
    };
    if (name === "react/jsx-runtime") return { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
    if (name === "next/navigation") return { useRouter: () => ({ replace: (url) => navigations.push(url), refresh() {} }) };
    if (name === "../lib/marketplace-connections-types") return types;
    if (name === "../lib/marketplace-catalog") return catalog;
    if (name === "./DashboardIcon") return { DashboardIcon: () => null };
    throw new Error(name);
  } });
  function render() { stateIndex = refIndex = effectIndex = 0; pendingEffects = []; tree = module.MarketplaceConnectionsPanel({ sellerId, sellerName: "Negozio demo" }); pendingEffects.forEach((effect) => effect()); }
  const nodes = () => elements(tree);
  const byId = (suffix) => nodes().find((node) => node.props?.id === `marketplace-${sellerId}-${suffix}`);
  const button = (label) => nodes().find((node) => node.type === "button" && text(node) === label);
  render();
  return { render, nodes, requests, navigations, byId, button, text: () => text(tree),
    async settle() { await new Promise(setImmediate); render(); },
    choose(name) { nodes().find((node) => node.props?.["aria-label"] === `Collega ${name}`).props.onClick(); render(); },
    change(suffix, value) { byId(suffix).props.onChange({ target: { value } }); render(); },
    async submit() { await nodes().find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); render(); },
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}

test("grid allows only implemented connectors, enforces required keys and resets secrets on close", async () => {
  const panel = component(async () => Response.json(fixture([]))); await panel.settle();
  const amazon = panel.nodes().find((node) => node.props?.["aria-label"] === "Amazon: integrazione da sviluppare");
  assert.equal(amazon.props.disabled, true); amazon.props.onClick(); panel.render(); assert.equal(panel.byId("account"), undefined);
  panel.choose("Kaufland"); panel.change("client", "client-only");
  assert.equal(panel.button("Verifica e collega").props.disabled, true); await panel.submit(); assert.equal(panel.requests.length, 1);
  panel.button("Chiudi").props.onClick(); panel.render(); panel.choose("Kaufland"); assert.equal(panel.byId("client").props.value, "");
  panel.unmount();
});

test("Kaufland connect sends both credentials, clears them after confirmed verification and shows metadata", async () => {
  const panel = component(async (_url, options) => Response.json(options.method === "GET" ? fixture([]) : fixture([{ ...entry(), connection_status: "connected", public_name: "Seller API", storefronts: ["de"], last_checked_at: "2026-09-07T12:00:00Z" }])));
  await panel.settle(); panel.choose("Kaufland"); panel.change("client", " client-test "); panel.change("secret", " secret-test "); await panel.submit();
  assert.deepEqual(JSON.parse(panel.requests.at(-1)[1].body), input()); assert.equal(panel.byId("client"), undefined);
  assert.match(panel.text(), /Marketplace collegato/); assert.match(panel.text(), /Seller API/); assert.match(panel.text(), /Storefront registrati: de/);
  panel.choose("Kaufland"); assert.equal(panel.byId("secret").props.value, ""); panel.unmount();
});

test("Worten uses its own API key and shop id rather than Kaufland credentials", async () => {
  const panel = component(async (_url, options) => Response.json(options.method === "GET" ? fixture([]) : fixture([{ ...entry(), marketplace: "worten", account_name: "Worten principale", connection_status: "connected" }])));
  await panel.settle(); panel.choose("Worten"); assert.equal(panel.byId("client"), undefined);
  panel.change("api-key", " api "); panel.change("shop-id", " 123 "); await panel.submit();
  assert.deepEqual(JSON.parse(panel.requests.at(-1)[1].body), { marketplace: "worten", account_name: "Worten principale", credentials: { api_key: "api", shop_id: "123", api_url: types.WORTEN_API_URL } });
  assert.match(panel.text(), /Marketplace collegato/); panel.unmount();
});

test("read-only access keeps credential forms and account mutations inaccessible", async () => {
  const panel = component(async () => Response.json({ ...fixture(), can_manage: false })); await panel.settle();
  panel.choose("Kaufland"); assert.equal(panel.byId("client"), undefined); assert.equal(panel.button("Verifica connessione"), undefined); assert.equal(panel.button("Elimina"), undefined);
  assert.match(panel.text(), /sola lettura/); assert.equal(panel.requests.length, 1); panel.unmount();
});

test("failed connection checks show sanitized reason and never claim a saved connection", async () => {
  const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture([])) : Response.json({ error_code: "invalid_credentials", detail: "secret-test" }, { status: 422 }));
  await panel.settle(); panel.choose("Kaufland"); panel.change("client", "c"); panel.change("secret", "s"); await panel.submit();
  assert.match(panel.text(), /non riconosce le credenziali/); assert.equal(panel.text().includes("secret-test"), false); assert.equal(panel.text().includes("Marketplace collegato."), false); panel.unmount();
});

test("uncertain writes clear keys, prevent retries and require a fresh read", async () => {
  const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture([])) : new Response(null, { status: 503 }));
  await panel.settle(); panel.choose("Kaufland"); panel.change("client", "c"); panel.change("secret", "s"); await panel.submit();
  assert.equal(panel.byId("client").props.value, ""); assert.equal(panel.button("Verifica e collega").props.disabled, true); await panel.submit(); assert.equal(panel.requests.length, 2);
  await panel.button("Aggiorna dati").props.onClick(); panel.render(); panel.choose("Kaufland"); panel.change("client", "c"); panel.change("secret", "s"); assert.equal(panel.button("Verifica e collega").props.disabled, false); panel.unmount();
});

test("reverification uses only the selected saved account and shows returned failure status", async () => {
  const panel = component(async (_url, options) => Response.json(options.method === "GET" ? fixture() : fixture([{ ...entry(), connection_status: "error", error_code: "invalid_credentials" }])));
  await panel.settle(); panel.button("Verifica connessione").props.onClick(); await panel.settle();
  assert.equal(panel.requests.at(-1)[0], `/api/sellers/${sellerId}/marketplace-connections/${accountId}/verify`); assert.equal(panel.requests.at(-1)[1].body, undefined);
  assert.match(panel.text(), /Verifica non riuscita/); assert.equal(panel.text().includes("Connessione verificata."), false); panel.unmount();
});

test("deletion needs exact ELIMINA and cannot affect a different account", async () => {
  const panel = component(async (_url, options) => Response.json(options.method === "DELETE" ? fixture([]) : fixture())); await panel.settle(); panel.button("Elimina").props.onClick(); panel.render();
  panel.change("delete", "elimina"); assert.equal(panel.button("Elimina definitivamente").props.disabled, true);
  panel.change("delete", "ELIMINA"); panel.button("Elimina definitivamente").props.onClick(); await panel.settle();
  assert.equal(panel.requests.at(-1)[0], `/api/sellers/${sellerId}/marketplace-connections/${accountId}`); assert.deepEqual(JSON.parse(panel.requests.at(-1)[1].body), { confirmation: "ELIMINA" }); assert.match(panel.text(), /Collegamento eliminato/); panel.unmount();
});

test("Seller switch aborts old requests and ignores a late success", async () => {
  let resolve; const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture([])) : new Promise((done) => { resolve = done; }));
  await panel.settle(); panel.choose("Kaufland"); panel.change("client", "c"); panel.change("secret", "s"); const write = panel.submit(); panel.unmount();
  assert.equal(panel.requests.at(-1)[1].signal.aborted, true); resolve(Response.json(fixture([{ ...entry(), connection_status: "connected" }]))); await write;
  assert.equal(panel.text().includes("Marketplace collegato."), false); assert.deepEqual(panel.navigations, []);
});

test("expired or revoked session removes credentials and protected data", async () => {
  for (const status of [401, 403]) {
    const panel = component(async (_url, options) => options.method === "GET" ? Response.json(fixture([])) : new Response(null, { status }));
    await panel.settle(); panel.choose("Kaufland"); panel.change("client", "c"); panel.change("secret", "s"); await panel.submit();
    assert.equal(panel.byId("client"), undefined); assert.deepEqual(panel.navigations, status === 401 ? ["/login/seller"] : []); panel.unmount();
  }
});
