"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { Logo } from "./Logo";
import { useWorkspace } from "./WorkspaceProvider";

const nav = [
  ["/dashboard","Dashboard","dashboard"], ["/orders","Ordini","marketplace_orders"], ["/accounting","Contabilità","accounting"],
  ["/catalogs","Cataloghi","suppliers_lists"], ["/buybox","Buy Box","buybox"], ["/jobs","Attività","dashboard"], ["/settings","Impostazioni","seller_management"],
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const path = usePathname(); const router = useRouter(); const { user, tenants, sellers, seller, loading, setSellerId, switchTenant } = useWorkspace();
  if (loading) return <div className="screenCenter"><div className="spinner"/>Caricamento workspace…</div>;
  if (!user) return null;
  const allowed = nav.filter(([, , p]) => user.is_admin || user.permissions.includes(p));
  return <div className="appFrame">
    <aside className="sidebar">
      <Logo />
      <nav className="nav">{allowed.map(([href,label]) => <Link key={href} href={href} className={path===href?"navItem active":"navItem"}>{label}</Link>)}</nav>
      <div className="sidebarFoot"><span className="muted">{user.display_name || user.username}</span><button className="ghostButton" onClick={async()=>{await api("/auth/logout",{method:"POST"});router.replace("/login")}}>Esci</button></div>
    </aside>
    <div className="mainArea">
      <header className="topbar">
        <div className="contextGroup">
          {tenants.length > 1 && <label className="contextField"><span>Azienda</span><select value={user.active_tenant_id} onChange={e=>void switchTenant(Number(e.target.value))}>{tenants.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select></label>}
          <label className="contextField"><span>Seller</span><select value={seller?.id || ""} onChange={e=>setSellerId(Number(e.target.value))}>{sellers.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        </div>
        <div className="tenantBadge"><b>{user.active_tenant_name}</b><span>{user.tenant_role || user.active_tenant_type}</span></div>
      </header>
      <main className="content">{children}</main>
    </div>
  </div>;
}
