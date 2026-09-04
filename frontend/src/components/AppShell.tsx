"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { Logo } from "./Logo";
import { useWorkspace } from "./WorkspaceProvider";
import { Icon, type IconName } from "./Icon";
import { JobPulse } from "./JobPulse";

type NavItem = {
  href: string;
  label: string;
  permission: string;
  icon: IconName;
};

type NavGroup = {
  label: string;
  items: readonly NavItem[];
};

const navGroups: readonly NavGroup[] = [
  { label: "Workspace", items: [
    { href:"/dashboard", label:"Dashboard", permission:"dashboard", icon:"dashboard" },
    { href:"/orders", label:"Ordini", permission:"marketplace_orders", icon:"orders" },
    { href:"/accounting", label:"Contabilità", permission:"accounting", icon:"accounting" },
  ]},
  { label: "Catalogo & vendite", items: [
    { href:"/catalogs", label:"Cataloghi", permission:"suppliers_lists", icon:"catalogs" },
    { href:"/buybox", label:"Buy Box", permission:"buybox", icon:"buybox" },
  ]},
  { label: "Sistema", items: [
    { href:"/jobs", label:"Attività", permission:"dashboard", icon:"jobs" },
    { href:"/settings", label:"Impostazioni", permission:"seller_management", icon:"settings" },
  ]},
];

export function AppShell({ children }: { children: ReactNode }) {
  const path = usePathname(); const router = useRouter();
  const { user, tenants, sellers, seller, loading, error, refresh, setSellerId, switchTenant } = useWorkspace();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { setCollapsed(localStorage.getItem("mh:sidebar:collapsed") === "1"); }, []);
  useEffect(() => { setMobileOpen(false); }, [path]);
  if (loading) return <div className="screenCenter"><div className="spinner"/><span>Preparazione workspace…</span></div>;
  if (!user) return <div className="screenCenter" role="alert"><p>{error || "Sessione non disponibile."}</p><button className="secondaryButton" onClick={() => void refresh()}>Riprova</button></div>;
  const groups = navGroups.map(group => ({ ...group, items: group.items.filter(item => user.is_admin || user.permissions.includes(item.permission)) })).filter(group => group.items.length);
  const current = navGroups.flatMap(group => group.items).find(item => path === item.href || path.startsWith(`${item.href}/`));
  const toggleCollapsed = () => { const next = !collapsed; setCollapsed(next); localStorage.setItem("mh:sidebar:collapsed", next ? "1" : "0"); };
  return <div className={`appFrame ${collapsed ? "sidebarCollapsed" : ""}`}>
    {mobileOpen && <button className="sidebarBackdrop" aria-label="Chiudi menu" onClick={() => setMobileOpen(false)}/>} 
    <aside className={`sidebar ${mobileOpen ? "mobileOpen" : ""}`}>
      <div className="sidebarBrand"><Logo compact={collapsed}/><button className="collapseButton" onClick={toggleCollapsed} title={collapsed?"Espandi menu":"Riduci menu"}><Icon name="chevron" size={16}/></button></div>
      <nav className="nav">{groups.map(group => <div className="navGroup" key={group.label}><span className="navGroupLabel">{group.label}</span>{group.items.map(item => {
        const active = path === item.href || path.startsWith(`${item.href}/`);
        return <Link key={item.href} href={item.href} className={active ? "navItem active" : "navItem"} title={collapsed ? item.label : undefined}><Icon name={item.icon as IconName}/><span>{item.label}</span>{active && <i className="navActiveMarker"/>}</Link>;
      })}</div>)}</nav>
      <div className="sidebarFoot"><div className="userMini"><span className="avatar">{(user.display_name || user.username).slice(0,1).toUpperCase()}</span><span className="userMiniText"><b>{user.display_name || user.username}</b><small>{user.tenant_role || "utente"}</small></span></div><button className="logoutButton" onClick={async()=>{await api("/auth/logout",{method:"POST"});router.replace("/login")}}>Esci</button></div>
    </aside>
    <div className="mainArea">
      <header className="topbar">
        <div className="topbarStart"><button className="mobileMenuButton" onClick={() => setMobileOpen(true)} aria-label="Apri menu"><Icon name="menu"/></button><div className="pageCrumb"><span>Marketplace Hub</span><Icon name="chevron" size={13}/><b>{current?.label || "Workspace"}</b></div></div>
        <div className="contextGroup">
          {tenants.length > 1 && <label className="contextField"><span><Icon name="building" size={14}/>Azienda</span><select value={user.active_tenant_id} onChange={e=>void switchTenant(Number(e.target.value))}>{tenants.map(t=><option key={t.id} value={t.id}>{t.name}</option>)}</select></label>}
          <label className="contextField"><span><Icon name="store" size={14}/>Seller</span><select value={seller?.id || ""} disabled={!sellers.length} onChange={e=>setSellerId(Number(e.target.value))}>{!sellers.length && <option value="">Nessun Seller disponibile</option>}{sellers.map(s=><option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        </div>
        <div className="topbarEnd"><JobPulse sellerId={seller?.id}/><div className="tenantBadge"><b>{user.active_tenant_name}</b><span>{user.active_tenant_type} · {user.tenant_role}</span></div></div>
      </header>
      <main className="content">{error && <div className="errorBox" role="alert">{error} <button className="secondaryButton" onClick={() => void refresh()}>Riprova caricamento</button></div>}{children}</main>
    </div>
  </div>;
}
