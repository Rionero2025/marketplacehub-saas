import { PortalShell } from "../components/PortalShell";
import { requireRealm } from "../lib/session";
import { fetchWorkspace } from "../lib/workspace";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { SessionUnavailable } from "../components/SessionUnavailable";

export default async function PlatformPortal() {
  const session = await requireRealm("platform", "/system-admin/login");
  if (!session) return <SessionUnavailable />;
  const result = await fetchWorkspace("platform", "/system-admin/login");
  return <PortalShell portal="PLATFORM ADMIN" displayName={session.display_name}><WorkspacePanel result={result} realm="platform" loginPath="/system-admin/login" /></PortalShell>;
}
