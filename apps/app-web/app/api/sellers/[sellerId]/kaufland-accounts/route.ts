import { NextRequest } from "next/server";
import { sellerSettingsProxy } from "../../../../lib/seller-settings-proxy";

export async function POST(request: NextRequest, { params }: { params: Promise<{ sellerId: string }> }) {
  return sellerSettingsProxy(request, (await params).sellerId, "add-account");
}
