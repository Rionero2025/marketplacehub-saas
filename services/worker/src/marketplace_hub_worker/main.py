from __future__ import annotations

from marketplace_hub_core.catalogs.queue import CATALOG_QUEUE_NAME
from marketplace_hub_core.orders.queue import ORDERS_QUEUE_NAME
from marketplace_hub_core.settings import get_settings
from redis import Redis
from rq import Queue
from rq.worker import RoundRobinWorker

WORKER_QUEUE_NAMES = (CATALOG_QUEUE_NAME, ORDERS_QUEUE_NAME)


def run() -> None:
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url.get_secret_value())
    queues = [Queue(name, connection=connection) for name in WORKER_QUEUE_NAMES]
    # RQ assigns a unique process name so old and new workers can overlap during deploys.
    # Round-robin avoids starving legacy order work while large catalog jobs arrive.
    worker = RoundRobinWorker(queues, connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    run()
