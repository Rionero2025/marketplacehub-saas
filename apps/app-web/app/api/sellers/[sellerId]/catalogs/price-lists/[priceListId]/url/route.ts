import type { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../../../lib/catalog-proxy";

export async function POST(request: NextRequest, context: { params: Promise<{ sellerId: string; priceListId: string }> }) {
  const { sellerId, priceListId } = await context.params;
  return catalogProxy(request, sellerId, "update-price-list-url", priceListId);
}
