"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { readSellerSettings, type SellerSettings } from "../lib/seller-settings-types";
import { DashboardIcon } from "./DashboardIcon";

type ProfileDraft = { name: string; legal_name: string; email: string };
const emptyDraft: ProfileDraft = { name: "", legal_name: "", email: "" };
const toDraft = (value: SellerSettings): ProfileDraft => ({ name: value.name, legal_name: value.legal_name ?? "", email: value.email ?? "" });

/** Mounted with the Seller id as React key, so switching shops destroys drafts and credential inputs. */
export function SellerSettingsPanel({ sellerId, loginPath, onSaved, marketplacePath, initiallyExpanded = false }: { sellerId: string; loginPath: string; onSaved: () => void; marketplacePath?: string; initiallyExpanded?: boolean }) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [settings, setSettings] = useState<SellerSettings | null>(null);
  const [draft, setDraft] = useState<ProfileDraft>(emptyDraft);
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
    return () => { active.current = false; requestVersion.current += 1; controller.current?.abort(); operationActive.current = false; };
  }, []);

  useEffect(() => {
    if (expanded) void load();
    // Loading starts on opening; retry uses the explicit refresh action.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expanded]);

  async function request(operation: "read" | "save", body?: object) {
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
      const response = await fetch(`/api/sellers/${sellerId}/settings`, {
        method: operation === "read" ? "GET" : "PUT",
        headers: { "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined,
        cache: "no-store", signal: abort.signal,
      });
      if (!current()) return;
      if (response.status === 401) {
        setSettings(null);
        router.replace(loginPath); router.refresh();
        return;
      }
      if (!response.ok) {
        if (response.status === 403 || response.status === 404) {
          setSettings(null);
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
      if (operation !== "read") {
        setSuccess("Dati del negozio salvati.");
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

  async function load() { await request("read"); }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit) return;
    if (!draft.name.trim()) { setError("Inserisci il nome del negozio."); return; }
    await request("save", { name: draft.name.trim(), legal_name: draft.legal_name.trim(), email: draft.email.trim() });
  }

  return <div className="seller-settings">
    {!initiallyExpanded && <button type="button" className="workspace-refresh settings-toggle" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-controls={domId} disabled={Boolean(pending)}>
      <DashboardIcon name="store" size={15} />{expanded ? "Chiudi impostazioni" : "Gestisci negozio"}
    </button>}
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
        {marketplacePath && <div className="settings-marketplace-link"><div><h3>Marketplace del negozio</h3><p className="settings-hint">Scegli i canali di vendita e gestisci i collegamenti API nella sezione dedicata.</p></div><a className="workspace-refresh" href={marketplacePath}><DashboardIcon name="plug" size={16} />Collega marketplace<DashboardIcon name="arrow" size={15} /></a></div>}
      </>}
    </div>}
  </div>;
}
