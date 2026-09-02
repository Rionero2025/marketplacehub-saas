from __future__ import annotations

from marketplace_core.buybox import BuyBoxCore, BuyBoxQuery, BuyBoxScope


def test_job_contract_is_worker_ready():
    core = BuyBoxCore()
    scope = BuyBoxScope(3, 7, "kaufland", "live")
    job = core.build_refresh_job(scope, mode="quick", storefronts=("de", "it"), skus=("A", "B"))
    assert job.kind == "buybox.kaufland.quick"
    assert job.seller_id == 3
    assert job.payload["account_id"] == 7
    assert job.payload["storefronts"] == ["de", "it"]


def test_query_validation():
    try:
        BuyBoxQuery(limit=0)
    except ValueError:
        pass
    else:
        raise AssertionError("limit=0 doveva essere rifiutato")
