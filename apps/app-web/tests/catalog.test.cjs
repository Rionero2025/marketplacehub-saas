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
const jobId = "d1000000-0000-4000-8000-000000000001";
const plain = (value) => JSON.parse(JSON.stringify(value));

function load(relativePath, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, AbortController, AbortSignal, Blob, File, FormData, Headers, Request, Response,
    URL, URLSearchParams, Uint8Array, crypto, setTimeout, clearTimeout, ...context,
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
const job = () => ({ id: jobId, status: "queued", processed_bytes: 0, total_bytes: null, progress: null, message: "Aggiornamento listino in coda.", error_code: null, result_version: null, created_at: "2026-09-10T08:00:00Z", updated_at: "2026-09-10T08:00:00Z" });
const priceList = () => ({ id: priceListId, supplier_id: supplierId, supplier_name: "Innpro", name: "Listino IT", provider: "generic", feed_role: "standard", source_type: "upload", file_name: "innpro.xlsx", file_format: "xlsx", row_count: 1, status: "ready", source_host: null, source_config_revision: 1, active_version_number: null, last_checked_at: null, last_success_at: null, latest_job: null, updated_at: "2026-09-10T08:00:00Z" });
const urlPriceList = () => ({ ...priceList(), source_type: "url", file_name: null, file_format: null, row_count: null, status: "queued", source_host: "feeds.innpro.example", latest_job: job() });
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
  const innproFull = { ...priceList(), provider: "innpro", feed_role: "full" };
  assert.deepEqual(plain(types.readCatalogDashboard({ ...dashboard(), price_lists: [innproFull] }, sellerId).price_lists[0]), innproFull);
  for (const invalid of [
    { provider: "generic", feed_role: "full" },
    { provider: "innpro", feed_role: "standard" },
    { provider: "other", feed_role: "standard" },
    { provider: "innpro", feed_role: "prices" },
  ]) assert.equal(types.readCatalogDashboard({ ...dashboard(), price_lists: [{ ...priceList(), ...invalid }] }, sellerId), null);
});

