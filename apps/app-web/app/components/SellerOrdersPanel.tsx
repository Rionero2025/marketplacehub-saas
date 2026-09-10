"use client";

import { useCallback, useEffect, useRef, useState, useTransition, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { WorkspaceResult } from "../lib/workspace";
import { readConnections, type MarketplaceConnection } from "../lib/marketplace-connections-types";
import { formatMoney, formatOrderDate, formatPercent, ordersDefaultBounds, ordersFilterInput, ordersQueryString, readOrderItem, readOrderJob, readOrderSelection, readOrdersList, readOrdersQuery, type OrderEnvironment, type OrderItem, type OrderJob, type OrdersList, type OrdersQuery, type OrdersSummary, type PaymentSelection, type PaymentSummary, type SelectionAction } from "../lib/orders-types";
import { DashboardIcon } from "./DashboardIcon";
import { OrderTrackingPanel } from "./OrderTrackingPanel";
import styles from "./SellerOrdersPanel.module.css";

const supported = (account: MarketplaceConnection) => account.marketplace === "kaufland" || account.marketplace === "worten";
const connected = (account: MarketplaceConnection) => account.active && account.connection_status === "connected";
const running = (job: OrderJob | null) => job?.status === "queued" || job?.status === "running";
const names: Record<string, string> = { kaufland: "Kaufland", worten: "Worten" };
const statusNames: Record<string, string> = { cancelled: "Cancellato", need_to_be_sent: "Da spedire", open: "Aperto", received: "Ricevuto", returned: "Reso", returned_paid: "Reso rimborsato", sent: "Spedito", sent_and_autopaid: "Spedito e pagato" };
const statusName = (status: string) => statusNames[status] || status.replaceAll("_", " ");
const initialQuery = (accountId: string, environment: OrderEnvironment): OrdersQuery => ({ account_id: accountId, environment, page: 1, page_size: 50, search: "", statuses: [], storefronts: [], currencies: [], carriers: [], date_from: "", date_to: "", status_selection: "all", storefront_selection: "all", currency_selection: "all", tracking: "all", commission: "all", payment: "all", amount_min: "", amount_max: "" });

export function SellerOrdersPage({ result }: { result: WorkspaceResult }) {
  const router = useRouter();
  const [refreshing, startRefresh] = useTransition();
  const seller = result.workspace?.active_seller;
  return <div className={styles.page}>
    <div className="workspace-heading"><div><p className="eyebrow">VENDITE MULTICANALE</p><h1>Elenco ordini</h1><p>Ordini, prodotti e valori economici dei tuoi account marketplace.</p></div></div>
    {result.error ? <section className="workspace-section workspace-empty"><h2>Dati momentaneamente non disponibili</h2><p className="form-error" role="alert">{result.error}</p><button type="button" className="workspace-refresh" disabled={refreshing} onClick={() => startRefresh(() => router.refresh())}>Riprova</button></section>
      : !seller ? <section className="workspace-section workspace-empty"><h2>Seleziona un negozio</h2><p>Gli ordini appartengono al negozio attivo.</p><a href="/seller/settings/store">Vai ai tuoi negozi</a></section>
        : !seller.permissions.includes("LOGISTICS") ? <section className="workspace-section workspace-empty"><h2>Accesso agli ordini non disponibile</h2><p>Il tuo account non ha il permesso di consultare la logistica di questo negozio.</p></section>
          : <><div className={styles.context}><strong>{seller.name}</strong><span>{seller.organization_name}</span><a href="/seller/settings/store">Cambia negozio</a></div><SellerOrdersPanel key={seller.id} sellerId={seller.id} /></>}
  </div>;
}

/** Seller changes unmount the panel, aborting all old account and order requests. */
export function SellerOrdersPanel({ sellerId }: { sellerId: string }) {
  const router = useRouter();
  const [accounts, setAccounts] = useState<MarketplaceConnection[] | null>(null);
  const [accountId, setAccountId] = useState("");
  const [environment, setEnvironment] = useState<OrderEnvironment>("live");
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  const [orderActionBusy, setOrderActionBusy] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    let active = true;
    setLoading(true); setError("");
    const timeout = setTimeout(() => abort.abort(), 25000);
    void (async () => {
      try {
        const response = await fetch(`/api/sellers/${sellerId}/marketplace-connections`, { cache: "no-store", signal: abort.signal });
        if (!active) return;
        if (response.status === 401) { setAccounts(null); router.replace("/login/seller"); router.refresh(); return; }
        if (!response.ok) throw new Error();
        const value = readConnections(await response.json(), sellerId);
        if (!active) return;
        if (!value) throw new Error();
        setAccounts(value.accounts);
        setAccountId((previous) => value.accounts.some((account) => supported(account) && account.id === previous) ? previous : value.accounts.find((account) => supported(account) && connected(account))?.id ?? value.accounts.find(supported)?.id ?? "");
      } catch { if (active) { setAccounts(null); setError("Impossibile caricare gli account marketplace. Riprova."); } }
      finally { clearTimeout(timeout); if (active) setLoading(false); }
    })();
    return () => { active = false; abort.abort(); clearTimeout(timeout); };
  }, [sellerId, refresh, router]);
  const account = accounts?.find((item) => item.id === accountId);
  return <>
    <section className={styles.card} aria-label="Account marketplace">
      <div className={styles.accountBar}><label className={styles.field}>Account marketplace<select value={accountId} disabled={loading || orderActionBusy || !accounts?.length} onChange={(event) => { setAccountId(event.target.value); setEnvironment("live"); }}><option value="" disabled>Seleziona un account</option>{accounts?.map((item) => <option key={item.id} value={item.id} disabled={!supported(item)}>{names[item.marketplace] ?? item.marketplace} · {item.account_name}{!supported(item) ? " · ordini non disponibili" : !connected(item) ? " · da verificare" : ""}</option>)}</select></label>
        {account?.marketplace === "kaufland" && <label className={styles.field}>Ambiente<select value={environment} disabled={orderActionBusy} onChange={(event) => setEnvironment(event.target.value as OrderEnvironment)}><option value="live">Live</option><option value="playground">Playground (test)</option></select></label>}
        <button type="button" className={styles.secondary} disabled={loading || orderActionBusy} onClick={() => setRefresh((value) => value + 1)}><DashboardIcon name="refresh" size={15} />Aggiorna account</button><a href="/seller/marketplaces/accounts">Gestisci collegamenti</a>
      </div>
      {loading && <p role="status" className={styles.muted}>Caricamento account…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {!loading && accounts && !account && <div className={styles.empty}><h2>Collega un marketplace per iniziare</h2><p>Gli ordini saranno importati dall’account scelto per questo negozio.</p><a href="/seller/marketplaces">Collega marketplace</a></div>}
    </section>
    {account && supported(account) && !loading && <OrderAccountPanel key={`${sellerId}:${account.id}:${environment}`} sellerId={sellerId} account={account} environment={environment} onBusyChange={setOrderActionBusy} />}
  </>;
}

