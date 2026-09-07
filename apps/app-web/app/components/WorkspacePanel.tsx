"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";
import { isWorkspace, type Workspace } from "../lib/workspace-types";
import type { WorkspaceResult } from "../lib/workspace";

const titles = {
  seller: { eyebrow: "AREA SELLER", title: "Il tuo negozio", description: "La tua organizzazione, il negozio e le autorizzazioni del tuo account." },
  agency: { eyebrow: "AREA AGENZIA", title: "I negozi della tua agenzia", description: "Scegli il Seller su cui lavorare tra i negozi affidati alla tua agenzia." },
  platform: { eyebrow: "AMMINISTRAZIONE", title: "Organizzazioni e Seller", description: "Consulta le organizzazioni e seleziona il negozio su cui operare." },
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

  return <div className="workspace-overview">
    <div className="workspace-heading">
      <div><p className="eyebrow">{title.eyebrow}</p><h1>{title.title}</h1><p>{title.description}</p></div>
      <button type="button" className="secondary-button" onClick={refresh} disabled={pending}>{refreshing ? "Aggiornamento…" : "Aggiorna"}</button>
    </div>
    <p role="status" className="visually-hidden">{announcement}</p>
    {result.error && <section className="workspace-section workspace-empty" aria-label="Area non disponibile"><h2>Dati momentaneamente non disponibili</h2><p role="alert">{result.error}</p><button type="button" onClick={refresh} disabled={pending}>Riprova</button></section>}
    {workspace && <>
      <section className="workspace-section" aria-labelledby="organizations-title">
        <div className="workspace-section-heading"><h2 id="organizations-title">{workspace.organizations.length === 1 ? "La tua organizzazione" : "Organizzazioni"}</h2><span className="count-badge">{workspace.organizations.length}</span></div>
        {workspace.organizations.length ? <ul className="organization-list">{workspace.organizations.map((organization) => <li key={organization.id}>
          <div className="organization-icon" aria-hidden="true">{organization.name.slice(0, 1).toUpperCase()}</div>
          <div className="organization-name"><strong>{organization.name}</strong><span>{kindLabels[organization.kind]}</span></div>
          <div className="organization-access"><span className="role-badge">{organization.role_label}</span>{organization.read_only && <span className="access-note">Sola lettura</span>}</div>
        </li>)}</ul> : <p className="workspace-muted">Nessuna organizzazione assegnata. Contatta l’amministratore per ricevere l’accesso.</p>}
      </section>

      {workspace.sellers.length > 0 ? <section className="workspace-section" aria-labelledby="seller-title">
        <div className="workspace-section-heading"><h2 id="seller-title">Negozio attivo</h2><span className="count-badge">{workspace.sellers.length} {workspace.sellers.length === 1 ? "negozio disponibile" : "negozi disponibili"}</span></div>
        <form className="seller-selector" onSubmit={selectSeller}>
          <div><label htmlFor="active-seller">{realm === "seller" ? "Negozio" : "Seller"}</label><select id="active-seller" value={selectedId} onChange={(event) => { setSelection(event.target.value); setError(""); }} disabled={pending} aria-describedby="seller-context-note">
            {!activeSeller && <option value="" disabled>Seleziona un negozio</option>}
            {workspace.sellers.map((seller) => <option key={seller.id} value={seller.id}>{seller.name} · {seller.organization_name}</option>)}
          </select></div>
          <button type="submit" disabled={pending || !selectedId || selectedId === activeSeller?.id}>{saving ? "Selezione…" : "Seleziona negozio"}</button>
        </form>
        <p id="seller-context-note" className="workspace-muted context-note">Le autorizzazioni mostrate si riferiscono al negozio attivo.</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        {activeSeller ? <>
          <div className="seller-profile">
            <div className="seller-profile-heading"><span className="store-mark" aria-hidden="true">{activeSeller.name.slice(0, 1).toUpperCase()}</span><div><h3>{activeSeller.name}</h3><p>{activeSeller.organization_name}</p></div><span className="role-badge">{activeSeller.role_label}</span></div>
            <dl className="seller-details"><div><dt>Ragione sociale</dt><dd>{activeSeller.legal_name || "Non indicata"}</dd></div><div><dt>Email del negozio</dt><dd>{activeSeller.email || "Non indicata"}</dd></div></dl>
          </div>
          <div className="seller-permissions"><h3>Autorizzazioni del tuo account</h3><p className="workspace-muted">Ambiti di accesso assegnati per {activeSeller.name}.</p>
            {activeSeller.permissions.length ? <ul className="permission-list">{activeSeller.permissions.map((permission) => <li key={permission}><span>{workspace.permission_labels[permission] ?? permission}</span><span className={`permission-mode ${activeSeller.write_permissions.includes(permission) ? "permission-edit" : ""}`}>{activeSeller.write_permissions.includes(permission) ? "Modifica" : "Consultazione"}</span></li>)}</ul> : <p className="workspace-muted">Nessuna autorizzazione operativa assegnata. Contatta l’amministratore della tua organizzazione.</p>}
          </div>
        </> : <p className="workspace-muted">Seleziona un negozio per consultarne i dati e le autorizzazioni.</p>}
      </section> : <section className="workspace-section workspace-empty" aria-labelledby="no-seller-title"><span className="store-mark" aria-hidden="true">M</span><h2 id="no-seller-title">Nessun negozio assegnato</h2><p>Il tuo account è attivo. Contatta l’amministratore della tua organizzazione per l’assegnazione di un negozio.</p></section>}
    </>}
  </div>;
}
