import { isUuid } from "./seller-settings-types";

export type OrderEnvironment = "live" | "playground";
export type OrderMarketplace = "kaufland" | "worten";
export type Decimal = string | null;
export type OrderItem = {
  id: string; external_line_id: string; order_id: string; marketplace: OrderMarketplace; created_at: string | null;
  status: string; status_label: string; storefront: string; currency: string; product_name: string; ean: string; sku: string; quantity: number;
  sale_amount: Decimal; shipping_amount: Decimal; commission_amount: Decimal; commission_rate: Decimal; payout_amount: Decimal;
  purchase_cost: Decimal; purchase_cost_source: string; profit_amount: Decimal; profit_pct: Decimal;
  sale_amount_eur: Decimal; shipping_amount_eur: Decimal; commission_amount_eur: Decimal; payout_amount_eur: Decimal;
  purchase_cost_eur: Decimal; profit_amount_eur: Decimal; monetary_warnings: string[]; details: OrderDetails;
};
export type OrderDetails = Partial<Record<
  "product_amount" | "product_amount_eur" | "revenue_gross" | "revenue_net" | "sale_original_amount" | "sale_original_amount_eur"
  | "refund_amount" | "refund_amount_eur" | "purchase_unit_cost_eur" | "minimum_price_sku_eur", Decimal>>
  & Partial<Record<"commission_source" | "commission_rate_source" | "payout_source" | "financial_source" | "refund_source" | "zero_economic_reason"
  | "carrier" | "tracking" | "received_source" | "shipped_source" | "released_source" | "sku_supplier" | "sku_product_code" | "sku_ean_note" | "economic_currency" | "detail_warning", string>>
  & { received_at?: string | null; shipped_at?: string | null; released_at?: string | null; updated_at?: string | null; detail_checked_at?: string | null;
    excluded_from_totals?: boolean; sku_ean_matches_order?: boolean | null; fx?: { rate: Decimal; date: string; source: string; online: boolean | null } };
export type OrderJob = {
  id: string; status: "queued" | "running" | "done" | "error"; processed: number; total: number | null; progress: number | null;
  message: string; error_code: string | null; account_id: string; marketplace: OrderMarketplace; environment: OrderEnvironment;
  maximum: 500 | 1000 | 5000 | null; include_details: boolean; created_at: string; started_at: string | null; finished_at: string | null;
};
export type OrdersList = { seller_id: string; account_id: string; marketplace: OrderMarketplace; environment: OrderEnvironment; can_sync: boolean;
  items: OrderItem[]; total: number; page: number; page_size: number; latest_job: OrderJob | null;
  filters: { statuses: string[]; storefronts: string[]; currencies: string[]; carriers: string[]; date_min: string | null; date_max: string | null; amount_min: Decimal; amount_max: Decimal }; selection: OrderSelection };
export type SelectionMode = "all" | "selected";
export type PresenceFilter = "all" | "present" | "missing";
export type OrdersQuery = { account_id: string; environment: OrderEnvironment; page: number; page_size: number; search: string;
  statuses: string[]; storefronts: string[]; currencies: string[]; carriers: string[]; date_from: string; date_to: string;
  status_selection: SelectionMode; storefront_selection: SelectionMode; currency_selection: SelectionMode;
  tracking: PresenceFilter; commission: PresenceFilter; amount_min: string; amount_max: string };
export type OrdersSyncInput = Pick<OrdersQuery, "account_id" | "environment"> & Pick<OrderJob, "maximum" | "include_details">;
export type OrdersFilterInput = { search: string; statuses: string[] | null; storefronts: string[] | null; currencies: string[] | null;
  carriers: string[]; tracking: PresenceFilter; commission: PresenceFilter; date_from: string | null; date_to: string | null; amount_min: Decimal; amount_max: Decimal };
