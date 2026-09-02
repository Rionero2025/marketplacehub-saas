export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const normalized = String(status || "").toLowerCase();
  const tone = ["done","active","completed","success","available"].includes(normalized)
    ? "success" : ["running","queued","trial","processing"].includes(normalized)
    ? "info" : ["error","failed","suspended","past_due","cancelled","canceled"].includes(normalized)
    ? "danger" : "neutral";
  return <span className={`statusBadge ${tone}`}><i />{label || status || "—"}</span>;
}
