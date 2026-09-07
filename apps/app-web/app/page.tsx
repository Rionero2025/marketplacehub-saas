import Link from "next/link";

export default function AppHome() {
  return (
    <main className="access-shell">
      <section className="access-intro">
        <span className="brand-mark">MH</span>
        <p className="eyebrow">MARKETPLACE HUB</p>
        <h1>Il tuo centro operativo per i marketplace.</h1>
        <p className="lead">Accedi allo spazio dedicato alla tua attività. Ogni portale applica autorizzazioni separate già dal login.</p>
      </section>
      <section className="portal-panel" aria-labelledby="portal-title">
        <p className="eyebrow">ACCESSO AL SOFTWARE</p>
        <h2 id="portal-title">Scegli il tuo portale</h2>
        <div className="portal-grid">
          <Link className="portal-card" href="/login/seller"><span className="portal-icon">S</span><strong>Accedi come Seller</strong><span>Gestisci il tuo negozio e le attività operative.</span></Link>
          <Link className="portal-card" href="/login/agency"><span className="portal-icon">A</span><strong>Accedi come Agenzia</strong><span>Coordina più Seller da un unico ambiente.</span></Link>
        </div>
      </section>
    </main>
  );
}
