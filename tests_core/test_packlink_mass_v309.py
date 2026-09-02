from marketplace_core.packlink import PacklinkCore, PacklinkScope


def _contains_secret(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"api_key", "secret_key", "credentials", "password"}:
                return True
            if _contains_secret(item):
                return True
    if isinstance(value, (list, tuple)):
        return any(_contains_secret(item) for item in value)
    return False


def test_mass_quote_job_is_small_and_credential_free():
    request = PacklinkCore().build_mass_quotes_job(
        PacklinkScope(3),
        [{
            "account_id": 7,
            "marketplace": "kaufland",
            "order_id": "M123",
            "order_key": "7:kaufland:M123",
            "package": {"weight": 1.2, "length": 20, "width": 15, "height": 10},
            "customer_name": "must not enter job",
        }],
        origin_country="IT",
        origin_zip="80078",
    )
    assert request.kind == "packlink.quotes.mass"
    assert request.seller_id == 3
    assert request.payload["tasks"][0]["order_id"] == "M123"
    assert "customer_name" not in request.payload["tasks"][0]
    assert not _contains_secret(request.payload)


def test_mass_draft_job_keeps_only_identity_and_shipping_choice():
    request = PacklinkCore().build_mass_drafts_job(
        PacklinkScope(3),
        [{
            "account_id": 7,
            "marketplace": "kaufland",
            "order_id": "M123",
            "order_key": "7:kaufland:M123",
            "package": {"weight": 1.2, "length": 20, "width": 15, "height": 10},
            "service": {"id": "99", "carrier": "DPD", "service": "Home", "price": 9.5},
            "declared_value": 49.9,
            "forced": False,
            "customer_email": "must not enter task",
        }],
        sender={"country": "IT", "zip_code": "80078"},
        warehouse_id="wh-1",
    )
    assert request.kind == "packlink.drafts.mass"
    task = request.payload["tasks"][0]
    assert task["service"]["id"] == "99"
    assert "customer_email" not in task
    assert not _contains_secret(request.payload)
