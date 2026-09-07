"use client";

import { useEffect, useRef, useState, useTransition, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import type { WorkspaceResult } from "../lib/workspace";
import { connectionErrors, readConnections, safeConnectionError, WORTEN_API_URL, type ConnectorCode, type MarketplaceConnections } from "../lib/marketplace-connections-types";
import { filterMarketplaces, marketplaceCatalog, type MarketplaceDirectoryEntry } from "../lib/marketplace-catalog";
import { DashboardIcon } from "./DashboardIcon";

function MarketplaceLogo({ code, name }: { code: string; name: string }) {
  const [failed, setFailed] = useState(false);
  return <span className="marketplace-logo">{failed ? <strong>{name}</strong> : <img src={`/marketplaces/${code}.svg`} alt={name} width={144} height={44} onError={() => setFailed(true)} />}</span>;
}

export function MarketplaceConnectionsPage({ result }: { result: WorkspaceResult }) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const seller = result.workspace?.active_seller;
  return <div className="marketplace-page">
    <div className="workspace-heading"><div><p className="eyebrow">I TUOI CANALI DI VENDITA</p><h1>Collega marketplace</h1><p>Scegli il marketplace e collega il tuo account al negozio.</p></div></div>
    {result.error ? <section className="workspace-section workspace-empty"><h2>Dati momentaneamente non disponibili</h2><p className="form-error" role="alert">{result.error}</p><button type="button" className="workspace-refresh" disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}>Riprova</button></section>
      : seller ? <><div className="marketplace-shop"><span><DashboardIcon name="store" size={18} /><strong>{seller.name}</strong><span>{seller.organization_name}</span></span><a href="/seller#workspace-seller">Cambia negozio<DashboardIcon name="chevron" size={14} /></a></div><MarketplaceConnectionsPanel key={seller.id} sellerId={seller.id} sellerName={seller.name} /></>
        : <section className="workspace-section workspace-empty"><DashboardIcon name="store" size={26} /><h2>Seleziona un negozio</h2><p>Per collegare un marketplace serve un negozio assegnato al tuo account.</p><a className="workspace-refresh" href="/seller#workspace-seller">Vai ai tuoi negozi</a></section>}
  </div>;
}