test("URL list and job DTOs accept pending fields while stripping URL, credentials and internal scope", () => {
  const remote = urlPriceList();
  remote.last_checked_at = ""; remote.last_success_at = "";
  remote.source_config_encrypted = "ciphertext";
  remote.url = "https://feeds.innpro.example/list.csv?token=secret";
  remote.latest_job = { ...job(), price_list_id: priceListId, organization_id: otherSellerId, source_config_revision: 3 };
  const parsed = types.readCatalogDashboard({ ...dashboard(), price_lists: [remote] }, sellerId);
  assert.deepEqual(plain(parsed.price_lists[0]), urlPriceList());
  assert.equal(JSON.stringify(parsed).includes("token=secret"), false);
  assert.equal(JSON.stringify(parsed).includes("ciphertext"), false);
  assert.equal(JSON.stringify(parsed).includes("organization_id"), false);
  const uploadWithEmptyHost = types.readCatalogDashboard({ ...dashboard(), price_lists: [{ ...priceList(), source_host: "" }] }, sellerId);
  assert.equal(uploadWithEmptyHost.price_lists[0].source_host, null);
  assert.equal(types.readCatalogDashboard({ ...dashboard(), price_lists: [{ ...urlPriceList(), source_host: "feeds.example/path?secret=1" }] }, sellerId), null);

  const mutation = types.readCatalogFeedMutation({ price_list: remote, job: { ...job(), password: "secret" }, ignored: "secret" });
  assert.equal(mutation.price_list.id, priceListId); assert.deepEqual(plain(mutation.job), job());
  assert.equal(JSON.stringify(mutation).includes("password"), false);
  assert.equal(types.readCatalogFeedMutation({ price_list: remote, job: { ...job(), price_list_id: otherSellerId } }), null);
  assert.deepEqual(plain(types.readCatalogFeedJobResponse({ job: { ...job(), message: "Download https://secret.example/token" } }, jobId)), { ...job(), message: null });
  assert.equal(types.readCatalogFeedJobResponse({ job: { ...job(), id: otherSellerId } }, jobId), null);
  assert.equal(types.readCatalogFeedJobResponse({ job: { ...job(), price_list_id: otherSellerId } }, jobId, priceListId), null);
  assert.deepEqual(plain(types.readCatalogPriceListMutation({ price_list: { ...remote, password: "secret" } }, priceListId)), urlPriceList());
  assert.equal(types.readCatalogPriceListMutation({ price_list: { ...remote, id: otherSellerId } }, priceListId), null);
  assert.equal(types.readCatalogPriceListMutation(remote, priceListId), null);
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
  assert.deepEqual(plain(types.readUrlPriceListInput({ supplier_id: supplierId, name: "  Feed Innpro ", provider: "innpro", feed_role: "light", url: " https://feeds.innpro.example/list.csv?lang=it ", username: " user ", password: "p@ss", seller_id: otherSellerId })), {
    supplier_id: supplierId, name: "Feed Innpro", provider: "innpro", feed_role: "light", url: "https://feeds.innpro.example/list.csv?lang=it", username: "user", password: "p@ss",
  });
  for (const invalid of [
    { url: "http://feeds.innpro.example/list.csv", username: "", password: "" },
    { url: "https://user:pass@feeds.innpro.example/list.csv", username: "", password: "" },
    { url: "https://feeds.innpro.example/list.csv#secret", username: "", password: "" },
    { url: "https://feeds.innpro.example/list.csv", username: "user", password: "" },
  ]) assert.equal(types.readUrlPriceListInput({ supplier_id: supplierId, name: "Feed", provider: "generic", feed_role: "standard", ...invalid }), null);
  assert.equal(types.readUrlPriceListInput({ supplier_id: supplierId, name: "Feed", provider: "generic", feed_role: "light", url: "https://feeds.example/list.csv", username: "", password: "" }), null);
  assert.equal(types.readUrlPriceListInput({ supplier_id: supplierId, name: "Feed", provider: "innpro", feed_role: "standard", url: "https://feeds.example/list.xml", username: "", password: "" }), null);

  assert.deepEqual(plain(types.readUpdateUrlPriceListInput({
    url: " https://new-feeds.innpro.example/list.csv?token=opaque ", credentials_mode: "keep",
    expected_config_revision: 1, username: "must-not-forward", password: "must-not-forward",
  })), { url: "https://new-feeds.innpro.example/list.csv?token=opaque", credentials_mode: "keep", expected_config_revision: 1 });
  assert.deepEqual(plain(types.readUpdateUrlPriceListInput({
    url: "https://new-feeds.innpro.example/list.csv", credentials_mode: "replace",
    expected_config_revision: 2, username: " api-user ", password: "new-password", ignored: "discard",
  })), { url: "https://new-feeds.innpro.example/list.csv", credentials_mode: "replace", expected_config_revision: 2, username: "api-user", password: "new-password" });
  assert.deepEqual(plain(types.readUpdateUrlPriceListInput({
    url: "https://new-feeds.innpro.example/list.csv", credentials_mode: "remove",
    expected_config_revision: 3, username: "discard", password: "discard",
  })), { url: "https://new-feeds.innpro.example/list.csv", credentials_mode: "remove", expected_config_revision: 3 });
  for (const invalid of [
    { url: "http://feeds.example/list.csv", credentials_mode: "keep", expected_config_revision: 1 },
    { url: "https://feeds.example/list.csv", credentials_mode: "unknown", expected_config_revision: 1 },
    { url: "https://feeds.example/list.csv", credentials_mode: "keep", expected_config_revision: 0 },
    { url: "https://feeds.example/list.csv", credentials_mode: "replace", expected_config_revision: 1, username: "", password: "secret" },
    { url: "https://feeds.example/list.csv", credentials_mode: "replace", expected_config_revision: 1, username: "user", password: "" },
  ]) assert.equal(types.readUpdateUrlPriceListInput(invalid), null);

  assert.equal(types.urlFeedHostMatches("https://feeds.example/new.csv", "feeds.example"), true);
  assert.equal(types.urlFeedHostMatches("https://FEEDS.EXAMPLE./new.csv", "feeds.example"), true);
  assert.equal(types.urlFeedHostMatches("https://other.example/new.csv", "feeds.example"), false);
  assert.equal(types.urlFeedHostMatches("not a URL", "feeds.example"), false);
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
  form.set("provider", "generic"); form.set("feed_role", "standard");
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

test("URL feed create, config update, refresh and job polling use fixed paths and sanitized public DTOs", async () => {
  const calls = [];
  const completedJob = { ...job(), status: "done", processed_bytes: 2048, total_bytes: 2048, progress: 100, message: "Listino aggiornato.", result_version: 2 };
  const run = proxy(async (url, options) => {
    calls.push([url, options]);
    if (options.method === "GET" && url.endsWith(`/${sellerId}/catalogs`)) return Response.json(dashboard());
    if (options.method === "GET") return Response.json({ job: { ...completedJob, source_config_revision: 8, url: "https://secret.example/?token=x" } });
    if (url.endsWith(`/${priceListId}/url`)) return Response.json({ price_list: { ...urlPriceList(), source_host: "new-feeds.innpro.example", source_config_revision: 2, password: "never" } });
    return Response.json({ price_list: { ...urlPriceList(), provider: "innpro", feed_role: "light", password: "never" }, job: { ...job(), organization_id: otherSellerId } }, { status: 202 });
  });
  const create = await run(jsonRequest({
    supplier_id: supplierId, name: "  Feed estate  ", provider: "innpro", feed_role: "light",
    url: "https://feeds.innpro.example/list.csv?token=upstream",
    username: " api-user ", password: "api-password", organization_id: otherSellerId, callback: "https://attacker.test",
  }), sellerId, "create-price-list-url");
  assert.equal(create.status, 202);
  assert.deepEqual(JSON.parse(calls[1][1].body), {
    supplier_id: supplierId, name: "Feed estate", provider: "innpro", feed_role: "light",
    url: "https://feeds.innpro.example/list.csv?token=upstream",
    username: "api-user", password: "api-password",
  });
  assert.equal(calls[1][0], `http://api.test/v1/sellers/${sellerId}/catalogs/price-lists/url`);
  const createText = await create.text();
  assert.equal(createText.includes("token=upstream"), false); assert.equal(createText.includes("api-password"), false);
  assert.equal(JSON.parse(createText).price_list.provider, "innpro");
  assert.equal(JSON.parse(createText).price_list.feed_role, "light");

  const update = await run(jsonRequest({
    url: "https://new-feeds.innpro.example/list.csv?token=new-secret", credentials_mode: "replace",
    expected_config_revision: 1, username: " new-user ", password: "new-password",
    seller_id: otherSellerId, callback: "https://attacker.test",
  }), sellerId, "update-price-list-url", priceListId);
  assert.equal(update.status, 200); assert.equal(calls[3][0], `http://api.test/v1/sellers/${sellerId}/catalogs/price-lists/${priceListId}/url`);
  assert.deepEqual(JSON.parse(calls[3][1].body), {
    url: "https://new-feeds.innpro.example/list.csv?token=new-secret", credentials_mode: "replace",
    expected_config_revision: 1, username: "new-user", password: "new-password",
  });
  const updateText = await update.text();
  assert.equal(updateText.includes("token=new-secret"), false); assert.equal(updateText.includes("new-password"), false);
  assert.equal(JSON.parse(updateText).price_list.source_host, "new-feeds.innpro.example");

  const refresh = await run(jsonRequest({ url: "https://attacker.test", password: "never" }), sellerId, "refresh-price-list", priceListId);
  assert.equal(refresh.status, 202); assert.equal(calls[5][0], `http://api.test/v1/sellers/${sellerId}/catalogs/price-lists/${priceListId}/refresh`);
  assert.deepEqual(JSON.parse(calls[5][1].body), {});

  const status = await run(getRequest(), sellerId, "job", priceListId, jobId);
  assert.equal(status.status, 200); assert.equal(calls[6][0], `http://api.test/v1/sellers/${sellerId}/catalogs/price-lists/${priceListId}/jobs/${jobId}`);
  assert.equal(calls[6][1].method, "GET"); assert.equal(status.headers.get("cache-control"), "no-store");
  assert.deepEqual(JSON.parse(await status.text()), completedJob);
});

test("URL feed BFF authenticates before body reads, caps JSON and never echoes URL credentials", async () => {
  const forged = jsonRequest({ supplier_id: supplierId, name: "Feed", provider: "generic", feed_role: "standard", url: "https://feeds.example/list.csv", username: "user", password: "secret" }, { cookie: "mh_session=forged" });
  let calls = 0;
  const reject = proxy(async () => { calls += 1; return new Response(null, { status: 401 }); });
  const rejected = await reject(forged, sellerId, "create-price-list-url");
  assert.equal(rejected.status, 401); assert.equal(forged.bodyUsed, false); assert.equal(calls, 1);

  const oversized = {
    url: "https://app.test/api/catalogs", body: null, signal: null,
    headers: new Headers({ cookie: "mh_session=existing", origin: "https://app.test", host: "app.test", "content-type": "application/json", "content-length": String(16 * 1024 + 1) }),
  };
  const sizeRun = proxy(async (url, options) => options.method === "GET" ? Response.json(dashboard()) : (() => { throw new Error("must not forward"); })());
  assert.equal((await sizeRun(oversized, sellerId, "create-price-list-url")).status, 413);

  const secretRun = proxy(async (url, options) => options.method === "GET"
    ? Response.json(dashboard())
    : Response.json({ detail: "https://feeds.example/?token=secret password=hunter2" }, { status: 422 }));
  const secretResponse = await secretRun(jsonRequest({ supplier_id: supplierId, name: "Feed", provider: "generic", feed_role: "standard", url: "https://feeds.example/list.csv", username: "", password: "" }), sellerId, "create-price-list-url");
  const text = await secretResponse.text();
  assert.equal(secretResponse.status, 422); assert.equal(text.includes("token=secret"), false); assert.equal(text.includes("hunter2"), false);

  const forgedUpdate = jsonRequest({
    url: "https://feeds.example/new.csv", credentials_mode: "replace", expected_config_revision: 1,
    username: "user", password: "new-secret",
  }, { cookie: "mh_session=forged" });
  calls = 0;
  const rejectedUpdate = await reject(forgedUpdate, sellerId, "update-price-list-url", priceListId);
  assert.equal(rejectedUpdate.status, 401); assert.equal(forgedUpdate.bodyUsed, false); assert.equal(calls, 1);
});

test("catalog BFF rejects unsupported profiles and verifies the created URL feed profile", async () => {
  let writes = 0;
  const run = proxy(async (url, options) => {
    if (options.method === "GET") return Response.json(dashboard());
    writes += 1;
    return Response.json({ price_list: urlPriceList(), job: job() }, { status: 202 });
  });
  const base = { supplier_id: supplierId, name: "Feed", url: "https://feeds.example/list.xml", username: "", password: "" };
  const invalid = await run(jsonRequest({ ...base, provider: "generic", feed_role: "full" }), sellerId, "create-price-list-url");
  assert.equal(invalid.status, 422); assert.equal(writes, 0);
  const mismatched = await run(jsonRequest({ ...base, provider: "innpro", feed_role: "light" }), sellerId, "create-price-list-url");
  assert.equal(mismatched.status, 502); assert.equal(writes, 1);
});

test("multipart import forwards only supplier, profile, name and an allowed file", async () => {
  let forwarded; let preflight;
  const run = proxy(async (url, options) => {
    if (url.endsWith(`/${sellerId}/catalogs`) && options.method === "GET") {
      preflight = options; return Response.json(dashboard());
    }
    forwarded = options;
    return Response.json({
      ...detail(), price_list: { ...priceList(), provider: "innpro", feed_role: "full" },
    }, { status: 201 });
  });
  const form = new FormData();
  form.set("supplier_id", supplierId); form.set("name", "  Listino estate  ");
  form.set("provider", "innpro"); form.set("feed_role", "full");
  form.set("file", new File(["ean,cost\n805,12.34"], "estate.csv", { type: "text/csv" }));
  form.set("upstream_url", "https://attacker.test"); form.set("seller_id", otherSellerId);
  const request = new Request("https://app.test/api/catalogs", { method: "POST", headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test" }, body: form });
  const response = await run(request, sellerId, "create-price-list");
  assert.equal(preflight.headers.cookie, "mh_session=existing"); assert.equal(preflight.cache, "no-store"); assert.equal(preflight.redirect, "error");
  assert.equal(response.status, 201); assert.equal(forwarded.method, "POST"); assert.equal(forwarded.headers["content-type"], undefined);
  assert.deepEqual(Array.from(forwarded.body.keys()), ["supplier_id", "name", "provider", "feed_role", "file"]);
  assert.equal(forwarded.body.get("name"), "Listino estate"); assert.equal(forwarded.body.get("provider"), "innpro");
  assert.equal(forwarded.body.get("feed_role"), "full"); assert.equal(forwarded.body.get("file").name, "estate.csv");
  assert.equal(JSON.parse(await response.text()).price_list.feed_role, "full");
});

test("catalog upload rejects an upstream response with a different feed profile", async () => {
  const run = proxy(async (url, options) => {
    if (options.method === "GET") return Response.json(dashboard());
    return Response.json({ ...detail(), price_list: priceList() }, { status: 201 });
  });
  const form = new FormData();
  form.set("supplier_id", supplierId); form.set("name", "InnPro FULL");
  form.set("provider", "innpro"); form.set("feed_role", "full");
  form.set("file", new File(["<products />"], "full.xml", { type: "application/xml" }));
  const request = new Request("https://app.test/api/catalogs", {
    method: "POST", headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test" }, body: form,
  });
  assert.equal((await run(request, sellerId, "create-price-list")).status, 502);
});

test("catalog upload rejects an incompatible provider and feed role before the file reaches upstream", async () => {
  let writes = 0;
  const run = proxy(async (url, options) => {
    if (options.method === "GET") return Response.json(dashboard());
    writes += 1; return new Response(null, { status: 201 });
  });
  const form = new FormData();
  form.set("supplier_id", supplierId); form.set("name", "Listino non valido");
  form.set("provider", "generic"); form.set("feed_role", "light");
  form.set("file", new File(["ean,cost\n805,12.34"], "estate.csv", { type: "text/csv" }));
  const request = new Request("https://app.test/api/catalogs", { method: "POST", headers: { cookie: "mh_session=existing", origin: "https://app.test", host: "app.test" }, body: form });
  assert.equal((await run(request, sellerId, "create-price-list")).status, 422);
  assert.equal(writes, 0);
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

  for (const resolve of pending) resolve(Response.json(detail(), { status: 201 }));
  assert.deepEqual((await Promise.all([first, second])).map((response) => response.status), [201, 201]);
  assert.equal(firstRequest.bodyUsed, true); assert.equal(secondRequest.bodyUsed, true);
});

test("an interrupted catalog body read returns a timeout and releases Seller admission", async () => {
  let uploads = 0;
  const run = proxy(async (url, options) => {
    if (options.method === "GET" && url.endsWith(`/${sellerId}/catalogs`)) return Response.json(dashboard());
    uploads += 1; return Response.json(detail(), { status: 201 });
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
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/route.ts", "POST", "create-price-list", undefined, undefined,
      new Request("https://app.test/api/catalogs", { method: "POST", headers: { "content-type": "multipart/form-data; boundary=x" } })],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/route.ts", "POST", "create-price-list-url", undefined, undefined,
      new Request("https://app.test/api/catalogs", { method: "POST", headers: { "content-type": "application/json; charset=utf-8" } })],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/route.ts", "GET", "detail", priceListId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/route.ts", "DELETE", "delete-price-list", priceListId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/url/route.ts", "POST", "update-price-list-url", priceListId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/refresh/route.ts", "POST", "refresh-price-list", priceListId],
    ["../app/api/sellers/[sellerId]/catalogs/price-lists/[priceListId]/jobs/[jobId]/route.ts", "GET", "job", priceListId, jobId],
  ];
  for (const [file, method, operation, id, secondId, request = "request"] of routes) {
    const route = load(file, { require: () => ({ catalogProxy: (...args) => { calls.push(args); return "ok"; } }) });
    assert.equal(await route[method](request, { params: Promise.resolve({ sellerId, supplierId, priceListId, jobId }) }), "ok");
    assert.deepEqual(calls.at(-1).slice(1), secondId ? [sellerId, operation, id, secondId] : id ? [sellerId, operation, id] : [sellerId, operation]);
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

test("Seller catalog UI keeps feed secrets transient and cleans up real job polling", () => {
  const source = fs.readFileSync(path.join(__dirname, "../app/components/SellerCatalogPanel.tsx"), "utf8");
  assert.match(source, /autoComplete="new-password"/);
  assert.match(source, /setFeedPassword\(""\);\s*await enqueueFeed/);
  assert.match(source, /setEditFeedUsername\(""\);\s*setEditFeedPassword\(""\);\s*await mutate/);
  assert.match(source, /jobs\/\$\{item\.jobId\}/);
  assert.match(source, /for \(const pollController of pollControllers\.current\) pollController\.abort\(\)/);
  assert.doesNotMatch(source, /`\$\{baseUrl\}\/price-lists\/url`/);
  assert.match(source, /operation: "create-price-list-url", url: `\$\{baseUrl\}\/price-lists`/);
  assert.match(source, /return \(\) => \{[\s\S]*?abort\.abort\(\);[\s\S]*?pollControllers\.current\.delete\(abort\)/);
  assert.equal(/localStorage|sessionStorage/.test(source), false);
  assert.match(source, /priceList\.source_host/);
  assert.doesNotMatch(source, /priceList\.(?:url|password|username)/);
  assert.match(source, /setEditFeedUrl\(""\)/);
  assert.match(source, /credentials_mode: editCredentialMode/);
  assert.match(source, /expected_config_revision: editPriceList\.source_config_revision/);
  assert.match(source, /input\.credentials_mode === "keep" && !urlFeedHostMatches\(input\.url, edited\.source_host \?\? ""\)/);
  assert.match(source, /<option value="generic">Generico<\/option><option value="innpro">InnPro IOF<\/option>/);
  assert.match(source, /<option value="full">FULL · contenuti prodotto<\/option><option value="light">LIGHT · prezzi e disponibilità<\/option>/);
  assert.match(source, /payload: \{[\s\S]*?provider: priceListProvider,[\s\S]*?feed_role: priceListFeedRole/);
  assert.match(source, /form\.set\("provider", priceListProvider\); form\.set\("feed_role", priceListFeedRole\)/);
  assert.match(source, /catalog-profile-badges/);
});

test("catalog progress renders measured download changes and honest unknown/processing states", () => {
  const { CatalogJobProgress } = load("../app/components/CatalogJobProgress.tsx", { require });
  const { createElement } = require("react");
  const { renderToStaticMarkup } = require("react-dom/server");
  const render = (overrides) => renderToStaticMarkup(createElement(CatalogJobProgress, {
    priceListName: "InnPro FULL", job: { ...job(), status: "running", ...overrides },
  }));
  for (const value of [25, 75]) {
    const html = render({ processed_bytes: value * 1_000_000, total_bytes: 100_000_000 });
    assert.match(html, new RegExp(`aria-valuenow="${value}"`));
    assert.match(html, new RegExp(`width:${value}%`));
    assert.match(html, new RegExp(`${value}% · Download`));
    assert.match(html, new RegExp(`${value} MB di 100 MB`));
  }
  const unknown = render({ processed_bytes: 12_500_000 });
  assert.match(unknown, /is-indeterminate/);
  assert.match(unknown, /12,5 MB scaricati/);
  assert.doesNotMatch(unknown, /aria-valuenow=|\d+%/);
  for (const message of ["Elaborazione prodotti in corso.", "Salvataggio prodotti in corso."]) {
    const raw = { ...job(), status: "running", progress: 99, processed_bytes: 100, total_bytes: 100, message };
    const parsed = types.readCatalogDashboard({ ...dashboard(), price_lists: [{ ...urlPriceList(), latest_job: raw }] }, sellerId);
    assert.equal(parsed.price_lists[0].latest_job.message, message);
    const html = render(raw);
    assert.match(html, /is-indeterminate/);
    assert.doesNotMatch(html, /aria-valuenow=|100%/);
    assert.ok(html.includes(message));
  }
  assert.match(render({ status: "done", progress: 100 }), /100% · Completato/);
  assert.doesNotMatch(render({ status: "done" }), /is-indeterminate/);
  assert.match(render({ status: "queued" }), /In attesa di avvio/);
  assert.doesNotMatch(render({ status: "queued" }), /is-indeterminate/);
  assert.equal(render({ status: "error" }), "");
});

test("overall forecast survives the BFF and displays a changing estimate without a false success", () => {
  const { CatalogJobProgress } = load("../app/components/CatalogJobProgress.tsx", { require });
  const render = (forecast, status = "running") => {
    const parsed = types.readCatalogFeedJobResponse({ job: { ...job(), status, forecast } }, jobId);
    return require("react-dom/server").renderToStaticMarkup(require("react").createElement(CatalogJobProgress, {
      job: parsed, priceListName: "FULL",
    }));
  };
  const first = { percent: 35, remaining_seconds: 186, state: "available", basis: "previous-size", private: "secret" };
  assert.match(render(first), /35% · Completamento stimato/);
  assert.match(render(first), /Tempo residuo: circa 4 min/);
  assert.match(render(first), /download precedenti/);
  assert.doesNotMatch(render(first), /secret/);
  assert.match(render({ ...first, percent: 52, remaining_seconds: 91 }), /circa 2 min/);
  assert.match(render({ ...first, percent: 99, remaining_seconds: null, state: "recalculating" }), /ricalcolo in corso/);
  assert.match(render({ ...first, state: "stalled", remaining_seconds: null }), /In attesa di nuovi dati/);
  assert.doesNotMatch(render({ ...first, percent: 100 }), /100%/);
  assert.match(render(first, "done"), /100% · Completato/);
  assert.doesNotMatch(render(first, "done"), /Tempo residuo/);
  for (const bad of [{ ...first, percent: 101 }, { ...first, remaining_seconds: -1 }, { ...first, state: "secret" }]) {
    const parsed = types.readCatalogFeedJobResponse({ job: { ...job(), forecast: bad } }, jobId);
    assert.equal(parsed.forecast, undefined);
  }
});

test("physical measurements are validated and filter query is forwarded with a fixed preview limit", async () => {
  const raw = detail();
  raw.filtered_count = 1;
  Object.assign(raw.products[0], { weight_kg: "1.03", length_cm: null, width_cm: null, height_cm: null, dimensions_unconfirmed: "29 × 18 × 5" });
  assert.equal(types.readPriceListDetail(raw, priceListId).products[0].weight_kg, "1.03");
  raw.products[0].weight_kg = "-1";
  assert.equal(types.readPriceListDetail(raw, priceListId), null);
  raw.products[0].weight_kg = "1.03";
  raw.filtered_count = 0;
  assert.equal(types.readPriceListDetail(raw, priceListId), null);
  raw.filtered_count = 1;
  const calls = [];
  const run = proxy(async (url) => { calls.push(url); return Response.json(raw); });
  const request = (query) => new Request(`https://app.test/api/catalogs?${query}`, { headers: { cookie: "mh_session=existing", host: "app.test" } });
  const response = await run(request("measure=weight_kg&exclude=above&lower=10&limit=9999&seller_id=other"), sellerId, "detail", priceListId);
  assert.equal(response.status, 200);
  assert.equal(new URL(calls[0]).search, "?limit=200&measure=weight_kg&exclude=above&lower=10");
  assert.equal((await run(request("lower=1&lower=2"), sellerId, "detail", priceListId)).status, 422);
  assert.equal(calls.length, 1);
});
