const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const crypto = require("node:crypto").webcrypto;
const ts = require("typescript");

const sellerId = "a1000000-0000-4000-8000-000000000001";
const otherSellerId = "a1000000-0000-4000-8000-000000000002";
const supplierId = "b1000000-0000-4000-8000-000000000001";
const priceListId = "c1000000-0000-4000-8000-000000000001";
const plain = (value) => JSON.parse(JSON.stringify(value));

function load(relativePath, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, AbortController, AbortSignal, Blob, File, FormData, Headers, Request, Response,
    URL, Uint8Array, crypto, setTimeout, clearTimeout, ...context,
  });
  return exports;
}

const settingsTypes = load("../app/lib/seller-settings-types.ts");
const types = load("../app/lib/catalog-types.ts", {
  require(name) {
    if (name === "./seller-settings-types") return settingsTypes;
    throw new Error(`Unexpected dependency ${name}`);
  },
});

const supplier = () => ({ id: supplierId, name: "Innpro", notes: "Feed principale", price_list_count: 1 });
const priceList = () => ({ id: priceListId, supplier_id: supplierId, supplier_name: "Innpro", name: "Listino IT", source_type: "upload", file_name: "innpro.xlsx", file_format: "xlsx", row_count: 1, status: "ready", updated_at: "2026-09-10T08:00:00Z" });
const dashboard = () => ({ seller_id: sellerId, can_manage: true, suppliers: [supplier()], price_lists: [priceList()] });
const detail = () => ({ price_list: priceList(), row_count: 1, preview_count: 1, products: [{ ean: "8050000000001", sku: "SKU-1", name: "Prodotto", cost: "12.34", shipping_cost: "1.50", total_cost: "13.84", quantity: "4" }] });

test("catalog DTOs enforce Seller/resource identity and discard unknown upstream fields", () => {
  const raw = dashboard(); raw.secret = "never-expose"; raw.suppliers[0].api_key = "never-expose"; raw.price_lists[0].local_path = "C:/private/file";
  const parsed = types.readCatalogDashboard(raw, sellerId);
  assert.deepEqual(plain(parsed), dashboard());
  assert.equal(JSON.stringify(parsed).includes("never-expose"), false);
  assert.equal(JSON.stringify(parsed).includes("C:/private"), false);
  assert.equal(types.readCatalogDashboard({ ...dashboard(), seller_id: otherSellerId }, sellerId), null);
  assert.equal(types.readCatalogDashboard({ ...dashboard(), suppliers: [supplier(), supplier()] }, sellerId), null);
  assert.equal(types.readCatalogDashboard({ ...dashboard(), price_lists: [{ ...priceList(), supplier_id: otherSellerId }] }, sellerId), null);
});

test("price-list detail accepts explicit nulls, limits preview to 200 rows and verifies list id", () => {
  const rows = Array.from({ length: 215 }, (_, index) => ({ ean: index ? null : "8050000000001", sku: `SKU-${index}`, name: null, cost: null, shipping_cost: "0", total_cost: null, quantity: "0" }));
  const parsed = types.readPriceListDetail({ price_list: priceList(), row_count: 215, preview_count: 200, products: rows, internal: "discard" }, priceListId);
  assert.equal(parsed.products.length, 200);
  assert.deepEqual(plain(parsed.products[0]), { ean: "8050000000001", sku: "SKU-0", name: "", cost: null, shipping_cost: "0", total_cost: null, quantity: "0" });
  assert.equal(types.readPriceListDetail({ ...detail(), price_list: { ...priceList(), id: otherSellerId } }, priceListId), null);
  assert.equal(types.readPriceListDetail({ ...detail(), products: [{ ...detail().products[0], cost: 12.34 }] }, priceListId), null);
  assert.equal(types.readPriceListDetail({ ...detail(), products: [{ ...detail().products[0], cost: "NaN" }] }, priceListId), null);
  assert.equal(types.readPriceListDetail({ ...detail(), products: [{ ...detail().products[0], quantity: "1e999" }] }, priceListId), null);
  const negative = types.readPriceListDetail({ ...detail(), products: [{ ...detail().products[0], cost: "-12.34", total_cost: "-10.84", quantity: "-1" }] }, priceListId);
  assert.equal(negative.products[0].cost, "-12.34"); assert.equal(negative.products[0].quantity, "-1");
  assert.equal(types.formatCatalogMoney("12.34").replace(/\s/u, " "), "12,34 €");
  assert.equal(types.formatCatalogMoney("-12.34").replace(/\s/u, " "), "-12,34 €");
  assert.equal(types.formatCatalogMoney(null), "—");
});

