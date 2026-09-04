"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Seller, Tenant, User } from "@/lib/types";

type Workspace = {
  user: User | null; tenants: Tenant[]; sellers: Seller[]; seller: Seller | null; loading: boolean;
  error: string;
  setSellerId: (id: number) => void; switchTenant: (id: number) => Promise<void>; refresh: () => Promise<void>;
};
const Ctx = createContext<Workspace | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null); const [tenants, setTenants] = useState<Tenant[]>([]);
  const [sellers, setSellers] = useState<Seller[]>([]); const [sellerId, setSellerIdState] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestId = useRef(0);

  const refresh = useCallback(async () => {
    const id = ++requestId.current;
    setError("");
    try {
      const me = await api<User>("/auth/me");
      if (id !== requestId.current) return;
      setUser(me);
      const [t, s] = await Promise.all([api<Tenant[]>("/tenants"), api<Seller[]>("/sellers")]);
      if (id !== requestId.current) return;
      setTenants(t); setSellers(s);
      let saved = 0;
      try { saved = Number(localStorage.getItem(`mh:seller:${me.active_tenant_id}`) || 0); } catch { /* Storage may be disabled in the browser. */ }
      setSellerIdState(s.some(x => x.id === saved) ? saved : (s[0]?.id || 0));
    } catch (e) {
      if (id !== requestId.current) return;
      setSellers([]); setSellerIdState(0);
      setError(e instanceof Error ? e.message : "Impossibile caricare il workspace.");
      if (e instanceof ApiError && e.status === 401) { setUser(null); router.replace("/login"); }
    } finally { if (id === requestId.current) setLoading(false); }
  }, [router]);
  useEffect(() => { void refresh(); return () => { requestId.current++; }; }, [refresh]);
  const setSellerId = (id: number) => {
    if (!sellers.some(s => s.id === id)) return;
    setSellerIdState(id);
    try { if (user) localStorage.setItem(`mh:seller:${user.active_tenant_id}`, String(id)); } catch { /* Optional preference only. */ }
  };
  const switchTenant = async (id: number) => {
    setLoading(true);
    try {
      await api(`/tenants/${id}/activate`, { method: "POST" });
      setSellers([]); setSellerIdState(0);
      await refresh(); router.refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Cambio azienda non riuscito."); }
    finally { setLoading(false); }
  };
  const seller = useMemo(() => sellers.find(x => x.id === sellerId) || null, [sellers, sellerId]);
  return <Ctx.Provider value={{ user, tenants, sellers, seller, loading, error, setSellerId, switchTenant, refresh }}>{children}</Ctx.Provider>;
}
export function useWorkspace() { const x = useContext(Ctx); if (!x) throw new Error("WorkspaceProvider mancante"); return x; }
