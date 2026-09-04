"use client";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { WorkspacePage } from "@/components/WorkspacePage";
import { PageHeader } from "@/components/PageHeader";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { api } from "@/lib/api";
import type { Job, MarketplaceAccount } from "@/lib/types";

const terminal = (job: Job) => ["done", "error", "cancelled"].includes(job.status);
const dateString = (daysAgo: number) => {
  const date = new Date(); date.setDate(date.getDate() - daysAgo);
  return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
};

function Body() {
  const { user, seller } = useWorkspace();
  const [accounts, setAccounts] = useState<MarketplaceAccount[]>([]);
  const [accountId, setAccountId] = useState(0);
  const account = useMemo(() => accounts.find(item => item.id === accountId), [accounts, accountId]);
  const [items, setItems] = useState<Record<string, unknown>[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [accountsLoading, setAccountsLoading] = useState(false);
  const [error, setError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [reload, setReload] = useState(0);
  const [retryAccounts, setRetryAccounts] = useState(0);
  const [dateFrom, setDateFrom] = useState(() => dateString(30));
  const [dateTo, setDateTo] = useState(() => dateString(0));
  const [maximum, setMaximum] = useState(1000);
  const scope = `${user?.active_tenant_id || 0}:${seller?.id || 0}`;
  const currentScope = useRef(scope); currentScope.current = scope;
  const activeJob = Boolean(job && !terminal(job));

  useEffect(() => {
    let live = true;
    setAccounts([]); setAccountId(0); setItems([]); setTotal(0); setOffset(0);
    setJob(null); setSubmitting(false); setError(""); setSyncError("");
    if (!seller) { setAccountsLoading(false); return; }
    setAccountsLoading(true);
    api<MarketplaceAccount[]>(`/sellers/${seller.id}/accounts`)
      .then(result => { if (live) { setAccounts(result); setAccountId(result[0]?.id || 0); } })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : "Impossibile caricare gli account."); })
      .finally(() => { if (live) setAccountsLoading(false); });
    return () => { live = false; };
  }, [scope, retryAccounts]);

  useEffect(() => {
    let live = true;
    if (!seller || !account || account.seller_id !== seller.id) { setLoading(false); return; }
    setLoading(true); setError(""); setItems([]);
    api<{ items: Record<string, unknown>[]; total: number }>(
      `/sellers/${seller.id}/orders?account_id=${account.id}&marketplace=${encodeURIComponent(account.marketplace)}&limit=100&offset=${offset}&search=${encodeURIComponent(search)}`,
    ).then(result => { if (live) { setItems(result.items); setTotal(result.total); } })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : "Impossibile caricare gli ordini."); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [scope, account, offset, search, reload]);

  useEffect(() => {
    let live = true;
    if (!seller || job) return;
    api<Job[]>(`/jobs?seller_id=${seller.id}&kind_prefix=orders.&limit=20`)
      .then(jobs => { if (live) setJob(jobs.find(item => !terminal(item)) || null); })
      .catch(e => { if (live) setSyncError(e instanceof Error ? e.message : "Impossibile leggere le sincronizzazioni."); });
    return () => { live = false; };
  }, [scope, job?.job_id]);

  useEffect(() => {
    if (!job || terminal(job)) return;
    const id = job.job_id;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const next = await api<Job>(`/jobs/${encodeURIComponent(id)}`);
        if (!live) return;
        setJob(next); setSyncError("");
        if (terminal(next)) {
          if (next.status === "done") { setOffset(0); setReload(value => value + 1); }
          return;
        }
      } catch (e) {
        if (!live) return;
        setSyncError(e instanceof Error ? e.message : "Impossibile aggiornare lo stato della sincronizzazione.");
      }
      if (live) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { live = false; if (timer) clearTimeout(timer); };
  }, [scope, job?.job_id, job?.status]);

  const synchronize = async () => {
    if (!seller || !account || submitting || activeJob) return;
    if (account.marketplace === "worten" && (!dateFrom || !dateTo || dateFrom > dateTo)) {
      setSyncError("Scegli un intervallo di date valido."); return;
    }
    if (account.marketplace === "kaufland" && (!Number.isInteger(maximum) || maximum < 1 || maximum > 100000)) {
      setSyncError("Il limite deve essere compreso tra 1 e 100.000."); return;
    }
    const submittedScope = scope;
    setSubmitting(true); setSyncError("");
    try {
      const next = await api<Job>(`/sellers/${seller.id}/orders/sync?account_id=${account.id}`, {
        method: "POST",
        body: JSON.stringify({ marketplace: account.marketplace, environment: "live", include_tracking_details: true,
          ...(account.marketplace === "worten" ? { date_from: dateFrom, date_to: dateTo } : { maximum }),
        }),
      });
      if (currentScope.current !== submittedScope) return;
      setJob(next);
      if (next.status === "done") setReload(value => value + 1);
    } catch (e) {
      if (currentScope.current === submittedScope) setSyncError(e instanceof Error ? e.message : "Sincronizzazione non avviata.");
    } finally { if (currentScope.current === submittedScope) setSubmitting(false); }
  };

  return <>
    <PageHeader title="Ordini" description="Scarica gli ordini dal marketplace e consulta lo storico del tuo negozio."/>
    {!seller ? <section className="panel"><p>Seleziona un Seller disponibile. Se il selettore è vuoto, verifica il workspace in Impostazioni.</p><Link href="/settings">Apri Impostazioni</Link></section> : <>
      <div className="toolbar">
        <label>Account marketplace <select value={accountId} disabled={accountsLoading || submitting || activeJob} onChange={e => { setAccountId(Number(e.target.value)); setOffset(0); }}>
          {!accounts.length && <option value={0}>{accountsLoading ? "Caricamento…" : "Nessun account disponibile"}</option>}
          {accounts.map(item => <option key={item.id} value={item.id}>{item.marketplace} · {item.account_name}</option>)}
        </select></label>
        <input aria-label="Cerca ordini" placeholder="Cerca ordine, EAN, SKU…" value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }}/>
      </div>
      {account && <section className="panel">
        <div className="panelTitle"><h2>Sincronizza ordini</h2></div>
        <p>Il collegamento API salva le credenziali; questo comando importa gli ordini e aggiorna i conteggi della dashboard.</p>
        <div className="toolbar">
          {account.marketplace === "worten" ? <>
            <label>Dal <input type="date" value={dateFrom} onChange={e => setDateFrom(e.target.value)}/></label>
            <label>Al <input type="date" value={dateTo} onChange={e => setDateTo(e.target.value)}/></label>
          </> : <label>Massimo unità ordine da scaricare <input type="number" min={1} max={100000} value={maximum} onChange={e => setMaximum(Number(e.target.value))}/></label>}
          <button className="primaryButton" disabled={submitting || activeJob} onClick={() => void synchronize()}>{submitting ? "Avvio…" : activeJob ? "Sincronizzazione in corso…" : "Sincronizza ordini"}</button>
        </div>
      </section>}
      {syncError && <div className="errorBox" role="alert">{syncError}</div>}
      {job && <section className="panel" aria-live="polite">
        <p>{job.status === "queued" ? "Sincronizzazione in coda: in attesa del worker." : job.status === "done" ? "Sincronizzazione completata. Lo storico è stato aggiornato." : job.status === "error" ? `Sincronizzazione non riuscita: ${job.error || job.message}` : job.status === "cancelled" ? "Sincronizzazione annullata." : job.message || "Download degli ordini in corso…"}</p>
        {activeJob && <progress max={100} value={job.progress_pct} aria-label="Avanzamento sincronizzazione"/>}
        <Link href="/jobs">Vedi dettagli in Attività</Link>
      </section>}
      {error && <div className="errorBox" role="alert">{error} <button className="secondaryButton" onClick={() => accounts.length ? setReload(value => value + 1) : setRetryAccounts(value => value + 1)}>Riprova</button></div>}
      <section className="panel">
        <div className="panelTitle"><h2>{account?.account_name || "Ordini"}</h2><span className="pill">{error ? "—" : total.toLocaleString("it-IT")} righe</span></div>
        {loading || accountsLoading ? <div className="empty">Caricamento…</div> : error ? <div className="empty">Dati non disponibili: risolvi l’errore indicato sopra.</div> : items.length ? <div className="dataTable"><table>
          <thead><tr><th>Ordine</th><th>Data</th><th>Marketplace</th><th>SKU</th><th>EAN</th><th>Stato</th><th>Totale</th></tr></thead>
          <tbody>{items.map((item, i) => <tr key={String(item.row_key || item.id_order_unit || i)}>
            <td>{String(item.order_id || item.id_order || item.order_number || "")}</td><td>{String(item.order_created || item.date_inserted_iso || item.created_at || "")}</td><td>{account?.marketplace}</td><td>{String(item.sku || item.id_offer || "")}</td><td>{String(item.ean || "")}</td><td>{String(item.status || item.order_status || "")}</td><td>{String(item.sale_amount_eur ?? item.total_amount ?? "")}</td>
          </tr>)}</tbody>
        </table></div> : <div className="empty">{search ? "Nessun ordine corrisponde alla ricerca." : account ? "Nessun ordine importato. Avvia Sincronizza ordini per scaricarli dal marketplace." : <Link href="/settings">Collega un marketplace in Impostazioni.</Link>}</div>}
        <div className="pager"><button className="secondaryButton" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - 100))}>Indietro</button><span>{total ? offset + 1 : 0}–{Math.min(offset + 100, total)} di {total}</span><button className="secondaryButton" disabled={offset + 100 >= total || loading} onClick={() => setOffset(offset + 100)}>Avanti</button></div>
      </section>
    </>}
  </>;
}
export default function Page() { return <WorkspacePage><Body/></WorkspacePage>; }
