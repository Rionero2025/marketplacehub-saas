"use client";
import { FormEvent, useEffect, useState } from "react";
import { WorkspacePage } from "@/components/WorkspacePage";
import { PageHeader } from "@/components/PageHeader";
import { useWorkspace } from "@/components/WorkspaceProvider";
import { api } from "@/lib/api";
import type { MarketplaceAccount } from "@/lib/types";

function Body(){
  const { user, seller, sellers } = useWorkspace();
  const [accounts,setAccounts]=useState<MarketplaceAccount[]>([]);
  const [marketplace,setMarketplace]=useState("kaufland");
  const [accountName,setAccountName]=useState("");
  const [clientKey,setClientKey]=useState(""); const [secretKey,setSecretKey]=useState("");
  const [apiKey,setApiKey]=useState(""); const [shopId,setShopId]=useState("");
  const [message,setMessage]=useState(""); const [busy,setBusy]=useState(false);
  const load=()=>seller?api<MarketplaceAccount[]>(`/sellers/${seller.id}/accounts`).then(setAccounts).catch(()=>setAccounts([])):Promise.resolve();
  useEffect(()=>{void load()},[seller?.id]);
  const submit=async(e:FormEvent)=>{e.preventDefault();if(!seller)return;setBusy(true);setMessage("");
    const credentials=marketplace==="kaufland"?{client_key:clientKey,secret_key:secretKey}:{api_key:apiKey,shop_id:shopId};
    try{await api("/onboarding/marketplaces",{method:"POST",body:JSON.stringify({seller_id:seller.id,marketplace,account_name:accountName,credentials,verify_credentials:true})});setMessage("Marketplace collegato e verificato.");setClientKey("");setSecretKey("");setApiKey("");setShopId("");await load()}catch(x){setMessage(x instanceof Error?x.message:"Collegamento non riuscito")}finally{setBusy(false)}};
  return <><PageHeader title="Impostazioni" description="Tenant, Seller e collegamenti marketplace del workspace."/>
    <div className="stats"><div className="statCard"><span>Tenant</span><strong>{user?.active_tenant_name}</strong><small>{user?.active_tenant_type}</small></div><div className="statCard"><span>Seller autorizzati</span><strong>{sellers.length}</strong><small>scope utente + tenant</small></div><div className="statCard"><span>Seller attivo</span><strong>{seller?.name||"—"}</strong><small>{seller?.legal_name}</small></div><div className="statCard"><span>Account marketplace</span><strong>{accounts.length}</strong><small>{accounts.map(x=>x.marketplace).join(" · ")||"nessuno"}</small></div></div>
    <section className="panel"><div className="panelTitle"><div><h2>Collega marketplace</h2><p>Le credenziali vengono inviate al backend e cifrate prima del salvataggio.</p></div></div>
      <form className="settingsForm" onSubmit={submit}><label>Marketplace<select value={marketplace} onChange={e=>setMarketplace(e.target.value)}><option value="kaufland">Kaufland</option><option value="worten">Worten</option></select></label><label>Nome account<input value={accountName} onChange={e=>setAccountName(e.target.value)} placeholder="es. Kaufland principale"/></label>
      {marketplace==="kaufland"?<><label>Client Key<input type="password" value={clientKey} onChange={e=>setClientKey(e.target.value)} required/></label><label>Secret Key<input type="password" value={secretKey} onChange={e=>setSecretKey(e.target.value)} required/></label></>:<><label>API Key<input type="password" value={apiKey} onChange={e=>setApiKey(e.target.value)} required/></label><label>Shop ID<input value={shopId} onChange={e=>setShopId(e.target.value)} required/></label></>}
      <div className="settingsActions"><button className="primaryButton" disabled={busy||!seller}>{busy?"Verifica…":"Verifica e collega"}</button>{message&&<span className="formMessage">{message}</span>}</div></form>
    </section>
    <section className="panel"><div className="panelTitle"><h2>Account collegati</h2></div><div className="table"><div className="tr th"><span>Marketplace</span><span>Account</span><span>Stato</span></div>{accounts.map(a=><div className="tr" key={a.id}><span className="caps">{a.marketplace}</span><span>{a.account_name}</span><span><i className="dot ok"/>Attivo</span></div>)}</div></section></>;
}
export default function Page(){return <WorkspacePage><Body/></WorkspacePage>}
