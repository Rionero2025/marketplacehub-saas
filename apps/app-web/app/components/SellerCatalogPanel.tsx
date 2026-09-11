"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useTransition } from "react";
import type { FormEvent } from "react";
import {
  MAX_PRICE_LIST_FILE_BYTES,
  formatCatalogMoney,
  isAllowedPriceListFile,
  readCatalogFeedJobResponse,
  readCatalogFeedMutation,
  readCatalogDashboard,
  readPriceListDetail,
  readUpdateUrlPriceListInput,
  urlFeedHostMatches,
  type CatalogCredentialMode,
  type CatalogDashboard,
  type CatalogFeedRole,
  type CatalogFeedJob,
  type CatalogPriceList,
  type CatalogProvider,
  type CatalogSupplier,
  type PriceListDetail,
} from "../lib/catalog-types";
import type { WorkspaceResult } from "../lib/workspace";
import { DashboardIcon } from "./DashboardIcon";
import { CatalogJobProgress } from "./CatalogJobProgress";

type CatalogView = "suppliers" | "price-lists";
type PriceListSource = "file" | "url";
type PendingAction = "read" | "create-supplier" | "delete-supplier" | "create-price-list"
  | "create-price-list-url" | "update-price-list-url" | "refresh-price-list" | "delete-price-list" | "detail";

const number = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 3 });
const maximumFileLabel = `${MAX_PRICE_LIST_FILE_BYTES / 1024 / 1024} MiB`;

function fallbackError(status: number, operation: PendingAction) {
  if (status === 403 || status === 404) return "Questi dati non sono più disponibili per il tuo account. Aggiorna la pagina.";
  if (status === 409) return operation === "create-supplier" ? "Esiste già un fornitore con questo nome."
    : operation === "create-price-list" || operation === "create-price-list-url" ? "Esiste già un listino con questo nome."
      : operation === "update-price-list-url" ? "La configurazione del feed è cambiata. Aggiorna i dati e riprova."
      : operation === "refresh-price-list" ? "Un aggiornamento di questo listino è già in corso."
      : "La risorsa non può essere eliminata nello stato attuale.";
  if (status === 413) return `Il file supera il limite di ${maximumFileLabel}.`;
  if (status === 429) return "Il servizio di importazione listini è occupato. Attendi e riprova.";
  if (status === 408) return "Tempo massimo superato. Riprova.";
  if (status === 422) return "Controlla i dati inseriti e riprova.";
  if (status === 502) return "Il servizio ha restituito dati non validi. Aggiorna la pagina.";
  return "Servizio catalogo momentaneamente non disponibile. Riprova tra poco.";
}

async function responseError(response: Response, operation: PendingAction) {
  const raw: unknown = await response.json().catch(() => null);
  if (raw && typeof raw === "object" && "detail" in raw && typeof raw.detail === "string" && raw.detail.length <= 300) return raw.detail;
  return fallbackError(response.status, operation);
}

function formatDate(value: string | null) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Rome" });
}

function sourceLabel(value: string) {
  const labels: Record<string, string> = { upload: "File caricato", file: "File caricato", url: "Feed URL" };
  return labels[value.toLowerCase()] ?? value;
}

function jobIsActive(job: CatalogFeedJob | null) {
  return Boolean(job && ["queued", "pending", "running"].includes(job.status));
}

function hasActiveCatalog(priceList: CatalogPriceList) {
  return priceList.status === "ready" || (priceList.active_version_number ?? 0) > 0;
}

function statusLabel(priceList: CatalogPriceList) {
  const status = priceList.latest_job?.status ?? priceList.status;
  return ({ queued: "In attesa", pending: "In attesa", running: "In corso", done: "Pronto", ready: "Pronto", error: "Errore" } as Record<string, string>)[status] ?? "In attesa";
}

function statusClass(priceList: CatalogPriceList) {
  const status = priceList.latest_job?.status ?? priceList.status;
  if (status === "queued" || status === "pending") return "pending";
  if (status === "done") return "ready";
  return status;
}

