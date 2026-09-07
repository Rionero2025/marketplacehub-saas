"use client";

import { useCallback, useEffect, useRef, useState, useTransition, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { WorkspaceResult } from "../lib/workspace";
import { readConnections, type MarketplaceConnection } from "../lib/marketplace-connections-types";
import { formatMoney, formatOrderDate, formatPercent, ordersDefaultBounds, ordersFilterInput, ordersQueryString, readOrderItem, readOrderJob, readOrderSelection, readOrdersList, readOrdersQuery, type OrderEnvironment, type OrderItem, type OrderJob, type OrdersList, type OrdersQuery, type OrdersSummary, type SelectionAction } from "../lib/orders-types";
import { DashboardIcon } from "./DashboardIcon";
import styles from "./SellerOrdersPanel.module.css";

const supported = (account: MarketplaceConnection) => account.marketplace === "kaufland" || account.marketplace === "worten";
const connected = (account: MarketplaceConnection) => account.active && account.connection_status === "connected";
const running = (job: OrderJob | null) => job?.status === "queued" || job?.status === "running";
const names: Record<string, string> = { kaufland: "Kaufland", worten: "Worten" };
const statusNames: Record<string, string> = { cancelled: "Cancellato", need_to_be_sent: "Da spedire", open: "Aperto", received: "Ricevuto", returned: "Reso", returned_paid: "Reso rimborsato", sent: "Spedito", sent_and_autopaid: "Spedito e pagato" };
const statusName = (status: string) => statusNames[status] || status.replaceAll("_", " ");
const initialQuery = (accountId: string, environment: OrderEnvironment): OrdersQuery => ({ account_id: accountId, environment, page: 1, page_size: 50, search: "", statuses: [], storefronts: [], currencies: [], carriers: [], date_from: "", date_to: "", status_selection: "all", storefront_selection: "all", currency_selection: "all", tracking: "all", commission: "all", amount_min: "", amount_max: "" });

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
  const [selectionPending, setSelectionPending] = useState(false);
  const [selectionNeedsRefresh, setSelectionNeedsRefresh] = useState(false);
  const [optimisticIds, setOptimisticIds] = useState<Set<string> | null>(null);
  const [exporting, setExporting] = useState<"selected" | "filtered" | null>(null);
  const alive = useRef(true), listVersion = useRef(0), syncBusy = useRef(false);
  const listAbort = useRef<AbortController | null>(null), syncAbort = useRef<AbortController | null>(null);
  const selectionAbort = useRef<AbortController | null>(null), exportAbort = useRef<AbortController | null>(null);
  const selectionQueue = useRef<SelectionAction[]>([]), selectionBusy = useRef(false), exportBusy = useRef(false);
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
      setList(value); setJob(value.latest_job); setNeedsRefresh(false); setSelectionNeedsRefresh(false); setOptimisticIds(null);
    } catch { if (current()) { setList(null); setError("Impossibile caricare gli ordini. Aggiorna i dati per riprovare."); } }
    finally { clearTimeout(timeout); if (current()) setLoading(false); }
  }, [baseUrl, query, responseAllowed, sellerId, account.id, environment]);

  useEffect(() => { alive.current = true; return () => { alive.current = false; listVersion.current += 1; listAbort.current?.abort(); syncAbort.current?.abort(); selectionAbort.current?.abort(); exportAbort.current?.abort(); selectionQueue.current = []; downloadUrls.current.forEach((url) => URL.revokeObjectURL(url)); downloadUrls.current.clear(); }; }, []);
  useEffect(() => { setSelected(null); void loadList(); return () => { listVersion.current += 1; listAbort.current?.abort(); }; }, [loadList]);
  useEffect(() => { setOptimisticIds(null); return () => { selectionAbort.current?.abort(); exportAbort.current?.abort(); selectionQueue.current = []; }; }, [query]);
  useEffect(() => { onBusyChange?.(selectionPending || exporting !== null); return () => onBusyChange?.(false); }, [onBusyChange, selectionPending, exporting]);

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
    if (selectionBusy.current || exportBusy.current) return;
    const validated = readOrdersQuery(new URLSearchParams(ordersQueryString({ ...draft, search: draft.search.trim(), amount_min: draft.amount_min.replace(",", "."), amount_max: draft.amount_max.replace(",", "."), page: 1 })));
    if (!validated) { setError("Controlla il periodo e gli importi: il valore minimo deve precedere il massimo."); return; }
    filtersApplied.current = true; setQuery(validated);
  }

  function changeSelection(action: SelectionAction) {
    if (!alive.current || loading || !list || selectionNeedsRefresh || exportBusy.current) return;
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
            const response = await fetch(`${baseUrl}/selection`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...scope, selection_id: selectionId, filters: ordersFilterInput(query), ...command }), cache: "no-store", signal: abort.signal });
            if (!current() || !responseAllowed(response)) return;
            if (!response.ok) throw new Error();
            const raw: unknown = await response.json();
            const selection = readOrderSelection(raw && typeof raw === "object" && "selection" in raw ? raw.selection : null);
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

  async function exportOrders(kind: "selected" | "filtered") {
    if (exportBusy.current || selectionBusy.current || loading || selectionNeedsRefresh || !list || !list.selection.selected_count || !list.selection.filtered_count) return;
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
  const filtersActive = Boolean(query.search || query.status_selection === "selected" || query.storefront_selection === "selected" || query.currency_selection === "selected" || query.carriers.length || query.tracking !== "all" || query.commission !== "all" || query.amount_min || query.amount_max || query.date_from || query.date_to);
  const pages = Math.max(1, Math.ceil((list?.total ?? 0) / query.page_size));
  const checkedIds = optimisticIds ?? new Set(list?.selection.selected_ids ?? []);
  const actionBusy = loading || selectionPending || exporting !== null;
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
    <section className={styles.card} aria-labelledby="orders-list-title"><div className={styles.sectionHeading}><div><h2 id="orders-list-title">Archivio ordini</h2><p>{list ? `${list.total.toLocaleString("it-IT")} righe${filtersActive ? " corrispondenti ai filtri" : " salvate"}` : "Consulta le righe importate dal marketplace."} · {account.account_name}</p></div><button type="button" className={styles.secondary} disabled={actionBusy || syncing} onClick={() => void loadList()}><DashboardIcon name="refresh" size={15} />Aggiorna dati</button></div>
      <form className={styles.filters} onSubmit={applyFilters}><label className={`${styles.field} ${styles.search}`}>Cerca ordine o prodotto<input type="search" value={draft.search} maxLength={200} placeholder="Ordine, prodotto, EAN, SKU…" onChange={(event) => setDraft({ ...draft, search: event.target.value })} /></label><label className={styles.field}>Dal<input type="date" value={draft.date_from} onChange={(event) => setDraft({ ...draft, date_from: event.target.value })} /></label><label className={styles.field}>Al<input type="date" value={draft.date_to} min={draft.date_from || undefined} onChange={(event) => setDraft({ ...draft, date_to: event.target.value })} /></label>
        <details className={styles.advancedFilters} open><summary>Stati, paesi e filtri avanzati</summary><div className={styles.choiceGrid}>
          <FilterChoices label="Stati ordine" options={list?.filters.statuses ?? []} value={draft.status_selection === "all" ? null : draft.statuses} display={statusName} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, status_selection: value === null ? "all" : "selected", statuses: value ?? [] })} />
          <FilterChoices label="Paesi / storefront" options={list?.filters.storefronts ?? []} value={draft.storefront_selection === "all" ? null : draft.storefronts} display={(value) => value.toUpperCase() || "Paese non disponibile"} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, storefront_selection: value === null ? "all" : "selected", storefronts: value ?? [] })} />
          <FilterChoices label="Valute originali" options={list?.filters.currencies ?? []} value={draft.currency_selection === "all" ? null : draft.currencies} display={(value) => value || "Valuta non disponibile"} disabled={actionBusy} onChange={(value) => setDraft({ ...draft, currency_selection: value === null ? "all" : "selected", currencies: value ?? [] })} />
        </div><div className={styles.advancedFields}>
          <label className={styles.field}>Tracking<select value={draft.tracking} disabled={actionBusy} onChange={(event) => setDraft({ ...draft, tracking: event.target.value as OrdersQuery["tracking"] })}><option value="all">Tutti</option><option value="present">Con tracking</option><option value="missing">Senza tracking</option></select></label>
          <label className={styles.field}>Commissione<select value={draft.commission} disabled={actionBusy} onChange={(event) => setDraft({ ...draft, commission: event.target.value as OrdersQuery["commission"] })}><option value="all">Tutte</option><option value="present">Disponibile</option><option value="missing">Non disponibile</option></select></label>
          <fieldset className={styles.choiceGroup} disabled={actionBusy}><legend>Corrieri</legend><div className={styles.choiceActions}><button type="button" className={styles.linkButton} onClick={() => setDraft({ ...draft, carriers: [] })}>Tutti i corrieri</button></div><div className={styles.choiceOptions}>{list?.filters.carriers.filter(Boolean).map((carrier) => <label key={carrier}><input type="checkbox" checked={draft.carriers.includes(carrier)} onChange={(event) => setDraft({ ...draft, carriers: event.target.checked ? [...draft.carriers, carrier] : draft.carriers.filter((value) => value !== carrier) })} />{carrier}</label>)}</div><small className={styles.hint}>{draft.carriers.length ? `${draft.carriers.length} corrieri selezionati` : "Nessuna restrizione: tutti i corrieri."}</small></fieldset>
          <label className={styles.field}>Vendita minima (EUR)<input type="number" min="0" step="0.01" inputMode="decimal" value={draft.amount_min} required disabled={actionBusy} onChange={(event) => setDraft({ ...draft, amount_min: event.target.value })} /></label>
          <label className={styles.field}>Vendita massima (EUR)<input type="number" min="0" step="0.01" inputMode="decimal" value={draft.amount_max} required disabled={actionBusy} onChange={(event) => setDraft({ ...draft, amount_max: event.target.value })} /></label>
        </div></details>
        <div className={styles.filterActions}><button className={styles.primary} type="submit" disabled={actionBusy}>Applica filtri</button><button className={styles.secondary} type="button" disabled={actionBusy} onClick={() => { if (selectionBusy.current || exportBusy.current) return; const reset = defaultBounds.current ?? initialQuery(account.id, environment); filtersApplied.current = false; setDraft(reset); setQuery(reset); }}>Ripristina filtri iniziali</button></div>
      </form><p className={styles.hint}>Il periodo filtra la data di creazione in UTC; le date visualizzate sono in ora italiana. «Nessuno» in stati, paesi o valute esclude tutte le righe di quel filtro. Gli importi sono in EUR; «—» indica un valore non disponibile.</p>
      {list && <><div className={styles.selectionBar}><div><strong>{list.selection.selected_count.toLocaleString("it-IT")} di {list.selection.filtered_count.toLocaleString("it-IT")} righe selezionate</strong><p>La selezione comprende tutte le pagine dei filtri applicati.</p></div><div className={styles.filterActions}><button type="button" className={styles.secondary} disabled={loading || selectionNeedsRefresh || exporting !== null || !list.total} onClick={() => changeSelection({ action: "select_all" })}>Seleziona tutti i filtrati</button><button type="button" className={styles.secondary} disabled={loading || selectionNeedsRefresh || exporting !== null || !list.total} onClick={() => changeSelection({ action: "clear" })}>Deseleziona tutti</button></div>{selectionPending && <span role="status">Salvataggio selezione…</span>}</div>
        <OrdersTotals summary={list.selection.summary} pending={selectionPending} />
        <div className={styles.exports}><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || list.selection.selected_count === 0} onClick={() => void exportOrders("selected")}>{exporting === "selected" ? "Preparazione CSV…" : "Esporta CSV selezionati"}</button><button type="button" className={styles.secondary} disabled={actionBusy || selectionNeedsRefresh || list.selection.selected_count === 0} onClick={() => void exportOrders("filtered")}>{exporting === "filtered" ? "Preparazione CSV…" : "Esporta CSV tutti i filtrati"}</button>{list.selection.selected_count === 0 && <p>Seleziona almeno una riga per visualizzare i totali operativi ed esportare.</p>}</div></>}
      {selected && <OrderDetail key={selected} sellerId={sellerId} lineId={selected} query={query} onClose={() => setSelected(null)} onResponse={responseAllowed} />}
      {loading ? <p className={styles.empty} role="status">Caricamento ordini…</p> : list && list.items.length ? <div className={styles.tableScroll} tabIndex={0} role="region" aria-label="Tabella ordini, scorrimento orizzontale"><table className={styles.table}><thead><tr><th>Selezione</th><th>Ordine / data</th><th>Prodotto / EAN</th><th>SKU composto</th><th>Quantità</th><th>Stato / paese</th><th>Vendita</th><th>Costo acquisto</th><th>Commissione</th><th>Netto da ricevere</th><th>Utile / % sul costo</th><th>Dettaglio</th></tr></thead><tbody>{list.items.map((item) => <tr key={item.id}><td className={styles.selectionCell}><input type="checkbox" aria-label={`Seleziona ordine ${item.order_id}, riga ${item.external_line_id}`} checked={checkedIds.has(item.id)} disabled={loading || selectionNeedsRefresh || exporting !== null} onChange={(event) => changeSelection({ action: "set", line_id: item.id, selected: event.target.checked })} /></td><td><strong>{item.order_id}</strong><small>{formatOrderDate(item.created_at)}</small><small>Riga {item.external_line_id}</small></td><td className={styles.product}><strong>{item.product_name || "Nome non disponibile"}</strong><small>EAN {item.ean || "—"}</small></td><td className={styles.sku}>{item.sku || "—"}</td><td className={styles.number}>{item.quantity}</td><td><span className={styles.badge}>{item.status_label || statusName(item.status)}</span><small>{item.storefront.toUpperCase() || "—"}</small>{item.details.excluded_from_totals && <small>Escluso dai totali</small>}</td><td className={styles.number}>{formatMoney(item.sale_amount_eur)}</td><td className={styles.number}>{formatMoney(item.purchase_cost_eur)}<small>{item.purchase_cost_source}</small></td><td className={styles.number}>{formatMoney(item.commission_amount_eur)}<small>{formatPercent(item.commission_rate)}</small></td><td className={styles.number}>{formatMoney(item.payout_amount_eur)}</td><td className={styles.number}>{formatMoney(item.profit_amount_eur)}<small>{formatPercent(item.profit_pct)}</small></td><td><button type="button" className={styles.linkButton} aria-expanded={selected === item.id} onClick={() => setSelected(selected === item.id ? null : item.id)}>Apri<span className={styles.srOnly}> ordine {item.order_id}, riga {item.external_line_id}</span></button>{item.monetary_warnings.length > 0 && <small className={styles.warningText}>Dati da verificare</small>}</td></tr>)}</tbody></table></div>
        : list && <div className={styles.empty}><h3>{filtersActive ? "Nessun ordine corrisponde ai filtri" : "Nessun ordine salvato"}</h3><p>{filtersActive ? "Modifica il periodo o azzera i filtri." : "Avvia la sincronizzazione per importare gli ordini di questo account."}</p></div>}
      {list && <div className={styles.pagination}><label>Righe per pagina <select value={query.page_size} disabled={actionBusy} onChange={(event) => { const page_size = Number(event.target.value); setQuery({ ...query, page: 1, page_size }); setDraft({ ...draft, page_size }); }}><option value={25}>25</option><option value={50}>50</option><option value={100}>100</option></select></label><span>Pagina {query.page} di {pages}</span><button className={styles.secondary} type="button" disabled={actionBusy || query.page <= 1} onClick={() => setQuery({ ...query, page: query.page - 1 })}>Precedente</button><button className={styles.secondary} type="button" disabled={actionBusy || query.page >= pages} onClick={() => setQuery({ ...query, page: query.page + 1 })}>Successiva</button></div>}
    </section>
  </>;
}

