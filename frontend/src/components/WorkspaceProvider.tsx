"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import type { Seller, Tenant, User } from "@/lib/types";

type Workspace = {
  user: User | null; tenants: Tenant[]; sellers: Seller[]; seller: Seller | null; loading: boolean;
  setSellerId: (id: number) => void; switchTenant: (id: number) => Promise<void>; refresh: () => Promise<void>;
};
const Ctx = createContext<Workspace | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter(); const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null); const [tenants, setTenants] = useState<Tenant[]>([]);
  const [sellers, setSellers] = useState<Seller[]>([]); const [sellerId, setSellerIdState] = useState<number>(0);
  const [loading, setLoading] = useState(true);

  const refresh = async () => {
    try {
      const me = await api<User>("/auth/me"); setUser(me);
      const [t, s] = await Promise.all([api<Tenant[]>("/tenants"), api<Seller[]>("/sellers")]);
      setTenants(t); setSellers(s);
      const saved = Number(localStorage.getItem(`mh:seller:${me.active_tenant_id}`) || 0);
      setSellerIdState(s.some(x => x.id === saved) ? saved : (s[0]?.id || 0));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401 && !pathname.startsWith("/login") && !pathname.startsWith("/signup")) router.replace("/login");
    } finally { setLoading(false); }
  };
  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const setSellerId = (id: number) => { setSellerIdState(id); if (user) localStorage.setItem(`mh:seller:${user.active_tenant_id}`, String(id)); };
  const switchTenant = async (id: number) => { await api(`/tenants/${id}/activate`, { method: "POST" }); setLoading(true); await refresh(); router.refresh(); };
  const seller = useMemo(() => sellers.find(x => x.id === sellerId) || null, [sellers, sellerId]);
  return <Ctx.Provider value={{ user, tenants, sellers, seller, loading, setSellerId, switchTenant, refresh }}>{children}</Ctx.Provider>;
}
export function useWorkspace() { const x = useContext(Ctx); if (!x) throw new Error("WorkspaceProvider mancante"); return x; }