export type OrdersSummary = { selected_rows: number; distinct_orders: number; quantity: number; cancelled_rows: number;
  sale_amount_eur: string; commission_amount_eur: string; payout_amount_eur: string; purchase_cost_eur: string; profit_amount_eur: string; profit_pct: Decimal;
  complete_economic_rows: number; missing_economic_rows: number; known_cost_rows: number; missing_cost_rows: number; loss_rows: number;
  sku_cost_rows: number; catalog_cost_rows: number; missing_currencies: string[] };
export type OrderSelection = { id: string; selected_ids: string[]; selected_count: number; filtered_count: number; summary: OrdersSummary };
export type SelectionAction = { action: "select_all" } | { action: "clear" } | { action: "set"; line_id: string; selected: boolean };
export type OrdersActionInput = { account_id: string; environment: OrderEnvironment; selection_id: string; filters: OrdersFilterInput }
  & (SelectionAction | { kind: "selected" | "filtered" });

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text);
const decimal = (value: unknown): value is Decimal => value === null || (typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)));
const timestamp = (value: unknown): value is string | null => value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const environment = (value: unknown): value is OrderEnvironment => value === "live" || value === "playground";
const marketplace = (value: unknown): value is OrderMarketplace => value === "kaufland" || value === "worten";
const maximum = (value: unknown): value is OrderJob["maximum"] => value === null || value === 500 || value === 1000 || value === 5000;
const presence = (value: unknown): value is PresenceFilter => value === "all" || value === "present" || value === "missing";
const mode = (value: unknown): value is SelectionMode => value === "all" || value === "selected";
const choices = (value: unknown): value is string[] => strings(value) && value.length <= 100 && value.every((entry) => entry.length <= 100);

export const orderJobErrors: Record<string, string> = {
  permission_revoked: "Autorizzazione al negozio revocata. Sincronizzazione interrotta.",
  account_unavailable: "Account marketplace non disponibile o da verificare.",
  credentials_unavailable: "Credenziali marketplace non disponibili per la sincronizzazione.",
  invalid_credentials: "Il marketplace non ha accettato le credenziali API.",
  permission_denied: "Le credenziali API non autorizzano la lettura degli ordini.",
  unexpected_response: "Il marketplace ha restituito dati ordine non riconoscibili.",
  invalid_response: "Il marketplace ha restituito dati ordine non riconoscibili.",
  invalid_configuration: "Configurazione marketplace non valida per la sincronizzazione.",
  unsupported_marketplace: "Ordini non disponibili per questo marketplace.",
  rate_limited: "Limite di richieste marketplace raggiunto. Riprova più tardi.",
  upstream_unavailable: "Marketplace temporaneamente non disponibile. Riprova.",
  timeout: "Tempo di sincronizzazione esaurito. Riprova.",
  queue_unavailable: "La sincronizzazione non può essere avviata in questo momento. Riprova.",
  worker_interrupted: "Sincronizzazione interrotta. Puoi avviarla di nuovo.",
  sync_failed: "Sincronizzazione non completata. I dati già salvati restano disponibili.",
};
const progressMessages = new Set(["Sincronizzazione in coda.", "Download ordini in corso.", "Recupero dettagli ordini.", "Preparazione delle righe ordine.", "Salvataggio ordini.", "Il marketplace richiede un'attesa. Nuovo tentativo in corso.", "Sincronizzazione completata."]);
const safeProgress = /^(?:Download ordini (?:cancelled|need_to_be_sent|open|received|returned|returned_paid|sent|sent_and_autopaid): [0-9]{1,12}|Download ordini Worten: [0-9]{1,12} ordini, [0-9]{1,12} righe|Sincronizzazione completata\.(?: [0-9]{1,12} righe con avvisi: consulta i dettagli degli ordini\.)?(?: Dettagli verificati: [0-9]{1,12}\.)?)$/;

