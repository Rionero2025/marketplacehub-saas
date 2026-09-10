const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");

const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const otherAccountId = "a2000000-0000-4000-8000-000000000002";
const lineId = "a3000000-0000-4000-8000-000000000001";
const jobId = "a4000000-0000-4000-8000-000000000001";
const paymentSelectionId = "a5000000-0000-4000-8000-000000000001";
const scope = { account_id: accountId, environment: "live" };
const query = { ...scope, page: 1, page_size: 50, search: "", statuses: [], storefronts: [], currencies: [], carriers: [], date_from: "", date_to: "", status_selection: "all", storefront_selection: "all", currency_selection: "all", tracking: "all", commission: "all", payment: "all", amount_min: "", amount_max: "" };
const item = () => ({ id: lineId, external_line_id: "K-UNIT-1", order_id: "K-ORDER-1", marketplace: "kaufland", created_at: "2026-09-07T08:30:00Z", status: "sent", status_label: "Spedito", storefront: "de", currency: "EUR", product_name: "Prodotto dimostrativo", ean: "1234567890123", sku: "AB_Online_1234567890123_40_60", quantity: 1,
  sale_amount: "60.00", shipping_amount: "0.00", commission_amount: "5.00", commission_rate: "8.3333", payout_amount: "55.00", purchase_cost: "40.00", purchase_cost_source: "Terzo valore dello SKU composto", profit_amount: "15.00", profit_pct: "37.5", sale_amount_eur: "60.00", shipping_amount_eur: "0.00", commission_amount_eur: "5.00", payout_amount_eur: "55.00", purchase_cost_eur: "40.00", profit_amount_eur: "15.00", monetary_warnings: [], details: { sku_supplier: "AB_Online", purchase_unit_cost_eur: "40", purchase_cost_method: "Costo unitario SKU × quantità", excluded_from_totals: false, payment_due_at: "2026-09-28T08:30:00+00:00", payment_days_remaining: 21, payment_available: false, payment_date_final: true, payment_status: "Tra 21 giorni", payment_rule: "Senza tracking: spedizione + 21 giorni", payment_source: "Senza tracking: spedizione + 21 giorni", ticket_delay_days: 0, ticket_open: false, ticket_count: 0, open_ticket_count: 0, ticket_ids: [], fx: { rate: "1", date: "", source: "EUR", online: null } } });
const job = () => ({ id: jobId, status: "running", processed: 100, total: null, progress: null, message: "Download ordini in corso.", error_code: null, ...scope, marketplace: "kaufland", maximum: 1000, include_details: true, created_at: "2026-09-07T09:00:00+00:00", started_at: "2026-09-07T09:00:01+00:00", finished_at: null });
const selection = () => ({ id: jobId, purpose: "orders", selected_ids: [lineId], selected_count: 1, filtered_count: 1, summary: { selected_rows: 1, distinct_orders: 1, quantity: 1, cancelled_rows: 0, sale_amount_eur: "60", commission_amount_eur: "5", payout_amount_eur: "55", purchase_cost_eur: "40", profit_amount_eur: "15", profit_pct: "37.5", complete_economic_rows: 1, missing_economic_rows: 0, known_cost_rows: 1, missing_cost_rows: 0, loss_rows: 0, sku_cost_rows: 1, catalog_cost_rows: 0, missing_currencies: [] } });
const paymentSelection = () => ({ id: paymentSelectionId, purpose: "payments", selected_ids: [], selected_count: 0, filtered_count: 1, summary: { ...selection().summary, selected_rows: 0, distinct_orders: 0, quantity: 0, sale_amount_eur: "0", commission_amount_eur: "0", payout_amount_eur: "0", purchase_cost_eur: "0", profit_amount_eur: "0", profit_pct: null, complete_economic_rows: 0, known_cost_rows: 0, sku_cost_rows: 0, payment_payable_rows: 0, payment_scheduled_rows: 0, payment_unscheduled_rows: 0, payment_unscheduled_ids: [], payment_available_rows: 0, payment_waiting_rows: 0, payment_all_dates_known: false, payment_all_available: false, latest_payment_due_at: null, available_payout_eur: "0", waiting_payout_eur: "0" } });
const list = () => ({ seller_id: sellerId, ...scope, marketplace: "kaufland", can_sync: true, items: [item()], total: 1, page: 1, page_size: 50, latest_job: job(), filters: { statuses: ["sent"], storefronts: ["de"], currencies: ["EUR"], carriers: [], date_min: "2026-09-07", date_max: "2026-09-07", amount_min: "0.00", amount_max: "100.00" }, selection: selection(), payment_selection: paymentSelection() });
const plain = (value) => JSON.parse(JSON.stringify(value));
function load(file, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, setTimeout, clearTimeout, URL, URLSearchParams, ...context });
  return exports;
}
const settings = load("../app/lib/seller-settings-types.ts");
const types = load("../app/lib/orders-types.ts", { require: () => settings });
function proxy(fetchImpl) {
  class NextResponse extends Response { static json(value, options) { return new NextResponse(JSON.stringify(value), options); } }
  return load("../app/lib/orders-proxy.ts", { fetch: fetchImpl, require(name) {
    if (name === "next/server") return { NextResponse };
    if (name === "./api-url") return { apiUrl: "http://api.test" };
    if (name === "./seller-settings-types") return settings;
    if (name === "./orders-types") return types;
    throw new Error(name);
  } }).ordersProxy;
}
const request = (params = types.ordersQueryString(query), payload, headers = {}) => ({ url: `https://app.test/api/orders?${params}`, headers: new Headers({ cookie: "mh_session=test-existing", ...headers }), json: async () => payload });

