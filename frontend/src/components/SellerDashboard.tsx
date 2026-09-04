"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useWorkspace } from "./WorkspaceProvider";
import { PageHeader } from "./PageHeader";
import { StatusBadge } from "./StatusBadge";
import type { MarketplaceAccount } from "@/lib/types";

type Totals = {orders:number;rows:number;sales:number;profit:number;our_amount:number;partner_amount:number;missing_profit_rows:number};
type Detail = {order_id:string;order_date:string;marketplace:string;status:string;row_key?:string;product_title?:string;ean?:string;composite_sku?:string;supplier?:string;sale_eur:number;net_revenue_eur:number|null;partner_share_eur:number|null;our_share_eur:number|null;missing_reason?:string;missing_profit_rows?:number};
type Product = {product_key:string;product_title:string;ean:string;composite_sku:string;quantity:number;orders:number;sales_eur:number;margin_eur:number;margin_missing_rows:number};
type Snapshot = {seller_id:number;summary:Totals;previous:Totals&{date_from:string;date_to:string};top_products:Product[];trend:{date:string;orders:number;sales:number;profit:number;missing_profit_rows:number}[];details:{items:Detail[];total:number;offset:number;limit:number;view:string};accounts:MarketplaceAccount[];cached_rows:number;last_synced:string;undated_rows:number;plan:{plan_name:string;plan_code:string;active:boolean};jobs:{id:string;kind:string;status:string;message:string;error:string;progress_pct:number}[]};
const euro=(n:number|null|undefined)=>n==null?'Da verificare':new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(n);
const today=()=>{const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Rome',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());return ['year','month','day'].map(k=>parts.find(p=>p.type===k)?.value).join('-')};
const dayLabel=(s:string)=>{if(!s)return 'Mai sincronizzato';const d=new Date(s.length===10?s+'T12:00:00Z':s);return Number.isNaN(d.getTime())?'Data da verificare':new Intl.DateTimeFormat('it-IT',{timeZone:'Europe/Rome',day:'2-digit',month:'2-digit',year:'numeric'}).format(d)};

export function SellerDashboard(){
 const {user,seller}=useWorkspace();
 if(!user)return null;
 if(!seller)return <><PageHeader title="Pannello Seller" description="Configura il tuo negozio per iniziare."/><section className="panel"><p>Nessun Seller disponibile nel workspace selezionato.</p><Link href="/settings" className="primaryButton linkButton">Apri Impostazioni</Link></section></>;
 return <Panel key={`${user.active_tenant_id}:${seller.id}`} sellerId={seller.id}/>;
}

function Panel({sellerId}:{sellerId:number}){
 const {user,seller}=useWorkspace();
 const [from,setFrom]=useState(today),[to,setTo]=useState(today),[period,setPeriod]=useState('day');
 const [account,setAccount]=useState(0),[view,setView]=useState('lines'),[search,setSearch]=useState(''),[offset,setOffset]=useState(0);
 const [auto,setAuto]=useState(true),[reload,setReload]=useState(0),[loading,setLoading]=useState(true),[error,setError]=useState('');
 const [stored,setStored]=useState<{key:string;value:Snapshot}|null>(null),[accounts,setAccounts]=useState<MarketplaceAccount[]>([]);
 const [syncing,setSyncing]=useState(false),[syncMessage,setSyncMessage]=useState('');
 const mounted=useRef(true);useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 const detailRef=useRef<HTMLElement>(null);
 const key=[sellerId,from,to,account,view,search,offset].join('|');
 const data=stored?.key===key?stored.value:null;
 useEffect(()=>{if(!auto)return;const timer=setInterval(()=>setReload(x=>x+1),30000);return()=>clearInterval(timer)},[auto]);
 useEffect(()=>{
   setError('');if(!from||!to||to<from){setError('Scegli un intervallo valido: la data finale non può precedere quella iniziale.');setLoading(false);return;}
   const controller=new AbortController();let live=true;setLoading(true);
   const query=new URLSearchParams({date_from:from,date_to:to,view,search,offset:String(offset),limit:'50'});if(account)query.set('account_id',String(account));
   api<Snapshot>(`/sellers/${sellerId}/dashboard?${query}`,{signal:controller.signal})
    .then(value=>{if(live){setStored({key,value});setAccounts(value.accounts)}})
    .catch(e=>{if(live){setStored(null);setError(e instanceof Error?e.message:'Caricamento non riuscito.')}})
    .finally(()=>{if(live)setLoading(false)});
   return()=>{live=false;controller.abort()};
 },[key,reload,sellerId,from,to,account,view,search,offset]);
 const preset=(value:string)=>{
   setPeriod(value);setOffset(0);if(value==='custom')return;
   const end=today();let start=end;
   if(value==='month')start=end.slice(0,8)+'01';
   if(value==='week'){const d=new Date(end+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-((d.getUTCDay()+6)%7));start=d.toISOString().slice(0,10)}
   setFrom(start);setTo(end);
 };
 const openDetail=(next:string)=>{setView(next);setSearch('');setOffset(0);detailRef.current?.scrollIntoView({behavior:'smooth',block:'start'})};
 const canSync=Boolean(user&&(user.is_admin||user.permissions.includes('accounting'))&&(user.is_admin||['owner','admin','manager','operator'].includes(user.tenant_role)));
 const activeJobs=data?.jobs.filter(j=>['queued','running'].includes(j.status))||[];
 async function sync(){
   setSyncing(true);setSyncMessage('');const results:string[]=[];
   for(const item of accounts.filter(a=>a.active&&(!account||a.id===account)&&['kaufland','worten'].includes(a.marketplace.toLowerCase()))){
    if(!mounted.current)break;
    try{await api(`/sellers/${sellerId}/accounting/sync?account_id=${item.id}`,{method:'POST',body:JSON.stringify({marketplace:item.marketplace,date_from:from,date_to:to,full:false})});results.push(`${item.account_name}: sincronizzazione avviata.`)}
    catch(e){results.push(`${item.account_name}: ${e instanceof Error?e.message:'avvio non riuscito'}`)}
   }
   if(mounted.current){setSyncMessage(results.join(' ')||'Nessun account attivo supportato da sincronizzare.');setSyncing(false);setReload(x=>x+1)}
 }
 function exportPage(){
   if(!data)return;const columns=['order_id','order_date','marketplace','product_title','ean','sale_eur','net_revenue_eur','partner_share_eur','missing_reason'] as const;
   const cell=(value:unknown)=>{let text=String(value??'');if(/^[\s]*[=+@-]/.test(text)&&typeof value!=='number')text="'"+text;return '"'+text.replaceAll('"','""')+'"'};
   const text='\uFEFF'+[columns.join(';'),...data.details.items.map(r=>columns.map(c=>cell(r[c])).join(';'))].join('\r\n');
   const url=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=`dashboard-${sellerId}-${from}-${to}-pagina-${offset/50+1}.csv`;a.click();URL.revokeObjectURL(url);
 }
 const summary=data?.summary;
 return <>
  <PageHeader title="Pannello Seller" description={`${seller?.name} · vendite e margini secondo le regole della tua contabilità.`} action={<div className="headerActions"><Link href="/orders" className="secondaryButton linkButton">Ordini</Link><Link href="/settings" className="secondaryButton linkButton">Gestisci negozio</Link></div>}/>
  <section className="panel" aria-label="Filtri dashboard"><div className="settingsForm">
   <label>Periodo<select value={period} onChange={e=>preset(e.target.value)}><option value="day">Giorno</option><option value="week">Settimana corrente</option><option value="month">Mese corrente</option><option value="custom">Intervallo personalizzato</option></select></label>
   <label>Account marketplace<select value={account} onChange={e=>{setAccount(Number(e.target.value));setOffset(0)}}><option value="0">Tutti gli account del Seller</option>{accounts.map(a=><option key={a.id} value={a.id}>{a.account_name}{a.active?'':' · disattivato'}</option>)}</select></label>
   <label>Dal<input type="date" value={from} onChange={e=>{setPeriod('custom');setFrom(e.target.value);setOffset(0)}}/></label>
   <label>Al<input type="date" value={to} onChange={e=>{setPeriod('custom');setTo(e.target.value);setOffset(0)}}/></label>
   <div className="settingsActions"><button className="secondaryButton" onClick={()=>setReload(x=>x+1)} disabled={loading}>Aggiorna dati</button>{canSync&&<button className="primaryButton" disabled={syncing||!accounts.length||to<from||!from||!to||activeJobs.some(j=>j.kind==='accounting.orders.sync')} onClick={()=>void sync()}>{syncing?'Avvio…':'Sincronizza dati economici'}</button>}<label><input type="checkbox" checked={auto} onChange={e=>setAuto(e.target.checked)}/>Aggiornamento ogni 30 secondi</label></div>
  </div><p className="muted">Periodo in ora italiana. I valori provengono dalla contabilità salvata: la sincronizzazione degli ordini e quella dei dati economici sono operazioni distinte.</p></section>
  {error&&<div className="errorBox" role="alert">{error} <button onClick={()=>setReload(x=>x+1)}>Riprova</button></div>}
  {syncMessage&&<div className="panel" role="status">{syncMessage} <Link href="/jobs">Segui le attività</Link></div>}
  {loading&&<p role="status">Aggiornamento dashboard…</p>}
  {data&&<p className="muted">Ultima sincronizzazione contabile: {data.last_synced?dayLabel(data.last_synced):'mai eseguita'} · {data.cached_rows} righe salvate · Piano {data.plan.plan_name||data.plan.plan_code||'da verificare'}</p>}
  {data&&!data.cached_rows&&<section className="panel"><h2>Carica i dati economici del tuo negozio</h2><p>Gli ordini già importati rimangono nella sezione Ordini. Avvia “Sincronizza dati economici” per alimentare vendite, margini e Top 10.</p></section>}
  {data&&data.undated_rows>0&&<div className="errorBox" role="alert">{data.undated_rows} righe hanno una data non riconoscibile e sono escluse dai periodi. Verificale in Contabilità.</div>}
  <div className="stats sellerMetricGrid">{[
    ['Ordini nel periodo',summary?.orders,'orders'],['Vendite',summary?euro(summary.sales):undefined,'lines'],['Margine utile',summary?euro(summary.profit):undefined,'lines'],['Quota Seller',summary?euro(summary.partner_amount):undefined,'lines'],['Quota gestore',summary?euro(summary.our_amount):undefined,'lines'],['Righe da verificare',summary?.missing_profit_rows,'missing']
   ].map(([label,value,target])=><button className="statCard sellerMetric" key={String(label)} disabled={!data} onClick={()=>openDetail(String(target))}><span className="statLabel">{label}</span><strong className="statValue">{value??'—'}</strong><span className="statMeta">Apri dettaglio →</span></button>)}</div>
  {summary&&summary.missing_profit_rows>0&&<p role="status">Il margine include solo gli importi determinabili: {summary.missing_profit_rows} righe richiedono verifica. Non sono considerate a margine zero.</p>}
  {data&&<section className="panel"><h2>Confronto con il periodo precedente</h2><p>{dayLabel(data.previous.date_from)} – {dayLabel(data.previous.date_to)}, stessa durata.</p><div className="tableWrap"><table className="dataTable"><thead><tr><th>Indicatore</th><th>Periodo precedente</th><th>Periodo selezionato</th><th>Differenza</th></tr></thead><tbody><tr><td>Ordini</td><td>{data.previous.orders}</td><td>{data.summary.orders}</td><td>{data.summary.orders-data.previous.orders}</td></tr><tr><td>Vendite</td><td>{euro(data.previous.sales)}</td><td>{euro(data.summary.sales)}</td><td>{euro(data.summary.sales-data.previous.sales)}</td></tr><tr><td>Margine determinabile</td><td>{euro(data.previous.profit)}</td><td>{euro(data.summary.profit)}</td><td>{euro(data.summary.profit-data.previous.profit)}</td></tr></tbody></table></div>{data.previous.missing_profit_rows>0&&<p>Periodo precedente: {data.previous.missing_profit_rows} righe con margine da verificare.</p>}</section>}
  <section className="panel" style={{marginTop:20}}><h2>Top 10 prodotti più venduti</h2><p>Quantità vendute nel periodo. Annullamenti e righe senza vendite economiche non incrementano la classifica.</p>{data?.top_products.length?<div className="tableWrap"><table className="dataTable"><thead><tr><th>Prodotto</th><th>Quantità</th><th>Ordini</th><th>Vendite</th><th>Margine determinabile</th><th>Da verificare</th></tr></thead><tbody>{data.top_products.map(p=><tr key={p.product_key}><td><button className="textLink" onClick={()=>{setView('lines');setSearch(p.ean||p.composite_sku||p.product_title);setOffset(0);detailRef.current?.scrollIntoView({behavior:'smooth'})}}>{p.product_title}</button><small className="sellerSubline">{p.ean||p.composite_sku}</small></td><td>{p.quantity}</td><td>{p.orders}</td><td>{euro(p.sales_eur)}</td><td>{euro(p.margin_eur)}</td><td>{p.margin_missing_rows}</td></tr>)}</tbody></table></div>:<p>{loading?'Caricamento…':'Nessun prodotto venduto nel periodo selezionato.'}</p>}</section>
  <section ref={detailRef} className="panel" style={{marginTop:20}}><h2>Dettaglio dei valori</h2><div className="settingsForm"><label>Vista<select value={view} onChange={e=>{setView(e.target.value);setOffset(0)}}><option value="lines">Righe contabili</option><option value="orders">Ordini unici</option><option value="missing">Righe da verificare</option></select></label><label>Cerca ordine o prodotto<input value={search} onChange={e=>{setSearch(e.target.value);setOffset(0)}} placeholder="Ordine, EAN, SKU o fornitore"/></label></div>
   <p>La ricerca filtra il dettaglio; gli indicatori sopra mantengono il totale del periodo.</p>
   <div className="tableWrap"><table className="dataTable"><thead><tr><th>Ordine / Data</th><th>{view==='orders'?'Stato':'Prodotto'}</th><th>Marketplace</th><th>Vendite</th><th>Margine</th><th>Quota Seller</th><th>Verifica</th></tr></thead><tbody>{data?.details.items.map((r,i)=><tr key={`${r.row_key||r.order_id}:${i}`}><td>{r.order_id}<small className="sellerSubline">{dayLabel(r.order_date)}</small></td><td>{r.product_title||r.status}<small className="sellerSubline">{r.ean||r.composite_sku}</small></td><td>{r.marketplace}</td><td>{euro(r.sale_eur)}</td><td>{euro(r.net_revenue_eur)}</td><td>{euro(r.partner_share_eur)}</td><td>{r.missing_reason||(r.missing_profit_rows?`${r.missing_profit_rows} righe`:'—')}</td></tr>)}</tbody></table></div>
   {data&&!data.details.items.length&&<p>Nessun risultato per questi filtri.</p>}
   <div className="headerActions"><button className="secondaryButton" disabled={!data||offset===0} onClick={()=>setOffset(Math.max(0,offset-50))}>Precedente</button><span>{data?`${data.details.total?offset+1:0}–${Math.min(offset+50,data.details.total)} di ${data.details.total}`:'—'}</span><button className="secondaryButton" disabled={!data||offset+50>=data.details.total} onClick={()=>setOffset(offset+50)}>Successiva</button><button className="secondaryButton" disabled={!data?.details.items.length} onClick={exportPage}>Esporta pagina CSV</button></div>
  </section>
  {!!data?.trend.length&&<details className="panel" style={{marginTop:20}}><summary>Andamento giornaliero</summary><div className="tableWrap"><table className="dataTable"><thead><tr><th>Giorno</th><th>Ordini</th><th>Vendite</th><th>Margine determinabile</th><th>Righe da verificare</th></tr></thead><tbody>{data.trend.map(d=><tr key={d.date}><td>{dayLabel(d.date)}</td><td>{d.orders}</td><td>{euro(d.sales)}</td><td>{euro(d.profit)}</td><td>{d.missing_profit_rows}</td></tr>)}</tbody></table></div></details>}
  <section className="panel" style={{marginTop:20}}><h2>Attività del Seller</h2>{data?.jobs.length?data.jobs.map(j=><div className="sellerJobRow" key={j.id}><Link href="/jobs">{j.kind}</Link><StatusBadge status={j.status}/><span>{j.error||j.message||`${j.progress_pct||0}%`}</span></div>):<p>Nessuna attività recente.</p>}<Link href="/jobs">Apri tutte le attività →</Link></section>
 </>;
}
