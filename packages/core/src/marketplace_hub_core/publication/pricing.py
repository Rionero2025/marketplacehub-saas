import csv
import io
import re

from marketplace_hub_core.catalogs.work import legacy_round
from marketplace_hub_core.publication.models import Rules


def prepare(row: dict, supplier: str, marketplace: str, rules: Rules):
    """Transfer original publication arithmetic; shipping is included once."""
    purchase = float(row.get("cost") or 0)
    shipping = max(0, float(row.get("total_cost") or 0) - purchase)
    base = purchase + shipping
    qty = int(float(row.get("quantity") or 0))
    weight = float(row.get("weight_kg") or 0)
    if qty < rules.min_qty or base < rules.min_cost:
        return None
    if weight > 0 and (
        (rules.weight_mode == "above" and weight > rules.weight_from)
        or (rules.weight_mode == "below" and weight < rules.weight_from)
        or (rules.weight_mode == "between" and rules.weight_from <= weight <= rules.weight_to)
    ):
        return None
    price = base * (1 + rules.margin / 100)
    minimum = base * (1 + rules.minimum_margin / 100)
    if marketplace == "kaufland":
        fee = price * rules.commission / 100
        cost, price, minimum, fee, profit = map(
            lambda v: round(v, 2), (base, price, minimum, fee, price - fee - base)
        )
    else:
        cost, price, minimum = map(legacy_round, (base, price, minimum))
        fee = legacy_round(price * rules.commission / 100)
        profit = legacy_round(price - fee - base)
    if marketplace == "worten" and profit < rules.min_profit:
        return None
    ean = str(row.get("ean") or "").strip().removesuffix(".0")
    sku = str(row.get("sku") or "").strip()
    prefix = re.sub(r"[^A-Za-z0-9-]+", "-", supplier.strip()).strip("-") or "FORNITORE"
    if rules.composite_sku:
        suffix = f"_{ean}_{cost:.2f}_{minimum:.2f}"
        sku = f"{prefix[: max(1, 40 - len(suffix))] if marketplace == 'worten' else prefix}{suffix}"
    if marketplace == "worten":
        sku = sku.replace("/", "-")[:40]
    problem = ""
    if not ean or ean.lower() in {"nan", "none", "null", "<na>"}:
        problem = "EAN mancante"
    elif not sku or len(sku) > 100:
        problem = "SKU mancante o troppo lungo"
    elif price <= 0 or minimum <= 0 or minimum > price:
        problem = "Prezzi non validi: verifica ricarico e prezzo minimo"
    if marketplace == "kaufland":
        listing = int(round(price * rules.multiplier * 100))
        floor = int(round(minimum * rules.multiplier * 100))
        maximum = {"cz": 2500000000, "pl": 450000000}.get(rules.storefront, 100000000)
        if min(listing, floor) < 1 or max(listing, floor) > maximum:
            problem = "Prezzi fuori dai limiti del marketplace"
        payload = {
            "ean": ean,
            "condition": "NEW",
            "id_offer": sku,
            "amount": min(99999, qty),
            "listing_price": listing,
            "minimum_price": floor,
            "handling_time": rules.handling,
            "id_shipping_group": rules.shipping_group,
            "id_warehouse": rules.warehouse,
            "vat_indicator": rules.vat,
        }
    else:
        payload = {
            "sku": sku,
            "product-id": ean,
            "product-id-type": "EAN",
            "description": str(row.get("name") or ""),
            "internal-description": str(row.get("name") or ""),
            "price": f"{price:.2f}",
            "quantity": str(max(0, qty)),
            "state": rules.state_code,
            "leadtime-to-ship": str(rules.handling),
            "logistic-class": rules.logistic_class,
            "update-delete": "update",
            "price[channel=WRT_PT_ONLINE]": f"{price:.2f}",
            "description-pt": str(row.get("name") or ""),
            "ship-from-country-offer": rules.ship_from,
        }
    public = {
        "name": str(row.get("name") or ""),
        "ean": ean,
        "sku": sku,
        "quantity": min(99999, qty) if marketplace == "kaufland" else qty,
        "cost": f"{cost:.2f}",
        "price": f"{price:.2f}",
        "minimum_price": f"{minimum:.2f}",
        "commission": f"{fee:.2f}",
        "profit": f"{profit:.2f}",
        "weight_kg": row.get("weight_kg"),
        "problem": problem,
    }
    return public, payload


WORTEN_COLUMNS = [
    "sku",
    "product-id",
    "product-id-type",
    "description",
    "internal-description",
    "price",
    "price-additional-info",
    "quantity",
    "min-quantity-alert",
    "state",
    "available-start-date",
    "available-end-date",
    "logistic-class",
    "favorite-rank",
    "discount-price",
    "discount-start-date",
    "discount-end-date",
    "leadtime-to-ship",
    "max-order-quantity",
    "package-quantity",
    "update-delete",
    "price[channel=WRT_ES_ONLINE]",
    "discount-price[channel=WRT_ES_ONLINE]",
    "discount-start-date[channel=WRT_ES_ONLINE]",
    "discount-end-date[channel=WRT_ES_ONLINE]",
    "price[channel=WRT_PT_ONLINE]",
    "discount-price[channel=WRT_PT_ONLINE]",
    "discount-start-date[channel=WRT_PT_ONLINE]",
    "discount-end-date[channel=WRT_PT_ONLINE]",
    "description-es",
    "description-pt",
    "ship-from-country-offer",
    "package-length",
    "package-width",
    "package-height",
    "package-weight",
    "package-fragile",
    "unit-measurement",
    "unit-price-es",
    "pvpr-es",
    "pvpr-pt",
    "unit-price-pt",
]


def worten_csv(rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=WORTEN_COLUMNS, delimiter=";", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8-sig")
