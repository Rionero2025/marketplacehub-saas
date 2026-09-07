import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { isUuid, readAccountInput, readSellerSettings, readSettingsInput } from "./seller-settings-types";

const failure = (detail: string, status: number) => NextResponse.json({ detail }, { status, headers: { "cache-control": "no-store" } });

function requestOrigin(request: NextRequest): string | null {
  // Next constructs request.url with the listening hostname behind a proxy.
  // The HTTP Host is the browser's target; do not accept a client-supplied forwarded host.
  const url = new URL(request.url);
  const host = request.headers.get("host");
  if (!host) return url.origin;
  if (/[\\/@?#\s,]/.test(host)) return null;
  try { return new URL(`${url.protocol}//${host}`).origin; } catch { return null; }
}

export async function sellerSettingsProxy(request: NextRequest, sellerId: string, operation: "read" | "save" | "add-account" | "delete-account", accountId?: string) {
  if (!isUuid(sellerId) || (operation === "delete-account" && !isUuid(accountId))) return failure("Negozio o account non valido.", 422);
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (operation !== "read") {
    const origin = request.headers.get("origin");
    if (origin && origin !== requestOrigin(request)) return failure("Richiesta non consentita.", 403);
  }
  let body: string | undefined;
  if (operation === "save" || operation === "add-account") {
    const value: unknown = await request.json().catch(() => null);
    const input = operation === "save" ? readSettingsInput(value) : readAccountInput(value);
    if (!input) return failure(operation === "save"
      ? "Controlla i dati del negozio: il nome è obbligatorio."
      : "Inserisci il nome dell’account e almeno una chiave API.", 422);
    body = JSON.stringify(input);
  }
  if (operation === "delete-account") {
    const value: unknown = await request.json().catch(() => null);
    if (typeof value !== "object" || value === null || !("confirmation" in value) || value.confirmation !== "ELIMINA") {
      return failure("Scrivi ELIMINA per confermare l’eliminazione dell’account.", 422);
    }
    body = JSON.stringify({ confirmation: "ELIMINA" });
  }
  const suffix = operation === "read" || operation === "save" ? "settings"
    : `kaufland-accounts${operation === "delete-account" ? `/${accountId}` : ""}`;
  const method = { read: "GET", save: "PUT", "add-account": "POST", "delete-account": "DELETE" }[operation];
  const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/${suffix}`, {
    method, headers: { cookie, "content-type": "application/json" }, body,
    cache: "no-store", signal: AbortSignal.timeout(15000),
  }).catch(() => null);
  if (!upstream) return failure("Servizio momentaneamente non disponibile. Riprova tra poco.", 503);
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (upstream.status === 403 || upstream.status === 404) return failure("Non hai accesso a questi dati o l’account non è più disponibile. Aggiorna i dati.", upstream.status);
  if (!upstream.ok) return failure("Operazione non completata. Controlla i dati e riprova.", upstream.status >= 500 ? 503 : 422);
  const value = readSellerSettings(await upstream.json().catch(() => null), sellerId);
  if (!value) return failure("Non è possibile confermare l’operazione. Aggiorna i dati prima di riprovare.", 502);
  return NextResponse.json(value, { headers: { "cache-control": "no-store" } });
}