export function OrderAccountPanel({ sellerId, account, environment, onBusyChange }: { sellerId: string; account: MarketplaceConnection; environment: OrderEnvironment; onBusyChange?: (busy: boolean) => void }) {
  const router = useRouter();
  const [query, setQuery] = useState(() => initialQuery(account.id, environment));
  const [draft, setDraft] = useState(() => initialQuery(account.id, environment));
  const [list, setList] = useState<OrdersList | null>(null);
  const [job, setJob] = useState<OrderJob | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [maximum, setMaximum] = useState<OrderJob["maximum"]>(1000);
  const [includeDetails, setIncludeDetails] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [detailVersion, setDetailVersion] = useState(0);
  const [selectionPending, setSelectionPending] = useState(false);
  const [paymentSelectionPending, setPaymentSelectionPending] = useState(false);
  const [trackingBusy, setTrackingBusy] = useState(false);
  const [selectionNeedsRefresh, setSelectionNeedsRefresh] = useState(false);
  const [paymentSelectionNeedsRefresh, setPaymentSelectionNeedsRefresh] = useState(false);
  const [optimisticIds, setOptimisticIds] = useState<Set<string> | null>(null);
  const [paymentOptimisticIds, setPaymentOptimisticIds] = useState<Set<string> | null>(null);
  const [exporting, setExporting] = useState<"selected" | "filtered" | null>(null);
  const alive = useRef(true), listVersion = useRef(0), syncBusy = useRef(false);
  const listAbort = useRef<AbortController | null>(null), syncAbort = useRef<AbortController | null>(null);
  const selectionAbort = useRef<AbortController | null>(null), paymentSelectionAbort = useRef<AbortController | null>(null), exportAbort = useRef<AbortController | null>(null);
  const selectionQueue = useRef<SelectionAction[]>([]), paymentSelectionQueue = useRef<SelectionAction[]>([]);
  const selectionBusy = useRef(false), paymentSelectionBusy = useRef(false), exportBusy = useRef(false);
  const defaultBounds = useRef<OrdersQuery | null>(null), emptyDefaults = useRef(false), filtersApplied = useRef(false);
  const contextSignature = useRef(ordersQueryString(query));
  contextSignature.current = ordersQueryString(query);
  const downloadUrls = useRef(new Set<string>());
  const baseUrl = `/api/sellers/${sellerId}/orders`;
  const scope = { account_id: account.id, environment };

  const responseAllowed = useCallback((response: Response) => {
    if (response.status === 401) { setList(null); setJob(null); setSelected(null); router.replace("/login/seller"); router.refresh(); return false; }
    if (response.status === 403 || response.status === 404) { setList(null); setJob(null); setSelected(null); setError("Questi ordini non sono più accessibili dal tuo account. Aggiorna gli account marketplace."); return false; }
    return true;
  }, [router]);

  const loadList = useCallback(async () => {
    listAbort.current?.abort();
    const abort = new AbortController(), version = ++listVersion.current;
    listAbort.current = abort;
    const current = () => alive.current && version === listVersion.current;
    setLoading(true); setError("");
    const timeout = setTimeout(() => abort.abort(), 25000);
    try {
      const response = await fetch(`${baseUrl}?${ordersQueryString(query)}`, { cache: "no-store", signal: abort.signal });
      if (!current() || !responseAllowed(response)) return;
      if (!response.ok) throw new Error();
      const value = readOrdersList(await response.json(), sellerId, query);
      if (!current()) return;
      if (!value) throw new Error();
      const hasArchiveBounds = value.filters.date_min !== null || value.filters.amount_min !== null;
      if (!defaultBounds.current || (emptyDefaults.current && hasArchiveBounds && !filtersApplied.current)) {
        const defaults = ordersDefaultBounds(initialQuery(account.id, environment), value.filters);
        defaultBounds.current = defaults; emptyDefaults.current = !hasArchiveBounds;
        setDraft(defaults); setQuery(defaults);
        return;
      }
      setList(value); setJob(value.latest_job); setNeedsRefresh(false); setSelectionNeedsRefresh(false); setPaymentSelectionNeedsRefresh(false); setOptimisticIds(null); setPaymentOptimisticIds(null);
    } catch { if (current()) { setList(null); setError("Impossibile caricare gli ordini. Aggiorna i dati per riprovare."); } }
    finally { clearTimeout(timeout); if (current()) setLoading(false); }
  }, [baseUrl, query, responseAllowed, sellerId, account.id, environment]);

  useEffect(() => { alive.current = true; return () => { alive.current = false; listVersion.current += 1; listAbort.current?.abort(); syncAbort.current?.abort(); selectionAbort.current?.abort(); paymentSelectionAbort.current?.abort(); exportAbort.current?.abort(); selectionQueue.current = []; paymentSelectionQueue.current = []; downloadUrls.current.forEach((url) => URL.revokeObjectURL(url)); downloadUrls.current.clear(); }; }, []);
  useEffect(() => { setSelected(null); void loadList(); return () => { listVersion.current += 1; listAbort.current?.abort(); }; }, [loadList]);
  useEffect(() => { setOptimisticIds(null); setPaymentOptimisticIds(null); return () => { selectionAbort.current?.abort(); paymentSelectionAbort.current?.abort(); exportAbort.current?.abort(); selectionQueue.current = []; paymentSelectionQueue.current = []; }; }, [query]);
  useEffect(() => { onBusyChange?.(selectionPending || paymentSelectionPending || exporting !== null || trackingBusy); return () => onBusyChange?.(false); }, [onBusyChange, selectionPending, paymentSelectionPending, exporting, trackingBusy]);

  useEffect(() => {
    if (!job || !running(job)) return;
    let active = true;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const timeout = setTimeout(() => abort.abort(), 25000);
      try {
        const response = await fetch(`${baseUrl}/jobs/${job.id}`, { cache: "no-store", signal: abort.signal });
        if (!active || !alive.current || !responseAllowed(response)) return;
        if (!response.ok) throw new Error();
        const raw: unknown = await response.json();
        const value = readOrderJob(raw && typeof raw === "object" && "job" in raw ? raw.job : null, { account_id: account.id, environment });
        if (!active || !alive.current) return;
        if (!value || value.id !== job.id) throw new Error();
        setJob(value);
        if (!running(value) && !selectionBusy.current) await loadList();
        else timer = setTimeout(poll, 2500);
      } catch { if (active && alive.current) { setNeedsRefresh(true); setError("Stato della sincronizzazione non disponibile. Il lavoro potrebbe essere ancora in corso: aggiorna i dati."); } }
      finally { clearTimeout(timeout); }
    };
    timer = setTimeout(poll, 1500);
    return () => { active = false; clearTimeout(timer); abort.abort(); };
  }, [job?.id, job?.status, baseUrl, account.id, environment, loadList, responseAllowed]); // Keep one poller per active job.

  async function synchronize() {
    if (syncBusy.current || running(job) || needsRefresh || !list?.can_sync || !connected(account)) return;
    syncBusy.current = true; setSyncing(true); setError("");
    const abort = new AbortController(); syncAbort.current = abort;
    const timeout = setTimeout(() => abort.abort(), 25000);
    try {
      const response = await fetch(`${baseUrl}/sync`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...scope, maximum, include_details: account.marketplace === "kaufland" && includeDetails }), cache: "no-store", signal: abort.signal });
      if (!alive.current || !responseAllowed(response)) return;
      const raw: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        if (response.status >= 500) setNeedsRefresh(true);
        setError(response.status === 422 ? "Verifica il collegamento marketplace prima di sincronizzare." : "Avvio non confermato. Aggiorna i dati prima di riprovare."); return;
      }
      const value = readOrderJob(raw && typeof raw === "object" && "job" in raw ? raw.job : null, scope);
      if (!alive.current) return;
      if (!value) throw new Error();
      setJob(value);
      if (!running(value)) await loadList();
    } catch { if (alive.current) { setNeedsRefresh(true); setError("Avvio non confermato. Aggiorna i dati prima di riprovare."); } }
    finally { clearTimeout(timeout); syncBusy.current = false; if (alive.current) setSyncing(false); }
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selectionBusy.current || paymentSelectionBusy.current || exportBusy.current) return;
    const validated = readOrdersQuery(new URLSearchParams(ordersQueryString({ ...draft, search: draft.search.trim(), amount_min: draft.amount_min.replace(",", "."), amount_max: draft.amount_max.replace(",", "."), page: 1 })));
    if (!validated) { setError("Controlla il periodo e gli importi: il valore minimo deve precedere il massimo."); return; }
    filtersApplied.current = true; setQuery(validated);
  }

  function changeSelection(action: SelectionAction) {
    if (!alive.current || loading || !list || selectionNeedsRefresh || paymentSelectionBusy.current || exportBusy.current) return;
    setOptimisticIds((previous) => {
      const next = new Set(previous ?? list.selection.selected_ids);
      if (action.action === "clear") return new Set<string>();
      if (action.action === "select_all") return new Set(list.items.map((item) => item.id));
      if (action.selected) next.add(action.line_id); else next.delete(action.line_id);
      return next;
    });
    selectionQueue.current.push(action);
    if (selectionBusy.current) return;
    selectionBusy.current = true; setSelectionPending(true); setError("");
    const signature = contextSignature.current, selectionId = list.selection.id;
    const current = () => alive.current && contextSignature.current === signature;
    void (async () => {
      try {
        while (selectionQueue.current.length && current()) {
          const command = selectionQueue.current.shift()!;
          const abort = new AbortController(); selectionAbort.current = abort;
          const timeout = setTimeout(() => abort.abort(), 25000);
          try {
            const response = await fetch(`${baseUrl}/selection`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...scope, purpose: "orders", selection_id: selectionId, filters: ordersFilterInput(query), ...command }), cache: "no-store", signal: abort.signal });
            if (!current() || !responseAllowed(response)) return;
            if (!response.ok) throw new Error();
            const raw: unknown = await response.json();
            const selection = readOrderSelection(raw && typeof raw === "object" && "selection" in raw ? raw.selection : null, "orders");
            if (!current()) return;
            if (!selection || selection.id !== selectionId) throw new Error();
            setList((previous) => previous && previous.selection.id === selection.id ? { ...previous, selection: { ...selection, selected_ids: previous.selection.selected_ids } } : previous);
          } finally { clearTimeout(timeout); }
        }
        if (current()) await loadList();
      } catch {
        if (current()) { selectionQueue.current = []; setOptimisticIds(null); setSelectionNeedsRefresh(true); setError("Modifica della selezione non confermata. Aggiorna i dati per recuperare lo stato salvato prima di continuare."); }
      } finally { selectionQueue.current = []; selectionBusy.current = false; if (alive.current) setSelectionPending(false); }
    })();
  }

  function changePaymentSelection(action: SelectionAction) {
    if (!alive.current || loading || !list?.payment_selection || paymentSelectionNeedsRefresh || selectionBusy.current || exportBusy.current) return;
    const eligibleIds = new Set(list.items.filter((item) => checkedIds.has(item.id)).map((item) => item.id));
    setPaymentOptimisticIds((previous) => {
      const next = new Set(previous ?? list.payment_selection?.selected_ids ?? []);
      if (action.action === "clear") return new Set<string>();
      if (action.action === "select_all") return eligibleIds;
      if (action.selected) next.add(action.line_id); else next.delete(action.line_id);
      return next;
    });
    paymentSelectionQueue.current.push(action);
    if (paymentSelectionBusy.current) return;
    paymentSelectionBusy.current = true; setPaymentSelectionPending(true); setError("");
    const signature = contextSignature.current, paymentSelectionId = list.payment_selection.id, ordersSelectionId = list.selection.id;
    const current = () => alive.current && contextSignature.current === signature;
    void (async () => {
      try {
        while (paymentSelectionQueue.current.length && current()) {
          const command = paymentSelectionQueue.current.shift()!;
          const abort = new AbortController(); paymentSelectionAbort.current = abort;
          const timeout = setTimeout(() => abort.abort(), 25000);
          try {
            const response = await fetch(`${baseUrl}/selection`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...scope, purpose: "payments", selection_id: paymentSelectionId, orders_selection_id: ordersSelectionId, filters: ordersFilterInput(query), ...command }), cache: "no-store", signal: abort.signal });
            if (!current() || !responseAllowed(response)) return;
            if (!response.ok) throw new Error();
            const raw: unknown = await response.json();
            const selection = readOrderSelection(raw && typeof raw === "object" && "selection" in raw ? raw.selection : null, "payments");
            if (!current()) return;
            if (!selection || selection.id !== paymentSelectionId) throw new Error();
            setList((previous) => previous?.payment_selection?.id === selection.id
              ? { ...previous, payment_selection: { ...selection, selected_ids: previous.payment_selection.selected_ids } }
              : previous);
          } finally { clearTimeout(timeout); }
        }
        if (current()) await loadList();
      } catch {
        if (current()) { paymentSelectionQueue.current = []; setPaymentOptimisticIds(null); setPaymentSelectionNeedsRefresh(true); setError("Modifica della selezione pagamenti non confermata. Aggiorna i dati per recuperare lo stato salvato prima di continuare."); }
      } finally { paymentSelectionQueue.current = []; paymentSelectionBusy.current = false; if (alive.current) setPaymentSelectionPending(false); }
    })();
  }

  async function exportOrders(kind: "selected" | "filtered") {
    if (exportBusy.current || selectionBusy.current || paymentSelectionBusy.current || loading || selectionNeedsRefresh || !list || !list.selection.selected_count || !list.selection.filtered_count) return;
    exportBusy.current = true; setExporting(kind); setError("");
    const signature = contextSignature.current, abort = new AbortController(); exportAbort.current = abort;
    const current = () => alive.current && contextSignature.current === signature;
    const timeout = setTimeout(() => abort.abort(), 95000);
    try {
      const response = await fetch(`${baseUrl}/export`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...scope, selection_id: list.selection.id, filters: ordersFilterInput(query), kind }), cache: "no-store", signal: abort.signal });
      if (!current() || !responseAllowed(response)) return;
      if (!response.ok || !response.headers.get("content-type")?.toLowerCase().startsWith("text/csv")) throw new Error();
      const blob = await response.blob();
      if (!current()) return;
      const url = URL.createObjectURL(blob); downloadUrls.current.add(url);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `ordini-${account.marketplace}-${environment}-${kind === "selected" ? "selezionati" : "filtrati"}.csv`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => { URL.revokeObjectURL(url); downloadUrls.current.delete(url); }, 1000);
    } catch { if (current()) setError("Esportazione non completata. Aggiorna i dati e riprova; nessun CSV è stato confermato."); }
    finally { clearTimeout(timeout); exportBusy.current = false; if (alive.current) setExporting(null); }
  }
  const filtersActive = Boolean(query.search || query.status_selection === "selected" || query.storefront_selection === "selected" || query.currency_selection === "selected" || query.carriers.length || query.tracking !== "all" || query.commission !== "all" || query.payment !== "all" || query.amount_min || query.amount_max || query.date_from || query.date_to);
  const pages = Math.max(1, Math.ceil((list?.total ?? 0) / query.page_size));
  const checkedIds = optimisticIds ?? new Set(list?.selection.selected_ids ?? []);
  const paymentCheckedIds = paymentOptimisticIds ?? new Set(list?.payment_selection?.selected_ids ?? []);
  const actionBusy = loading || selectionPending || paymentSelectionPending || exporting !== null || trackingBusy;
  return <>
    <section className={styles.card} aria-labelledby="orders-sync-title"><div className={styles.sectionHeading}><div><h2 id="orders-sync-title">Sincronizza ordini</h2><p>Importa gli ordini da {names[account.marketplace]}. Puoi continuare a lavorare durante il download.</p></div><span className={environment === "playground" ? styles.warningBadge : styles.badge}>{environment === "playground" ? "Ambiente di test" : "Live"}</span></div>
      {!connected(account) && <p className={styles.notice}>L’account deve essere verificato per importare nuovi dati. Gli ordini già salvati restano consultabili. <a href="/seller/marketplaces/accounts">Verifica collegamento</a></p>}
      <div className={styles.syncControls}><label className={styles.field}>Righe da importare<select value={maximum ?? "all"} disabled={syncing || running(job)} onChange={(event) => setMaximum(event.target.value === "all" ? null : Number(event.target.value) as OrderJob["maximum"])}><option value="500">Ultime 500</option><option value="1000">Ultime 1.000</option><option value="5000">Ultime 5.000</option><option value="all">Tutte disponibili</option></select></label>
        {account.marketplace === "kaufland" && <label className={styles.check}><input type="checkbox" checked={includeDetails} disabled={syncing || running(job)} onChange={(event) => setIncludeDetails(event.target.checked)} />Verifica dettagli degli ordini spediti</label>}
        <button className={styles.primary} type="button" disabled={loading || syncing || running(job) || needsRefresh || !list?.can_sync || !connected(account)} onClick={() => void synchronize()}><DashboardIcon name="refresh" size={15} />{syncing ? "Avvio…" : running(job) ? "Sincronizzazione in corso" : "Sincronizza ordini"}</button>
      </div>
      {list && !list.can_sync && connected(account) && <p className={styles.muted}>Hai accesso in lettura: la sincronizzazione richiede il permesso di modifica della logistica.</p>}
      {job && <div className={`${styles.job} ${job.status === "error" ? styles.jobError : ""}`} role="status" aria-live="polite"><div><strong>{job.message}</strong><span>{job.processed.toLocaleString("it-IT")} righe{job.total !== null ? ` / ${job.total.toLocaleString("it-IT")}` : " elaborate"}</span></div>{running(job) && <progress aria-label="Avanzamento sincronizzazione" value={job.progress ?? undefined} max={100} />}<small>{job.finished_at ? `Terminata: ${formatOrderDate(job.finished_at)}` : `Avviata: ${formatOrderDate(job.created_at)}`}</small></div>}
    </section>
    {error && <div className="form-error" role="alert">{error}</div>}
    {account.marketplace === "kaufland" && <OrderTrackingPanel sellerId={sellerId} accountId={account.id} environment={environment} orders={list?.items ?? []} disabled={loading || syncing || running(job) || selectionPending || paymentSelectionPending || exporting !== null} onBusyChange={setTrackingBusy} onMutated={async () => { setDetailVersion((value) => value + 1); await loadList(); }} onResponse={responseAllowed} />}
    <section className={styles.card} aria-labelledby="orders-list-title"><div className={styles.sectionHeading}><div><h2 id="orders-list-title">Archivio ordini</h2><p>{list ? `${list.total.toLocaleString("it-IT")} righe${filtersActive ? " corrispondenti ai filtri" : " salvate"}` : "Consulta le righe importate dal marketplace."} · {account.account_name}</p></div><button type="button" className={styles.secondary} disabled={actionBusy || syncing} onClick={() => void loadList()}><DashboardIcon name="refresh" size={15} />Aggiorna dati</button></div>
      {account.marketplace !== "kaufland" && <p className={styles.notice} role="status">Programma pagamenti non disponibile per questo marketplace.</p>}
      <form className={styles.filters} onSubmit={applyFilters}><label className={`${styles.field} ${styles.search}`}>Cerca ordine o prodotto<input type="search" value={draft.search} maxLength={200} placeholder="Ordine, prodotto, EAN, SKU, tracking…" onChange={(event) => setDraft({ ...draft, search: event.target.value })} /></label><label className={styles.field}>Dal<input type="date" value={draft.date_from} onChange={(event) => setDraft({ ...draft, date_from: event.target.value })} /></label><label className={styles.field}>Al<input type="date" value={draft.date_to} min={draft.date_from || undefined} onChange={(event) => setDraft({ ...draft, date_to: event.target.value })} /></label>
        <details className={styles.advancedFilters} open><summary>Stati, paesi e filtri avanzati</summary><div className={styles.choiceGrid}>
          <FilterChoices label="Stati ordine" options={list?.filters.statuses ?? []} value={draft.status_selection === "all" ? null : draft.statuses} display={statusName} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, status_selection: value === null ? "all" : "selected", statuses: value ?? [] })} />
          <FilterChoices label="Paesi / storefront" options={list?.filters.storefronts ?? []} value={draft.storefront_selection === "all" ? null : draft.storefronts} display={(value) => value.toUpperCase() || "Paese non disponibile"} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, storefront_selection: value === null ? "all" : "selected", storefronts: value ?? [] })} />
          <FilterChoices label="Valute originali" options={list?.filters.currencies ?? []} value={draft.currency_selection === "all" ? null : draft.currencies} display={(value) => value || "Valuta non disponibile"} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, currency_selection: value === null ? "all" : "selected", currencies: value ?? [] })} />
        </div><div className={styles.advancedFields}>
           <label className={styles.field}>Tracking<select value={draft.tracking} disabled={actionBusy} onChange={(event) => setDraft({ ...draft, tracking: event.target.value as OrdersQuery["tracking"] })}><option value="all">Tutti</option><option value="present">Con tracking</option><option value="missing">Senza tracking</option></select></label>
           <label className={styles.field}>Commissione<select value={draft.commission} disabled={actionBusy} onChange={(event) => setDraft({ ...draft, commission: event.target.value as OrdersQuery["commission"] })}><option value="all">Tutte</option><option value="present">Disponibile</option><option value="missing">Non disponibile</option></select></label>
          {account.marketplace === "kaufland" && <label className={styles.field}>Pagamento<select value={draft.payment} disabled={actionBusy} onChange={(event) => setDraft({ ...draft, payment: event.target.value as OrdersQuery["payment"] })}><option value="all">Tutti</option><option value="available">Disponibile</option><option value="waiting">In attesa con data</option><option value="unknown">Data da definire</option><option value="ticket_open">Ticket aperto</option></select></label>}
          <fieldset className={styles.choiceGroup} disabled={actionBusy}><legend>Corrieri</legend><div className={styles.choiceActions}><button type="button" className={styles.linkButton} onClick={() => setDraft({ ...draft, carriers: [] })}>Tutti i corrieri</button></div><div className={styles.choiceOptions}>{list?.filters.carriers.filter(Boolean).map((carrier) => <label key={carrier}><input type="checkbox" checked={draft.carriers.includes(carrier)} onChange={(event) => setDraft({ ...draft, carriers: event.target.checked ? [...draft.carriers, carrier] : draft.carriers.filter((value) => value !== carrier) })} />{carrier}</label>)}</div><small className={styles.hint}>{draft.carriers.length ? `${draft.carriers.length} corrieri selezionati` : "Nessuna restrizione: tutti i corrieri."}</small></fieldset>
          <label className={styles.field}>Vendita minima (EUR)<input type="number" min="0" step="0.01" inputMode="decimal" value={draft.amount_min} required disabled={actionBusy} onChange={(event) => setDraft({ ...draft, amount_min: event.target.value })} /></label>
          <label className={styles.field}>Vendita massima (EUR)<input type="number" min="0" step="0.01" inputMode="decimal" value={draft.amount_max} required disabled={actionBusy} onChange={(event) => setDraft({ ...draft, amount_max: event.target.value })} /></label>
        </div></details>
        <div className={styles.filterActions}><button className={styles.primary} type="submit" disabled={actionBusy}>Applica filtri</button><button className={styles.secondary} type="button" disabled={actionBusy} onClick={() => { if (selectionBusy.current || paymentSelectionBusy.current || exportBusy.current) return; const reset = defaultBounds.current ?? initialQuery(account.id, environment); filtersApplied.current = false; setDraft(reset); setQuery(reset); }}>Ripristina filtri iniziali</button></div>
      </form><p className={styles.hint}>Il periodo filtra la data di creazione in UTC; le date visualizzate sono in ora italiana. «Nessuno» in stati, paesi o valute esclude tutte le righe di quel filtro. Gli importi sono in EUR; «—» indica un valore non disponibile.{account.marketplace === "kaufland" ? " Le date di pagamento includono gli eventuali ritardi causati dai ticket." : ""}</p>
      {list && <><div className={styles.selectionBar}><div><strong>{list.selection.selected_count.toLocaleString("it-IT")} di {list.selection.filtered_count.toLocaleString("it-IT")} righe selezionate</strong><p>La selezione comprende tutte le pagine dei filtri applicati.</p></div><div className={styles.filterActions}><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || !list.total} onClick={() => changeSelection({ action: "select_all" })}>Seleziona tutti i filtrati</button><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || !list.total} onClick={() => changeSelection({ action: "clear" })}>Deseleziona tutti</button></div>{selectionPending && <span role="status">Salvataggio selezione ordini…</span>}</div>
        <OrdersTotals summary={list.selection.summary} pending={selectionPending} marketplace={account.marketplace} />
        <div className={styles.exports}><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || list.selection.selected_count === 0} onClick={() => void exportOrders("selected")}>{exporting === "selected" ? "Preparazione CSV…" : "Esporta CSV selezionati"}</button><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || list.selection.selected_count === 0} onClick={() => void exportOrders("filtered")}>{exporting === "filtered" ? "Preparazione CSV…" : "Esporta CSV tutti i filtrati"}</button>{list.selection.selected_count === 0 && <p>Seleziona almeno una riga per visualizzare i totali operativi ed esportare.</p>}</div></>}
      {selected && <OrderDetail key={`${selected}:${detailVersion}`} sellerId={sellerId} lineId={selected} query={query} onClose={() => setSelected(null)} onResponse={responseAllowed} />}
      {loading ? <p className={styles.empty} role="status">Caricamento ordini…</p> : list && list.items.length ? <div className={styles.tableScroll} tabIndex={0} role="region" aria-label="Tabella ordini, scorrimento orizzontale"><table className={styles.table}><thead><tr><th scope="col">Selezione</th><th scope="col">Ordine / data</th><th scope="col">Prodotto / EAN</th><th scope="col">SKU composto</th><th scope="col">Quantità</th><th scope="col">Stato / paese</th><th scope="col">Corriere / tracking</th>{account.marketplace === "kaufland" && <><th scope="col">Pagamento</th><th scope="col">Ticket</th></>}<th scope="col">Vendita</th><th scope="col">Costo acquisto</th><th scope="col">Commissione</th><th scope="col">Netto da ricevere</th><th scope="col">Utile / % sul costo</th><th scope="col">Dettaglio</th></tr></thead><tbody>{list.items.map((item) => <tr key={item.id}><td className={styles.selectionCell}><input type="checkbox" aria-label={`Seleziona ordine ${item.order_id}, riga ${item.external_line_id}`} checked={checkedIds.has(item.id)} disabled={actionBusy || selectionNeedsRefresh} onChange={(event) => changeSelection({ action: "set", line_id: item.id, selected: event.target.checked })} /></td><td><strong>{item.order_id}</strong><small>{formatOrderDate(item.created_at)}</small><small>Riga {item.external_line_id}</small></td><td className={styles.product}><strong>{item.product_name || "Nome non disponibile"}</strong><small>EAN {item.ean || "—"}</small></td><td className={styles.sku}>{item.sku || "—"}</td><td className={styles.number}>{item.quantity}</td><td><span className={styles.badge}>{item.status_label || statusName(item.status)}</span><small>{item.storefront.toUpperCase() || "—"}</small>{item.details.excluded_from_totals && <small>Escluso dai totali</small>}</td><td className={styles.shipment}><strong>{item.details.carrier || "—"}</strong><small>{item.details.tracking || "Tracking non disponibile"}</small></td>{account.marketplace === "kaufland" && <><PaymentCell item={item} /><TicketCell item={item} /></>}<td className={styles.number}>{formatMoney(item.sale_amount_eur)}</td><td className={styles.number}>{formatMoney(item.purchase_cost_eur)}<small>{item.purchase_cost_source}</small></td><td className={styles.number}>{formatMoney(item.commission_amount_eur)}<small>{formatPercent(item.commission_rate)}</small></td><td className={styles.number}>{formatMoney(item.payout_amount_eur)}</td><td className={styles.number}>{formatMoney(item.profit_amount_eur)}<small>{formatPercent(item.profit_pct)}</small></td><td><button type="button" className={styles.linkButton} aria-expanded={selected === item.id} onClick={() => setSelected(selected === item.id ? null : item.id)}>Apri<span className={styles.srOnly}> ordine {item.order_id}, riga {item.external_line_id}</span></button>{item.monetary_warnings.length > 0 && <small className={styles.warningText}>Dati da verificare</small>}</td></tr>)}</tbody></table></div>
        : list && <div className={styles.empty}><h3>{filtersActive ? "Nessun ordine corrisponde ai filtri" : "Nessun ordine salvato"}</h3><p>{filtersActive ? "Modifica il periodo o azzera i filtri." : "Avvia la sincronizzazione per importare gli ordini di questo account."}</p></div>}
      {list?.payment_selection && <PaymentSchedule items={list.items} orderIds={checkedIds} selection={list.payment_selection} checkedIds={paymentCheckedIds} pending={paymentSelectionPending} needsRefresh={paymentSelectionNeedsRefresh} disabled={actionBusy || selectionNeedsRefresh} onChange={changePaymentSelection} />}
      {list && <div className={styles.pagination}><label>Righe per pagina <select value={query.page_size} disabled={actionBusy} onChange={(event) => { const page_size = Number(event.target.value); setQuery({ ...query, page: 1, page_size }); setDraft({ ...draft, page_size }); }}><option value={25}>25</option><option value={50}>50</option><option value={100}>100</option></select></label><span>Pagina {query.page} di {pages}</span><button className={styles.secondary} type="button" disabled={actionBusy || query.page <= 1} onClick={() => setQuery({ ...query, page: query.page - 1 })}>Precedente</button><button className={styles.secondary} type="button" disabled={actionBusy || query.page >= pages} onClick={() => setQuery({ ...query, page: query.page + 1 })}>Successiva</button></div>}
    </section>
  </>;
}

