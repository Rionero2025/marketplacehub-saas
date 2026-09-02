"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { Job } from "@/lib/types";
import { Icon } from "./Icon";

export function JobPulse({ sellerId }: { sellerId?: number }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  useEffect(() => {
    if (!sellerId) { setJobs([]); return; }
    let live = true;
    const load = () => api<Job[]>(`/jobs?seller_id=${sellerId}&limit=8`).then(x => live && setJobs(x)).catch(() => live && setJobs([]));
    void load(); const timer = setInterval(load, 6000);
    return () => { live = false; clearInterval(timer); };
  }, [sellerId]);
  const active = useMemo(() => jobs.filter(j => ["queued","running"].includes(j.status.toLowerCase())), [jobs]);
  const latest = active[0] || jobs[0];
  return <Link href="/jobs" className={`jobPulse ${active.length ? "busy" : ""}`} title={latest?.message || "Attività background"}>
    <span className="jobPulseIcon"><Icon name="activity" size={16}/>{active.length > 0 && <i className="pulseDot"/>}</span>
    <span className="jobPulseText"><b>{active.length ? `${active.length} attività` : "Sistema pronto"}</b><small>{active.length ? `${Math.round(latest?.progress_pct || 0)}% · ${latest?.kind || "job"}` : "Nessun job in esecuzione"}</small></span>
  </Link>;
}
