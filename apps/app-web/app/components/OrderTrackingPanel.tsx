"use client";

import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import type { OrderEnvironment, OrderItem } from "../lib/orders-types";
import { TRACKING_UI_FILE_OPERATION_TIMEOUT_MS, emptyTrackingMapping, readTrackingCapabilities, readTrackingImportResult, readTrackingPreview, trackingFields, type TrackingCapabilities, type TrackingImportResult, type TrackingMapping, type TrackingPreview } from "../lib/orders-tracking-types";
import { DashboardIcon } from "./DashboardIcon";
import styles from "./SellerOrdersPanel.module.css";

const labels: Record<keyof TrackingMapping, string> = {
  id_order_unit: "ID unità ordine (se presente)", id_order: "Numero ordine", carrier_code: "Corriere",
  tracking_numbers: "Tracking", combined_shipment: "Campo combinato (es. DPD | numero)",
};
const scopeQuery = (accountId: string, environment: OrderEnvironment) => new URLSearchParams({ account_id: accountId, environment }).toString();
const allowedExtension = (name: string) => /\.(?:csv|xlsx|xls)$/i.test(name);
const safeErrors = new Set([
  "La sessione è scaduta. Accedi di nuovo.", "Richiesta non consentita.", "Account o ambiente non valido.",
  "Il file deve essere CSV, XLSX o XLS e non può superare 5 MB.", "File non leggibile. Carica un CSV, XLSX o XLS valido.",
  "Controlla l’associazione delle colonne.", "Associa un identificativo ordine e almeno un dato di spedizione.",
  "Non hai accesso alla gestione tracking di questo account.", "L’unità ordine non è presente nell’archivio.",
  "L’account marketplace non è più disponibile.", "Il file supera il limite consentito.",
  "La richiesta supera il limite consentito.",
  "Il file non è leggibile oppure supera i limiti consentiti.", "Controlla il file e l’associazione delle colonne.",
  "Indica almeno il corriere oppure il tracking.", "Funzioni tracking non disponibili per questo account.",
  "Servizio tracking temporaneamente non disponibile. Riprova.",
  "Operazione tracking non confermata. Aggiorna gli ordini e riprova.",
  "Un’altra operazione tracking è già in corso per questo account. Attendi e riprova.",
  "Il servizio di importazione tracking è occupato. Attendi e riprova.",
  "Tempo massimo di caricamento superato. Riprova.",
]);
async function errorMessage(response: Response, fallback: string): Promise<string> {
  const raw: unknown = await response.json().catch(() => null);
  const detail = raw && typeof raw === "object" && "detail" in raw ? raw.detail : null;
  const reference = raw && typeof raw === "object" && "request_id" in raw && typeof raw.request_id === "string"
    && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(raw.request_id) ? raw.request_id : null;
  return `${typeof detail === "string" && safeErrors.has(detail) ? detail : fallback}${reference ? ` Riferimento: ${reference}.` : ""}`;
}

