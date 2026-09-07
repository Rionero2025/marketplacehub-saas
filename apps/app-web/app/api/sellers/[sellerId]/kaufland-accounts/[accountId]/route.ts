import { NextRequest } from "next/server";
import { sellerSettingsProxy } from "../../../../../lib/seller-settings-proxy";

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ sellerId: string; accountId: string }> }) {
  const { sellerId, accountId } = await params;
  return sellerSettingsProxy(request, sellerId, "delete-account", accountId);
}
