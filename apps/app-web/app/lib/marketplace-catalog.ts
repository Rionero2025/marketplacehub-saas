import type { ConnectorCode } from "./marketplace-connections-types";

export type MarketplaceDirectoryEntry = { code: string; name: string; category: "Generalista" | "Elettronica" | "Casa e fai da te" | "Moda e lifestyle"; connector: ConnectorCode | null };
export const marketplaceCatalog: MarketplaceDirectoryEntry[] = [
  { code: "kaufland", name: "Kaufland", category: "Generalista", connector: "kaufland" },
  { code: "worten", name: "Worten", category: "Elettronica", connector: "worten" },
  { code: "amazon", name: "Amazon", category: "Generalista", connector: null },
  { code: "ebay", name: "eBay", category: "Generalista", connector: null },
  { code: "allegro", name: "Allegro", category: "Generalista", connector: null },
  { code: "cdiscount", name: "Cdiscount", category: "Generalista", connector: null },
  { code: "fnac", name: "Fnac", category: "Elettronica", connector: null },
  { code: "darty", name: "Darty", category: "Elettronica", connector: null },
  { code: "mediamarkt", name: "MediaMarkt", category: "Elettronica", connector: null },
  { code: "mediaworld", name: "MediaWorld", category: "Elettronica", connector: null },
  { code: "bigbang", name: "Big Bang", category: "Elettronica", connector: null },
  { code: "bol", name: "bol", category: "Generalista", connector: null },
  { code: "manomano", name: "ManoMano", category: "Casa e fai da te", connector: null },
  { code: "leroymerlin", name: "Leroy Merlin", category: "Casa e fai da te", connector: null },
  { code: "carrefour", name: "Carrefour", category: "Generalista", connector: null },
  { code: "rakuten", name: "Rakuten", category: "Generalista", connector: null },
  { code: "aliexpress", name: "AliExpress", category: "Generalista", connector: null },
  { code: "temu", name: "Temu", category: "Generalista", connector: null },
  { code: "tiktokshop", name: "TikTok Shop", category: "Generalista", connector: null },
  { code: "etsy", name: "Etsy", category: "Moda e lifestyle", connector: null },
  { code: "zalando", name: "Zalando", category: "Moda e lifestyle", connector: null },
  { code: "otto", name: "OTTO", category: "Generalista", connector: null },
  { code: "decathlon", name: "Decathlon", category: "Moda e lifestyle", connector: null },
  { code: "eprice", name: "ePRICE", category: "Elettronica", connector: null },
  { code: "conforama", name: "Conforama", category: "Casa e fai da te", connector: null },
  { code: "pccomponentes", name: "PcComponentes", category: "Elettronica", connector: null },
  { code: "emag", name: "eMAG", category: "Generalista", connector: null },
  { code: "fruugo", name: "Fruugo", category: "Generalista", connector: null },
];
export function filterMarketplaces(query: string, availability: string, category: string) {
  const needle = query.trim().toLocaleLowerCase("it");
  return marketplaceCatalog.filter((entry) => entry.name.toLocaleLowerCase("it").includes(needle)
    && (availability === "all" || (availability === "available" ? Boolean(entry.connector) : !entry.connector))
    && (category === "all" || entry.category === category));
}
