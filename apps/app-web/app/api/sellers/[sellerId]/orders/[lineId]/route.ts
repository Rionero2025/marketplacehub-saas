import { NextRequest } from "next/server";
import { ordersProxy } from "../../../../../lib/orders-proxy";
export async function GET(request: NextRequest, { params }: { params: Promise<{ sellerId: string; lineId: string }> }) { const { sellerId, lineId } = await params; return ordersProxy(request, sellerId, "detail", lineId); }
