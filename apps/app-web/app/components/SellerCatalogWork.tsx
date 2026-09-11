"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { readCatalogDashboard, type CatalogDashboard, type CatalogPriceList } from "../lib/catalog-types";
import { economicFields, readWorkData, readWorkIndex, readWorkView, type Edits, type EditableField, type Recipe, type WorkData, type WorkIndex, type WorkRow, type WorkView } from "../lib/catalog-work-types";

const labels = {cost:"Costo (€)", shipping_cost:"Spedizione (€)", total_cost:"Costo totale (€)", quantity:"Quantità", price:"Prezzo (€)", minimum_price:"Prezzo minimo (€)"};
const number = new Intl.NumberFormat("it-IT", {maximumFractionDigits:3});
const blank = (list: CatalogPriceList, full?: CatalogPriceList): Recipe => ({price_list_id:list.id, version:list.active_version_number ?? 1, content_list_id:full?.id ?? null, content_version:full?.active_version_number ?? null, search:"", min_qty:"0", min_cost:"0", max_cost:"0", measure:"weight_kg", exclude:"none", lower:"0", upper:"0", shipping:"0", margin:"35", minimum_margin:"10"});

export function SellerCatalogWork({sellerId}: {sellerId:string}) {
  const router = useRouter();
  const [catalog, setCatalog] = useState<CatalogDashboard | null>(null);
  const [workspace, setWorkspace] = useState<WorkIndex | null>(null);
  const [recipe, setRecipe] = useState<Recipe | null>(null);
  const [data, setData] = useState<WorkData | null>(null);
  const [name, setName] = useState("");
  const [targets, setTargets] = useState<string[]>([]);
  const [edits, setEdits] = useState<Edits>({});
  const [added, setAdded] = useState<WorkRow[]>([]);
  const [all, setAll] = useState(true);
  const [selection, setSelection] = useState<string[]>([]);
  const [overwrite, setOverwrite] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(true);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const alive = useRef(true), busy = useRef(false);
  const controllers = useRef(new Set<AbortController>());
  const root = `/api/sellers/${sellerId}/catalogs`;
  const validLists = catalog?.price_lists.filter(l => (l.active_version_number ?? 0) > 0 && l.feed_role !== "full") ?? [];
  const selectedList = catalog?.price_lists.find(l => l.id === recipe?.price_list_id);
  const fullLists = catalog?.price_lists.filter(l => l.feed_role === "full" && l.supplier_id === selectedList?.supplier_id && (l.active_version_number ?? 0) > 0) ?? [];
  const saved = data?.view;
  const writable = !!workspace?.can_manage && !pending && !uncertain;
  const selectedCount = data ? all ? data.total - selection.length + added.length : selection.length : 0;

  async function request(path:string, method="GET", body?:unknown) {
    const controller = new AbortController(); controllers.current.add(controller);
    const timeout = setTimeout(() => controller.abort(), 65_000);
    try {
      const response = await fetch(root + path, {method, headers:body ? {"content-type":"application/json"} : undefined, body:body ? JSON.stringify(body) : undefined, cache:"no-store", signal:controller.signal});
      if (!alive.current) throw new Error("Richiesta annullata.");
      if (response.status === 401) { router.replace("/login/seller"); throw new Error("Sessione scaduta."); }
      const result:unknown = await response.json();
      if (!response.ok) {
        if (method !== "GET" && path !== "/work/preview" && response.status >= 500) setUncertain(true);
        throw new Error(result && typeof result === "object" && "detail" in result && typeof result.detail === "string" ? result.detail : "Operazione non riuscita.");
      }
      return result;
    } finally { clearTimeout(timeout); controllers.current.delete(controller); }
  }
  async function loadIndex() {
    const [a,b] = await Promise.all([request(""), request("/work")]);
    const nextCatalog = readCatalogDashboard(a, sellerId), nextWorkspace = readWorkIndex(b, sellerId);
    if (!nextCatalog || !nextWorkspace) throw new Error("Risposta catalogo non valida.");
    setCatalog(nextCatalog); setWorkspace(nextWorkspace);
    return nextCatalog;
  }
  function choose(list:CatalogPriceList, lists=catalog?.price_lists ?? []) {
    const contents = list.provider === "innpro" ? lists.filter(l => l.feed_role === "full" && l.supplier_id === list.supplier_id && (l.active_version_number ?? 0) > 0) : [];
    setRecipe(blank(list, contents.length === 1 ? contents[0] : undefined));
    setData(null); setEdits({}); setAdded([]); setSelection([]); setAll(true); setName(""); setTargets([]); setOverwrite(""); setSuccess("");
  }
  useEffect(() => {
    alive.current = true;
    busy.current = true;
    void loadIndex().then(c => {
      if (!alive.current) return;
      const first = c.price_lists.find(l => l.feed_role !== "full" && (l.active_version_number ?? 0) > 0);
      if (first) choose(first, c.price_lists);
    }).catch(e => { if (alive.current) setError(e.message); }).finally(() => { if (alive.current) { setPending(false); busy.current=false; } });
    return () => { alive.current=false; controllers.current.forEach(c => c.abort()); };
  // SellerCatalogPage keys this component by Seller; no state survives a switch.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sellerId]);

  async function run(action:()=>Promise<void>, mutation=false) {
    if (busy.current) return;
    busy.current=true; setPending(true); setError(""); setSuccess("");
    try { await action(); }
    catch(e) { if (alive.current) { setError(e instanceof Error ? e.message : "Operazione non riuscita."); if (mutation && e instanceof Error && /fetch|network|abort|annullata/i.test(e.message)) setUncertain(true); } }
    finally { busy.current=false; if(alive.current) setPending(false); }
  }
  function change(patch:Partial<Recipe>) {
    if (!recipe) return;
    setRecipe({...recipe,...patch}); setData(null); setEdits({}); setAdded([]); setSelection([]); setAll(true); setOverwrite("");
  }
  async function preview(page=1) {
    if (!recipe) return;
    const result=readWorkData(await request("/work/preview","POST",{recipe,page}),sellerId);
    if (!result) throw new Error("Anteprima non valida.");
    setData(result);
  }
  async function open(view:WorkView, page=1, reset=true) {
    const result=readWorkData(await request(`/work/views/${view.id}?page=${page}`),sellerId,view.id);
    if (!result) throw new Error("Vista non valida.");
    setData(result);
    if(reset) {setName(view.name);setTargets(view.account_ids.filter(id=>workspace?.accounts.some(a=>a.id===id)));setEdits({}); setAdded([]);setSelection([]);setAll(true);setConfirmation("");setOverwrite("");}
  }
  async function save() {
    if (!data || (!saved && !recipe)) return;
    const replacing=workspace?.views.find(v=>v.id===overwrite);
    const result = await request(saved ? `/work/views/${saved.id}` : "/work/views", saved ? "PUT" : "POST", saved ? {
      name,account_ids:targets,expected_revision:saved.revision,edits,removed:selection,added:added.map(({id,length_cm,width_cm,height_cm,...row})=>row),
    } : {
      recipe,name,account_ids:targets,select_all:all,selection,edits,
      overwrite_id:replacing?.id ?? null,expected_revision:replacing?.revision ?? null,
    });
    const view=readWorkView(result);
    if(!view) {setUncertain(true);throw new Error("Verifica le viste salvate prima di ripetere il salvataggio.");}
    await loadIndex(); await open(view); setSuccess(`Vista «${view.name}» salvata: ${number.format(view.row_count)} prodotti.`);
  }
  function toggle(id:string) {if(added.some(r=>r.id===id)){setAdded(current=>current.filter(r=>r.id!==id));return;}setSelection(current=>current.includes(id)?current.filter(v=>v!==id):[...current,id]);}
  function edit(row:WorkRow, field:EditableField, value:string) {
    if(added.some(r=>r.id===row.id)){setAdded(current=>current.map(r=>r.id===row.id?{...r,[field]:value}:r));return;}
    setEdits(current=>({...current,[row.id]:{...current[row.id],[field]:value}}));
  }
  const numeric = (field:keyof Recipe,label:string,max=999999) => <label key={field}>{label}<input type="number" min="0" max={max} step="any" required value={String(recipe?.[field] ?? "0")} onChange={e=>change({[field]:e.target.value})} /></label>;

  return <div className="catalog-work">
    <div className="catalog-form-actions"><button className="workspace-refresh" disabled={pending} onClick={()=>void run(async()=>{const c=await loadIndex();setUncertain(false);if(!saved && recipe){const l=c.price_lists.find(x=>x.id===recipe.price_list_id);if(l)choose(l,c.price_lists);}})}>Aggiorna dati</button>{saved && selectedList && <button className="workspace-refresh" disabled={pending} onClick={()=>choose(selectedList)}>Nuova lavorazione</button>}</div>
    {error && <p role="alert" className="form-error">{error}</p>}{success && <p role="status" className="settings-notice">{success}</p>}
    {pending && <p role="status">Elaborazione in corso…</p>}
    {uncertain && <p className="form-error">Esito da verificare: aggiorna i dati e controlla le viste salvate prima di ripetere l’operazione.</p>}
    {workspace && !workspace.can_manage && <p className="settings-notice">Puoi consultare e preparare l’anteprima. Il salvataggio richiede il permesso Catalogo in modifica.</p>}
    {!saved && <section className="workspace-section"><div className="workspace-section-heading"><h2>1. Filtra e prepara la vista</h2></div>
      <form className="catalog-form workspace-section-body" onSubmit={e=>{e.preventDefault();void run(()=>preview());}}><fieldset disabled={pending}>
        <div className="catalog-work-fields"><label>Listino da lavorare<select value={recipe?.price_list_id ?? ""} required onChange={e=>{const l=validLists.find(l=>l.id===e.target.value);if(l)choose(l);}}><option value="" disabled>Seleziona un listino</option>{validLists.map(l=><option key={l.id} value={l.id}>{l.supplier_name} · {l.name}</option>)}</select></label>
          {selectedList?.provider === "innpro" && <label>Contenuti dal FULL<select value={recipe?.content_list_id ?? ""} onChange={e=>{const f=fullLists.find(f=>f.id===e.target.value);change({content_list_id:f?.id ?? null,content_version:f?.active_version_number ?? null});}}><option value="">Solo dati del LIGHT</option>{fullLists.map(l=><option key={l.id} value={l.id}>{l.name}</option>)}</select></label>}
        </div>
        {selectedList?.provider === "innpro" && <p className="workspace-field-note">Costo e disponibilità provengono dal LIGHT; il FULL completa nome e misure per EAN esatto. Le dimensioni prive di unità confermata restano indisponibili per il filtro in cm.</p>}
        {recipe && <><div className="catalog-work-fields"><label>Cerca EAN, SKU o nome<input value={recipe.search} onChange={e=>change({search:e.target.value})} maxLength={200} /></label>{numeric("min_qty","Quantità minima")}{numeric("min_cost","Costo minimo (€)")}{numeric("max_cost","Costo massimo (€) · 0 = nessun limite")}</div>
          <div className="catalog-work-fields"><label>Misura<select value={recipe.measure} onChange={e=>change({measure:e.target.value})}><option value="weight_kg">Peso (kg)</option><option value="length_cm">Lunghezza (cm)</option><option value="width_cm">Larghezza (cm)</option><option value="height_cm">Altezza (cm)</option></select></label><label>Esclusione<select value={recipe.exclude} onChange={e=>change({exclude:e.target.value})}><option value="none">Nessuna esclusione</option><option value="above">Superiori a</option><option value="below">Inferiori a</option><option value="between">Compresi tra Da e A</option></select></label>{recipe.exclude!=="none" && numeric("lower",recipe.exclude==="between"?"Da":"Soglia")}{recipe.exclude==="between" && numeric("upper","A")}</div>
          <p className="workspace-field-note">Le misure sconosciute restano incluse. Il filtro tra Da e A comprende gli estremi.</p>
          <div className="catalog-work-fields">{numeric("shipping","Spedizione aggiuntiva (€)",9999)}{numeric("margin","Ricarico iniziale (%)",500)}{numeric("minimum_margin","Ricarico prezzo minimo (%)",500)}</div>
          <p className="workspace-field-note">Costo totale = costo + spedizione del feed + spedizione aggiuntiva. Prezzo = costo totale × (1 + ricarico / 100), arrotondato a due decimali.</p>
          <button className="settings-save" type="submit">Prepara la vista</button></>}
        {!validLists.length && workspace && <p>Importa prima un listino utilizzabile in <a href="/seller/catalog/price-lists">Listini</a>.</p>}
      </fieldset></form>
    </section>}
    {data && <form onSubmit={e=>{e.preventDefault();void run(save,true);}}>
      <section className="workspace-section"><div className="workspace-section-heading"><h2>{saved ? `Modifica vista · ${saved.name}` : "Prodotti nella vista"}</h2><span>{number.format(data.total)} prodotti · {number.format(selectedCount)} {saved?"mantenuti":"selezionati"}</span></div>
        <div className="workspace-section-body"><p>Modifica i valori economici nelle celle. Le modifiche rimangono nella vista e non cambiano il listino originale.</p>
          {!saved && <div className="catalog-form-actions"><button type="button" className="workspace-refresh" disabled={pending} onClick={()=>{setAll(true);setSelection([]);}}>Seleziona tutti i filtrati</button><button type="button" className="workspace-refresh" disabled={pending} onClick={()=>{setAll(false);setSelection([]);}}>Deseleziona tutti</button><span>La selezione comprende tutte le pagine.</span></div>}
        </div>
        <div className="catalog-table-wrap" tabIndex={0} role="region" aria-label="Prodotti della vista, scorrimento orizzontale"><table className="catalog-table catalog-work-table"><thead><tr><th>{saved?"Mantieni":"Seleziona"}</th><th>Nome prodotto</th><th>EAN</th><th>SKU</th><th>Peso (kg)</th><th>Dimensioni (cm)</th>{economicFields.map(f=><th key={f}>{labels[f]}</th>)}</tr></thead><tbody>{[...data.rows,...added].map(row=><tr key={row.id}><td><input type="checkbox" aria-label={`${saved?"Mantieni":"Seleziona"} ${row.sku || row.ean}`} checked={all?!selection.includes(row.id):selection.includes(row.id)} disabled={pending} onChange={()=>toggle(row.id)} /></td>{(["name","ean","sku"] as const).map(f=><td key={f}>{saved?<input aria-label={`${f} ${row.sku || row.ean}`} value={edits[row.id]?.[f] ?? row[f]} disabled={!writable} maxLength={f==="ean"?128:f==="sku"?500:10000} onChange={e=>edit(row,f,e.target.value)} />:row[f] || "—"}</td>)}<td>{saved?<input aria-label={`Peso kg ${row.sku || row.ean}`} type="number" min="0" max="999999" step="any" value={edits[row.id]?.weight_kg ?? row.weight_kg ?? "0"} disabled={!writable} onChange={e=>edit(row,"weight_kg",e.target.value)} />:row.weight_kg===null?"—":number.format(Number(row.weight_kg))}</td><td>{[row.length_cm,row.width_cm,row.height_cm].map(v=>v===null?"—":number.format(Number(v))).join(" × ")}</td>{economicFields.map(f=><td key={f}><input aria-label={`${labels[f]} ${row.sku || row.ean}`} type="number" min="0" max="999999999999" step="any" required disabled={!writable} value={edits[row.id]?.[f] ?? row[f]} onChange={e=>edit(row,f,e.target.value)} /></td>)}</tr>)}</tbody></table></div>
        <div className="workspace-section-body catalog-form-actions">{saved && <button type="button" className="workspace-refresh" disabled={!writable||added.length>=100} onClick={()=>setAdded(current=>[...current,{id:crypto.randomUUID(),ean:"",sku:"",name:"",weight_kg:null,length_cm:null,width_cm:null,height_cm:null,cost:"0",shipping_cost:"0",total_cost:"0",quantity:"0",price:"0",minimum_price:"0"}])}>Aggiungi riga</button>}<button type="button" className="workspace-refresh" disabled={pending||data.page===1} onClick={()=>void run(()=>saved?open(saved,data.page-1,false):preview(data.page-1))}>Precedente</button><span>Pagina {data.page} di {Math.max(1,Math.ceil(data.total/50))}</span><button type="button" className="workspace-refresh" disabled={pending||data.page*50>=data.total} onClick={()=>void run(()=>saved?open(saved,data.page+1,false):preview(data.page+1))}>Successiva</button></div>
      </section>
      <section className="workspace-section"><div className="workspace-section-heading"><h2>2. Scegli i marketplace di destinazione</h2></div><div className="workspace-section-body catalog-work-targets">{workspace?.accounts.map(a=><label key={a.id}><input type="checkbox" checked={targets.includes(a.id)} disabled={!writable} onChange={e=>setTargets(current=>e.target.checked?[...current,a.id]:current.filter(id=>id!==a.id))} />{a.marketplace} · {a.name}</label>)}{!workspace?.accounts.length && <p>Collega prima un account in <a href="/seller/marketplaces">Marketplace</a>.</p>}<p className="workspace-field-note">Le destinazioni vengono associate alla vista. Il salvataggio non pubblica prodotti sui marketplace.</p></div></section>
      <section className="workspace-section"><div className="workspace-section-heading"><h2>3. Salva la vista</h2></div><div className="catalog-form workspace-section-body"><fieldset disabled={!writable}><div className="catalog-work-fields"><label>Nome della vista<input value={name} required maxLength={200} onChange={e=>setName(e.target.value)} /></label>{!saved && <label>Salvataggio<select value={overwrite} onChange={e=>setOverwrite(e.target.value)}><option value="">Crea una nuova vista</option>{workspace?.views.map(v=><option key={v.id} value={v.id}>Sovrascrivi: {v.name}</option>)}</select></label>}</div>{overwrite && <p>Il salvataggio sostituirà tutti i prodotti e le destinazioni della vista scelta.</p>}<button type="submit" className="settings-save" disabled={!targets.length||selectedCount<1||!name.trim()}>{saved?"Aggiorna vista salvata":"Salva vista e destinazioni"}</button></fieldset></div></section>
    </form>}
    <section className="workspace-section"><div className="workspace-section-heading"><h2>Viste salvate</h2></div><div className="catalog-table-wrap"><table className="catalog-table"><thead><tr><th>Vista</th><th>Listino</th><th>Prodotti</th><th>Destinazioni</th><th>Azioni</th></tr></thead><tbody>{workspace?.views.map(v=><tr key={v.id}><td>{v.name}</td><td>{v.source_name}</td><td>{number.format(v.row_count)}</td><td>{v.account_ids.map(id=>{const a=workspace.accounts.find(a=>a.id===id);return a?`${a.marketplace} · ${a.name}`:"Account non più attivo";}).join(", ")}</td><td><button className="workspace-refresh" disabled={pending} onClick={()=>void run(()=>open(v))}>Apri e modifica</button></td></tr>)}</tbody></table></div>{!workspace?.views.length && <p className="workspace-section-body">Nessuna vista salvata.</p>}</section>
    {saved && <details className="workspace-section workspace-section-body"><summary>Elimina la vista «{saved.name}»</summary><p>Il listino originale rimane disponibile. Digita ELIMINA per confermare.</p><input aria-label="Conferma eliminazione vista" value={confirmation} disabled={!writable} onChange={e=>setConfirmation(e.target.value)} /><button className="settings-delete" disabled={!writable||confirmation!=="ELIMINA"} onClick={()=>void run(async()=>{await request(`/work/views/${saved.id}`,"DELETE",{confirmation,expected_revision:saved.revision});setData(null);setConfirmation("");await loadIndex();setSuccess("Vista eliminata.");},true)}>Elimina definitivamente</button></details>}
  </div>;
}
