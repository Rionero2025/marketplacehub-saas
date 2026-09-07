export type DashboardIconName = "home" | "store" | "building" | "shield" | "menu" | "close" | "exit" | "chevron" | "refresh" | "check" | "users" | "arrow" | "plug" | "search";

export function DashboardIcon({ name, size = 18 }: { name: DashboardIconName; size?: number }) {
  const paths: Record<DashboardIconName, ReactNode> = {
    plug: <><path d="M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0Zm6 9v4" /></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    home: <><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z" /></>,
    store: <><path d="M4 10v11h16V10M3 10l2-7h14l2 7M3 10a3 3 0 0 0 5 2 3 3 0 0 0 4 0 3 3 0 0 0 4 0 3 3 0 0 0 5-2M9 21v-6h6v6" /></>,
    building: <><path d="M4 21V3h11v18M15 9h5v12M2 21h20M8 7h3M8 11h3M8 15h3M8 21v-2h3v2M17 13h1M17 17h1" /></>,
    shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z" /><path d="m8 12 3 3 5-6" /></>,
    menu: <><path d="M4 6h16M4 12h16M4 18h16" /></>,
    close: <><path d="m6 6 12 12M18 6 6 18" /></>,
    exit: <><path d="M9 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h5M9 12h12m-4-4 4 4-4 4" /></>,
    chevron: <><path d="m9 5 7 7-7 7" /></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6 6a8 8 0 0 1 13 2l1 4M4 12l1 4a8 8 0 0 0 13 2" /></>,
    check: <><path d="m5 12 4 4L19 6" /></>,
    users: <><circle cx="9" cy="7" r="3" /><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6M18 13a5 5 0 0 1 3 5v3" /></>,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6" /></>,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{paths[name]}</svg>;
}
import type { ReactNode } from "react";
