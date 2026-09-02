from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence
import re


WAITING_FILE_STATUSES = {
    "WAITING LABEL",
    "CREATED",
}

# SENT TO WAREHOUSE is not equivalent to WAITING LABEL. If tracking and carrier
# already exist, the shipment can be transmitted to a marketplace order still
# in SHIPPING state.
WAREHOUSE_READY_STATUSES = {
    "SENT TO WAREHOUSE",
}

SHIPPED_FILE_STATUSES = {
    "IN TRANSIT",
    "IN TRANSIT/INCIDENCE",
    "OUT FOR DELIVERY",
    "DELIVERED",
    "DELIVERED TO AGENCY",
    "DELIVERED TO PICKUP POINT",
    "DELIVERED_TO_PICKUP_POINT",
}

CANCELLED_FILE_STATUSES = {
    "CANCELLED",
    "CANCELED",
    "CANCELLED BY BUYER",
    "CANCELLED BY SELLER",
}

WORTEN_SHIPPABLE_MARKETPLACE_STATUSES = {"SHIPPING"}
WORTEN_ALREADY_SHIPPED_STATUSES = {"SHIPPED", "TO COLLECT", "TO_COLLECT", "RECEIVED", "CLOSED"}
WORTEN_BLOCKED_STATUSES = {
    "REFUNDED",
    "REFUSED",
    "CANCELLED",
    "CANCELED",
    "RETURNED",
}


@dataclass(frozen=True)
class ShipmentEligibility:
    allowed: bool
    operational_status: str
    reason: str
    already_shipped: bool = False


def normalize_status(value: object) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def canonical_marketplace_status(value: object) -> str:
    """Extract the actual marketplace state from UI labels and API values.

    The UI may display values such as ``RECEIVED · già spedito``.  Business
    rules must use the raw state (``RECEIVED``), not the translated suffix.
    """
    text = normalize_status(value)
    if not text:
        return ""
    # The first segment is the raw Mirakl state in all labels produced by the UI.
    first = re.split(r"\s*[·|]\s*", text, maxsplit=1)[0].strip()
    aliases = {
        "IN ATTESA DI SPEDIZIONE": "SHIPPING",
        "DA SPEDIRE": "SHIPPING",
        "SPEDITO": "SHIPPED",
        "SPEDITA": "SHIPPED",
        "RICEVUTO": "RECEIVED",
        "RICEVUTA": "RECEIVED",
        "RIMBORSATO": "REFUNDED",
        "RIMBORSATA": "REFUNDED",
        "ANNULLATO": "CANCELLED",
        "ANNULLATA": "CANCELLED",
    }
    return aliases.get(first, first)


def canonical_file_status(value: object) -> str:
    """Extract a Cecotec/carrier state from raw values or UI labels.

    The order table intentionally displays friendly labels such as
    ``Spedita · tracking disponibile``.  Re-validating the same row before the
    API call must not reject that label.  Raw statuses are still preferred and
    remain authoritative whenever available.
    """
    text = normalize_status(value)
    if not text:
        return ""
    first = re.split(r"\s*[·|]\s*", text, maxsplit=1)[0].strip()
    aliases = {
        "SPEDITA": "IN TRANSIT",
        "SPEDITO": "IN TRANSIT",
        "TRACKING DISPONIBILE": "IN TRANSIT",
        "PRONTA PER L INVIO": "IN TRANSIT",
        "PRONTA PER L'INVIO": "IN TRANSIT",
        "IN ATTESA DI SPEDIZIONE": "WAITING LABEL",
        "ANNULLATA NEL FILE": "CANCELLED",
        "ANNULLATO NEL FILE": "CANCELLED",
    }
    return aliases.get(first, first)


def split_tracking_numbers(value: object) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    parts = re.split(r"\s*(?:/|\||,|;)\s*", text)
    return [part for part in parts if part]


