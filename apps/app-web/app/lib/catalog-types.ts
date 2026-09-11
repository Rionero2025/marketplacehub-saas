import { isUuid } from "./seller-settings-types";

export const MAX_PRICE_LIST_FILE_BYTES = 20 * 1024 * 1024;
export const PRICE_LIST_EXTENSIONS = ["csv", "txt", "tsv", "xls", "xlsx", "xml"] as const;

export type CatalogProvider = "generic" | "innpro";
export type CatalogFeedRole = "standard" | "full" | "light";
export type CatalogFeedProfile = { provider: CatalogProvider; feed_role: CatalogFeedRole };

export type CatalogSupplier = {
  id: string;
  name: string;
  notes: string;
  price_list_count: number;
};

export type CatalogPriceList = {
  id: string;
  supplier_id: string;
  supplier_name: string;
  name: string;
  provider: CatalogProvider;
  feed_role: CatalogFeedRole;
  source_type: "upload" | "file" | "url";
  file_name: string | null;
  file_format: string | null;
  row_count: number | null;
  status: "pending" | "queued" | "running" | "error" | "ready";
  source_host: string | null;
  source_config_revision: number;
  active_version_number: number | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  latest_job: CatalogFeedJob | null;
  updated_at: string;
};

export type CatalogFeedJob = {
  id: string;
  status: "queued" | "pending" | "running" | "done" | "ready" | "error";
  processed_bytes: number;
  total_bytes: number | null;
  progress: number | null;
  message: string | null;
  error_code: string | null;
  result_version: number | null;
  created_at: string;
  updated_at: string;
};

export type CatalogFeedMutation = {
  price_list: CatalogPriceList;
  job: CatalogFeedJob;
};

export type CatalogDashboard = {
  seller_id: string;
  can_manage: boolean;
  suppliers: CatalogSupplier[];
  price_lists: CatalogPriceList[];
};

export type CatalogProduct = {
  ean: string;
  sku: string;
  name: string;
  cost: CatalogDecimal;
  shipping_cost: CatalogDecimal;
  total_cost: CatalogDecimal;
  quantity: CatalogDecimal;
};

export type CatalogDecimal = string | null;

export type PriceListDetail = {
  price_list: CatalogPriceList;
  products: CatalogProduct[];
  total: number;
};

export type SupplierInput = { name: string; notes: string };
export type DeleteSupplierInput = { confirmation: string };
export type UrlPriceListInput = {
  supplier_id: string;
  name: string;
  provider: CatalogProvider;
  feed_role: CatalogFeedRole;
  url: string;
  username: string;
  password: string;
};

export type CatalogCredentialMode = "keep" | "replace" | "remove";
export type UpdateUrlPriceListInput = {
  url: string;
  credentials_mode: CatalogCredentialMode;
  expected_config_revision: number;
  username?: string;
  password?: string;
};

type ObjectValue = Record<string, unknown>;
const isObject = (value: unknown): value is ObjectValue => typeof value === "object" && value !== null && !Array.isArray(value);
const requiredText = (value: unknown, maximum = 500): value is string => typeof value === "string" && Boolean(value.trim()) && value.length <= maximum;
const optionalText = (value: unknown, maximum = 5_000): value is string => typeof value === "string" && value.length <= maximum;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const nullableCount = (value: unknown): value is number | null => value === null || count(value);
const timestampOrNull = (value: unknown): value is string | null => value === null
  || (typeof value === "string" && value.length <= 100 && Number.isFinite(Date.parse(value)));
const decimalOrNull = (value: unknown): value is CatalogDecimal => value === null
  || (typeof value === "string" && value.length <= 50 && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)));

const sourceTypes = new Set(["upload", "file", "url"]);
const catalogProviders = new Set<CatalogProvider>(["generic", "innpro"]);
const catalogFeedRoles = new Set<CatalogFeedRole>(["standard", "full", "light"]);
const priceListStatuses = new Set(["pending", "queued", "running", "error", "ready"]);
const jobStatuses = new Set(["queued", "pending", "running", "done", "ready", "error"]);
const jobErrorCodes = new Set([
  "queue_unavailable", "permission_revoked", "source_unavailable", "source_changed", "download_failed",
  "invalid_feed", "file_too_large", "timeout", "refresh_failed", "worker_interrupted",
]);
const jobMessages = new Set([
  "Importazione listino in coda.", "Aggiornamento listino in coda.",
  "Download del listino in corso.", "Download listino in corso.", "Listino aggiornato.",
  "Elaborazione prodotti in corso.", "Salvataggio prodotti in corso.",
  "Listino invariato: versione già acquisita.",
  "Il servizio di importazione non è disponibile. Riprova tra poco.",
  "L’accesso al catalogo non è più autorizzato.", "La sorgente del listino non è disponibile.",
  "La configurazione del listino è cambiata. Avvia un nuovo aggiornamento.",
  "Non è stato possibile scaricare il listino.",
  "Il contenuto scaricato non è un listino supportato.",
  "Il listino supera il limite consentito: 20 MiB per i feed generici, 200 MiB per i feed URL InnPro.",
  "Il download del listino ha superato il tempo massimo.",
  "L’aggiornamento del listino non è riuscito.",
  "Aggiornamento listino interrotto dal worker. Puoi avviarlo di nuovo.",
]);