function providerLabel(value: CatalogProvider) {
  return value === "innpro" ? "InnPro IOF" : "Generico";
}

function feedRoleLabel(value: CatalogFeedRole) {
  return value === "full" ? "FULL" : value === "light" ? "LIGHT" : "Standard";
}

function feedRoleDescription(provider: CatalogProvider, role: CatalogFeedRole) {
  if (provider === "generic") return "Usa questa modalità per CSV, fogli di calcolo, XML e feed di fornitori diversi da InnPro.";
  return role === "full"
    ? "FULL importa schede prodotto, descrizioni, immagini, categorie, varianti e dati tecnici."
    : "LIGHT importa prezzi di acquisto e disponibilità: è la fonte InnPro usata dalla contabilità.";
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
  const [priceListSource, setPriceListSource] = useState<PriceListSource>("file");
  const [priceListProvider, setPriceListProvider] = useState<CatalogProvider>("generic");
  const [priceListFeedRole, setPriceListFeedRole] = useState<CatalogFeedRole>("standard");
  const [priceListFile, setPriceListFile] = useState<File | null>(null);
  const [feedUrl, setFeedUrl] = useState("");
  const [feedUsername, setFeedUsername] = useState("");
  const [feedPassword, setFeedPassword] = useState("");
  const [editPriceList, setEditPriceList] = useState<CatalogPriceList | null>(null);
  const [editFeedUrl, setEditFeedUrl] = useState("");
  const [editCredentialMode, setEditCredentialMode] = useState<CatalogCredentialMode>("keep");
  const [editFeedUsername, setEditFeedUsername] = useState("");
  const [editFeedPassword, setEditFeedPassword] = useState("");
  const [deletePriceList, setDeletePriceList] = useState<CatalogPriceList | null>(null);
  const [priceListConfirmation, setPriceListConfirmation] = useState("");
  const alive = useRef(true);
  const version = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const pollControllers = useRef(new Set<AbortController>());
  const busy = useRef(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const previewHeading = useRef<HTMLHeadingElement>(null);
  const baseUrl = `/api/sellers/${sellerId}/catalogs`;
  const canManage = Boolean(dashboard?.can_manage) && !pending && !needsReload;
  const totalRows = dashboard?.price_lists.reduce((sum, item) => sum + (item.row_count ?? 0), 0) ?? 0;
  const activeFeedJobs = dashboard?.price_lists
    .filter((item) => jobIsActive(item.latest_job))
    .map((item) => ({ priceListId: item.id, jobId: item.latest_job!.id })) ?? [];
  const activeFeedJobKey = activeFeedJobs.map((item) => `${item.priceListId}:${item.jobId}`).join("|");
  const domId = `catalog-${sellerId}`;

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false; version.current += 1; controller.current?.abort();
      for (const pollController of pollControllers.current) pollController.abort();
      pollControllers.current.clear(); busy.current = false;
    };
    // A Seller change unmounts the panel and invalidates every in-flight request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (detail) previewHeading.current?.focus(); }, [detail]);
  useEffect(() => {
    if (!activeFeedJobKey) return;
    const jobs = activeFeedJobs;
    const abort = new AbortController();
    pollControllers.current.add(abort);
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const schedule = (delay: number) => {
      if (!stopped && !abort.signal.aborted) timer = setTimeout(() => void poll(), delay);
    };
    const patchJobs = (updates: Array<{ priceListId: string; job: CatalogFeedJob }>) => {
      setDashboard((previous) => previous ? {
        ...previous,
        price_lists: previous.price_lists.map((priceList) => {
          const update = updates.find((candidate) => candidate.priceListId === priceList.id);
          if (!update) return priceList;
          const completed = update.job.status === "done" || update.job.status === "ready";
          const nextStatus: CatalogPriceList["status"] = completed ? "ready"
            : update.job.status === "queued" ? "queued"
              : update.job.status === "pending" ? "pending"
                : update.job.status === "running" ? "running" : "error";
          return {
            ...priceList,
            status: nextStatus,
            active_version_number: update.job.result_version ?? priceList.active_version_number,
            last_checked_at: update.job.updated_at,
            last_success_at: completed ? update.job.updated_at : priceList.last_success_at,
            latest_job: update.job,
          };
        }),
      } : previous);
    };
    const poll = async () => {
      const updates: Array<{ priceListId: string; job: CatalogFeedJob }> = [];
      let transientFailure = false;
      for (const item of jobs) {
        try {
          const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(20_000)]);
          const response = await fetch(`${baseUrl}/price-lists/${item.priceListId}/jobs/${item.jobId}`, {
            cache: "no-store", signal,
          });
          if (stopped || abort.signal.aborted) return;
          if (response.status === 401) { stopped = true; expireSession(); return; }
          if (!response.ok) { transientFailure = true; continue; }
          const job = readCatalogFeedJobResponse(await response.json().catch(() => null), item.jobId, item.priceListId);
          if (!job) { transientFailure = true; continue; }
          updates.push({ priceListId: item.priceListId, job });
        } catch {
          if (!abort.signal.aborted) transientFailure = true;
        }
      }
      if (stopped || abort.signal.aborted) return;
      const completed = updates.some(({ job }) => job.status === "done" || job.status === "ready");
      let dashboardStillActive = false;
      if (completed && !busy.current) {
        try {
          const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(20_000)]);
          const response = await fetch(baseUrl, { cache: "no-store", signal });
          if (response.status === 401) { stopped = true; expireSession(); return; }
          const value = response.ok ? readCatalogDashboard(await response.json().catch(() => null), sellerId) : null;
          if (value && !stopped && !abort.signal.aborted && !busy.current) {
            dashboardStillActive = jobs.some((item) => {
              const priceList = value.price_lists.find((candidate) => candidate.id === item.priceListId);
              return priceList?.latest_job?.id === item.jobId && jobIsActive(priceList.latest_job);
            });
            acceptDashboard(value);
          } else {
            transientFailure = true;
            patchJobs(updates.filter(({ job }) => job.status !== "done" && job.status !== "ready"));
          }
        } catch {
          if (!abort.signal.aborted) {
            transientFailure = true;
            patchJobs(updates.filter(({ job }) => job.status !== "done" && job.status !== "ready"));
          }
        }
      } else {
        if (completed) {
          // Keep successful jobs active until the authoritative dashboard can be reloaded.
          // Otherwise the effect would unmount itself and leave row counts/version data stale.
          transientFailure = true;
          patchJobs(updates.filter(({ job }) => job.status !== "done" && job.status !== "ready"));
        } else {
          patchJobs(updates);
        }
      }
      const failed = updates.find(({ job }) => job.status === "error");
      if (failed && !busy.current) setError(failed.job.message ?? "L’aggiornamento del listino non è riuscito.");
      if (transientFailure || dashboardStillActive || updates.some(({ job }) => jobIsActive(job))) schedule(transientFailure ? 3_000 : 1_500);
    };
    schedule(700);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      abort.abort();
      pollControllers.current.delete(abort);
    };
    // The key changes only when a polled job starts or becomes terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeFeedJobKey]);

  function current(requestVersion: number) { return alive.current && requestVersion === version.current; }
  function expireSession() {
    setDashboard(null); setDetail(null); resetSupplier(); resetPriceList(); resetFeedEditor();
    router.replace("/login/seller"); router.refresh();
  }
  function resetSupplier() { setSupplierName(""); setSupplierNotes(""); setDeleteSupplier(null); setSupplierConfirmation(""); }
  function resetPriceList() {
    setPriceListName(""); setPriceListFile(null); setFeedUrl(""); setFeedUsername(""); setFeedPassword("");
    setPriceListProvider("generic"); setPriceListFeedRole("standard");
    setDeletePriceList(null); setPriceListConfirmation("");
    if (fileInput.current) fileInput.current.value = "";
  }
  function resetFeedEditor() {
    setEditPriceList(null); setEditFeedUrl(""); setEditCredentialMode("keep");
    setEditFeedUsername(""); setEditFeedPassword("");
  }
  function openFeedEditor(priceList: CatalogPriceList) {
    if (priceList.source_type !== "url" || jobIsActive(priceList.latest_job)) return;
    setEditPriceList(priceList); setEditFeedUrl(""); setEditCredentialMode("keep");
    setEditFeedUsername(""); setEditFeedPassword(""); setDeletePriceList(null); setPriceListConfirmation("");
    setError(""); setSuccess("");
  }
  function acceptDashboard(value: CatalogDashboard) {
    setDashboard(value);
    setSelectedSupplierId((selected) => value.suppliers.some((item) => item.id === selected) ? selected : value.suppliers[0]?.id ?? "");
    if (detail && !value.price_lists.some((item) => item.id === detail.price_list.id)) setDetail(null);
    // Preserve the revision captured when editing started. Silently rebasing it here
    // would defeat the server-side compare-and-swap protection against another tab.
    setEditPriceList((editing) => editing
      ? value.price_lists.some((item) => item.id === editing.id && item.source_type === "url") ? editing : null
      : null);
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
        if (response.status >= 500 || response.status === 403 || response.status === 404
          || (operation === "update-price-list-url" && response.status === 409)) setNeedsReload(true);
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
    if (!selectedSupplierId || !name || !canManage) return;
    if (priceListSource === "url") {
      const url = feedUrl.trim();
      const username = feedUsername.trim();
      if (!url || Boolean(username) !== Boolean(feedPassword)) {
        setError("Indica un URL HTTPS e compila entrambe le credenziali Basic, oppure lasciale vuote.");
        return;
      }
      try {
        const parsed = new URL(url);
        if (parsed.protocol !== "https:" || !parsed.hostname.includes(".") || parsed.username || parsed.password
          || parsed.hash || (parsed.port && parsed.port !== "443")) throw new Error();
      } catch {
        setError("Indica un URL HTTPS pubblico valido, senza credenziali o frammenti nell’indirizzo.");
        return;
      }
      const password = feedPassword;
      setFeedPassword("");
      await enqueueFeed({
        operation: "create-price-list-url", url: `${baseUrl}/price-lists`,
        payload: {
          supplier_id: selectedSupplierId, name, provider: priceListProvider,
          feed_role: priceListFeedRole, url, username, password,
        },
        message: `Feed ${name} registrato. L’importazione è in corso.`, reset: resetPriceList,
      });
      return;
    }
    if (!priceListFile) return;
    if (priceListFile.size > MAX_PRICE_LIST_FILE_BYTES) { setError(`Il file supera il limite di ${maximumFileLabel}.`); return; }
    if (!isAllowedPriceListFile(priceListFile)) { setError("Formato non supportato. Usa CSV, TXT, TSV, XLS, XLSX o XML."); return; }
    const form = new FormData();
    form.set("supplier_id", selectedSupplierId); form.set("name", name);
    form.set("provider", priceListProvider); form.set("feed_role", priceListFeedRole);
    form.set("file", priceListFile, priceListFile.name);
    const supplierId = selectedSupplierId;
    await mutate({
      operation: "create-price-list", url: `${baseUrl}/price-lists`, body: form,
      message: `Listino ${name} importato.`,
      verify: (value) => value.price_lists.some((item) => item.supplier_id === supplierId && item.name === name), reset: resetPriceList,
    });
  }

  async function enqueueFeed({ operation, url, payload, message, expectedPriceListId, reset }: {
    operation: "create-price-list-url" | "refresh-price-list";
    url: string;
    payload: Record<string, unknown>;
    message: string;
    expectedPriceListId?: string;
    reset?: () => void;
  }) {
    if (busy.current || !canManage) return;
    busy.current = true;
    const requestVersion = ++version.current;
    const abort = new AbortController();
    controller.current = abort;
    setPending(operation); setError(""); setSuccess("");
    const timeout = setTimeout(() => abort.abort(), 38_000);
    try {
      const response = await fetch(url, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
        cache: "no-store", signal: abort.signal,
      });
      if (!current(requestVersion)) return;
      if (response.status === 401) { expireSession(); return; }
      if (!response.ok) {
        if (response.status >= 500 || response.status === 403 || response.status === 404) setNeedsReload(true);
        setError(await responseError(response, operation));
        return;
      }
      const value = readCatalogFeedMutation(await response.json().catch(() => null), expectedPriceListId);
      if (!value || !dashboard?.suppliers.some((supplier) => supplier.id === value.price_list.supplier_id)) {
        setNeedsReload(true); setError("Operazione ricevuta, ma la risposta non è valida. Aggiorna i dati."); return;
      }
      const nextPriceList = { ...value.price_list, latest_job: value.job };
      setDashboard((previous) => {
        if (!previous) return previous;
        const exists = previous.price_lists.some((item) => item.id === nextPriceList.id);
        return {
          ...previous,
          price_lists: exists
            ? previous.price_lists.map((item) => item.id === nextPriceList.id ? nextPriceList : item)
            : [...previous.price_lists, nextPriceList],
        };
      });
      if (detail?.price_list.id === nextPriceList.id) setDetail(null);
      setNeedsReload(false); reset?.(); setSuccess(message);
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

  async function refreshFeed(priceList: CatalogPriceList) {
    if (priceList.source_type !== "url" || jobIsActive(priceList.latest_job)) return;
    await enqueueFeed({
      operation: "refresh-price-list", url: `${baseUrl}/price-lists/${priceList.id}/refresh`, payload: {},
      expectedPriceListId: priceList.id,
      message: `Aggiornamento di ${priceList.name} avviato.`,
    });
  }

  async function updateFeed(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editPriceList || !canManage || jobIsActive(editPriceList.latest_job)) return;
    const input = readUpdateUrlPriceListInput({
      url: editFeedUrl,
      credentials_mode: editCredentialMode,
      expected_config_revision: editPriceList.source_config_revision,
      ...(editCredentialMode === "replace" ? { username: editFeedUsername, password: editFeedPassword } : {}),
    });
    if (!input) {
      setError(editCredentialMode === "replace"
        ? "Indica un URL HTTPS valido e compila username e password Basic."
        : "Indica il nuovo URL HTTPS completo del feed.");
      return;
    }
    const edited = editPriceList;
    if (input.credentials_mode === "keep" && !urlFeedHostMatches(input.url, edited.source_host ?? "")) {
      setError("Il dominio del feed è cambiato. Scegli Sostituisci oppure Rimuovi per proteggere le credenziali salvate.");
      return;
    }
    const nextHost = new URL(input.url).hostname.toLowerCase();
    setEditFeedUsername(""); setEditFeedPassword("");
    await mutate({
      operation: "update-price-list-url",
      url: `${baseUrl}/price-lists/${edited.id}/url`,
      body: JSON.stringify(input),
      message: `Configurazione di ${edited.name} salvata. Il listino attivo resta disponibile finché non avvii un aggiornamento.`,
      verify: (value) => value.price_lists.some((item) => item.id === edited.id
        && item.source_type === "url" && item.source_host?.toLowerCase() === nextHost
        && item.source_config_revision > edited.source_config_revision),
      reset: resetFeedEditor,
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
    if (busy.current || !hasActiveCatalog(priceList)) return;
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
        <div className="workspace-section-heading"><h2 id={`${domId}-import-title`}><DashboardIcon name="file" />Importa un listino</h2><span className="catalog-format-badge">FILE O FEED HTTPS</span></div>
        <div className="workspace-section-body">
          {dashboard.suppliers.length ? dashboard.can_manage ? <form className="catalog-form catalog-import-form" onSubmit={createPriceList}><fieldset disabled={!canManage}><legend className="visually-hidden">Dati del listino da importare</legend>
            <div className="catalog-source-choice" role="group" aria-label="Origine del listino"><span>Origine del listino</span><button type="button" aria-pressed={priceListSource === "file"} className={priceListSource === "file" ? "is-active" : ""} onClick={() => { setPriceListSource("file"); setFeedUrl(""); setFeedUsername(""); setFeedPassword(""); setError(""); }}>File</button><button type="button" aria-pressed={priceListSource === "url"} className={priceListSource === "url" ? "is-active" : ""} onClick={() => { setPriceListSource("url"); setPriceListFile(null); if (fileInput.current) fileInput.current.value = ""; setError(""); }}>URL</button></div>
            <div className="catalog-profile-config">
              <div><label htmlFor={`${domId}-list-provider`}>Tipo di listino</label><select id={`${domId}-list-provider`} aria-describedby={`${domId}-list-profile-help`} value={priceListProvider} onChange={(event) => { const provider = event.target.value as CatalogProvider; setPriceListProvider(provider); setPriceListFeedRole(provider === "innpro" ? "full" : "standard"); setError(""); }}><option value="generic">Generico</option><option value="innpro">InnPro IOF</option></select></div>
              {priceListProvider === "innpro" && <div><label htmlFor={`${domId}-list-feed-role`}>Ruolo del feed InnPro</label><select id={`${domId}-list-feed-role`} aria-describedby={`${domId}-list-profile-help`} value={priceListFeedRole} onChange={(event) => { setPriceListFeedRole(event.target.value as CatalogFeedRole); setError(""); }}><option value="full">FULL · contenuti prodotto</option><option value="light">LIGHT · prezzi e disponibilità</option></select></div>}
              <p id={`${domId}-list-profile-help`}>{feedRoleDescription(priceListProvider, priceListFeedRole)}</p>
            </div>
            <div><label htmlFor={`${domId}-list-supplier`}>Fornitore</label><select id={`${domId}-list-supplier`} required value={selectedSupplierId} onChange={(event) => { setSelectedSupplierId(event.target.value); setError(""); }}>{dashboard.suppliers.map((supplier) => <option key={supplier.id} value={supplier.id}>{supplier.name}</option>)}</select></div>
            <div><label htmlFor={`${domId}-list-name`}>Nome listino</label><input id={`${domId}-list-name`} required maxLength={200} value={priceListName} onChange={(event) => { setPriceListName(event.target.value); setError(""); }} /></div>
            <div className="catalog-source-fields">{priceListSource === "file" ? <div className="catalog-file-field"><label htmlFor={`${domId}-list-file`}>File del listino</label><input ref={fileInput} id={`${domId}-list-file`} type="file" required accept=".csv,.txt,.tsv,.xls,.xlsx,.xml" onChange={(event) => { setPriceListFile(event.target.files?.[0] ?? null); setError(""); }} /><p>{priceListFile ? `${priceListFile.name} · ${number.format(priceListFile.size / 1024)} KiB` : `Dimensione massima ${maximumFileLabel}. I file PKL non sono accettati.`}</p></div> : <>
              <div className="catalog-url-primary"><label htmlFor={`${domId}-list-url`}>URL HTTPS del feed</label><input id={`${domId}-list-url`} type="url" inputMode="url" required maxLength={4096} placeholder="https://fornitore.example/listino.csv" value={feedUrl} onChange={(event) => { setFeedUrl(event.target.value); setError(""); }} /></div>
              <div><label htmlFor={`${domId}-feed-username`}>Username Basic <span>facoltativo</span></label><input id={`${domId}-feed-username`} autoComplete="off" maxLength={500} value={feedUsername} onChange={(event) => { setFeedUsername(event.target.value); setError(""); }} /></div>
              <div><label htmlFor={`${domId}-feed-password`}>Password Basic <span>facoltativa</span></label><input id={`${domId}-feed-password`} type="password" autoComplete="new-password" maxLength={4096} value={feedPassword} onChange={(event) => { setFeedPassword(event.target.value); setError(""); }} /></div>
              <p className="catalog-url-help">Le credenziali sono cifrate dal servizio e non vengono mai mostrate né reinserite nel modulo. Username e password vanno compilati insieme.</p>
            </>}</div>
            <div className="catalog-form-actions"><button type="submit" className="settings-primary" disabled={!canManage || !selectedSupplierId || !priceListName.trim() || (priceListSource === "file" ? !priceListFile : !feedUrl.trim() || Boolean(feedUsername.trim()) !== Boolean(feedPassword))}>{pending === "create-price-list" || pending === "create-price-list-url" ? "Importazione…" : priceListSource === "file" ? "Importa file" : "Collega feed"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending) || (!priceListName && !priceListFile && !feedUrl && !feedUsername && !feedPassword)} onClick={resetPriceList}>Azzera</button></div>
          </fieldset></form> : <p className="catalog-help">Il tuo account può consultare i listini ma non importarli.</p>
            : <div className="catalog-empty"><DashboardIcon name="supplier" size={24} /><strong>Crea prima un fornitore</strong><p>Ogni listino deve appartenere a un fornitore del negozio.</p><a className="workspace-refresh" href="/seller/catalog/suppliers">Vai ai fornitori</a></div>}
        </div>
      </section>
      <section className="workspace-section catalog-list-card" aria-labelledby={`${domId}-lists-title`}>
        <div className="workspace-section-heading"><h2 id={`${domId}-lists-title`}><DashboardIcon name="file" />Listini importati <span className="count-badge">{dashboard.price_lists.length}</span></h2><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={refresh}><DashboardIcon name="refresh" size={14} />Aggiorna</button></div>
        {dashboard.price_lists.length ? <div className="catalog-table-wrap" tabIndex={0} role="region" aria-label="Tabella dei listini, scorrimento orizzontale"><table className="catalog-table"><caption className="visually-hidden">Listini importati per {sellerName}</caption><thead><tr><th scope="col">Listino</th><th scope="col">Fornitore</th><th scope="col">Origine</th><th scope="col">Prodotti</th><th scope="col">Stato</th><th scope="col">Ultima sincronizzazione</th><th scope="col"><span className="visually-hidden">Azioni</span></th></tr></thead><tbody>{dashboard.price_lists.map((priceList) => <tr key={priceList.id}><td><strong>{priceList.name}</strong><span>{sourceLabel(priceList.source_type)}</span><div className="catalog-profile-badges"><span>{providerLabel(priceList.provider)}</span><span className={`role-${priceList.feed_role}`}>{feedRoleLabel(priceList.feed_role)}</span></div></td><td>{priceList.supplier_name}</td><td>{priceList.source_type === "url" ? <><span className="catalog-file-name">{priceList.source_host}</span><span>{priceList.active_version_number ? `Versione ${priceList.active_version_number}` : "Prima versione in preparazione"}</span></> : <><span className="catalog-file-name">{priceList.file_name ?? "File in elaborazione"}</span><span>{priceList.file_format?.toUpperCase() ?? "Formato in verifica"}</span></>}</td><td>{priceList.row_count === null ? "—" : number.format(priceList.row_count)}</td><td><span className={`catalog-state state-${statusClass(priceList)}`}>{statusLabel(priceList)}</span><CatalogJobProgress job={priceList.latest_job} priceListName={priceList.name} />{priceList.latest_job?.status === "error" && priceList.latest_job.error_code && <span className="catalog-job-error">Codice: {priceList.latest_job.error_code}</span>}</td><td>{priceList.last_success_at ? formatDate(priceList.last_success_at) : "Mai completata"}{priceList.source_type === "url" && <span>Ultimo controllo: {formatDate(priceList.last_checked_at)}</span>}</td><td><div className="catalog-row-actions"><button type="button" className="workspace-refresh" disabled={Boolean(pending) || !hasActiveCatalog(priceList)} onClick={() => void openDetail(priceList)}>{pending === "detail" ? "Apertura…" : "Anteprima"}</button>{priceList.source_type === "url" && dashboard.can_manage && <><button type="button" className="workspace-refresh" disabled={!canManage || jobIsActive(priceList.latest_job)} onClick={() => openFeedEditor(priceList)}>Modifica feed</button><button type="button" className="workspace-refresh" disabled={!canManage || jobIsActive(priceList.latest_job)} onClick={() => void refreshFeed(priceList)}>{pending === "refresh-price-list" ? "Avvio…" : jobIsActive(priceList.latest_job) ? "In corso…" : "Aggiorna ora dal feed"}</button></>}{dashboard.can_manage && <button type="button" className="settings-delete" disabled={!canManage || jobIsActive(priceList.latest_job)} onClick={() => { setDeletePriceList(priceList); setPriceListConfirmation(""); resetFeedEditor(); setError(""); }}>Elimina</button>}</div></td></tr>)}</tbody></table></div>
          : <div className="workspace-section-body catalog-empty"><DashboardIcon name="file" size={24} /><strong>Nessun listino importato</strong><p>Carica un file o collega un feed HTTPS per visualizzare qui prodotti, origine e stato.</p></div>}
        {editPriceList && <form className="catalog-feed-edit" onSubmit={updateFeed}>
          <fieldset disabled={!canManage || jobIsActive(editPriceList.latest_job)}><legend>Modifica feed · {editPriceList.name}</legend>
            <p>L’indirizzo completo salvato non viene mostrato. Host attuale: <strong>{editPriceList.source_host}</strong>. Incolla il nuovo URL completo.</p>
            <div className="catalog-edit-url"><label htmlFor={`${domId}-edit-feed-url`}>Nuovo URL catalogo principale (HTTPS)</label><input id={`${domId}-edit-feed-url`} type="url" inputMode="url" required maxLength={4096} autoComplete="off" placeholder="https://fornitore.example/listino.csv" value={editFeedUrl} onChange={(event) => { setEditFeedUrl(event.target.value); setError(""); }} /></div>
            <fieldset className="catalog-credential-mode"><legend>Credenziali HTTP Basic</legend>
              {([["keep", "Mantieni quelle salvate"], ["replace", "Sostituisci"], ["remove", "Rimuovi"]] as Array<[CatalogCredentialMode, string]>).map(([mode, label]) => <label key={mode}><input type="radio" name={`${domId}-credential-mode`} value={mode} checked={editCredentialMode === mode} onChange={() => { setEditCredentialMode(mode); setEditFeedUsername(""); setEditFeedPassword(""); setError(""); }} />{label}</label>)}
            </fieldset>
            {editCredentialMode === "replace" && <div className="catalog-edit-credentials">
              <div><label htmlFor={`${domId}-edit-feed-username`}>Nuovo username Basic</label><input id={`${domId}-edit-feed-username`} required autoComplete="off" maxLength={500} value={editFeedUsername} onChange={(event) => { setEditFeedUsername(event.target.value); setError(""); }} /></div>
              <div><label htmlFor={`${domId}-edit-feed-password`}>Nuova password Basic</label><input id={`${domId}-edit-feed-password`} type="password" required autoComplete="new-password" maxLength={4096} value={editFeedPassword} onChange={(event) => { setEditFeedPassword(event.target.value); setError(""); }} /></div>
            </div>}
            <p className="catalog-url-help">Il salvataggio cambia solo la configurazione. La versione attiva resta disponibile; usa “Aggiorna ora dal feed” quando vuoi acquisire i nuovi dati.</p>
            <div className="catalog-form-actions"><button type="submit" className="settings-primary" disabled={!canManage || !editFeedUrl.trim() || (editCredentialMode === "replace" && (!editFeedUsername.trim() || !editFeedPassword))}>{pending === "update-price-list-url" ? "Salvataggio…" : "Salva URL feed"}</button><button type="button" className="workspace-refresh" disabled={Boolean(pending)} onClick={resetFeedEditor}>Annulla</button></div>
          </fieldset>
        </form>}
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
