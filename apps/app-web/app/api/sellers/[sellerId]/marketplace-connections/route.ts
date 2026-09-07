import { NextRequest } from "next/server";
import { marketplaceConnectionsProxy } from "../../../../lib/marketplace-connections-proxy";
type Context = { params: Promise<{ sellerId: string }> };
export async function GET(request: NextRequest, { params }: Context) { return marketplaceConnectionsProxy(request, (await params).sellerId, "read"); }
export async function POST(request: NextRequest, { params }: Context) { return marketplaceConnectionsProxy(request, (await params).sellerId, "connect"); }
