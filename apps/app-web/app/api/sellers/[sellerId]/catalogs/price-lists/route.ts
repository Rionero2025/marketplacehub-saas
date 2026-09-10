import { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../lib/catalog-proxy";

type Context = { params: Promise<{ sellerId: string }> };
export async function POST(request: NextRequest, { params }: Context) {
  const mediaType = request.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  const operation = mediaType === "application/json" ? "create-price-list-url" : "create-price-list";
  return catalogProxy(request, (await params).sellerId, operation);
}
