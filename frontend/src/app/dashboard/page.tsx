"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { WorkspacePage } from "@/components/WorkspacePage";
import { PageHeader } from "@/components/PageHeader";
import { StatCard } from "@/components/StatCard";
import { StatusBadge } from "@/components/StatusBadge";
import { Icon } from "@/components/Icon";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { api } from "@/lib/api";
import type { Entitlements, Job, MarketplaceAccount } from "@/lib/types";

type AccountPulse = MarketplaceAccount & { orderTotal: number; cachedOrders: number; lastOrder: string; syncState: string };

function Body(){
  const { user, seller } = useWorkspace();
  const [accounts,setAccounts]=useState<MarketplaceAccount[]>([]); const [ent,setEnt]=useState<Entitlements|null>(null);
  const [catalogs,setCatalogs]=useState<Record<string,unknown>[]>([]); const [jobs,setJobs]=useState<Job[]>([]); const [pulses,setPulses]=useState<AccountPulse[]>([]);
  const [loading,setLoading]=useState(true);
  useEffect(()=>{
    if(!seller||!user)return; let live=true; setLoading(true);
    const load=async()=>{
      const [a,e,c,j]=await Promise.allSettled([
        api<MarketplaceAccount[]>(`/sellers/${seller.id}/accounts`),
        api<Entitlements>(`/tenants/${user.active_tenant_id}/entitlements`),
        api<Record<string,unknown>[]>(`/sellers/${seller.id}/catalogs`),
        api<Job[]>(`/jobs?seller_id=${seller.id}&limit=8`),
      ]);
      if(!live)return;
      const aa=a.status==="fulfilled"?a.value:[]; setAccounts(aa); if(e.status==="fulfilled")setEnt(e.value); setCatalogs(c.status==="fulfilled"?c.value:[]); setJobs(j.status==="fulfilled"?j.value:[]);
      const pulse=await Promise.all(aa.map(async ac=>{
        const [orders,status]=await Promise.allSettled([
          api<{total:number}>(`/sellers/${seller.id}/orders?account_id=${ac.id}&marketplace=${ac.marketplace}&limit=1&offset=0`),
          user.is_admin||user.permissions.includes("accounting") ? api<{cache_summary?:Record<string,unknown>;sync_state?:Record<string,unknown>}>(`/sellers/${seller.id}/accounting/status?account_id=${ac.id}&marketplace=${ac.marketplace}`) : Promise.resolve({}),
        ]);
        const s: {cache_summary?:Record<string,unknown>;sync_state?:Record<string,unknown>} = status.status==="fulfilled" ? status.value : {}; const cache=s.cache_summary||{}; const sync=s.sync_state||{};
        return {...ac,orderTotal:orders.status==="fulfilled"?Number(orders.value.total||0):0,cachedOrders:Number(cache.total_orders||0),lastOrder:String(cache.last_order_created||""),syncState:String(sync.status||sync.last_status||"")};
      })); if(live){setPulses(pulse);setLoading(false)};
    }; void load(); const timer=setInterval(load,20000); return()=>{live=false;clearInterval(timer)};
  },[seller?.id,user?.active_tenant_id]);
  const totalOrders=useMemo(()=>pulses.reduce((n,x)=>n+x.orderTotal,0),[pulses]); const activeJobs=jobs.filter(j=>["queued","running"].includes(j.status.toLowerCase()));
  const onboarding=[
    {done:Boolean(seller),label:"Seller configurato",text:seller?.name||"Crea il primo Seller"},
    {done:accounts.length>0,label:"Marketplace collegato",text:accounts.length?`${accounts.length} account attivi`:"Collega Kaufland o Worten"},
    {done:catalogs.length>0,label:"Catalogo disponibile",text:catalogs.length?`${catalogs.length} listini pronti`:"Aggiungi un listino fornitore"},
    {done:Boolean(ent?.active),label:"Piano attivo",text:ent?.plan_name||ent?.plan_code||"Verifica abbonamento"},
  ];
  const completed=onboarding.filter(x=>x.done).length; const firstName=(user?.display_name||user?.username||"").split(" ")[0];
  return <>
    <PageHeader title={firstName?`Buongiorno, ${firstName}`:"Dashboard"} description="Una vista unica su Seller, marketplace e attività operative." action={<div className="headerActions"><Link className="secondaryButton linkButton" href="/jobs"><Icon name="activity" size={16}/>Attività</Link><Link className="primaryButton linkButton" href="/orders">Apri ordini<Icon name="arrow" size={16}/></Link></div>}/>
    <div className="stats"><StatCard label="Ordini indicizzati" value={loading?"…":totalOrders.toLocaleString("it-IT")} meta={`${accounts.length} marketplace/account`}/><StatCard label="Cataloghi pronti" value={loading?"…":catalogs.length} meta="query server-side"/><StatCard label="Attività in corso" value={activeJobs.length} meta={activeJobs[0]?.message||"Nessun job bloccante"}/><StatCard label="Piano" value={ent?.plan_name||ent?.plan_code||"—"} meta={ent?.active?"Abbonamento operativo":"Da verificare"}/></div>
    <div className="dashboardGrid">
      <section className="panel span2"><div className="panelTitle"><div><span className="sectionEyebrow">Operations pulse</span><h2>Marketplace collegati</h2><p>Ordini e cache vengono letti dal backend senza caricare lo storico nel browser.</p></div><span className="pill">{pulses.length} account</span></div>
        {pulses.length?<div className="accountPulseList">{pulses.map(p=><div className="accountPulse" key={p.id}><div className={`marketLogo ${p.marketplace.toLowerCase()}`}>{p.marketplace.slice(0,1).toUpperCase()}</div><div className="accountPulseMain"><div><b>{p.account_name}</b><span className="caps">{p.marketplace}</span></div><small>{p.lastOrder?`Ultimo ordine: ${formatDate(p.lastOrder)}`:"Cache pronta al primo sync"}</small></div><div className="accountMetric"><strong>{p.orderTotal.toLocaleString("it-IT")}</strong><span>ordini/righe</span></div><StatusBadge status={p.syncState||"active"} label={p.syncState||"Connesso"}/></div>)}</div>:<div className="emptyPro"><Icon name="store" size={24}/><div><b>Nessun marketplace collegato</b><p>Collega il primo account per iniziare a sincronizzare ordini e offerte.</p></div><Link href="/settings" className="secondaryButton linkButton">Configura</Link></div>}
      </section>
      <section className="panel onboardingCard"><div className="panelTitle"><div><span className="sectionEyebrow">Setup</span><h2>Workspace pronto al {Math.round(completed/onboarding.length*100)}%</h2></div><span className="progressRing">{completed}/{onboarding.length}</span></div><div className="setupProgress"><i style={{width:`${completed/onboarding.length*100}%`}}/></div><div className="checkList">{onboarding.map((item,i)=><div className={`checkItem ${item.done?"done":""}`} key={item.label}><span className="checkIcon">{item.done?<Icon name="check" size={15}/>:i+1}</span><div><b>{item.label}</b><small>{item.text}</small></div></div>)}</div><Link href="/settings" className="textLink">Completa configurazione <Icon name="arrow" size={14}/></Link></section>
    </div>
    <div className="dashboardGrid secondRow">
      <section className="panel span2"><div className="panelTitle"><div><span className="sectionEyebrow">Background</span><h2>Attività recenti</h2></div><Link href="/jobs" className="textLink">Vedi tutte <Icon name="arrow" size={14}/></Link></div>{jobs.length?<div className="recentJobs">{jobs.slice(0,5).map(j=><div className="recentJob" key={j.job_id}><span className="jobKindIcon"><Icon name="clock" size={16}/></span><div className="recentJobText"><b>{friendlyKind(j.kind)}</b><small>{j.message||j.error||j.status}</small></div><div className="recentJobProgress"><i style={{width:`${Math.max(j.status==="done"?100:2,j.progress_pct)}%`}}/></div><StatusBadge status={j.status}/></div>)}</div>:<div className="empty">Nessuna attività recente.</div>}</section>
      <section className="panel quickPanel"><div className="panelTitle"><div><span className="sectionEyebrow">Scorciatoie</span><h2>Azioni frequenti</h2></div></div><div className="quickLinks"><Link href="/orders"><Icon name="orders"/><span><b>Ordini</b><small>Consulta lo storico</small></span><Icon name="chevron" size={15}/></Link><Link href="/catalogs"><Icon name="catalogs"/><span><b>Cataloghi</b><small>Listini e prodotti</small></span><Icon name="chevron" size={15}/></Link><Link href="/buybox"><Icon name="buybox"/><span><b>Buy Box</b><small>Controlli salvati</small></span><Icon name="chevron" size={15}/></Link><Link href="/accounting"><Icon name="accounting"/><span><b>Contabilità</b><small>Costi e payout</small></span><Icon name="chevron" size={15}/></Link></div></section>
    </div>
  </>;
}
function formatDate(value:string){const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString("it-IT",{day:"2-digit",month:"2-digit",year:"2-digit",hour:"2-digit",minute:"2-digit"})}
function friendlyKind(kind:string){return kind.replaceAll("_"," ").replace(/\b\w/g,x=>x.toUpperCase())}
export default function Page(){return <WorkspacePage><Body/></WorkspacePage>}
