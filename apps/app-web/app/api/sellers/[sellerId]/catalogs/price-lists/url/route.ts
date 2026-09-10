import type { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../../lib/catalog-proxy";

export async function POST(request: NextRequest, context: { params: Promise<{ sellerId: string }> }) {
  const { sellerId } = await context.params;
  return catalogProxy(request, sellerId, "create-price-list-url");
}
