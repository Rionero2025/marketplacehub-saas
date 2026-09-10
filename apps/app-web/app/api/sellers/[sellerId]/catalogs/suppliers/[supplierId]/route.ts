import { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../../lib/catalog-proxy";

type Context = { params: Promise<{ sellerId: string; supplierId: string }> };
export async function DELETE(request: NextRequest, { params }: Context) {
  const { sellerId, supplierId } = await params;
  return catalogProxy(request, sellerId, "delete-supplier", supplierId);
}
