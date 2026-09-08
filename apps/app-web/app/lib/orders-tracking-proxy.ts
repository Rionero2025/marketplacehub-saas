import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { isUuid } from "./seller-settings-types";
import { readOrderItem } from "./orders-types";
import { TRACKING_BODY_READ_TIMEOUT_MS, TRACKING_FILE_OPERATION_TIMEOUT_MS, TRACKING_PREFLIGHT_TIMEOUT_MS, readTrackingCapabilities, readTrackingImportResult, readTrackingManualInput, readTrackingMapping, readTrackingPreview, readTrackingScope, type TrackingManualInput } from "./orders-tracking-types";

type TrackingOperation = "capabilities" | "preview" | "import" | "manual";
const MAX_FILE_BYTES = 5_242_880;
const MAX_MULTIPART_BYTES = MAX_FILE_BYTES + 16_384;
const MAX_JSON_BYTES = 8_192;
const MAX_CONCURRENT_MULTIPARTS = 2;
type WebTrackingLease = { token: string; accountKey: string };
type WebTrackingAdmissionState = { tokens: Set<string>; accounts: Map<string, string> };
type TrackingGlobal = typeof globalThis & {
  __marketplaceHubTrackingAdmission?: WebTrackingAdmissionState;
};
const trackingGlobal = globalThis as TrackingGlobal;
const webTrackingAdmission = trackingGlobal.__marketplaceHubTrackingAdmission
  ??= { tokens: new Set<string>(), accounts: new Map<string, string>() };

function acquireWebTracking(sellerId: string, accountId: string): WebTrackingLease | "busy" | "capacity" {
  const accountKey = `${sellerId}:${accountId}`;
  if (webTrackingAdmission.accounts.has(accountKey)) return "busy";
  if (webTrackingAdmission.tokens.size >= MAX_CONCURRENT_MULTIPARTS) return "capacity";
  const lease = { token: crypto.randomUUID(), accountKey };
  webTrackingAdmission.tokens.add(lease.token);
  webTrackingAdmission.accounts.set(accountKey, lease.token);
  return lease;
}

function releaseWebTracking(lease: WebTrackingLease | null): void {
  if (!lease) return;
  if (webTrackingAdmission.accounts.get(lease.accountKey) === lease.token) {
    webTrackingAdmission.accounts.delete(lease.accountKey);
  }
  webTrackingAdmission.tokens.delete(lease.token);
}

const requestId = (value: string | null): string | null => value && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(value) ? value : null;
const localRequestId = () => `web-${crypto.randomUUID()}`;
const failure = (detail: string, status: number, reference: string | null = null, retryAfter: number | null = null) => {
  const effectiveReference = reference ?? (status >= 500 ? localRequestId() : null);
  return NextResponse.json(
    { detail, ...(effectiveReference ? { request_id: effectiveReference } : {}) },
    { status, headers: { "cache-control": "no-store", ...(effectiveReference ? { "x-request-id": effectiveReference } : {}),
      ...(retryAfter === null ? {} : { "retry-after": String(retryAfter) }) } },
  );
};