export function OrderTrackingPanel({ sellerId, accountId, environment, orders, disabled, onBusyChange, onMutated, onResponse }: {
  sellerId: string; accountId: string; environment: OrderEnvironment; orders: OrderItem[]; disabled: boolean;
  onBusyChange: (busy: boolean) => void; onMutated: () => Promise<void>; onResponse: (response: Response) => boolean;
}) {
  const baseUrl = `/api/sellers/${sellerId}/orders/tracking`, query = scopeQuery(accountId, environment);
  const [capabilities, setCapabilities] = useState<TrackingCapabilities | null>(null);
  const [file, setFile] = useState<File | null>(null), [preview, setPreview] = useState<TrackingPreview | null>(null);
  const [mapping, setMapping] = useState<TrackingMapping>(() => emptyTrackingMapping());
  const [result, setResult] = useState<TrackingImportResult | null>(null);
  const [manualLineId, setManualLineId] = useState(""), [carrier, setCarrier] = useState(""), [tracking, setTracking] = useState("");
  const [busy, setBusy] = useState<"preview" | "import" | "manual" | null>(null), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [writeUncertain, setWriteUncertain] = useState(false);
  const alive = useRef(true), requestVersion = useRef(0), requestAbort = useRef<AbortController | null>(null);
  const currentManual = orders.find((item) => item.id === manualLineId) ?? null;

  useEffect(() => { alive.current = true; return () => { alive.current = false; requestVersion.current += 1; requestAbort.current?.abort(); }; }, []);
  useEffect(() => { onBusyChange(busy !== null); return () => onBusyChange(false); }, [busy, onBusyChange]);
  useEffect(() => {
    const abort = new AbortController(); let active = true;
    const timeout = setTimeout(() => abort.abort(), 30_000);
    void (async () => {
      try {
        const response = await fetch(`${baseUrl}/capabilities?${query}`, { cache: "no-store", signal: abort.signal });
        if (!active || !onResponse(response)) return;
        if (!response.ok) { setError(await errorMessage(response, "Le funzioni di recupero tracking non sono disponibili. Aggiorna la pagina e riprova.")); return; }
        const raw: unknown = await response.json();
        const value = readTrackingCapabilities(raw && typeof raw === "object" && "capabilities" in raw ? raw.capabilities : null);
        if (!value) throw new Error();
        setCapabilities(value);
      } catch { if (active) setError("Le funzioni di recupero tracking non sono disponibili. Aggiorna la pagina e riprova."); }
      finally { clearTimeout(timeout); }
    })();
    return () => { active = false; abort.abort(); clearTimeout(timeout); };
  }, [baseUrl, query, onResponse]);
  useEffect(() => {
    const chosen = orders.find((item) => item.id === manualLineId) ?? orders[0];
    setManualLineId(chosen?.id ?? ""); setCarrier(chosen?.details.carrier ?? ""); setTracking(chosen?.details.tracking ?? "");
  }, [orders, manualLineId]);

  const runPreview = useCallback(async (chosen: File) => {
    requestAbort.current?.abort(); const abort = new AbortController(), version = ++requestVersion.current;
    requestAbort.current = abort; setBusy("preview"); setError(""); setNotice(""); setPreview(null); setResult(null);
    const timeout = setTimeout(() => abort.abort(), TRACKING_UI_FILE_OPERATION_TIMEOUT_MS);
    try {
      const form = new FormData(); form.set("file", chosen, chosen.name);
      const response = await fetch(`${baseUrl}/preview?${query}`, { method: "POST", body: form, cache: "no-store", signal: abort.signal });
      if (!alive.current || version !== requestVersion.current || !onResponse(response)) return;
      if (!response.ok) { setError(await errorMessage(response, "Impossibile leggere l’export. Verifica che sia un CSV, XLSX o XLS valido.")); return; }
      const raw: unknown = await response.json();
      const value = readTrackingPreview(raw && typeof raw === "object" && "preview" in raw ? raw.preview : null);
      if (!value || value.file_name !== chosen.name) throw new Error();
      setPreview(value); setMapping(emptyTrackingMapping(value.detected));
    } catch { if (alive.current && version === requestVersion.current) setError("Impossibile leggere l’export. Verifica che sia un CSV, XLSX o XLS valido."); }
    finally { clearTimeout(timeout); if (alive.current && version === requestVersion.current) setBusy(null); }
  }, [baseUrl, query, onResponse]);

  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const chosen = event.target.files?.[0] ?? null;
    requestAbort.current?.abort(); requestVersion.current += 1; setFile(chosen); setPreview(null); setResult(null); setMapping(emptyTrackingMapping()); setError(""); setNotice("");
    if (!chosen) return;
    if (!allowedExtension(chosen.name) || chosen.size < 1 || chosen.size > (capabilities?.max_bytes ?? 5_242_880)) {
      setError("Il file deve essere CSV, XLSX o XLS e non può superare 5 MB."); return;
    }
    void runPreview(chosen);
  }

  async function importTracking() {
    if (!file || !preview || busy || disabled || writeUncertain || !capabilities?.can_import) return;
    const hasIdentifier = mapping.id_order_unit || mapping.id_order;
    const hasShipment = mapping.carrier_code || mapping.tracking_numbers || mapping.combined_shipment;
    if (!hasIdentifier) { setError("Associa almeno la colonna Numero ordine oppure ID unità ordine."); return; }
    if (!hasShipment) { setError("Associa almeno Tracking, Corriere oppure il campo combinato."); return; }
    const version = ++requestVersion.current, abort = new AbortController(); requestAbort.current = abort;
    setBusy("import"); setError(""); setNotice(""); setResult(null);
    const timeout = setTimeout(() => abort.abort(), TRACKING_UI_FILE_OPERATION_TIMEOUT_MS);
    try {
      const form = new FormData(); form.set("file", file, file.name); form.set("mapping", JSON.stringify(mapping));
      const response = await fetch(`${baseUrl}/import?${query}`, { method: "POST", body: form, cache: "no-store", signal: abort.signal });
      if (!alive.current || version !== requestVersion.current || !onResponse(response)) return;
      if (!response.ok) { if (response.status >= 500) setWriteUncertain(true); setError(await errorMessage(response, "Importazione non completata. Controlla il file e la mappatura, poi riprova.")); return; }
      const raw: unknown = await response.json();
      const value = readTrackingImportResult(raw && typeof raw === "object" && "result" in raw ? raw.result : null);
      if (!value) throw new Error();
      setResult(value);
      if (value.updated) await onMutated();
    } catch { if (alive.current && version === requestVersion.current) { setWriteUncertain(true); setError("Importazione non confermata. Ricarica la pagina prima di riprovare."); } }
    finally { clearTimeout(timeout); if (alive.current && version === requestVersion.current) setBusy(null); }
  }

  function chooseManual(lineId: string) {
    const chosen = orders.find((item) => item.id === lineId);
    setManualLineId(lineId); setCarrier(chosen?.details.carrier ?? ""); setTracking(chosen?.details.tracking ?? ""); setError(""); setNotice("");
  }

  async function saveManual() {
    if (!currentManual || busy || disabled || writeUncertain || !capabilities?.can_edit || (!carrier.trim() && !tracking.trim())) {
      if (!carrier.trim() && !tracking.trim()) setError("Indica almeno il corriere oppure il tracking.");
      return;
    }
    const version = ++requestVersion.current, abort = new AbortController(); requestAbort.current = abort;
    setBusy("manual"); setError(""); setNotice(""); setResult(null);
    const timeout = setTimeout(() => abort.abort(), 30_000);
    try {
      const response = await fetch(`${baseUrl}/manual`, { method: "PATCH", headers: { "content-type": "application/json" },
        body: JSON.stringify({ account_id: accountId, environment, line_id: currentManual.id, carrier: carrier.trim(), tracking: tracking.trim() }), cache: "no-store", signal: abort.signal });
      if (!alive.current || version !== requestVersion.current || (response.status !== 404 && !onResponse(response))) return;
      if (!response.ok) { if (response.status >= 500) setWriteUncertain(true); setError(await errorMessage(response, "Salvataggio non confermato. Aggiorna gli ordini e riprova.")); return; }
      const raw: unknown = await response.json();
      if (!raw || typeof raw !== "object" || !("updated" in raw) || raw.updated !== 1) throw new Error();
      await onMutated();
      if (alive.current && version === requestVersion.current) setNotice("Corriere e tracking salvati nell’archivio.");
    } catch { if (alive.current && version === requestVersion.current) { setWriteUncertain(true); setError("Salvataggio non confermato. Ricarica la pagina prima di riprovare."); } }
    finally { clearTimeout(timeout); if (alive.current && version === requestVersion.current) setBusy(null); }
  }

  return <details className={`${styles.card} ${styles.trackingPanel}`}>
    <summary><span><DashboardIcon name="orders" size={17} /><strong>Recupera o completa corriere e tracking</strong></span><small>Importazione export o correzione manuale</small></summary>
    <div className={styles.trackingBody}>
      <p className={styles.trackingInfo}>Lo storico delle spedizioni non è sempre disponibile tramite le API pubbliche di Kaufland. Carica l’export CSV/XLSX/XLS oppure completa un ordine manualmente. I valori salvati restano disponibili dopo le sincronizzazioni successive.</p>
      {error && <p className="form-error" role="alert">{error}</p>}
      {writeUncertain && <p className={styles.muted}>Le modifiche tracking restano bloccate finché non ricarichi la pagina e rileggi l’archivio.</p>}
      {notice && <p className={styles.successNotice} role="status">{notice}</p>}
      {!capabilities && !error && <p role="status" className={styles.muted}>Verifica delle funzioni tracking…</p>}
      {capabilities && <>
        <section className={styles.trackingSection} aria-labelledby="tracking-import-title"><div className={styles.sectionHeading}><div><h3 id="tracking-import-title">Importa export ordini Kaufland</h3><p>Massimo {capabilities.max_rows.toLocaleString("it-IT")} righe, {capabilities.max_columns} colonne, {capabilities.max_cells.toLocaleString("it-IT")} celle importate, {capabilities.max_cell_length.toLocaleString("it-IT")} caratteri per cella e 5 MB. I file Excel con una struttura eccessivamente complessa vengono rifiutati. Il file non viene inviato al marketplace.</p></div></div>
          <label className={`${styles.field} ${styles.fileField}`}>Export ordini Kaufland (CSV, XLSX o XLS)<input type="file" accept=".csv,.xlsx,.xls,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" disabled={disabled || busy !== null || !capabilities.can_import} onChange={chooseFile} /></label>
          {busy === "preview" && <p role="status" className={styles.muted}>Lettura del file e riconoscimento delle colonne…</p>}
          {preview && <><p className={styles.hint}>File letto: <strong>{preview.file_name}</strong> · {preview.row_count.toLocaleString("it-IT")} righe. Controlla l’associazione prima di importare.</p>
            <div className={styles.previewScroll} tabIndex={0} role="region" aria-label="Anteprima delle prime dieci righe"><table className={styles.previewTable}><thead><tr>{preview.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{preview.rows.map((row, index) => <tr key={index}>{preview.columns.map((column) => <td key={column}>{row[column] || "—"}</td>)}</tr>)}</tbody></table></div>
            <div className={styles.mappingGrid}>{trackingFields.map((field) => <label className={styles.field} key={field}>{labels[field]}<select value={mapping[field]} disabled={disabled || busy !== null} onChange={(event) => setMapping({ ...mapping, [field]: event.target.value })}><option value="">— Non associata —</option>{preview.columns.map((column) => <option key={column} value={column}>{column}</option>)}</select></label>)}</div>
            <button type="button" className={styles.primary} disabled={disabled || writeUncertain || busy !== null || !preview.row_count || !capabilities.can_import} onClick={() => void importTracking()}>{busy === "import" ? "Importazione…" : "Importa tracking nell’archivio"}</button>
          </>}
          {result && <ImportResult result={result} />}
        </section>
        <section className={styles.trackingSection} aria-labelledby="tracking-manual-title"><div className={styles.sectionHeading}><div><h3 id="tracking-manual-title">Inserimento o correzione manuale</h3><p>Scegli una riga visibile. Per trovare altri ordini usa ricerca e filtri dell’archivio.</p></div></div>
          {orders.length ? <div className={styles.manualGrid}><label className={`${styles.field} ${styles.manualOrder}`}>Ordine da completare<select value={manualLineId} disabled={disabled || busy !== null || !capabilities.can_edit} onChange={(event) => chooseManual(event.target.value)}>{orders.map((item) => <option key={item.id} value={item.id}>{item.order_id} · unità {item.external_line_id} · {item.storefront.toUpperCase()} · {item.product_name || "Nome non disponibile"}</option>)}</select></label><label className={styles.field}>Corriere<input value={carrier} maxLength={2000} placeholder="Esempio: DPD" disabled={disabled || busy !== null || !capabilities.can_edit} onChange={(event) => setCarrier(event.target.value)} /></label><label className={styles.field}>Tracking<input value={tracking} maxLength={2000} placeholder="Esempio: 08448875901263" disabled={disabled || busy !== null || !capabilities.can_edit} onChange={(event) => setTracking(event.target.value)} /></label><button type="button" className={styles.secondary} disabled={disabled || writeUncertain || busy !== null || !capabilities.can_edit || (!carrier.trim() && !tracking.trim())} onClick={() => void saveManual()}>{busy === "manual" ? "Salvataggio…" : "Salva corriere e tracking"}</button></div>
            : <p className={styles.muted}>Sincronizza gli ordini oppure modifica i filtri per scegliere una riga.</p>}
          {!capabilities.can_edit && <p className={styles.muted}>Il tuo account può consultare i dati, ma non modificare la logistica.</p>}
        </section>
      </>}
    </div>
  </details>;
}