function PaymentCell({ item, compact = false, showRule = false }: { item: OrderItem; compact?: boolean; showRule?: boolean }) {
  const details = item.details;
  if (isCancelledOrder(item)) return <td className={styles.paymentCell}><span className={`${styles.paymentBadge} ${styles.paymentUnknown}`}>Non dovuto</span><small>Non dovuto · ordine cancellato</small>{showRule && <><small>Regola: {details.payment_rule || "—"}</small><small>Fonte data: {details.payment_source || "non disponibile"}</small></>}</td>;
  const state = details.ticket_open ? "Ticket aperto" : details.payment_available ? "Disponibile" : details.payment_due_at ? "In attesa" : "Data da definire";
  const tone = details.ticket_open ? styles.paymentTicket : details.payment_available ? styles.paymentAvailable : details.payment_due_at ? styles.paymentWaiting : styles.paymentUnknown;
  return <td className={styles.paymentCell}><span className={`${styles.paymentBadge} ${tone}`}>{state}</span><small>{details.payment_status || "Stato non disponibile"}</small>{showRule && <><small>Regola: {details.payment_rule || "non disponibile"}</small><small>Fonte data: {details.payment_source || "non disponibile"}</small></>}{!compact && details.payment_due_at && <small>Data: {formatOrderDate(details.payment_due_at)}</small>}</td>;
}

