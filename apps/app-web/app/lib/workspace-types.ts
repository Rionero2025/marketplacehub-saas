import type { Realm } from "./session";

export type WorkspaceOrganization = {
  id: string;
  name: string;
  kind: "SELLER" | "AGENCY" | "PLATFORM";
  role_code: string;
  role_label: string;
  read_only: boolean;
  permissions: string[];
};

export type WorkspaceSeller = {
  id: string;
  organization_id: string;
  organization_name: string;
  name: string;
  legal_name: string | null;
  email: string | null;
  role_code: string;
  role_label: string;
  permissions: string[];
  write_permissions: string[];
};

export type Workspace = {
  realm: Realm;
  organizations: WorkspaceOrganization[];
  sellers: WorkspaceSeller[];
  active_seller: WorkspaceSeller | null;
  permission_labels: Record<string, string>;
};

type ObjectValue = Record<string, unknown>;
const isObject = (value: unknown): value is ObjectValue => typeof value === "object" && value !== null && !Array.isArray(value);
const isStrings = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");
const isNullableString = (value: unknown): value is string | null => value === null || typeof value === "string";

function isOrganization(value: unknown): value is WorkspaceOrganization {
  return isObject(value) && typeof value.id === "string" && typeof value.name === "string"
    && ["SELLER", "AGENCY", "PLATFORM"].includes(String(value.kind))
    && typeof value.role_code === "string" && typeof value.role_label === "string"
    && typeof value.read_only === "boolean" && isStrings(value.permissions);
}

function isSeller(value: unknown): value is WorkspaceSeller {
  return isObject(value) && typeof value.id === "string" && typeof value.organization_id === "string"
    && typeof value.organization_name === "string" && typeof value.name === "string"
    && isNullableString(value.legal_name) && isNullableString(value.email)
    && typeof value.role_code === "string" && typeof value.role_label === "string"
    && isStrings(value.permissions) && isStrings(value.write_permissions);
}

export function isWorkspace(value: unknown): value is Workspace {
  return isObject(value) && ["seller", "agency", "platform"].includes(String(value.realm))
    && Array.isArray(value.organizations) && value.organizations.every(isOrganization)
    && Array.isArray(value.sellers) && value.sellers.every(isSeller)
    && (value.active_seller === null || isSeller(value.active_seller))
    && isObject(value.permission_labels)
    && Object.values(value.permission_labels).every((label) => typeof label === "string")
    && (value.active_seller === null || value.sellers.some((seller) => seller.id === (value.active_seller as WorkspaceSeller).id));
}
