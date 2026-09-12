"""Bounded, exact-EAN enrichment of existing seller views from InnPro snapshots."""

import json
from datetime import UTC, datetime
from html.parser import HTMLParser

from sqlalchemy import bindparam, case, func, select, update

from marketplace_hub_core.catalogs.measurements import FIELDS
from marketplace_hub_core.catalogs.schema import seller_catalog_view_rows as rows
from marketplace_hub_core.catalogs.schema import seller_catalog_views as views
from marketplace_hub_core.catalogs.schema import seller_price_list_products as products
from marketplace_hub_core.catalogs.service import CatalogValidationError


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def extra_content(canonical):
    if not canonical:
        return {}
    source = json.loads(canonical).get("source", {})
    if not isinstance(source, dict):
        return {}
    result = {
        key: str(source.get(key) or "")[:500]
        for key in ("brand", "category", "producer_code_standard", "warranty")
    }
    plain = PlainText()
    plain.feed(str(source.get("description") or source.get("short_description") or "")[:20000])
    result["description"] = " ".join(plain.parts)[:4000]
    return result


def enrich_view(repo, org, seller, view_id, payload):
    recipe = payload.recipe
    if not recipe.content_list_id:
        raise CatalogValidationError("Seleziona il FULL dello stesso fornitore InnPro.")
    report = dict.fromkeys(
        (
            "total",
            "full_matched",
            "full_missing",
            "full_duplicate",
            "light_matched",
            "light_missing",
            "light_duplicate",
        ),
        0,
    )
    stamp = datetime.now(UTC)
    with repo.engine.begin() as c:
        repo._source(c, org, seller, recipe, lock=True)
        view = repo._view(c, org, seller, view_id, lock=True)
        repo._revision(view, payload.expected_revision)
        original = json.loads(view["recipe_json"])
        if original.get("price_list_id") != str(recipe.price_list_id):
            raise CatalogValidationError("Usa il LIGHT di origine della vista.")
        last = -1
        while True:
            batch = (
                c.execute(
                    select(rows.c.id, rows.c.position, rows.c.data_json)
                    .where(rows.c.view_id == view_id, rows.c.position > last)
                    .order_by(rows.c.position)
                    .limit(100)
                )
                .mappings()
                .all()
            )
            if not batch:
                break
            last = batch[-1]["position"]
            decoded = [(r, json.loads(r["data_json"])) for r in batch]
            eans = {d.get("ean", "").strip() for _, d in decoded} - {""}
            sources = []
            for list_id, version, content in (
                (recipe.price_list_id, recipe.version, False),
                (recipe.content_list_id, recipe.content_version, True),
            ):
                scope = [
                    products.c.price_list_id == list_id,
                    products.c.version_number == version,
                    *repo.scope(products, org, seller),
                    products.c.ean.in_(eans),
                ]
                counts = dict(
                    c.execute(
                        select(products.c.ean, func.count()).where(*scope).group_by(products.c.ean)
                    ).all()
                )
                unique = [ean for ean, count in counts.items() if count == 1]
                columns = [
                    products.c.ean,
                    products.c.name,
                    products.c.quantity,
                    *[products.c[f] for f in FIELDS],
                ]
                if content:
                    # A large description must never materialize the whole FULL feed.
                    columns.append(
                        case(
                            (
                                func.length(products.c.canonical_json) <= 131072,
                                products.c.canonical_json,
                            ),
                            else_=None,
                        ).label("canonical")
                    )
                found = (
                    c.execute(select(*columns).where(*scope, products.c.ean.in_(unique)))
                    .mappings()
                    .all()
                    if unique
                    else []
                )
                sources.append((counts, {r["ean"]: r for r in found}))
            patches = []
            for record, data in decoded:
                report["total"] += 1
                ean = data.get("ean", "").strip()
                states = {}
                for role, (counts, found) in zip(("light", "full"), sources, strict=True):
                    state = (
                        "matched"
                        if counts.get(ean) == 1
                        else "duplicate"
                        if counts.get(ean, 0) > 1
                        else "missing"
                    )
                    report[f"{role}_{state}"] += 1
                    states[role] = state
                    match = found.get(ean)
                    if not match:
                        continue
                    if role == "light":
                        data["quantity"] = (
                            str(match["quantity"])
                            if match["quantity"] is not None
                            else data["quantity"]
                        )
                        if match["quantity"] is None:
                            states[role] = "missing"
                            report["light_matched"] -= 1
                            report["light_missing"] += 1
                    else:
                        if not data.get("name", "").strip():
                            data["name"] = match["name"] or ""
                        for field in FIELDS:
                            if not data.get(field) or float(data[field]) <= 0:
                                data[field] = (
                                    str(match[field]) if match[field] is not None else None
                                )
                        data["product_info"] = extra_content(match["canonical"])
                data["innpro_match"] = {
                    **states,
                    "light_version": recipe.version,
                    "full_version": recipe.content_version,
                    "checked_at": stamp.isoformat(),
                }
                patches.append(
                    {"row_id": record["id"], "content": json.dumps(data, ensure_ascii=False)}
                )
            c.execute(
                update(rows)
                .where(rows.c.view_id == view_id, rows.c.id == bindparam("row_id"))
                .values(data_json=bindparam("content")),
                patches,
            )
        # Preserve the original economic recipe and filters; record the content association.
        original.update(
            content_list_id=str(recipe.content_list_id), content_version=recipe.content_version
        )
        c.execute(
            update(views)
            .where(views.c.id == view_id)
            .values(
                recipe_json=json.dumps(original), revision=view["revision"] + 1, updated_at=stamp
            )
        )
    result = repo.detail(org, seller, view_id, 1)
    result["match_report"] = report
    return result
