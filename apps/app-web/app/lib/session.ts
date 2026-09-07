import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { apiUrl } from "./api-url";

export type Realm = "seller" | "agency" | "platform";
export type Session = { user_id: string; login: string; display_name: string; realm: Realm; expires_at: string };

function isSession(value: unknown): value is Session {
  if (typeof value !== "object" || value === null) return false;
  const session = value as Record<string, unknown>;
  return typeof session.user_id === "string" && typeof session.login === "string"
    && typeof session.display_name === "string" && typeof session.expires_at === "string"
    && ["seller", "agency", "platform"].includes(String(session.realm));
}

export async function requireRealm(realm: Realm, loginPath: string): Promise<Session | null> {
  const cookieHeader = (await cookies()).toString();
  const response = await fetch(`${apiUrl}/v1/auth/session`, {
    headers: { cookie: cookieHeader }, cache: "no-store", signal: AbortSignal.timeout(15000),
  }).catch(() => null);
  if (response?.status === 401 || response?.status === 403) redirect(loginPath);
  if (!response?.ok) return null;
  const session: unknown = await response.json().catch(() => null);
  if (!isSession(session)) return null;
  if (session.realm !== realm) redirect(loginPath);
  return session;
}
