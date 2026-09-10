const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const selectionId = "a3000000-0000-4000-8000-000000000001";
const paymentSelectionId = "a3000000-0000-4000-8000-000000000002";
const ids = Array.from({ length: 51 }, (_, index) => `a4000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`);
const query = { account_id: accountId, environment: "live", page: 1, page_size: 50, search: "", statuses: [], storefronts: [], currencies: [], carriers: [], date_from: "", date_to: "", status_selection: "all", storefront_selection: "all", currency_selection: "all", tracking: "all", commission: "all", payment: "all", amount_min: "", amount_max: "" };
const account = { id: accountId, marketplace: "kaufland", account_name: "Account di prova", active: true, connection_status: "connected" };
const plain = (value) => JSON.parse(JSON.stringify(value));
function load(file, context = {}) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, { exports, AbortController, AbortSignal, URL, URLSearchParams, ...context });
  return exports;
}
const settings = load("../app/lib/seller-settings-types.ts");
const types = load("../app/lib/orders-types.ts", { require: () => settings });
const item = (id) => ({ id, external_line_id: id, order_id: `ORD-${id.slice(-3)}`, marketplace: "kaufland", created_at: "2026-09-07T10:00:00Z", status: "sent", status_label: "Spedito", storefront: "de", currency: "EUR", product_name: "Caffè demo", ean: "1234567890123", sku: "in_codice_10_20", quantity: 1, sale_amount: "20", shipping_amount: "0", commission_amount: "2", commission_rate: "10", payout_amount: "18", purchase_cost: "10", purchase_cost_source: "SKU composto", profit_amount: "8", profit_pct: "80", sale_amount_eur: "20", shipping_amount_eur: "0", commission_amount_eur: "2", payout_amount_eur: "18", purchase_cost_eur: "10", profit_amount_eur: "8", monetary_warnings: [], details: { carrier: "DHL", tracking: "TRACK-DEMO", shipped_at: "2026-09-08T10:00:00Z", received_at: "2026-09-10T10:00:00Z", purchase_cost_method: "Costo unitario SKU × quantità", payment_due_at: "", payment_days_remaining: null, payment_available: false, payment_date_final: false, payment_status: "Data di spedizione non disponibile", payment_rule: "Senza tracking: spedizione + 21 giorni", payment_source: "Senza tracking: spedizione + 21 giorni", ticket_delay_days: 0, ticket_open: false, ticket_count: 0, open_ticket_count: 0, ticket_ids: [] } });
const economicSummary = (count) => ({ selected_rows: count, distinct_orders: count, quantity: count, cancelled_rows: 0, sale_amount_eur: count ? "987.65" : "0", commission_amount_eur: "0", payout_amount_eur: "0", purchase_cost_eur: "0", profit_amount_eur: "0", profit_pct: null, complete_economic_rows: count, missing_economic_rows: 0, known_cost_rows: count, missing_cost_rows: 0, loss_rows: 0, sku_cost_rows: count, catalog_cost_rows: 0, missing_currencies: [] });
const paymentSummary = (count) => ({ ...economicSummary(count), payment_payable_rows: count, payment_scheduled_rows: 0, payment_unscheduled_rows: count, payment_unscheduled_ids: ids.slice(0, count), payment_available_rows: 0, payment_waiting_rows: count, payment_all_dates_known: false, payment_all_available: false, latest_payment_due_at: null, available_payout_eur: "0", waiting_payout_eur: "0" });
const pageIds = (page) => page === 1 ? ids.slice(0, 2) : ids.slice(2, 3);
const selection = (selected = new Set(ids), page = 1) => ({ id: selectionId, purpose: "orders", selected_ids: pageIds(page).filter((id) => selected.has(id)), selected_count: selected.size, filtered_count: ids.length, summary: economicSummary(selected.size) });
const paymentSelection = (selected = new Set(), ordersSelected = new Set(ids), page = 1) => ({ id: paymentSelectionId, purpose: "payments", selected_ids: pageIds(page).filter((id) => selected.has(id) && ordersSelected.has(id)), selected_count: selected.size, filtered_count: ordersSelected.size, summary: paymentSummary(selected.size) });
const list = (selected = new Set(ids), page = 1, paymentSelected = new Set()) => ({ seller_id: sellerId, account_id: accountId, environment: "live", marketplace: "kaufland", can_sync: false, total: ids.length, page, page_size: 50, latest_job: null, filters: { statuses: ["sent", "cancelled"], storefronts: ["de", "pl"], currencies: ["EUR", "PLN", ""], carriers: ["DHL"], date_min: "2026-09-07", date_max: "2026-09-07", amount_min: "0.00", amount_max: "100.00" }, selection: selection(selected, page), payment_selection: paymentSelection(paymentSelected, selected, page), items: pageIds(page).map(item) });

