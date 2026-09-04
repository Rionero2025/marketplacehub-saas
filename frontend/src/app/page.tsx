"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Logo } from "@/components/Logo";
import { api } from "@/lib/api";
import "./marketing.css";

type Offer = { code: string; name: string; monthly_price_cents: number; currency: string };
export default function Home() {
  const [plans,setPlans]=useState<Offer[]>([]), [days,setDays]=useState<number|null>(null), [error,setError]=useState("");
  const load=()=>{setError("");api<{plans:Offer[];trial_days:number}>("/onboarding/plans").then(x=>{setPlans(x.plans);setDays(x.trial_days)}).catch(e=>setError(e.message));};
  useEffect(load,[]);
  return <div className="marketing">
    <div className="marketingHero"><header><Link href="/" aria-label="Marketplace Hub, home"><Logo/></Link><nav><a href="#piattaforma">La piattaforma</a><a href="#piani">Piani</a><Link href="/login">Accedi</Link></nav></header>
      <section><span className="eyebrow">Marketplace Hub</span><h1>Più marketplace.<br/>Un solo spazio di lavoro.</h1><p>Organizza ordini, cataloghi e attività dei tuoi negozi. Lavora come Seller o coordina più clienti con il workspace Agency.</p><div className="marketingActions"><Link className="primaryButton" href="/signup?plan=enterprise">Prova Enterprise</Link><a className="marketingOutline" href="#piani">Confronta i piani</a></div>{days&&<small>{days} giorni di prova · Nessun addebito automatico</small>}</section>
    </div>
    <main><section id="piattaforma" className="marketingSection"><span className="eyebrow">Un’organizzazione chiara</span><h2>Uno spazio per ogni ruolo.</h2><div className="roleCards">
      <article><span>01 / SELLER</span><h3>Il tuo lavoro quotidiano</h3><p>Accedi ai tuoi negozi, ai marketplace collegati e alle funzioni del tuo abbonamento.</p></article>
      <article><span>02 / AGENCY</span><h3>Più seller, una regia</h3><p>Passa fra i clienti assegnati alla tua agenzia, mantenendo separati negozi, dati e accessi.</p></article>
      
    </div></section>
    <section id="piani" className="marketingSection"><span className="eyebrow">Abbonamenti mensili</span><h2>Il piano giusto per la tua attività.</h2><p>Inizia con una prova Enterprise. Scegli il piano di tuo interesse e definisci in seguito i moduli aggiuntivi per ordini, listini e funzioni.</p>
      {error?<div className="errorBox" role="alert">Non riusciamo a caricare i piani. <button onClick={load}>Riprova</button></div>:!plans.length?<p role="status">Caricamento piani…</p>:<div className="pricingGrid">{plans.map(plan=><article className={`priceCard ${plan.code==='enterprise'?'enterprise':''}`} key={plan.code}><span className="eyebrow">{plan.code==='enterprise'?'La prova completa':'Marketplace Hub'}</span><h3>{plan.name}</h3><div className="planPrice">{new Intl.NumberFormat('it-IT',{style:'currency',currency:plan.currency,maximumFractionDigits:2}).format(plan.monthly_price_cents/100)}<small>/mese</small></div><p>{plan.code==='enterprise'?'Il piano più completo per esplorare il tuo workspace.':'Seleziona questo piano per il tuo percorso su Marketplace Hub.'}</p><Link className={plan.code==='enterprise'?'primaryButton':'secondaryButton'} href={`/signup?plan=${plan.code}`}>Inizia la prova</Link></article>)}</div>}
    </section></main><footer><Logo/><span>Seller · Agency</span><Link href="/login">Accedi al workspace →</Link></footer>
  </div>;
}
