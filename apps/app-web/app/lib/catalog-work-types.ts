import { isUuid } from "./seller-settings-types";

export type WorkAccount = { id: string; marketplace: string; name: string };
export type WorkView = { id: string; name: string; source_name: string; row_count: number; revision: number; account_ids: string[]; updated_at: string };
export type WorkRow = { id: string; ean: string; sku: string; name: string; cost: string; shipping_cost: string; total_cost: string; quantity: string; price: string; minimum_price: string; weight_kg: string | null; length_cm: string | null; width_cm: string | null; height_cm: string | null };
export type Recipe = { price_list_id: string; version: number; content_list_id: string | null; content_version: number | null; search: string; min_qty: string; min_cost: string; max_cost: string; measure: string; exclude: string; lower: string; upper: string; shipping: string; margin: string; minimum_margin: string };
export type WorkIndex = { seller_id: string; can_manage: boolean; accounts: WorkAccount[]; views: WorkView[] };
export type WorkData = { seller_id: string; page: number; total: number; rows: WorkRow[]; view?: WorkView };
export const economicFields = ["cost", "shipping_cost", "total_cost", "quantity", "price", "minimum_price"] as const;
export type EditableField = typeof economicFields[number] | "ean" | "sku" | "name" | "weight_kg";
export type Edits = Record<string, Partial<Record<EditableField, string>>>;
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === "string" && v.length <= 10000;
const count = (v: unknown): v is number => Number.isSafeInteger(v) && Number(v) >= 0;
const decimal = (v: unknown): v is string => typeof v === "string" && /^\d+(\.\d+)?$/.test(v) && Number.isFinite(Number(v));

export function readWorkView(v: unknown): WorkView | null {
  if (!object(v) || !isUuid(v.id) || !text(v.name) || !text(v.source_name) || !count(v.row_count) || !count(v.revision) || v.revision < 1 || !text(v.updated_at) || !Array.isArray(v.account_ids) || !v.account_ids.every(isUuid)) return null;
  return { id: v.id, name: v.name, source_name: v.source_name, row_count: v.row_count, revision: v.revision, updated_at: v.updated_at, account_ids: v.account_ids as string[] };
}
export function readWorkIndex(v: unknown, seller: string): WorkIndex | null {
  if (!object(v) || v.seller_id !== seller || typeof v.can_manage !== "boolean" || !Array.isArray(v.accounts) || !Array.isArray(v.views)) return null;
  const accounts: WorkAccount[] = [], views: WorkView[] = [];
  for (const a of v.accounts) {
    if (!object(a) || !isUuid(a.id) || !text(a.marketplace) || !text(a.name)) return null;
    accounts.push({ id: a.id, marketplace: a.marketplace, name: a.name });
  }
  for (const item of v.views) { const view = readWorkView(item); if (!view) return null; views.push(view); }
  return { seller_id: seller, can_manage: v.can_manage, accounts, views };
}
export function readWorkData(v: unknown, seller: string, viewId?: string): WorkData | null {
  if (!object(v) || v.seller_id !== seller || !count(v.page) || v.page < 1 || !count(v.total) || !Array.isArray(v.rows) || v.rows.length > 50 || v.rows.length > v.total) return null;
  const rows: WorkRow[] = [];
  for (const r of v.rows) {
    if (!object(r) || !isUuid(r.id) || !text(r.ean) || !text(r.sku) || !text(r.name) || !economicFields.every(f => decimal(r[f]))) return null;
    const physical: Record<string, string | null> = {};
    for (const f of ["weight_kg", "length_cm", "width_cm", "height_cm"]) {
      if (r[f] !== null && !decimal(r[f])) return null;
      physical[f] = r[f] as string | null;
    }
    rows.push({ id: r.id, ean: r.ean, sku: r.sku, name: r.name, ...Object.fromEntries(economicFields.map(f => [f, r[f]])), ...physical } as WorkRow);
  }
  if (new Set(rows.map(r => r.id)).size !== rows.length) return null;
  const view = "view" in v ? readWorkView(v.view) : null;
  if (viewId && (!view || view.id !== viewId || view.row_count !== v.total)) return null;
  return { seller_id: seller, page: v.page, total: v.total, rows, ...(view ? {view} : {}) };
}
