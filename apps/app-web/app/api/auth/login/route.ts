import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "../../../lib/api-url";
import { isLoginSession, loginErrorMessage, readLoginInput } from "../../../lib/auth-login-contract";

const failure = (status: number) => NextResponse.json({ detail: loginErrorMessage(status) }, { status, headers: { "cache-control": "no-store" } });

function requestOrigin(request: NextRequest): string | null {
  const url = new URL(request.url);
  const host = request.headers.get("host");
  if (!host) return url.origin;
  if (/[\\/@?#\s,]/.test(host)) return null;
  try { return new URL(`${url.protocol}//${host}`).origin; } catch { return null; }
}

export async function POST(request: NextRequest) {
  const origin = request.headers.get("origin");
  if (origin && origin !== requestOrigin(request)) return failure(403);
  const input = readLoginInput(await request.json().catch(() => null));
  if (!input) return failure(422);
  const signal = AbortSignal.timeout(30000);
  try {
    // Exactly one password submission; redirects and retries are deliberately disabled.
    const upstream = await fetch(`${apiUrl}/v1/auth/login`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input), cache: "no-store", redirect: "error", signal });
    if (!upstream.ok) return failure([401, 403, 422, 429, 502, 503, 504].includes(upstream.status) ? upstream.status : 503);
    const value: unknown = await upstream.json().catch(() => null);
    if (signal.aborted) return failure(504);
    const setCookie = upstream.headers.get("set-cookie");
    if (!isLoginSession(value, input.realm) || !setCookie) return failure(502);
    const response = NextResponse.json({ authenticated: true, realm: input.realm }, { headers: { "cache-control": "no-store" } });
    response.headers.set("set-cookie", setCookie);
    return response;
  } catch {
    return failure(signal.aborted ? 504 : 503);
  }
}
