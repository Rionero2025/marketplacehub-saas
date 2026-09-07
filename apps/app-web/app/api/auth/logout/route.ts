import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "../../../lib/api-url";

export async function POST(request: NextRequest) {
  const upstream = await fetch(`${apiUrl}/v1/auth/logout`, {
    method: "POST", headers: { cookie: request.headers.get("cookie") ?? "" },
    cache: "no-store", signal: AbortSignal.timeout(15000),
  }).catch(() => null);
  if (!upstream?.ok) return NextResponse.json({ detail: "Servizio momentaneamente non disponibile." }, { status: 503, headers: { "cache-control": "no-store" } });
  const response = new NextResponse(null, { status: upstream.status, headers: { "cache-control": "no-store" } });
  const setCookie = upstream.headers.get("set-cookie");
  if (setCookie) response.headers.set("set-cookie", setCookie);
  return response;
}
