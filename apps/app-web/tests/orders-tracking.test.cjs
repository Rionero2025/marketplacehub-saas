const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto").webcrypto;
const ts = require("typescript");
const { File } = require("node:buffer");

const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const lineId = "a4000000-0000-4000-8000-000000000001";
const scope = { account_id: accountId, environment: "live" };
const capabilities = { marketplace: "kaufland", formats: ["csv", "xlsx", "xls"], max_bytes: 5242880, max_rows: 10000, max_columns: 100, max_cells: 200000, max_cell_length: 2000, preview_rows: 10, can_import: true, can_edit: true };
const preview = { file_name: "tracking.csv", row_count: 2, columns: ["Order ID", "Carrier", "Tracking"], rows: [
  { "Order ID": "ORDER-1", Carrier: "DPD", Tracking: "TRACK-1" }, { "Order ID": "ORDER-2", Carrier: "DHL", Tracking: "TRACK-2" },
], detected: { id_order: "Order ID", carrier_code: "Carrier", tracking_numbers: "Tracking" } };
const mapping = { id_order_unit: "", id_order: "Order ID", carrier_code: "Carrier", tracking_numbers: "Tracking", combined_shipment: "" };
const item = () => ({ id: lineId, marketplace: "kaufland", external_line_id: "UNIT-1", order_id: "ORDER-1", status: "sent", status_label: "Spedito",
  storefront: "de", created_at: "2026-09-07T08:30:00Z", product_name: "Prodotto", ean: "1234567890123", sku: "AB_123", quantity: 1, currency: "EUR",
  sale_amount: "60", shipping_amount: "0", commission_amount: "5", commission_rate: "8.33", payout_amount: "55", purchase_cost: "40", profit_amount: "15", profit_pct: "37.5",
  sale_amount_eur: "60", shipping_amount_eur: "0", commission_amount_eur: "5", payout_amount_eur: "55", purchase_cost_eur: "40", profit_amount_eur: "15",
  purchase_cost_source: "SKU", monetary_warnings: [], details: { carrier: "DPD", tracking: "TRACK-1" } });

function load(file, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, Blob, File, FormData, Headers, Request, Response, TextDecoder, TextEncoder, Uint8Array, URL, URLSearchParams, crypto, ...context });
  return exports;
}

const settings = load("../app/lib/seller-settings-types.ts");
const orders = load("../app/lib/orders-types.ts", { require: () => settings });
const types = load("../app/lib/orders-tracking-types.ts");