test("order DTO preserves composite SKU, quantity, economic provenance and missing monetary values", () => {
  const source = { ...item(), purchase_cost: null, purchase_cost_eur: null, profit_amount: null, profit_amount_eur: null, profit_pct: null, monetary_warnings: ["Costo non calcolabile"] };
  const value = types.readOrderItem(source);
  assert.equal(value.sku, "AB_Online_1234567890123_40_60");
  assert.equal(value.purchase_cost_eur, null);
  assert.equal(value.details.sku_supplier, "AB_Online");
  assert.equal(value.details.purchase_cost_method, "Costo unitario SKU × quantità");
  assert.equal(value.quantity, 1);
  assert.equal(types.readOrderItem({ ...item(), details: { ...item().details, detail_checked_at: "2026-09-07T10:00:00Z" } }).details.detail_checked_at, "2026-09-07T10:00:00Z");
  assert.equal(types.readOrderItem({ ...item(), details: { ...item().details, detail_checked_at: "bad date" } }), null);
  assert.equal(types.formatMoney(null), "—");
  assert.equal(types.formatMoney("0"), "0,00 €");
  assert.equal(types.formatPercent("37.5"), "37,5%");
});

test("Kaufland payment and ticket contract accepts available, waiting, unknown and open-ticket states", () => {
  const available = types.readOrderItem({ ...item(), details: { ...item().details, payment_days_remaining: -2, payment_available: true, payment_status: "Disponibile da 2 giorni" } });
  const waiting = types.readOrderItem(item());
  const unknown = types.readOrderItem({ ...item(), details: { ...item().details, payment_due_at: "", payment_days_remaining: null, payment_date_final: false, payment_status: "Data di spedizione non disponibile" } });
  const ticket = types.readOrderItem({ ...item(), details: { ...item().details, payment_date_final: false, payment_status: "Ticket aperto · data in aggiornamento", ticket_delay_days: 3.5, ticket_open: true, ticket_count: 2, open_ticket_count: 1, ticket_ids: ["T-1", "T-2"] } });
  const releasedWithOpenTicket = types.readOrderItem({ ...item(), details: { ...item().details, released_at: "2026-09-01T08:30:00+00:00", released_source: "API Kaufland", payment_due_at: "2026-09-01T08:30:00+00:00", payment_days_remaining: -6, payment_available: true, payment_date_final: true, payment_status: "Disponibile da 6 giorni", payment_rule: "Data effettiva comunicata da Kaufland", payment_source: "API Kaufland", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] } });
  const futureReleaseWithOpenTicket = types.readOrderItem({ ...item(), details: { ...item().details, released_at: "2026-10-01T08:30:00+00:00", released_source: "API Kaufland", payment_due_at: "2026-10-01T08:30:00+00:00", payment_days_remaining: 24, payment_available: false, payment_date_final: true, payment_status: "Disponibile tra 24 giorni", payment_rule: "Data effettiva comunicata da Kaufland", payment_source: "API Kaufland", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] } });
  const autopaidWithOpenTicket = types.readOrderItem({ ...item(), status: "sent_and_autopaid", details: { ...item().details, payment_due_at: "", payment_days_remaining: null, payment_available: true, payment_date_final: false, payment_status: "Ricavato già disponibile · data non disponibile", payment_rule: "Pagamento confermato da Kaufland", payment_source: "Pagamento confermato da Kaufland", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] } });
  assert.equal(available.details.payment_available, true);
  assert.equal(waiting.details.payment_available, false);
  assert.equal(unknown.details.payment_due_at, null);
  assert.equal(ticket.details.ticket_open, true);
  assert.equal(ticket.details.ticket_delay_days, 3.5);
  assert.equal(releasedWithOpenTicket.details.open_ticket_count, 1);
  assert.equal(futureReleaseWithOpenTicket.details.payment_available, false);
  assert.equal(autopaidWithOpenTicket.details.payment_available, true);
  for (const details of [
    { ...item().details, payment_days_remaining: 1.5 },
    { ...item().details, ticket_delay_days: -1 },
    { ...item().details, ticket_open: true, open_ticket_count: 0 },
    { ...item().details, ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] },
    { ...item().details, ticket_count: 1, ticket_ids: ["T-1", "T-2"] },
    { ...item().details, payment_rule: "Data effettiva comunicata da Kaufland", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] },
    { ...item().details, payment_rule: "Data effettiva comunicata da Kaufland", payment_available: true, payment_date_final: true, released_at: "2026-09-01T08:30:00+00:00", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] },
    { ...item().details, payment_rule: "Pagamento confermato da Kaufland", payment_available: true, payment_date_final: false, payment_due_at: "", ticket_open: false, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-1"] },
  ]) assert.equal(types.readOrderItem({ ...item(), details }), null);
  assert.equal(types.readOrderItem({ ...item(), details: { ...releasedWithOpenTicket.details, ticket_delay_days: 0.5 } }), null);
  assert.equal(types.readOrderItem({ ...item(), status: "sent_and_autopaid", details: { ...autopaidWithOpenTicket.details, ticket_delay_days: 1 } }), null);
  assert.equal(types.readOrderItem({ ...item(), status: "sent_and_autopaid", details: { ...autopaidWithOpenTicket.details, payment_days_remaining: 0 } }), null);
});

