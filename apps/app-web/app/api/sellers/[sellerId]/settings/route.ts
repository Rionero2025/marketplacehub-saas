import { NextRequest } from "next/server";
import { sellerSettingsProxy } from "../../../../lib/seller-settings-proxy";

type Context = { params: Promise<{ sellerId: string }> };
export async function GET(request: NextRequest, { params }: Context) {
  return sellerSettingsProxy(request, (await params).sellerId, "read");
}
export async function PUT(request: NextRequest, { params }: Context) {
  return sellerSettingsProxy(request, (await params).sellerId, "save");
}