test("tracking file operations allow the documented dense spreadsheet processing window", () => {
  assert.equal(types.TRACKING_FILE_OPERATION_TIMEOUT_MS, 120_000);
  assert.equal(types.TRACKING_UI_TIMEOUT_GRACE_MS, 5_000);
  assert.equal(types.TRACKING_BODY_READ_TIMEOUT_MS, 60_000);
  assert.equal(types.TRACKING_PREFLIGHT_TIMEOUT_MS, 30_000);
  assert.equal(types.TRACKING_UI_FILE_OPERATION_TIMEOUT_MS, 215_000);
});
function proxy(fetchImpl) {
  class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
  return load("../app/lib/orders-tracking-proxy.ts", { fetch: fetchImpl, require(name) {
    if (name === "next/server") return { NextResponse };
    if (name === "./api-url") return { apiUrl: "http://api.test" };
    if (name === "./seller-settings-types") return settings;
    if (name === "./orders-types") return orders;
    if (name === "./orders-tracking-types") return types;
    throw new Error(name);
  } }).ordersTrackingProxy;
}
function request(operation, { headers = {}, form = null, json = null } = {}) {
  const query = operation === "manual" ? "" : `?account_id=${accountId}&environment=live`;
  const method = operation === "capabilities" ? "GET" : operation === "manual" ? "PATCH" : "POST";
  const body = form ?? (json === null ? undefined : JSON.stringify(json));
  return new Request(`https://app.test/api/sellers/${sellerId}/orders/tracking/${operation}${query}`, {
    method, headers: { cookie: "mh_session=test", origin: "https://app.test",
      ...(json === null ? {} : { "content-type": "application/json" }), ...headers }, body,
  });
}
function csvFile(content = "Order ID,Carrier,Tracking\r\nORDER-1,DPD,TRACK-1\r\n") {
  return new File([content], "tracking.csv", { type: "text/csv" });
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("tracking runtime DTOs preserve only the public preview, capabilities and deterministic import result", () => {
  assert.deepEqual(plain(types.readTrackingCapabilities({ ...capabilities, private: "secret" })), capabilities);
  assert.deepEqual(plain(types.readTrackingPreview({ ...preview, private: "secret" })), preview);
  const result = { updated: 3, unmatched: [{ row: 2, order_unit_id: "UNIT-X", order_id: "ORDER-X" }], invalid: [{ row: 3, error: "Tracking/corriere assente" }] };
  assert.deepEqual(plain(types.readTrackingImportResult({ ...result, raw: "secret" })), result);
  assert.equal(JSON.stringify(types.readTrackingPreview({ ...preview, private: "secret" })).includes("secret"), false);
});

test("tracking runtime rejects malformed scopes, previews, mappings, unsafe diagnostics and empty manual edits", () => {
  assert.deepEqual(plain(types.readTrackingScope(new URLSearchParams(`account_id=${accountId}&environment=live`))), scope);
  for (const params of ["account_id=../foreign", `account_id=${accountId}&environment=prod`]) assert.equal(types.readTrackingScope(new URLSearchParams(params)), null);
  assert.deepEqual(plain(types.readTrackingMapping(mapping, preview.columns)), mapping);
  for (const value of [{ ...mapping, id_order: "" }, { ...mapping, tracking_numbers: "", carrier_code: "" }, { ...mapping, id_order: "Missing" }, { ...mapping, private: "ignored", carrier_code: 12 }]) assert.equal(types.readTrackingMapping(value, preview.columns), null);
  assert.equal(types.readTrackingPreview({ ...preview, rows: [...preview.rows, ...Array(9).fill(preview.rows[0])] }), null);
  assert.equal(types.readTrackingPreview({ ...preview, detected: { id_order: "Missing" } }), null);
  assert.equal(types.readTrackingImportResult({ updated: 1, unmatched: [], invalid: [{ row: 1, error: "database secret" }] }), null);
  assert.equal(types.readTrackingManualInput({ ...scope, line_id: lineId, carrier: " ", tracking: " " }), null);
  assert.deepEqual(plain(types.readTrackingManualInput({ ...scope, line_id: lineId, carrier: " DPD ", tracking: " TRACK-1 " })), { ...scope, line_id: lineId, carrier: "DPD", tracking: "TRACK-1" });
});

test("tracking runtime enforces the same column, cell and updated-unit caps as the backend", () => {
  const columns = Array.from({ length: 101 }, (_value, index) => `Column ${index}`);
  assert.equal(types.readTrackingPreview({ ...preview, columns, rows: [], detected: {} }), null);
  assert.equal(types.readTrackingPreview({ ...preview, rows: [{ "Order ID": "x".repeat(2_001), Carrier: "", Tracking: "" }] }), null);
  assert.deepEqual(plain(types.readTrackingImportResult({ updated: 10_000, unmatched: [], invalid: [] })), { updated: 10_000, unmatched: [], invalid: [] });
  assert.equal(types.readTrackingImportResult({ updated: 10_001, unmatched: [], invalid: [] }), null);
});

test("tracking preview safely preserves a column literally named __proto__", () => {
  const unusual = {
    ...preview,
    columns: ["__proto__"],
    rows: [JSON.parse('{"__proto__":"TRACK-001"}')],
    detected: {},
  };
  const parsed = types.readTrackingPreview(unusual);
  assert.equal(parsed.rows[0]["__proto__"], "TRACK-001");
  assert.equal(Object.getPrototypeOf(parsed.rows[0]), null);
  assert.equal(Object.hasOwn(plain(parsed.rows[0]), "__proto__"), true);
});

test("capabilities BFF forwards the exact authenticated Seller scope and validates the feature contract", async () => {
  const run = proxy(async (url, options) => {
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/tracking/capabilities?account_id=${accountId}&environment=live`);
    assert.equal(options.method, "GET"); assert.equal(options.headers.cookie, "mh_session=test");
    assert.equal(options.cache, "no-store"); assert.equal(options.redirect, "error"); assert.ok(options.signal instanceof AbortSignal);
    return Response.json({ capabilities: { ...capabilities, private: "secret" } });
  });
  const response = await run(request("capabilities"), sellerId, "capabilities");
  assert.equal(response.status, 200); assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.text()).includes("secret"), false);
});

test("preview BFF verifies file extension, MIME and CSV content then returns at most ten public rows", async () => {
  const form = new FormData(); form.set("file", csvFile());
  const run = proxy(async (url, options) => {
    if (url.includes("/tracking/capabilities?")) return Response.json({ capabilities });
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/tracking/preview?account_id=${accountId}&environment=live`);
    assert.equal(options.method, "POST"); assert.equal(options.headers["content-type"], undefined);
    assert.equal(options.body.get("private"), null); assert.equal(options.body.get("file").name, "tracking.csv");
    return Response.json({ preview: { ...preview, private: "secret" } });
  });
  const response = await run(request("preview", { form }), sellerId, "preview");
  assert.equal(response.status, 200); assert.deepEqual((await response.json()).preview.rows, preview.rows);
});

