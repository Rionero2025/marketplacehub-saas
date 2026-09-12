"use client";

import {useEffect,useRef,useState} from "react";
import {useRouter} from "next/navigation";
import type {WorkspaceResult} from "../lib/workspace";
import {defaultRules,readPublicationIndex,readPublicationJob,readPublicationOptions,type PublicationIndex,type PublicationJob,type PublicationRules,type Options} from "../lib/publication-types";
import styles from "./SellerPublicationPanel.module.css";

const euro=new Intl.NumberFormat("it-IT",{style:"currency",currency:"EUR"});
const labels:Record<string,string>={draft:"Anteprima da confermare",queued:"In coda",running:"Invio in corso",interrupted:"Invio interrotto: verifica gli esiti",finished:"Invio terminato",pending:"Da inviare",invalid:"Da correggere",sending:"Invio in corso",accepted:"Accettato dall’API",submitted:"Importazione ricevuta",rejected:"Rifiutato",unknown:"Esito da verificare",skipped:"Escluso"};
const countries:Record<string,string>={de:"Germania · EUR",cz:"Cechia · CZK",sk:"Slovacchia · EUR",at:"Austria · EUR",pl:"Polonia · PLN",fr:"Francia · EUR",it:"Italia · EUR",pt:"Portogallo · EUR"};

export function SellerPublicationPage({result}:{result:WorkspaceResult}) {
  const router=useRouter(),seller=result.workspace?.active_seller;
  return <div className="catalog-page"><div className="workspace-heading"><div><p className="eyebrow">MARKETPLACE</p><h1>Pubblica sui marketplace</h1><p>Prepara le offerte dalle tue viste salvate e controlla gli esiti degli invii.</p></div></div>
    {result.error?<section className="workspace-section"><p role="alert">{result.error}</p><button onClick={()=>router.refresh()}>Riprova</button></section>:seller?<><div className="catalog-shop"><strong>{seller.name}</strong><a href="/seller/settings/store">Cambia negozio</a></div><SellerPublicationPanel key={seller.id} sellerId={seller.id}/></>:<p>Seleziona un negozio dalle impostazioni.</p>}
  </div>;
}