function TicketCell({ item }: { item: OrderItem }) {
  if (isCancelledOrder(item)) return <td className={styles.ticketCell}><strong>0 ticket aperti</strong><small>Ritardo accumulato: 0 giorni</small><small>ID: {item.details.ticket_ids?.join(", ") || "nessuno"}</small></td>;
  const count = item.details.ticket_count ?? 0, open = item.details.open_ticket_count ?? 0, delay = item.details.ticket_delay_days ?? 0;
  if (!count) return <td className={styles.ticketCell}><strong>Nessun ticket</strong><small>Nessun ritardo rilevato</small><small>ID: nessuno</small></td>;
  return <td className={styles.ticketCell}><strong>{count} ticket</strong><small>{open ? `${open} ${open === 1 ? "aperto" : "aperti"}` : "Tutti chiusi"}</small><small>{delay > 0 ? `Ritardo accumulato: +${new Intl.NumberFormat("it-IT", { maximumFractionDigits: 2 }).format(delay)} giorni` : "Nessun ritardo accumulato"}</small><small>ID: {item.details.ticket_ids?.join(", ") || "nessuno"}</small></td>;
}

function FilterChoices({ label, options, value, display, onChange, disabled }: { label: string; options: string[]; value: string[] | null; display: (value: string) => string; onChange: (value: string[] | null) => void; disabled: boolean }) {
  return <fieldset className={styles.choiceGroup} disabled={disabled}><legend>{label}</legend><div className={styles.choiceActions}><button type="button" className={styles.linkButton} onClick={() => onChange(null)}>Tutti</button><button type="button" className={styles.linkButton} onClick={() => onChange([])}>Nessuno</button><span>{value === null ? "Tutti inclusi" : value.length ? `${value.length} selezionati` : "Nessuno incluso"}</span></div><div className={styles.choiceOptions}>{options.length ? options.map((option) => <label key={option}><input type="checkbox" checked={value === null || value.includes(option)} onChange={(event) => { const selected = value ?? options; const next = event.target.checked ? [...new Set([...selected, option])] : selected.filter((entry) => entry !== option); onChange(next.length === options.length && options.every((entry) => next.includes(entry)) ? null : next); }} />{display(option)}</label>) : <p>Nessun valore disponibile.</p>}</div></fieldset>;
}