test("import BFF forwards one validated file and sanitized five-field mapping", async () => {
  const form = new FormData(); form.set("file", csvFile()); form.set("mapping", JSON.stringify({ ...mapping, api_key: "secret" }));
  const result = { updated: 2, unmatched: [], invalid: [] };
  let calls = 0;
  const run = proxy(async (url, options) => {
    if (url.includes("/tracking/capabilities?")) return Response.json({ capabilities });
    calls++; assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/tracking/import?account_id=${accountId}&environment=live`);
    assert.deepEqual(JSON.parse(options.body.get("mapping")), mapping);
    assert.equal(options.body.get("file").name, "tracking.csv");
    return Response.json({ result });
  });
  const response = await run(request("import", { form }), sellerId, "import");
  assert.equal(response.status, 200); assert.equal(calls, 1); assert.deepEqual(await response.json(), { result });
});

test("manual BFF trims values, checks the returned line and keeps credentials outside the response", async () => {
  const input = { ...scope, line_id: lineId, carrier: " DPD ", tracking: " TRACK-1 ", api_key: "secret" };
  const run = proxy(async (url, options) => {
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/tracking/manual`);
    assert.equal(options.method, "PATCH"); assert.deepEqual(JSON.parse(options.body), { ...scope, line_id: lineId, carrier: "DPD", tracking: "TRACK-1" });
    return Response.json({ updated: 1, item: { ...item(), raw: "secret" }, private: "secret" });
  });
  const response = await run(request("manual", { json: input }), sellerId, "manual");
  assert.equal(response.status, 200); const body = await response.text(); assert.equal(body.includes("secret"), false);
});

