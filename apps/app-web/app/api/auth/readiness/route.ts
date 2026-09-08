import { NextResponse } from "next/server";
import { apiUrl } from "../../../lib/api-url";

export async function GET() {
  // Render free instances can need well over ten seconds to wake. Keep this
  // read-only request connected long enough for the cold start to complete.
  const signal = AbortSignal.timeout(60000);
  let ready = false;
  try {
    const upstream = await fetch(`${apiUrl}/health/ready`, { method: "GET", headers: { accept: "application/json" }, cache: "no-store", redirect: "error", signal });
    const value: unknown = await upstream.json().catch(() => null);
    ready = upstream.status === 200 && typeof value === "object" && value !== null && "status" in value && value.status === "ok";
  } catch { /* Public readiness reveals no infrastructure or upstream error details. */ }
  return NextResponse.json({ ready }, { status: ready ? 200 : 503, headers: { "cache-control": "no-store" } });
}