function MoneyMetric({ label, value, note, tone = "default" }: { label: string; value: string; note: string; tone?: "default" | "primary" | "profit" | "loss" }) {
  const toneClass = tone === "primary" ? styles.moneyMetricPrimary : tone === "profit" ? styles.moneyMetricProfit : tone === "loss" ? styles.moneyMetricLoss : "";
  return <dl className={`${styles.moneyMetric} ${toneClass}`}><dt>{label}</dt><dd className={styles.moneyValue}>{value}</dd><dd className={styles.moneyNote}>{note}</dd></dl>;
}

export function OrdersTotals({ summary, pending, marketplace }: { summary: OrdersSummary; pending: boolean; marketplace: string }) {
  const activeRows = Math.max(0, summary.selected_rows - summary.cancelled_rows);
  const completeEconomicRows = Math.max(0, summary.complete_economic_rows - summary.cancelled_rows);
  const costCoverage = activeRows ? Math.min(100, summary.known_cost_rows / activeRows * 100) : 0;
  const profitIsLoss = Number(summary.profit_amount_eur) < 0;
  const profitLabel = profitIsLoss ? "Perdita calcolata" : "Utile calcolato";
  const marketplaceLabel = names[marketplace] || "marketplace";
  return <section className={styles.totals} aria-labelledby="orders-totals-title" aria-busy={pending}>
    <div className={styles.totalsHeading}><div><h3 id="orders-totals-title">Risultato economico della selezione</h3><p>{summary.distinct_orders.toLocaleString("it-IT")} ordini distinti · {summary.selected_rows.toLocaleString("it-IT")} righe · {summary.quantity.toLocaleString("it-IT")} pezzi</p></div><span>Importi in EUR</span></div>
    <div className={styles.moneyGroups}>
      <section className={styles.moneyGroup} aria-labelledby="orders-cashflow-title">
        <div className={styles.moneyGroupHeading}><div><span>INCASSI MARKETPLACE</span><h4 id="orders-cashflow-title">Dal venduto al netto</h4></div><small>{completeEconomicRows.toLocaleString("it-IT")} righe con vendita, commissione e netto completi</small></div>
        <div className={styles.moneyFlow}>
          <MoneyMetric label="Vendite conteggiate" value={formatMoney(summary.sale_amount_eur)} note="Importi di vendita inclusi nel totale" />
          <span className={styles.moneyOperator}><span aria-hidden="true">−</span><span className={styles.srOnly}>meno</span></span>
          <MoneyMetric label="Commissioni marketplace" value={formatMoney(summary.commission_amount_eur)} note={`Trattenute da ${marketplaceLabel}`} />
          <span className={`${styles.moneyOperator} ${styles.moneyOperatorArrow}`}><span aria-hidden="true">→</span><span className={styles.srOnly}>porta al</span></span>
          <MoneyMetric label="Netto da ricevere" value={formatMoney(summary.payout_amount_eur)} note="Somma dei netti delle singole righe" tone="primary" />
        </div>
        <p className={styles.moneyExplanation}>Il netto è calcolato oppure comunicato direttamente dal marketplace; per questo può non coincidere con Vendite conteggiate meno Commissioni. Gli arrotondamenti per riga possono creare ulteriori scostamenti.</p>
      </section>
      <section className={styles.moneyGroup} aria-labelledby="orders-profit-title">
        <div className={styles.moneyGroupHeading}><div><span>REDDITIVITÀ</span><h4 id="orders-profit-title">Margine calcolabile</h4></div><small>{summary.known_cost_rows.toLocaleString("it-IT")} di {activeRows.toLocaleString("it-IT")} righe non cancellate entrano nel calcolo di costo e utile</small></div>
        <div className={styles.profitFlow}>
          <MoneyMetric label="Costi di acquisto inclusi" value={formatMoney(summary.purchase_cost_eur)} note={`Somma riferita a ${summary.known_cost_rows.toLocaleString("it-IT")} righe`} />
          <MoneyMetric label={profitLabel} value={formatMoney(summary.profit_amount_eur)} note={`Utile sui costi inclusi: ${formatPercent(summary.profit_pct)}`} tone={profitIsLoss ? "loss" : "profit"} />
        </div>
        <p className={styles.moneyExplanation}>Il costo e l’utile di questo riquadro sommano soltanto le righe in cui entrambi sono calcolabili. Per questo non vanno confrontati direttamente con il netto complessivo.</p>
      </section>
    </div>
    <div className={styles.costCoverage} role="note">
      <div><strong>Copertura del margine: {new Intl.NumberFormat("it-IT", { maximumFractionDigits: 1 }).format(costCoverage)}%</strong><span>{summary.known_cost_rows.toLocaleString("it-IT")} di {activeRows.toLocaleString("it-IT")} righe non cancellate</span></div>
      <progress aria-label="Quota di righe incluse nel calcolo di costo e utile" max={activeRows || 1} value={summary.known_cost_rows} />
      {summary.missing_cost_rows > 0 && <p><strong>{summary.missing_cost_rows.toLocaleString("it-IT")} {summary.missing_cost_rows === 1 ? "riga non ha" : "righe non hanno"} costo e utile entrambi calcolabili: {summary.missing_cost_rows === 1 ? "è esclusa" : "sono escluse"} dal margine.</strong> Per questo costo e utile possono riferirsi a meno righe del netto complessivo.</p>}
    </div>
    <div className={styles.moneyNotes} aria-label="Qualità dei dati economici">
      {summary.missing_economic_rows > 0 && <p className={styles.notice}>{summary.missing_economic_rows.toLocaleString("it-IT")} {summary.missing_economic_rows === 1 ? "riga ha" : "righe hanno"} vendita, commissione o netto incompleti: {summary.missing_economic_rows === 1 ? "è esclusa" : "sono escluse"} da questi tre totali. Non sono valori a zero.</p>}
      {summary.missing_currencies.length > 0 && <p className={styles.notice}>Valute delle righe con importi incompleti: {summary.missing_currencies.join(", ")}.</p>}
      {summary.cancelled_rows > 0 && <p className={styles.dataNote}>{summary.cancelled_rows.toLocaleString("it-IT")} {summary.cancelled_rows === 1 ? "riga cancellata è inclusa" : "righe cancellate sono incluse"} nel conteggio, con contributo economico nullo.</p>}
      {summary.loss_rows > 0 && <p className={styles.lossNote}>{summary.loss_rows.toLocaleString("it-IT")} {summary.loss_rows === 1 ? "riga in perdita è inclusa" : "righe in perdita sono incluse"} nel totale dell’utile.</p>}
      <p className={styles.dataNote}>Origine costi: {summary.sku_cost_rows.toLocaleString("it-IT")} righe da SKU composto · {summary.catalog_cost_rows.toLocaleString("it-IT")} da catalogo.</p>
    </div>
  </section>;
}

