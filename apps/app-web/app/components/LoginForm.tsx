"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { isLoginSuccess, loginErrorMessage, type LoginRealm } from "../lib/auth-login-contract";
import { waitForLoginReadiness } from "../lib/auth-login-readiness";

type Realm = LoginRealm;
const destinations: Record<Realm, string> = { seller: "/seller", agency: "/agency", platform: "/system-admin" };

export function LoginForm({ realm, title, description }: { realm: Realm; title: string; description: string }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState("");
  const active = useRef(true);
  const busy = useRef(false);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    active.current = true;
    return () => { active.current = false; controller.current?.abort(); busy.current = false; };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current) return;
    busy.current = true;
    const abort = new AbortController();
    controller.current = abort;
    const current = () => active.current && controller.current === abort;
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setPending(true);
    setError("");
    setProgress("Preparazione dell’accesso…");
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const ready = await waitForLoginReadiness({ signal: abort.signal, onWaiting: () => {
        if (current()) setProgress("Avvio del servizio in corso. Il primo accesso può richiedere circa un minuto.");
      } });
      if (!current()) return;
      if (!ready) { setError("Il servizio non è ancora pronto. Attendi qualche istante e riprova: le credenziali non sono state inviate."); return; }
      setProgress("Accesso in corso…");
      timeout = setTimeout(() => abort.abort(), 35000);
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ login: form.get("login"), password: form.get("password"), realm }),
        cache: "no-store", redirect: "error", signal: abort.signal,
      });
      if (!current()) return;
      if (!response.ok) {
        setError(loginErrorMessage(response.status));
        return;
      }
      const payload: unknown = await response.json().catch(() => null);
      if (!current()) return;
      if (!isLoginSuccess(payload, realm)) { setError(loginErrorMessage(502)); return; }
      const passwordInput = formElement.elements.namedItem("password");
      if (passwordInput instanceof HTMLInputElement) passwordInput.value = "";
      router.replace(destinations[realm]);
      router.refresh();
    } catch {
      if (current()) setError(loginErrorMessage(abort.signal.aborted ? 504 : 503));
    } finally {
      clearTimeout(timeout);
      if (current()) { busy.current = false; setPending(false); setProgress(""); }
    }
  }

  return (
    <main className="login-shell">
      <section className="login-brand"><span className="brand-mark">MH</span><p className="eyebrow">MARKETPLACE HUB</p><h1>Operazioni marketplace, in un unico spazio.</h1></section>
      <section className="login-panel"><div className="login-box"><p className="eyebrow">ACCESSO RISERVATO</p><h2>{title}</h2><p>{description}</p>
        <form onSubmit={submit} aria-busy={pending}>
          <label htmlFor="login">Email o username</label><input id="login" name="login" autoComplete="username" required maxLength={254} disabled={pending} />
          <label htmlFor="password">Password</label><input id="password" name="password" type="password" autoComplete="current-password" required maxLength={1024} disabled={pending} />
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          {progress ? <p className="workspace-muted" role="status">{progress}</p> : null}
          <button type="submit" disabled={pending}>{pending ? "Attendi…" : "Accedi"}</button>
        </form>
      </div></section>
    </main>
  );
}
