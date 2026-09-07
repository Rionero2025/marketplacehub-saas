import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export type Realm = "seller" | "agency" | "platform";
export type Session = { user_id: string; login: string; display_name: string; realm: Realm; expires_at: string };
const configuredApiUrl =
  process.env.INTERNAL_API_URL ??
  process.env.MARKETPLACE_HUB_API_HOSTPORT ??
  "http://localhost:8000";
const apiUrl = configuredApiUrl.includes("://") ? configuredApiUrl : `http://${configuredApiUrl}`;

export async function requireRealm(realm: Realm, loginPath: string): Promise<Session> {
  const cookieHeader = (await cookies()).toString();
  const response = await fetch(`${apiUrl}/v1/auth/session`, { headers: { cookie: cookieHeader }, cache: "no-store" }).catch(() => null);
  if (!response?.ok) redirect(loginPath);
  const session = (await response.json()) as Session;
  if (session.realm !== realm) redirect(loginPath);
  return session;
}