export function readOrderJob(value: unknown, scope?: { account_id: string; environment: OrderEnvironment }): OrderJob | null {
  if (!object(value) || !isUuid(value.id) || !["queued", "running", "done", "error"].includes(String(value.status))
    || !integer(value.processed) || !(value.total === null || integer(value.total))
    || !(value.progress === null || (integer(value.progress) && value.progress <= 100))
    || !isUuid(value.account_id) || !marketplace(value.marketplace) || !environment(value.environment)
    || (value.marketplace === "worten" && value.environment !== "live")
    || !maximum(value.maximum) || typeof value.include_details !== "boolean" || !text(value.created_at) || !timestamp(value.created_at)
    || !timestamp(value.started_at) || !timestamp(value.finished_at)
    || !(value.error_code === null || (text(value.error_code) && Object.hasOwn(orderJobErrors, value.error_code)))
    || (scope && (scope.account_id !== value.account_id || scope.environment !== value.environment))) return null;
  const code = value.error_code as string | null;
  const fallback = value.status === "done" ? "Sincronizzazione completata." : value.status === "queued" ? "Sincronizzazione in coda." : "Download ordini in corso.";
  return { id: value.id, status: value.status as OrderJob["status"], processed: value.processed, total: value.total as number | null,
    progress: value.progress as number | null, message: code ? orderJobErrors[code] : text(value.message) && (progressMessages.has(value.message) || safeProgress.test(value.message)) ? value.message : fallback,
    error_code: code, account_id: value.account_id, marketplace: value.marketplace, environment: value.environment,
    maximum: value.maximum, include_details: value.include_details, created_at: value.created_at, started_at: value.started_at, finished_at: value.finished_at };
}

export function readOrderItem(value: unknown): OrderItem | null {
  if (!object(value) || !isUuid(value.id) || !marketplace(value.marketplace) || !timestamp(value.created_at) || !integer(value.quantity) || value.quantity < 1
    || !strings(value.monetary_warnings) || !object(value.details)) return null;
  const result: Record<string, unknown> = { id: value.id, marketplace: value.marketplace, created_at: value.created_at, quantity: value.quantity, monetary_warnings: [...value.monetary_warnings] };
  for (const key of ["external_line_id", "order_id", "status", "status_label", "storefront", "currency", "product_name", "ean", "sku", "purchase_cost_source"]) {
    if (!text(value[key])) return null;
    result[key] = value[key];
  }
  for (const key of ["sale_amount", "shipping_amount", "commission_amount", "commission_rate", "payout_amount", "purchase_cost", "profit_amount", "profit_pct", "sale_amount_eur", "shipping_amount_eur", "commission_amount_eur", "payout_amount_eur", "purchase_cost_eur", "profit_amount_eur"]) {
    if (!decimal(value[key])) return null;
    result[key] = value[key];
  }
  const details: Record<string, unknown> = {};
  for (const key of ["product_amount", "product_amount_eur", "revenue_gross", "revenue_net", "sale_original_amount", "sale_original_amount_eur", "refund_amount", "refund_amount_eur", "purchase_unit_cost_eur", "minimum_price_sku_eur"]) {
    if (key in value.details) { if (!decimal(value.details[key])) return null; details[key] = value.details[key]; }
  }
  for (const key of ["commission_source", "commission_rate_source", "payout_source", "financial_source", "refund_source", "zero_economic_reason", "carrier", "tracking", "received_source", "shipped_source", "released_source", "sku_supplier", "sku_product_code", "sku_ean_note", "economic_currency", "detail_warning"]) {
    if (key in value.details) { if (!text(value.details[key])) return null; details[key] = value.details[key]; }
  }
  for (const key of ["received_at", "shipped_at", "released_at", "updated_at", "detail_checked_at"]) {
    if (key in value.details) { if (!timestamp(value.details[key])) return null; details[key] = value.details[key]; }
  }
  for (const key of ["excluded_from_totals", "sku_ean_matches_order"]) {
    if (key in value.details) { if (!(typeof value.details[key] === "boolean" || (key === "sku_ean_matches_order" && value.details[key] === null))) return null; details[key] = value.details[key]; }
  }
  if ("fx" in value.details) {
    const fx = value.details.fx;
    if (!object(fx) || !decimal(fx.rate) || !text(fx.date) || !text(fx.source) || !(fx.online === null || typeof fx.online === "boolean")) return null;
    details.fx = { rate: fx.rate, date: fx.date, source: fx.source, online: fx.online };
  }
  return { ...result, details } as OrderItem;
}

