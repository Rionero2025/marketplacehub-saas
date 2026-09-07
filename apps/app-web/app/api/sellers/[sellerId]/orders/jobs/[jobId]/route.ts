import { NextRequest } from "next/server";
import { ordersProxy } from "../../../../../../lib/orders-proxy";
export async function GET(request: NextRequest, { params }: { params: Promise<{ sellerId: string; jobId: string }> }) { const { sellerId, jobId } = await params; return ordersProxy(request, sellerId, "job", jobId); }