test("all and no values are distinct in each filter, including an unknown original currency", () => {
  for (const [mode, field, param] of [["status_selection", "statuses", "status"], ["storefront_selection", "storefronts", "storefront"], ["currency_selection", "currencies", "currency"]]) {
    const none = { ...query, [mode]: "selected", [field]: [] };
    const read = types.readOrdersQuery(new URLSearchParams(types.ordersQueryString(none)));
    assert.deepEqual(plain(types.ordersFilterInput(read)[field]), []);
    assert.equal(types.ordersFilterInput(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString(query))))[field], null);
    assert.equal(new URLSearchParams(types.ordersQueryString(none)).getAll(param).length, 0);
  }
  const unknown = types.readOrdersQuery(new URLSearchParams(types.ordersQueryString({ ...query, currency_selection: "selected", currencies: [""] })));
  assert.deepEqual(plain(types.ordersFilterInput(unknown).currencies), [""]);
});

test("advanced filters preserve payment state, inclusive EUR bounds and reject invalid values", () => {
  const filter = { ...query, amount_min: "0", amount_max: "12.50", tracking: "missing", commission: "present", payment: "waiting", carriers: ["DHL"] };
  assert.deepEqual(plain(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString(filter)))), filter);
  assert.equal(types.ordersFilterInput(filter).payment, "waiting");
  for (const change of [{ amount_min: "-1" }, { amount_max: "Infinity" }, { amount_min: "15", amount_max: "10" }, { tracking: "other" }, { commission: "false" }, { payment: "paid_later" }]) assert.equal(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString({ ...query, ...change }))), null);
});

test("non-Kaufland orders state that the payment program is unavailable", async () => {
  const wortenAccount = { ...account, marketplace: "worten", account_name: "Account Worten" };
  const value = list();
  delete value.payment_selection;
  value.marketplace = "worten";
  value.items = value.items.map((entry) => ({ ...entry, marketplace: "worten" }));
  const panel = component(async () => Response.json(value), wortenAccount);
  await panel.settle();
  assert.match(panel.text(), /Programma pagamenti non disponibile per questo marketplace\./);
  assert.equal(panel.text().includes("Date previste di pagamento"), false);
  assert.equal(panel.select("Pagamento"), undefined);
  panel.unmount();
});

