import type { CatalogFeedJob } from "../lib/catalog-types";

const percentage = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 0 });
const megabytes = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 1 });

export function CatalogJobProgress({ job, priceListName }: { job: CatalogFeedJob | null; priceListName: string }) {
  if (!job || job.status === "error") return null;
  const done = job.status === "done";
  const queued = job.status === "queued" || job.status === "pending";
  const processing = job.message === "Elaborazione prodotti in corso." || job.message === "Salvataggio prodotti in corso.";
  // Only the download has a measurable total. Parsing and saving stay indeterminate.
  const value = done ? 100 : queued ? 0 : processing ? null
    : job.total_bytes ? Math.min(100, Math.floor(job.processed_bytes / job.total_bytes * 100)) : null;
  const label = done ? "100% · Completato" : queued ? "In attesa di avvio"
    : processing ? job.message! : value === null ? "Download in corso" : `${percentage.format(value)}% · Download`;
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
    {!queued && job.processed_bytes > 0 && <span className="catalog-progress-bytes">{downloaded}</span>}
  </span>;
}