function PaymentSchedule({ items, orderIds, selection, checkedIds, pending, needsRefresh, disabled, onChange }: {
  items: OrderItem[]; orderIds: Set<string>; selection: PaymentSelection; checkedIds: Set<string>;
  pending: boolean; needsRefresh: boolean; disabled: boolean; onChange: (action: SelectionAction) => void;
}) {
  const rows = items.filter((item) => orderIds.has(item.id));
  const summary = selection.summary;
  return <section className={styles.paymentProgram} aria-labelledby="orders-payment-program-title" aria-busy={pending}>
    <div className={styles.paymentProgramHeading}><div><p className={styles.eyebrow}>KAUFLAND</p><h3 id="orders-payment-program-title">Date previste di pagamento</h3><p>Scegli in modo indipendente le righe da includere nel riepilogo pagamenti. Qui compaiono solo gli ordini selezionati nell’archivio.</p></div><span className={styles.paymentSelectionCount}>{selection.selected_count.toLocaleString("it-IT")} di {selection.filtered_count.toLocaleString("it-IT")} nel riepilogo</span></div>
    <div className={styles.paymentToolbar}><div className={styles.filterActions}><button type="button" className={styles.secondary} disabled={disabled || needsRefresh || selection.filtered_count === 0} onClick={() => onChange({ action: "select_all" })}>Seleziona tutte per i pagamenti</button><button type="button" className={styles.secondary} disabled={disabled || needsRefresh || selection.selected_count === 0} onClick={() => onChange({ action: "clear" })}>Deseleziona tutte dai pagamenti</button></div>{pending && <span role="status" aria-live="polite">Salvataggio selezione pagamenti…</span>}</div>
    {needsRefresh && <p className={styles.notice}>La selezione pagamenti deve essere riletta dal server. Usa «Aggiorna dati» prima di continuare.</p>}
    {rows.length > 0 ? <div className={styles.paymentTableScroll} tabIndex={0} role="region" aria-label="Tabella date previste di pagamento, scorrimento orizzontale"><table className={`${styles.table} ${styles.paymentTable}`}><thead><tr><th scope="col">Riepilogo</th><th scope="col">Ordine / riga</th><th scope="col">Prodotto / EAN</th><th scope="col">Nazione / stato ordine</th><th scope="col">Tracking</th><th scope="col">Spedito il</th><th scope="col">Ricevuto il</th><th scope="col">Giorni</th><th scope="col">Netto</th><th scope="col">Costo</th><th scope="col">Guadagno</th><th scope="col">Metodo / fonte costo</th><th scope="col">Stato / regola pagamento</th><th scope="col">Data prevista</th><th scope="col">Ritardo / ticket / ID</th></tr></thead><tbody>{rows.map((item) => <PaymentScheduleRow key={item.id} item={item} checked={checkedIds.has(item.id)} disabled={disabled || needsRefresh} onChange={onChange} />)}</tbody></table></div>
      : <div className={styles.paymentEmpty}><strong>Nessuna riga ordine disponibile in questa pagina.</strong><p>{selection.filtered_count > 0 ? "La selezione ordini comprende righe in altre pagine. Apri la pagina corrispondente per scegliere i pagamenti." : "Seleziona prima una o più righe nella tabella ordini."}</p></div>}
    <PaymentSelectionSummary summary={summary} selectedCount={selection.selected_count} />
  </section>;
}

