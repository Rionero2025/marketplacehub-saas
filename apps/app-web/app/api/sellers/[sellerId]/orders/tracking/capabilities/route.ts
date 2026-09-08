import { NextRequest } from "next/server";
import { ordersTrackingProxy } from "../../../../../../lib/orders-tracking-proxy";

export async function GET(request: NextRequest, { params }: { params: Promise<{ sellerId: string }> }) {
  return ordersTrackingProxy(request, (await params).sellerId, "capabilities");
}
