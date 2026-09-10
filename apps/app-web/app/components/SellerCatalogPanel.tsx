"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import type { FormEvent } from "react";
import {
  MAX_PRICE_LIST_FILE_BYTES,
  formatCatalogMoney,
  isAllowedPriceListFile,
  readCatalogDashboard,
  readPriceListDetail,
  type CatalogDashboard,
  type CatalogPriceList,
  type CatalogSupplier,
  type PriceListDetail,
} from "../lib/catalog-types";
import type { WorkspaceResult } from "../lib/workspace";
import { DashboardIcon } from "./DashboardIcon";

type CatalogView = "suppliers" | "price-lists";
type PendingAction = "read" | "create-supplier" | "delete-supplier" | "create-price-list" | "delete-price-list" | "detail";

const number = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 3 });
const maximumFileLabel = `${MAX_PRICE_LIST_FILE_BYTES / 1024 / 1024} MiB`;

function fallbackError(status: number, operation: PendingAction) {
  if (status === 403 || status === 404) return "Questi dati non sono più disponibili per il tuo account. Aggiorna la pagina.";
  if (status === 409) return operation === "create-supplier" ? "Esiste già un fornitore con questo nome."
    : operation === "create-price-list" ? "Esiste già un listino con questo nome."
      : "La risorsa non può essere eliminata nello stato attuale.";
  if (status === 413) return `Il file supera il limite di ${maximumFileLabel}.`;
  if (status === 422) return "Controlla i dati inseriti e riprova.";
  if (status === 502) return "Il servizio ha restituito dati non validi. Aggiorna la pagina.";
  return "Servizio catalogo momentaneamente non disponibile. Riprova tra poco.";
}

async function responseError(response: Response, operation: PendingAction) {
  const raw: unknown = await response.json().catch(() => null);
  if (raw && typeof raw === "object" && "detail" in raw && typeof raw.detail === "string" && raw.detail.length <= 300) return raw.detail;
  return fallbackError(response.status, operation);
}

function formatDate(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Rome" });
}

function sourceLabel(value: string) {
  const labels: Record<string, string> = { upload: "File caricato", file: "File caricato" };
  return labels[value.toLowerCase()] ?? value;
}

export function SellerCatalogPage({ result, view }: { result: WorkspaceResult; view: CatalogView }) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const seller = result.workspace?.active_seller;
  const title = view === "suppliers" ? {
    eyebrow: "CATALOGO",
    heading: "Fornitori",
    description: "Organizza le fonti dei prodotti e i listini associati al tuo negozio.",
  } : {
    eyebrow: "CATALOGO",
    heading: "Listini",
    description: "Importa i listini fornitori e controlla i prodotti normalizzati.",
  };
  return <div className="catalog-page">
    <div className="workspace-heading"><div><p className="eyebrow">{title.eyebrow}</p><h1>{title.heading}</h1><p>{title.description}</p></div>
      <button type="button" className="workspace-refresh" disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}><DashboardIcon name="refresh" size={16} />{refreshing ? "Aggiornamento…" : "Aggiorna workspace"}</button>
    </div>
    {result.error ? <section className="workspace-section workspace-empty"><h2>Dati momentaneamente non disponibili</h2><p className="form-error" role="alert">{result.error}</p><button type="button" className="workspace-refresh" disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}>Riprova</button></section>
      : seller ? <><div className="catalog-shop"><span><DashboardIcon name="store" size={18} /><strong>{seller.name}</strong><span>{seller.organization_name}</span></span><a href="/seller/settings/store">Cambia negozio<DashboardIcon name="chevron" size={14} /></a></div><SellerCatalogPanel key={`${seller.id}-${view}`} sellerId={seller.id} sellerName={seller.name} view={view} /></>
        : <section className="workspace-section workspace-empty"><DashboardIcon name="store" size={26} /><h2>Seleziona un negozio</h2><p>Per gestire fornitori e listini serve un negozio assegnato al tuo account.</p><a className="workspace-refresh" href="/seller/settings/store">Vai ai tuoi negozi</a></section>}
  </div>;
}

