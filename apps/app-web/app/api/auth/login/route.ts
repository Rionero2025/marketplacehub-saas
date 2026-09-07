import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "../../../lib/api-url";

export async function POST(request: NextRequest) {
  const upstream = await fetch(`${apiUrl}/v1/auth/login`, { method: "POST", headers: { "content-type": "application/json" }, body: await request.text(), cache: "no-store" }).catch(() => null);
  if (!upstream) return NextResponse.json({ detail: "Servizio momentaneamente non disponibile." }, { status: 503 });
  const response = new NextResponse(await upstream.text(), { status: upstream.status, headers: { "content-type": upstream.headers.get("content-type") ?? "application/json", "cache-control": "no-store" } });
  const setCookie = upstream.headers.get("set-cookie");
  if (setCookie) response.headers.set("set-cookie", setCookie);
  return response;
}
