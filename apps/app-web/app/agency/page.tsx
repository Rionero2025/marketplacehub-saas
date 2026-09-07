import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";
import { fetchWorkspace } from "../lib/workspace";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { SessionUnavailable } from "../components/SessionUnavailable";

export default async function AgencyPortal() {
  const session = await requireRealm("agency", "/login/agency");
  if (!session) return <SessionUnavailable />;
  const result = await fetchWorkspace("agency", "/login/agency");
  return <PortalShell portal="AGENZIA" displayName={session.display_name}><WorkspacePanel result={result} realm="agency" loginPath="/login/agency" /></PortalShell>;
}