function sameOrigin(request: NextRequest): boolean {
  if (request.headers.get("sec-fetch-site") === "cross-site") return false;
  const origin = request.headers.get("origin");
  if (!origin) return false;
  const url = new URL(request.url), host = request.headers.get("host");
  if (host && /[\\/@?#\s,]/.test(host)) return false;
  try { return origin === (host ? new URL(`${url.protocol}//${host}`).origin : url.origin); }
  catch { return false; }
}

const extension = (name: string): "csv" | "xlsx" | "xls" | null => {
  const match = name.toLowerCase().match(/\.([a-z0-9]+)$/);
  return match && (match[1] === "csv" || match[1] === "xlsx" || match[1] === "xls") ? match[1] : null;
};

const fileEntry = (value: FormDataEntryValue | null): value is File => value instanceof Blob && typeof (value as File).name === "string";

class BodyLimitError extends Error {}
class BodyTimeoutError extends Error {}

function readBodyChunk(reader: ReadableStreamDefaultReader<Uint8Array>, signal: AbortSignal): Promise<ReadableStreamReadResult<Uint8Array>> {
  return new Promise((resolve, reject) => {
    const aborted = () => reject(new BodyTimeoutError());
    if (signal.aborted) { aborted(); return; }
    signal.addEventListener("abort", aborted, { once: true });
    void reader.read().then(
      (value) => { signal.removeEventListener("abort", aborted); resolve(value); },
      (error) => { signal.removeEventListener("abort", aborted); reject(error); },
    );
  });
}

function multipartBoundary(contentType: string | null): string | null {
  if (!contentType || contentType.length > 200) return null;
  const match = contentType.match(/^multipart\/form-data\s*;\s*boundary=(?:"([0-9A-Za-z'()+_,\-./:=?]{1,70})"|([0-9A-Za-z'()+_,\-./:=?]{1,70}))$/i);
  return match?.[1] ?? match?.[2] ?? null;
}

async function boundedBody(request: NextRequest, maximum: number): Promise<Uint8Array> {
  if (!request.body) throw new TypeError("missing body");
  const declared = request.headers.get("content-length");
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > maximum)) throw new BodyLimitError();
  const reader = request.body.getReader(), chunks: Uint8Array[] = [];
  const deadline = AbortSignal.timeout(TRACKING_BODY_READ_TIMEOUT_MS);
  const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
  let size = 0;
  try {
    while (true) {
      const { done, value } = await readBodyChunk(reader, signal);
      if (done) break;
      size += value.byteLength;
      if (size > maximum) {
        throw new BodyLimitError();
      }
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

async function boundedMultipart(request: NextRequest, operation: "preview" | "import"): Promise<FormData> {
  const contentType = request.headers.get("content-type"), boundary = multipartBoundary(contentType);
  if (!boundary) throw new TypeError("invalid multipart");
  const bytes = await boundedBody(request, MAX_MULTIPART_BYTES);
  const encoded = new TextDecoder("latin1").decode(bytes), marker = `--${boundary}`;
  let parts = 0, index = encoded.indexOf(marker);
  while (index >= 0) {
    if (index === 0 || encoded.slice(index - 2, index) === "\r\n") parts += 1;
    index = encoded.indexOf(marker, index + marker.length);
  }
  if (parts !== (operation === "preview" ? 2 : 3)) throw new TypeError("invalid part count");
  return new Response(bytes.buffer as ArrayBuffer, { headers: { "content-type": contentType! } }).formData();
}

async function boundedJson(request: NextRequest): Promise<unknown> {
  const contentType = request.headers.get("content-type")?.toLowerCase() ?? "";
  if (!/^application\/json(?:\s*;\s*charset=utf-8)?$/.test(contentType)) throw new TypeError("invalid json type");
  const bytes = await boundedBody(request, MAX_JSON_BYTES);
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}

async function validFile(value: FormDataEntryValue | null): Promise<boolean> {
  if (!fileEntry(value) || !value.name || value.name.length > 255 || /[\\/\0]/.test(value.name)
    || value.size < 1 || value.size > MAX_FILE_BYTES) return false;
  const kind = extension(value.name);
  if (!kind) return false;
  const mime = value.type.toLowerCase().split(";", 1)[0];
  const mimes: Record<typeof kind, Set<string>> = {
    csv: new Set(["", "text/csv", "application/csv", "text/plain", "application/vnd.ms-excel"]),
    xlsx: new Set(["", "application/octet-stream", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"]),
    xls: new Set(["", "application/octet-stream", "application/vnd.ms-excel"]),
  };
  if (!mimes[kind].has(mime)) return false;
  const bytes = new Uint8Array(await value.slice(0, 512).arrayBuffer());
  if (kind === "xlsx") return bytes.length >= 4 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 0x03 && bytes[3] === 0x04;
  if (kind === "xls") return bytes.length >= 8 && [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1].every((byte, index) => bytes[index] === byte);
  if (bytes.some((byte) => byte === 0)) return false;
  const start = new TextDecoder("utf-8", { fatal: false }).decode(bytes).replace(/^\uFEFF/, "").trimStart().toLowerCase();
  return !start.startsWith("<html") && !start.startsWith("<!doctype html");
}

export async function ordersTrackingProxy(request: NextRequest, sellerId: string, operation: TrackingOperation) {
  if (!isUuid(sellerId)) return failure("Riferimento non valido.", 422);
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (operation !== "capabilities" && !sameOrigin(request)) return failure("Richiesta non consentita.", 403);

  let scope = operation === "manual" ? null : readTrackingScope(new URL(request.url).searchParams);
  let body: BodyInit | undefined;
  let manualInput: TrackingManualInput | null = null;
  if (operation === "manual") {
    let raw: unknown = null;
    try { raw = await boundedJson(request); }
    catch (error) {
      return error instanceof BodyLimitError
        ? failure("La richiesta supera il limite consentito.", 413)
        : error instanceof BodyTimeoutError
          ? failure("Tempo massimo di caricamento superato. Riprova.", 408)
        : failure("Scegli un ordine e indica almeno il corriere oppure il tracking.", 422);
    }
    const input = readTrackingManualInput(raw);
    if (!input) return failure("Scegli un ordine e indica almeno il corriere oppure il tracking.", 422);
    manualInput = input;
    scope = { account_id: input.account_id, environment: input.environment };
    body = JSON.stringify(input);
  } else if (!scope) return failure("Account o ambiente non valido.", 422);

  let webLease: WebTrackingLease | null = null;
  try {
    if (operation === "preview" || operation === "import") {
    // Validate the cookie and exact Seller/account grant before consuming a
    // multi-megabyte body. A forged non-empty Cookie must not reach multipart
    // materialization in this process.
    const preflightDeadline = AbortSignal.timeout(TRACKING_PREFLIGHT_TIMEOUT_MS);
    const preflightSignal = request.signal
      ? AbortSignal.any([request.signal, preflightDeadline])
      : preflightDeadline;
    const preflight = await fetch(
      `${apiUrl}/v1/sellers/${sellerId}/orders/tracking/capabilities?${new URLSearchParams(scope)}`,
      { method: "GET", headers: { cookie }, cache: "no-store", redirect: "error", signal: preflightSignal },
    ).catch(() => null);
    if (!preflight) return failure("Servizio tracking temporaneamente non disponibile. Riprova.", 503);
    const preflightReference = requestId(preflight.headers.get("x-request-id"));
    if (preflight.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401, preflightReference);
    if (preflight.status === 403) return failure("Non hai accesso alla gestione tracking di questo account.", 403, preflightReference);
    if (preflight.status === 404) return failure("L’account marketplace non è più disponibile.", 404, preflightReference);
    if (!preflight.ok) return failure("Servizio tracking temporaneamente non disponibile. Riprova.", preflight.status >= 500 ? 503 : 422, preflightReference);
    const preflightRaw: unknown = await preflight.json().catch(() => null);
    const preflightCapabilities = preflightRaw && typeof preflightRaw === "object" && "capabilities" in preflightRaw
      ? readTrackingCapabilities(preflightRaw.capabilities)
      : null;
    if (!preflightCapabilities?.can_import) return failure("Non hai accesso alla gestione tracking di questo account.", 403, preflightReference);

    const admission = acquireWebTracking(sellerId, scope.account_id);
    if (admission === "busy") return failure("Un’altra operazione tracking è già in corso per questo account. Attendi e riprova.", 409);
    if (admission === "capacity") return failure("Il servizio di importazione tracking è occupato. Attendi e riprova.", 429, null, 5);
    webLease = admission;

    let incoming: FormData | null = null;
    try { incoming = await boundedMultipart(request, operation); }
    catch (error) {
      return error instanceof BodyLimitError
        ? failure("Il file supera il limite consentito.", 413)
        : error instanceof BodyTimeoutError
          ? failure("Tempo massimo di caricamento superato. Riprova.", 408)
        : failure("File non leggibile. Carica un CSV, XLSX o XLS valido.", 422);
    }
    if (!incoming) return failure("File non leggibile. Carica un CSV, XLSX o XLS valido.", 422);
    const allowed = operation === "preview" ? new Set(["file"]) : new Set(["file", "mapping"]);
    if ([...incoming.keys()].some((key) => !allowed.has(key)) || incoming.getAll("file").length !== 1
      || (operation === "import" && incoming.getAll("mapping").length !== 1)) {
      return failure("File non leggibile. Carica un CSV, XLSX o XLS valido.", 422);
    }
    const file = incoming.get("file");
    if (!await validFile(file)) return failure(`Il file deve essere CSV, XLSX o XLS e non può superare ${MAX_FILE_BYTES / 1_048_576} MB.`, fileEntry(file) && file.size > MAX_FILE_BYTES ? 413 : 422);
    if (!fileEntry(file)) return failure("File non leggibile. Carica un CSV, XLSX o XLS valido.", 422);
    const outgoing = new FormData(); outgoing.set("file", file, file.name);
    if (operation === "import") {
      const raw = incoming.get("mapping");
      if (typeof raw !== "string") return failure("Controlla l’associazione delle colonne.", 422);
      let parsed: unknown = null;
      try { parsed = JSON.parse(raw || "null"); } catch { return failure("Controlla l’associazione delle colonne.", 422); }
      const mapping = readTrackingMapping(parsed);
      if (!mapping) return failure("Associa un identificativo ordine e almeno un dato di spedizione.", 422);
      outgoing.set("mapping", JSON.stringify(mapping));
    }
    body = outgoing;
  }

  const query = operation === "manual" ? "" : `?${new URLSearchParams(scope!)}`;
  const deadline = AbortSignal.timeout(
    operation === "preview" || operation === "import" ? TRACKING_FILE_OPERATION_TIMEOUT_MS : 30_000,
  );
  const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
  const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/orders/tracking/${operation}${query}`, {
    method: operation === "capabilities" ? "GET" : operation === "manual" ? "PATCH" : "POST",
    headers: { cookie, ...(operation === "manual" ? { "content-type": "application/json" } : {}) },
    body, cache: "no-store", redirect: "error", signal,
  }).catch(() => null);
  if (!upstream) return failure("Operazione tracking non confermata. Aggiorna gli ordini e riprova.", 503);
  const reference = requestId(upstream.headers.get("x-request-id"));
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401, reference);
  if (upstream.status === 403) return failure("Non hai accesso alla gestione tracking di questo account.", 403, reference);
  if (upstream.status === 404) return failure(operation === "manual"
    ? "L’unità ordine non è presente nell’archivio."
    : "L’account marketplace non è più disponibile.", 404, reference);
  if (upstream.status === 409) return failure("Un’altra operazione tracking è già in corso per questo account. Attendi e riprova.", 409, reference);
  if (upstream.status === 429) return failure("Il servizio di importazione tracking è occupato. Attendi e riprova.", 429, reference, 5);
  if (upstream.status === 408) return failure("Tempo massimo di caricamento superato. Riprova.", 408, reference);
  if (!upstream.ok) {
    const status = upstream.status === 413 ? 413 : upstream.status >= 500 ? 503 : 422;
    const validationMessage = operation === "preview" ? "Il file non è leggibile oppure supera i limiti consentiti."
      : operation === "import" ? "Controlla il file e l’associazione delle colonne."
        : operation === "manual" ? "Indica almeno il corriere oppure il tracking."
          : "Funzioni tracking non disponibili per questo account.";
    return failure(status === 413 ? "Il file supera il limite consentito." : status === 422 ? validationMessage : "Servizio tracking temporaneamente non disponibile. Riprova.", status, reference);
  }
  const raw: unknown = await upstream.json().catch(() => null);
  if (!raw || typeof raw !== "object") return failure("Risposta tracking non valida. Aggiorna i dati.", 502, reference);
  if (operation === "capabilities") {
    const capabilities = readTrackingCapabilities("capabilities" in raw ? raw.capabilities : null);
    return capabilities ? NextResponse.json({ capabilities }, { headers: { "cache-control": "no-store" } }) : failure("Funzioni tracking non disponibili. Aggiorna i dati.", 502, reference);
  }
  if (operation === "preview") {
    const preview = readTrackingPreview("preview" in raw ? raw.preview : null);
    return preview ? NextResponse.json({ preview }, { headers: { "cache-control": "no-store" } }) : failure("Anteprima del file non valida. Controlla l’export.", 502, reference);
  }
  if (operation === "import") {
    const result = readTrackingImportResult("result" in raw ? raw.result : null);
    return result ? NextResponse.json({ result }, { headers: { "cache-control": "no-store" } }) : failure("Esito dell’importazione non valido. Aggiorna gli ordini.", 502, reference);
  }
  const item = readOrderItem("item" in raw ? raw.item : null);
  return raw && "updated" in raw && raw.updated === 1 && item && item.id === manualInput?.line_id
    ? NextResponse.json({ updated: 1, item }, { headers: { "cache-control": "no-store" } })
    : failure("Salvataggio tracking non confermato. Aggiorna gli ordini.", 502, reference);
  } finally {
    releaseWebTracking(webLease);
  }
}
