import { NextRequest } from "next/server";
import { catalogWorkProxy } from "../../../../../../lib/catalog-work-proxy";

type Context = { params: Promise<{ sellerId: string; parts?: string[] }> };
async function route(request: NextRequest, context: Context) {
  const { sellerId, parts = [] } = await context.params;
  return catalogWorkProxy(request, sellerId, parts);
}
export const GET = route;
export const POST = route;
export const PUT = route;
export const DELETE = route;
