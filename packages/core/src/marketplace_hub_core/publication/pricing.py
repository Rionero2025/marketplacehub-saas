import csv
import io
import re
from decimal import ROUND_HALF_UP, Decimal

from marketplace_hub_core.catalogs.measurements import FIELDS
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
    if (row.get("innpro_match") or {}).get("light", "matched") not in {"matched", "manual"}:
        problem = "Stock LIGHT non verificato: controlla il match EAN"
    public = {
        **{field: row.get(field) for field in FIELDS},
        "product_info": row.get("product_info"),
        "innpro_match": row.get("innpro_match"),
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


def edit_offer(public, payload, changes, marketplace, rules):
    """Edit a draft snapshot without applying commercial markups a second time."""
    public, payload = dict(public), dict(payload)
    public.update(changes)

    def money(v):
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    for key in ("cost", "price", "minimum_price", "commission"):
        public[key] = str(money(public[key]))
    if "price" in changes and "commission" not in changes:
        public["commission"] = str(
            money(Decimal(public["price"]) * Decimal(str(rules.commission)) / 100)
        )
    if {"price", "cost", "commission"} & changes.keys():
        public["profit"] = str(
            money(
                Decimal(public["price"]) - Decimal(public["cost"]) - Decimal(public["commission"])
            )
        )
    public["ean"] = str(public["ean"]).strip()
    public["sku"] = str(public["sku"]).strip()
    if "sku" in changes:
        public["sku_manual"] = True
    if (
        rules.composite_sku
        and not public.get("sku_manual", False)
        and {"ean", "cost", "minimum_price"} & changes.keys()
    ):
        prefix = public["sku"].rsplit("_", 3)[0]
        suffix = f"_{public['ean']}_{public['cost']}_{public['minimum_price']}"
        public["sku"] = (
            prefix[: max(1, 40 - len(suffix))] if marketplace == "worten" else prefix
        ) + suffix
    weight = public.get("weight_kg")
    public["weight_kg"] = None if weight is None else str(weight)
    price, minimum = float(public["price"]), float(public["minimum_price"])
    problem = ""
    if not public["ean"] or public["ean"].lower() in {"nan", "none", "null", "<na>"}:
        problem = "EAN mancante"
    elif not public["sku"] or len(public["sku"]) > (40 if marketplace == "worten" else 100):
        problem = "SKU mancante o troppo lungo"
    elif minimum <= 0 or price <= 0 or minimum > price:
        problem = "Il prezzo minimo deve essere positivo e non superiore alla vendita"
    if marketplace == "kaufland":
        listing, floor = (int(round(v * rules.multiplier * 100)) for v in (price, minimum))
        maximum = {"cz": 2500000000, "pl": 450000000}.get(rules.storefront, 100000000)
        if min(listing, floor) < 1 or max(listing, floor) > maximum:
            problem = "Prezzi fuori dai limiti del marketplace"
        payload.update(
            ean=public["ean"],
            id_offer=public["sku"],
            amount=public["quantity"],
            listing_price=listing,
            minimum_price=floor,
        )
    else:
        payload.update(
            {
                "sku": public["sku"],
                "product-id": public["ean"],
                "description": public["name"],
                "internal-description": public["name"],
                "description-pt": public["name"],
                "quantity": str(public["quantity"]),
                "price": public["price"],
                "price[channel=WRT_PT_ONLINE]": public["price"],
            }
        )
    match = public.get("innpro_match")
    if match:
        if "quantity" in changes:
            public["innpro_match"] = {**match, "light": "manual"}
        elif match.get("light") not in {"matched", "manual"}:
            problem = "Stock LIGHT non verificato: controlla il match EAN"
    public["problem"] = problem
    return public, payload
