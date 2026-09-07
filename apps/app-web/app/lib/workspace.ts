import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { apiUrl } from "./api-url";
import type { Realm } from "./session";
import { isWorkspace, type Workspace } from "./workspace-types";

export type WorkspaceResult = { workspace: Workspace; error?: never } | { workspace?: never; error: string };

export async function fetchWorkspace(realm: Realm, loginPath: string): Promise<WorkspaceResult> {
  const cookieHeader = (await cookies()).toString();
  const response = await fetch(`${apiUrl}/v1/workspace`, {
    headers: { cookie: cookieHeader }, cache: "no-store", signal: AbortSignal.timeout(15000),
  }).catch(() => null);

  if (response?.status === 401) redirect(loginPath);
  if (response?.status === 403) return { error: "Non hai accesso a questa area. Contatta l’amministratore della tua organizzazione." };
  if (!response?.ok) return { error: "Non riusciamo a caricare i tuoi negozi in questo momento. Riprova tra poco." };

  const value: unknown = await response.json().catch(() => null);
  if (!isWorkspace(value) || value.realm !== realm) {
    return { error: "Non riusciamo a caricare i tuoi negozi in questo momento. Riprova tra poco." };
  }
  return { workspace: value };
}
