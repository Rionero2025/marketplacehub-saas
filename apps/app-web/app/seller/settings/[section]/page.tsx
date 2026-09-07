import { notFound } from "next/navigation";
import { PortalShell } from "../../../components/PortalShell";
import { WorkspacePanel } from "../../../components/WorkspacePanel";
import { SessionUnavailable } from "../../../components/SessionUnavailable";
import { requireRealm } from "../../../lib/session";
import { fetchWorkspace } from "../../../lib/workspace";

export default async function SellerSettings({ params }: { params: Promise<{ section: string }> }) {
  const session = await requireRealm("seller", "/login/seller");
  if (!session) return <SessionUnavailable />;
  const { section } = await params;
  if (section !== "store" && section !== "organizations" && section !== "permissions") notFound();
  const result = await fetchWorkspace("seller", "/login/seller");
  return <PortalShell portal="SELLER" displayName={session.display_name} currentPage={section}>
    <WorkspacePanel key={section} result={result} realm="seller" loginPath="/login/seller" view={section} />
  </PortalShell>;
}
