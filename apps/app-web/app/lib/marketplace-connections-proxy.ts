import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { isUuid } from "./seller-settings-types";
import { connectionErrors, readConnectionInput, readConnections, safeConnectionError } from "./marketplace-connections-types";

const failure = (detail: string, status: number, error_code?: string) => NextResponse.json({ detail, ...(error_code ? { error_code } : {}) }, { status, headers: { "cache-control": "no-store" } });
function requestOrigin(request: NextRequest): string | null {
  const url = new URL(request.url);
  const host = request.headers.get("host");
  if (!host) return url.origin;
  if (/[\\/@?#\s,]/.test(host)) return null;
  try { return new URL(`${url.protocol}//${host}`).origin; } catch { return null; }
}

export async function marketplaceConnectionsProxy(request: NextRequest, sellerId: string, operation: "read" | "connect" | "verify" | "delete", accountId?: string) {
  if (!isUuid(sellerId) || ((operation === "verify" || operation === "delete") && !isUuid(accountId))) return failure("Negozio o account non valido.", 422);
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (operation !== "read") {
    const origin = request.headers.get("origin");
    if (origin && origin !== requestOrigin(request)) return failure("Richiesta non consentita.", 403);
  }
  let body: string | undefined;
  if (operation === "connect") {
    const input = readConnectionInput(await request.json().catch(() => null));
    if (!input) return failure("Controlla il marketplace e compila tutti i campi richiesti.", 422);
    body = JSON.stringify(input);
  }
  if (operation === "delete") {
    const input: unknown = await request.json().catch(() => null);
    if (!input || typeof input !== "object" || !("confirmation" in input) || input.confirmation !== "ELIMINA") return failure("Scrivi ELIMINA per confermare l’eliminazione.", 422);
    body = JSON.stringify({ confirmation: "ELIMINA" });
  }
  const suffix = `marketplace-connections${accountId ? `/${accountId}` : ""}${operation === "verify" ? "/verify" : ""}`;
  const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/${suffix}`, {
    method: operation === "read" ? "GET" : operation === "delete" ? "DELETE" : "POST",
    headers: { cookie, "content-type": "application/json" }, body, cache: "no-store", signal: AbortSignal.timeout(35000),
  }).catch(() => null);
  if (!upstream) return failure("Operazione non confermata. Aggiorna i dati prima di riprovare.", 503);
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (upstream.status === 403 || upstream.status === 404) return failure("Non hai accesso a questi dati o l’account non è più disponibile.", upstream.status);
  if (!upstream.ok) {
    const code = safeConnectionError(await upstream.json().catch(() => null));
    return failure(code ? connectionErrors[code] : upstream.status === 409 ? "Un account con questo nome esiste già per questo marketplace." : "Operazione non completata. Controlla i dati e riprova.",
      upstream.status === 409 ? 409 : upstream.status >= 500 ? 503 : 422, code ?? undefined);
  }
  const value = readConnections(await upstream.json().catch(() => null), sellerId);
  if (!value) return failure("Risposta non valida. Aggiorna i dati prima di riprovare.", 502);
  return NextResponse.json(value, { status: operation === "connect" ? 201 : 200, headers: { "cache-control": "no-store" } });
}
