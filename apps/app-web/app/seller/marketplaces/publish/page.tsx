import { PortalShell } from "../../../components/PortalShell";
import { SellerPublicationPage } from "../../../components/SellerPublicationPanel";
import { SessionUnavailable } from "../../../components/SessionUnavailable";
import { requireRealm } from "../../../lib/session";
import { fetchWorkspace } from "../../../lib/workspace";

export default async function PublicationPage() {
  const session = await requireRealm("seller", "/login/seller");
  if (!session) return <SessionUnavailable />;
  const result = await fetchWorkspace("seller", "/login/seller");
  return <PortalShell portal="SELLER" displayName={session.display_name} currentPage="publication">
    <SellerPublicationPage result={result} />
  </PortalShell>;
}