test("payment program shows available, waiting, unknown, open-ticket and cancelled rows with payment-only totals", async () => {
  const paymentItems = [
    { ...item(ids[0]), details: { ...item(ids[0]).details, released_at: "2026-09-05T10:00:00Z", released_source: "API Kaufland", payment_due_at: "2026-09-05T10:00:00Z", payment_days_remaining: -2, payment_available: true, payment_date_final: true, payment_status: "Disponibile da 2 giorni", payment_rule: "Data effettiva comunicata da Kaufland", payment_source: "API Kaufland" } },
    { ...item(ids[1]), details: { ...item(ids[1]).details, payment_due_at: "2026-09-30T10:00:00Z", payment_days_remaining: 4, payment_date_final: true, payment_status: "Tra 4 giorni", payment_rule: "Con tracking: consegna + 14 giorni", payment_source: "Con tracking: consegna + 14 giorni" } },
    { ...item(ids[2]), purchase_cost: null, purchase_cost_eur: null, profit_amount: null, profit_amount_eur: null, profit_pct: null, purchase_cost_source: "Costo non calcolabile", details: { ...item(ids[2]).details, purchase_cost_method: "Costo non calcolabile", payment_status: "Tracking presente · consegna non ancora rilevata", payment_rule: "Con tracking: consegna + 14 giorni", payment_source: "Con tracking: consegna + 14 giorni" } },
    { ...item(ids[3]), details: { ...item(ids[3]).details, payment_due_at: "2026-10-05T10:00:00Z", payment_days_remaining: 9, payment_status: "Ticket aperto · data in aggiornamento", payment_rule: "Con tracking: consegna + 14 giorni", payment_source: "Con tracking: consegna + 14 giorni", ticket_delay_days: 5, ticket_open: true, ticket_count: 1, open_ticket_count: 1, ticket_ids: ["T-OPEN"] } },
    { ...item(ids[4]), status: "cancelled", status_label: "Cancellato", payout_amount_eur: "999", purchase_cost_eur: "999", profit_amount_eur: "999", details: { ...item(ids[4]).details, excluded_from_totals: true, payment_due_at: "2026-10-06T10:00:00Z", payment_days_remaining: 10, payment_status: "Ticket aperto · data in aggiornamento", payment_rule: "Con tracking: consegna + 14 giorni", payment_source: "Con tracking: consegna + 14 giorni", ticket_delay_days: 6, ticket_open: true, ticket_count: 2, open_ticket_count: 1, ticket_ids: ["T-CANCEL"] } },
  ];
  const selectedSummary = { ...economicSummary(5), cancelled_rows: 1, sale_amount_eur: "80", commission_amount_eur: "8", payout_amount_eur: "72", purchase_cost_eur: "30", profit_amount_eur: "24", known_cost_rows: 3, missing_cost_rows: 1, sku_cost_rows: 3 };
  const value = { ...list(new Set(ids.slice(0, 5))), total: 5, items: paymentItems, latest_job: null,
    selection: { id: selectionId, purpose: "orders", selected_ids: ids.slice(0, 5), selected_count: 5, filtered_count: 5, summary: selectedSummary },
    payment_selection: { id: paymentSelectionId, purpose: "payments", selected_ids: ids.slice(0, 5), selected_count: 5, filtered_count: 5,
      summary: { ...selectedSummary, payment_payable_rows: 4, payment_scheduled_rows: 2, payment_unscheduled_rows: 2, payment_unscheduled_ids: [ids[2], ids[3]], payment_available_rows: 1, payment_waiting_rows: 3,
        payment_all_dates_known: false, payment_all_available: false, latest_payment_due_at: "2026-09-30T10:00:00Z", available_payout_eur: "18", waiting_payout_eur: "54" } } };
  const panel = component(async (url) => String(url).includes(`/orders/${ids[0]}?`) ? Response.json({ item: paymentItems[0] }) : Response.json(value));
  await panel.settle();
  assert.match(panel.text(), /Date previste di pagamento/);
  assert.match(panel.text(), /Disponibile da 2 giorni/);
  assert.match(panel.text(), /Tra 4 giorni/);
  assert.match(panel.text(), /Data da definire/);
  assert.match(panel.text(), /Ticket aperto/);
  assert.match(panel.text(), /Netto disponibile18,00/);
  assert.match(panel.text(), /Righe scelte5/);
  assert.match(panel.text(), /Netto totale72,00/);
  assert.match(panel.text(), /Costo rilevato30,00/);
  assert.match(panel.text(), /Guadagno rilevato24,00/);
  assert.match(panel.text(), /Netto in attesa54,00/);
  assert.match(panel.text(), /2 righe non hanno ancora una data definitiva/);
  assert.match(panel.text(), /1 riga ha un costo ignoto/);
  assert.match(panel.text(), /1 riga cancellata è conteggiata/);
  assert.match(panel.text(), /1 ticket1 aperto/);
  for (const heading of ["Nazione / stato ordine", "Tracking", "Spedito il", "Ricevuto il", "Giorni", "Netto", "Costo", "Guadagno", "Metodo / fonte costo", "Stato / regola pagamento", "Ritardo / ticket / ID"]) assert.match(panel.text(), new RegExp(heading.replace("/", "\\/")));
  assert.match(panel.text(), /TRACK-DEMO/); assert.match(panel.text(), /Costo unitario SKU × quantità/); assert.match(panel.text(), /Fonte: SKU composto/);
  assert.match(panel.text(), /4 mancanti/); assert.match(panel.text(), /Regola: Con tracking: consegna \+ 14 giorni/); assert.match(panel.text(), /ID: T-OPEN/);
  const cancelled = panel.paymentRowText(ids[4]);
  assert.match(cancelled, /Cancellato/); assert.match(cancelled, /Ordine cancellato/); assert.match(cancelled, /Non dovuto/);
  assert.equal((cancelled.match(/0,00/g) || []).length, 3); assert.equal(cancelled.includes("999,00"), false);
  assert.match(cancelled, /Costo unitario SKU × quantità/); assert.match(cancelled, /Fonte: SKU composto/);
  assert.match(cancelled, /Regola: Con tracking: consegna \+ 14 giorni/); assert.match(cancelled, /Fonte data: Con tracking: consegna \+ 14 giorni/);
  assert.match(cancelled, /0 ticket aperti/); assert.match(cancelled, /Ritardo accumulato: 0 giorni/); assert.match(cancelled, /ID: T-CANCEL/);
  panel.buttonStarts("Apri").props.onClick(); panel.render(); await panel.settle();
  assert.match(panel.text(), /Data effettiva comunicata da Kaufland/);
  assert.match(panel.text(), /API Kaufland/);
  panel.select("Pagamento").props.onChange({ target: { value: "ticket_open" } }); panel.render();
  panel.nodes().find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); panel.render(); await panel.settle();
  assert.equal(new URL(panel.requests.at(-1)[0], "http://local").searchParams.get("payment"), "ticket_open");
  panel.unmount();
});