def evaluate_worten_shipment(
    *,
    marketplace_status: object,
    file_status: object,
    tracking: object,
    carrier: object,
    supplier: object = "",
    distinct_tracking_numbers: Sequence[str] | None = None,
) -> ShipmentEligibility:
    """Decide whether a Worten/Mirakl order can be marked as shipped.

    Correct business rule:
    - SHIPPING means the order is waiting for seller shipment and is therefore
      the state accepted by Mirakl OR24.
    - WAITING LABEL / CREATED remain blocked by default.
    - For Cecotec, WAITING LABEL / CREATED become ready when both tracking and
      carrier are already present in the supplier file. This is an explicit
      operational rule requested for Cecotec shipments.
    - SENT TO WAREHOUSE becomes ready when tracking and carrier are present.
    - IN TRANSIT and later carrier states are ready when tracking and carrier
      are present.
    - SHIPPED / RECEIVED / CLOSED must not be transmitted again.
    - refunded, refused, cancelled or returned orders are blocked.
    """
    market = canonical_marketplace_status(marketplace_status)
    source = canonical_file_status(file_status)
    tracking_text = str(tracking or "").strip()
    carrier_text = str(carrier or "").strip()
    tracking_values = list(distinct_tracking_numbers or split_tracking_numbers(tracking_text))
    tracking_values = list(dict.fromkeys(item.strip() for item in tracking_values if item.strip()))
    supplier_text = str(supplier or "").strip().casefold()
    is_cecotec = supplier_text == "cecotec" or supplier_text.startswith("cecotec ")

    if market in WORTEN_ALREADY_SHIPPED_STATUSES:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Già spedito sul marketplace",
            reason=f"stato marketplace {market}: ordine già spedito",
            already_shipped=True,
        )

    if market in WORTEN_BLOCKED_STATUSES:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Non spedibile",
            reason=f"stato marketplace {market}: ordine non spedibile",
        )

    if market not in WORTEN_SHIPPABLE_MARKETPLACE_STATUSES:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Non ancora spedibile",
            reason=f"stato marketplace {market or 'non disponibile'} non compatibile con l'invio",
        )

    if source in CANCELLED_FILE_STATUSES:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Annullata nel file",
            reason=f"stato file {source}: spedizione annullata",
        )

    if len(tracking_values) > 1:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Da verificare",
            reason="più tracking differenti sullo stesso ordine Worten",
        )

    if not tracking_text:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Tracking mancante",
            reason="numero di tracking non disponibile",
        )

    if not carrier_text:
        return ShipmentEligibility(
            allowed=False,
            operational_status="Corriere mancante",
            reason="corriere non disponibile",
        )

    if source in WAITING_FILE_STATUSES:
        if is_cecotec:
            return ShipmentEligibility(
                allowed=True,
                operational_status="Pronta per l'invio",
                reason=(
                    f"file Cecotec {source}; tracking e corriere già disponibili: "
                    "invio consentito"
                ),
            )
        return ShipmentEligibility(
            allowed=False,
            operational_status="In attesa di spedizione",
            reason=f"stato file {source}: attendere la creazione/assegnazione della spedizione",
        )

    if source in WAREHOUSE_READY_STATUSES | SHIPPED_FILE_STATUSES:
        return ShipmentEligibility(
            allowed=True,
            operational_status="Pronta per l'invio",
            reason=(
                f"marketplace {market}; file {source}; tracking e corriere disponibili"
            ),
        )

    return ShipmentEligibility(
        allowed=False,
        operational_status="Stato file da verificare",
        reason=f"stato file {source or 'non disponibile'} non riconosciuto come spedibile",
    )


def apply_worten_eligibility(row: Mapping[str, object]) -> dict[str, object]:
    """Return a copy of a UI row enriched with safe shipping fields."""
    result = dict(row)
    eligibility = evaluate_worten_shipment(
        marketplace_status=(
            row.get("marketplace_status")
            or row.get("Stato marketplace")
            or row.get("raw_marketplace_status")
        ),
        file_status=(
            row.get("file_status")
            or row.get("Stato file")
            or row.get("raw_file_status")
        ),
        tracking=row.get("tracking") or row.get("Tracking"),
        carrier=row.get("carrier") or row.get("Corriere"),
        supplier=row.get("supplier") or row.get("Fornitore"),
        distinct_tracking_numbers=row.get("tracking_numbers") if isinstance(row.get("tracking_numbers"), (list, tuple)) else None,
    )
    result["Invio consentito"] = "Sì" if eligibility.allowed else "No"
    result["Problemi"] = "" if eligibility.allowed else eligibility.reason
    result["Stato operativo"] = eligibility.operational_status
    result["already_shipped"] = eligibility.already_shipped
    result["api_allowed"] = eligibility.allowed
    return result


def eligible_rows(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return [item for item in (apply_worten_eligibility(row) for row in rows) if item["api_allowed"]]
