from __future__ import annotations

from marketplace_hub_core.settings import get_settings
from redis import Redis
from rq import Queue, Worker


def run() -> None:
    settings = get_settings()
    connection = Redis.from_url(settings.redis_url.get_secret_value())
    queue = Queue("marketplace-hub", connection=connection)
    # RQ assigns a unique process name so old and new workers can overlap during deploys.
    worker = Worker([queue], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    run()