test("payment selection starts empty and exposes only rows chosen in the orders selection", async () => {
  const panel = component(async () => Response.json(list(new Set([ids[0]]))));
  await panel.settle();
  assert.equal(panel.rowCheckbox(ids[0]).props.checked, true);
  assert.equal(panel.rowCheckbox(ids[1]).props.checked, false);
  assert.equal(panel.paymentCheckbox(ids[0]).props.checked, false);
  assert.equal(panel.paymentCheckbox(ids[1]), undefined);
  assert.match(panel.text(), /0 di 1 nel riepilogo/);
  assert.match(panel.text(), /Nessuna riga scelta/);
  assert.equal(panel.text().includes("Netto disponibile"), false);
  panel.unmount();
});

test("orders and payment selections remain independent and use separate commands", async () => {
  let ordersSelected = new Set(ids), paymentsSelected = new Set(), commands = [];
  const apply = (selected, body) => {
    if (body.action === "clear") return new Set();
    if (body.action === "select_all") return new Set(body.purpose === "payments" ? [...ordersSelected] : ids);
    const next = new Set(selected); if (body.selected) next.add(body.line_id); else next.delete(body.line_id); return next;
  };
  const panel = component(async (url, options) => {
    if (!url.endsWith("/selection")) return Response.json(list(ordersSelected, 1, paymentsSelected));
    const body = JSON.parse(options.body); commands.push(body);
    if (body.purpose === "payments") {
      paymentsSelected = apply(paymentsSelected, body);
      return Response.json({ selection: { ...paymentSelection(paymentsSelected, ordersSelected), selected_ids: [] } });
    }
    ordersSelected = apply(ordersSelected, body); paymentsSelected = new Set([...paymentsSelected].filter((id) => ordersSelected.has(id)));
    return Response.json({ selection: { ...selection(ordersSelected), selected_ids: [] } });
  });
  await panel.settle();
  panel.paymentCheckbox(ids[0]).props.onChange({ target: { checked: true } }); panel.render(); await panel.settle();
  assert.equal(commands[0].purpose, "payments"); assert.equal(commands[0].selection_id, paymentSelectionId); assert.equal(commands[0].orders_selection_id, selectionId);
  assert.equal(Object.hasOwn(commands[0], "main_selection_id"), false);
  assert.equal(panel.rowCheckbox(ids[0]).props.checked, true); assert.equal(panel.paymentCheckbox(ids[0]).props.checked, true);
  panel.button("Seleziona tutte per i pagamenti").props.onClick(); panel.render(); await panel.settle();
  assert.equal(commands[1].purpose, "payments"); assert.equal(commands[1].action, "select_all");
  panel.button("Deseleziona tutte dai pagamenti").props.onClick(); panel.render(); await panel.settle();
  assert.equal(commands[2].purpose, "payments"); assert.equal(commands[2].action, "clear");
  panel.rowCheckbox(ids[1]).props.onChange({ target: { checked: false } }); panel.render(); await panel.settle();
  assert.equal(commands[3].purpose, "orders"); assert.equal(commands[3].selection_id, selectionId); assert.equal(Object.hasOwn(commands[3], "orders_selection_id"), false);
  assert.equal(panel.rowCheckbox(ids[1]).props.checked, false); assert.equal(panel.paymentCheckbox(ids[1]), undefined);
  panel.unmount();
});

