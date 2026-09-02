import type { ReactNode } from "react";
export function StatCard({ label, value, meta }: { label: string; value: ReactNode; meta?: ReactNode }) {
  return <div className="statCard"><span>{label}</span><strong>{value}</strong>{meta && <small>{meta}</small>}</div>;
}