function PaymentScheduleRow({ item, checked, disabled, onChange }: {
  item: OrderItem; checked: boolean; disabled: boolean;
  onChange: (action: SelectionAction) => void;
}) {
  const cancelled = isCancelledOrder(item);
  return <tr><td className={styles.selectionCell}><input type="checkbox" aria-label={`Includi ordine ${item.order_id}, riga ${item.external_line_id} nel riepilogo pagamenti`} checked={checked} disabled={disabled} onChange={(event) => onChange({ action: "set", line_id: item.id, selected: event.target.checked })} /></td><td><strong>{item.order_id}</strong><small>Riga {item.external_line_id}</small></td><td className={styles.product}><strong>{item.product_name || "Nome non disponibile"}</strong><small>EAN {item.ean || "—"}</small></td><td><strong>{item.storefront.toUpperCase() || "—"}</strong><small>{item.status_label || statusName(item.status)}</small></td><td className={styles.shipment}><strong>{item.details.tracking || "Tracking non disponibile"}</strong><small>{item.details.carrier || "Corriere non disponibile"}</small></td><td>{cancelled ? "—" : formatOrderDate(item.details.shipped_at)}</td><td>{cancelled ? "—" : formatOrderDate(item.details.received_at)}</td><td><strong>{paymentDaysLabel(item)}</strong></td><td className={styles.number}>{formatMoney(cancelled ? "0" : item.payout_amount_eur)}</td><td className={styles.number}>{formatMoney(cancelled ? "0" : item.purchase_cost_eur)}</td><td className={styles.number}>{formatMoney(cancelled ? "0" : item.profit_amount_eur)}</td><td className={styles.costMethod}><strong>{item.details.purchase_cost_method || "Metodo non disponibile"}</strong><small>Fonte: {item.purchase_cost_source || "non disponibile"}</small></td><PaymentCell item={item} compact showRule /><td><strong>{cancelled ? "Ordine cancellato" : formatOrderDate(item.details.payment_due_at)}</strong><small>{cancelled ? "Non dovuto" : item.details.payment_date_final ? "Data definitiva" : "Data da definire o aggiornare"}</small></td><TicketCell item={item} /></tr>;
}

function PaymentSelectionSummary({ summary, selectedCount }: { summary: PaymentSummary; selectedCount: number }) {
  if (selectedCount === 0) return <div className={styles.paymentSummary} aria-labelledby="orders-payment-summary-title"><div className={styles.paymentSummaryHeading}><div><h4 id="orders-payment-summary-title">Riepilogo pagamenti</h4><p>Nessuna riga scelta. Usa le caselle qui sopra per creare il riepilogo.</p></div></div></div>;
  return <div className={styles.paymentSummary} aria-labelledby="orders-payment-summary-title"><div className={styles.paymentSummaryHeading}><div><h4 id="orders-payment-summary-title">Riepilogo pagamenti</h4><p>{summary.payment_payable_rows.toLocaleString("it-IT")} righe pagabili; le righe cancellate non entrano nel programma.</p></div>{summary.payment_all_available && <span className={`${styles.paymentBadge} ${styles.paymentAvailable}`}>Tutto disponibile</span>}</div><div className={styles.paymentEconomicCards}><div><span>Righe scelte</span><strong>{summary.selected_rows.toLocaleString("it-IT")}</strong><small>{summary.distinct_orders.toLocaleString("it-IT")} ordini distinti</small></div><div><span>Netto totale</span><strong>{formatMoney(summary.payout_amount_eur)}</strong><small>{summary.payment_payable_rows.toLocaleString("it-IT")} righe pagabili</small></div><div><span>Costo rilevato</span><strong>{formatMoney(summary.purchase_cost_eur)}</strong><small>{summary.known_cost_rows.toLocaleString("it-IT")} righe con costo</small></div><div><span>Guadagno rilevato</span><strong>{formatMoney(summary.profit_amount_eur)}</strong><small>{summary.loss_rows.toLocaleString("it-IT")} righe in perdita</small></div></div><div className={styles.paymentCards}><div><span>Netto disponibile</span><strong>{formatMoney(summary.available_payout_eur)}</strong><small>{summary.payment_available_rows.toLocaleString("it-IT")} righe</small></div><div><span>Netto in attesa</span><strong>{formatMoney(summary.waiting_payout_eur)}</strong><small>{summary.payment_waiting_rows.toLocaleString("it-IT")} righe</small></div><div><span>{summary.payment_all_dates_known ? "Disponibile entro" : "Ultima data definitiva nota"}</span><strong>{formatOrderDate(summary.latest_payment_due_at)}</strong><small>{summary.payment_scheduled_rows.toLocaleString("it-IT")} righe con data definitiva</small></div></div>
    {summary.missing_cost_rows > 0 && <p className={styles.notice}>{summary.missing_cost_rows.toLocaleString("it-IT")} {summary.missing_cost_rows === 1 ? "riga ha" : "righe hanno"} un costo ignoto. Costo e guadagno rilevati includono solo le righe con costo calcolabile.</p>}
    {summary.cancelled_rows > 0 && <p className={styles.hint}>{summary.cancelled_rows.toLocaleString("it-IT")} {summary.cancelled_rows === 1 ? "riga cancellata è conteggiata" : "righe cancellate sono conteggiate"} tra le righe scelte e {summary.cancelled_rows === 1 ? "ha" : "hanno"} contributo economico nullo.</p>}
    {summary.payment_unscheduled_rows > 0 && <p className={styles.notice}>{summary.payment_unscheduled_rows.toLocaleString("it-IT")} {summary.payment_unscheduled_rows === 1 ? "riga non ha" : "righe non hanno"} ancora una data definitiva. Può mancare la spedizione o la consegna, oppure un ticket aperto può rendere la data provvisoria.</p>}
    {summary.payment_payable_rows > 0 && summary.payment_all_dates_known && !summary.payment_all_available && <p className={styles.hint}>Tutte le date sono definitive; il netto in attesa sarà disponibile entro la data più lontana indicata.</p>}
  </div>;
}

function paymentDaysLabel(item: OrderItem): string {
  if (isCancelledOrder(item)) return "—";
  const days = item.details.payment_days_remaining;
  if (days === null || days === undefined) return "Non calcolabili";
  if (days > 0) return `${days.toLocaleString("it-IT")} mancanti`;
  if (days === 0) return "Disponibile oggi";
  return `Disponibile da ${Math.abs(days).toLocaleString("it-IT")} giorni`;
}

