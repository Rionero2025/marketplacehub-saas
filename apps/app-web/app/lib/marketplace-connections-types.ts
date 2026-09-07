import { isUuid } from "./seller-settings-types";

export const WORTEN_API_URL = "https://marketplace.worten.pt/api";
export type ConnectorCode = "kaufland" | "worten";
export type MarketplaceConnection = {
  id: string; marketplace: string; account_name: string; active: boolean;
  connection_status: "unverified" | "connected" | "error";
  last_checked_at: string | null; public_name: string | null; external_shop_id: string | null;
  storefronts: string[]; credential_mask: string; error_code: string | null;
};
export type MarketplaceConnections = { seller_id: string; can_manage: boolean; accounts: MarketplaceConnection[] };
export type ConnectionInput = { marketplace: ConnectorCode; account_name: string; credentials: { client_key?: string; secret_key?: string; api_key?: string; shop_id?: string; api_url?: string } };

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const nullableString = (value: unknown): value is string | null => value === null || typeof value === "string";
const nonempty = (value: unknown): value is string => typeof value === "string" && Boolean(value.trim());
export const connectionErrors: Record<string, string> = {
  invalid_credentials: "Il marketplace non riconosce le credenziali. Controlla le chiavi API.",
  permission_denied: "Le credenziali non hanno i permessi richiesti sul marketplace.",
  invalid_configuration: "Controlla la configurazione e l’identificativo del negozio.",
  unexpected_response: "Il marketplace ha restituito una risposta non riconosciuta. Riprova più tardi.",
  upstream_unavailable: "Il marketplace non è disponibile in questo momento. Riprova più tardi.",
  timeout: "Il marketplace non ha risposto in tempo. Aggiorna i dati prima di riprovare.",
  credentials_unavailable: "Le credenziali salvate non sono disponibili per la verifica.",
};

export function readConnections(value: unknown, sellerId: string): MarketplaceConnections | null {
  if (!object(value) || value.seller_id !== sellerId || typeof value.can_manage !== "boolean" || !Array.isArray(value.accounts)) return null;
  const accounts: MarketplaceConnection[] = [];
  for (const account of value.accounts) {
    if (!object(account) || !isUuid(account.id) || !nonempty(account.marketplace) || !nonempty(account.account_name)
      || typeof account.active !== "boolean" || !["unverified", "connected", "error"].includes(String(account.connection_status))
      || !nullableString(account.last_checked_at) || (account.last_checked_at !== null && !Number.isFinite(Date.parse(account.last_checked_at)))
      || !nullableString(account.public_name) || !nullableString(account.external_shop_id)
      || !Array.isArray(account.storefronts) || !account.storefronts.every((item) => typeof item === "string")
      || typeof account.credential_mask !== "string" || !/^(?:—|•{8}[\s\S]{0,4})$/u.test(account.credential_mask)
      || !(account.error_code === null || (typeof account.error_code === "string" && Object.hasOwn(connectionErrors, account.error_code)))
      || accounts.some((existing) => existing.id === account.id)) return null;
    accounts.push({ id: account.id, marketplace: account.marketplace, account_name: account.account_name, active: account.active,
      connection_status: account.connection_status as MarketplaceConnection["connection_status"], last_checked_at: account.last_checked_at,
      public_name: account.public_name, external_shop_id: account.external_shop_id, storefronts: [...account.storefronts] as string[],
      credential_mask: account.credential_mask, error_code: account.error_code as string | null });
  }
  return { seller_id: sellerId, can_manage: value.can_manage, accounts };
}

export function readConnectionInput(value: unknown): ConnectionInput | null {
  if (!object(value) || !nonempty(value.account_name) || !object(value.credentials)) return null;
  const credentials = value.credentials;
  if (value.marketplace === "kaufland" && nonempty(credentials.client_key) && nonempty(credentials.secret_key)) {
    return { marketplace: "kaufland", account_name: value.account_name.trim(), credentials: { client_key: credentials.client_key.trim(), secret_key: credentials.secret_key.trim() } };
  }
  if (value.marketplace === "worten" && nonempty(credentials.api_key) && nonempty(credentials.shop_id)
    && (credentials.api_url === undefined || credentials.api_url === WORTEN_API_URL)) {
    return { marketplace: "worten", account_name: value.account_name.trim(), credentials: { api_key: credentials.api_key.trim(), shop_id: credentials.shop_id.trim(), api_url: WORTEN_API_URL } };
  }
  return null;
}

export function safeConnectionError(value: unknown): string | null {
  if (!object(value)) return null;
  const detail = object(value.detail) ? value.detail.code : value.error_code;
  return typeof detail === "string" && Object.hasOwn(connectionErrors, detail) ? detail : null;
}
