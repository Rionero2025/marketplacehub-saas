"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { isWorkspace, type Workspace } from "../lib/workspace-types";
import type { WorkspaceResult } from "../lib/workspace";
import { DashboardIcon } from "./DashboardIcon";

const titles = {
  seller: { eyebrow: "IL TUO SPAZIO DI LAVORO", title: "Panoramica Seller", description: "Organizzazione, negozio e accessi, sempre a portata di mano." },
  agency: { eyebrow: "IL TUO SPAZIO DI LAVORO", title: "Panoramica Agenzia", description: "I Seller affidati alla tua agenzia e le autorizzazioni del tuo account." },
  platform: { eyebrow: "AMMINISTRAZIONE", title: "Panoramica piattaforma", description: "Le organizzazioni e i negozi a cui puoi accedere." },
};
const kindLabels = { SELLER: "Seller", AGENCY: "Agenzia", PLATFORM: "Piattaforma" };

export function WorkspacePanel({ result, realm, loginPath }: {
  result: WorkspaceResult; realm: Workspace["realm"]; loginPath: string;
}) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [selection, setSelection] = useState<string | null>(null);
  const workspace = result.workspace;
  const activeSeller = workspace?.active_seller;
  const selectedId = selection ?? activeSeller?.id ?? "";
  const pending = saving || refreshing;
  const title = titles[realm];

  function refresh() {
    setError("");
    setSelection(null);
    startRefresh(() => router.refresh());
  }

  async function selectSeller(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setAnnouncement("");
    try {
      const response = await fetch("/api/workspace/select", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ seller_id: selectedId }),
      });
      if (response.status === 401) {
        router.replace(loginPath);
        router.refresh();
        return;
      }
      const value: unknown = await response.json().catch(() => null);
      if (!response.ok || !isWorkspace(value) || value.realm !== realm || value.active_seller?.id !== selectedId) {
        setError(response.status === 403 || response.status === 404
          ? "Questo negozio non è più disponibile per il tuo account. Aggiorna l’elenco e riprova."
          : "Impossibile cambiare negozio. Riprova tra poco.");
        return;
      }
      setAnnouncement(`Negozio selezionato: ${value.active_seller.name}.`);
      setSelection(null);
      startRefresh(() => router.refresh());
    } catch {
      setError("Impossibile cambiare negozio. Controlla la connessione e riprova.");
    } finally {
      setSaving(false);
    }
  }

  return <div className="workspace-overview" id="workspace-overview">
    <div className="workspace-heading">
      <div><p className="eyebrow">{title.eyebrow}</p><h1>{title.title}</h1><p>{title.description}</p></div>
      <button type="button" className="workspace-refresh" onClick={refresh} disabled={pending}><DashboardIcon name="refresh" size={16} />{refreshing ? "Aggiornamento…" : "Aggiorna dati"}</button>
    </div>
    <p role="status" className="visually-hidden">{announcement}</p>
    {result.error && <section className="workspace-section workspace-empty" aria-label="Area non disponibile"><h2>Dati momentaneamente non disponibili</h2><p role="alert">{result.error}</p><button type="button" onClick={refresh} disabled={pending}>Riprova</button></section>}
    {workspace && <>
      <div className="workspace-summary" aria-label="Riepilogo del tuo spazio di lavoro">
        <div className="workspace-stat"><span className="workspace-stat-icon"><DashboardIcon name="building" size={21} /></span><div><span>Organizzazioni</span><strong>{workspace.organizations.length}</strong></div><a href="#workspace-organizations" aria-label="Vai alle organizzazioni"><DashboardIcon name="arrow" size={17} /></a></div>
        <div className="workspace-stat"><span className="workspace-stat-icon stat-green"><DashboardIcon name="store" size={21} /></span><div><span>Negozi disponibili</span><strong>{workspace.sellers.length}</strong></div><a href="#workspace-seller" aria-label="Vai al negozio attivo"><DashboardIcon name="arrow" size={17} /></a></div>
        <div className="workspace-stat"><span className="workspace-stat-icon stat-violet"><DashboardIcon name="shield" size={21} /></span><div><span>Ambiti autorizzati</span><strong>{activeSeller ? activeSeller.permissions.length : "—"}</strong></div><a href="#workspace-permissions" aria-label="Vai alle autorizzazioni"><DashboardIcon name="arrow" size={17} /></a></div>
      </div>
      <div className="workspace-columns">
        <section className="workspace-section workspace-store" id="workspace-seller" aria-labelledby="seller-title">
          <div className="workspace-section-heading"><h2 id="seller-title"><DashboardIcon name="store" />Negozio attivo</h2>{activeSeller && <span className="workspace-status"><span />Accesso verificato</span>}</div>
          {workspace.sellers.length > 0 ? <>
            <div className="workspace-section-body">
              <form className="seller-selector" onSubmit={selectSeller}>
                <div><label htmlFor="active-seller">{realm === "seller" ? "Negozio" : "Seller"}</label><select id="active-seller" value={selectedId} onChange={(event) => { setSelection(event.target.value); setError(""); }} disabled={pending} aria-describedby="seller-context-note">
                  {!activeSeller && <option value="" disabled>Seleziona un negozio</option>}
                  {workspace.sellers.map((seller) => <option key={seller.id} value={seller.id}>{seller.name} · {seller.organization_name}</option>)}
                </select></div>
                <button type="submit" disabled={pending || !selectedId || selectedId === activeSeller?.id}>{saving ? "Selezione…" : "Seleziona"}<DashboardIcon name="chevron" size={15} /></button>
              </form>
              <p id="seller-context-note" className="workspace-muted context-note">Il negozio selezionato definisce il tuo contesto di lavoro.</p>
              {error && <p className="form-error" role="alert">{error}</p>}
              {activeSeller ? <div className="seller-profile">
                <div className="seller-profile-heading"><span className="store-mark" aria-hidden="true">{activeSeller.name.slice(0, 1).toUpperCase()}</span><div><h3>{activeSeller.name}</h3><p>{activeSeller.organization_name}</p></div></div>
                <dl className="seller-details"><div><dt>Ruolo nel negozio</dt><dd><span className="role-badge">{activeSeller.role_label}</span></dd></div><div><dt>Ragione sociale</dt><dd>{activeSeller.legal_name || "Non indicata"}</dd></div><div className="seller-email"><dt>Email del negozio</dt><dd>{activeSeller.email || "Non indicata"}</dd></div></dl>
              </div> : <p className="workspace-muted">Seleziona un negozio per consultarne i dati e le autorizzazioni.</p>}
            </div>
          </> : <div className="workspace-section-body workspace-empty"><span className="store-mark"><DashboardIcon name="store" size={24} /></span><h3>Nessun negozio assegnato</h3><p>Il tuo account è attivo. Contatta l’amministratore della tua organizzazione per l’assegnazione di un negozio.</p></div>}
        </section>
        <section className="workspace-section workspace-organizations" id="workspace-organizations" aria-labelledby="organizations-title">
          <div className="workspace-section-heading"><h2 id="organizations-title"><DashboardIcon name="building" />{workspace.organizations.length === 1 ? "La tua organizzazione" : "Organizzazioni"}</h2><span className="count-badge">{workspace.organizations.length}</span></div>
          <div className="workspace-section-body">
            {workspace.organizations.length ? <ul className="organization-list">{workspace.organizations.map((organization) => <li key={organization.id}>
              <div className="organization-summary"><div className="organization-icon" aria-hidden="true">{organization.name.slice(0, 1).toUpperCase()}</div><div className="organization-name"><strong>{organization.name}</strong><span>{kindLabels[organization.kind]}</span></div></div>
              <div className="organization-access"><span className="role-badge">{organization.role_label}</span>{organization.read_only && <span className="access-note">Sola lettura</span>}</div>
            </li>)}</ul> : <p className="workspace-muted">Nessuna organizzazione assegnata. Contatta l’amministratore per ricevere l’accesso.</p>}
          </div>
          <div className="workspace-section-foot"><DashboardIcon name="shield" size={15} /><span>Visualizzi le organizzazioni autorizzate per il tuo account.</span></div>
        </section>
      </div>
      <section className="workspace-section workspace-permissions" id="workspace-permissions" aria-labelledby="permissions-title">
        <div className="workspace-section-heading"><h2 id="permissions-title"><DashboardIcon name="shield" />Autorizzazioni del tuo account</h2>{activeSeller && <span className="workspace-current-store"><DashboardIcon name="store" size={14} />{activeSeller.name}</span>}</div>
        <p className="permission-intro">Livelli di accesso assegnati al negozio selezionato.</p>
        {activeSeller?.permissions.length ? <div className="permission-table-wrap"><table className="permission-table"><thead><tr><th scope="col">Ambito</th><th scope="col">Livello di accesso</th></tr></thead><tbody>{activeSeller.permissions.map((permission) => <tr key={permission}><td>{workspace.permission_labels[permission] ?? permission}</td><td><span className={`permission-mode ${activeSeller.write_permissions.includes(permission) ? "permission-edit" : ""}`}><DashboardIcon name={activeSeller.write_permissions.includes(permission) ? "check" : "shield"} size={13} />{activeSeller.write_permissions.includes(permission) ? "Modifica" : "Consultazione"}</span></td></tr>)}</tbody></table></div> : <p className="workspace-section-body workspace-muted">{activeSeller ? "Nessuna autorizzazione operativa assegnata. Contatta l’amministratore della tua organizzazione." : "Seleziona un negozio per consultare le autorizzazioni."}</p>}
      </section>
    </>}
  </div>;
}