test("order DTO strips payloads and unknown private detail fields while rejecting malformed economics", () => {
  const source = { ...item(), raw: { api_key: "secret-never-echo" }, credentials: "secret-never-echo", details: { ...item().details, raw: "secret-never-echo", api_key: "secret-never-echo" } };
  assert.equal(JSON.stringify(types.readOrderItem(source)).includes("secret-never-echo"), false);
  for (const change of [{ quantity: 0 }, { quantity: 1.5 }, { sale_amount: "NaN" }, { sale_amount: 10 }, { sale_amount: "1e309" }, { created_at: "yesterday" }, { marketplace: "amazon" }, { monetary_warnings: [12] }, { details: { ...item().details, purchase_cost_method: 12 } }, { details: { fx: { rate: "Infinity", date: "", source: "", online: false } } }]) assert.equal(types.readOrderItem({ ...item(), ...change }), null);
});

test("job state validates identity, scope, environment and safe messages including partial-success summaries", () => {
  for (const message of ["Download ordini cancelled: 100", "Download ordini Worten: 200 ordini, 241 righe", "Sincronizzazione completata. 2 righe con avvisi: consulta i dettagli degli ordini. Dettagli verificati: 30.", "Sincronizzazione completata. Ticket non aggiornati; conservato l’ultimo snapshot disponibile.", "Sincronizzazione completata. 2 righe con avvisi: consulta i dettagli degli ordini. Dettagli verificati: 30. Ticket non aggiornati; conservato l’ultimo snapshot disponibile."]) assert.equal(types.readOrderJob({ ...job(), message }, scope).message, message);
  assert.equal(types.readOrderJob({ ...job(), message: "Sincronizzazione completata. Ticket non aggiornati; dump=private" }, scope).message, "Download ordini in corso.");
  assert.equal(types.readOrderJob({ ...job(), message: "secret-never-echo" }, scope).message, "Download ordini in corso.");
  assert.equal(types.readOrderJob({ ...job(), status: "error", error_code: "invalid_response", message: "raw response" }, scope).message, types.orderJobErrors.invalid_response);
  assert.equal(types.readOrderJob({ ...job(), status: "error", error_code: "account_busy", message: "raw response" }, scope).message, types.orderJobErrors.account_busy);
  for (const change of [{ account_id: otherAccountId }, { environment: "playground" }, { progress: 101 }, { processed: -1 }, { maximum: 99 }, { error_code: "unknown-private-error" }, { started_at: "invalid" }]) assert.equal(types.readOrderJob({ ...job(), ...change }, scope), null);
  assert.equal(types.readOrderJob({ ...job(), marketplace: "worten", environment: "playground" }), null);
});

