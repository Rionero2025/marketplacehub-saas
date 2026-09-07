import { NextRequest } from "next/server";
import { marketplaceConnectionsProxy } from "../../../../../lib/marketplace-connections-proxy";
type Context = { params: Promise<{ sellerId: string; accountId: string }> };
export async function DELETE(request: NextRequest, { params }: Context) {
  const { sellerId, accountId } = await params;
  return marketplaceConnectionsProxy(request, sellerId, "delete", accountId);
}