export function SellerCatalogPanel({ sellerId, sellerName, view }: { sellerId: string; sellerName: string; view: CatalogView }) {
  const router = useRouter();
  const [dashboard, setDashboard] = useState<CatalogDashboard | null>(null);
  const [detail, setDetail] = useState<PriceListDetail | null>(null);
  const [pending, setPending] = useState<PendingAction | null>("read");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [needsReload, setNeedsReload] = useState(false);
  const [supplierName, setSupplierName] = useState("");
  const [supplierNotes, setSupplierNotes] = useState("");
  const [deleteSupplier, setDeleteSupplier] = useState<CatalogSupplier | null>(null);
  const [supplierConfirmation, setSupplierConfirmation] = useState("");
  const [selectedSupplierId, setSelectedSupplierId] = useState("");
  const [priceListName, setPriceListName] = useState("");
  const [priceListFile, setPriceListFile] = useState<File | null>(null);
  const [deletePriceList, setDeletePriceList] = useState<CatalogPriceList | null>(null);
  const [priceListConfirmation, setPriceListConfirmation] = useState("");
  const alive = useRef(true);
  const version = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const previewHeading = useRef<HTMLHeadingElement>(null);
  const baseUrl = `/api/sellers/${sellerId}/catalogs`;
  const canManage = Boolean(dashboard?.can_manage) && !pending && !needsReload;
  const totalRows = dashboard?.price_lists.reduce((sum, item) => sum + item.row_count, 0) ?? 0;
  const domId = `catalog-${sellerId}`;

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => { alive.current = false; version.current += 1; controller.current?.abort(); busy.current = false; };
    // A Seller change unmounts the panel and invalidates every in-flight request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (detail) previewHeading.current?.focus(); }, [detail]);

  function current(requestVersion: number) { return alive.current && requestVersion === version.current; }
  function expireSession() {
    setDashboard(null); setDetail(null); resetSupplier(); resetPriceList();
    router.replace("/login/seller"); router.refresh();
  }
  function resetSupplier() { setSupplierName(""); setSupplierNotes(""); setDeleteSupplier(null); setSupplierConfirmation(""); }
  function resetPriceList() {
    setPriceListName(""); setPriceListFile(null); setDeletePriceList(null); setPriceListConfirmation("");
    if (fileInput.current) fileInput.current.value = "";
  }
  function acceptDashboard(value: CatalogDashboard) {
    setDashboard(value);
    setSelectedSupplierId((selected) => value.suppliers.some((item) => item.id === selected) ? selected : value.suppliers[0]?.id ?? "");
    if (detail && !value.price_lists.some((item) => item.id === detail.price_list.id)) setDetail(null);
  }

  async function refresh() {
    if (busy.current) return;
    busy.current = true;
    const requestVersion = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    setPending("read"); setError(""); setSuccess("");
    const timeout = setTimeout(() => abort.abort(), 32_000);
    try {
      const response = await fetch(baseUrl, { cache: "no-store", signal: abort.signal });
      if (!current(requestVersion)) return;
      if (response.status === 401) { expireSession(); return; }
      if (!response.ok) { setDashboard(null); setDetail(null); setError(await responseError(response, "read")); return; }
      const value = readCatalogDashboard(await response.json().catch(() => null), sellerId);
      if (!value) { setDashboard(null); setDetail(null); setError("Risposta catalogo non valida. Riprova."); return; }
      acceptDashboard(value); setNeedsReload(false);
    } catch {
      if (current(requestVersion)) { setDashboard(null); setDetail(null); setError("Impossibile caricare il catalogo. Controlla la connessione e riprova."); }
    } finally {
      clearTimeout(timeout);
      if (current(requestVersion)) { busy.current = false; setPending(null); }
    }
  }

  async function mutate({ operation, url, body, message, verify, reset }: {
    operation: Exclude<PendingAction, "read" | "detail">; url: string; body: BodyInit; message: string;
    verify: (value: CatalogDashboard) => boolean; reset: () => void;
  }) {
    if (busy.current || !canManage) return;
    busy.current = true;
    const requestVersion = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    setPending(operation); setError(""); setSuccess("");
    const timeout = setTimeout(() => abort.abort(), operation === "create-price-list" ? 68_000 : 38_000);
    try {
      const isForm = body instanceof FormData;
      const response = await fetch(url, {
        method: operation.startsWith("delete") ? "DELETE" : "POST",
        headers: isForm ? undefined : { "content-type": "application/json" }, body, cache: "no-store", signal: abort.signal,
      });
      if (!current(requestVersion)) return;
      if (response.status === 401) { expireSession(); return; }
      if (!response.ok) {
        if (response.status >= 500 || response.status === 403 || response.status === 404) setNeedsReload(true);
        setError(await responseError(response, operation));
        return;
      }
      const refreshed = await fetch(baseUrl, { cache: "no-store", signal: abort.signal });
      if (!current(requestVersion)) return;
      if (refreshed.status === 401) { expireSession(); return; }
      const value = refreshed.ok ? readCatalogDashboard(await refreshed.json().catch(() => null), sellerId) : null;
      if (!value || !verify(value)) {
        setNeedsReload(true);
        setError("Operazione ricevuta, ma l’aggiornamento non è stato confermato. Aggiorna i dati prima di continuare.");
        return;
      }
      acceptDashboard(value); setNeedsReload(false); reset(); setSuccess(message);
    } catch {
      if (current(requestVersion)) {
        setNeedsReload(true);
        setError("Operazione non confermata. Aggiorna i dati prima di riprovare.");
      }
    } finally {
      clearTimeout(timeout);
      if (current(requestVersion)) { busy.current = false; setPending(null); }
    }
  }

  async function createSupplier(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = supplierName.trim();
    if (!name || !canManage) return;
    await mutate({
      operation: "create-supplier", url: `${baseUrl}/suppliers`,
      body: JSON.stringify({ name, notes: supplierNotes.trim() }),
      message: `Fornitore ${name} creato.`,
      verify: (value) => value.suppliers.some((item) => item.name === name), reset: resetSupplier,
    });
  }

  async function removeSupplier() {
    if (!deleteSupplier || supplierConfirmation !== deleteSupplier.name || !canManage) return;
    const removed = deleteSupplier;
    await mutate({
      operation: "delete-supplier", url: `${baseUrl}/suppliers/${removed.id}`,
      body: JSON.stringify({ confirmation: supplierConfirmation }),
      message: `Fornitore ${removed.name} e ${removed.price_list_count === 1 ? "il listino collegato" : `i ${removed.price_list_count} listini collegati`} eliminati.`,
      verify: (value) => !value.suppliers.some((item) => item.id === removed.id)
        && !value.price_lists.some((item) => item.supplier_id === removed.id), reset: resetSupplier,
    });
  }

  async function createPriceList(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = priceListName.trim();
    if (!selectedSupplierId || !name || !priceListFile || !canManage) return;
    if (priceListFile.size > MAX_PRICE_LIST_FILE_BYTES) { setError(`Il file supera il limite di ${maximumFileLabel}.`); return; }
    if (!isAllowedPriceListFile(priceListFile)) { setError("Formato non supportato. Usa CSV, TXT, TSV, XLS, XLSX o XML."); return; }
    const form = new FormData();
    form.set("supplier_id", selectedSupplierId); form.set("name", name); form.set("file", priceListFile, priceListFile.name);
    const supplierId = selectedSupplierId;
    await mutate({
      operation: "create-price-list", url: `${baseUrl}/price-lists`, body: form,
      message: `Listino ${name} importato.`,
      verify: (value) => value.price_lists.some((item) => item.supplier_id === supplierId && item.name === name), reset: resetPriceList,
    });
  }

  async function removePriceList() {
    if (!deletePriceList || priceListConfirmation !== "ELIMINA" || !canManage) return;
    const removed = deletePriceList;
    await mutate({
      operation: "delete-price-list", url: `${baseUrl}/price-lists/${removed.id}`,
      body: JSON.stringify({ confirmation: "ELIMINA" }), message: `Listino ${removed.name} eliminato.`,
      verify: (value) => !value.price_lists.some((item) => item.id === removed.id), reset: resetPriceList,
    });
  }

  async function openDetail(priceList: CatalogPriceList) {
    if (busy.current) return;
    busy.current = true;
    const requestVersion = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    setPending("detail"); setError(""); setSuccess(""); setDetail(null);
    const timeout = setTimeout(() => abort.abort(), 32_000);
    try {
      const response = await fetch(`${baseUrl}/price-lists/${priceList.id}`, { cache: "no-store", signal: abort.signal });
      if (!current(requestVersion)) return;
      if (response.status === 401) { expireSession(); return; }
      if (!response.ok) { setError(await responseError(response, "detail")); return; }
      const value = readPriceListDetail(await response.json().catch(() => null), priceList.id);
      if (!value) { setError("Anteprima listino non valida. Aggiorna i dati."); return; }
      setDetail(value);
    } catch {
      if (current(requestVersion)) setError("Impossibile aprire l’anteprima. Controlla la connessione e riprova.");
    } finally {
      clearTimeout(timeout);
      if (current(requestVersion)) { busy.current = false; setPending(null); }
    }
  }

  const status = <div className="catalog-feedback" aria-live="polite">
    {error && <p className="form-error" role="alert">{error}</p>}
    {success && <p className="settings-success" role="status">{success}</p>}
    {(needsReload || (error && !dashboard)) && <button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={refresh}><DashboardIcon name="refresh" size={14} />Aggiorna i dati</button>}
  </div>;

  return <div className="catalog-workspace" aria-busy={Boolean(pending)}>
    <div className="catalog-summary" aria-label="Riepilogo catalogo">
      <div><span className="catalog-summary-icon"><DashboardIcon name="supplier" size={20} /></span><span>Fornitori</span><strong>{dashboard ? dashboard.suppliers.length : "—"}</strong></div>
      <div><span className="catalog-summary-icon is-green"><DashboardIcon name="file" size={20} /></span><span>Listini</span><strong>{dashboard ? dashboard.price_lists.length : "—"}</strong></div>
      <div><span className="catalog-summary-icon is-violet"><DashboardIcon name="catalog" size={20} /></span><span>Prodotti indicizzati</span><strong>{dashboard ? number.format(totalRows) : "—"}</strong></div>
    </div>
    {pending === "read" && !dashboard && <section className="workspace-section catalog-loading" role="status"><span className="catalog-spinner" aria-hidden="true" />Caricamento del catalogo…</section>}
    {status}
    {dashboard && !dashboard.can_manage && <p className="settings-notice">Accesso in sola lettura. Puoi consultare fornitori, listini e anteprime; per modificarli serve l’autorizzazione Catalogo.</p>}

    {dashboard && view === "suppliers" && <div className="catalog-layout">
      <section className="workspace-section catalog-form-card" aria-labelledby={`${domId}-supplier-form-title`}>
        <div className="workspace-section-heading"><h2 id={`${domId}-supplier-form-title`}><DashboardIcon name="supplier" />Nuovo fornitore</h2></div>
        <div className="workspace-section-body">
          <p className="catalog-help">Crea il fornitore prima di associare uno o più listini.</p>
          {dashboard.can_manage ? <form className="catalog-form" onSubmit={createSupplier}><fieldset disabled={!canManage}><legend className="visually-hidden">Dati del nuovo fornitore</legend>
            <label htmlFor={`${domId}-supplier-name`}>Nome fornitore</label><input id={`${domId}-supplier-name`} required maxLength={200} value={supplierName} onChange={(event) => { setSupplierName(event.target.value); setError(""); }} />
            <label htmlFor={`${domId}-supplier-notes`}>Note <span>facoltative</span></label><textarea id={`${domId}-supplier-notes`} maxLength={5000} rows={5} value={supplierNotes} onChange={(event) => setSupplierNotes(event.target.value)} />
            <div className="catalog-form-actions"><button type="submit" className="settings-primary" disabled={!canManage || !supplierName.trim()}>{pending === "create-supplier" ? "Creazione…" : "Crea fornitore"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending) || (!supplierName && !supplierNotes)} onClick={resetSupplier}>Azzera</button></div>
          </fieldset></form> : <p className="catalog-help">Il tuo account può consultare i fornitori ma non crearli.</p>}
        </div>
      </section>
      <section className="workspace-section catalog-list-card" aria-labelledby={`${domId}-suppliers-title`}>
        <div className="workspace-section-heading"><h2 id={`${domId}-suppliers-title`}><DashboardIcon name="supplier" />Fornitori del negozio <span className="count-badge">{dashboard.suppliers.length}</span></h2><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={refresh}><DashboardIcon name="refresh" size={14} />Aggiorna</button></div>
        <div className="workspace-section-body">
          {dashboard.suppliers.length ? <ul className="catalog-supplier-list">{dashboard.suppliers.map((supplier) => <li key={supplier.id}>
            <div className="catalog-supplier-row"><span className="catalog-supplier-mark" aria-hidden="true">{supplier.name.slice(0, 1).toUpperCase()}</span><div><strong>{supplier.name}</strong><p>{supplier.notes || "Nessuna nota"}</p><span>{supplier.price_list_count === 1 ? "1 listino collegato" : `${supplier.price_list_count} listini collegati`}</span></div>
              {dashboard.can_manage && <button type="button" className="settings-delete" disabled={!canManage} onClick={() => { setDeleteSupplier(supplier); setSupplierConfirmation(""); setError(""); }}>Elimina</button>}</div>
            {deleteSupplier?.id === supplier.id && <div className="catalog-delete-confirm"><strong>Eliminazione definitiva</strong><p>Eliminando {supplier.name} saranno eliminati anche {supplier.price_list_count === 1 ? "il listino collegato" : `tutti i ${supplier.price_list_count} listini collegati`}. Scrivi esattamente <b>{supplier.name}</b> per confermare.</p>
              <label htmlFor={`${domId}-delete-supplier`}>Nome esatto del fornitore</label><input id={`${domId}-delete-supplier`} autoFocus autoComplete="off" value={supplierConfirmation} disabled={!canManage} onChange={(event) => setSupplierConfirmation(event.target.value)} />
              <div className="catalog-form-actions"><button type="button" className="settings-delete" disabled={!canManage || supplierConfirmation !== supplier.name} onClick={removeSupplier}>{pending === "delete-supplier" ? "Eliminazione…" : "Elimina fornitore e listini"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={() => { setDeleteSupplier(null); setSupplierConfirmation(""); }}>Annulla</button></div>
            </div>}
          </li>)}</ul> : <div className="catalog-empty"><DashboardIcon name="supplier" size={24} /><strong>Nessun fornitore</strong><p>Crea il primo fornitore per iniziare a importare i listini.</p></div>}
        </div>
      </section>
    </div>}

    {dashboard && view === "price-lists" && <>
      <section className="workspace-section catalog-import-card" aria-labelledby={`${domId}-import-title`}>
        <div className="workspace-section-heading"><h2 id={`${domId}-import-title`}><DashboardIcon name="file" />Importa un listino</h2><span className="catalog-format-badge">CSV · TXT · TSV · XLS · XLSX · XML</span></div>
        <div className="workspace-section-body">
          {dashboard.suppliers.length ? dashboard.can_manage ? <form className="catalog-form catalog-import-form" onSubmit={createPriceList}><fieldset disabled={!canManage}><legend className="visually-hidden">Dati del listino da importare</legend>
            <div><label htmlFor={`${domId}-list-supplier`}>Fornitore</label><select id={`${domId}-list-supplier`} required value={selectedSupplierId} onChange={(event) => { setSelectedSupplierId(event.target.value); setError(""); }}>{dashboard.suppliers.map((supplier) => <option key={supplier.id} value={supplier.id}>{supplier.name}</option>)}</select></div>
            <div><label htmlFor={`${domId}-list-name`}>Nome listino</label><input id={`${domId}-list-name`} required maxLength={200} value={priceListName} onChange={(event) => { setPriceListName(event.target.value); setError(""); }} /></div>
            <div className="catalog-file-field"><label htmlFor={`${domId}-list-file`}>File del listino</label><input ref={fileInput} id={`${domId}-list-file`} type="file" required accept=".csv,.txt,.tsv,.xls,.xlsx,.xml" onChange={(event) => { setPriceListFile(event.target.files?.[0] ?? null); setError(""); }} /><p>{priceListFile ? `${priceListFile.name} · ${number.format(priceListFile.size / 1024)} KiB` : `Dimensione massima ${maximumFileLabel}. I file PKL non sono accettati.`}</p></div>
            <div className="catalog-form-actions"><button type="submit" className="settings-primary" disabled={!canManage || !selectedSupplierId || !priceListName.trim() || !priceListFile}>{pending === "create-price-list" ? "Importazione…" : "Importa listino"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending) || (!priceListName && !priceListFile)} onClick={resetPriceList}>Azzera</button></div>
          </fieldset></form> : <p className="catalog-help">Il tuo account può consultare i listini ma non importarli.</p>
            : <div className="catalog-empty"><DashboardIcon name="supplier" size={24} /><strong>Crea prima un fornitore</strong><p>Ogni listino deve appartenere a un fornitore del negozio.</p><a className="workspace-refresh" href="/seller/catalog/suppliers">Vai ai fornitori</a></div>}
        </div>
      </section>
      <section className="workspace-section catalog-list-card" aria-labelledby={`${domId}-lists-title`}>
        <div className="workspace-section-heading"><h2 id={`${domId}-lists-title`}><DashboardIcon name="file" />Listini importati <span className="count-badge">{dashboard.price_lists.length}</span></h2><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={refresh}><DashboardIcon name="refresh" size={14} />Aggiorna</button></div>
        {dashboard.price_lists.length ? <div className="catalog-table-wrap" tabIndex={0} role="region" aria-label="Tabella dei listini, scorrimento orizzontale"><table className="catalog-table"><caption className="visually-hidden">Listini importati per {sellerName}</caption><thead><tr><th scope="col">Listino</th><th scope="col">Fornitore</th><th scope="col">File</th><th scope="col">Prodotti</th><th scope="col">Stato</th><th scope="col">Aggiornato</th><th scope="col"><span className="visually-hidden">Azioni</span></th></tr></thead><tbody>{dashboard.price_lists.map((priceList) => <tr key={priceList.id}><td><strong>{priceList.name}</strong><span>{sourceLabel(priceList.source_type)}</span></td><td>{priceList.supplier_name}</td><td><span className="catalog-file-name">{priceList.file_name}</span><span>{priceList.file_format.toUpperCase()}</span></td><td>{number.format(priceList.row_count)}</td><td><span className={`catalog-state state-${priceList.status.toLowerCase().replace(/[^a-z0-9-]/g, "-")}`}>{priceList.status}</span></td><td>{formatDate(priceList.updated_at)}</td><td><div className="catalog-row-actions"><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={() => void openDetail(priceList)}>{pending === "detail" ? "Apertura…" : "Anteprima"}</button>{dashboard.can_manage && <button type="button" className="settings-delete" disabled={!canManage} onClick={() => { setDeletePriceList(priceList); setPriceListConfirmation(""); setError(""); }}>Elimina</button>}</div></td></tr>)}</tbody></table></div>
          : <div className="workspace-section-body catalog-empty"><DashboardIcon name="file" size={24} /><strong>Nessun listino importato</strong><p>Carica un file per visualizzare qui prodotti, formato e stato.</p></div>}
        {deletePriceList && <div className="catalog-delete-confirm catalog-list-delete"><strong>Eliminare «{deletePriceList.name}»?</strong><p>Il listino e i dati importati saranno rimossi. Scrivi ELIMINA per confermare.</p><label htmlFor={`${domId}-delete-list`}>Conferma eliminazione</label><input id={`${domId}-delete-list`} autoFocus autoComplete="off" value={priceListConfirmation} disabled={!canManage} onChange={(event) => setPriceListConfirmation(event.target.value)} /><div className="catalog-form-actions"><button type="button" className="settings-delete" disabled={!canManage || priceListConfirmation !== "ELIMINA"} onClick={removePriceList}>{pending === "delete-price-list" ? "Eliminazione…" : "Elimina definitivamente"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={() => { setDeletePriceList(null); setPriceListConfirmation(""); }}>Annulla</button></div></div>}
      </section>
      {detail && <section className="workspace-section catalog-preview" aria-labelledby={`${domId}-preview-title`}>
        <div className="workspace-section-heading"><div><h2 ref={previewHeading} tabIndex={-1} id={`${domId}-preview-title`}><DashboardIcon name="catalog" />Anteprima · {detail.price_list.name}</h2><p>Prime {detail.products.length} righe su {number.format(detail.total)} prodotti.</p></div><button type="button" className="workspace-refresh" onClick={() => setDetail(null)}><DashboardIcon name="close" size={14} />Chiudi</button></div>
        {detail.products.length ? <div className="catalog-table-wrap" tabIndex={0} role="region" aria-label="Anteprima prodotti, scorrimento orizzontale"><table className="catalog-table catalog-products"><caption className="visually-hidden">Anteprima prodotti del listino {detail.price_list.name}</caption><thead><tr><th scope="col">Nome prodotto</th><th scope="col">EAN</th><th scope="col">SKU</th><th scope="col">Costo</th><th scope="col">Spedizione</th><th scope="col">Costo totale</th><th scope="col">Quantità</th></tr></thead><tbody>{detail.products.map((product, index) => <tr key={`${product.ean}-${product.sku}-${index}`}><td>{product.name || "—"}</td><td>{product.ean || "—"}</td><td>{product.sku || "—"}</td><td>{formatCatalogMoney(product.cost)}</td><td>{formatCatalogMoney(product.shipping_cost)}</td><td>{formatCatalogMoney(product.total_cost)}</td><td>{product.quantity === null ? "—" : number.format(Number(product.quantity))}</td></tr>)}</tbody></table></div>
          : <div className="workspace-section-body catalog-empty"><strong>Nessun prodotto disponibile</strong><p>Il listino non contiene righe visualizzabili.</p></div>}
      </section>}
    </>}
  </div>;
}