test("list cannot cross Seller/account/environment or carry mismatched providers and duplicate line identities", () => {
  const parsed = types.readOrdersList(list(), sellerId, query);
  assert.equal(parsed.items.length, 1); assert.equal(parsed.selection.purpose, "orders"); assert.equal(parsed.payment_selection.purpose, "payments");
  for (const change of [{ seller_id: "foreign" }, { account_id: otherAccountId }, { environment: "playground" }, { page: 2 }, { page_size: 25 }, { items: [item(), item()] }, { latest_job: { ...job(), marketplace: "worten" } }, { items: [{ ...item(), marketplace: "worten" }] }]) assert.equal(types.readOrdersList({ ...list(), ...change }, sellerId, query), null);
  assert.equal(types.readOrdersList({ ...list(), payment_selection: undefined }, sellerId, query), null);
  assert.equal(types.readOrdersList({ ...list(), payment_selection: { ...paymentSelection(), purpose: "orders" } }, sellerId, query), null);
  assert.equal(types.readOrdersList({ ...list(), payment_selection: { ...paymentSelection(), filtered_count: 0 } }, sellerId, query), null);
  const foreignPayment = { ...paymentSelection(), selected_ids: [otherAccountId], selected_count: 1, summary: { ...selection().summary,
    payment_payable_rows: 1, payment_scheduled_rows: 1, payment_unscheduled_rows: 0, payment_unscheduled_ids: [], payment_available_rows: 0, payment_waiting_rows: 1,
    payment_all_dates_known: true, payment_all_available: false, latest_payment_due_at: "2026-09-28T08:30:00+00:00", available_payout_eur: "0", waiting_payout_eur: "55" } };
  assert.equal(types.readOrdersList({ ...list(), payment_selection: foreignPayment }, sellerId, query), null);
});

test("filters retain repeated statuses/storefronts and reject invalid calendar dates, reversals and huge pages", () => {
  const value = { ...query, search: "SKU _ +", statuses: ["sent", "cancelled"], storefronts: ["de", "pl"], date_from: "2026-09-01", date_to: "2026-09-07" };
  assert.deepEqual(plain(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString(value)))), value);
  for (const change of [{ date_from: "2026-02-30" }, { date_from: "2026-09-08", date_to: "2026-09-07" }, { page: 0 }, { page_size: 101 }, { search: "a".repeat(201) }, { account_id: "../other" }, { environment: "prod" }]) assert.equal(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString({ ...query, ...change }))), null);
});

test("sync validates limits, retains all-orders null and strips caller-supplied marketplace or secrets", () => {
  assert.deepEqual(plain(types.readOrdersSyncInput({ ...scope, maximum: null, include_details: true, marketplace: "untrusted", api_key: "private" })), { ...scope, maximum: null, include_details: true });
  for (const maximum of [0, -1, 501, "1000", undefined]) assert.equal(types.readOrdersSyncInput({ ...scope, maximum, include_details: true }), null);
});

