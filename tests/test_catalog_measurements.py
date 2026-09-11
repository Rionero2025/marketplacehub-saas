from __future__ import annotations

import json
from decimal import Decimal

import pytest
import test_catalog_artifact_repository as helpers
from marketplace_hub_core.catalogs import measurements as module
from marketplace_hub_core.catalogs.measurements import MeasurementFilter, measurements
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.catalogs.schema import seller_price_list_products as products
from sqlalchemy import select

workspace = helpers.workspace


def test_generic_units_and_unknowns_are_not_zero():
    value = measurements(json.dumps({"source": {
        "Peso (kg)": "1,03", "Lunghezza (mm)": "290", "Larghezza (cm)": "18",
        "Altezza (m)": "0.05", "image_width": "1200",
    }}))
    assert value == dict(weight_kg=Decimal("1.03"), length_cm=Decimal(29),
                         width_cm=Decimal(18), height_cm=Decimal(5))
    for raw in (None, "", "NaN", "-1", "0", "unknown", "10-20", "1e999", ["1", "2"]):
        assert measurements(json.dumps({"source": {"weight_kg": raw}}))["weight_kg"] is None
    assert measurements('{"source":{"image_width":1200,"width":1200}}')["width_cm"] is None


def test_innpro_weight_and_explicit_unit_requirement(monkeypatch):
    canonical = json.dumps({"source": {"weight_g": "1030", "parameters": {
        "Box length": "29,00", "Box width": "18,00", "Box height": "5,00",
    }}})
    monkeypatch.setattr(module, "INNPRO_BOX_UNIT", None)
    value = measurements(canonical, provider="innpro")
    assert value["weight_kg"] == Decimal("1.03")
    assert value["length_cm"] is None
    assert module.unknown_dimensions(canonical, provider="innpro") == "29 × 18 × 5"
    monkeypatch.setattr(module, "INNPRO_BOX_UNIT", "cm")
    assert measurements(canonical, provider="innpro")["length_cm"] == Decimal(29)
    assert module.unknown_dimensions(canonical, provider="innpro") is None


def _list(workspace, rows):
    org, seller = helpers._scope(workspace)
    repo = SqlCatalogsRepository(workspace.engine)
    supplier = repo.add_supplier(org, seller, name="Misure", notes="")
    items = [{**helpers._product(i + 1), "canonical_json": json.dumps({"source": source})}
             for i, source in enumerate(rows)]
    list_id = repo.add_price_list(org, seller, supplier, name="Listino",
                                 original_filename="list.csv", media_type="text/csv",
                                 file_format="csv", artifact=b"data", normalized_products=items)
    return repo, org, seller, list_id


@pytest.mark.parametrize(("mode", "lower", "upper", "expected"), [
    ("above", "10", "0", [1, 2, 3, 4]),
    ("below", "10", "0", [1, 2, 4, 5]),
    ("between", "5", "10", [1, 2, 5]),
    ("none", "0", "0", [1, 2, 3, 4, 5]),
])
def test_filters_match_streamlit_boundaries_and_keep_unknowns(
    workspace, mode, lower, upper, expected,
):
    repo, org, seller, list_id = _list(workspace, [{"weight_kg": v} for v in (None, 0, 5, 10, 15)])
    result = repo.detail(org, seller, list_id, limit=200, measurement_filter=MeasurementFilter(
        "weight_kg", mode, Decimal(lower), Decimal(upper),
    ))
    assert [p["source_row"] for p in result["products"]] == expected
    assert result["row_count"] == 5
    assert result["filtered_count"] == len(expected)


def test_filter_applies_before_preview_limit_and_preserves_tenant_scope(workspace):
    repo, org, seller, list_id = _list(
        workspace, [{"length_cm": "20"}] * 201 + [{"length_cm": "5"}],
    )
    other_repo, other_org, other_seller, other_list = _list(workspace, [{"length_cm": "5"}])
    query = MeasurementFilter("length_cm", "above", Decimal(10))
    result = repo.detail(org, seller, list_id, limit=1, measurement_filter=query)
    assert result["filtered_count"] == 1
    assert result["products"][0]["source_row"] == 202
    assert other_repo.detail(other_org, other_seller, other_list, limit=1)["row_count"] == 1
    from marketplace_hub_core.catalogs.repository import CatalogPriceListNotFoundError
    with pytest.raises(CatalogPriceListNotFoundError):
        repo.detail(org, seller, other_list, limit=1, measurement_filter=query)


def test_backfill_is_bounded_repeatable_and_scoped(workspace):
    repo, org, seller, list_id = _list(workspace, [{"weight_g": "2500"}] * 21)
    _, _, other_seller, _ = _list(workspace, [{"weight_kg": "3"}])
    with workspace.engine.begin() as c:
        c.execute(products.update().values(weight_kg=None))
    assert repo.backfill_measurements(seller) == 21
    assert repo.backfill_measurements(seller) == 21
    assert repo.detail(org, seller, list_id, limit=1)["products"][0]["weight_kg"] == "2.5"
    with workspace.engine.connect() as c:
        assert c.scalar(select(products.c.weight_kg).where(
            products.c.seller_id == other_seller,
        )) is None


@pytest.mark.parametrize("args", [
    ("bad", "above", "1", "0"), ("weight_kg", "invalid", "1", "0"),
    ("weight_kg", "between", "10", "5"), ("weight_kg", "above", "NaN", "0"),
    ("weight_kg", "above", "-1", "0"),
])
def test_invalid_filters_are_rejected(args):
    field, mode, lower, upper = args
    with pytest.raises(ValueError):
        MeasurementFilter(field, mode, Decimal(lower), Decimal(upper))
