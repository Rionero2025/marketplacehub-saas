import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { isUuid } from "./seller-settings-types";
import { ordersQueryString, readOrderItem, readOrderJob, readOrdersList, readOrdersQuery, readOrdersSyncInput } from "./orders-types";

const failure = (detail: string, status: number) => NextResponse.json({ detail }, { status, headers: { "cache-control": "no-store" } });
function requestOrigin(request: NextRequest): string | null {
  const url = new URL(request.url), host = request.headers.get("host");
  if (!host) return url.origin;
  if (/[\\/@?#\s,]/.test(host)) return null;
  try { return new URL(`${url.protocol}//${host}`).origin; } catch { return null; }
}
export async function ordersProxy(request: NextRequest, sellerId: string, operation: "list" | "sync" | "job" | "detail", id?: string) {
  if (!isUuid(sellerId) || ((operation === "job" || operation === "detail") && !isUuid(id))) return failure("Riferimento non valido.", 422);
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  let body: string | undefined;
  const params = new URL(request.url).searchParams;
  const query = operation === "list" || operation === "detail" ? readOrdersQuery(params) : null;
  if ((operation === "list" || operation === "detail") && !query) return failure("Controlla l’account, il periodo e i filtri.", 422);
  let syncInput = null;
  if (operation === "sync") {
    const origin = request.headers.get("origin");
    if (origin && origin !== requestOrigin(request)) return failure("Richiesta non consentita.", 403);
    syncInput = readOrdersSyncInput(await request.json().catch(() => null));
    if (!syncInput) return failure("Controlla account e impostazioni della sincronizzazione.", 422);
    body = JSON.stringify(syncInput);
  }
  const suffix = operation === "list" ? `?${ordersQueryString(query!)}` : operation === "sync" ? "/sync"
    : operation === "job" ? `/jobs/${id}` : `/${id}?${new URLSearchParams({ account_id: query!.account_id, environment: query!.environment })}`;
  const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/orders${suffix}`, { method: operation === "sync" ? "POST" : "GET", headers: { cookie, "content-type": "application/json" }, body, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(20000) }).catch(() => null);
  if (!upstream) return failure("Operazione non confermata. Aggiorna i dati prima di riprovare.", 503);
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (upstream.status === 403 || upstream.status === 404) return failure("Non hai accesso a questi ordini o l’account non è più disponibile.", upstream.status);
  if (!upstream.ok) return failure(upstream.status === 422 ? "Controlla i filtri e verifica che il marketplace sia collegato." : "Ordini temporaneamente non disponibili. Aggiorna i dati prima di riprovare.", upstream.status >= 500 ? 503 : 422);
  const raw: unknown = await upstream.json().catch(() => null);
  if (operation === "list") {
    const value = readOrdersList(raw, sellerId, query!);
    return value ? NextResponse.json(value, { headers: { "cache-control": "no-store" } }) : failure("Risposta non valida. Aggiorna i dati.", 502);
  }
  if (typeof raw !== "object" || raw === null) return failure("Risposta non valida. Aggiorna i dati.", 502);
  if (operation === "detail") {
    const item = readOrderItem("item" in raw ? raw.item : null);
    return item && item.id === id ? NextResponse.json({ item }, { headers: { "cache-control": "no-store" } }) : failure("Dettaglio non valido. Aggiorna i dati.", 502);
  }
  const job = readOrderJob("job" in raw ? raw.job : null, syncInput ?? undefined);
  if (!job || (operation === "job" && job.id !== id)) return failure("Stato non confermato. Aggiorna i dati prima di riprovare.", 502);
  return NextResponse.json({ job }, { status: operation === "sync" ? 202 : 200, headers: { "cache-control": "no-store" } });
}
