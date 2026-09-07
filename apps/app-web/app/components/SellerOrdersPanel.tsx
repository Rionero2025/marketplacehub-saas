"use client";

import { useCallback, useEffect, useRef, useState, useTransition, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { WorkspaceResult } from "../lib/workspace";
import { readConnections, type MarketplaceConnection } from "../lib/marketplace-connections-types";
import { formatMoney, formatOrderDate, formatPercent, ordersQueryString, readOrderItem, readOrderJob, readOrdersList, type OrderEnvironment, type OrderItem, type OrderJob, type OrdersList, type OrdersQuery } from "../lib/orders-types";
import { DashboardIcon } from "./DashboardIcon";
import styles from "./SellerOrdersPanel.module.css";

const supported = (account: MarketplaceConnection) => account.marketplace === "kaufland" || account.marketplace === "worten";
const connected = (account: MarketplaceConnection) => account.active && account.connection_status === "connected";
const running = (job: OrderJob | null) => job?.status === "queued" || job?.status === "running";
const names: Record<string, string> = { kaufland: "Kaufland", worten: "Worten" };
const statusNames: Record<string, string> = { cancelled: "Cancellato", need_to_be_sent: "Da spedire", open: "Aperto", received: "Ricevuto", returned: "Reso", returned_paid: "Reso rimborsato", sent: "Spedito", sent_and_autopaid: "Spedito e pagato" };
const statusName = (status: string) => statusNames[status] || status.replaceAll("_", " ");
const initialQuery = (accountId: string, environment: OrderEnvironment): OrdersQuery => ({ account_id: accountId, environment, page: 1, page_size: 50, search: "", statuses: [], storefronts: [], date_from: "", date_to: "" });

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
      <div className={styles.accountBar}><label className={styles.field}>Account marketplace<select value={accountId} disabled={loading || !accounts?.length} onChange={(event) => { setAccountId(event.target.value); setEnvironment("live"); }}><option value="" disabled>Seleziona un account</option>{accounts?.map((item) => <option key={item.id} value={item.id} disabled={!supported(item)}>{names[item.marketplace] ?? item.marketplace} · {item.account_name}{!supported(item) ? " · ordini non disponibili" : !connected(item) ? " · da verificare" : ""}</option>)}</select></label>
        {account?.marketplace === "kaufland" && <label className={styles.field}>Ambiente<select value={environment} onChange={(event) => setEnvironment(event.target.value as OrderEnvironment)}><option value="live">Live</option><option value="playground">Playground (test)</option></select></label>}
        <button type="button" className={styles.secondary} disabled={loading} onClick={() => setRefresh((value) => value + 1)}><DashboardIcon name="refresh" size={15} />Aggiorna account</button><a href="/seller/marketplaces/accounts">Gestisci collegamenti</a>
      </div>
      {loading && <p role="status" className={styles.muted}>Caricamento account…</p>}
      {error && <p className="form-error" role="alert">{error}</p>}
      {!loading && accounts && !account && <div className={styles.empty}><h2>Collega un marketplace per iniziare</h2><p>Gli ordini saranno importati dall’account scelto per questo negozio.</p><a href="/seller/marketplaces">Collega marketplace</a></div>}
    </section>
    {account && supported(account) && !loading && <OrderAccountPanel key={`${sellerId}:${account.id}:${environment}`} sellerId={sellerId} account={account} environment={environment} />}
  </>;
}

