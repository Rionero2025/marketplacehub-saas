"use client";
import { useEffect,useState } from "react";
import { WorkspacePage } from "@/components/WorkspacePage";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { PageHeader } from "@/components/PageHeader";
import { api } from "@/lib/api";
type Plan={code:string;name:string;monthly_price_cents:number;features:string[];limits:Record<string,number|null>};
function Body(){
  const{user,tenants,refresh}=useWorkspace();const[plans,setPlans]=useState<Plan[]>([]),[error,setError]=useState(''),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  useEffect(()=>{if(user?.is_admin)api<Plan[]>('/plans').then(setPlans).catch(e=>setError(e.message));},[user?.is_admin]);
  if(!user)return null;
  if(!user.is_admin)return <div className="errorBox" role="alert">Accesso riservato all’Admin della piattaforma. Il ruolo Owner di un cliente non concede questo accesso.</div>;
  const save=async(p:Plan)=>{setBusy(true);setError('');setMessage('');try{await api(`/plans/${p.code}`,{method:'PUT',body:JSON.stringify(p)});setMessage(`Piano ${p.name} aggiornato.`);}catch(e){setError(e instanceof Error?e.message:'Salvataggio non riuscito');}finally{setBusy(false);}};
  const trial=async(id:number)=>{setBusy(true);setError('');setMessage('');try{await api(`/tenants/${id}/billing/trial`,{method:'POST',body:JSON.stringify({plan_code:'enterprise',days:14})});await refresh();setMessage('Prova Enterprise di 14 giorni attivata.');}catch(e){setError(e instanceof Error?e.message:'Attivazione non riuscita');}finally{setBusy(false);}};
  return <><PageHeader title="Admin piattaforma" description="Gestisci i clienti, i piani commerciali e le prove Enterprise."/>{error&&<div className="errorBox" role="alert">{error}</div>}{message&&<p role="status">{message}</p>}
    <div className="panel"><h2>Clienti e agenzie · {tenants.length}</h2><div className="tableWrap"><table className="dataTable"><thead><tr><th>Workspace</th><th>Area</th><th>Piano</th><th>Stato</th><th>Prova</th></tr></thead><tbody>{tenants.map(t=><tr key={t.id}><td>{t.name}</td><td>{t.tenant_type==='agency'?'Agency':'Seller'}</td><td>{t.plan_code}</td><td>{t.status}</td><td><button className="secondaryButton" disabled={busy} onClick={()=>void trial(t.id)}>Attiva Enterprise · 14 giorni</button></td></tr>)}</tbody></table></div></div>
    <div className="panel" style={{marginTop:24}}><h2>Prezzi mensili</h2><p>Configura il prezzo mensile in euro di ogni piano.</p><div className="formGrid">{plans.map(p=><label key={p.code}>{p.name}<input type="number" min="0" step="0.01" value={p.monthly_price_cents/100} onChange={e=>setPlans(all=>all.map(x=>x.code===p.code?{...x,monthly_price_cents:Math.round(Number(e.target.value)*100)}:x))}/><button className="secondaryButton" disabled={busy} onClick={()=>void save(p)}>Salva {p.name}</button></label>)}</div></div></>;
}
export default function Admin(){return <WorkspacePage><Body/></WorkspacePage>;}
