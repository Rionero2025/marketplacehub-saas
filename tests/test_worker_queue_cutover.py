from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

from marketplace_hub_core.catalogs import queue as catalog_queue
from marketplace_hub_core.orders import queue as orders_queue
from marketplace_hub_worker import main as worker_main


class RecordingQueue:
    instances: list[RecordingQueue] = []

    def __init__(self, name: str, *, connection: object) -> None:
        self.name = name
        self.connection = connection
        self.enqueued: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.instances.append(self)

    def enqueue(self, *args: object, **kwargs: object) -> None:
        self.enqueued.append((args, kwargs))


def test_catalog_and_order_jobs_use_separate_named_queues(monkeypatch) -> None:
    RecordingQueue.instances = []
    connection = object()
    monkeypatch.setattr(catalog_queue, "Queue", RecordingQueue)
    monkeypatch.setattr(orders_queue, "Queue", RecordingQueue)

    catalog_adapter = catalog_queue.RQCatalogsQueue(connection)
    orders_adapter = orders_queue.RQOrdersQueue(connection)
    catalog_job_id = UUID(int=101)
    order_job_id = UUID(int=102)

    catalog_adapter.enqueue(catalog_job_id)
    orders_adapter.enqueue(order_job_id)

    assert [queue.name for queue in RecordingQueue.instances] == [
        catalog_queue.CATALOG_QUEUE_NAME,
        orders_queue.ORDERS_QUEUE_NAME,
    ]
    assert catalog_adapter.queue.enqueued == [
        (
            ("marketplace_hub_worker.jobs.refresh_catalog", str(catalog_job_id)),
            {
                "job_id": f"catalog-feed:{catalog_job_id}",
                "job_timeout": 1_200,
                "result_ttl": 86_400,
                "failure_ttl": 86_400,
            },
        )
    ]
    assert orders_adapter.queue.enqueued[0][0] == (
        "marketplace_hub_worker.jobs.sync_orders",
        str(order_job_id),
    )


def test_worker_listens_to_catalog_and_legacy_queues_with_round_robin(monkeypatch) -> None:
    RecordingQueue.instances = []
    connection = object()
    worker_calls: list[tuple[list[RecordingQueue], object]] = []
    work_calls: list[dict[str, object]] = []

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str) -> object:
            assert url == "redis://cutover.test/0"
            return connection

    class FakeRoundRobinWorker:
        def __init__(
            self,
            queues: list[RecordingQueue],
            *,
            connection: object,
        ) -> None:
            worker_calls.append((queues, connection))

        def work(self, **kwargs: object) -> None:
            work_calls.append(kwargs)

    monkeypatch.setattr(worker_main, "Redis", FakeRedis)
    monkeypatch.setattr(worker_main, "Queue", RecordingQueue)
    monkeypatch.setattr(worker_main, "RoundRobinWorker", FakeRoundRobinWorker)
    monkeypatch.setattr(
        worker_main,
        "get_settings",
        lambda: SimpleNamespace(
            redis_url=SimpleNamespace(get_secret_value=lambda: "redis://cutover.test/0")
        ),
    )

    worker_main.run()

    assert worker_main.WORKER_QUEUE_NAMES == (
        catalog_queue.CATALOG_QUEUE_NAME,
        orders_queue.ORDERS_QUEUE_NAME,
    )
    assert [queue.name for queue in RecordingQueue.instances] == [
        "marketplace-hub-catalog-v2",
        "marketplace-hub",
    ]
    assert worker_calls == [(RecordingQueue.instances, connection)]
    assert work_calls == [{"with_scheduler": False}]


def test_catalog_state_recovery_keeps_the_durable_job_identity(monkeypatch) -> None:
    RecordingQueue.instances = []
    connection = object()
    job_id = UUID(int=103)
    fetch_calls: list[tuple[str, object]] = []

    monkeypatch.setattr(catalog_queue, "Queue", RecordingQueue)

    def fetch(rq_job_id: str, *, connection: object) -> SimpleNamespace:
        fetch_calls.append((rq_job_id, connection))
        return SimpleNamespace(
            get_status=lambda refresh: SimpleNamespace(value="queued"),
            last_heartbeat=None,
        )

    monkeypatch.setattr(catalog_queue.Job, "fetch", fetch)

    adapter = catalog_queue.RQCatalogsQueue(connection)

    assert adapter.state(job_id) == "queued"
    assert adapter.queue.name == "marketplace-hub-catalog-v2"
    assert fetch_calls == [(f"catalog-feed:{job_id}", connection)]
