import type { DashboardIconName } from "../components/DashboardIcon";

export type SellerPage = "overview" | "orders" | "suppliers" | "price-lists" | "work-lists" | "marketplaces" | "marketplace-accounts" | "store" | "organizations" | "permissions";
type Section = { page: SellerPage; href: string; label: string; icon: DashboardIconName };
type Area = { id: string; label: string; icon: DashboardIconName; sections: Section[] };

export const sellerAreas: Area[] = [
  { id: "home", label: "Panoramica", icon: "home", sections: [
    { page: "overview", href: "/seller", label: "Il tuo workspace", icon: "home" },
  ] },
  { id: "orders", label: "Ordini", icon: "orders", sections: [
    { page: "orders", href: "/seller/orders", label: "Elenco ordini", icon: "orders" },
  ] },
  { id: "catalog", label: "Catalogo", icon: "catalog", sections: [
    { page: "suppliers", href: "/seller/catalog/suppliers", label: "Fornitori", icon: "supplier" },
    { page: "price-lists", href: "/seller/catalog/price-lists", label: "Listini", icon: "file" },
    { page: "work-lists", href: "/seller/catalog/work", label: "Lavora sui listini", icon: "catalog" },
  ] },
  { id: "marketplaces", label: "Marketplace", icon: "plug", sections: [
    { page: "marketplaces", href: "/seller/marketplaces", label: "Collega marketplace", icon: "plug" },
    { page: "marketplace-accounts", href: "/seller/marketplaces/accounts", label: "Account collegati", icon: "store" },
  ] },
  { id: "settings", label: "Impostazioni", icon: "settings", sections: [
    { page: "store", href: "/seller/settings/store", label: "Negozio", icon: "store" },
    { page: "organizations", href: "/seller/settings/organizations", label: "Organizzazioni", icon: "building" },
    { page: "permissions", href: "/seller/settings/permissions", label: "Autorizzazioni", icon: "shield" },
  ] },
];

export const legacySellerSections: Record<string, string> = {
  "#workspace-seller": "/seller/settings/store",
  "#workspace-organizations": "/seller/settings/organizations",
  "#workspace-permissions": "/seller/settings/permissions",
};