function nullableText(value: unknown, maximum: number): value is string | null {
  return value === null || (typeof value === "string" && value.length <= maximum);
}

function publicHostOrNull(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length <= 253
    && /^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(value) && value.includes(".")
    && !value.includes(".."));
}

function readFeedJob(value: unknown): CatalogFeedJob | null {
  if (!isObject(value) || !isUuid(value.id) || typeof value.status !== "string" || !jobStatuses.has(value.status)
    || !count(value.processed_bytes) || !nullableCount(value.total_bytes)
    || !(value.progress === null || (typeof value.progress === "number" && Number.isFinite(value.progress) && value.progress >= 0 && value.progress <= 100))
    || !nullableText(value.message, 300) || !nullableText(value.error_code, 100)
    || !nullableCount(value.result_version) || !timestampOrNull(value.created_at) || value.created_at === null
    || !timestampOrNull(value.updated_at) || value.updated_at === null) return null;
  const message = value.message !== null && jobMessages.has(value.message) ? value.message : null;
  const errorCode = value.error_code !== null && jobErrorCodes.has(value.error_code) ? value.error_code : null;
  return {
    id: value.id, status: value.status as CatalogFeedJob["status"], processed_bytes: value.processed_bytes,
    total_bytes: value.total_bytes, progress: value.progress, message, error_code: errorCode,
    result_version: value.result_version, created_at: value.created_at, updated_at: value.updated_at,
  };
}

function readSupplier(value: unknown): CatalogSupplier | null {
  if (!isObject(value) || !isUuid(value.id) || !requiredText(value.name, 200)
    || !optionalText(value.notes) || !count(value.price_list_count)) return null;
  return { id: value.id, name: value.name, notes: value.notes, price_list_count: value.price_list_count };
}

/** Accepts only provider/role pairs supported by the catalog contract. */
export function readCatalogFeedProfile(provider: unknown, feedRole: unknown): CatalogFeedProfile | null {
  if (typeof provider !== "string" || !catalogProviders.has(provider as CatalogProvider)
    || typeof feedRole !== "string" || !catalogFeedRoles.has(feedRole as CatalogFeedRole)) return null;
  if ((provider === "generic") !== (feedRole === "standard")) return null;
  return { provider: provider as CatalogProvider, feed_role: feedRole as CatalogFeedRole };
}

function readPriceList(value: unknown): CatalogPriceList | null {
  if (!isObject(value)) return null;
  const profile = readCatalogFeedProfile(value.provider, value.feed_role);
  const sourceHostValue = value.source_host === "" || value.source_host == null ? null : value.source_host;
  const lastCheckedValue = value.last_checked_at === "" || value.last_checked_at == null ? null : value.last_checked_at;
  const lastSuccessValue = value.last_success_at === "" || value.last_success_at == null ? null : value.last_success_at;
  if (!profile || !isUuid(value.id) || !isUuid(value.supplier_id)
    || !requiredText(value.supplier_name, 200) || !requiredText(value.name, 200)
    || typeof value.source_type !== "string" || !sourceTypes.has(value.source_type)
    || !nullableText(value.file_name ?? null, 500) || !nullableText(value.file_format ?? null, 20)
    || !nullableCount(value.row_count ?? null) || typeof value.status !== "string" || !priceListStatuses.has(value.status)
    || !publicHostOrNull(sourceHostValue) || !count(value.source_config_revision) || value.source_config_revision < 1
    || !nullableCount(value.active_version_number ?? null)
    || !timestampOrNull(lastCheckedValue) || !timestampOrNull(lastSuccessValue)
    || !timestampOrNull(value.updated_at) || value.updated_at === null) return null;
  const latestJobValue = value.latest_job ?? null;
  const latestJob = latestJobValue === null ? null : readFeedJob(latestJobValue);
  if (latestJobValue !== null && !latestJob) return null;
  if (value.source_type === "url" && sourceHostValue === null) return null;
  return {
    id: value.id, supplier_id: value.supplier_id, supplier_name: value.supplier_name,
    name: value.name, provider: profile.provider, feed_role: profile.feed_role,
    source_type: value.source_type as CatalogPriceList["source_type"],
    file_name: (value.file_name ?? null) as string | null,
    file_format: (value.file_format ?? null) as string | null,
    row_count: (value.row_count ?? null) as number | null,
    status: value.status as CatalogPriceList["status"],
    source_host: sourceHostValue as string | null,
    source_config_revision: value.source_config_revision,
    active_version_number: (value.active_version_number ?? null) as number | null,
    last_checked_at: lastCheckedValue as string | null,
    last_success_at: lastSuccessValue as string | null,
    latest_job: latestJob, updated_at: value.updated_at,
  };
}

