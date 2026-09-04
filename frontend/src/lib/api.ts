export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path.startsWith("/api/") ? path : `/api/v1${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    cache: "no-store",
  });
  if (response.status === 204) return undefined as T;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(item => item.msg || "Parametro non valido").join("; ")
      : detail?.code === "PLAN_ENTITLEMENT_REQUIRED" ? "Il piano attuale non abilita questa funzione."
      : `Impossibile completare la richiesta (HTTP ${response.status}).`;
    throw new ApiError(response.status, message);
  }
  return body as T;
}