export function SellerPublicationPanel({sellerId}:{sellerId:string}) {
  const router=useRouter();
  const [index,setIndex]=useState<PublicationIndex|null>(null),[rules,setRules]=useState({...defaultRules});
  const [job,setJob]=useState<PublicationJob|null>(null),[options,setOptions]=useState<Options>({});
  const [selected,setSelected]=useState<string[]>([]),[confirmation,setConfirmation]=useState("");
  const [pending,setPending]=useState(true),[error,setError]=useState(""),[uncertain,setUncertain]=useState(false);
  const alive=useRef(true),busy=useRef(false),controllers=useRef(new Set<AbortController>());
  const root=`/api/sellers/${sellerId}/publication`;
  const account=index?.accounts.find(a=>a.id===rules.account_id),isWorten=account?.marketplace==="worten";
  const views=index?.views.filter(v=>v.account_ids.includes(rules.account_id))??[];
  const editable=!!index?.can_manage&&!pending;

  async function request(path:string,method="GET",body?:unknown) {
    const c=new AbortController();controllers.current.add(c);const timer=setTimeout(()=>c.abort(),70000);
    try {
      const res=await fetch(root+path,{method,body:body?JSON.stringify(body):undefined,headers:body?{"content-type":"application/json"}:undefined,cache:"no-store",signal:c.signal});
      if(!alive.current)throw new Error("Richiesta annullata.");
      if(res.status===401){router.replace("/login/seller");throw new Error("Sessione scaduta.");}
      const value:unknown=await res.json();
      if(!res.ok)throw new Error(value&&typeof value==="object"&&"detail" in value&&typeof value.detail==="string"?value.detail:"Operazione non riuscita.");
      return value;
    } finally {clearTimeout(timer);controllers.current.delete(c);}
  }
  async function refresh() {
    const value=readPublicationIndex(await request(""),sellerId);
    if(!value)throw new Error("Dati della pubblicazione non validi.");
    setIndex(value);return value;
  }
  async function run(action:()=>Promise<void>) {
    if(busy.current)return;busy.current=true;setPending(true);setError("");
    try{await action();}catch(e){if(alive.current)setError(e instanceof Error?e.message:"Operazione non riuscita.");}
    finally{busy.current=false;if(alive.current)setPending(false);}
  }
  useEffect(()=>{
    alive.current=true;void run(async()=>{await refresh();});
    return()=>{alive.current=false;controllers.current.forEach(c=>c.abort());};
    // Component is keyed by Seller: a shop switch discards selection and requests.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[sellerId]);
  async function open(id:string) {
    const value=readPublicationJob(await request(`/jobs/${id}`),sellerId);
    if(!value||value.id!==id)throw new Error("Invio non valido.");
    setRules({...value.rules});setOptions({});setJob(value);setSelected(value.rows.filter(r=>r.status==="pending").map(r=>r.id));setConfirmation("");setUncertain(false);
  }
  useEffect(()=>{
    if(!job||!["queued","running"].includes(job.status))return;
    let cancelled=false;let timer:ReturnType<typeof setTimeout>;
    const id=job.id;
    async function poll(){
      if(!busy.current){try{const value=readPublicationJob(await request(`/jobs/${id}`),sellerId);if(!cancelled&&value?.id===id)setJob(value);}catch(e){if(!cancelled)setError(e instanceof Error?e.message:"Impossibile aggiornare lo stato.");}}
      if(!cancelled)timer=setTimeout(poll,4000);
    }
    timer=setTimeout(poll,4000);return()=>{cancelled=true;clearTimeout(timer);};
    // Poll only the selected receipt; never repeat a POST.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  },[job?.id,job?.status]);
  function change(patch:Partial<PublicationRules>){setRules({...rules,...patch});setJob(null);setSelected([]);setConfirmation("");setUncertain(false);}
  function choose(id:string){const a=index?.accounts.find(a=>a.id===id);setRules({...defaultRules,account_id:id,storefront:a?.marketplace==="worten"?"pt":"de",playground:a?.marketplace!=="worten",handling:a?.marketplace==="worten"?2:1});setJob(null);setOptions({});setSelected([]);setConfirmation("");setUncertain(false);}
  async function preview(){const value=readPublicationJob(await request("/preview","POST",rules),sellerId);if(!value)throw new Error("Anteprima non valida.");setJob(value);setSelected(value.rows.filter(r=>r.status==="pending").map(r=>r.id));setConfirmation("");setUncertain(false);await refresh();}
  async function submit(){if(!job)return;setUncertain(true);const value=readPublicationJob(await request(`/jobs/${job.id}/submit`,"POST",{confirmation,selected}),sellerId);if(!value)throw new Error("Invio registrato: aggiorna lo storico per verificarlo.");setJob(value);setConfirmation("");setUncertain(false);await refresh();}
  const numeric=(key:keyof PublicationRules,label:string,min=0,max=999999,step="any")=><label>{label}<input type="number" value={String(rules[key])} min={min} max={max} step={step} onChange={e=>change({[key]:e.target.value===""?0:Number(e.target.value)})}/></label>;
  const metadata=(key:"shipping_group"|"warehouse"|"logistic_class"|"state_code",label:string,list:string)=><label>{label}{options[list]?.length?<select value={rules[key]} onChange={e=>change({[key]:e.target.value})}><option value="">Scegli dalla configurazione collegata</option>{options[list].map(o=><option key={o.id} value={o.id}>{o.name} · {o.id}</option>)}</select>:<input value={rules[key]} onChange={e=>change({[key]:e.target.value})}/>}</label>;
  const processed=job?job.total-(job.counts.pending??0)-(job.counts.sending??0):0;
  const percent=job?Math.floor(processed/job.total*100):0;
  const missingShipping=job?.marketplace==="kaufland"&&(!job.rules.shipping_group||!job.rules.warehouse);
  const canSubmit=job&&["draft","interrupted"].includes(job.status)&&editable&&!uncertain&&!missingShipping&&confirmation==="PUBBLICA"&&selected.length>0;
  return <div className={styles.panel}>
    {error&&<div role="alert" className={styles.error}>{error}</div>}
    <section className={styles.card}><h2>1. Destinazione e vista</h2><fieldset disabled={!editable}><div className={styles.grid}>
      <label>Account marketplace<select value={rules.account_id} onChange={e=>choose(e.target.value)}><option value="">Scegli un account collegato</option>{index?.accounts.map(a=><option key={a.id} value={a.id} disabled={!["kaufland","worten"].includes(a.marketplace)}>{a.name} · {a.marketplace}{!["kaufland","worten"].includes(a.marketplace)?" · pubblicazione non disponibile":""}</option>)}</select></label>
      <label>Vista salvata<select value={rules.view_id} onChange={e=>{const view=views.find(v=>v.id===e.target.value);change({view_id:view?.id??"",revision:view?.revision??1});}}><option value="">Scegli una vista</option>{views.map(v=><option key={v.id} value={v.id}>{v.name} · {v.row_count} prodotti</option>)}</select></label>
      <label>Paese<select value={rules.storefront} onChange={e=>{change({storefront:e.target.value,shipping_group:"",warehouse:"",multiplier:1,fx_date:""});setOptions({});}}>{Object.entries(countries).filter(([k])=>isWorten?k==="pt":k!=="pt").map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>
      {!isWorten&&<label>Ambiente<select value={String(rules.playground)} onChange={e=>{change({playground:e.target.value==="true",shipping_group:"",warehouse:""});setOptions({});}}><option value="true">Playground · prova Kaufland</option><option value="false">Reale · account di vendita</option></select></label>}
    </div></fieldset><p className={styles.note}>Sono disponibili solo le viste associate all’account scelto. <a href="/seller/catalog/work">Prepara o modifica una vista</a>. <a href="/seller/marketplaces">Collega un marketplace</a>.</p>
      {!index?.can_manage&&index&&<p>Hai accesso in lettura. La pubblicazione richiede il permesso di modifica del catalogo.</p>}
      <button disabled={pending} onClick={()=>void run(async()=>{await refresh();})}>Aggiorna viste e storico</button>
    </section>
    {account&&<section className={styles.card}><h2>2. Regole di pubblicazione</h2><fieldset disabled={!editable}><div className={styles.grid}>
      {numeric("margin","Ricarico sul costo (%)",0,500)}{numeric("minimum_margin","Ricarico prezzo minimo (%)",0,500)}{numeric("commission","Commissione stimata (%)",0,100)}
      {numeric("min_qty","Quantità minima",0,99999,"1")}{numeric("min_cost","Costo minimo, spedizione inclusa (€)")}{isWorten&&numeric("min_profit","Guadagno minimo (€)")}
      <label>Escludi per peso<select value={rules.weight_mode} onChange={e=>change({weight_mode:e.target.value})}><option value="none">Nessuna esclusione</option><option value="above">Peso superiore alla soglia</option><option value="below">Peso inferiore alla soglia</option><option value="between">Peso compreso nell’intervallo</option></select></label>
      {rules.weight_mode!=="none"&&numeric("weight_from","Soglia / da (kg)")}{rules.weight_mode==="between"&&numeric("weight_to","Fino a (kg)")}
      {numeric("start","Prima riga filtrata",1,1000000,"1")}{numeric("limit","Prodotti in questo invio",1,100,"1")}
    </div><div className={styles.actions}><label className={styles.check}><input type="checkbox" checked={rules.composite_sku} onChange={e=>change({composite_sku:e.target.checked})}/>Genera SKU composto: fornitore, EAN, costo e prezzo minimo</label></div>
    <p className={styles.note}>I prezzi vengono ricalcolati dal costo della vista, con spedizione inclusa una sola volta. La commissione serve a stimare il guadagno. I prodotti senza peso restano inclusi. Ogni invio contiene fino a 100 prodotti.</p>
    <div className={styles.grid}>
      {isWorten?<>{metadata("logistic_class","Classe logistica","logistic_classes")}{metadata("state_code","Codice condizione prodotto","states")}<label>Paese di spedizione<input value={rules.ship_from} onChange={e=>change({ship_from:e.target.value})}/></label></>:<>{metadata("shipping_group","ID gruppo spedizione","shipping_groups")}{metadata("warehouse","ID magazzino","warehouses")}<label>Aliquota IVA marketplace<select value={rules.vat} onChange={e=>change({vat:e.target.value})}>{["standard_rate","reduced_rate_1","reduced_rate_2","super_reduced_rate","zero_rate"].map(v=><option key={v} value={v}>{v}</option>)}</select></label></>}
      {numeric("handling","Preparazione spedizione (giorni)",0,isWorten?44:30,"1")}
      {["cz","pl"].includes(rules.storefront)&&<>{numeric("multiplier",`Cambio: 1 EUR in ${rules.storefront==="cz"?"CZK":"PLN"}`,0.000001,1000)}<label>Data del cambio<input type="date" value={rules.fx_date} onChange={e=>change({fx_date:e.target.value})}/></label></>}
    </div><div className={styles.actions}><button onClick={()=>void run(async()=>{const v=readPublicationOptions(await request(`/options/${rules.account_id}?storefront=${rules.storefront}&playground=${rules.playground}`),sellerId,rules.account_id);if(!v)throw new Error("Configurazione non valida.");setOptions(v);})}>Leggi configurazione dal marketplace</button><button className={styles.primary} disabled={!rules.view_id} onClick={()=>void run(preview)}>Prepara anteprima</button></div></fieldset>{Object.keys(options).length>0&&<p className={styles.note} role="status">Configurazione letta: {isWorten?`${options.logistic_classes?.length??0} classi logistiche, ${options.states?.length??0} condizioni`:`${options.shipping_groups?.length??0} gruppi spedizione, ${options.warehouses?.length??0} magazzini`}. Se un elenco è vuoto, verifica la configurazione nel portale del marketplace.</p>}</section>}
    {job&&<section className={styles.card}><h2>3. Anteprima ed esiti · {labels[job.status]}</h2><div className={styles.summary}><span>Account<strong>{job.account_name}</strong></span><span>Destinazione<strong>{countries[job.rules.storefront]} · {job.rules.playground?"Playground":"REALE"}</strong></span><span>Vista<strong>{job.view_name}</strong></span><span>Intervallo<strong>{job.rules.start}–{job.rules.start+job.total-1} su {job.filtered_total} filtrati</strong></span></div>
      {job.status!=="draft"&&<><progress className={styles.progress} max={100} value={percent}/><p className={styles.note}>{percent}% elaborato · {processed}/{job.total} righe · accettate API: {job.counts.accepted??0} · ricevute per importazione: {job.counts.submitted??0} · rifiutate: {job.counts.rejected??0} · da verificare: {job.counts.unknown??0}</p></>}
      <p className={styles.note}>Importi base in EUR. {["cz","pl"].includes(job.rules.storefront)&&`Prezzi inviati in ${job.rules.storefront==="cz"?"CZK":"PLN"}, moltiplicatore ${job.rules.multiplier} del ${job.rules.fx_date}. `}L’accettazione dell’API non certifica ancora la visibilità dell’offerta. Per gli esiti incerti verifica il portale marketplace prima di preparare un altro invio.</p>
      <div className={styles.table}><table><thead><tr><th>Invia</th><th>Prodotto</th><th>EAN / SKU</th><th>Quantità</th><th>Costo totale</th><th>Vendita</th><th>Minimo</th><th>Commissione stimata</th><th>Guadagno stimato</th><th>Esito</th></tr></thead><tbody>{job.rows.map(r=><tr key={r.id}><td><input aria-label={`Seleziona ${r.ean}`} type="checkbox" disabled={!editable||r.status!=="pending"||!["draft","interrupted"].includes(job.status)||uncertain} checked={selected.includes(r.id)} onChange={e=>setSelected(e.target.checked?[...selected,r.id]:selected.filter(id=>id!==r.id))}/></td><td>{r.name||"Nome non disponibile"}</td><td>{r.ean}<br/>{r.sku}</td><td>{r.quantity}</td>{[r.cost,r.price,r.minimum_price,r.commission,r.profit].map((v,i)=><td key={i} className={styles.money}>{euro.format(Number(v))}</td>)}<td>{r.problem||labels[r.status]}{r.result_code&&<div>{r.result_code}</div>}</td></tr>)}</tbody></table></div>
      {["draft","interrupted"].includes(job.status)&&<><p className={styles.note}>{job.rules.playground?"Confermi l’invio al Playground Kaufland.":"Confermando invii prezzi e disponibilità all’account reale. Le offerte con lo stesso SKU possono essere aggiornate."} {job.status==="interrupted"&&"Puoi riprendere solo le righe ancora da inviare; gli esiti incerti restano esclusi."}</p><div className={styles.actions}><label>Scrivi PUBBLICA <input className={styles.confirm} value={confirmation} disabled={!editable||uncertain} onChange={e=>setConfirmation(e.target.value)}/></label><button className={styles.primary} disabled={!canSubmit} onClick={()=>void run(submit)}>{job.rules.playground?"Invia al Playground":"Pubblica selezionati"} ({selected.length})</button></div></>}
      {missingShipping&&job.status==="draft"&&<p className={styles.note}>Per inviare, scegli gruppo spedizione e magazzino nelle regole e prepara una nuova anteprima.</p>}
      <div className={styles.actions}><button disabled={pending} onClick={()=>void run(async()=>{await open(job.id);await refresh();})}>Aggiorna esiti</button>{uncertain&&<span className={styles.note}>Richiesta non confermata: aggiorna gli esiti prima di riprovare.</span>}</div>
    </section>}
    <section className={styles.card}><h2>Storico invii e anteprime</h2>{!index?.jobs.length?<p className={styles.note}>Nessun invio preparato.</p>:index.jobs.map(j=><div className={styles.history} key={j.id}><div><strong>{j.view_name} · {j.account_name}</strong><div className={styles.note}>{new Date(j.created_at).toLocaleString("it-IT")} · {j.rules.storefront.toUpperCase()} · {j.rules.playground?"Playground":"Reale"} · {labels[j.status]}</div></div><button disabled={pending} onClick={()=>void run(()=>open(j.id))}>Apri</button></div>)}</section>
  </div>;
}
