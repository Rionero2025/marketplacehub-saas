import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { isUuid } from "./seller-settings-types";
import { readOrderSelection, readOrdersActionInput } from "./orders-types";

const failure = (detail: string, status: number) => NextResponse.json({ detail }, {
  status, headers: { "cache-control": "no-store" },
});

function sameOrigin(request: NextRequest): boolean {
  if (request.headers.get("sec-fetch-site") === "cross-site") return false;
  const origin = request.headers.get("origin");
  if (!origin) return true;
  const url = new URL(request.url), host = request.headers.get("host");
  if (host && /[\\/@?#\s,]/.test(host)) return false;
  try { return origin === (host ? new URL(`${url.protocol}//${host}`).origin : url.origin); }
  catch { return false; }
}

export async function ordersActionsProxy(request: NextRequest, sellerId: string, operation: "selection" | "export") {
  if (!isUuid(sellerId)) return failure("Riferimento non valido.", 422);
  const cookie = request.headers.get("cookie");
  if (!cookie) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (!sameOrigin(request)) return failure("Richiesta non consentita.", 403);
  const input = readOrdersActionInput(await request.json().catch(() => null), operation);
  if (!input) return failure("Controlla i filtri e la selezione degli ordini.", 422);

  const deadline = AbortSignal.timeout(operation === "export" ? 90000 : 25000);
  const signal = request.signal ? AbortSignal.any([request.signal, deadline]) : deadline;
  const upstream = await fetch(`${apiUrl}/v1/sellers/${sellerId}/orders/${operation}`, {
    method: "POST", headers: { cookie, "content-type": "application/json" },
    body: JSON.stringify(input), cache: "no-store", redirect: "error", signal,
  }).catch(() => null);
  if (!upstream) return failure(operation === "export"
    ? "Esportazione non completata. Riprova."
    : "Selezione non confermata. Aggiorna gli ordini prima di riprovare.", 503);
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (upstream.status === 403 || upstream.status === 404) return failure(
    "Non hai accesso a questi ordini o la selezione non è più disponibile.", upstream.status);
  if (!upstream.ok) return failure(upstream.status === 422
    ? "Controlla i filtri e aggiorna la selezione degli ordini."
    : "Ordini temporaneamente non disponibili. Riprova.", upstream.status >= 500 ? 503 : 422);

  if (operation === "export") {
    if (!/^text\/csv(?:\s*;|$)/i.test(upstream.headers.get("content-type") ?? "") || !upstream.body) {
      return failure("Il file ricevuto non è un CSV valido. Riprova.", 502);
    }
    // Keep the export streamed: the BFF never loads the full order archive in memory.
    const kind = "kind" in input && input.kind === "filtered" ? "filtrati" : "selezionati";
    return new Response(upstream.body, { headers: {
      "content-type": "text/csv; charset=utf-8",
      "content-disposition": `attachment; filename="ordini_${kind}_${input.environment}.csv"`,
      "cache-control": "no-store", "x-content-type-options": "nosniff",
    } });
  }

  const raw: unknown = await upstream.json().catch(() => null);
  const expectedPurpose = "purpose" in input ? input.purpose : "orders";
  const selection = readOrderSelection(raw && typeof raw === "object" && "selection" in raw ? raw.selection : null, expectedPurpose);
  if (!selection || selection.id !== input.selection_id) return failure(
    "Selezione non confermata. Aggiorna gli ordini.", 502);
  return NextResponse.json({ selection }, { headers: { "cache-control": "no-store" } });
}
