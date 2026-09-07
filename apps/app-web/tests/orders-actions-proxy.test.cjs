const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("typescript");

const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const selectionId = "a3000000-0000-4000-8000-000000000001";
const lineId = "a4000000-0000-4000-8000-000000000001";
const filters = { search: "", statuses: null, storefronts: null, currencies: null, carriers: [],
  tracking: "all", commission: "all", date_from: null, date_to: null, amount_min: null, amount_max: null };
const input = (extra = {}) => ({ account_id: accountId, environment: "live", selection_id: selectionId, filters, ...extra });
const summary = { selected_rows: 1, distinct_orders: 1, quantity: 1, cancelled_rows: 0,
  sale_amount_eur: "100.00", commission_amount_eur: "10.00", payout_amount_eur: "90.00",
  purchase_cost_eur: "50.00", profit_amount_eur: "40.00", profit_pct: "80.00",
  complete_economic_rows: 1, missing_economic_rows: 0, known_cost_rows: 1, missing_cost_rows: 0,
  loss_rows: 0, sku_cost_rows: 1, catalog_cost_rows: 0, missing_currencies: [] };
const selection = () => ({ id: selectionId, selected_ids: [lineId], selected_count: 1, filtered_count: 1, summary });

function load(file, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortSignal, URL, URLSearchParams, Response, ...context });
  return exports;
}
const settings = load("../app/lib/seller-settings-types.ts");
const types = load("../app/lib/orders-types.ts", { require: () => settings });
function proxy(fetchImpl) {
  class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
  return load("../app/lib/orders-actions-proxy.ts", { fetch: fetchImpl, require(name) {
    if (name === "next/server") return { NextResponse };
    if (name === "./api-url") return { apiUrl: "http://api.test" };
    if (name === "./seller-settings-types") return settings;
    if (name === "./orders-types") return types;
    throw new Error(name);
  } }).ordersActionsProxy;
}
function request(body, headers = {}, signal) {
  return { url: "https://app.test/api/orders", headers: new Headers({ cookie: "mh_session=test", origin: "https://app.test", ...headers }),
    signal, json: async () => body };
}

test("selection forwards only validated scope and verifies the returned selection identity", async () => {
  let calls = 0;
  const run = proxy(async (url, options) => {
    calls++;
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/selection`);
    assert.equal(options.method, "POST");
    assert.equal(options.headers.cookie, "mh_session=test");
    assert.equal(options.cache, "no-store");
    assert.equal(options.redirect, "error");
    assert.equal(JSON.parse(options.body).selection_id, selectionId);
    assert.equal(JSON.parse(options.body).line_id, lineId);
    return Response.json({ selection: { ...selection(), private_payload: "must-not-escape" } });
  });
  const result = await run(request(input({ action: "set", line_id: lineId, selected: false })), sellerId, "selection");
  assert.equal(result.status, 200);
  assert.equal(calls, 1);
  assert.equal((await result.text()).includes("must-not-escape"), false);
  const bad = proxy(async () => Response.json({ selection: { ...selection(), id: accountId } }));
  assert.equal((await bad(request(input({ action: "clear" })), sellerId, "selection")).status, 502);
});

test("selection and export reject foreign origins, forged paths, missing sessions and invalid input before network", async () => {
  let calls = 0;
  const run = proxy(async () => { calls++; throw new Error(); });
  for (const operation of ["selection", "export"]) {
    const body = input(operation === "selection" ? { action: "clear" } : { kind: "filtered" });
    assert.equal((await run(request(body, { origin: "https://evil.test" }), sellerId, operation)).status, 403);
    assert.equal((await run(request(body, { "sec-fetch-site": "cross-site" }), sellerId, operation)).status, 403);
    assert.equal((await run(request(body, { host: "evil.test/app.test" }), sellerId, operation)).status, 403);
    assert.equal((await run(request(body), "../foreign", operation)).status, 422);
    assert.equal((await run(request(body, { cookie: "" }), sellerId, operation)).status, 401);
    assert.equal((await run(request({ ...body, account_id: "../bad" }), sellerId, operation)).status, 422);
    assert.equal((await run(request({ ...body, selection_id: "../bad" }), sellerId, operation)).status, 422);
  }
  assert.equal(calls, 0);
});

test("CSV is streamed intact with BOM and controlled download headers, without forwarding upstream private headers", async () => {
  const bytes = new TextEncoder().encode('\uFEFFOrdine,Prodotto,Quantità\r\nA1,"Caffè, gusto intenso",2\r\n');
  const run = proxy(async (url, options) => {
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/export`);
    assert.equal(JSON.parse(options.body).kind, "filtered");
    assert.ok(options.signal instanceof AbortSignal);
    return new Response(new ReadableStream({ start(controller) { controller.enqueue(bytes.slice(0, 8)); controller.enqueue(bytes.slice(8)); controller.close(); } }), {
      headers: { "content-type": "text/csv; charset=utf-8", "content-disposition": 'attachment; filename="private-token.csv"', "set-cookie": "private-session", "x-private": "secret" },
    });
  });
  const result = await run(request(input({ kind: "filtered" })), sellerId, "export");
  assert.equal(result.status, 200);
  assert.deepEqual(new Uint8Array(await result.arrayBuffer()), bytes);
  assert.equal(result.headers.get("content-disposition"), 'attachment; filename="ordini_filtrati_live.csv"');
  assert.equal(result.headers.get("cache-control"), "no-store");
  assert.equal(result.headers.get("x-content-type-options"), "nosniff");
  assert.equal(result.headers.has("set-cookie"), false);
  assert.equal(result.headers.has("x-private"), false);
});

test("invalid CSV responses and upstream failures never become successful downloaded files or leak their bodies", async () => {
  const body = input({ kind: "selected" });
  for (const contentType of ["text/html", "application/json", "text/csv-invented"]) {
    const run = proxy(async () => new Response("secret-upstream", { headers: { "content-type": contentType } }));
    const result = await run(request(body), sellerId, "export");
    assert.equal(result.status, 502);
    assert.equal((await result.text()).includes("secret-upstream"), false);
  }
  for (const [status, expected] of [[401, 401], [403, 403], [404, 404], [422, 422], [500, 503], [502, 503]]) {
    const run = proxy(async () => new Response("secret-upstream", { status }));
    const result = await run(request(body), sellerId, "export");
    assert.equal(result.status, expected);
    assert.equal((await result.text()).includes("secret-upstream"), false);
  }
  const offline = proxy(async () => { throw new Error("private-address"); });
  assert.equal((await offline(request(body), sellerId, "export")).status, 503);
});

test("client cancellation propagates to the API stream request", async () => {
  const controller = new AbortController();
  const run = proxy(async (_url, options) => {
    assert.equal(options.signal.aborted, false);
    controller.abort();
    assert.equal(options.signal.aborted, true);
    throw new Error("cancelled");
  });
  assert.equal((await run(request(input({ kind: "selected" }), {}, controller.signal), sellerId, "export")).status, 503);
});
