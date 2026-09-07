import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";
import { fetchWorkspace } from "../lib/workspace";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { SessionUnavailable } from "../components/SessionUnavailable";

export default async function SellerPortal() {
  const session = await requireRealm("seller", "/login/seller");
  if (!session) return <SessionUnavailable />;
  const result = await fetchWorkspace("seller", "/login/seller");
  return <PortalShell portal="SELLER" displayName={session.display_name}><WorkspacePanel result={result} realm="seller" loginPath="/login/seller" /></PortalShell>;
}
