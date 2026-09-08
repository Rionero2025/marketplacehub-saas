import type { OrderEnvironment, OrderItem } from "./orders-types";

export const TRACKING_BODY_READ_TIMEOUT_MS = 60_000;
export const TRACKING_PREFLIGHT_TIMEOUT_MS = 30_000;
export const TRACKING_FILE_OPERATION_TIMEOUT_MS = 120_000;
export const TRACKING_UI_TIMEOUT_GRACE_MS = 5_000;
export const TRACKING_UI_FILE_OPERATION_TIMEOUT_MS = TRACKING_PREFLIGHT_TIMEOUT_MS
  + TRACKING_BODY_READ_TIMEOUT_MS
  + TRACKING_FILE_OPERATION_TIMEOUT_MS + TRACKING_UI_TIMEOUT_GRACE_MS;

export const trackingFields = ["id_order_unit", "id_order", "carrier_code", "tracking_numbers", "combined_shipment"] as const;
export type TrackingField = typeof trackingFields[number];
export type TrackingMapping = Record<TrackingField, string>;

export type TrackingCapabilities = {
  marketplace: "kaufland";
  formats: Array<"csv" | "xlsx" | "xls">;
  max_bytes: number;
  max_rows: number;
  max_columns: number;
  max_cells: number;
  max_cell_length: number;
  preview_rows: number;
  can_import: boolean;
  can_edit: boolean;
};

export type TrackingPreview = {
  file_name: string;
  row_count: number;
  columns: string[];
  rows: Array<Record<string, string>>;
  detected: Partial<TrackingMapping>;
};

export type TrackingImportIssue = { row: number; order_unit_id?: string; order_id?: string; error?: string };
export type TrackingImportResult = {
  updated: number;
  unmatched: TrackingImportIssue[];
  invalid: TrackingImportIssue[];
};

export type TrackingScope = { account_id: string; environment: OrderEnvironment };
export type TrackingManualInput = TrackingScope & { line_id: string; carrier: string; tracking: string };
export type TrackingManualResult = { updated: 1; item: OrderItem };

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const text = (value: unknown, max = 500): value is string => typeof value === "string" && value.length <= max;
const natural = (value: unknown, max: number): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= max;
const environment = (value: unknown): value is OrderEnvironment => value === "live" || value === "playground";
const uuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);

export function readTrackingScope(params: URLSearchParams): TrackingScope | null {
  const account_id = params.get("account_id"), value = params.get("environment") ?? "live";
  return uuid(account_id) && environment(value) ? { account_id, environment: value } : null;
}

export function readTrackingCapabilities(value: unknown): TrackingCapabilities | null {
  if (!object(value) || value.marketplace !== "kaufland" || !Array.isArray(value.formats)
    || value.formats.length !== 3 || new Set(value.formats).size !== 3
    || !value.formats.every((item) => item === "csv" || item === "xlsx" || item === "xls")
    || value.max_bytes !== 5_242_880 || value.max_rows !== 10_000
    || value.max_columns !== 100 || value.max_cells !== 200_000
    || value.max_cell_length !== 2_000 || value.preview_rows !== 10
    || typeof value.can_import !== "boolean" || typeof value.can_edit !== "boolean") return null;
  return { marketplace: "kaufland", formats: [...value.formats] as TrackingCapabilities["formats"],
    max_bytes: value.max_bytes, max_rows: value.max_rows, max_columns: value.max_columns,
    max_cells: value.max_cells, max_cell_length: value.max_cell_length,
    preview_rows: value.preview_rows,
    can_import: value.can_import, can_edit: value.can_edit };
}

export function emptyTrackingMapping(detected: Partial<TrackingMapping> = {}): TrackingMapping {
  return Object.fromEntries(trackingFields.map((field) => [field, detected[field] ?? ""])) as TrackingMapping;
}

export function readTrackingMapping(value: unknown, columns?: string[]): TrackingMapping | null {
  if (!object(value)) return null;
  const clean = {} as TrackingMapping;
  for (const field of trackingFields) {
    const entry = value[field];
    if (!text(entry, 200) || (entry && (!entry.trim() || (columns && !columns.includes(entry))))) return null;
    clean[field] = entry;
  }
  if (!clean.id_order_unit && !clean.id_order) return null;
  if (!clean.carrier_code && !clean.tracking_numbers && !clean.combined_shipment) return null;
  return clean;
}

export function readTrackingPreview(value: unknown): TrackingPreview | null {
  if (!object(value) || !text(value.file_name, 255) || !value.file_name || /[\\/\0]/.test(value.file_name)
    || !natural(value.row_count, 10_000) || !Array.isArray(value.columns) || value.columns.length < 1 || value.columns.length > 100
    || !value.columns.every((column) => text(column, 200) && Boolean(column.trim())) || new Set(value.columns).size !== value.columns.length
    || !Array.isArray(value.rows) || value.rows.length > 10 || value.rows.length > value.row_count || !object(value.detected)) return null;
  const columns = value.columns as string[];
  const rows: Array<Record<string, string>> = [];
  for (const raw of value.rows) {
    if (!object(raw) || Object.keys(raw).some((key) => !columns.includes(key))) return null;
    const row = Object.create(null) as Record<string, string>;
    for (const column of columns) {
      const cell = raw[column] ?? "";
      if (!text(cell, 2_000)) return null;
      row[column] = cell;
    }
    rows.push(row);
  }
  const detected: Partial<TrackingMapping> = {};
  for (const field of trackingFields) {
    const column = value.detected[field];
    if (column === undefined) continue;
    if (!text(column, 200) || !columns.includes(column)) return null;
    detected[field] = column;
  }
  return { file_name: value.file_name, row_count: value.row_count, columns: [...columns], rows, detected };
}

function readUnmatched(value: unknown): TrackingImportIssue | null {
  if (!object(value) || !natural(value.row, 10_000) || value.row < 1
    || !(value.order_unit_id === undefined || text(value.order_unit_id, 200))
    || !(value.order_id === undefined || text(value.order_id, 200))) return null;
  return { row: value.row, ...(value.order_unit_id === undefined ? {} : { order_unit_id: value.order_unit_id }),
    ...(value.order_id === undefined ? {} : { order_id: value.order_id }) };
}

function readInvalid(value: unknown): TrackingImportIssue | null {
  if (!object(value) || !natural(value.row, 10_000) || value.row < 1
    || (value.error !== "Identificativo ordine assente" && value.error !== "Tracking/corriere assente")) return null;
  return { row: value.row, error: value.error };
}

export function readTrackingImportResult(value: unknown): TrackingImportResult | null {
  if (!object(value) || !natural(value.updated, 10_000) || !Array.isArray(value.unmatched) || !Array.isArray(value.invalid)
    || value.unmatched.length > 10_000 || value.invalid.length > 10_000) return null;
  const unmatched = value.unmatched.map(readUnmatched), invalid = value.invalid.map(readInvalid);
  if (unmatched.some((item) => !item) || invalid.some((item) => !item)) return null;
  return { updated: value.updated, unmatched: unmatched as TrackingImportIssue[], invalid: invalid as TrackingImportIssue[] };
}

export function readTrackingManualInput(value: unknown): TrackingManualInput | null {
  if (!object(value) || !uuid(value.account_id) || !environment(value.environment) || !uuid(value.line_id)
    || !text(value.carrier, 2_000) || !text(value.tracking, 2_000)) return null;
  const carrier = value.carrier.trim(), tracking = value.tracking.trim();
  return carrier || tracking ? { account_id: value.account_id, environment: value.environment, line_id: value.line_id, carrier, tracking } : null;
}
