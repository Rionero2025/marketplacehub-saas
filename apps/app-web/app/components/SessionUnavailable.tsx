"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";

export function SessionUnavailable() {
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  return <main className="session-unavailable"><section className="workspace-section workspace-empty" aria-labelledby="session-unavailable-title">
    <span className="brand-mark" aria-hidden="true">MH</span>
    <h1 id="session-unavailable-title">Servizio momentaneamente non disponibile</h1>
    <p role="status">Non riusciamo a verificare il tuo accesso. Riprova tra poco.</p>
    <button type="button" disabled={pending} onClick={() => startTransition(() => router.refresh())}>{pending ? "Verifica in corso…" : "Riprova"}</button>
  </section></main>;
}
