import { NextRequest } from "next/server";
import { ordersActionsProxy } from "../../../../../lib/orders-actions-proxy";

export async function POST(request: NextRequest, { params }: { params: Promise<{ sellerId: string }> }) {
  return ordersActionsProxy(request, (await params).sellerId, "export");
}