export function readOrdersList(value: unknown, sellerId: string, query: Pick<OrdersQuery, "account_id" | "environment" | "page" | "page_size">): OrdersList | null {
  if (!object(value) || value.seller_id !== sellerId || value.account_id !== query.account_id || value.environment !== query.environment
    || !marketplace(value.marketplace) || typeof value.can_sync !== "boolean" || !integer(value.total)
    || value.page !== query.page || value.page_size !== query.page_size || !Array.isArray(value.items)
    || !object(value.filters) || !strings(value.filters.statuses) || !strings(value.filters.storefronts)
    || !strings(value.filters.currencies) || !strings(value.filters.carriers)
    || ![value.filters.date_min, value.filters.date_max].every((date) => date === null || text(date) && Boolean(date) && validDate(date))
    || !decimal(value.filters.amount_min) || !decimal(value.filters.amount_max) || Number(value.filters.amount_min) < 0 || Number(value.filters.amount_max) < 0) return null;
  const items: OrderItem[] = [];
  for (const raw of value.items) { const item = readOrderItem(raw); if (!item || item.marketplace !== value.marketplace || items.some((row) => row.id === item.id)) return null; items.push(item); }
  if (items.length > query.page_size) return null;
  const job = value.latest_job === null ? null : readOrderJob(value.latest_job, query);
  if (value.latest_job !== null && (!job || job.marketplace !== value.marketplace)) return null;
  const selection = readOrderSelection(value.selection);
  if (!selection || selection.filtered_count !== value.total || selection.selected_ids.some((id) => !items.some((item) => item.id === id))) return null;
  return { seller_id: sellerId, account_id: query.account_id, marketplace: value.marketplace, environment: query.environment,
    can_sync: value.can_sync, items, total: value.total, page: query.page, page_size: query.page_size, latest_job: job,
    filters: { statuses: [...value.filters.statuses], storefronts: [...value.filters.storefronts], currencies: [...value.filters.currencies], carriers: [...value.filters.carriers],
      date_min: value.filters.date_min as string | null, date_max: value.filters.date_max as string | null, amount_min: value.filters.amount_min, amount_max: value.filters.amount_max }, selection };
}

export function ordersDefaultBounds(query: OrdersQuery, filters: OrdersList["filters"]): OrdersQuery {
  return { ...query, date_from: filters.date_min ?? "", date_to: filters.date_max ?? "", amount_min: filters.amount_min ?? "0", amount_max: filters.amount_max ?? "0" };
}

export function readOrderSelection(value: unknown): OrderSelection | null {
  if (!object(value) || !isUuid(value.id) || !strings(value.selected_ids) || value.selected_ids.length > 100 || !value.selected_ids.every(isUuid)
    || new Set(value.selected_ids).size !== value.selected_ids.length || !integer(value.selected_count) || !integer(value.filtered_count)
    || value.selected_count > value.filtered_count || value.selected_ids.length > value.selected_count || !object(value.summary)) return null;
  const summary: Record<string, unknown> = {};
  for (const key of ["selected_rows", "distinct_orders", "quantity", "cancelled_rows", "complete_economic_rows", "missing_economic_rows", "known_cost_rows", "missing_cost_rows", "loss_rows", "sku_cost_rows", "catalog_cost_rows"]) {
    if (!integer(value.summary[key]) || (key !== "quantity" && value.summary[key] > value.selected_count)) return null;
    summary[key] = value.summary[key];
  }
  if (summary.selected_rows !== value.selected_count || (summary.distinct_orders as number) > value.selected_count
    || (summary.cancelled_rows as number) > value.selected_count) return null;
  for (const key of ["sale_amount_eur", "commission_amount_eur", "payout_amount_eur", "purchase_cost_eur", "profit_amount_eur"]) {
    if (!text(value.summary[key]) || !decimal(value.summary[key])) return null;
    summary[key] = value.summary[key];
  }
  if (!decimal(value.summary.profit_pct) || !choices(value.summary.missing_currencies)) return null;
  summary.profit_pct = value.summary.profit_pct;
  summary.missing_currencies = [...value.summary.missing_currencies];
  return { id: value.id, selected_ids: [...value.selected_ids], selected_count: value.selected_count, filtered_count: value.filtered_count, summary: summary as OrdersSummary };
}

