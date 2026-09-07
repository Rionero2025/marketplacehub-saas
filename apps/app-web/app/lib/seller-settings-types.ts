export type MarketplaceAccount = {
  id: string;
  marketplace: "kaufland";
  account_name: string;
  active: boolean;
  credentials_configured: boolean;
  client_key_masked: string;
};

export type SellerSettings = {
  seller_id: string;
  name: string;
  legal_name: string | null;
  email: string | null;
  can_manage: boolean;
  marketplace_accounts: MarketplaceAccount[];
};

export type SellerSettingsInput = Pick<SellerSettings, "name"> & { legal_name: string; email: string };
export type KauflandAccountInput = { account_name: string; client_key: string; secret_key: string };

export const isUuid = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(value);
const isObject = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const nullableString = (value: unknown): value is string | null => value === null || typeof value === "string";

/** Construct the public DTO explicitly: credentials and additional upstream data cannot cross the BFF. */
export function readSellerSettings(value: unknown, sellerId: string): SellerSettings | null {
  if (!isObject(value) || value.seller_id !== sellerId || typeof value.name !== "string" || !value.name.trim()
    || !nullableString(value.legal_name) || !nullableString(value.email)
    || typeof value.can_manage !== "boolean" || !Array.isArray(value.marketplace_accounts)) return null;
  const accounts: MarketplaceAccount[] = [];
  for (const account of value.marketplace_accounts) {
    if (!isObject(account) || !isUuid(account.id) || account.marketplace !== "kaufland"
      || typeof account.account_name !== "string" || typeof account.active !== "boolean"
      || typeof account.credentials_configured !== "boolean" || typeof account.client_key_masked !== "string"
      || !/^(?:—|•{8}[\s\S]{0,4})$/u.test(account.client_key_masked)
      || accounts.some((existing) => existing.id === account.id)) return null;
    accounts.push({ id: account.id, marketplace: "kaufland", account_name: account.account_name, active: account.active,
      credentials_configured: account.credentials_configured, client_key_masked: account.client_key_masked });
  }
  return { seller_id: sellerId, name: value.name, legal_name: value.legal_name, email: value.email,
    can_manage: value.can_manage, marketplace_accounts: accounts };
}

export function readSettingsInput(value: unknown): SellerSettingsInput | null {
  if (!isObject(value) || typeof value.name !== "string" || !value.name.trim()
    || typeof value.legal_name !== "string" || typeof value.email !== "string") return null;
  return { name: value.name.trim(), legal_name: value.legal_name.trim(), email: value.email.trim() };
}

export function readAccountInput(value: unknown): KauflandAccountInput | null {
  if (!isObject(value) || typeof value.account_name !== "string" || !value.account_name.trim()
    || typeof value.client_key !== "string" || typeof value.secret_key !== "string"
    || !(value.client_key.trim() || value.secret_key.trim())) return null;
  return { account_name: value.account_name.trim(), client_key: value.client_key.trim(), secret_key: value.secret_key.trim() };
}
