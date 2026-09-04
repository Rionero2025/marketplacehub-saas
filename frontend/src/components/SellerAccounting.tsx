"use client";
import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';
import type { MarketplaceAccount } from '@/lib/types';
import { useWorkspace } from './WorkspaceProvider';
import { PageHeader } from './PageHeader';

type Value = string|number|null;
type Row = {id:number;order_id:string;order_date:string;status_label:string;net_revenue_eur:number|null;partner_share_eur:number|null;our_share_eur:number|null;edit_values:Record<string,Value>;[key:string]:unknown};
type Data = {seller:{id:number;name:string};profit_split:{our_pct:number;partner_pct:number;our_amount:number;partner_amount:number};filter_options:{suppliers:string[];countries:string[];statuses:{value:string;label:string}[]};items:Row[];total:number;totals:Record<string,number>;missing_rows:number;editable_fields:Record<string,string>};
type Catalogs = {configured:boolean;enabled_ids:number[];options:{price_list_id:number;supplier_name:string;list_name:string}[]};
const labels:Record<string,string>={supplier:'Fornitore',product_title:'Prodotto',ean:'EAN',quantity:'Quantità',sale_eur:'Vendita €',purchase_cost_eur:'Acquisto €',commission_eur:'Commissione €',refund_eur:'Rimborso €',payout_eur:'Da ricevere €',extra_cost_eur:'Costo extra €',supplier_order_number:'Ordine fornitore',payment_estimated:'Pagamento stimato',customer_name:'Cliente',tracking:'Tracking',receipt:'Scontrino',note:'Note'};
const euro=(v:unknown)=>v==null?'Da verificare':new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(Number(v));
const today=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Rome'}).format(new Date());

export function SellerAccounting(){
 const {user,seller}=useWorkspace();
 if(!user||!seller)return <p>Seleziona un Seller per aprire la contabilità.</p>;
 return <Panel key={`${user.active_tenant_id}:${seller.id}`} sellerId={seller.id}/>;
}

