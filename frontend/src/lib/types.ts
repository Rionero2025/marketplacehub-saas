export type User = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  permissions: string[];
  seller_ids: number[] | null;
  expires_at: number;
  tenant_ids: number[];
  active_tenant_id: number;
  active_tenant_name: string;
  active_tenant_type: string;
  tenant_role: string;
};

export type Tenant = {
  id: number;
  name: string;
  slug: string;
  tenant_type: string;
  status: string;
  plan_code: string;
  access_mode: string;
  role: string;
  active: boolean;
};

export type Seller = { id: number; name: string; legal_name: string; active: boolean };
export type MarketplaceAccount = { id: number; seller_id: number; marketplace: string; account_name: string; active: boolean };
export type Job = {
  job_id: string; kind: string; status: string; seller_id: number | null; tenant_id: number | null;
  progress_current: number; progress_total: number; progress_pct: number; message: string;
  result: Record<string, unknown>; error: string; created_at: string; started_at: string; finished_at: string;
};
export type Entitlements = {
  tenant_id: number; plan_code: string; plan_name: string; status: string; active: boolean;
  monthly_price_cents: number; currency: string; features: Record<string, boolean>;
  limits: Record<string, number | null>; usage: Record<string, number>; remaining: Record<string, number | null>;
};
