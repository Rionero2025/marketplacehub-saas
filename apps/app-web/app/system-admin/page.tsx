import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";

export default async function PlatformPortal() {
  const session = await requireRealm("platform", "/system-admin/login");
  return <PortalShell portal="PLATFORM ADMIN" displayName={session.display_name}><div className="workspace-card"><p className="eyebrow">AREA INTERNA</p><h1>Amministrazione piattaforma</h1><p>Accesso verificato. Questo percorso non è pubblicizzato nelle pagine di accesso Seller o Agenzia.</p></div></PortalShell>;
}
