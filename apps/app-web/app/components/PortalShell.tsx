"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { DashboardIcon } from "./DashboardIcon";
import type { DashboardIconName } from "./DashboardIcon";
import { sellerAreas, legacySellerSections, type SellerPage } from "../lib/seller-navigation";

export function PortalShell({ portal, displayName, children, currentPage = "overview" }: { portal: string; displayName: string; children: ReactNode; currentPage?: SellerPage }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuArea, setMenuArea] = useState<string | null>(null);
  const [activeSection, setActiveSection] = useState(currentPage === "overview" ? "#workspace-overview" : `/seller/${currentPage}`);
  const sidebarRef = useRef<HTMLElement>(null);
  const menuToggleRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (portal !== "SELLER" || currentPage !== "overview") return;
    function followLegacySection() {
      const destination = legacySellerSections[window.location.hash];
      if (destination) router.replace(destination);
    }
    followLegacySection();
    window.addEventListener("hashchange", followLegacySection);
    return () => window.removeEventListener("hashchange", followLegacySection);
  }, [portal, currentPage, router]);
  useEffect(() => {
    if (!menuOpen) return;
    const mobile = window.matchMedia("(max-width: 900px)");
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
  const overviewPrefix = currentPage !== "overview" ? "/seller" : "";
  const portalLabel = sellerPortal ? "Area Seller" : portal === "AGENZIA" ? "Area Agenzia" : "Amministrazione";
  const pageArea = sellerAreas.find((area) => area.sections.some((section) => section.page === currentPage)) ?? sellerAreas[0];
  const activeArea = menuOpen && menuArea ? sellerAreas.find((area) => area.id === menuArea) ?? pageArea : pageArea;
  const currentSection = pageArea.sections.find((section) => section.page === currentPage) ?? pageArea.sections[0];
  const navigation: { href: string; label: string; icon: DashboardIconName }[] = sellerPortal ? activeArea.sections : [
    { href: `${overviewPrefix}#workspace-overview`, label: "Panoramica", icon: "home" },
    { href: `${overviewPrefix}#workspace-seller`, label: sellerPortal ? "Negozio" : "Negozi", icon: "store" },
    { href: `${overviewPrefix}#workspace-organizations`, label: "Organizzazioni", icon: "building" },
    { href: `${overviewPrefix}#workspace-permissions`, label: "Autorizzazioni", icon: "shield" },
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
        <Link className="hub-rail-brand" href={sellerPortal ? "/seller" : "#workspace-overview"} prefetch={false} onClick={() => { setActiveSection("#workspace-overview"); setMenuOpen(false); }} aria-label="Marketplace Hub, panoramica">MH</Link>
        <nav aria-label={sellerPortal ? "Macroaree Seller" : "Scorciatoie del workspace"}>
          {sellerPortal ? sellerAreas.map((area) => <Link key={area.id} className={`hub-rail-link${activeArea.id === area.id ? " is-active" : ""}${area.id === "settings" ? " hub-rail-settings" : ""}`} href={area.sections[0].href} prefetch={false} aria-label={area.label} aria-current={activeArea.id === area.id ? "location" : undefined} onClick={(event) => { if (menuOpen) { event.preventDefault(); setMenuArea(area.id); } }}><DashboardIcon name={area.icon} size={21} /><span className="hub-rail-tooltip" aria-hidden="true">{area.label}</span></Link>)
            : navigation.map((item) => <a key={item.href} className={`hub-rail-link${activeSection === item.href ? " is-active" : ""}`} href={item.href} title={item.label} aria-label={item.label} aria-current={activeSection === item.href ? "location" : undefined} onClick={() => { setActiveSection(item.href); setMenuOpen(false); }}><DashboardIcon name={item.icon} size={21} /></a>)}
        </nav>
      </div>
      <div className="hub-sidebar-panel">
      <button className="hub-menu-close" type="button" aria-label="Chiudi il menu" onClick={() => setMenuOpen(false)}><DashboardIcon name="close" size={20} /></button>
      <a className="hub-brand" href={`${overviewPrefix}#workspace-overview`} onClick={() => { setActiveSection("#workspace-overview"); setMenuOpen(false); }} aria-label="Marketplace Hub, panoramica">
        <span><span className="hub-brand-name">Marketplace Hub</span><span className="hub-brand-caption">Il tuo spazio operativo</span></span>
      </a>
      <p className="hub-nav-label">{sellerPortal ? activeArea.label : "Workspace"}</p>
      <nav className="hub-nav" aria-label={sellerPortal ? `Sottosezioni ${activeArea.label}` : "Navigazione del workspace"}>
        {navigation.map((item) => <Link key={item.href} prefetch={false} className={`hub-nav-link${(sellerPortal ? currentSection.href === item.href : activeSection === item.href) ? " is-active" : ""}`} href={item.href} aria-current={(sellerPortal ? currentSection.href === item.href : activeSection === item.href) ? "page" : undefined} onClick={() => { setActiveSection(item.href); setMenuOpen(false); }}>
          <DashboardIcon name={item.icon} /><span>{item.label}</span>
        </Link>)}
      </nav>
      <div className="hub-sidebar-footer">
        <span className="hub-sidebar-portal">{portalLabel}</span>
        <p className="hub-sidebar-help">Il tuo negozio, le tue autorizzazioni.</p>
      </div>
      </div>
    </aside>
    <section className="hub-main" inert={menuOpen}>
      <header className="hub-topbar">
        <button ref={menuToggleRef} type="button" className="hub-menu-toggle" aria-label={menuOpen ? "Chiudi il menu" : "Apri il menu"} aria-controls="hub-sidebar" aria-expanded={menuOpen} onClick={() => { setMenuArea(null); setMenuOpen(!menuOpen); }}><DashboardIcon name={menuOpen ? "close" : "menu"} size={21} /></button>
        <div className="hub-breadcrumbs" aria-label="Percorso corrente"><span>{sellerPortal ? pageArea.label : "Workspace"}</span><DashboardIcon name="chevron" size={12} /><strong>{sellerPortal ? currentSection.label : "Panoramica"}</strong></div>
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