function Panel({sellerId}:{sellerId:number}){
 const {user,seller}=useWorkspace();
 const [accounts,setAccounts]=useState<MarketplaceAccount[]>([]),[account,setAccount]=useState(0);
 const [suppliers,setSuppliers]=useState<string[]>([]),[statuses,setStatuses]=useState<string[]>([]),[countries,setCountries]=useState<string[]>([]);
 const [from,setFrom]=useState(()=>today().slice(0,8)+'01'),[to,setTo]=useState(today),[search,setSearch]=useState(''),[missing,setMissing]=useState(false),[offset,setOffset]=useState(0);
 const [reload,setReload]=useState(0),[stored,setStored]=useState<{key:string;value:Data}|null>(null),[loading,setLoading]=useState(true),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 const [catalogs,setCatalogs]=useState<Catalogs|null>(null),[catalogIds,setCatalogIds]=useState<number[]>([]),[catalogError,setCatalogError]=useState('');
 const [edit,setEdit]=useState<Row|null>(null),[draft,setDraft]=useState<Record<string,string>>({}),[editError,setEditError]=useState('');
 const mounted=useRef(true);useEffect(()=>{mounted.current=true;return()=>{mounted.current=false}},[]);
 const writable=Boolean(user&&(user.is_admin||['owner','admin','manager','operator'].includes(user.tenant_role)));
 const base=`/sellers/${sellerId}/accounting`;
 const params=new URLSearchParams({account_id:String(account),date_from:from,date_to:to,search,missing_only:String(missing)});
 for(const [name,values] of [['suppliers',suppliers],['statuses',statuses],['countries',countries]] as const)for(const value of values)params.append(name,value);
 const query=params.toString();
 const key=query+'|'+offset;
 const data=stored?.key===key?stored.value:null;
 const valid=Boolean(account&&from&&to&&to>=from);
 useEffect(()=>{
  const controller=new AbortController();let live=true;
  api<MarketplaceAccount[]>(`/sellers/${sellerId}/accounting/accounts`,{signal:controller.signal}).then(values=>{if(live){setAccounts(values);setAccount(values[0]?.id||0);if(!values.length)setLoading(false)}}).catch(e=>{if(live){setError(e.message);setLoading(false)}});
  return()=>{live=false;controller.abort()};
 },[sellerId]);
 useEffect(()=>{
  const controller=new AbortController();let live=true;
  api<Catalogs>(base+'/catalogs',{signal:controller.signal}).then(value=>{if(live){setCatalogs(value);setCatalogIds(value.enabled_ids);setCatalogError('')}}).catch(e=>{if(live)setCatalogError(e.message)});
  return()=>{live=false;controller.abort()};
 },[base,reload]);
 useEffect(()=>{
  if(!account)return;
  if(!valid){setError('Scegli un intervallo di date valido.');setLoading(false);return;}
  const controller=new AbortController();let live=true;setLoading(true);setError('');
  api<Data>(`${base}/rows?${query}&offset=${offset}&limit=50`,{signal:controller.signal}).then(value=>{if(live)setStored({key,value})}).catch(e=>{if(live){setStored(null);setError(e.message)}}).finally(()=>{if(live)setLoading(false)});
  return()=>{live=false;controller.abort()};
 },[base,key,query,offset,reload,account,valid]);
 function filtersChanged(){setOffset(0);setEdit(null);setNotice('')}
 function resetFacets(){setStored(null);setSuppliers([]);setStatuses([]);setCountries([])}
 async function operation(path:string,body:unknown,method='POST'){
  setBusy(true);setNotice('');setError('');
  try{await api(base+path,{method,body:JSON.stringify(body)});if(mounted.current){setNotice(method==='PUT'?'Scelta dei listini salvata. Avvia il ricalcolo per applicarla ai costi già importati.':'Attività avviata. Segui l’avanzamento in Attività e poi aggiorna i dati.');setReload(x=>x+1)}}
  catch(e){if(mounted.current)setError(e instanceof Error?e.message:'Operazione non riuscita.')}
  finally{if(mounted.current)setBusy(false)}
 }
 function startEdit(row:Row){setEdit(row);setDraft(Object.fromEntries(Object.entries(row.edit_values).map(([k,v])=>[k,String(v??'')])));setEditError('')}
 async function saveEdit(){
  if(!edit||!data)return;
  const fields:Record<string,Value>={};
  for(const [name,value] of Object.entries(draft)){
   if(value===String(edit.edit_values[name]??''))continue;
   const numeric=['integer','money_zero','money_nullable'].includes(data.editable_fields[name]);
   const parsed=numeric?(value.trim()===''?null:Number(value.replace(',','.'))):value;
   if(numeric&&parsed!==null&&!Number.isFinite(parsed)){setEditError('Inserisci importi numerici validi.');return;}
   fields[name]=parsed;
  }
  if(!Object.keys(fields).length){setEdit(null);return;}
  setBusy(true);setEditError('');
  try{await api(`${base}/rows/${edit.id}?account_id=${account}`,{method:'PATCH',body:JSON.stringify({fields,expected:edit.edit_values})});if(mounted.current){setEdit(null);setNotice('Modifiche salvate. Margini e quote ricalcolati con le regole originali.');setReload(x=>x+1)}}
  catch(e){if(mounted.current)setEditError(e instanceof Error?e.message:'Salvataggio non riuscito.')}
  finally{if(mounted.current)setBusy(false)}
 }
 async function download(){
  setBusy(true);setError('');
  try{
   const response=await fetch(`/api/v1${base}/export.xlsx?${query}`,{credentials:'include',cache:'no-store'});
   if(!response.ok){const body=await response.json().catch(()=>({}));throw new Error(typeof body.detail==='string'?body.detail:`Esportazione non riuscita (HTTP ${response.status}).`)}
   const blob=await response.blob();if(!mounted.current)return;
   const url=URL.createObjectURL(blob),anchor=document.createElement('a');anchor.href=url;anchor.download=`contabilita-${sellerId}-${account}-${from}-${to}.xlsx`;anchor.click();URL.revokeObjectURL(url);
  }catch(e){if(mounted.current)setError(e instanceof Error?e.message:'Esportazione non riuscita.')}
  finally{if(mounted.current)setBusy(false)}
 }
 const selected=accounts.find(a=>a.id===account);
 const jobEnabled=valid&&selected?.active&&['kaufland','worten'].includes(selected.marketplace.toLowerCase());
 return <>
  <PageHeader title="Contabilità Seller" description="Ordini, costi e quote. Le modifiche manuali restano valide dopo le sincronizzazioni." action={<Link className="secondaryButton linkButton" href="/jobs">Apri Attività</Link>}/>
  <section className="panel"><strong>Seller: {seller?.name || "Seleziona un Seller"}</strong><p className="muted">Importi e quote appartengono esclusivamente al Seller selezionato e al suo account marketplace. Cambiando Seller cambiano anche listini, dati e percentuali.</p></section>
  <section className="panel" aria-label="Filtri contabilità"><fieldset disabled={busy||Boolean(edit)} className="accountingFieldset"><div className="settingsForm">
   <label>Account marketplace<select value={account} onChange={e=>{filtersChanged();resetFacets();setAccount(Number(e.target.value))}}><option value="0" disabled>Seleziona account</option>{accounts.map(a=><option key={a.id} value={a.id}>{a.account_name}{a.active?'':' · disattivato'}</option>)}</select></label>
   <label>Dal<input type="date" value={from} onChange={e=>{filtersChanged();resetFacets();setFrom(e.target.value)}}/></label><label>Al<input type="date" value={to} onChange={e=>{filtersChanged();resetFacets();setTo(e.target.value)}}/></label>
   <label>Cerca ordine, prodotto o cliente<input value={search} onChange={e=>{filtersChanged();setSearch(e.target.value)}} maxLength={200}/></label>
   <FacetFilter label="Fornitore" values={suppliers} options={(stored?.value.filter_options?.suppliers||[]).map(value=>({value,label:value}))} onChange={v=>{filtersChanged();setSuppliers(v)}}/>
   <FacetFilter label="Stato ordine" values={statuses} options={stored?.value.filter_options?.statuses||[]} onChange={v=>{filtersChanged();setStatuses(v)}}/>
   <FacetFilter label="Nazione" values={countries} options={(stored?.value.filter_options?.countries||[]).map(value=>({value,label:value}))} onChange={v=>{filtersChanged();setCountries(v)}}/>
   <label className="accountingCheck"><input type="checkbox" checked={missing} onChange={e=>{filtersChanged();setMissing(e.target.checked)}}/>Solo margini da verificare</label>
   <div className="settingsActions"><button className="secondaryButton" disabled={loading} onClick={()=>setReload(x=>x+1)}>Aggiorna dati</button>{writable&&<><button className="primaryButton" disabled={!jobEnabled} onClick={()=>void operation(`/sync?account_id=${account}`,{marketplace:selected?.marketplace,date_from:from,date_to:to,full:false})}>Sincronizza contabilità</button><button className="secondaryButton" disabled={!jobEnabled} onClick={()=>void operation(`/refresh-costs?account_id=${account}`,{marketplace:selected?.marketplace,date_from:from,date_to:to})}>Ricalcola costi da listini</button></>}<button className="secondaryButton" disabled={!valid||!data?.total} onClick={()=>void download()}>Esporta Excel filtrato</button></div>
  </div></fieldset><p className="muted">Date in ora italiana. Totali ed Excel includono tutte le righe filtrate, anche oltre la pagina corrente. Massimo 20.000 righe per download. Nei filtri a scelta multipla, nessuna selezione include tutti i valori; usa Ctrl o Cmd per più scelte.</p></section>
  {error&&<div className="errorBox" role="alert">{error}</div>}{notice&&<div className="panel" role="status">{notice}</div>}
  {!accounts.length&&!loading&&<section className="panel"><p>Nessun account marketplace disponibile.</p><Link href="/settings">Collega un marketplace</Link></section>}
  {loading&&<p role="status">Caricamento contabilità…</p>}
  {data&&<><div className="stats sellerMetricGrid">{[
   ['Vendite nette',data.totals.sale],['Commissioni',data.totals.commission],['Da ricevere',data.totals.payout],['Margine utile noto',data.totals.net_revenue],['Acquisti',data.totals.purchase],['Rimborsi',data.totals.refund],['Margine lordo',data.totals.gross_margin],
   [`Quota gestore · ${data.profit_split?.our_pct ?? "—"}%`,data.profit_split?.our_amount],[`Quota ${data.seller?.name || 'Seller'} · ${data.profit_split?.partner_pct ?? "—"}%`,data.profit_split?.partner_amount]
  ].map(([label,v])=><section className="panel statCard" key={String(label)}><span>{label}</span><strong>{euro(v)}</strong></section>)}</div><p>{data.total} righe filtrate · {data.missing_rows} con margine da verificare. Gli importi mancanti non sono stimati.</p></>}

  {edit&&data&&<section className="panel" aria-label="Modifica riga contabile"><h2>Modifica ordine {edit.order_id}</h2><p>Le regole di annullamento e rimborso prevalgono sugli importi manuali. Il salvataggio controlla che la riga non sia cambiata nel frattempo.</p><fieldset className="accountingFieldset" disabled={busy}><div className="settingsForm">{Object.entries(data.editable_fields).map(([name,kind])=><label key={name}>{labels[name]||name}<input value={draft[name]??''} maxLength={4000} inputMode={['integer','money_zero','money_nullable'].includes(kind)?'decimal':'text'} onChange={e=>setDraft(v=>({...v,[name]:e.target.value}))}/></label>)}</div></fieldset>{editError&&<div className="errorBox" role="alert">{editError}</div>}<div className="settingsActions"><button className="primaryButton" disabled={busy} onClick={()=>void saveEdit()}>Salva modifiche</button><button className="secondaryButton" disabled={busy} onClick={()=>{setEdit(null);setReload(x=>x+1)}}>Chiudi e ricarica</button></div></section>}
  {data&&<section className="panel"><div className="panelTitle"><h2>Righe contabili</h2><span>Pagina {offset/50+1}</span></div><div className="dataTable accountingTable"><table><thead><tr><th>Ordine / data</th><th>Market / Nazione</th><th>Prodotto / EAN</th><th>Stato</th><th>Fornitore</th><th>Q.tà</th><th>Vendita</th><th>Acquisto</th><th>Commissione</th><th>Rimborso</th><th>Da ricevere</th><th>Extra</th><th>Margine netto</th><th>Quota Seller</th><th>Quota gestore</th><th>Note</th>{writable&&<th>Azioni</th>}</tr></thead><tbody>{data.items.map(row=><tr key={row.id}><td>{row.order_id}<small className="muted">{row.order_date}</small></td><td>{String(row.market_label||'')}<small className="muted">{String(row.country_code||'')}</small></td><td>{String(row.product_title||'')}<small className="muted">{String(row.ean||'')}</small></td><td>{row.status_label}</td><td>{String(row.supplier||'')}</td><td>{String(row.quantity||1)}</td>{['sale_eur','purchase_cost_eur','commission_eur','refund_eur','payout_eur','extra_cost_eur','net_revenue_eur','partner_share_eur','our_share_eur'].map(k=><td key={k}>{euro(row[k])}</td>)}<td>{String(row.note||'')}</td>{writable&&<td><button className="secondaryButton" disabled={busy||Boolean(edit)} onClick={()=>startEdit(row)}>Modifica</button></td>}</tr>)}</tbody></table></div>{!data.total&&<p>Nessuna riga nel periodo. Verifica i filtri o avvia la sincronizzazione contabile.</p>}<div className="settingsActions"><button className="secondaryButton" disabled={busy||Boolean(edit)||offset===0} onClick={()=>setOffset(v=>Math.max(0,v-50))}>Precedente</button><button className="secondaryButton" disabled={busy||Boolean(edit)||offset+50>=data.total} onClick={()=>setOffset(v=>v+50)}>Successiva</button></div></section>}
  <section className="panel"><h2>Listini per il costo di acquisto</h2><p>La selezione vale per tutti gli account del Seller. Le modifiche manuali ai costi mantengono la precedenza anche dopo il ricalcolo.</p>{catalogError&&<div className="errorBox" role="alert">{catalogError}</div>}{catalogs&&<><p className="muted">{catalogs.configured?'Selezione esplicita: i nuovi listini vanno abilitati qui.':'Nessuna selezione salvata: tutti i listini disponibili sono abilitati.'}</p><fieldset className="accountingFieldset" disabled={!writable||busy||Boolean(edit)}>{catalogs.options.map(item=><label className="accountingCatalog" key={item.price_list_id}><input type="checkbox" checked={catalogIds.includes(item.price_list_id)} onChange={e=>setCatalogIds(ids=>e.target.checked?[...ids,item.price_list_id]:ids.filter(id=>id!==item.price_list_id))}/>{item.supplier_name} · {item.list_name}</label>)}</fieldset>{!catalogs.options.length&&<p>Nessun listino disponibile. Aggiungi un listino nella sezione Cataloghi.</p>}{writable&&<button className="secondaryButton" disabled={busy||Boolean(edit)} onClick={()=>void operation('/catalogs',{enabled_ids:catalogIds},'PUT')}>Salva scelta listini</button>}</>}</section>
 </>;
}

function FacetFilter({label,values,options,onChange}:{label:string;values:string[];options:{value:string;label:string}[];onChange:(v:string[])=>void}){
 // Retain selected options while the next filtered request is loading.
 const available=[...options];for(const value of values)if(!available.some(o=>o.value===value))available.push({value,label:value});
 return <div><label>{label}<select aria-label={label} multiple size={3} value={values} onChange={e=>onChange(Array.from(e.target.selectedOptions,o=>o.value))}>{available.map(o=><option key={o.value} value={o.value}>{o.label}</option>)}</select></label><button type="button" className="secondaryButton" onClick={()=>onChange([])} disabled={!values.length}>Tutti: {label.toLowerCase()}</button></div>;
}
