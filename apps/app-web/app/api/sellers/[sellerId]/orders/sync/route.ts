import { NextRequest } from "next/server";
import { ordersProxy } from "../../../../../lib/orders-proxy";
export async function POST(request: NextRequest, { params }: { params: Promise<{ sellerId: string }> }) { return ordersProxy(request, (await params).sellerId, "sync"); }