test("BFF list forwards authenticated fixed Seller route, filters, deadlines and no-store", async () => {
  const run = proxy(async (url, options) => {
    assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders?${types.ordersQueryString(query)}`);
    assert.equal(options.method, "GET"); assert.equal(options.headers.cookie, "mh_session=test-existing");
    assert.equal(options.cache, "no-store"); assert.equal(options.redirect, "error"); assert.ok(options.signal instanceof AbortSignal);
    return Response.json(list());
  });
  const response = await run(request(), sellerId, "list");
  assert.equal(response.status, 200); assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.json()).items[0].id, lineId);
});

test("BFF synchronization sends one sanitized command and validates the queued job against chosen scope", async () => {
  let calls = 0;
  const input = { ...scope, maximum: 500, include_details: true };
  const run = proxy(async (url, options) => { calls++; assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/sync`); assert.equal(options.method, "POST"); assert.deepEqual(JSON.parse(options.body), input); return Response.json({ job: job() }); });
  const response = await run(request("", { ...input, credentials: "private" }, { origin: "https://app.test" }), sellerId, "sync");
  assert.equal(response.status, 202); assert.equal(calls, 1);
  const wrong = proxy(async () => Response.json({ job: { ...job(), account_id: otherAccountId } }));
  assert.equal((await wrong(request("", input), sellerId, "sync")).status, 502);
});

test("BFF refuses path injection, missing session, cross-origin writes and invalid scopes before network", async () => {
  let calls = 0; const run = proxy(async () => { calls++; throw new Error(); });
  const input = { ...scope, maximum: 1000, include_details: true };
  assert.equal((await run(request(), "../foreign", "list")).status, 422);
  assert.equal((await run(request(), sellerId, "detail", "../foreign")).status, 422);
  assert.equal((await run(request(undefined, undefined, { cookie: "" }), sellerId, "list")).status, 401);
  assert.equal((await run(request("", input, { origin: "https://other.test" }), sellerId, "sync")).status, 403);
  assert.equal((await run(request("", { ...input, maximum: 999 }), sellerId, "sync")).status, 422);
  assert.equal((await run(request("account_id=invalid"), sellerId, "list")).status, 422);
  assert.equal(calls, 0);
});

test("BFF detail and polling check requested identities and never expose raw marketplace credentials", async () => {
  const detail = proxy(async (url) => { assert.equal(url, `http://api.test/v1/sellers/${sellerId}/orders/${lineId}?account_id=${accountId}&environment=live`); return Response.json({ item: { ...item(), raw: "secret-never-echo" } }); });
  const response = await detail(request(), sellerId, "detail", lineId);
  assert.equal(response.status, 200); assert.equal((await response.text()).includes("secret-never-echo"), false);
  const wrong = proxy(async () => Response.json({ job: { ...job(), id: lineId } }));
  assert.equal((await wrong(request(), sellerId, "job", jobId)).status, 502);
});

test("BFF upstream outages, HTML and permission failures produce safe errors without automatic POST retry", async () => {
  for (const status of [401, 403, 404, 422, 500, 502]) {
    let calls = 0; const run = proxy(async () => { calls++; return new Response("private-api-response", { status }); });
    const response = await run(request(), sellerId, "list");
    assert.equal(response.status, status >= 500 ? 503 : status); assert.equal(calls, 1);
    assert.equal((await response.text()).includes("private-api-response"), false);
  }
  const html = proxy(async () => new Response("<html>private-proxy-error</html>"));
  assert.equal((await html(request(), sellerId, "list")).status, 502);
  let calls = 0; const offline = proxy(async () => { calls++; throw new Error("private-timeout"); });
  const result = await offline(request("", { ...scope, maximum: 1000, include_details: true }), sellerId, "sync");
  assert.equal(result.status, 503); assert.equal(calls, 1); assert.equal((await result.text()).includes("private-timeout"), false);
});

test("order dates show Italy local time without changing unknown values to fabricated dates", () => {
  assert.equal(types.formatOrderDate(null), "—");
  assert.equal(types.formatOrderDate("bad"), "—");
  assert.match(types.formatOrderDate("2026-09-07T08:30:00Z"), /10:30/);
});
