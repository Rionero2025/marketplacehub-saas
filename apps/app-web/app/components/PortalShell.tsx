"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { ReactNode } from "react";

export function PortalShell({ portal, displayName, children }: { portal: string; displayName: string; children: ReactNode }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [logoutError, setLogoutError] = useState("");
  async function logout() {
    setPending(true);
    setLogoutError("");
    try {
      const response = await fetch("/api/auth/logout", { method: "POST", signal: AbortSignal.timeout(20000) });
      if (!response.ok) {
        setLogoutError("Non è stato possibile completare l’uscita. Riprova.");
        return;
      }
      router.replace("/");
      router.refresh();
    } catch {
      setLogoutError("Non è stato possibile completare l’uscita. Controlla la connessione e riprova.");
    } finally {
      setPending(false);
    }
  }
  return <main className="workspace-shell"><aside><div className="workspace-brand"><span className="brand-mark">MH</span> Marketplace Hub</div><p className="workspace-realm">{portal}</p></aside><section className="workspace-content"><header><span>{displayName}</span><button className="secondary-button" onClick={logout} disabled={pending}>{pending ? "Uscita…" : "Esci"}</button></header>{logoutError && <p className="form-error logout-error" role="alert">{logoutError}</p>}{children}</section></main>;
}
