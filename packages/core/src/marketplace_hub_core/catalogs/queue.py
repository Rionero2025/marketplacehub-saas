from datetime import UTC, datetime, timedelta

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

CATALOG_QUEUE_NAME = "marketplace-hub-catalog-v2"


class RQCatalogsQueue:
    """Enqueue catalog work using durable identifiers only."""

    def __init__(self, connection: Redis) -> None:
        self.connection = connection
        self.queue = Queue(CATALOG_QUEUE_NAME, connection=connection)

    @staticmethod
    def _rq_job_id(job_id) -> str:
        return f"catalog-feed:{job_id}"

    def enqueue(self, job_id) -> None:
        self.queue.enqueue(
            "marketplace_hub_worker.jobs.refresh_catalog",
            str(job_id),
            job_id=self._rq_job_id(job_id),
            job_timeout=1_200,
            result_ttl=86_400,
            failure_ttl=86_400,
        )

    def state(self, job_id) -> str:
        """Return a sanitized RQ state for one durable catalog job."""
        try:
            job = Job.fetch(self._rq_job_id(job_id), connection=self.connection)
            status = job.get_status(refresh=True)
            state = getattr(status, "value", str(status))
            heartbeat = job.last_heartbeat
            if heartbeat and heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
            if (
                state == "started"
                and heartbeat
                and datetime.now(UTC) - heartbeat > timedelta(minutes=5)
            ):
                return "stale"
            return state
        except NoSuchJobError:
            return "missing"
