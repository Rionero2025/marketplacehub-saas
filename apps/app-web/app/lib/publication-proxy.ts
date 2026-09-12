import {NextRequest,NextResponse} from "next/server";
import {apiUrl} from "./api-url";
import {boundedBody,requestOrigin} from "./catalog-proxy";
import {isUuid} from "./seller-settings-types";
import {readPublicationIndex,readPublicationJob,readPublicationOptions} from "./publication-types";
const reply=(v:unknown,status=200)=>NextResponse.json(v,{status,headers:{"cache-control":"no-store"}});
const fail=(status:number)=>reply({detail:status===401?"Sessione scaduta. Accedi di nuovo.":status===403?"Non hai il permesso di pubblicare.":"Richiesta non riuscita. Controlla lo storico prima di ripetere un invio."},status);
export async function publicationProxy(request:NextRequest,seller:string,parts:string[]) {
  if(!isUuid(seller))return fail(404);
  const method=request.method,index=method==="GET"&&parts.length===0;
  const preview=method==="POST"&&parts.length===1&&parts[0]==="preview";
  const detail=method==="GET"&&parts.length===2&&parts[0]==="jobs"&&isUuid(parts[1]);
  const submit=method==="POST"&&parts.length===3&&parts[0]==="jobs"&&isUuid(parts[1])&&["submit","edit"].includes(parts[2]);
  const options=method==="GET"&&parts.length===2&&parts[0]==="options"&&isUuid(parts[1]);
  if(!index&&!preview&&!detail&&!submit&&!options)return fail(404);
  const cookie=request.headers.get("cookie")??"";if(!cookie.includes("mh_session="))return fail(401);
  if(method!=="GET"&&request.headers.get("origin")!==requestOrigin(request))return fail(403);
  const root=`${apiUrl}/v1/sellers/${seller}/publication`,signal=AbortSignal.any([request.signal,AbortSignal.timeout(65000)]);
  try{
    let body:string|undefined;
    if(method!=="GET"){
      const check=await fetch(root,{headers:{cookie},cache:"no-store",redirect:"error",signal});if(!check.ok)return fail(check.status);
      const ws=readPublicationIndex(await check.json(),seller);if(!ws)return fail(502);if(!ws.can_manage)return fail(403);
      if(!request.headers.get("content-type")?.startsWith("application/json"))return fail(422);
      try{body=new TextDecoder().decode(await boundedBody(request,parts[2]==="edit"?8388608:parts[2]==="submit"?1048576:16384,10000));JSON.parse(body);}catch{return fail(413);}
    }
    let suffix=parts.length?"/"+parts.join("/"):"";
    if(options){const q=new URL(request.url).searchParams,sf=q.get("storefront")??"de",pg=q.get("playground")??"true";
      if(q.getAll("storefront").length>1||q.getAll("playground").length>1||!["de","cz","sk","at","pl","fr","it","pt"].includes(sf)||!["true","false"].includes(pg))return fail(422);
      suffix+=`?storefront=${sf}&playground=${pg}`;
    }
    const response=await fetch(root+suffix,{method,headers:{cookie,"content-type":"application/json"},body,cache:"no-store",redirect:"error",signal});
    const raw:unknown=await response.json();
    if(!response.ok){if(raw&&typeof raw==="object"&&"detail" in raw&&typeof raw.detail==="string"&&raw.detail.length<350&&[403,404,409,422,502,503].includes(response.status))return reply({detail:raw.detail},response.status);return fail(response.status);}
    const value=index?readPublicationIndex(raw,seller):options?readPublicationOptions(raw,seller,parts[1]):readPublicationJob(raw,seller);
    if(!value||((detail||submit)&&"id" in value&&value.id!==parts[1]))return fail(502);
    return reply(options?{...value,seller_id:seller,account_id:parts[1]}:value,response.status);
  }catch{return fail(503);}
}