test("input readers trim allowed fields, enforce exact confirmations and reject PKL or empty files", () => {
  assert.deepEqual(plain(types.readSupplierInput({ name: "  Innpro  ", notes: "  Nota  ", seller_id: otherSellerId })), { name: "Innpro", notes: "Nota" });
  assert.deepEqual(plain(types.readDeleteSupplierInput({ confirmation: "Innpro", ignored: true })), { confirmation: "Innpro" });
  assert.equal(types.readDeleteSupplierInput({ confirmation: " Innpro " }), null);
  assert.equal(types.isAllowedPriceListFile(new File(["ean,cost"], "listino.CSV")), true);
  assert.equal(types.isAllowedPriceListFile(new File(["unsafe"], "listino.pkl")), false);
  assert.equal(types.isAllowedPriceListFile(new File([], "vuoto.csv")), false);
});

function proxy(fetchImpl) {
  class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
  return load("../app/lib/catalog-proxy.ts", {
    fetch: fetchImpl,
    require(name) {
      if (name === "next/server") return { NextResponse };
      if (name === "./api-url") return { apiUrl: "http://api.test" };
      if (name === "./catalog-types") return types;
      if (name === "./seller-settings-types") return settingsTypes;
      throw new Error(`Unexpected dependency ${name}`);
    },
  }).catalogProxy;
}

const jsonRequest = (payload, { cookie = "mh_session=existing", origin = "https://app.test", host = "app.test" } = {}) => new Request("https://app.test/api/catalogs", {
  method: "POST", headers: { cookie, origin, host, "content-type": "application/json" }, body: JSON.stringify(payload),
});
const getRequest = (cookie = "mh_session=existing") => new Request("https://app.test/api/catalogs", { headers: { cookie, host: "app.test" } });
function uploadRequest({ cookie = "mh_session=existing", content = "ean,cost\n805,12.34" } = {}) {
  const form = new FormData();
  form.set("supplier_id", supplierId); form.set("name", "Listino estate");
  form.set("file", new File([content], "estate.csv", { type: "text/csv" }));
  return new Request("https://app.test/api/catalogs", {
    method: "POST", headers: { cookie, origin: "https://app.test", host: "app.test" }, body: form,
  });
}

test("catalog BFF reads only fixed authenticated Seller paths with no-store and sanitized DTOs", async () => {
  const calls = [];
  const run = proxy(async (url, options) => {
    calls.push([url, options]);
    return Response.json(url.endsWith(priceListId) ? { ...detail(), raw_credentials: "secret" } : { ...dashboard(), raw_credentials: "secret" });
  });
  const summary = await run(getRequest(), sellerId, "dashboard");
  const preview = await run(getRequest(), sellerId, "detail", priceListId);
  assert.equal(calls[0][0], `http://api.test/v1/sellers/${sellerId}/catalogs`);
  assert.equal(calls[1][0], `http://api.test/v1/sellers/${sellerId}/catalogs/price-lists/${priceListId}?limit=200`);
  for (const [, options] of calls) {
    assert.equal(options.method, "GET"); assert.equal(options.headers.cookie, "mh_session=existing");
    assert.equal(options.cache, "no-store"); assert.equal(options.redirect, "error"); assert.ok(options.signal instanceof AbortSignal);
  }
  assert.equal(summary.headers.get("cache-control"), "no-store"); assert.equal(preview.headers.get("cache-control"), "no-store");
  assert.equal((await summary.text()).includes("raw_credentials"), false); assert.equal((await preview.text()).includes("raw_credentials"), false);
});

test("JSON writes are same-origin, field-whitelisted and use exact deletion confirmations", async () => {
  const calls = [];
  const run = proxy(async (url, options) => { calls.push([url, options]); return new Response(null, { status: 204 }); });
  assert.equal((await run(jsonRequest({ name: "  Innpro ", notes: "  Nota ", seller_id: otherSellerId }), sellerId, "create-supplier")).status, 201);
  assert.equal((await run(jsonRequest({ confirmation: "Innpro", seller_id: otherSellerId }), sellerId, "delete-supplier", supplierId)).status, 200);
  assert.equal((await run(jsonRequest({ confirmation: "ELIMINA", price_list_id: otherSellerId }), sellerId, "delete-price-list", priceListId)).status, 200);
  assert.equal(calls[0][0], `http://api.test/v1/sellers/${sellerId}/catalogs/suppliers`);
  assert.deepEqual(JSON.parse(calls[0][1].body), { name: "Innpro", notes: "Nota" });
  assert.equal(calls[1][1].method, "DELETE"); assert.deepEqual(JSON.parse(calls[1][1].body), { confirmation: "Innpro" });
  assert.deepEqual(JSON.parse(calls[2][1].body), { confirmation: "ELIMINA" });
  assert.equal((await run(jsonRequest({ confirmation: "elimina" }), sellerId, "delete-price-list", priceListId)).status, 422);
  assert.equal(calls.length, 3);
});