function validDate(value: string): boolean { return !value || /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value; }
export function readOrdersQuery(params: URLSearchParams): OrdersQuery | null {
  const account_id = params.get("account_id"), env = params.get("environment") ?? "live";
  const page = Number(params.get("page") ?? 1), page_size = Number(params.get("page_size") ?? 50), search = params.get("search") ?? "";
  const statuses = params.getAll("status"), storefronts = params.getAll("storefront"), currencies = params.getAll("currency"), carriers = params.getAll("carrier");
  const date_from = params.get("date_from") ?? "", date_to = params.get("date_to") ?? "";
  const status_selection = params.get("status_selection") ?? (statuses.length ? "selected" : "all"), storefront_selection = params.get("storefront_selection") ?? (storefronts.length ? "selected" : "all"), currency_selection = params.get("currency_selection") ?? (currencies.length ? "selected" : "all");
  const tracking = params.get("tracking") ?? "all", commission = params.get("commission") ?? "all", amount_min = params.get("amount_min") ?? "", amount_max = params.get("amount_max") ?? "";
  if (!isUuid(account_id) || !environment(env) || !integer(page) || page < 1 || !integer(page_size) || page_size < 1 || page_size > 100 || search.length > 200
    || ![statuses, storefronts, currencies, carriers].every(choices) || ![status_selection, storefront_selection, currency_selection].every(mode)
    || !presence(tracking) || !presence(commission) || (amount_min && (!decimal(amount_min) || Number(amount_min) < 0)) || (amount_max && (!decimal(amount_max) || Number(amount_max) < 0))
    || (amount_min && amount_max && Number(amount_min) > Number(amount_max))
    || !validDate(date_from) || !validDate(date_to) || (date_from && date_to && date_from > date_to)) return null;
  return { account_id, environment: env, page, page_size, search: search.trim(), statuses, storefronts, currencies, carriers, date_from, date_to,
    status_selection: status_selection as SelectionMode, storefront_selection: storefront_selection as SelectionMode, currency_selection: currency_selection as SelectionMode,
    tracking, commission, amount_min, amount_max };
}
export function ordersQueryString(query: OrdersQuery): string {
  const params = new URLSearchParams({ account_id: query.account_id, environment: query.environment, page: String(query.page), page_size: String(query.page_size) });
  if (query.search) params.set("search", query.search);
  if (query.date_from) params.set("date_from", query.date_from);
  if (query.date_to) params.set("date_to", query.date_to);
  params.set("status_selection", query.status_selection); params.set("storefront_selection", query.storefront_selection); params.set("currency_selection", query.currency_selection);
  params.set("tracking", query.tracking); params.set("commission", query.commission);
  if (query.amount_min) params.set("amount_min", query.amount_min); if (query.amount_max) params.set("amount_max", query.amount_max);
  query.statuses.forEach((value) => params.append("status", value)); query.storefronts.forEach((value) => params.append("storefront", value));
  query.currencies.forEach((value) => params.append("currency", value)); query.carriers.forEach((value) => params.append("carrier", value));
  return params.toString();
}
export function ordersFilterInput(query: OrdersQuery): OrdersFilterInput {
  return { search: query.search.trim(), statuses: query.status_selection === "all" ? null : [...query.statuses],
    storefronts: query.storefront_selection === "all" ? null : [...query.storefronts], currencies: query.currency_selection === "all" ? null : [...query.currencies],
    carriers: [...query.carriers], tracking: query.tracking, commission: query.commission, date_from: query.date_from || null, date_to: query.date_to || null,
    amount_min: query.amount_min || null, amount_max: query.amount_max || null };
}
export function readOrdersActionInput(value: unknown, operation: "selection" | "export"): OrdersActionInput | null {
  if (!object(value) || !isUuid(value.account_id) || !environment(value.environment) || !isUuid(value.selection_id) || !object(value.filters)) return null;
  const filters = value.filters;
  if (!text(filters.search) || filters.search.length > 200 || ![filters.statuses, filters.storefronts, filters.currencies].every((entry) => entry === null || choices(entry))
    || !choices(filters.carriers) || !presence(filters.tracking) || !presence(filters.commission)
    || ![filters.date_from, filters.date_to].every((entry) => entry === null || text(entry) && Boolean(entry) && validDate(entry))
    || !decimal(filters.amount_min) || !decimal(filters.amount_max) || Number(filters.amount_min) < 0 || Number(filters.amount_max) < 0
    || (text(filters.date_from) && text(filters.date_to) && filters.date_from > filters.date_to)
    || (text(filters.amount_min) && text(filters.amount_max) && Number(filters.amount_min) > Number(filters.amount_max))) return null;
  const clean: OrdersFilterInput = { search: filters.search.trim(), statuses: filters.statuses === null ? null : [...filters.statuses as string[]],
    storefronts: filters.storefronts === null ? null : [...filters.storefronts as string[]], currencies: filters.currencies === null ? null : [...filters.currencies as string[]],
    carriers: [...filters.carriers], tracking: filters.tracking, commission: filters.commission, date_from: filters.date_from as string | null,
    date_to: filters.date_to as string | null, amount_min: filters.amount_min, amount_max: filters.amount_max };
  const scope = { account_id: value.account_id, environment: value.environment, selection_id: value.selection_id, filters: clean };
  if (operation === "export") return value.kind === "selected" || value.kind === "filtered" ? { ...scope, kind: value.kind } : null;
  if (value.action === "select_all" || value.action === "clear") return { ...scope, action: value.action };
  return value.action === "set" && isUuid(value.line_id) && typeof value.selected === "boolean" ? { ...scope, action: "set", line_id: value.line_id, selected: value.selected } : null;
}
export function readOrdersSyncInput(value: unknown): OrdersSyncInput | null {
  if (!object(value) || !isUuid(value.account_id) || !environment(value.environment) || !maximum(value.maximum) || typeof value.include_details !== "boolean") return null;
  return { account_id: value.account_id, environment: value.environment, maximum: value.maximum, include_details: value.include_details };
}
export function formatMoney(value: Decimal | undefined, currency = "EUR"): string {
  if (value === null || value === undefined || !decimal(value)) return "—";
  try { return new Intl.NumberFormat("it-IT", { style: "currency", currency }).format(Number(value)); }
  catch { return `${new Intl.NumberFormat("it-IT", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value))} ${currency || "(valuta non indicata)"}`; }
}
export function formatPercent(value: Decimal | undefined): string { return value === null || value === undefined || !decimal(value) ? "—" : `${new Intl.NumberFormat("it-IT", { maximumFractionDigits: 2 }).format(Number(value))}%`; }
export function formatOrderDate(value: string | null | undefined): string { return !value || !Number.isFinite(Date.parse(value)) ? "—" : new Date(value).toLocaleString("it-IT", { timeZone: "Europe/Rome", dateStyle: "short", timeStyle: "short" }); }