test("changing payment filter reloads the independent payment selection for the new context", async () => {
  const panel = component(async (url) => {
    const paymentFilter = new URL(url, "http://local").searchParams.get("payment");
    return Response.json(list(new Set(ids), 1, paymentFilter === "waiting" ? new Set() : new Set([ids[0]])));
  });
  await panel.settle(); assert.equal(panel.paymentCheckbox(ids[0]).props.checked, true);
  panel.select("Pagamento").props.onChange({ target: { value: "waiting" } }); panel.render();
  panel.nodes().find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); panel.render(); await panel.settle();
  assert.equal(new URL(panel.requests.at(-1)[0], "http://local").searchParams.get("payment"), "waiting");
  assert.equal(panel.paymentCheckbox(ids[0]).props.checked, false); assert.match(panel.text(), /Nessuna riga scelta/);
  panel.unmount();
});

test("default date and EUR intervals come from the whole archive metadata before orders are shown", async () => {
  const panel = component(async () => Response.json(list()));
  assert.equal(panel.text().includes("Caffè demo"), false);
  await panel.settle();
  assert.equal(panel.requests.length, 2);
  const applied = new URL(panel.requests[1][0], "http://local").searchParams;
  assert.equal(applied.get("date_from"), "2026-09-07"); assert.equal(applied.get("date_to"), "2026-09-07");
  assert.equal(applied.get("amount_min"), "0.00"); assert.equal(applied.get("amount_max"), "100.00");
  assert.match(panel.text(), /Caffè demo/);
  panel.button("Ripristina filtri iniziali").props.onClick(); panel.render(); await panel.settle();
  const reset = new URL(panel.requests.at(-1)[0], "http://local").searchParams;
  assert.equal(reset.get("amount_max"), "100.00"); panel.unmount();
});

test("first successful import replaces empty-archive bounds instead of leaving all new sales outside zero range", async () => {
  let populated = false;
  const empty = { ...list(), total: 0, items: [], selection: { ...selection(new Set()), filtered_count: 0 }, payment_selection: { ...paymentSelection(), filtered_count: 0 }, filters: { ...list().filters, date_min: null, date_max: null, amount_min: null, amount_max: null } };
  const panel = component(async () => Response.json(populated ? list() : empty));
  await panel.settle(); assert.match(panel.text(), /Nessun ordine/);
  populated = true; await panel.button("Aggiorna dati").props.onClick(); await panel.settle(); await panel.settle();
  const applied = new URL(panel.requests.at(-1)[0], "http://local").searchParams;
  assert.equal(applied.get("amount_max"), "100.00"); assert.match(panel.text(), /Caffè demo/); panel.unmount();
});

test("incomplete EUR amounts are not mislabeled as a missing EUR exchange rate and cancelled rows are not labeled complete active rows", async () => {
  const value = list(); value.selection.summary = { ...value.selection.summary, complete_economic_rows: 50, cancelled_rows: 9, missing_economic_rows: 1, missing_currencies: ["EUR"] };
  const panel = component(async () => Response.json(value)); await panel.settle();
  assert.match(panel.text(), /Valute delle righe con importi incompleti: EUR/);
  assert.equal(panel.text().includes("Cambio EUR non disponibile"), false);
  assert.match(panel.text(), /41 righe non cancellate con dati economici completi/); panel.unmount();
});

test("summary and page selection reject foreign IDs, malformed counts and private fields", () => {
  const valid = selection();
  assert.equal(types.readOrderSelection({ ...valid, private: "never-echo", summary: { ...valid.summary, raw: "never-echo" } }).summary.sale_amount_eur, "987.65");
  assert.equal(JSON.stringify(types.readOrderSelection({ ...valid, private: "never-echo" })).includes("never-echo"), false);
  for (const change of [{ selected_ids: ["bad"] }, { selected_ids: [ids[0], ids[0]] }, { selected_count: 52 }, { summary: { ...valid.summary, selected_rows: 1 } }, { summary: { ...valid.summary, loss_rows: 99 } }, { summary: { ...valid.summary, sale_amount_eur: null } }]) assert.equal(types.readOrderSelection({ ...valid, ...change }), null);
  assert.equal(types.readOrderSelection({ ...valid, purpose: "payments" }), null);
  assert.equal(types.readOrderSelection(paymentSelection(), "payments").purpose, "payments");
  const twoPayments = paymentSelection(new Set(ids.slice(0, 2)));
  assert.deepEqual(plain(types.readOrderSelection(twoPayments, "payments").summary.payment_unscheduled_ids), ids.slice(0, 2));
  assert.equal(types.readOrderSelection({ ...twoPayments, summary: { ...twoPayments.summary, payment_unscheduled_ids: [ids[0], ids[0]] } }, "payments"), null);
  assert.equal(types.readOrderSelection({ ...twoPayments, summary: { ...twoPayments.summary, payment_unscheduled_ids: [ids[0], "foreign"] } }, "payments"), null);
  assert.equal(types.readOrderSelection({ ...paymentSelection(), purpose: "orders" }, "payments"), null);
  assert.equal(types.readOrdersList({ ...list(), selection: { ...valid, selected_ids: [ids[10]] } }, sellerId, query), null);
});

