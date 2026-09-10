import { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../lib/catalog-proxy";

type Context = { params: Promise<{ sellerId: string }> };
export async function POST(request: NextRequest, { params }: Context) {
  return catalogProxy(request, (await params).sellerId, "create-price-list");
}
