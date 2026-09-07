"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

type Realm = "seller" | "agency" | "platform";
const destinations: Record<Realm, string> = { seller: "/seller", agency: "/agency", platform: "/system-admin" };

export function LoginForm({ realm, title, description }: { realm: Realm; title: string; description: string }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ login: form.get("login"), password: form.get("password"), realm }),
      });
      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
        setError(payload?.detail ?? "Impossibile accedere. Riprova.");
        return;
      }
      router.replace(destinations[realm]);
      router.refresh();
    } catch {
      setError("Servizio momentaneamente non disponibile.");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-brand"><span className="brand-mark">MH</span><p className="eyebrow">MARKETPLACE HUB</p><h1>Operazioni marketplace, in un unico spazio.</h1></section>
      <section className="login-panel"><div className="login-box"><p className="eyebrow">ACCESSO RISERVATO</p><h2>{title}</h2><p>{description}</p>
        <form onSubmit={submit}>
          <label htmlFor="login">Email o username</label><input id="login" name="login" autoComplete="username" required maxLength={254} />
          <label htmlFor="password">Password</label><input id="password" name="password" type="password" autoComplete="current-password" required maxLength={1024} />
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          <button type="submit" disabled={pending}>{pending ? "Accesso..." : "Accedi"}</button>
        </form>
      </div></section>
    </main>
  );
}
