from datetime import UTC, datetime, timedelta

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

ORDERS_QUEUE_NAME = "marketplace-hub"


class RQOrdersQueue:
    def __init__(self, connection: Redis):
        self.connection = connection
        self.queue = Queue(ORDERS_QUEUE_NAME, connection=connection)

    def enqueue(self, job_id):
        self.queue.enqueue("marketplace_hub_worker.jobs.sync_orders", str(job_id),
                           job_id=str(job_id), job_timeout=3600, result_ttl=86400,
                           failure_ttl=86400)

    def state(self, job_id):
        try:
            job = Job.fetch(str(job_id), connection=self.connection)
            status = job.get_status(refresh=True)
            state = getattr(status, "value", str(status))
            heartbeat = job.last_heartbeat
            if heartbeat and heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=UTC)
            if (state == "started" and heartbeat
                    and datetime.now(UTC) - heartbeat > timedelta(minutes=5)):
                return "stale"
            return state
        except NoSuchJobError:
            return "missing"
