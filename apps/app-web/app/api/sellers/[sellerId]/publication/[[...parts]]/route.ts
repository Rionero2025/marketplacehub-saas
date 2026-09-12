import {NextRequest} from "next/server";
import {publicationProxy} from "../../../../../lib/publication-proxy";
async function route(request:NextRequest,context:{params:Promise<{sellerId:string;parts?:string[]}>}) {
  const {sellerId,parts=[]}=await context.params;return publicationProxy(request,sellerId,parts);
}
export const GET=route;
export const POST=route;