test("selection/export bodies share strict filters and cannot carry caller-selected credentials or Seller IDs", () => {
  const body = { account_id: accountId, environment: "live", selection_id: selectionId, purpose: "orders", filters: types.ordersFilterInput(query), action: "set", line_id: ids[0], selected: false, secret_key: "private", seller_id: "other" };
  const parsed = types.readOrdersActionInput(body, "selection");
  assert.equal(JSON.stringify(parsed).includes("private"), false); assert.equal(parsed.selected, false); assert.equal(parsed.purpose, "orders");
  assert.equal(types.readOrdersActionInput({ ...body, line_id: "bad" }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, selected: 1 }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, filters: { ...body.filters, amount_min: "-0.01" } }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, purpose: undefined }, "selection"), null);
  const payments = types.readOrdersActionInput({ ...body, purpose: "payments", selection_id: paymentSelectionId, orders_selection_id: selectionId }, "selection");
  assert.equal(payments.purpose, "payments"); assert.equal(payments.orders_selection_id, selectionId);
  assert.equal(types.readOrdersActionInput({ ...body, purpose: "payments", selection_id: paymentSelectionId }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, purpose: "payments", selection_id: paymentSelectionId, main_selection_id: selectionId }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, kind: "selected" }, "export").kind, "selected");
  assert.equal(types.readOrdersActionInput({ ...body, kind: "all" }, "export"), null);
});

