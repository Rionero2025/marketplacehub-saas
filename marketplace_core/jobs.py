from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from marketplace_core.contracts import JobReceipt, JobRequest


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    job_id: str
    kind: str
    status: str
    seller_id: int | None
    progress_current: int
    progress_total: int
    progress_pct: float
    message: str
    result: Mapping[str, Any]
    error: str
    created_at: str
    started_at: str
    finished_at: str

    @property
    def terminal(self) -> bool:
        return self.status in {"done", "error", "cancelled"}


class JobsCore:
    """Persistent background-job boundary, independent from Streamlit.

    The current deployment can execute jobs in a daemon thread. A dedicated
    worker process can claim the exact same records later without changing the UI.
    """

    def submit(self, request: JobRequest) -> JobReceipt:
        from services.background_jobs import enqueue_job
        return enqueue_job(request)

    def snapshot(self, job_id: str) -> JobSnapshot | None:
        from services.background_jobs import get_job
        item = get_job(job_id)
        if not item:
            return None
        return JobSnapshot(
            job_id=str(item.get("id") or ""),
            kind=str(item.get("kind") or ""),
            status=str(item.get("status") or ""),
            seller_id=int(item["seller_id"]) if item.get("seller_id") is not None else None,
            progress_current=int(item.get("progress_current") or 0),
            progress_total=int(item.get("progress_total") or 0),
            progress_pct=float(item.get("progress_pct") or 0.0),
            message=str(item.get("message") or ""),
            result=item.get("result") or {},
            error=str(item.get("error") or ""),
            created_at=str(item.get("created_at") or ""),
            started_at=str(item.get("started_at") or ""),
            finished_at=str(item.get("finished_at") or ""),
        )

    def start_local(self, job_id: str) -> bool:
        from services.background_jobs import start_job_in_background
        return start_job_in_background(job_id)
