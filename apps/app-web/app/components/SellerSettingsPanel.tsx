"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { readSellerSettings, type SellerSettings } from "../lib/seller-settings-types";
import { DashboardIcon } from "./DashboardIcon";

type ProfileDraft = { name: string; legal_name: string; email: string };
const emptyDraft: ProfileDraft = { name: "", legal_name: "", email: "" };
const toDraft = (value: SellerSettings): ProfileDraft => ({ name: value.name, legal_name: value.legal_name ?? "", email: value.email ?? "" });

/** Mounted with the Seller id as React key, so switching shops destroys drafts and credential inputs. */
export function SellerSettingsPanel({ sellerId, loginPath, onSaved }: { sellerId: string; loginPath: string; onSaved: () => void }) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [settings, setSettings] = useState<SellerSettings | null>(null);
  const [draft, setDraft] = useState<ProfileDraft>(emptyDraft);
  const [accountName, setAccountName] = useState("Kaufland principale");
  const [clientKey, setClientKey] = useState("");
  const [secretKey, setSecretKey] = useState("");
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [needsReload, setNeedsReload] = useState(false);
  const active = useRef(true);
  const requestVersion = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const operationActive = useRef(false);
  const canEdit = Boolean(settings?.can_manage) && !pending && !needsReload;
  const domId = `seller-settings-${sellerId}`;

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; requestVersion.current += 1; controller.current?.abort(); };
  }, []);

  useEffect(() => {
    if (expanded) void load();
    // Loading starts on opening; retry uses the explicit refresh action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  function clearKeys() { setClientKey(""); setSecretKey(""); }

  async function request(operation: "read" | "save" | "add-account" | "delete-account", body?: object, accountId?: string) {
    if (operationActive.current) return;
    operationActive.current = true;
    const version = ++requestVersion.current;
    const abort = new AbortController();
    controller.current = abort;
    setPending(operation);
    setError("");
    setSuccess("");
    const current = () => active.current && version === requestVersion.current;
    const timeout = setTimeout(() => abort.abort(), 20000);
    try {
      const path = operation === "read" || operation === "save" ? "settings" : `kaufland-accounts${accountId ? `/${accountId}` : ""}`;
      const response = await fetch(`/api/sellers/${sellerId}/${path}`, {
        method: { read: "GET", save: "PUT", "add-account": "POST", "delete-account": "DELETE" }[operation],
        headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
        cache: "no-store", signal: abort.signal,
      });
      if (!current()) return;
      if (response.status === 401) {
        clearKeys(); setSettings(null);
        router.replace(loginPath); router.refresh();
        return;
      }
      if (!response.ok) {
        if (response.status === 403 || response.status === 404) {
          setSettings(null); clearKeys();
          setError("Questi dati non sono più disponibili per il tuo account. Aggiorna i dati.");
        } else if (operation !== "read" && response.status >= 500) {
          setNeedsReload(true);
          setError("Operazione non confermata. Aggiorna i dati prima di riprovare.");
        } else {
          if (operation === "read") setSettings(null);
          setError(operation === "read" ? "Impossibile caricare le impostazioni. Riprova tra poco." : "Salvataggio non riuscito. Controlla i campi e riprova.");
        }
        return;
      }
      const value = readSellerSettings(await response.json().catch(() => null), sellerId);
      if (!current()) return;
      if (!value) {
        if (operation === "read") setSettings(null);
        else setNeedsReload(true);
        setError("Risposta non valida. Aggiorna i dati prima di riprovare.");
        return;
      }
      setSettings(value); setDraft(toDraft(value)); setNeedsReload(false);
      if (!value.can_manage) clearKeys();
      if (operation !== "read") {
        if (operation === "add-account") { clearKeys(); setAccountName("Kaufland principale"); }
        if (operation === "delete-account") { setDeleteId(null); setConfirmation(""); }
        setSuccess(operation === "save" ? "Dati del negozio salvati." : operation === "add-account" ? "Account Kaufland salvato. La connessione API non è stata verificata." : "Account eliminato.");
        onSaved();
      }
    } catch {
      if (!current()) return;
      if (operation === "read") { setSettings(null); setError("Impossibile caricare le impostazioni. Controlla la connessione e riprova."); }
      else { setNeedsReload(true); setError("Operazione non confermata. Aggiorna i dati prima di riprovare."); }
    } finally {
      clearTimeout(timeout);
      if (current()) { operationActive.current = false; setPending(null); }
    }
  }

  async function load() { clearKeys(); setDeleteId(null); setConfirmation(""); await request("read"); }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit) return;
    if (!draft.name.trim()) { setError("Inserisci il nome del negozio."); return; }
    await request("save", { name: draft.name.trim(), legal_name: draft.legal_name.trim(), email: draft.email.trim() });
  }

  async function addAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit) return;
    if (!accountName.trim() || !(clientKey.trim() || secretKey.trim())) { setError("Inserisci il nome dell’account e almeno una chiave API."); return; }
    await request("add-account", { account_name: accountName.trim(), client_key: clientKey.trim(), secret_key: secretKey.trim() });
  }

  return <div className="seller-settings">
    <button type="button" className="workspace-refresh settings-toggle" onClick={() => { if (expanded) clearKeys(); setExpanded(!expanded); }} aria-expanded={expanded} aria-controls={domId} disabled={Boolean(pending)}>
      <DashboardIcon name="store" size={15} />{expanded ? "Chiudi impostazioni" : "Gestisci negozio"}
    </button>
    {expanded && <div id={domId} className="settings-content" aria-busy={Boolean(pending)}>
      <div className="settings-heading"><h3>Impostazioni del negozio</h3><button type="button" className="workspace-refresh" onClick={load} disabled={Boolean(pending)}><DashboardIcon name="refresh" size={14} />Aggiorna dati</button></div>
      {pending === "read" && <p className="workspace-muted" role="status">Caricamento delle impostazioni…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {success && <p className="settings-success" role="status">{success}</p>}
      {settings && <>
        {!settings.can_manage && <p className="settings-notice">Accesso in sola lettura. Per modificare questi dati serve l’autorizzazione alla gestione del negozio.</p>}
        <form className="settings-form" onSubmit={saveProfile}>
          <fieldset disabled={!canEdit}>
            <legend>Anagrafica del negozio</legend>
            <div className="settings-fields">
              <div><label htmlFor={`${domId}-name`}>Nome negozio</label><input id={`${domId}-name`} required value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></div>
              <div><label htmlFor={`${domId}-legal`}>Ragione sociale</label><input id={`${domId}-legal`} value={draft.legal_name} onChange={(event) => setDraft({ ...draft, legal_name: event.target.value })} /></div>
              <div className="settings-field-wide"><label htmlFor={`${domId}-email`}>Email del negozio</label><input id={`${domId}-email`} type="text" value={draft.email} onChange={(event) => setDraft({ ...draft, email: event.target.value })} /></div>
            </div>
            {settings.can_manage && <button className="settings-primary" type="submit" disabled={!canEdit || !draft.name.trim()}>{pending === "save" ? "Salvataggio…" : "Salva dati negozio"}</button>}
          </fieldset>
        </form>
        <div className="settings-accounts">
          <h3>Account Kaufland</h3>
          <p className="settings-hint">Gli account salvati sono disponibili per questo negozio. Il salvataggio delle chiavi non verifica la connessione al marketplace.</p>
          {settings.marketplace_accounts.length ? <ul className="settings-account-list">{settings.marketplace_accounts.map((account) => <li key={account.id}>
            <div className="settings-account-summary"><div><strong>{account.account_name}</strong><span>{account.active ? "Attivo" : "Disattivo"} · {account.credentials_configured ? "Credenziali salvate" : "Credenziali non configurate"}</span><span>Client Key: {account.client_key_masked}</span></div>
              {settings.can_manage && <button type="button" className="settings-delete" disabled={!canEdit} onClick={() => { setDeleteId(account.id); setConfirmation(""); setError(""); setSuccess(""); }}>Elimina</button>}
            </div>
            {deleteId === account.id && settings.can_manage && <div className="settings-delete-confirm">
              <p>Eliminare l’account «{account.account_name}» da questo negozio?</p>
              <label htmlFor={`${domId}-delete`}>Scrivi ELIMINA per confermare</label><input id={`${domId}-delete`} value={confirmation} disabled={!canEdit} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
              <div className="settings-actions"><button type="button" className="settings-delete" disabled={!canEdit || confirmation !== "ELIMINA"} onClick={() => { if (confirmation === "ELIMINA" && canEdit) void request("delete-account", { confirmation }, account.id); }}>{pending === "delete-account" ? "Eliminazione…" : "Elimina definitivamente"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={() => { setDeleteId(null); setConfirmation(""); }}>Annulla</button></div>
            </div>}
          </li>)}</ul> : <p className="settings-empty">Nessun account Kaufland salvato.</p>}
          {settings.can_manage && <form className="settings-form settings-add-account" onSubmit={addAccount} autoComplete="off">
            <fieldset disabled={!canEdit}>
              <legend>Aggiungi account Kaufland</legend>
              <div className="settings-fields">
                <div className="settings-field-wide"><label htmlFor={`${domId}-account`}>Nome account</label><input id={`${domId}-account`} required value={accountName} onChange={(event) => setAccountName(event.target.value)} autoComplete="off" /></div>
                <div><label htmlFor={`${domId}-client`}>Client Key</label><input id={`${domId}-client`} type="password" value={clientKey} onChange={(event) => setClientKey(event.target.value)} autoComplete="new-password" spellCheck={false} /></div>
                <div><label htmlFor={`${domId}-secret`}>Secret Key</label><input id={`${domId}-secret`} type="password" value={secretKey} onChange={(event) => setSecretKey(event.target.value)} autoComplete="new-password" spellCheck={false} /></div>
              </div>
              <p className="settings-hint">Inserisci almeno una chiave. Le credenziali vengono salvate cifrate e non vengono mostrate dopo il salvataggio.</p>
              <button className="settings-primary" type="submit" disabled={!canEdit || !accountName.trim() || !(clientKey.trim() || secretKey.trim())}>{pending === "add-account" ? "Salvataggio…" : "Salva account Kaufland"}</button>
            </fieldset>
          </form>}
        </div>
      </>}
    </div>}
  </div>;
}
