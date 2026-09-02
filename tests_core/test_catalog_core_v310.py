from marketplace_core.catalogs import CatalogCore


def test_catalog_materialize_job_is_small_and_credential_free():
    request = CatalogCore().build_materialize_job(3, 22)
    assert request.kind == "catalog.materialize"
    assert request.seller_id == 3
    assert request.payload == {"price_list_id": 22}
    assert "path" not in request.payload
    assert "credentials" not in request.payload


def test_catalog_csv_separator_detection(tmp_path):
    path = tmp_path / "catalog.csv"
    path.write_text("EAN;SKU;Price\\n123;ABC;10.5\\n", encoding="utf-8")
    assert CatalogCore()._csv_separator(path) == ";"