function FilterChoices({ label, options, value, display, onChange, disabled }: { label: string; options: string[]; value: string[] | null; display: (value: string) => string; onChange: (value: string[] | null) => void; disabled: boolean }) {
  return <fieldset className={styles.choiceGroup} disabled={disabled}><legend>{label}</legend><div className={styles.choiceActions}><button type="button" className={styles.linkButton} onClick={() => onChange(null)}>Tutti</button><button type="button" className={styles.linkButton} onClick={() => onChange([])}>Nessuno</button><span>{value === null ? "Tutti inclusi" : value.length ? `${value.length} selezionati` : "Nessuno incluso"}</span></div><div className={styles.choiceOptions}>{options.length ? options.map((option) => <label key={option}><input type="checkbox" checked={value === null || value.includes(option)} onChange={(event) => { const selected = value ?? options; const next = event.target.checked ? [...new Set([...selected, option])] : selected.filter((entry) => entry !== option); onChange(next.length === options.length && options.every((entry) => next.includes(entry)) ? null : next); }} />{display(option)}</label>) : <p>Nessun valore disponibile.</p>}</div></fieldset>;
}

export function OrdersTotals({ summary, pending }: { summary: OrdersSummary; pending: boolean }) {
  const cards = [
    ["Vendita", formatMoney(summary.sale_amount_eur)], ["Commissioni", formatMoney(summary.commission_amount_eur)],
    ["Netto da ricevere", formatMoney(summary.payout_amount_eur)], ["Costo acquisto", formatMoney(summary.purchase_cost_eur)],
    ["Utile", formatMoney(summary.profit_amount_eur)], ["Utile sul costo", formatPercent(summary.profit_pct)],
  ];
  return <section className={styles.totals} aria-labelledby="orders-totals-title" aria-busy={pending}><h3 id="orders-totals-title">Totali delle righe selezionate</h3><p>{summary.distinct_orders.toLocaleString("it-IT")} ordini distinti · {summary.selected_rows.toLocaleString("it-IT")} righe · {summary.quantity.toLocaleString("it-IT")} pezzi</p><div className={styles.totalCards}>{cards.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    {summary.missing_economic_rows > 0 && <p className={styles.notice}>{summary.missing_economic_rows} righe hanno vendita, commissione o netto incompleti: sono escluse da questi tre totali. Non sono valori a zero.</p>}
    {summary.missing_cost_rows > 0 && <p className={styles.notice}>{summary.missing_cost_rows} righe non hanno costo e utile calcolabili: sono escluse dai totali di costo e utile.</p>}
    {summary.missing_currencies.length > 0 && <p className={styles.notice}>Valute delle righe con importi incompleti: {summary.missing_currencies.join(", ")}.</p>}
    {summary.cancelled_rows > 0 && <p className={styles.hint}>{summary.cancelled_rows} righe cancellate sono incluse nel conteggio, con contributo economico nullo.</p>}
    {summary.loss_rows > 0 && <p className={styles.warningText}>{summary.loss_rows} righe in perdita sono incluse nei totali.</p>}
    <p className={styles.hint}>{Math.max(0, summary.complete_economic_rows - summary.cancelled_rows)} righe non cancellate con dati economici completi · {summary.known_cost_rows} con costo calcolabile. Costi da SKU: {summary.sku_cost_rows}; da catalogo: {summary.catalog_cost_rows}.</p>
  </section>;
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
      <h4>Valori economici</h4><dl className={styles.facts}><Fact label="Vendita totale (EUR)">{formatMoney(item.sale_amount_eur)}</Fact><Fact label="Spedizione inclusa (EUR)">{formatMoney(item.shipping_amount_eur)}</Fact><Fact label="Costo acquisto (EUR)">{formatMoney(item.purchase_cost_eur)}</Fact><Fact label="Origine costo">{item.purchase_cost_source}</Fact><Fact label="Commissione (EUR)">{formatMoney(item.commission_amount_eur)} · {formatPercent(item.commission_rate)}</Fact><Fact label="Origine commissione">{item.details.commission_source}</Fact><Fact label="Netto da ricevere (EUR)">{formatMoney(item.payout_amount_eur)}</Fact><Fact label="Origine netto">{item.details.payout_source}</Fact><Fact label="Utile (EUR)">{formatMoney(item.profit_amount_eur)}</Fact><Fact label="Utile sul costo di acquisto">{formatPercent(item.profit_pct)}</Fact><Fact label="Rimborso (EUR)">{formatMoney(item.details.refund_amount_eur)}</Fact><Fact label="Origine importi">{item.details.financial_source}</Fact></dl>
      <h4>Valuta originale e cambio</h4><dl className={styles.facts}><Fact label="Valuta marketplace">{item.currency}</Fact><Fact label="Vendita originale">{formatMoney(item.sale_amount, item.currency)}</Fact><Fact label="Commissione originale">{formatMoney(item.commission_amount, item.currency)}</Fact><Fact label="Netto originale">{formatMoney(item.payout_amount, item.currency)}</Fact><Fact label="Cambio: unità valuta per 1 EUR">{item.details.fx?.rate}</Fact><Fact label="Fonte e data del cambio">{[item.details.fx?.source, item.details.fx?.date].filter(Boolean).join(" · ")}</Fact></dl>
      <h4>Stato e spedizione</h4><dl className={styles.facts}><Fact label="Stato">{item.status_label || item.status}</Fact><Fact label="Paese / storefront">{item.storefront.toUpperCase()}</Fact><Fact label="Data ordine">{formatOrderDate(item.created_at)}</Fact><Fact label="Ultimo aggiornamento marketplace">{formatOrderDate(item.details.updated_at)}</Fact><Fact label="Corriere">{item.details.carrier}</Fact><Fact label="Tracking">{item.details.tracking}</Fact><Fact label="Data spedizione">{formatOrderDate(item.details.shipped_at)}</Fact><Fact label="Data ricezione">{formatOrderDate(item.details.received_at)}</Fact><Fact label="Data rilascio pagamento">{formatOrderDate(item.details.released_at)}</Fact><Fact label="Riga marketplace">{item.external_line_id}</Fact><Fact label="Ultima verifica dettagli">{formatOrderDate(item.details.detail_checked_at)}</Fact></dl>
      {item.details.excluded_from_totals && <p className={styles.notice}>Riga esclusa dai totali economici secondo lo stato dell’ordine.</p>}{item.details.zero_economic_reason && <p className={styles.notice}>Importi azzerati secondo la regola dello stato: {item.details.zero_economic_reason}.</p>}
    </>}
  </section>;
}
