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
  items: OrderItem[]; total: number; page: number; page_size: number; latest_job: OrderJob | null; filters: { statuses: string[]; storefronts: string[] } };
export type OrdersQuery = { account_id: string; environment: OrderEnvironment; page: number; page_size: number; search: string;
  statuses: string[]; storefronts: string[]; date_from: string; date_to: string };
export type OrdersSyncInput = Pick<OrdersQuery, "account_id" | "environment"> & Pick<OrderJob, "maximum" | "include_details">;

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown): value is string => typeof value === "string";
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(text);
const decimal = (value: unknown): value is Decimal => value === null || (typeof value === "string" && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)));
const timestamp = (value: unknown): value is string | null => value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
const integer = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const environment = (value: unknown): value is OrderEnvironment => value === "live" || value === "playground";
const marketplace = (value: unknown): value is OrderMarketplace => value === "kaufland" || value === "worten";
const maximum = (value: unknown): value is OrderJob["maximum"] => value === null || value === 500 || value === 1000 || value === 5000;

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
    || !object(value.filters) || !strings(value.filters.statuses) || !strings(value.filters.storefronts)) return null;
  const items: OrderItem[] = [];
  for (const raw of value.items) { const item = readOrderItem(raw); if (!item || item.marketplace !== value.marketplace || items.some((row) => row.id === item.id)) return null; items.push(item); }
  if (items.length > query.page_size) return null;
  const job = value.latest_job === null ? null : readOrderJob(value.latest_job, query);
  if (value.latest_job !== null && (!job || job.marketplace !== value.marketplace)) return null;
  return { seller_id: sellerId, account_id: query.account_id, marketplace: value.marketplace, environment: query.environment,
    can_sync: value.can_sync, items, total: value.total, page: query.page, page_size: query.page_size, latest_job: job,
    filters: { statuses: [...value.filters.statuses], storefronts: [...value.filters.storefronts] } };
}

function validDate(value: string): boolean { return !value || /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value; }
export function readOrdersQuery(params: URLSearchParams): OrdersQuery | null {
  const account_id = params.get("account_id"), env = params.get("environment") ?? "live";
  const page = Number(params.get("page") ?? 1), page_size = Number(params.get("page_size") ?? 50), search = params.get("search") ?? "";
  const statuses = params.getAll("status"), storefronts = params.getAll("storefront"), date_from = params.get("date_from") ?? "", date_to = params.get("date_to") ?? "";
  if (!isUuid(account_id) || !environment(env) || !integer(page) || page < 1 || !integer(page_size) || page_size < 1 || page_size > 100 || search.length > 200
    || statuses.length > 50 || storefronts.length > 50 || [...statuses, ...storefronts].some((value) => !value || value.length > 100)
    || !validDate(date_from) || !validDate(date_to) || (date_from && date_to && date_from > date_to)) return null;
  return { account_id, environment: env, page, page_size, search: search.trim(), statuses, storefronts, date_from, date_to };
}
export function ordersQueryString(query: OrdersQuery): string {
  const params = new URLSearchParams({ account_id: query.account_id, environment: query.environment, page: String(query.page), page_size: String(query.page_size) });
  if (query.search) params.set("search", query.search);
  if (query.date_from) params.set("date_from", query.date_from);
  if (query.date_to) params.set("date_to", query.date_to);
  query.statuses.forEach((value) => params.append("status", value)); query.storefronts.forEach((value) => params.append("storefront", value));
  return params.toString();
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
