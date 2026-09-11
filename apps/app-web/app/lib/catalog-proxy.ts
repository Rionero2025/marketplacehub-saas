import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import {
  MAX_PRICE_LIST_FILE_BYTES,
  isAllowedPriceListFile,
  readCatalogFeedProfile,
  readCatalogFeedJobResponse,
  readCatalogFeedMutation,
  readCatalogPriceListMutation,
  readCatalogDashboard,
  readDeleteSupplierInput,
  readPriceListDetail,
  readSupplierInput,
  readUpdateUrlPriceListInput,
  readUrlPriceListInput,
} from "./catalog-types";
import { isUuid } from "./seller-settings-types";

export type CatalogProxyOperation = "dashboard" | "create-supplier" | "delete-supplier"
  | "create-price-list" | "create-price-list-url" | "update-price-list-url" | "refresh-price-list"
  | "detail" | "job" | "delete-price-list";

const MAX_MULTIPART_BODY_BYTES = MAX_PRICE_LIST_FILE_BYTES + 64 * 1024;
const MAX_JSON_BODY_BYTES = 16 * 1024;
const CATALOG_BODY_READ_TIMEOUT_MS = 60_000;
const CATALOG_PREFLIGHT_TIMEOUT_MS = 30_000;
const MAX_CONCURRENT_CATALOG_UPLOADS = 2;
type CatalogUploadLease = { token: string; sellerId: string };
type CatalogAdmissionState = { tokens: Set<string>; sellers: Map<string, string> };
type CatalogGlobal = typeof globalThis & { __marketplaceHubCatalogAdmission?: CatalogAdmissionState };
const catalogGlobal = globalThis as CatalogGlobal;
const catalogAdmission = catalogGlobal.__marketplaceHubCatalogAdmission
  ??= { tokens: new Set<string>(), sellers: new Map<string, string>() };

function acquireCatalogUpload(sellerId: string): CatalogUploadLease | "busy" | "capacity" {
  if (catalogAdmission.sellers.has(sellerId)) return "busy";
  if (catalogAdmission.tokens.size >= MAX_CONCURRENT_CATALOG_UPLOADS) return "capacity";
  const lease = { token: crypto.randomUUID(), sellerId };
  catalogAdmission.tokens.add(lease.token);
  catalogAdmission.sellers.set(sellerId, lease.token);
  return lease;
}

function releaseCatalogUpload(lease: CatalogUploadLease | null): void {
  if (!lease) return;
  if (catalogAdmission.sellers.get(lease.sellerId) === lease.token) {
    catalogAdmission.sellers.delete(lease.sellerId);
  }
  catalogAdmission.tokens.delete(lease.token);
}

const failure = (detail: string, status: number, retryAfter: number | null = null) => NextResponse.json(
  { detail },
  { status, headers: { "cache-control": "no-store", ...(retryAfter === null ? {} : { "retry-after": String(retryAfter) }) } },
);

