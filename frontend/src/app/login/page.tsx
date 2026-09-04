import Link from "next/link";
import { Logo } from "@/components/Logo";
export default function LoginChoice() {
  return <div className="authPage"><div className="authVisual"><Link href="/"><Logo/></Link><div><span className="eyebrow">Marketplace Hub</span><h1>Accedi al tuo spazio di lavoro.</h1><p>Scegli l’accesso in base al workspace che gestisci.</p></div></div>
    <div className="authPanel"><section className="authCard"><h1>Come vuoi accedere?</h1>
      <div className="panel"><h2>Seller</h2><p>Gestisci i tuoi negozi, ordini e marketplace.</p><Link className="primaryButton linkButton" href="/login/seller">Accedi Seller</Link></div>
      <div className="panel"><h2>Agenzia</h2><p>Coordina i seller e i clienti assegnati alla tua agenzia.</p><Link className="primaryButton linkButton" href="/login/agency">Accedi Agenzia</Link></div>
      <small>Non hai un account? <Link href="/signup">Crea il workspace</Link></small>
    </section></div></div>;
}
