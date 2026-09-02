from __future__ import annotations

import json


def active_sent_offers(operation_rows: list[dict], environment: str | None = None) -> list[dict]:
    """Return the latest Kaufland offers that history does not mark as deleted.

    The exact ``sku_inviato`` is retained.  Rebuilding a composite SKU from the
    current price list would be unsafe because costs and commercial rules can
    change after publication. Playground and production histories are isolated.

    Legacy publications without environment metadata remain visible in both
    environments. Legacy deletions are treated as Playground operations because
    the old interface defaulted to Playground; this prevents a test deletion
    from hiding a real production offer.
    """
    requested_environment=str(environment or "").strip().lower()
    active: dict[tuple[str, str], dict] = {}
    ordered=sorted(operation_rows,key=lambda item:(str(item.get("created_at", "")),int(item.get("id", 0) or 0)))
    for operation in ordered:
        try:
            payload=json.loads(operation.get("details_json") or "{}")
        except (TypeError,ValueError,json.JSONDecodeError):
            continue
        detail_rows=payload.get("rows", []) if isinstance(payload,dict) else []
        operation_type=str(operation.get("operation_type", "")).upper()
        operation_environment=(
            str(payload.get("environment") or "").strip().lower()
            if isinstance(payload,dict) else ""
        )
        if requested_environment:
            if operation_environment and operation_environment!=requested_environment:
                continue
            if (not operation_environment and operation_type.startswith("ELIMINA")
                    and requested_environment=="live"):
                continue
        for detail in detail_rows:
            if not isinstance(detail,dict) or detail.get("ok") is not True:
                continue
            storefront=str(detail.get("paese") or operation.get("storefront") or "").strip().lower()
            sku=str(detail.get("sku_inviato") or "").strip()
            if not storefront or not sku:
                continue
            key=(storefront,sku)
            if operation_type=="CREA/AGGIORNA":
                active[key]={
                    "paese":storefront,
                    "sku_inviato":sku,
                    "ean":str(detail.get("ean") or "").strip(),
                    "sku_originale":str(detail.get("sku_originale") or "").strip(),
                    "saved_view_id":int(payload.get("saved_view_id",0) or 0),
                    "price_list_id":int(
                        operation.get("price_list_id", 0) or 0
                    ),
                    "valuta":str(detail.get("valuta") or "").strip().upper(),
                    "tasso_eur":float(detail.get("tasso_eur",1) or 1),
                    "created_at":str(operation.get("created_at") or ""),
                    "operation_id":int(operation.get("id", 0) or 0),
                }
            elif operation_type.startswith("ELIMINA"):
                active.pop(key,None)
    return sorted(active.values(),key=lambda item:(item["paese"],item["sku_inviato"]))
