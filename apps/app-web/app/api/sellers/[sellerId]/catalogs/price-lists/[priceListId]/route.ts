import { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../../lib/catalog-proxy";

type Context = { params: Promise<{ sellerId: string; priceListId: string }> };
export async function GET(request: NextRequest, { params }: Context) {
  const { sellerId, priceListId } = await params;
  return catalogProxy(request, sellerId, "detail", priceListId);
}
export async function DELETE(request: NextRequest, { params }: Context) {
  const { sellerId, priceListId } = await params;
  return catalogProxy(request, sellerId, "delete-price-list", priceListId);
}
