export type LoginRealm = "seller" | "agency" | "platform";
export type LoginInput = { login: string; password: string; realm: LoginRealm };
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
export const isLoginRealm = (value: unknown): value is LoginRealm => typeof value === "string" && ["seller", "agency", "platform"].includes(value);

export function readLoginInput(value: unknown): LoginInput | null {
  if (!object(value) || typeof value.login !== "string" || !value.login.trim() || value.login.length > 254
    || typeof value.password !== "string" || value.password.length < 1 || value.password.length > 1024 || !isLoginRealm(value.realm)) return null;
  return { login: value.login.trim(), password: value.password, realm: value.realm };
}

export function isLoginSession(value: unknown, realm: LoginRealm): boolean {
  return object(value) && value.realm === realm && typeof value.user_id === "string"
    && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value.user_id)
    && typeof value.login === "string" && Boolean(value.login.trim()) && typeof value.display_name === "string"
    && typeof value.expires_at === "string" && Number.isFinite(Date.parse(value.expires_at));
}

export function isLoginSuccess(value: unknown, realm: LoginRealm): boolean {
  return object(value) && value.authenticated === true && value.realm === realm;
}

export function loginErrorMessage(status: number): string {
  if (status === 401) return "Credenziali non valide.";
  if (status === 429) return "Troppi tentativi di accesso. Attendi qualche minuto prima di riprovare.";
  if (status === 422) return "Controlla email o username e password.";
  if (status === 403) return "Richiesta di accesso non consentita. Ricarica la pagina e riprova.";
  if (status === 502) return "Il servizio ha restituito una risposta non valida. Riprova tra poco.";
  if (status === 504) return "L’accesso non è stato confermato in tempo. Riprova tra poco.";
  return "Il servizio di accesso è momentaneamente non disponibile. Riprova tra poco.";
}