/** A Seller change unmounts this component, clearing credentials and invalidating in-flight requests. */
export function MarketplaceConnectionsPanel({ sellerId, sellerName }: { sellerId: string; sellerName: string }) {
  const router = useRouter();
  const [connections, setConnections] = useState<MarketplaceConnections | null>(null);
  const [query, setQuery] = useState("");
  const [availability, setAvailability] = useState("all");
  const [category, setCategory] = useState("all");
  const [selected, setSelected] = useState<MarketplaceDirectoryEntry | null>(null);
  const [accountName, setAccountName] = useState("");
  const [clientKey, setClientKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [shopId, setShopId] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState<string | null>("read");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [needsReload, setNeedsReload] = useState(false);
  const alive = useRef(true);
  const version = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const formHeading = useRef<HTMLHeadingElement>(null);
  const canManage = Boolean(connections?.can_manage) && !pending && !needsReload;
  const domId = `marketplace-${sellerId}`;
  const availableCount = marketplaceCatalog.filter((entry) => entry.connector).length;
  const visible = filterMarketplaces(query, availability, category);

  useEffect(() => {
    alive.current = true;
    void request("read");
    return () => { alive.current = false; version.current += 1; controller.current?.abort(); busy.current = false; };
    // The component is keyed by the active Seller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (selected) formHeading.current?.focus(); }, [selected]);

  function clearCredentials() { setClientKey(""); setSecretKey(""); setApiKey(""); setShopId(""); }
  function resetForm() { clearCredentials(); setSelected(null); setAccountName(""); setDeleteId(null); setConfirmation(""); }

  async function request(operation: "read" | "connect" | "verify" | "delete", body?: object, accountId?: string) {
    if (busy.current) return;
    busy.current = true;
    const requestVersion = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    const current = () => alive.current && requestVersion === version.current;
    setPending(operation === "verify" ? `verify-${accountId}` : operation); setError(""); setSuccess("");
    const timeout = setTimeout(() => abort.abort(), 40000);
    try {
      const response = await fetch(`/api/sellers/${sellerId}/marketplace-connections${accountId ? `/${accountId}` : ""}${operation === "verify" ? "/verify" : ""}`, {
        method: operation === "read" ? "GET" : operation === "delete" ? "DELETE" : "POST",
        headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined, cache: "no-store", signal: abort.signal,
      });
      if (!current()) return;
      if (response.status === 401) { resetForm(); setConnections(null); router.replace("/login/seller"); router.refresh(); return; }
      if (response.status === 403 || response.status === 404) {
        resetForm(); setConnections(null); setError("Questi dati non sono più disponibili per il tuo account. Aggiorna i dati."); return;
      }
      const raw: unknown = await response.json().catch(() => null);
      if (!current()) return;
      if (!response.ok) {
        const code = safeConnectionError(raw);
        if (operation !== "read" && response.status >= 500) {
          setNeedsReload(true); clearCredentials();
          setError(`${code ? `${connectionErrors[code]} ` : ""}Operazione non confermata. Aggiorna i dati prima di riprovare.`);
        } else {
          if (operation === "read") setConnections(null);
          setError(code ? connectionErrors[code] : response.status === 409 ? "Un account con questo nome esiste già per questo marketplace." : "Operazione non riuscita. Controlla i campi e riprova.");
        }
        return;
      }
      const value = readConnections(raw, sellerId);
      if (!value) {
        clearCredentials();
        if (operation === "read") setConnections(null); else setNeedsReload(true);
        setError("Risposta non valida. Aggiorna i dati prima di riprovare."); return;
      }
      setConnections(value); setNeedsReload(false);
      if (!value.can_manage) resetForm();
      if (operation === "connect") {
        const confirmed = value.accounts.some((entry) => entry.marketplace === selected?.connector && entry.account_name === accountName.trim() && entry.connection_status === "connected");
        resetForm();
        if (confirmed) setSuccess("Marketplace collegato. La connessione API è stata verificata.");
        else setError("Il collegamento non risulta verificato. Controlla lo stato dell’account.");
      }
      if (operation === "delete") { setDeleteId(null); setConfirmation(""); setSuccess("Collegamento eliminato dal negozio."); }
      if (operation === "verify") {
        const account = value.accounts.find((entry) => entry.id === accountId);
        if (account?.connection_status === "connected") setSuccess("Connessione verificata. I dati dell’account sono aggiornati.");
        else setError(account?.error_code ? connectionErrors[account.error_code] : "La connessione non è stata confermata. Controlla lo stato dell’account.");
      }
    } catch {
      if (!current()) return;
      clearCredentials();
      if (operation === "read") { setConnections(null); setError("Impossibile caricare i collegamenti. Controlla la connessione e riprova."); }
      else { setNeedsReload(true); setError("Operazione non confermata. Aggiorna i dati prima di riprovare."); }
    } finally {
      clearTimeout(timeout);
      if (current()) { busy.current = false; setPending(null); }
    }
  }

  async function refresh() { resetForm(); await request("read"); }
  function choose(entry: MarketplaceDirectoryEntry) {
    if (!entry.connector || !canManage) return;
    resetForm(); setSelected(entry); setAccountName(`${entry.name} principale`); setError(""); setSuccess("");
  }
  const formValid = Boolean(accountName.trim() && (selected?.connector === "kaufland" ? clientKey.trim() && secretKey.trim() : apiKey.trim() && shopId.trim()));
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canManage || !selected?.connector || !formValid) return;
    const marketplace: ConnectorCode = selected.connector;
    await request("connect", { marketplace, account_name: accountName.trim(), credentials: marketplace === "kaufland"
      ? { client_key: clientKey.trim(), secret_key: secretKey.trim() }
      : { api_key: apiKey.trim(), shop_id: shopId.trim(), api_url: WORTEN_API_URL } });
  }

  return <div className="marketplace-workspace" aria-busy={Boolean(pending)}>
    <section className="workspace-section marketplace-linked" aria-labelledby={`${domId}-linked`}>
      <div className="workspace-section-heading"><h2 id={`${domId}-linked`}><DashboardIcon name="plug" />I tuoi collegamenti {connections && <span className="count-badge">{connections.accounts.length}</span>}</h2><button type="button" className="workspace-refresh" onClick={refresh} disabled={Boolean(pending)}><DashboardIcon name="refresh" size={14} />Aggiorna dati</button></div>
      <div className="workspace-section-body">
        {pending === "read" && <p className="workspace-muted" role="status">Caricamento dei collegamenti…</p>}
        {error && <p className="form-error" role="alert">{error}</p>}
        {success && <p className="settings-success" role="status">{success}</p>}
        {connections && !connections.can_manage && <p className="settings-notice">Accesso in sola lettura. Per collegare o modificare un account serve l’autorizzazione alla gestione del negozio.</p>}
        {connections?.accounts.length ? <ul className="marketplace-account-list">{connections.accounts.map((account) => {
          const entry = marketplaceCatalog.find((item) => item.code === account.marketplace);
          const canVerify = Boolean(entry?.connector) && account.active;
          const status = account.connection_status === "connected" ? "Collegato" : account.connection_status === "error" ? "Verifica non riuscita" : "Da verificare";
          return <li key={account.id}><div className="marketplace-account-row"><MarketplaceLogo code={entry?.code ?? "unknown"} name={entry?.name ?? account.marketplace} /><div className="marketplace-account-details"><strong>{account.account_name}</strong><span>{entry?.name ?? account.marketplace} · <span className={`connection-state state-${account.connection_status}`}>{status}</span>{!account.active && " · Disattivo"}</span>
            {account.public_name && <span>Nome sul marketplace: {account.public_name}</span>}
            {account.external_shop_id && <span>ID negozio: {account.external_shop_id}</span>}
            {account.storefronts.length > 0 && <span>Storefront registrati: {account.storefronts.join(", ")}</span>}
            <span>Chiave API: {account.credential_mask}</span>
            {account.last_checked_at && <span>Ultima verifica: {new Date(account.last_checked_at).toLocaleString("it-IT", { timeZone: "Europe/Rome" })}</span>}
            {account.error_code && <span className="marketplace-account-error">{connectionErrors[account.error_code]}</span>}
            {account.connection_status === "unverified" && <span>Credenziali salvate; verifica la connessione per confermare l’accesso.</span>}
          </div>{connections.can_manage && <div className="marketplace-account-actions"><button className="workspace-refresh" type="button" disabled={!canManage || !canVerify} onClick={() => { if (canManage && canVerify) void request("verify", undefined, account.id); }}>{pending === `verify-${account.id}` ? "Verifica in corso…" : "Verifica connessione"}</button><button type="button" className="settings-delete" disabled={!canManage} onClick={() => { if (canManage) { setDeleteId(account.id); setConfirmation(""); } }}>Elimina</button></div>}</div>
            {deleteId === account.id && connections.can_manage && <div className="settings-delete-confirm"><p>Eliminare il collegamento «{account.account_name}» da {sellerName}?</p><label htmlFor={`${domId}-delete`}>Scrivi ELIMINA per confermare</label><input id={`${domId}-delete`} autoComplete="off" disabled={!canManage} value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /><div className="settings-actions"><button type="button" className="settings-delete" disabled={!canManage || confirmation !== "ELIMINA"} onClick={() => { if (canManage && confirmation === "ELIMINA") void request("delete", { confirmation }, account.id); }}>{pending === "delete" ? "Eliminazione…" : "Elimina definitivamente"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={() => { setDeleteId(null); setConfirmation(""); }}>Annulla</button></div></div>}
          </li>;
        })}</ul> : connections && <div className="marketplace-no-accounts"><span className="marketplace-empty-icon"><DashboardIcon name="plug" size={24} /></span><div><strong>Collega il tuo primo marketplace</strong><p>Scegli un’integrazione disponibile nella griglia per iniziare.</p></div></div>}
      </div>
    </section>

    {selected?.connector && connections?.can_manage && <section className="workspace-section marketplace-connect" aria-labelledby={`${domId}-form-title`}>
      <div className="workspace-section-heading"><h2 ref={formHeading} tabIndex={-1} id={`${domId}-form-title`}>Collega {selected.name}</h2><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={resetForm}><DashboardIcon name="close" size={15} />Chiudi</button></div>
      <div className="workspace-section-body"><p className="settings-hint">Le credenziali vengono verificate tramite API prima di salvare il collegamento a {sellerName}.</p><form className="marketplace-connect-form" onSubmit={connect} autoComplete="off"><fieldset disabled={!canManage}><legend className="visually-hidden">Credenziali {selected.name}</legend><div className="settings-fields">
        <div className="settings-field-wide"><label htmlFor={`${domId}-account`}>Nome del collegamento</label><input id={`${domId}-account`} value={accountName} required onChange={(event) => setAccountName(event.target.value)} /></div>
        {selected.connector === "kaufland" ? <><div><label htmlFor={`${domId}-client`}>Client Key</label><input id={`${domId}-client`} value={clientKey} type="password" autoComplete="new-password" spellCheck={false} required onChange={(event) => setClientKey(event.target.value)} /></div><div><label htmlFor={`${domId}-secret`}>Secret Key</label><input id={`${domId}-secret`} value={secretKey} type="password" autoComplete="new-password" spellCheck={false} required onChange={(event) => setSecretKey(event.target.value)} /></div></>
          : <><div><label htmlFor={`${domId}-api-key`}>API Key</label><input id={`${domId}-api-key`} value={apiKey} type="password" autoComplete="new-password" spellCheck={false} required onChange={(event) => setApiKey(event.target.value)} /></div><div><label htmlFor={`${domId}-shop-id`}>ID negozio Worten</label><input id={`${domId}-shop-id`} value={shopId} autoComplete="off" spellCheck={false} required onChange={(event) => setShopId(event.target.value)} /></div><p className="settings-field-wide settings-hint">Endpoint Worten: {WORTEN_API_URL}</p></>}
        </div><p className="settings-hint">Le chiavi sono salvate cifrate e non vengono mostrate dopo il collegamento.</p><div className="settings-actions"><button type="submit" className="settings-primary" disabled={!canManage || !formValid}>{pending === "connect" ? "Verifica in corso…" : "Verifica e collega"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={resetForm}>Annulla</button></div></fieldset></form></div>
    </section>}

    <section className="marketplace-directory" aria-labelledby={`${domId}-catalog-title`}>
      <div className="marketplace-directory-heading"><div><h2 id={`${domId}-catalog-title`}>Scegli il marketplace</h2><p>{availableCount} integrazioni disponibili per il collegamento API. Le altre integrazioni sono da sviluppare.</p></div><span className="count-badge">{marketplaceCatalog.length} marketplace</span></div>
      <div className="marketplace-toolbar"><div className="marketplace-search"><DashboardIcon name="search" size={18} /><label className="visually-hidden" htmlFor={`${domId}-search`}>Cerca marketplace</label><input id={`${domId}-search`} type="search" placeholder="Cerca marketplace…" value={query} onChange={(event) => setQuery(event.target.value)} /></div><div><label className="visually-hidden" htmlFor={`${domId}-category`}>Categoria marketplace</label><select id={`${domId}-category`} value={category} onChange={(event) => setCategory(event.target.value)}><option value="all">Tutte le categorie</option>{[...new Set(marketplaceCatalog.map((entry) => entry.category))].map((value) => <option key={value} value={value}>{value}</option>)}</select></div></div>
      <div className="marketplace-filters" aria-label="Disponibilità delle integrazioni">{[["all", "Tutti", marketplaceCatalog.length], ["available", "Disponibili", availableCount], ["planned", "Da sviluppare", marketplaceCatalog.length - availableCount]].map(([value, label, count]) => <button type="button" key={value} aria-pressed={availability === value} className={availability === value ? "is-active" : ""} onClick={() => setAvailability(String(value))}>{label}<span>{count}</span></button>)}</div>
      <p className="visually-hidden" role="status">{visible.length} marketplace visualizzati.</p>
      {visible.length ? <div className="marketplace-grid">{visible.map((entry) => <article className={`marketplace-card${entry.connector ? " marketplace-available" : ""}`} key={entry.code}><div className="marketplace-card-logo"><MarketplaceLogo code={entry.code} name={entry.name} /></div><h3>{entry.name}</h3><p>{entry.category}</p><span className={`marketplace-availability${entry.connector ? " available" : ""}`}><span />{entry.connector ? "Collegamento API disponibile" : "Integrazione da sviluppare"}</span><button type="button" disabled={!entry.connector || !canManage} className="marketplace-card-button" onClick={() => choose(entry)} aria-label={entry.connector ? `Collega ${entry.name}` : `${entry.name}: integrazione da sviluppare`}>{entry.connector ? "Collega" : "Da sviluppare"}{entry.connector && <DashboardIcon name="arrow" size={15} />}</button></article>)}</div> : <div className="workspace-section workspace-empty"><h3>Nessun marketplace trovato</h3><p>Prova un altro nome o modifica i filtri.</p><button type="button" className="workspace-refresh" onClick={() => { setQuery(""); setCategory("all"); setAvailability("all"); }}>Azzera filtri</button></div>}
    </section>
  </div>;
}
