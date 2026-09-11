import type { CatalogFeedJob } from "../lib/catalog-types";

const percentage = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 0 });
const megabytes = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 1 });

function duration(seconds: number) {
  if (seconds < 60) return "meno di 1 minuto";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ""}`;
}

export function CatalogJobProgress({ job, priceListName }: { job: CatalogFeedJob | null; priceListName: string }) {
  if (!job || job.status === "error") return null;
  const done = job.status === "done" || job.status === "ready";
  const queued = job.status === "queued" || job.status === "pending";
  const processing = job.message === "Elaborazione prodotti in corso." || job.message === "Salvataggio prodotti in corso.";
  const forecast = job.forecast;
  // Old API responses remain readable during a rolling deployment.
  const value = done ? 100 : queued ? 0 : forecast ? Math.min(99, forecast.percent ?? 0) : processing ? null
    : job.total_bytes ? Math.min(100, Math.floor(job.processed_bytes / job.total_bytes * 100)) : null;
  const label = done ? "100% · Completato" : queued ? "0% · In attesa di avvio"
    : forecast ? `${percentage.format(value ?? 0)}% · Completamento stimato`
      : processing ? job.message! : value === null ? "Download in corso" : `${percentage.format(value)}% · Download`;
  const remaining = forecast?.state === "stalled" ? "In attesa di nuovi dati"
    : forecast?.state === "recalculating" ? "Tempo residuo: ricalcolo in corso"
      : forecast?.remaining_seconds != null ? `Tempo residuo: circa ${duration(forecast.remaining_seconds)}`
        : queued ? "Tempo residuo: disponibile all’avvio" : "Tempo residuo: calcolo in corso";
  const bytes = `${megabytes.format(job.processed_bytes / 1_000_000)} MB`;
  const downloaded = job.total_bytes && !processing && !done
    ? `${bytes} di ${megabytes.format(job.total_bytes / 1_000_000)} MB`
    : `${bytes} scaricati`;

  return <span className={`catalog-job-progress${done ? " is-complete" : ""}`}>
    <span className={`catalog-progress-track${value === null ? " is-indeterminate" : ""}`}
      role="progressbar" aria-label={`Avanzamento aggiornamento ${priceListName}`}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined} aria-valuetext={label}>
      <span className="catalog-progress-fill" style={value === null ? undefined : { width: `${value}%` }} />
    </span>
    <span className="catalog-progress-label">{label}</span>
    {!done && <span className="catalog-progress-eta">{remaining}</span>}
    {!done && !queued && forecast && <span className="catalog-progress-bytes">{processing ? job.message : "Download in corso"}</span>}
    {!done && forecast?.basis === "previous-size" && <span className="catalog-progress-bytes">Stima basata sui download precedenti</span>}
    {!queued && job.processed_bytes > 0 && <span className="catalog-progress-bytes">{downloaded}</span>}
  </span>;
}
