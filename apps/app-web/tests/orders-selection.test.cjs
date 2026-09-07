const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const ts = require("typescript");
const sellerId = "a1000000-0000-4000-8000-000000000001";
const accountId = "a2000000-0000-4000-8000-000000000001";
const selectionId = "a3000000-0000-4000-8000-000000000001";
const ids = Array.from({ length: 51 }, (_, index) => `a4000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`);
const query = { account_id: accountId, environment: "live", page: 1, page_size: 50, search: "", statuses: [], storefronts: [], currencies: [], carriers: [], date_from: "", date_to: "", status_selection: "all", storefront_selection: "all", currency_selection: "all", tracking: "all", commission: "all", amount_min: "", amount_max: "" };
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
const item = (id) => ({ id, external_line_id: id, order_id: `ORD-${id.slice(-3)}`, marketplace: "kaufland", created_at: "2026-09-07T10:00:00Z", status: "sent", status_label: "Spedito", storefront: "de", currency: "EUR", product_name: "Caffè demo", ean: "1234567890123", sku: "in_codice_10_20", quantity: 1, sale_amount: "20", shipping_amount: "0", commission_amount: "2", commission_rate: "10", payout_amount: "18", purchase_cost: "10", purchase_cost_source: "SKU", profit_amount: "8", profit_pct: "80", sale_amount_eur: "20", shipping_amount_eur: "0", commission_amount_eur: "2", payout_amount_eur: "18", purchase_cost_eur: "10", profit_amount_eur: "8", monetary_warnings: [], details: {} });
const summary = (count) => ({ selected_rows: count, distinct_orders: count, quantity: count, cancelled_rows: 0, sale_amount_eur: count ? "987.65" : "0", commission_amount_eur: "0", payout_amount_eur: "0", purchase_cost_eur: "0", profit_amount_eur: "0", profit_pct: null, complete_economic_rows: count, missing_economic_rows: 0, known_cost_rows: count, missing_cost_rows: 0, loss_rows: 0, sku_cost_rows: count, catalog_cost_rows: 0, missing_currencies: [] });
const selection = (selected = new Set(ids), page = 1) => ({ id: selectionId, selected_ids: (page === 1 ? ids.slice(0, 2) : ids.slice(2, 3)).filter((id) => selected.has(id)), selected_count: selected.size, filtered_count: ids.length, summary: summary(selected.size) });
const list = (selected = new Set(ids), page = 1) => ({ seller_id: sellerId, account_id: accountId, environment: "live", marketplace: "kaufland", can_sync: false, total: ids.length, page, page_size: 50, latest_job: null, filters: { statuses: ["sent", "cancelled"], storefronts: ["de", "pl"], currencies: ["EUR", "PLN", ""], carriers: ["DHL"], date_min: "2026-09-07", date_max: "2026-09-07", amount_min: "0.00", amount_max: "100.00" }, selection: selection(selected, page), items: (page === 1 ? ids.slice(0, 2) : ids.slice(2, 3)).map(item) });

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

test("advanced filters preserve inclusive EUR bounds and reject negative, infinite or reversed values", () => {
  const filter = { ...query, amount_min: "0", amount_max: "12.50", tracking: "missing", commission: "present", carriers: ["DHL"] };
  assert.deepEqual(plain(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString(filter)))), filter);
  for (const change of [{ amount_min: "-1" }, { amount_max: "Infinity" }, { amount_min: "15", amount_max: "10" }, { tracking: "other" }, { commission: "false" }]) assert.equal(types.readOrdersQuery(new URLSearchParams(types.ordersQueryString({ ...query, ...change }))), null);
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
  const empty = { ...list(), total: 0, items: [], selection: { ...selection(new Set()), filtered_count: 0 }, filters: { ...list().filters, date_min: null, date_max: null, amount_min: null, amount_max: null } };
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
  assert.equal(types.readOrdersList({ ...list(), selection: { ...valid, selected_ids: [ids[10]] } }, sellerId, query), null);
});

test("selection/export bodies share strict filters and cannot carry caller-selected credentials or Seller IDs", () => {
  const body = { account_id: accountId, environment: "live", selection_id: selectionId, filters: types.ordersFilterInput(query), action: "set", line_id: ids[0], selected: false, secret_key: "private", seller_id: "other" };
  const parsed = types.readOrdersActionInput(body, "selection");
  assert.equal(JSON.stringify(parsed).includes("private"), false); assert.equal(parsed.selected, false);
  assert.equal(types.readOrdersActionInput({ ...body, line_id: "bad" }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, selected: 1 }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, filters: { ...body.filters, amount_min: "-0.01" } }, "selection"), null);
  assert.equal(types.readOrdersActionInput({ ...body, kind: "selected" }, "export").kind, "selected");
  assert.equal(types.readOrdersActionInput({ ...body, kind: "all" }, "export"), null);
});

function component(fetchImpl) {
  const states = [], refs = [], effects = [], callbacks = [], timers = new Map(), requests = [], navigations = [], downloads = [], revoked = [];
  let stateIndex, refIndex, effectIndex, callbackIndex, pendingEffects, tree, timerId = 0;
  const nodes = (node) => !node || typeof node !== "object" ? [] : Array.isArray(node) ? node.flatMap(nodes) : [node, ...nodes(node.props?.children)];
  const text = (node) => typeof node === "string" ? node : typeof node === "number" ? String(node) : Array.isArray(node) ? node.map(text).join("") : node?.props ? text(node.props.children) : "";
  const element = (type, props) => typeof type === "function" && ["FilterChoices", "OrdersTotals"].includes(type.name) ? type(props) : ({ type, props });
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
      if (name.endsWith(".module.css")) return { default: new Proxy({}, { get: (_target, key) => key }) };
      throw new Error(`Unexpected dependency ${name}`);
    },
  });
  function render() { stateIndex = refIndex = effectIndex = callbackIndex = 0; pendingEffects = []; tree = module.OrderAccountPanel({ sellerId, account, environment: "live" }); pendingEffects.forEach((effect) => effect()); }
  render();
  return {
    render, requests, navigations, downloads, revoked, nodes: () => nodes(tree), text: () => text(tree),
    button: (label) => nodes(tree).find((node) => node.type === "button" && text(node) === label),
    rowCheckbox: (id) => nodes(tree).find((node) => node.type === "input" && node.props?.["aria-label"]?.endsWith(`riga ${id}`)),
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
