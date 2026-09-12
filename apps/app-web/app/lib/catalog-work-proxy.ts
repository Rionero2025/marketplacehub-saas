import { NextRequest, NextResponse } from "next/server";
import { apiUrl } from "./api-url";
import { boundedBody, requestOrigin } from "./catalog-proxy";
import { isUuid } from "./seller-settings-types";
import { readWorkData, readWorkIndex, readWorkView } from "./catalog-work-types";

const reply = (data: unknown, status = 200) => NextResponse.json(data, {status, headers: {"cache-control": "no-store"}});
const failure = (status: number) => reply({detail: ({401: "Sessione scaduta. Accedi di nuovo.", 403: "Non hai il permesso di modificare il catalogo.", 404: "Listino o vista non disponibile.", 409: "I dati sono cambiati. Riapri il listino o la vista prima di salvare.", 413: "Troppe modifiche insieme. Salva in più passaggi.", 422: "Controlla filtri, selezione, nome e marketplace di destinazione."} as Record<number,string>)[status] ?? "Servizio listini momentaneamente non disponibile. Verifica le viste salvate prima di ripetere un salvataggio."}, status);
export async function catalogWorkProxy(request: NextRequest, seller: string, parts: string[]) {
  if (!isUuid(seller)) return failure(404);
  const method = request.method;
  const index = parts.length === 0 && method === "GET";
  const preview = parts.length === 1 && parts[0] === "preview" && method === "POST";
  const save = parts.length === 1 && parts[0] === "views" && method === "POST";
  const resource = parts.length === 2 && parts[0] === "views" && isUuid(parts[1]) && ["GET","PUT","DELETE"].includes(method);
  const enrich = parts.length===3&&parts[0]==="views"&&isUuid(parts[1])&&parts[2]==="enrich"&&method==="POST";
  if (!index && !preview && !save && !resource && !enrich) return failure(404);
  const cookie = request.headers.get("cookie") ?? "";
  if (!cookie.includes("mh_session=")) return failure(401);
  if (method !== "GET" && request.headers.get("origin") !== requestOrigin(request)) return failure(403);
  const root = `${apiUrl}/v1/sellers/${seller}/catalogs/work`;
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(60_000)]);
  try {
    let body: string | undefined;
    if (method !== "GET") {
      const check = await fetch(root, {headers:{cookie}, cache:"no-store", redirect:"error", signal});
      if (!check.ok) return failure(check.status);
      const workspace = readWorkIndex(await check.json(), seller);
      if (!workspace) return failure(502);
      if (!preview && !workspace.can_manage) return failure(403);
      if (!request.headers.get("content-type")?.startsWith("application/json")) return failure(422);
      try { body = new TextDecoder().decode(await boundedBody(request, preview ? 16384 : 1048576, 30_000)); JSON.parse(body); }
      catch { return failure(413); }
    }
    let suffix = parts.length ? `/${parts.join("/")}` : "";
    if (resource && method === "GET") {
      const params = new URL(request.url).searchParams;
      const page = params.get("page") ?? "1";
      if (params.getAll("page").length > 1 || !/^\d{1,6}$/.test(page) || Number(page) < 1 || Number(page) > 100000) return failure(422);
      suffix += `?page=${page}`;
    }
    const response = await fetch(root + suffix, {method, body, headers:{cookie, "content-type":"application/json"}, cache:"no-store", redirect:"error", signal});
    if (!response.ok) {
      const rejected = await response.json().catch(()=>null);
      const safeMessages = ["Usa il LIGHT di origine della vista.", "Per costi e disponibilità InnPro seleziona il LIGHT.", "Seleziona il FULL dello stesso fornitore InnPro.", "Scegli account marketplace attivi del tuo negozio.", "La selezione contiene prodotti fuori dalla vista.", "Seleziona almeno un prodotto.", "La vista deve contenere almeno un prodotto.", "Mantieni almeno EAN o SKU nella riga.", "Questo fornitore richiede le regole dedicate di costo e destinazione, ancora da trasferire. La lavorazione generica non è applicabile."];
      if (response.status === 422 && safeMessages.includes(rejected?.detail)) return reply({detail:rejected.detail},422);
      return failure(response.status);
    }
    const raw: unknown = await response.json();
    if (method === "DELETE") {
      if (!raw || typeof raw !== "object" || !("id" in raw) || raw.id !== parts[1] || !("deleted" in raw) || raw.deleted !== true) return failure(502);
      return reply({id:parts[1], deleted:true});
    }
    const parsed = index ? readWorkIndex(raw, seller) : preview || enrich || (resource && method === "GET") ? readWorkData(raw, seller, resource || enrich ? parts[1] : undefined) : readWorkView(raw);
    if (!parsed || (resource && method === "PUT" && "id" in parsed && parsed.id !== parts[1])) return failure(502);
    return reply(parsed, response.status);
  } catch { return failure(503); }
}
