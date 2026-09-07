import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";

export default async function AgencyPortal() {
  const session = await requireRealm("agency", "/login/agency");
  return <PortalShell portal="AGENZIA" displayName={session.display_name}><div className="workspace-card"><p className="eyebrow">AREA AGENZIA</p><h1>Pannello Agenzia</h1><p>Accesso verificato. La gestione multi Seller verrà aggiunta con il modello organizzativo.</p></div></PortalShell>;
}