function isCancelledOrder(item: OrderItem): boolean {
  const status = item.status.toLowerCase();
  return status === "cancelled" || status === "canceled"
    || item.details.excluded_from_totals === true;
}

function Fact({ label, children }: { label: string; children: ReactNode }) { return <div><dt>{label}</dt><dd>{children || "—"}</dd></div>; }
function OrderDetail({ sellerId, lineId, query, onClose, onResponse }: { sellerId: string; lineId: string; query: OrdersQuery; onClose: () => void; onResponse: (response: Response) => boolean }) {
  const [item, setItem] = useState<OrderItem | null>(null), [error, setError] = useState("");
  const title = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    title.current?.focus();
    const abort = new AbortController(); let active = true;
    const timeout = setTimeout(() => abort.abort(), 25000);
    void (async () => {
      try {
        const response = await fetch(`/api/sellers/${sellerId}/orders/${lineId}?${ordersQueryString(query)}`, { cache: "no-store", signal: abort.signal });
        if (!active || !onResponse(response)) return;
        if (!response.ok) throw new Error();
        const raw: unknown = await response.json();
        const value = readOrderItem(raw && typeof raw === "object" && "item" in raw ? raw.item : null);
        if (!active) return;
        if (!value || value.id !== lineId) throw new Error();
        setItem(value);
      } catch { if (active) setError("Dettaglio non disponibile. Chiudi e riapri per riprovare."); }
      finally { clearTimeout(timeout); }
    })();
    return () => { active = false; abort.abort(); clearTimeout(timeout); };
  }, [sellerId, lineId, query, onResponse]);
  return <section className={styles.detail} aria-labelledby="order-detail-title"><div className={styles.sectionHeading}><h3 id="order-detail-title" tabIndex={-1} ref={title}>Dettaglio {item ? `ordine ${item.order_id}` : "ordine"}</h3><button className={styles.secondary} type="button" onClick={onClose}><DashboardIcon name="close" size={15} />Chiudi dettaglio</button></div>
    {error ? <p className="form-error" role="alert">{error}</p> : !item ? <p role="status">Caricamento dettaglio…</p> : <>
      {item.monetary_warnings.length > 0 && <div className={styles.notice}><strong>Dati da verificare</strong><ul>{item.monetary_warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul></div>}
      <h4>Prodotto e SKU</h4><dl className={styles.facts}><Fact label="Nome prodotto">{item.product_name}</Fact><Fact label="EAN">{item.ean}</Fact><Fact label="SKU composto">{item.sku}</Fact><Fact label="Quantità">{item.quantity}</Fact><Fact label="Fornitore nello SKU">{item.details.sku_supplier}</Fact><Fact label="Codice prodotto nello SKU">{item.details.sku_product_code}</Fact><Fact label="Costo unitario nello SKU (EUR)">{formatMoney(item.details.purchase_unit_cost_eur)}</Fact><Fact label="Prezzo minimo nello SKU (EUR)">{formatMoney(item.details.minimum_price_sku_eur)}</Fact></dl>
      <h4>Valori economici</h4><dl className={styles.facts}><Fact label="Vendita totale (EUR)">{formatMoney(item.sale_amount_eur)}</Fact><Fact label="Spedizione inclusa (EUR)">{formatMoney(item.shipping_amount_eur)}</Fact><Fact label="Costo acquisto (EUR)">{formatMoney(item.purchase_cost_eur)}</Fact><Fact label="Metodo costo">{item.details.purchase_cost_method}</Fact><Fact label="Origine costo">{item.purchase_cost_source}</Fact><Fact label="Commissione (EUR)">{formatMoney(item.commission_amount_eur)} · {formatPercent(item.commission_rate)}</Fact><Fact label="Origine commissione">{item.details.commission_source}</Fact><Fact label="Netto da ricevere (EUR)">{formatMoney(item.payout_amount_eur)}</Fact><Fact label="Origine netto">{item.details.payout_source}</Fact><Fact label="Utile (EUR)">{formatMoney(item.profit_amount_eur)}</Fact><Fact label="Utile sul costo di acquisto">{formatPercent(item.profit_pct)}</Fact><Fact label="Rimborso (EUR)">{formatMoney(item.details.refund_amount_eur)}</Fact><Fact label="Origine importi">{item.details.financial_source}</Fact></dl>
      <h4>Valuta originale e cambio</h4><dl className={styles.facts}><Fact label="Valuta marketplace">{item.currency}</Fact><Fact label="Vendita originale">{formatMoney(item.sale_amount, item.currency)}</Fact><Fact label="Commissione originale">{formatMoney(item.commission_amount, item.currency)}</Fact><Fact label="Netto originale">{formatMoney(item.payout_amount, item.currency)}</Fact><Fact label="Cambio: unità valuta per 1 EUR">{item.details.fx?.rate}</Fact><Fact label="Fonte e data del cambio">{[item.details.fx?.source, item.details.fx?.date].filter(Boolean).join(" · ")}</Fact></dl>
      <h4>Stato e spedizione</h4><dl className={styles.facts}><Fact label="Stato">{item.status_label || item.status}</Fact><Fact label="Paese / storefront">{item.storefront.toUpperCase()}</Fact><Fact label="Data ordine">{formatOrderDate(item.created_at)}</Fact><Fact label="Ultimo aggiornamento marketplace">{formatOrderDate(item.details.updated_at)}</Fact><Fact label="Corriere">{item.details.carrier}</Fact><Fact label="Tracking">{item.details.tracking}</Fact><Fact label="Data spedizione">{formatOrderDate(item.details.shipped_at)}</Fact><Fact label="Data ricezione">{formatOrderDate(item.details.received_at)}</Fact><Fact label="Data rilascio pagamento">{formatOrderDate(item.details.released_at)}</Fact><Fact label="Riga marketplace">{item.external_line_id}</Fact><Fact label="Ultima verifica dettagli">{formatOrderDate(item.details.detail_checked_at)}</Fact></dl>
      {item.marketplace === "kaufland" && <><h4>Programma pagamenti Kaufland</h4><dl className={styles.facts}><Fact label="Stato pagamento">{item.details.payment_status}</Fact><Fact label="Data prevista o effettiva">{formatOrderDate(item.details.payment_due_at)}</Fact><Fact label="Disponibilità">{item.details.payment_available ? "Disponibile" : "In attesa"}</Fact><Fact label="Data definitiva">{item.details.payment_date_final ? "Sì" : "No, ancora in aggiornamento"}</Fact><Fact label="Giorni rispetto a oggi">{item.details.payment_days_remaining === null || item.details.payment_days_remaining === undefined ? "Non calcolabili" : item.details.payment_days_remaining > 0 ? `${item.details.payment_days_remaining} mancanti` : item.details.payment_days_remaining === 0 ? "Disponibile oggi" : `Disponibile da ${Math.abs(item.details.payment_days_remaining)} giorni`}</Fact><Fact label="Regola applicata">{item.details.payment_rule}</Fact><Fact label="Fonte della data">{item.details.payment_source}</Fact><Fact label="Ritardo ticket">{item.details.ticket_delay_days ? `+${new Intl.NumberFormat("it-IT", { maximumFractionDigits: 2 }).format(item.details.ticket_delay_days)} giorni` : "Nessun ritardo"}</Fact><Fact label="Ticket collegati">{item.details.ticket_count ?? 0}</Fact><Fact label="Ticket aperti">{item.details.open_ticket_count ?? 0}</Fact><Fact label="ID ticket">{item.details.ticket_ids?.join(", ") || "Nessuno"}</Fact></dl>{item.details.ticket_open && <p className={styles.ticketNotice}>Un ticket è ancora aperto. La data di pagamento è provvisoria e continuerà a spostarsi fino alla chiusura.</p>}</>}
      {item.details.excluded_from_totals && <p className={styles.notice}>Riga esclusa dai totali economici secondo lo stato dell’ordine.</p>}{item.details.zero_economic_reason && <p className={styles.notice}>Importi azzerati secondo la regola dello stato: {item.details.zero_economic_reason}.</p>}
    </>}
  </section>;
}
