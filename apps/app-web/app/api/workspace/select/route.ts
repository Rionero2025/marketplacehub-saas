import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "../../../lib/api-url";
import { isWorkspace } from "../../../lib/workspace-types";

function failure(detail: string, status: number) {
  return NextResponse.json({ detail }, { status, headers: { "cache-control": "no-store" } });
}

export async function POST(request: NextRequest) {
  const body: unknown = await request.json().catch(() => null);
  if (typeof body !== "object" || body === null || !("seller_id" in body)
    || typeof body.seller_id !== "string" || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(body.seller_id)) {
    return failure("Seleziona un negozio valido.", 422);
  }

  const upstream = await fetch(`${apiUrl}/v1/workspace/select`, {
    method: "POST",
    headers: { "content-type": "application/json", cookie: request.headers.get("cookie") ?? "" },
    body: JSON.stringify({ seller_id: body.seller_id }), cache: "no-store", signal: AbortSignal.timeout(15000),
  }).catch(() => null);

  if (!upstream) return failure("Impossibile cambiare negozio. Riprova tra poco.", 503);
  if (upstream.status === 401) return failure("La sessione è scaduta. Accedi di nuovo.", 401);
  if (upstream.status === 403 || upstream.status === 404) return failure("Questo negozio non è disponibile per il tuo account. Aggiorna l’elenco e riprova.", upstream.status);
  if (!upstream.ok) return failure("Impossibile cambiare negozio. Riprova tra poco.", upstream.status >= 500 ? 503 : 422);
  const value: unknown = await upstream.json().catch(() => null);
  if (!isWorkspace(value) || value.active_seller?.id !== body.seller_id) return failure("Impossibile confermare il negozio selezionato. Aggiorna la pagina.", 502);

  const response = NextResponse.json(value, { headers: { "cache-control": "no-store" } });
  const cookie = upstream.headers.get("set-cookie");
  if (cookie) response.headers.set("set-cookie", cookie);
  return response;
}
