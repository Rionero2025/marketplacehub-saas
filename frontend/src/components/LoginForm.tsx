"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { Logo } from "./Logo";

export function LoginForm({ area }: { area: "seller" | "agency" | "admin" }) {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const label = area === "admin" ? "Admin del sistema" : area === "agency" ? "Agenzia" : "Seller";
  const destination = area === "admin" ? "/internal/admin" : area === "agency" ? "/agency" : "/dashboard";
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api("/auth/login", { method: "POST", body: JSON.stringify({ username, password, remember, area }) });
      router.replace(destination);
    } catch (e) { setError(e instanceof Error ? e.message : "Accesso non riuscito."); }
    finally { setBusy(false); }
  }
  return <div className="authPage">
    <div className="authVisual"><Logo/><div><span className="eyebrow">{label}</span>
      <h1>{area === "admin" ? "Controllo della piattaforma." : area === "agency" ? "I tuoi clienti, una sola dashboard." : "Il controllo dei tuoi negozi."}</h1>
      <p>{area === "admin" ? "Accesso riservato al team di amministrazione Marketplace Hub." : area === "agency" ? "Accedi ai seller e ai workspace assegnati alla tua agenzia." : "Gestisci marketplace, ordini e cataloghi del tuo workspace."}</p>
    </div></div>
    <div className="authPanel"><form className="authCard" onSubmit={submit}>
      <h1>Accedi {label}</h1>
      <label>Username<input value={username} onChange={e=>setUsername(e.target.value)} autoComplete="username" required/></label>
      <label>Password<input type="password" value={password} onChange={e=>setPassword(e.target.value)} autoComplete="current-password" required/></label>
      <label className="check"><input type="checkbox" checked={remember} onChange={e=>setRemember(e.target.checked)}/>Mantieni la sessione su questo browser</label>
      {error && <div className="errorBox" role="alert">{error}</div>}
      <button type="submit" className="primaryButton" disabled={busy}>{busy ? "Accesso…" : `Accedi ${label}`}</button>
      {area !== "admin" && <><Link href="/login">Cambia tipo di accesso</Link><small>Non hai un account? <Link href={`/signup?area=${area}`}>Crea il workspace</Link></small></>}
    </form></div>
  </div>;
}
