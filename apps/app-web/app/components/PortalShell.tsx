"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { ReactNode } from "react";

export function PortalShell({ portal, displayName, children }: { portal: string; displayName: string; children: ReactNode }) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  async function logout() {
    setPending(true);
    await fetch("/api/auth/logout", { method: "POST" });
    router.replace("/");
    router.refresh();
  }
  return <main className="workspace-shell"><aside><div className="workspace-brand"><span className="brand-mark">MH</span> Marketplace Hub</div><p className="workspace-realm">{portal}</p></aside><section className="workspace-content"><header><span>{displayName}</span><button className="secondary-button" onClick={logout} disabled={pending}>Esci</button></header>{children}</section></main>;
}
