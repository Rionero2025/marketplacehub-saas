import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";

export default async function SellerPortal() {
  const session = await requireRealm("seller", "/login/seller");
  return <PortalShell portal="SELLER" displayName={session.display_name}><div className="workspace-card"><p className="eyebrow">AREA SELLER</p><h1>Pannello Seller</h1><p>Accesso verificato. Le funzioni operative verranno ricostruite nei blocchi dedicati seguendo il programma Streamlit.</p></div></PortalShell>;
}
