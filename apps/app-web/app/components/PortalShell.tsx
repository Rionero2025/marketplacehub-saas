"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { DashboardIcon } from "./DashboardIcon";
import type { DashboardIconName } from "./DashboardIcon";

export function PortalShell({ portal, displayName, children }: { portal: string; displayName: string; children: ReactNode }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [activeSection, setActiveSection] = useState("#workspace-overview");
  const sidebarRef = useRef<HTMLElement>(null);
  const menuToggleRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const mobile = window.matchMedia("(max-width: 600px)");
    if (!mobile.matches) { setMenuOpen(false); return; }
    const sidebar = sidebarRef.current;
    if (!sidebar) return;
    const focusable = () => Array.from(sidebar.querySelectorAll<HTMLElement>("a[href], button:not([disabled])"))
      .filter((element) => element.getClientRects().length > 0);
    sidebar.querySelector<HTMLButtonElement>(".hub-menu-close")?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
      } else if (event.key === "Tab") {
        const items = focusable();
        const first = items[0];
        const last = items[items.length - 1];
        if (!first || !last) return;
        if (!sidebar?.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
          event.preventDefault();
          (event.shiftKey ? last : first).focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }
    function onViewportChange() { if (!mobile.matches) setMenuOpen(false); }
    document.addEventListener("keydown", onKeyDown);
    mobile.addEventListener("change", onViewportChange);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      mobile.removeEventListener("change", onViewportChange);
      if (mobile.matches) menuToggleRef.current?.focus({ preventScroll: true });
      else sidebar.querySelector<HTMLElement>("a[aria-current]")?.focus({ preventScroll: true });
    };
  }, [menuOpen]);
  const sellerPortal = portal === "SELLER";
  const portalLabel = sellerPortal ? "Area Seller" : portal === "AGENZIA" ? "Area Agenzia" : "Amministrazione";
  const navigation: { href: string; label: string; icon: DashboardIconName }[] = [
    { href: "#workspace-overview", label: "Panoramica", icon: "home" },
    { href: "#workspace-seller", label: sellerPortal ? "Negozio" : "Negozi", icon: "store" },
    { href: "#workspace-organizations", label: "Organizzazioni", icon: "building" },
    { href: "#workspace-permissions", label: "Autorizzazioni", icon: "shield" },
  ];
  async function logout() {
    setPending(true);
    setLogoutError("");
    try {
      const response = await fetch("/api/auth/logout", { method: "POST", signal: AbortSignal.timeout(20000) });
      if (!response.ok) {
        setLogoutError("Non è stato possibile completare l’uscita. Riprova.");
        return;
      }
      router.replace("/");
      router.refresh();
    } catch {
      setLogoutError("Non è stato possibile completare l’uscita. Controlla la connessione e riprova.");
    } finally {
      setPending(false);
    }
  }
  return <div className={`hub-shell${menuOpen ? " hub-menu-open" : ""}`}>
    {menuOpen && <button className="hub-menu-backdrop" type="button" aria-label="Chiudi il menu" onClick={() => setMenuOpen(false)} />}
    <aside ref={sidebarRef} className="hub-sidebar" id="hub-sidebar" role={menuOpen ? "dialog" : undefined} aria-modal={menuOpen || undefined} aria-label="Navigazione del workspace">
      <div className="hub-rail">
        <a className="hub-rail-brand" href="#workspace-overview" onClick={() => { setActiveSection("#workspace-overview"); setMenuOpen(false); }} aria-label="Marketplace Hub, panoramica">MH</a>
        <nav aria-label="Scorciatoie del workspace">
          {navigation.map((item) => <a key={item.href} className={`hub-rail-link${activeSection === item.href ? " is-active" : ""}`} href={item.href} title={item.label} aria-label={item.label} aria-current={activeSection === item.href ? "location" : undefined} onClick={() => { setActiveSection(item.href); setMenuOpen(false); }}><DashboardIcon name={item.icon} size={21} /></a>)}
        </nav>
      </div>
      <div className="hub-sidebar-panel">
      <button className="hub-menu-close" type="button" aria-label="Chiudi il menu" onClick={() => setMenuOpen(false)}><DashboardIcon name="close" size={20} /></button>
      <a className="hub-brand" href="#workspace-overview" onClick={() => { setActiveSection("#workspace-overview"); setMenuOpen(false); }} aria-label="Marketplace Hub, panoramica">
        <span><span className="hub-brand-name">Marketplace Hub</span><span className="hub-brand-caption">Il tuo spazio operativo</span></span>
      </a>
      <p className="hub-nav-label">Workspace</p>
      <nav className="hub-nav" aria-label="Navigazione del workspace">
        {navigation.map((item) => <a key={item.href} className={`hub-nav-link${activeSection === item.href ? " is-active" : ""}`} href={item.href} aria-current={activeSection === item.href ? "location" : undefined} onClick={() => { setActiveSection(item.href); setMenuOpen(false); }}>
          <DashboardIcon name={item.icon} /><span>{item.label}</span>
        </a>)}
      </nav>
      <div className="hub-sidebar-footer">
        <span className="hub-sidebar-portal">{portalLabel}</span>
        <p className="hub-sidebar-help">Il tuo negozio, le tue autorizzazioni.</p>
      </div>
      </div>
    </aside>
    <section className="hub-main" inert={menuOpen}>
      <header className="hub-topbar">
        <button ref={menuToggleRef} type="button" className="hub-menu-toggle" aria-label={menuOpen ? "Chiudi il menu" : "Apri il menu"} aria-controls="hub-sidebar" aria-expanded={menuOpen} onClick={() => setMenuOpen(!menuOpen)}><DashboardIcon name={menuOpen ? "close" : "menu"} size={21} /></button>
        <div className="hub-breadcrumbs"><span>Workspace</span><DashboardIcon name="chevron" size={12} /><strong>Panoramica</strong></div>
        <div className="hub-topbar-account">
          <span className="hub-user-avatar" aria-hidden="true">{displayName.trim().slice(0, 1).toUpperCase() || "M"}</span>
          <div className="hub-user-details"><span className="hub-user-name">{displayName}</span><span className="hub-user-realm">{portalLabel}</span></div>
          <button id="hub-logout" type="button" className="hub-logout" aria-label={pending ? "Uscita in corso" : "Esci dall’account"} onClick={logout} disabled={pending}><DashboardIcon name="exit" size={17} /><span>{pending ? "Uscita…" : "Esci"}</span></button>
        </div>
      </header>
      <main className="hub-content">
        {logoutError && <p className="form-error hub-logout-error" role="alert">{logoutError}</p>}
        {children}
      </main>
    </section>
  </div>;
}