function ImportResult({ result }: { result: TrackingImportResult }) {
  return <div className={styles.importResult} role="status" aria-live="polite">
    {result.updated > 0 ? <p className={styles.success}>Aggiornate {result.updated.toLocaleString("it-IT")} unità ordine nell’archivio.</p> : <p>Nessuna unità ordine è stata aggiornata.</p>}
    {result.unmatched.length > 0 && <details><summary>{result.unmatched.length.toLocaleString("it-IT")} righe non corrispondono agli ordini già sincronizzati</summary><ul>{result.unmatched.slice(0, 20).map((issue) => <li key={`u-${issue.row}`}>Riga {issue.row}: {[issue.order_id, issue.order_unit_id].filter(Boolean).join(" · ") || "identificativo non riconosciuto"}</li>)}</ul>{result.unmatched.length > 20 && <p>Mostrate le prime 20 righe.</p>}</details>}
    {result.invalid.length > 0 && <details><summary>{result.invalid.length.toLocaleString("it-IT")} righe non contengono dati sufficienti</summary><ul>{result.invalid.slice(0, 20).map((issue) => <li key={`i-${issue.row}`}>Riga {issue.row}: {issue.error}</li>)}</ul>{result.invalid.length > 20 && <p>Mostrate le prime 20 righe.</p>}</details>}
  </div>;
}