/** Builds a public DTO explicitly, so unknown upstream fields never cross the BFF. */
export function readCatalogDashboard(value: unknown, sellerId: string): CatalogDashboard | null {
  if (!isObject(value) || value.seller_id !== sellerId || typeof value.can_manage !== "boolean"
    || !Array.isArray(value.suppliers) || !Array.isArray(value.price_lists)) return null;
  const suppliers: CatalogSupplier[] = [];
  for (const candidate of value.suppliers) {
    const supplier = readSupplier(candidate);
    if (!supplier || suppliers.some((item) => item.id === supplier.id)) return null;
    suppliers.push(supplier);
  }
  const priceLists: CatalogPriceList[] = [];
  for (const candidate of value.price_lists) {
    const priceList = readPriceList(candidate);
    if (!priceList || !suppliers.some((supplier) => supplier.id === priceList.supplier_id)
      || priceLists.some((item) => item.id === priceList.id)) return null;
    priceLists.push(priceList);
  }
  return { seller_id: sellerId, can_manage: value.can_manage, suppliers, price_lists: priceLists };
}

export function readPriceListDetail(value: unknown, priceListId: string): PriceListDetail | null {
  if (!isObject(value) || !Array.isArray(value.products)) return null;
  const total = count(value.total) ? value.total : count(value.row_count) ? value.row_count : null;
  if (total === null || ("total" in value && "row_count" in value && value.total !== value.row_count)) return null;
  const priceList = readPriceList(value.price_list);
  if (!priceList || priceList.id !== priceListId) return null;
  const products: CatalogProduct[] = [];
  for (const candidate of value.products.slice(0, 200)) {
    if (!isObject(candidate)
      || !(typeof candidate.ean === "string" || candidate.ean === null)
      || !(typeof candidate.sku === "string" || candidate.sku === null)
      || !(typeof candidate.name === "string" || candidate.name === null)
      || !decimalOrNull(candidate.cost) || !decimalOrNull(candidate.shipping_cost)
      || !decimalOrNull(candidate.total_cost) || !decimalOrNull(candidate.quantity)) return null;
    products.push({
      ean: candidate.ean ?? "", sku: candidate.sku ?? "", name: candidate.name ?? "",
      cost: candidate.cost, shipping_cost: candidate.shipping_cost,
      total_cost: candidate.total_cost, quantity: candidate.quantity,
    });
  }
  if (products.length > total) return null;
  return { price_list: priceList, products, total };
}

/** Parses a 202 response and strips every field outside the public feed contract. */
export function readCatalogFeedMutation(value: unknown, expectedPriceListId?: string): CatalogFeedMutation | null {
  if (!isObject(value)) return null;
  const priceList = readPriceList(value.price_list);
  const job = readFeedJob(value.job);
  if (!priceList || !job || (expectedPriceListId && priceList.id !== expectedPriceListId)) return null;
  if (isObject(value.job) && "price_list_id" in value.job
    && (!isUuid(value.job.price_list_id) || value.job.price_list_id !== priceList.id)) return null;
  return { price_list: priceList, job };
}

/** The API returns {job}; the BFF exposes the same fixed job fields without the wrapper. */
export function readCatalogFeedJobResponse(value: unknown, expectedJobId: string, expectedPriceListId?: string): CatalogFeedJob | null {
  if (!isObject(value)) return null;
  const candidate = "job" in value ? value.job : value;
  const job = readFeedJob(candidate);
  if (!job || job.id !== expectedJobId) return null;
  if (expectedPriceListId && isObject(candidate) && "price_list_id" in candidate
    && (!isUuid(candidate.price_list_id) || candidate.price_list_id !== expectedPriceListId)) return null;
  return job;
}

