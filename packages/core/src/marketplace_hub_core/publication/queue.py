from uuid import uuid4

from rq import Queue

PUBLICATION_QUEUE = "marketplace-hub-publication-v1"


class PublicationQueue:
    def __init__(self, connection):
        self.queue = Queue(PUBLICATION_QUEUE, connection=connection)

    def enqueue(self, job_id):
        self.queue.enqueue(
            "marketplace_hub_worker.jobs.publish_offers",
            str(job_id),
            job_id=f"publication-{job_id}-{uuid4()}",
            job_timeout=3600,
            result_ttl=86400,
            failure_ttl=86400,
        )
