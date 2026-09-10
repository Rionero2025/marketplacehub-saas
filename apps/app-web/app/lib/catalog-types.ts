import { isUuid } from "./seller-settings-types";

export const MAX_PRICE_LIST_FILE_BYTES = 20 * 1024 * 1024;
export const PRICE_LIST_EXTENSIONS = ["csv", "txt", "tsv", "xls", "xlsx", "xml"] as const;

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
  source_type: string;
  file_name: string;
  file_format: string;
  row_count: number;
  status: string;
  updated_at: string;
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

type ObjectValue = Record<string, unknown>;
const isObject = (value: unknown): value is ObjectValue => typeof value === "object" && value !== null && !Array.isArray(value);
const requiredText = (value: unknown, maximum = 500): value is string => typeof value === "string" && Boolean(value.trim()) && value.length <= maximum;
const optionalText = (value: unknown, maximum = 5_000): value is string => typeof value === "string" && value.length <= maximum;
const count = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;
const decimalOrNull = (value: unknown): value is CatalogDecimal => value === null
  || (typeof value === "string" && value.length <= 50 && /^-?\d+(?:\.\d+)?$/.test(value) && Number.isFinite(Number(value)));

function readSupplier(value: unknown): CatalogSupplier | null {
  if (!isObject(value) || !isUuid(value.id) || !requiredText(value.name, 200)
    || !optionalText(value.notes) || !count(value.price_list_count)) return null;
  return { id: value.id, name: value.name, notes: value.notes, price_list_count: value.price_list_count };
}

function readPriceList(value: unknown): CatalogPriceList | null {
  if (!isObject(value) || !isUuid(value.id) || !isUuid(value.supplier_id)
    || !requiredText(value.supplier_name, 200) || !requiredText(value.name, 200)
    || !requiredText(value.source_type, 40) || !requiredText(value.file_name, 500)
    || !requiredText(value.file_format, 20) || !count(value.row_count)
    || !requiredText(value.status, 40) || !requiredText(value.updated_at, 100)) return null;
  return {
    id: value.id, supplier_id: value.supplier_id, supplier_name: value.supplier_name,
    name: value.name, source_type: value.source_type, file_name: value.file_name,
    file_format: value.file_format, row_count: value.row_count, status: value.status,
    updated_at: value.updated_at,
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