test("tracking writes reject missing sessions, forged origins, paths, files, mappings and empty edits before upstream", async () => {
  let calls = 0; const run = proxy(async (url) => {
    if (url.includes("/tracking/capabilities?")) return Response.json({ capabilities });
    calls++; throw new Error();
  });
  const validForm = new FormData(); validForm.set("file", csvFile());
  assert.equal((await run(request("preview", { headers: { cookie: "" }, form: validForm }), sellerId, "preview")).status, 401);
  assert.equal((await run(request("preview", { headers: { origin: "https://evil.test" }, form: validForm }), sellerId, "preview")).status, 403);
  assert.equal((await run(request("preview", { headers: { origin: "" }, form: validForm }), sellerId, "preview")).status, 403);
  assert.equal((await run(request("preview", { form: validForm }), "../foreign", "preview")).status, 422);
  const badName = new FormData(); badName.set("file", new File(["a,b"], "tracking.exe", { type: "text/csv" }));
  assert.equal((await run(request("preview", { form: badName }), sellerId, "preview")).status, 422);
  const html = new FormData(); html.set("file", csvFile("<!doctype html><p>private</p>"));
  assert.equal((await run(request("preview", { form: html }), sellerId, "preview")).status, 422);
  const fakeXlsx = new FormData(); fakeXlsx.set("file", new File(["not-a-zip"], "tracking.xlsx", { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
  assert.equal((await run(request("preview", { form: fakeXlsx }), sellerId, "preview")).status, 422);
  const fakeXls = new FormData(); fakeXls.set("file", new File(["not-an-ole-file"], "tracking.xls", { type: "application/vnd.ms-excel" }));
  assert.equal((await run(request("preview", { form: fakeXls }), sellerId, "preview")).status, 422);
  const wrongMime = new FormData(); wrongMime.set("file", new File(["a,b\r\n1,2"], "tracking.csv", { type: "application/pdf" }));
  assert.equal((await run(request("preview", { form: wrongMime }), sellerId, "preview")).status, 422);
  const tooLarge = new FormData(); tooLarge.set("file", new File([new Uint8Array(5_242_881)], "tracking.csv", { type: "text/csv" }));
  assert.equal((await run(request("preview", { form: tooLarge }), sellerId, "preview")).status, 413);
  const duplicate = new FormData(); duplicate.append("file", csvFile()); duplicate.append("file", csvFile());
  assert.equal((await run(request("preview", { form: duplicate }), sellerId, "preview")).status, 422);
  const unknown = new FormData(); unknown.set("file", csvFile()); unknown.set("private", "must-not-parse");
  assert.equal((await run(request("preview", { form: unknown }), sellerId, "preview")).status, 422);
  const hugeManual = { ...scope, line_id: lineId, carrier: "x".repeat(9_000), tracking: "" };
  assert.equal((await run(request("manual", { json: hugeManual }), sellerId, "manual")).status, 413);
  const malformed = new FormData(); malformed.set("file", csvFile()); malformed.set("mapping", "{bad-json");
  assert.equal((await run(request("import", { form: malformed }), sellerId, "import")).status, 422);
  assert.equal((await run(request("manual", { json: { ...scope, line_id: lineId, carrier: "", tracking: "" } }), sellerId, "manual")).status, 422);
  assert.equal(calls, 0);
});

test("tracking upload preflight rejects a forged cookie before consuming the multipart body", async () => {
  const form = new FormData(); form.set("file", csvFile("Order ID,Tracking\r\nORDER-1,TRACK-1\r\n"));
  const incoming = request("preview", { headers: { cookie: "mh_session=forged" }, form });
  let calls = 0;
  const run = proxy(async (url, options) => {
    calls++;
    assert.match(url, /\/tracking\/capabilities\?/);
    assert.equal(options.method, "GET");
    return new Response(null, { status: 401 });
  });
  const response = await run(incoming, sellerId, "preview");
  assert.equal(response.status, 401);
  assert.equal(incoming.bodyUsed, false);
  assert.equal(calls, 1);
});

test("web admission rejects same-account and excess uploads before materializing their bodies", async () => {
  const accountTwo = "a2000000-0000-4000-8000-000000000002";
  const accountThree = "a2000000-0000-4000-8000-000000000003";
  const pending = [];
  let actualCalls = 0;
  const run = proxy(async (url) => {
    if (url.includes("/tracking/capabilities?")) return Response.json({ capabilities });
    actualCalls++;
    return new Promise((resolve) => pending.push(resolve));
  });
  const scopedPreview = (account) => {
    const form = new FormData(); form.set("file", csvFile());
    return new Request(`https://app.test/api/sellers/${sellerId}/orders/tracking/preview?account_id=${account}&environment=live`, {
      method: "POST", headers: { cookie: "mh_session=test", origin: "https://app.test" }, body: form,
    });
  };
  const waitForActualCalls = async (expected) => {
    while (actualCalls < expected) await new Promise(setImmediate);
  };

  const firstRequest = scopedPreview(accountId);
  const first = run(firstRequest, sellerId, "preview");
  await waitForActualCalls(1);
  const sameAccount = scopedPreview(accountId);
  const sameResponse = await run(sameAccount, sellerId, "preview");
  assert.equal(sameResponse.status, 409); assert.equal(sameAccount.bodyUsed, false);

  const secondRequest = scopedPreview(accountTwo);
  const second = run(secondRequest, sellerId, "preview");
  await waitForActualCalls(2);
  const excess = scopedPreview(accountThree);
  const excessResponse = await run(excess, sellerId, "preview");
  assert.equal(excessResponse.status, 429); assert.equal(excessResponse.headers.get("retry-after"), "5");
  assert.equal(excess.bodyUsed, false);

  for (const resolve of pending) resolve(Response.json({ preview }));
  assert.deepEqual((await Promise.all([first, second])).map((response) => response.status), [200, 200]);
  assert.equal(firstRequest.bodyUsed, true); assert.equal(secondRequest.bodyUsed, true);
});

test("an interrupted slow upload releases web admission before the next file", async () => {
  let actualCalls = 0;
  const run = proxy(async (url) => {
    if (url.includes("/tracking/capabilities?")) return Response.json({ capabilities });
    actualCalls++; return Response.json({ preview });
  });
  const controller = new AbortController();
  const slow = new ReadableStream({ start() {} });
  const slowRequest = new Request(`https://app.test/api/sellers/${sellerId}/orders/tracking/preview?account_id=${accountId}&environment=live`, {
    method: "POST", headers: { cookie: "mh_session=test", origin: "https://app.test", "content-type": "multipart/form-data; boundary=slow" },
    body: slow, signal: controller.signal, duplex: "half",
  });
  const timedOut = run(slowRequest, sellerId, "preview");
  await new Promise(setImmediate); controller.abort();
  const timedOutResponse = await timedOut;
  assert.equal(timedOutResponse.status, 408); assert.equal(actualCalls, 0);

  const form = new FormData(); form.set("file", csvFile());
  const nextResponse = await run(request("preview", { form }), sellerId, "preview");
  assert.equal(nextResponse.status, 200); assert.equal(actualCalls, 1);
});

test("tracking upstream failures use operation-specific safe errors and manual 404 stays distinguishable", async () => {
  const manual = { ...scope, line_id: lineId, carrier: "DPD", tracking: "" };
  for (const [status, expected] of [[401, 401], [403, 403], [404, 404], [408, 408], [409, 409], [429, 429], [422, 422], [500, 503]]) {
    const run = proxy(async () => new Response("database-password=secret", { status, headers: status === 500 ? { "x-request-id": "req-safe-123" } : {} }));
    const response = await run(request("manual", { json: manual }), sellerId, "manual");
    assert.equal(response.status, expected); const body = await response.text(); assert.equal(body.includes("secret"), false);
    if (status === 404) assert.match(body, /unità ordine non è presente/);
    if (status === 408) assert.match(body, /Tempo massimo di caricamento superato/);
    if (status === 409) assert.match(body, /operazione tracking è già in corso/);
    if (status === 429) { assert.match(body, /servizio di importazione tracking è occupato/); assert.equal(response.headers.get("retry-after"), "5"); }
    if (status === 500) { assert.match(body, /req-safe-123/); assert.equal(response.headers.get("x-request-id"), "req-safe-123"); }
  }
  const wrongLine = proxy(async () => Response.json({ updated: 1, item: { ...item(), id: accountId } }));
  assert.equal((await wrongLine(request("manual", { json: manual }), sellerId, "manual")).status, 502);
  const unsafeReference = proxy(async () => new Response("private", { status: 500, headers: { "x-request-id": "private request id" } }));
  const unsafeResponse = await unsafeReference(request("manual", { json: manual }), sellerId, "manual");
  const safeReplacement = unsafeResponse.headers.get("x-request-id");
  assert.match(safeReplacement, /^web-[0-9a-f-]{36}$/i); assert.equal((await unsafeResponse.text()).includes("private request"), false);
  const networkFailure = proxy(async () => { throw new Error("private network failure"); });
  const networkResponse = await networkFailure(request("manual", { json: manual }), sellerId, "manual");
  const networkBody = await networkResponse.json();
  assert.equal(networkResponse.status, 503); assert.match(networkBody.request_id, /^web-[0-9a-f-]{36}$/i);
  assert.equal(networkResponse.headers.get("x-request-id"), networkBody.request_id);
});

function trackingComponent(fetchImpl) {
  const states = [], refs = [], effects = [], callbacks = [], timers = new Map(), requests = [];
  let stateIndex, refIndex, effectIndex, callbackIndex, pendingEffects, tree, timerId = 0, mutations = 0;
  const rows = [item()];
  const nodes = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(nodes) : [node, ...nodes(node.props?.children)];
  const text = (node) => typeof node === "string" ? node : typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  const element = (type, props) => typeof type === "function" ? type(props) : ({ type, props });
  const onBusyChange = () => {};
  const onMutated = async () => { mutations++; };
  const onResponse = () => true;
  const module = load("../app/components/OrderTrackingPanel.tsx", {
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout: (id) => timers.delete(id),
    fetch: async (...args) => { requests.push(args); return fetchImpl(...args); }, require(name) {
      if (name === "react") return {
        useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial; return [states[index], (next) => { states[index] = typeof next === "function" ? next(states[index]) : next; }]; },
        useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
        useEffect(create, dependencies) { const index = effectIndex++; if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) pendingEffects.push(() => { effects[index]?.cleanup?.(); effects[index] = { dependencies, cleanup: create() }; }); },
        useCallback(fn, dependencies) { const index = callbackIndex++; if (!callbacks[index] || dependencies.some((value, position) => value !== callbacks[index].dependencies[position])) callbacks[index] = { dependencies, fn }; return callbacks[index].fn; },
      };
      if (name === "react/jsx-runtime") return { jsx: element, jsxs: element };
      if (name === "../lib/orders-tracking-types") return types;
      if (name === "./DashboardIcon") return { DashboardIcon: () => null };
      if (name.endsWith(".module.css")) return { default: new Proxy({}, { get: (_target, key) => key }) };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  function render() { stateIndex = refIndex = effectIndex = callbackIndex = 0; pendingEffects = []; tree = module.OrderTrackingPanel({ sellerId, accountId, environment: "live", orders: rows, disabled: false, onBusyChange, onMutated, onResponse }); pendingEffects.forEach((effect) => effect()); }
  render();
  return {
    render, requests, text: () => text(tree), nodes: () => nodes(tree), mutations: () => mutations,
    find: (predicate) => nodes(tree).find(predicate),
    button: (label) => nodes(tree).find((node) => node.type === "button" && text(node) === label),
    async settle() { await new Promise(setImmediate); render(); await new Promise(setImmediate); render(); },
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}

test("tracking UI detects mappings, previews ten-row data and refreshes immediately after a confirmed import", async () => {
  const result = { updated: 2, unmatched: [{ row: 2, order_unit_id: "UNKNOWN", order_id: "" }], invalid: [{ row: 3, error: "Tracking/corriere assente" }] };
  const panel = trackingComponent(async (url, options = {}) => {
    if (url.includes("/capabilities?")) return Response.json({ capabilities });
    if (url.includes("/preview?")) { assert.equal(options.body.get("file").name, "tracking.csv"); return Response.json({ preview }); }
    if (url.includes("/import?")) { assert.deepEqual(JSON.parse(options.body.get("mapping")), mapping); return Response.json({ result }); }
    throw new Error(url);
  });
  await panel.settle(); assert.match(panel.text(), /Recupera o completa corriere e tracking/);
  const input = panel.find((node) => node.type === "input" && node.props.type === "file");
  input.props.onChange({ target: { files: [csvFile()] } }); await panel.settle();
  assert.match(panel.text(), /File letto: tracking.csv · 2 righe/);
  const selects = panel.nodes().filter((node) => node.type === "select");
  assert.deepEqual(selects.slice(0, 5).map((select) => select.props.value), ["", "Order ID", "Carrier", "Tracking", ""]);
  panel.button("Importa tracking nell’archivio").props.onClick(); await panel.settle();
  assert.equal(panel.mutations(), 1); assert.match(panel.text(), /Aggiornate 2 unità ordine/);
  assert.match(panel.text(), /1 righe non corrispondono/); assert.match(panel.text(), /1 righe non contengono dati sufficienti/);
  panel.unmount();
});

test("tracking UI preloads the visible order, saves a manual correction and makes success observable", async () => {
  const panel = trackingComponent(async (url, options = {}) => {
    if (url.includes("/capabilities?")) return Response.json({ capabilities });
    if (url.endsWith("/manual")) {
      const body = JSON.parse(options.body); assert.equal(body.line_id, lineId); assert.equal(body.carrier, "GLS"); assert.equal(body.tracking, "NEW-TRACK");
      return Response.json({ updated: 1, item: { ...item(), details: { carrier: "GLS", tracking: "NEW-TRACK" } } });
    }
    throw new Error(url);
  });
  await panel.settle();
  const carrierInput = panel.find((node) => node.type === "input" && node.props.placeholder === "Esempio: DPD");
  const trackingInput = panel.find((node) => node.type === "input" && node.props.placeholder?.startsWith("Esempio: 084"));
  carrierInput.props.onChange({ target: { value: "GLS" } }); trackingInput.props.onChange({ target: { value: "NEW-TRACK" } }); panel.render();
  panel.button("Salva corriere e tracking").props.onClick(); await panel.settle();
  assert.equal(panel.mutations(), 1); assert.match(panel.text(), /Corriere e tracking salvati nell’archivio/);
  panel.unmount();
});

test("tracking UI exposes a safe request reference when a write outcome is uncertain", async () => {
  const panel = trackingComponent(async (url) => {
    if (url.includes("/capabilities?")) return Response.json({ capabilities });
    if (url.endsWith("/manual")) return Response.json({
      detail: "Operazione tracking non confermata. Aggiorna gli ordini e riprova.",
      request_id: "web-123e4567-e89b-42d3-a456-426614174000",
    }, { status: 503 });
    throw new Error(url);
  });
  await panel.settle();
  panel.button("Salva corriere e tracking").props.onClick(); await panel.settle();
  assert.match(panel.text(), /Aggiorna gli ordini e riprova/);
  assert.match(panel.text(), /Riferimento: web-123e4567-e89b-42d3-a456-426614174000/);
  assert.equal(panel.mutations(), 0);
  panel.unmount();
});

test("tracking UI requires a refresh before retrying an import with an uncertain network outcome", async () => {
  const panel = trackingComponent(async (url) => {
    if (url.includes("/capabilities?")) return Response.json({ capabilities });
    if (url.includes("/preview?")) return Response.json({ preview });
    if (url.includes("/import?")) throw new Error("private network failure");
    throw new Error(url);
  });
  await panel.settle();
  const input = panel.find((node) => node.type === "input" && node.props.type === "file");
  input.props.onChange({ target: { files: [csvFile()] } }); await panel.settle();
  panel.button("Importa tracking nell’archivio").props.onClick(); await panel.settle();
  assert.match(panel.text(), /Importazione non confermata\. Ricarica la pagina prima di riprovare\./);
  assert.match(panel.text(), /modifiche tracking restano bloccate/);
  assert.equal(panel.button("Importa tracking nell’archivio").props.disabled, true);
  assert.equal(panel.mutations(), 0);
  panel.unmount();
});