function requestOrigin(request: NextRequest): string | null {
  const url = new URL(request.url);
  const host = request.headers.get("host");
  if (!host) return url.origin;
  if (/[\\/@?#\s,]/.test(host)) return null;
  try { return new URL(`${url.protocol}//${host}`).origin; } catch { return null; }
}

function isWrite(operation: CatalogProxyOperation) {
  return !["dashboard", "detail", "job"].includes(operation);
}

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

async function boundedBody(request: NextRequest, maximum: number, timeoutMs: number): Promise<Uint8Array> {
  const statedLength = request.headers.get("content-length");
  if (statedLength && (!/^\d+$/.test(statedLength) || Number(statedLength) > maximum)) {
    throw new BodyLimitError();
  }
  if (!request.body) throw new TypeError("missing body");
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  const deadline = AbortSignal.timeout(timeoutMs);
  const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
  let total = 0;
  try {
    while (true) {
      const { done, value } = await readBodyChunk(reader, signal);
      if (done) break;
      total += value.byteLength;
      if (total > maximum) {
        throw new BodyLimitError();
      }
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}

async function limitedFormData(request: NextRequest): Promise<FormData> {
  const contentType = request.headers.get("content-type");
  if (!multipartBoundary(contentType)) throw new TypeError("invalid multipart");
  const bytes = await boundedBody(request, MAX_MULTIPART_BODY_BYTES, CATALOG_BODY_READ_TIMEOUT_MS);
  return new Response(bytes.buffer as ArrayBuffer, { headers: { "content-type": contentType! } }).formData();
}

async function limitedJson(request: NextRequest): Promise<unknown> {
  const contentType = request.headers.get("content-type");
  if (!contentType || !/^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(contentType)) {
    throw new TypeError("invalid json content type");
  }
  const bytes = await boundedBody(request, MAX_JSON_BODY_BYTES, 10_000);
  return new Response(bytes.buffer as ArrayBuffer).json();
}

async function authorizeCatalogManage(
  request: NextRequest,
  sellerId: string,
  cookie: string,
): Promise<NextResponse | null> {
  const deadline = AbortSignal.timeout(CATALOG_PREFLIGHT_TIMEOUT_MS);
  const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
  const preflight = await fetch(`${apiUrl}/v1/sellers/${sellerId}/catalogs`, {
    method: "GET", headers: { cookie, accept: "application/json" }, cache: "no-store",
    redirect: "error", signal,
  }).catch(() => null);
  if (!preflight) return failure("Servizio catalogo momentaneamente non disponibile.", 503);
  if (preflight.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (preflight.status === 403 || preflight.status === 404) {
    return failure("Non hai accesso alla gestione dei listini di questo negozio.", preflight.status);
  }
  if (!preflight.ok) {
    return preflight.status === 429
      ? failure("Il servizio di importazione listini è occupato. Attendi e riprova.", 429, 5)
      : failure("Servizio catalogo momentaneamente non disponibile.", preflight.status >= 500 ? 503 : 422);
  }
  const dashboard = readCatalogDashboard(await preflight.json().catch(() => null), sellerId);
  if (!dashboard) return failure("Risposta catalogo non valida.", 502);
  return dashboard.can_manage
    ? null
    : failure("Non hai accesso alla gestione dei listini di questo negozio.", 403);
}

function upstreamFailure(operation: CatalogProxyOperation, status: number) {
  if (status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (status === 403 || status === 404) return failure("Non hai accesso a questi dati o la risorsa non è più disponibile.", status);
  if (status === 409) {
    const detail = operation === "create-supplier" ? "Esiste già un fornitore con questo nome."
      : operation === "create-price-list" || operation === "create-price-list-url" ? "Esiste già un listino con questo nome."
        : operation === "update-price-list-url" ? "La configurazione del feed è cambiata. Aggiorna i dati e riprova."
        : operation === "refresh-price-list" ? "Un aggiornamento di questo listino è già in corso."
        : operation === "delete-supplier" ? "Il fornitore non può essere eliminato nello stato attuale."
          : "Il listino non può essere eliminato nello stato attuale.";
    return failure(detail, 409);
  }
  if (status === 413) return failure("Il file supera il limite di 20 MiB.", 413);
  if (status === 429) return failure("Il servizio di importazione listini è occupato. Attendi e riprova.", 429, 5);
  if (status === 408) return failure("Tempo massimo di caricamento superato. Riprova.", 408);
  if (status === 422) return failure("Controlla i dati inseriti e riprova.", 422);
  return failure("Servizio catalogo momentaneamente non disponibile.", 503);
}

export async function catalogProxy(
  request: NextRequest,
  sellerId: string,
  operation: CatalogProxyOperation,
  resourceId?: string,
  jobId?: string,
) {
  const resourceRequired = ["delete-supplier", "detail", "delete-price-list", "update-price-list-url", "refresh-price-list", "job"].includes(operation);
  if (!isUuid(sellerId) || (resourceRequired && !isUuid(resourceId)) || (operation === "job" && !isUuid(jobId))) {
    return failure("Negozio o risorsa non valida.", 422);
  }
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (isWrite(operation)) {
    const origin = request.headers.get("origin");
    if (!origin || origin !== requestOrigin(request)) return failure("Richiesta non consentita.", 403);
  }

  let uploadLease: CatalogUploadLease | null = null;
  try {
    let body: BodyInit | undefined;
    let contentType: string | undefined;
    let expectedSupplierId: string | undefined;
    let expectedFeedProfile: ReturnType<typeof readCatalogFeedProfile> = null;
    if (operation === "create-supplier") {
      const input = readSupplierInput(await limitedJson(request).catch(() => null));
      if (!input) return failure("Inserisci un nome fornitore valido e controlla le note.", 422);
      body = JSON.stringify(input);
      contentType = "application/json";
    } else if (operation === "delete-supplier") {
      const input = readDeleteSupplierInput(await limitedJson(request).catch(() => null));
      if (!input) return failure("Scrivi esattamente il nome del fornitore per confermare.", 422);
      body = JSON.stringify(input);
      contentType = "application/json";
    } else if (operation === "delete-price-list") {
      const input: unknown = await limitedJson(request).catch(() => null);
      if (!input || typeof input !== "object" || !("confirmation" in input) || input.confirmation !== "ELIMINA") {
        return failure("Scrivi ELIMINA per confermare l’eliminazione.", 422);
      }
      body = JSON.stringify({ confirmation: "ELIMINA" });
      contentType = "application/json";
    } else if (operation === "create-price-list" || operation === "create-price-list-url"
      || operation === "update-price-list-url" || operation === "refresh-price-list") {
      // Authenticate and authorize the exact Seller before reading a potentially
      // multi-megabyte body or any secret. A forged non-empty Cookie never reaches parsing.
      const preflightFailure = await authorizeCatalogManage(request, sellerId, cookie);
      if (preflightFailure) return preflightFailure;

      if (operation === "create-price-list-url") {
        let input: ReturnType<typeof readUrlPriceListInput>;
        try { input = readUrlPriceListInput(await limitedJson(request)); }
        catch (error) {
          return error instanceof BodyLimitError
            ? failure("I dati del feed superano il limite consentito.", 413)
            : error instanceof BodyTimeoutError
              ? failure("Tempo massimo di lettura superato. Riprova.", 408)
              : failure("Controlla URL HTTPS e credenziali del feed.", 422);
        }
        if (!input) return failure("Controlla URL HTTPS e credenziali del feed.", 422);
        expectedSupplierId = input.supplier_id;
        expectedFeedProfile = { provider: input.provider, feed_role: input.feed_role };
        body = JSON.stringify(input);
        contentType = "application/json";
      } else if (operation === "update-price-list-url") {
        let input: ReturnType<typeof readUpdateUrlPriceListInput>;
        try { input = readUpdateUrlPriceListInput(await limitedJson(request)); }
        catch (error) {
          return error instanceof BodyLimitError
            ? failure("I dati del feed superano il limite consentito.", 413)
            : error instanceof BodyTimeoutError
              ? failure("Tempo massimo di lettura superato. Riprova.", 408)
              : failure("Controlla URL HTTPS e impostazioni delle credenziali.", 422);
        }
        if (!input) return failure("Controlla URL HTTPS e impostazioni delle credenziali.", 422);
        body = JSON.stringify(input);
        contentType = "application/json";
      } else if (operation === "refresh-price-list") {
        let input: unknown;
        try { input = await limitedJson(request); }
        catch (error) {
          return error instanceof BodyLimitError
            ? failure("La richiesta supera il limite consentito.", 413)
            : error instanceof BodyTimeoutError
              ? failure("Tempo massimo di lettura superato. Riprova.", 408)
              : failure("Richiesta di aggiornamento non valida.", 422);
        }
        if (!input || typeof input !== "object" || Array.isArray(input)) {
          return failure("Richiesta di aggiornamento non valida.", 422);
        }
        body = "{}";
        contentType = "application/json";
      } else {

        const admission = acquireCatalogUpload(sellerId);
        if (admission === "busy") {
          return failure("Un’altra importazione listino è già in corso per questo negozio. Attendi e riprova.", 409);
        }
        if (admission === "capacity") {
          return failure("Il servizio di importazione listini è occupato. Attendi e riprova.", 429, 5);
        }
        uploadLease = admission;

        let form: FormData;
        try { form = await limitedFormData(request); }
        catch (error) {
          return error instanceof BodyLimitError
            ? failure("Il file supera il limite di 20 MiB.", 413)
            : error instanceof BodyTimeoutError
              ? failure("Tempo massimo di caricamento superato. Riprova.", 408)
              : failure("File non leggibile. Usa CSV, TXT, TSV, XLS, XLSX o XML.", 422);
        }
        const supplierId = form.get("supplier_id");
        const nameValue = form.get("name");
        const profile = readCatalogFeedProfile(form.get("provider"), form.get("feed_role"));
        const file = form.get("file");
        const name = typeof nameValue === "string" ? nameValue.trim() : "";
        if (!isUuid(supplierId) || !name || name.length > 200 || !profile || !(file instanceof File)) {
          return failure("Seleziona fornitore, tipo di listino e file validi.", 422);
        }
        if (file.size > MAX_PRICE_LIST_FILE_BYTES) return failure("Il file supera il limite di 20 MiB.", 413);
        if (!isAllowedPriceListFile(file)) return failure("Formato non supportato. Usa CSV, TXT, TSV, XLS, XLSX o XML.", 422);
        expectedSupplierId = supplierId;
        expectedFeedProfile = profile;
        const safeForm = new FormData();
        safeForm.set("supplier_id", supplierId);
        safeForm.set("name", name);
        safeForm.set("provider", profile.provider);
        safeForm.set("feed_role", profile.feed_role);
        safeForm.set("file", file, file.name);
        body = safeForm;
      }
    }

    const suffix = operation === "dashboard" ? "catalogs"
      : operation === "create-supplier" ? "catalogs/suppliers"
        : operation === "delete-supplier" ? `catalogs/suppliers/${resourceId}`
          : operation === "create-price-list" ? "catalogs/price-lists"
            : operation === "create-price-list-url" ? "catalogs/price-lists/url"
              : operation === "update-price-list-url" ? `catalogs/price-lists/${resourceId}/url`
                : operation === "refresh-price-list" ? `catalogs/price-lists/${resourceId}/refresh`
                  : operation === "job" ? `catalogs/price-lists/${resourceId}/jobs/${jobId}`
                    : `catalogs/price-lists/${resourceId}${operation === "detail" ? "?limit=200" : ""}`;
    const method = operation === "dashboard" || operation === "detail" || operation === "job" ? "GET"
      : operation === "delete-supplier" || operation === "delete-price-list" ? "DELETE" : "POST";
    const headers: Record<string, string> = { cookie, accept: "application/json" };
    if (contentType) headers["content-type"] = contentType;
    const deadline = AbortSignal.timeout(operation === "create-price-list" ? 55_000 : 25_000);
    const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
    const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/${suffix}`, {
      method, headers, body, cache: "no-store", redirect: "error", signal,
    }).catch(() => null);
    if (!upstream) return failure("Servizio catalogo momentaneamente non disponibile.", 503);
    if (!upstream.ok) return upstreamFailure(operation, upstream.status);

    if (operation === "dashboard") {
      const dashboard = readCatalogDashboard(await upstream.json().catch(() => null), sellerId);
      if (!dashboard) return failure("Risposta catalogo non valida.", 502);
      return NextResponse.json(dashboard, { headers: { "cache-control": "no-store" } });
    }
    if (operation === "detail") {
      const detail = readPriceListDetail(await upstream.json().catch(() => null), resourceId!);
      if (!detail) return failure("Anteprima listino non valida.", 502);
      return NextResponse.json(detail, { headers: { "cache-control": "no-store" } });
    }
    if (operation === "job") {
      const job = readCatalogFeedJobResponse(await upstream.json().catch(() => null), jobId!, resourceId);
      if (!job) return failure("Stato aggiornamento listino non valido.", 502);
      return NextResponse.json(job, { headers: { "cache-control": "no-store" } });
    }
    if (operation === "create-price-list-url" || operation === "refresh-price-list") {
      if (upstream.status !== 202) return failure("Risposta catalogo non valida.", 502);
      const mutation = readCatalogFeedMutation(
        await upstream.json().catch(() => null),
        operation === "refresh-price-list" ? resourceId : undefined,
      );
      if (!mutation) return failure("Risposta catalogo non valida.", 502);
      if (expectedSupplierId && mutation.price_list.supplier_id !== expectedSupplierId) {
        return failure("Risposta catalogo non valida.", 502);
      }
      if (expectedFeedProfile && (mutation.price_list.provider !== expectedFeedProfile.provider
        || mutation.price_list.feed_role !== expectedFeedProfile.feed_role)) {
        return failure("Risposta catalogo non valida.", 502);
      }
      return NextResponse.json(mutation, { status: 202, headers: { "cache-control": "no-store" } });
    }
    if (operation === "update-price-list-url") {
      if (upstream.status !== 200) return failure("Risposta catalogo non valida.", 502);
      const priceList = readCatalogPriceListMutation(await upstream.json().catch(() => null), resourceId!);
      if (!priceList || priceList.source_type !== "url") return failure("Risposta catalogo non valida.", 502);
      return NextResponse.json({ price_list: priceList }, { headers: { "cache-control": "no-store" } });
    }
    if (operation === "create-price-list") {
      const payload: unknown = await upstream.json().catch(() => null);
      const candidateId = payload && typeof payload === "object" && "price_list" in payload
        && payload.price_list && typeof payload.price_list === "object" && "id" in payload.price_list
        ? payload.price_list.id : null;
      if (!isUuid(candidateId)) return failure("Risposta catalogo non valida.", 502);
      const detail = readPriceListDetail(payload, candidateId);
      if (!detail || detail.price_list.supplier_id !== expectedSupplierId
        || detail.price_list.provider !== expectedFeedProfile?.provider
        || detail.price_list.feed_role !== expectedFeedProfile.feed_role) {
        return failure("Risposta catalogo non valida.", 502);
      }
      return NextResponse.json(detail, { status: 201, headers: { "cache-control": "no-store" } });
    }
    return NextResponse.json({ ok: true }, {
      status: operation === "create-supplier" ? 201 : 200,
      headers: { "cache-control": "no-store" },
    });
  } finally {
    releaseCatalogUpload(uploadLease);
  }
}