function OrderAccountPanel({ sellerId, account, environment }: { sellerId: string; account: MarketplaceConnection; environment: OrderEnvironment }) {
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
  const alive = useRef(true), listVersion = useRef(0), syncBusy = useRef(false);
  const listAbort = useRef<AbortController | null>(null), syncAbort = useRef<AbortController | null>(null);
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
      setList(value); setJob(value.latest_job); setNeedsRefresh(false);
    } catch { if (current()) { setList(null); setError("Impossibile caricare gli ordini. Aggiorna i dati per riprovare."); } }
    finally { clearTimeout(timeout); if (current()) setLoading(false); }
  }, [baseUrl, query, responseAllowed, sellerId]);

  useEffect(() => { alive.current = true; return () => { alive.current = false; listVersion.current += 1; listAbort.current?.abort(); syncAbort.current?.abort(); }; }, []);
  useEffect(() => { setSelected(null); void loadList(); return () => { listVersion.current += 1; listAbort.current?.abort(); }; }, [loadList]);

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
        if (!running(value)) await loadList();
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
    if (draft.date_from && draft.date_to && draft.date_from > draft.date_to) { setError("La data iniziale deve precedere la data finale."); return; }
    setQuery({ ...draft, search: draft.search.trim(), page: 1 });
  }
  const filtersActive = Boolean(query.search || query.statuses.length || query.storefronts.length || query.date_from || query.date_to);
  const pages = Math.max(1, Math.ceil((list?.total ?? 0) / query.page_size));
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
    <section className={styles.card} aria-labelledby="orders-list-title"><div className={styles.sectionHeading}><div><h2 id="orders-list-title">Archivio ordini</h2><p>{list ? `${list.total.toLocaleString("it-IT")} righe${filtersActive ? " corrispondenti ai filtri" : " salvate"}` : "Consulta le righe importate dal marketplace."} · {account.account_name}</p></div><button type="button" className={styles.secondary} disabled={loading || syncing} onClick={() => void loadList()}><DashboardIcon name="refresh" size={15} />Aggiorna dati</button></div>
      <form className={styles.filters} onSubmit={applyFilters}><label className={`${styles.field} ${styles.search}`}>Cerca ordine o prodotto<input type="search" value={draft.search} maxLength={200} placeholder="Ordine, prodotto, EAN, SKU…" onChange={(event) => setDraft({ ...draft, search: event.target.value })} /></label><label className={styles.field}>Dal<input type="date" value={draft.date_from} onChange={(event) => setDraft({ ...draft, date_from: event.target.value })} /></label><label className={styles.field}>Al<input type="date" value={draft.date_to} min={draft.date_from || undefined} onChange={(event) => setDraft({ ...draft, date_to: event.target.value })} /></label>
        <label className={styles.field}>Stato<select multiple size={3} value={draft.statuses} onChange={(event) => setDraft({ ...draft, statuses: Array.from(event.target.selectedOptions, (option) => option.value) })}>{list?.filters.statuses.map((status) => <option key={status} value={status}>{statusName(status)}</option>)}</select></label><label className={styles.field}>Paese / storefront<select multiple size={3} value={draft.storefronts} onChange={(event) => setDraft({ ...draft, storefronts: Array.from(event.target.selectedOptions, (option) => option.value) })}>{list?.filters.storefronts.map((storefront) => <option key={storefront} value={storefront}>{storefront.toUpperCase()}</option>)}</select></label>
        <div className={styles.filterActions}><button className={styles.primary} type="submit" disabled={loading}>Applica filtri</button><button className={styles.secondary} type="button" disabled={loading} onClick={() => { const reset = initialQuery(account.id, environment); setDraft(reset); setQuery(reset); }}>Azzera filtri</button></div>
      </form><p className={styles.hint}>Il periodo filtra la data di creazione in UTC; le date visualizzate sono in ora italiana. Stato e paese consentono più selezioni. Gli importi della tabella sono in EUR; nel dettaglio trovi valuta originale e provenienza. «—» indica un valore non disponibile.</p>
      {selected && <OrderDetail key={selected} sellerId={sellerId} lineId={selected} query={query} onClose={() => setSelected(null)} onResponse={responseAllowed} />}
      {loading ? <p className={styles.empty} role="status">Caricamento ordini…</p> : list && list.items.length ? <div className={styles.tableScroll} tabIndex={0} role="region" aria-label="Tabella ordini, scorrimento orizzontale"><table className={styles.table}><thead><tr><th>Ordine / data</th><th>Prodotto / EAN</th><th>SKU composto</th><th>Quantità</th><th>Stato / paese</th><th>Vendita</th><th>Costo acquisto</th><th>Commissione</th><th>Netto da ricevere</th><th>Utile / % sul costo</th><th>Dettaglio</th></tr></thead><tbody>{list.items.map((item) => <tr key={item.id}><td><strong>{item.order_id}</strong><small>{formatOrderDate(item.created_at)}</small><small>Riga {item.external_line_id}</small></td><td className={styles.product}><strong>{item.product_name || "Nome non disponibile"}</strong><small>EAN {item.ean || "—"}</small></td><td className={styles.sku}>{item.sku || "—"}</td><td className={styles.number}>{item.quantity}</td><td><span className={styles.badge}>{item.status_label || statusName(item.status)}</span><small>{item.storefront.toUpperCase() || "—"}</small>{item.details.excluded_from_totals && <small>Escluso dai totali</small>}</td><td className={styles.number}>{formatMoney(item.sale_amount_eur)}</td><td className={styles.number}>{formatMoney(item.purchase_cost_eur)}<small>{item.purchase_cost_source}</small></td><td className={styles.number}>{formatMoney(item.commission_amount_eur)}<small>{formatPercent(item.commission_rate)}</small></td><td className={styles.number}>{formatMoney(item.payout_amount_eur)}</td><td className={styles.number}>{formatMoney(item.profit_amount_eur)}<small>{formatPercent(item.profit_pct)}</small></td><td><button type="button" className={styles.linkButton} aria-expanded={selected === item.id} onClick={() => setSelected(selected === item.id ? null : item.id)}>Apri<span className={styles.srOnly}> ordine {item.order_id}, riga {item.external_line_id}</span></button>{item.monetary_warnings.length > 0 && <small className={styles.warningText}>Dati da verificare</small>}</td></tr>)}</tbody></table></div>
        : list && <div className={styles.empty}><h3>{filtersActive ? "Nessun ordine corrisponde ai filtri" : "Nessun ordine salvato"}</h3><p>{filtersActive ? "Modifica il periodo o azzera i filtri." : "Avvia la sincronizzazione per importare gli ordini di questo account."}</p></div>}
      {list && <div className={styles.pagination}><label>Righe per pagina <select value={query.page_size} disabled={loading} onChange={(event) => { const page_size = Number(event.target.value); setQuery({ ...query, page: 1, page_size }); setDraft({ ...draft, page_size }); }}><option value={25}>25</option><option value={50}>50</option><option value={100}>100</option></select></label><span>Pagina {query.page} di {pages}</span><button className={styles.secondary} type="button" disabled={loading || query.page <= 1} onClick={() => setQuery({ ...query, page: query.page - 1 })}>Precedente</button><button className={styles.secondary} type="button" disabled={loading || query.page >= pages} onClick={() => setQuery({ ...query, page: query.page + 1 })}>Successiva</button></div>}
    </section>
  </>;
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