test("multipart import forwards only supplier, name and an allowed file", async () => {
  let forwarded; let preflight;
  const run = proxy(async (url, options) => {
    if (url.endsWith(`/${sellerId}/catalogs`) && options.method === "GET") {
      preflight = options; return Response.json(dashboard());
    }
    forwarded = options; return new Response(null, { status: 201 });
  });
  const form = new FormData();
  form.set("supplier_id", supplierId); form.set("name", "  Listino estate  "); form.set("file", new File(["ean,cost\n805,12.34"], "estate.csv", { type: "text/csv" }));
  form.set("upstream_url", "https://attacker.test"); form.set("seller_id", otherSellerId);
  const request = new Request("https://app.test/api/catalogs", { method: "POST", headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test" }, body: form });
  const response = await run(request, sellerId, "create-price-list");
  assert.equal(preflight.headers.cookie, "mh_session=existing"); assert.equal(preflight.cache, "no-store"); assert.equal(preflight.redirect, "error");
  assert.equal(response.status, 201); assert.equal(forwarded.method, "POST"); assert.equal(forwarded.headers["content-type"], undefined);
  assert.deepEqual(Array.from(forwarded.body.keys()), ["supplier_id", "name", "file"]);
  assert.equal(forwarded.body.get("name"), "Listino estate"); assert.equal(forwarded.body.get("file").name, "estate.csv");
});

test("catalog upload preflight rejects forged cookies and read-only grants without consuming multipart", async () => {
  for (const [preflightResponse, expectedStatus] of [
    [new Response(null, { status: 401 }), 401],
    [Response.json({ ...dashboard(), can_manage: false }), 403],
    [Response.json({ ...dashboard(), seller_id: otherSellerId }), 502],
  ]) {
    const incoming = uploadRequest({ cookie: "mh_session=forged" });
    let calls = 0;
    const run = proxy(async (url, options) => {
      calls += 1; assert.equal(url, `http://api.test/v1/sellers/${sellerId}/catalogs`); assert.equal(options.method, "GET");
      return preflightResponse.clone();
    });
    const response = await run(incoming, sellerId, "create-price-list");
    assert.equal(response.status, expectedStatus); assert.equal(incoming.bodyUsed, false); assert.equal(calls, 1);
  }
});

test("catalog upload admission allows one import per Seller and at most two globally before body reads", async () => {
  const thirdSellerId = "a1000000-0000-4000-8000-000000000003";
  const pending = []; let uploads = 0;
  const run = proxy(async (url, options) => {
    const matchedSeller = url.match(/\/v1\/sellers\/([0-9a-f-]+)\/catalogs$/i)?.[1];
    if (options.method === "GET" && matchedSeller) return Response.json({ ...dashboard(), seller_id: matchedSeller });
    uploads += 1; return new Promise((resolve) => pending.push(resolve));
  });
  const waitForUploads = async (expected) => { while (uploads < expected) await new Promise(setImmediate); };

  const firstRequest = uploadRequest(); const first = run(firstRequest, sellerId, "create-price-list");
  await waitForUploads(1);
  const duplicateRequest = uploadRequest(); const duplicate = await run(duplicateRequest, sellerId, "create-price-list");
  assert.equal(duplicate.status, 409); assert.equal(duplicateRequest.bodyUsed, false);

  const secondRequest = uploadRequest(); const second = run(secondRequest, otherSellerId, "create-price-list");
  await waitForUploads(2);
  const excessRequest = uploadRequest(); const excess = await run(excessRequest, thirdSellerId, "create-price-list");
  assert.equal(excess.status, 429); assert.equal(excess.headers.get("retry-after"), "5"); assert.equal(excessRequest.bodyUsed, false);

  for (const resolve of pending) resolve(new Response(null, { status: 201 }));
  assert.deepEqual((await Promise.all([first, second])).map((response) => response.status), [201, 201]);
  assert.equal(firstRequest.bodyUsed, true); assert.equal(secondRequest.bodyUsed, true);
});

test("an interrupted catalog body read returns a timeout and releases Seller admission", async () => {
  let uploads = 0;
  const run = proxy(async (url, options) => {
    if (options.method === "GET" && url.endsWith(`/${sellerId}/catalogs`)) return Response.json(dashboard());
    uploads += 1; return new Response(null, { status: 201 });
  });
  const controller = new AbortController();
  const slowRequest = new Request("https://app.test/api/catalogs", {
    method: "POST",
    headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test", "content-type": "multipart/form-data; boundary=slow" },
    body: new ReadableStream({ start() {} }), signal: controller.signal, duplex: "half",
  });
  const interrupted = run(slowRequest, sellerId, "create-price-list");
  while (!slowRequest.bodyUsed) await new Promise(setImmediate);
  controller.abort();
  const interruptedResponse = await interrupted;
  assert.equal(interruptedResponse.status, 408); assert.equal(uploads, 0);

  const nextResponse = await run(uploadRequest(), sellerId, "create-price-list");
  assert.equal(nextResponse.status, 201); assert.equal(uploads, 1);
});

test("BFF rejects path injection, missing session, cross-origin writes, PKL and oversized bodies before upstream", async () => {
  let calls = 0; let preflights = 0;
  const run = proxy(async (url, options) => {
    if (url.endsWith(`/${sellerId}/catalogs`) && options.method === "GET") { preflights += 1; return Response.json(dashboard()); }
    calls += 1; throw new Error("must not call");
  });
  assert.equal((await run(getRequest(), "../other-seller", "dashboard")).status, 422);
  assert.equal((await run(getRequest(), sellerId, "detail", "../../secret")).status, 422);
  assert.equal((await run(jsonRequest({ name: "Innpro", notes: "" }, { cookie: "" }), sellerId, "create-supplier")).status, 401);
  assert.equal((await run(jsonRequest({ name: "Innpro", notes: "" }, { origin: "https://other.test" }), sellerId, "create-supplier")).status, 403);
  assert.equal((await run(jsonRequest({ name: "Innpro", notes: "" }, { origin: "", host: "app.test" }), sellerId, "create-supplier")).status, 403);
  const unsafe = new FormData(); unsafe.set("supplier_id", supplierId); unsafe.set("name", "Unsafe"); unsafe.set("file", new File(["payload"], "unsafe.pkl"));
  assert.equal((await run(new Request("https://app.test/api/catalogs", { method: "POST", headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test" }, body: unsafe }), sellerId, "create-price-list")).status, 422);
  const oversized = { url: "https://app.test/api/catalogs", body: null, headers: new Headers({ cookie: "mh_session=existing", origin: "https://app.test", host: "app.test", "content-type": "multipart/form-data; boundary=x", "content-length": String(21 * 1024 * 1024) }) };
  assert.equal((await run(oversized, sellerId, "create-price-list")).status, 413);
  assert.equal(calls, 0); assert.equal(preflights, 2);
});

test("upstream failures and malformed responses are normalized without echoing API details", async () => {
  for (const [response, expected] of [
    [() => Response.json({ detail: "database secret" }, { status: 409 }), 409],
    [() => Response.json({ detail: "database secret" }, { status: 422 }), 422],
    [() => new Response(null, { status: 500 }), 503],
    [() => Response.json({ ...dashboard(), seller_id: otherSellerId }), 502],
    [() => { throw new Error("database secret"); }, 503],
  ]) {
    const result = await proxy(async () => response())(getRequest(), sellerId, "dashboard");
    assert.equal(result.status, expected); assert.equal((await result.text()).includes("database secret"), false);
  }
});

test("Next routes delegate resolved UUIDs and fixed operations to the shared proxy", async () => {
  const calls = [];
  const routes = [
    ["../app/api/sellers/[sellerId]/catalogs/route.ts", "GET", "dashboard", undefined],
    ["../app/api/sellers/[sellerId]/catalogs/suppliers/route.ts", "POST", "create-supplier", undefined],
    ["../app/api/sellers/[sellerId]/catalogs/suppliers/[supplierId]/route.ts", "DELETE", "delete-supplier", supplierId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/route.ts", "POST", "create-price-list", undefined],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/route.ts", "GET", "detail", priceListId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/route.ts", "DELETE", "delete-price-list", priceListId],
  ];
  for (const [file, method, operation, id] of routes) {
    const route = load(file, { require: () => ({ catalogProxy: (...args) => { calls.push(args); return "ok"; } }) });
    assert.equal(await route[method]("request", { params: Promise.resolve({ sellerId, supplierId, priceListId }) }), "ok");
    assert.deepEqual(calls.at(-1).slice(1), id ? [sellerId, operation, id] : [sellerId, operation]);
  }
});

test("Seller navigation exposes one Catalog macroarea with the two real routes", () => {
  const navigation = load("../app/lib/seller-navigation.ts");
  const catalog = navigation.sellerAreas.find((area) => area.id === "catalog");
  assert.ok(catalog); assert.equal(catalog.label, "Catalogo");
  assert.deepEqual(plain(catalog.sections), [
    { page: "suppliers", href: "/seller/catalog/suppliers", label: "Fornitori", icon: "supplier" },
    { page: "price-lists", href: "/seller/catalog/price-lists", label: "Listini", icon: "file" },
  ]);
});