function component(fetchImpl, selectedAccount = account) {
  const states = [], refs = [], effects = [], callbacks = [], timers = new Map(), requests = [], navigations = [], downloads = [], revoked = [];
  let stateIndex, refIndex, effectIndex, callbackIndex, pendingEffects, tree, timerId = 0;
  const nodes = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(nodes) : [node, ...nodes(node.props?.children)];
  const text = (node) => typeof node === "string" ? node : typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  const element = (type, props) => typeof type === "function" && ["FilterChoices", "OrdersTotals", "PaymentSchedule", "PaymentScheduleRow", "PaymentSelectionSummary", "PaymentCell", "TicketCell", "OrderDetail"].includes(type.name) ? type(props) : ({ type, props });
  const router = { replace: (url) => navigations.push(url), refresh() {} };
  const browserDocument = { body: { appendChild() {} }, createElement() { const anchor = { href: "", download: "", click() { downloads.push({ href: this.href, filename: this.download }); }, remove() {} }; return anchor; } };
  class DownloadURL extends URL { static createObjectURL() { return "blob:orders-test"; } static revokeObjectURL(url) { revoked.push(url); } }
  const module = load("../app/components/SellerOrdersPanel.tsx", {
    document: browserDocument, URL: DownloadURL,
    setTimeout: (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout: (id) => timers.delete(id),
    fetch: async (...args) => { requests.push(args); return fetchImpl(...args); }, require(name) {
      if (name === "react") return {
        useState(initial) { const index = stateIndex++; if (!(index in states)) states[index] = typeof initial === "function" ? initial() : initial; return [states[index], (next) => { states[index] = typeof next === "function" ? next(states[index]) : next; }]; },
        useRef(initial) { const index = refIndex++; return refs[index] ?? (refs[index] = { current: initial }); },
        useEffect(create, dependencies) { const index = effectIndex++; if (!effects[index] || dependencies.some((value, position) => value !== effects[index].dependencies[position])) pendingEffects.push(() => { effects[index]?.cleanup?.(); effects[index] = { dependencies, cleanup: create() }; }); },
        useCallback(fn, dependencies) { const index = callbackIndex++; if (!callbacks[index] || dependencies.some((value, position) => value !== callbacks[index].dependencies[position])) callbacks[index] = { dependencies, fn }; return callbacks[index].fn; },
        useTransition: () => [false, (fn) => fn()],
      };
      if (name === "react/jsx-runtime") return { jsx: element, jsxs: element };
      if (name === "next/navigation") return { useRouter: () => router };
      if (name === "../lib/orders-types") return types;
      if (name === "../lib/marketplace-connections-types") return {};
      if (name === "./DashboardIcon") return { DashboardIcon: () => null };
      if (name === "./OrderTrackingPanel") return { OrderTrackingPanel: () => null };
      if (name.endsWith(".module.css")) return { default: new Proxy({}, { get: (_target, key) => key }) };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  function render() { stateIndex = refIndex = effectIndex = callbackIndex = 0; pendingEffects = []; tree = module.OrderAccountPanel({ sellerId, account: selectedAccount, environment: "live" }); pendingEffects.forEach((effect) => effect()); }
  render();
  return {
    render, requests, navigations, downloads, revoked, nodes: () => nodes(tree), text: () => text(tree),
    button: (label) => nodes(tree).find((node) => node.type === "button" && text(node) === label),
    buttonStarts: (label) => nodes(tree).find((node) => node.type === "button" && text(node).startsWith(label)),
    select: (label) => nodes(nodes(tree).find((node) => node.type === "label" && text(node).startsWith(label))).find((node) => node.type === "select"),
    rowCheckbox: (id) => nodes(tree).find((node) => node.type === "input" && node.props?.["aria-label"]?.endsWith(`riga ${id}`)),
    paymentCheckbox: (id) => nodes(tree).find((node) => node.type === "input" && node.props?.["aria-label"]?.endsWith(`riga ${id} nel riepilogo pagamenti`)),
    paymentRowText: (id) => text(nodes(tree).find((node) => node.type === "tr" && nodes(node).some((child) => child.type === "input" && child.props?.["aria-label"]?.endsWith(`riga ${id} nel riepilogo pagamenti`)))),
    filterCheckbox: (label) => nodes(nodes(tree).find((node) => node.type === "label" && text(node) === label)).find((node) => node.type === "input"),
    async settle() { await new Promise(setImmediate); render(); await new Promise(setImmediate); render(); },
    runTimers(ms) { for (const [id, timer] of timers) if (timer.ms === ms) { timers.delete(id); timer.fn(); } },
    unmount() { effects.forEach((effect) => effect?.cleanup?.()); },
  };
}

test("rechecking all status, country or currency checkboxes restores the original all-filter identity", async () => {
  for (const [label, marker, field] of [["Spedito", "status_selection", "status"], ["DE", "storefront_selection", "storefront"], ["EUR", "currency_selection", "currency"]]) {
    const panel = component(async () => Response.json(list())); await panel.settle();
    panel.filterCheckbox(label).props.onChange({ target: { checked: false } }); panel.render();
    assert.equal(panel.filterCheckbox(label).props.checked, false);
    panel.filterCheckbox(label).props.onChange({ target: { checked: true } }); panel.render();
    panel.nodes().find((node) => node.type === "form").props.onSubmit({ preventDefault() {} }); panel.render(); await panel.settle();
    const params = new URL(panel.requests.at(-1)[0], "http://local").searchParams;
    assert.equal(params.get(marker), "all"); assert.equal(params.getAll(field).length, 0); panel.unmount();
  }
});

test("rapid row clicks are serialized without dropping intent and totals come from the full server selection", async () => {
  const selected = new Set(ids), commands = [];
  let release, first = true;
  const panel = component(async (url, options) => {
    if (!url.endsWith("/selection")) return Response.json(list(selected));
    const body = JSON.parse(options.body); commands.push(body);
    if (first) { first = false; await new Promise((resolve) => { release = resolve; }); }
    if (body.selected) selected.add(body.line_id); else selected.delete(body.line_id);
    return Response.json({ selection: { ...selection(selected), selected_ids: [] } });
  });
  await panel.settle(); assert.match(panel.text(), /987,65/); assert.match(panel.text(), /51 di 51 righe selezionate/);
  panel.rowCheckbox(ids[0]).props.onChange({ target: { checked: false } }); panel.render();
  panel.rowCheckbox(ids[1]).props.onChange({ target: { checked: false } }); panel.render();
  panel.rowCheckbox(ids[0]).props.onChange({ target: { checked: true } }); panel.render();
  assert.equal(commands.length, 1); assert.equal(panel.button("Successiva").props.disabled, true);
  release(); await panel.settle(); await panel.settle();
  assert.deepEqual(commands.map((command) => [command.line_id, command.selected]), [[ids[0], false], [ids[1], false], [ids[0], true]]);
  assert.equal(panel.rowCheckbox(ids[0]).props.checked, true); assert.equal(panel.rowCheckbox(ids[1]).props.checked, false);
  assert.match(panel.text(), /50 di 51 righe selezionate/); panel.unmount();
});

test("global clear includes every filtered page and zero selected blocks both CSV exports", async () => {
  let selected = new Set(ids);
  const panel = component(async (url, options) => {
    if (url.endsWith("/selection")) { const body = JSON.parse(options.body); selected = body.action === "clear" ? new Set() : new Set(ids); return Response.json({ selection: { ...selection(selected), selected_ids: [] } }); }
    return Response.json(list(selected, Number(new URL(url, "http://local").searchParams.get("page"))));
  });
  await panel.settle(); panel.button("Successiva").props.onClick(); panel.render(); await panel.settle();
  assert.equal(panel.rowCheckbox(ids[2]).props.checked, true);
  panel.button("Deseleziona tutti").props.onClick(); panel.render(); await panel.settle();
  assert.match(panel.text(), /0 di 51 righe selezionate/);
  assert.equal(panel.button("Esporta CSV selezionati").props.disabled, true); assert.equal(panel.button("Esporta CSV tutti i filtrati").props.disabled, true);
  panel.button("Precedente").props.onClick(); panel.render(); await panel.settle();
  assert.equal(panel.rowCheckbox(ids[0]).props.checked, false);
  panel.button("Seleziona tutti i filtrati").props.onClick(); panel.render(); await panel.settle();
  assert.match(panel.text(), /51 di 51 righe selezionate/); panel.unmount();
});

test("an uncertain selection write blocks new operations until an explicit fresh read", async () => {
  const panel = component(async (url) => url.endsWith("/selection") ? new Response(null, { status: 503 }) : Response.json(list()));
  await panel.settle(); panel.rowCheckbox(ids[0]).props.onChange({ target: { checked: false } }); panel.render(); await panel.settle();
  assert.match(panel.text(), /selezione non confermata/);
  assert.equal(panel.rowCheckbox(ids[0]).props.disabled, true); assert.equal(panel.button("Esporta CSV selezionati").props.disabled, true);
  await panel.button("Aggiorna dati").props.onClick(); await panel.settle();
  assert.equal(panel.rowCheckbox(ids[0]).props.disabled, false); assert.equal(panel.rowCheckbox(ids[0]).props.checked, true);
  panel.unmount();
});

test("unmount aborts the selected account request and never sends queued actions into another context", async () => {
  let release;
  const panel = component(async (url) => url.endsWith("/selection") ? new Promise((resolve) => { release = resolve; }) : Response.json(list()));
  await panel.settle(); panel.rowCheckbox(ids[0]).props.onChange({ target: { checked: false } }); panel.render();
  panel.rowCheckbox(ids[1]).props.onChange({ target: { checked: false } }); panel.render();
  panel.unmount(); assert.equal(panel.requests.at(-1)[1].signal.aborted, true);
  release(Response.json({ selection: { ...selection(), selected_ids: [] } })); await panel.settle();
  assert.equal(panel.requests.filter(([url]) => url.endsWith("/selection")).length, 1);
});

test("read-only logistics can export CSV using the chosen server selection, then revokes the download URL", async () => {
  const panel = component(async (url, options) => {
    if (url.endsWith("/export")) { const body = JSON.parse(options.body); assert.equal(body.selection_id, selectionId); assert.equal(body.kind, "selected"); assert.equal(body.filters.statuses, null); return new Response("\uFEFFOrdine,Prodotto\r\n1,Caffè\r\n", { headers: { "content-type": "text/csv; charset=utf-8" } }); }
    return Response.json(list());
  });
  await panel.settle(); assert.equal(panel.button("Sincronizza ordini").props.disabled, true);
  await panel.button("Esporta CSV selezionati").props.onClick(); await panel.settle();
  assert.equal(panel.downloads.length, 1); assert.match(panel.downloads[0].filename, /kaufland-live-selezionati\.csv$/);
  panel.runTimers(1000); assert.deepEqual(panel.revoked, ["blob:orders-test"]); panel.unmount();
});

test("HTML export failures create no download and preserve the selection for retry", async () => {
  const panel = component(async (url) => url.endsWith("/export") ? new Response("<html>private upstream failure</html>") : Response.json(list()));
  await panel.settle(); await panel.button("Esporta CSV tutti i filtrati").props.onClick(); await panel.settle();
  assert.equal(panel.downloads.length, 0); assert.equal(panel.text().includes("private upstream"), false);
  assert.match(panel.text(), /Esportazione non completata/); assert.equal(panel.button("Esporta CSV tutti i filtrati").props.disabled, false); panel.unmount();
});
