"use client";
import { useEffect,useState } from "react";
import { useRouter } from "next/navigation";
import { WorkspacePage } from "@/components/WorkspacePage";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { PageHeader } from "@/components/PageHeader";
import { api } from "@/lib/api";
import type { Tenant } from "@/lib/types";

function Body(){
  const {user,sellers,switchTenant,setSellerId}=useWorkspace();const router=useRouter();const[error,setError]=useState("");const[clients,setClients]=useState<Tenant[]>([]);
  useEffect(()=>{setClients([]);setError('');if(user?.active_tenant_type!=='agency')return;let live=true;api<Tenant[]>(`/tenants/${user.active_tenant_id}/agency-clients`).then(x=>{if(live)setClients(x)}).catch(e=>{if(live)setError(e.message)});return()=>{live=false};},[user?.active_tenant_id,user?.active_tenant_type]);
  if(!user)return null;
  if(!user.is_admin&&user.active_tenant_type!=="agency")return <div className="errorBox" role="alert">Questa area è riservata ai workspace Agency. Se ne gestisci uno, selezionalo dal menu Azienda.</div>;
  const open=async(id:number)=>{setError("");if(await switchTenant(id))router.push('/dashboard');};
  return <><PageHeader title="Area Agency" description="I clienti assegnati alla tua agenzia e i negozi del workspace."/>{error&&<div className="errorBox">{error}</div>}
    <div className="panel"><h2>Clienti gestiti · {clients.length}</h2>{clients.length?<div className="tableWrap"><table className="dataTable"><thead><tr><th>Cliente</th><th>Piano</th><th>Stato</th><th>Accesso</th></tr></thead><tbody>{clients.map(t=><tr key={t.id}><td>{t.name}</td><td>{t.plan_code}</td><td>{t.status}</td><td><button className="secondaryButton" onClick={()=>void open(t.id)}>Apri workspace</button></td></tr>)}</tbody></table></div>:<p>Nessun cliente assegnato. I clienti collegati all’agenzia compariranno qui.</p>}</div>
    <div className="panel" style={{marginTop:24}}><h2>Negozi del workspace · {sellers.length}</h2>{sellers.map(s=><div className="headerActions" key={s.id} style={{justifyContent:'space-between',padding:'12px 0'}}><span>{s.name}</span><button className="secondaryButton" onClick={()=>{setSellerId(s.id);router.push('/dashboard');}}>Apri Seller</button></div>)}</div></>;
}
export default function Agency(){return <WorkspacePage><Body/></WorkspacePage>;}
