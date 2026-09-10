import type { NextRequest } from "next/server";
import { catalogProxy } from "../../../../../../../../lib/catalog-proxy";

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ sellerId: string; priceListId: string; jobId: string }> },
) {
  const { sellerId, priceListId, jobId } = await context.params;
  return catalogProxy(request, sellerId, "job", priceListId, jobId);
}