export function readSupplierInput(value: unknown): SupplierInput | null {
  if (!isObject(value) || typeof value.name !== "string" || typeof value.notes !== "string") return null;
  const name = value.name.trim();
  const notes = value.notes.trim();
  if (!name || name.length > 200 || notes.length > 5_000) return null;
  return { name, notes };
}

export function readDeleteSupplierInput(value: unknown): DeleteSupplierInput | null {
  if (!isObject(value) || typeof value.confirmation !== "string") return null;
  const confirmation = value.confirmation;
  if (!confirmation || confirmation.length > 200 || confirmation !== confirmation.trim()) return null;
  return { confirmation };
}

export function readUrlPriceListInput(value: unknown): UrlPriceListInput | null {
  if (!isObject(value) || !isUuid(value.supplier_id) || typeof value.name !== "string"
    || typeof value.url !== "string" || typeof value.username !== "string" || typeof value.password !== "string") return null;
  const profile = readCatalogFeedProfile(value.provider, value.feed_role);
  const name = value.name.trim();
  const urlValue = value.url.trim();
  const username = value.username.trim();
  if (!profile || !name || name.length > 200 || !urlValue || urlValue.length > 4_096
    || username.length > 500 || value.password.length > 4_096 || Boolean(username) !== Boolean(value.password)) return null;
  let parsed: URL;
  try { parsed = new URL(urlValue); } catch { return null; }
  if (parsed.protocol !== "https:" || !parsed.hostname.includes(".") || parsed.username || parsed.password
    || parsed.hash || (parsed.port && parsed.port !== "443")) return null;
  return {
    supplier_id: value.supplier_id, name, provider: profile.provider, feed_role: profile.feed_role,
    url: urlValue, username, password: value.password,
  };
}

export function readUpdateUrlPriceListInput(value: unknown): UpdateUrlPriceListInput | null {
  if (!isObject(value) || typeof value.url !== "string" || typeof value.credentials_mode !== "string"
    || !["keep", "replace", "remove"].includes(value.credentials_mode)
    || !count(value.expected_config_revision) || value.expected_config_revision < 1
    || value.expected_config_revision > 2_147_483_646) return null;
  const urlValue = value.url.trim();
  if (!urlValue || urlValue.length > 4_096) return null;
  let parsed: URL;
  try { parsed = new URL(urlValue); } catch { return null; }
  if (parsed.protocol !== "https:" || !parsed.hostname.includes(".") || parsed.username || parsed.password
    || parsed.hash || (parsed.port && parsed.port !== "443")) return null;
  const credentialsMode = value.credentials_mode as CatalogCredentialMode;
  if (credentialsMode !== "replace") {
    return { url: urlValue, credentials_mode: credentialsMode, expected_config_revision: value.expected_config_revision };
  }
  if (typeof value.username !== "string" || typeof value.password !== "string") return null;
  const username = value.username.trim();
  if (!username || !value.password || username.length > 500 || value.password.length > 4_096) return null;
  return {
    url: urlValue, credentials_mode: credentialsMode,
    expected_config_revision: value.expected_config_revision, username, password: value.password,
  };
}

export function urlFeedHostMatches(url: string, currentHost: string): boolean {
  try {
    const nextHost = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    return Boolean(nextHost) && nextHost === currentHost.toLowerCase().replace(/\.$/, "");
  } catch {
    return false;
  }
}

export function readCatalogPriceListMutation(value: unknown, expectedPriceListId: string): CatalogPriceList | null {
  if (!isObject(value) || !("price_list" in value)) return null;
  const priceList = readPriceList(value.price_list);
  return priceList?.id === expectedPriceListId ? priceList : null;
}

export function isAllowedPriceListFile(file: File): boolean {
  if (!file.name || file.size <= 0 || file.size > MAX_PRICE_LIST_FILE_BYTES) return false;
  const extension = file.name.split(".").pop()?.toLowerCase();
  return Boolean(extension && PRICE_LIST_EXTENSIONS.includes(extension as (typeof PRICE_LIST_EXTENSIONS)[number]));
}

export function formatCatalogMoney(value: CatalogDecimal): string {
  if (value === null || !decimalOrNull(value)) return "—";
  return new Intl.NumberFormat("it-IT", {
    style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(value));
}
