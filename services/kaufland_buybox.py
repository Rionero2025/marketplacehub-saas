from __future__ import annotations

from typing import Any


class OfferLookupError(ValueError):
    error_type="Errore identificazione Kaufland"
    status="Errore"


class OfferNotPublishedError(OfferLookupError):
    error_type="Offerta non presente nell'account Kaufland"
    status="Offerta non presente"


class ProductNotFoundError(OfferLookupError):
    error_type="Prodotto/EAN non presente nel catalogo Kaufland"
    status="Prodotto non trovato"


class ProductResolutionError(OfferLookupError):
    error_type="Risposta Kaufland incompleta"
    status="Errore"


def _data(payload: Any) -> Any:
    if isinstance(payload,dict) and "data" in payload:
        return payload["data"]
    return payload


def product_id_from_response(payload: Any) -> int:
    """Extract Kaufland's product ID from the EAN lookup response."""
    value=_data(payload)
    candidates=value if isinstance(value,list) else [value]
    for candidate in candidates:
        if not isinstance(candidate,dict):
            continue
        for key in ("id_product","id_item"):
            raw=candidate.get(key)
            if raw not in (None,""):
                return int(raw)
        product=candidate.get("product")
        if isinstance(product,dict):
            for key in ("id_product","id_item","id"):
                raw=product.get(key)
                if raw not in (None,""):
                    return int(raw)
    raise ValueError("Kaufland non ha restituito l'ID prodotto per questo EAN.")


def ean_lookup_candidates(ean: Any) -> list[str]:
    """Return safe UPC/EAN variants accepted by different Kaufland resources."""
    value=str(ean or "").strip().removesuffix(".0")
    if not value:
        return []
    candidates=[value]
    # Kaufland's own Buy Box notifications can expose the same GTIN both as a
    # 12-digit UPC and as its zero-prefixed 13-digit EAN representation.
    if value.isdigit() and len(value)==12:
        candidates.append("0"+value)
    elif value.isdigit() and len(value)==13 and value.startswith("0"):
        candidates.append(value[1:])
    return list(dict.fromkeys(candidates))


def minimum_price_from_composed_sku(sku: Any,ean: Any) -> float | None:
    """Read the saved EUR minimum only from this app's exact composite SKU."""
    parts=str(sku or "").strip().rsplit("_",3)
    expected_ean=str(ean or "").strip().removesuffix(".0")
    if len(parts)!=4 or not parts[0] or parts[1]!=expected_ean:
        return None
    purchase=_number(parts[2])
    minimum=_number(parts[3])
    if purchase is None or purchase<0 or minimum is None or minimum<=0:
        return None
    return round(float(minimum),2)


def commission_rates_from_response(payload: Any) -> dict[str,dict]:
    """Normalize the bulk commission lookup response, preserving every status."""
    value=_data(payload)
    items=value if isinstance(value,list) else []
    normalized={}
    for item in items:
        if not isinstance(item,dict):
            continue
        ean=str(item.get("ean") or "").strip()
        if not ean:
            continue
        estimate=(
            item.get("commission_rate_estimate")
            if isinstance(item.get("commission_rate_estimate"),dict) else {}
        )
        normalized[ean]={
            "status":str(item.get("status") or "").strip().upper(),
            "variable_fee":_number(estimate.get("variable_fee")),
            "fixed_fee_minor":_number(estimate.get("fixed_fee")),
        }
    return normalized


def resolve_offer_product(
    client: Any,
    id_offer: str,
    ean: str,
    storefront: str,
    cached_id: int | None=None,
) -> dict:
    """Resolve a published offer through its private unit before public EAN lookup.

    The unit endpoint is authoritative for an offer already published by the
    account and avoids false failures when the public product-by-EAN endpoint
    does not return the item.  EAN lookup remains a compatibility fallback.
    """
    unit_payload=[]
    unit_error=""
    unit_request_succeeded=False
    try:
        unit_payload=client.units(id_offer,storefront,embedded="seller")
        unit_request_succeeded=True
    except Exception as error:
        unit_error=str(error)

    if unit_request_succeeded and not unit_payload:
        lookup_errors=[]
        product_found=False
        for candidate in ean_lookup_candidates(ean):
            try:
                product_id_from_response(client.product_by_ean(candidate,storefront))
                product_found=True
                break
            except Exception as error:
                lookup_errors.append(str(error))
        if product_found:
            raise OfferNotPublishedError(
                f"Lo SKU {id_offer} non è presente tra le offerte dell'account "
                f"Kaufland {str(storefront).upper()}, anche se il prodotto esiste "
                "nel catalogo."
            )
        not_found=bool(lookup_errors) and all(
            "HTTP 404" in message
            or "non ha restituito l'ID prodotto" in message
            or "non trovato" in message.lower()
            for message in lookup_errors
        )
        if not_found:
            raise ProductNotFoundError(
                f"Lo SKU {id_offer} non è presente tra le offerte dell'account e "
                f"l'EAN {ean} non è stato trovato nel catalogo Kaufland "
                f"{str(storefront).upper()}."
            )
        raise OfferNotPublishedError(
            f"Lo SKU {id_offer} non è presente tra le offerte dell'account "
            f"Kaufland {str(storefront).upper()}."
        )

    if cached_id not in (None,""):
        return {
            "id_product":int(cached_id),"units":unit_payload,
            "lookup":None,"lookup_ean":"","unit_error":unit_error,
        }
    try:
        id_product=product_id_from_response(unit_payload)
        return {
            "id_product":id_product,"units":unit_payload,
            "lookup":None,"lookup_ean":"","unit_error":unit_error,
        }
    except (TypeError,ValueError):
        pass

    lookup_errors=[]
    for candidate in ean_lookup_candidates(ean):
        try:
            lookup_payload=client.product_by_ean(candidate,storefront)
            return {
                "id_product":product_id_from_response(lookup_payload),
                "units":unit_payload,"lookup":lookup_payload,
                "lookup_ean":candidate,"unit_error":unit_error,
            }
        except Exception as error:
            lookup_errors.append(f"{candidate}: {error}")
    details=[]
    if unit_error:
        details.append(f"unità: {unit_error}")
    if lookup_errors:
        details.append("EAN: "+" | ".join(lookup_errors))
    suffix=(" Dettagli: "+"; ".join(details)) if details else ""
    raise ProductResolutionError(
        f"Impossibile ricavare l'ID prodotto Kaufland per lo SKU {id_offer}.{suffix}"
    )


def _number(value: Any) -> float | None:
    if value in (None,""):
        return None
    if isinstance(value,bool):
        return None
    try:
        return float(str(value).strip().replace(",","."))
    except (TypeError,ValueError):
        return None


def _money(value: Any,minor_units: bool=True) -> tuple[float | None,str]:
    currency=""
    if isinstance(value,dict):
        currency=str(value.get("currency") or value.get("currency_code") or value.get("iso") or "")
        raw=value.get("amount")
        if raw is None:raw=value.get("value")
        if raw is None:raw=value.get("price")
        number=_number(raw)
        # Monetary objects returned by Kaufland use the smallest currency unit.
        return ((number/100.0 if number is not None and minor_units else number),currency)
    number=_number(value)
    return ((number/100.0 if number is not None and minor_units else number),currency)


def _first_money(source: dict,keys: tuple[str,...],minor_units: bool=True) -> tuple[float | None,str]:
    for key in keys:
        if key in source and source.get(key) not in (None,""):
            return _money(source.get(key),minor_units)
    return None,""


def _rank(offer: dict) -> int | None:
    for key in ("rank","buybox_rank","position"):
        number=_number(offer.get(key))
        if number is not None:
            return int(number)
    return None


def _seller_name(offer: dict) -> str:
    seller=offer.get("seller")
    if isinstance(seller,dict):
        return str(seller.get("pseudonym") or seller.get("name") or seller.get("display_name") or "")
    return str(offer.get("seller_pseudonym") or offer.get("seller_name") or seller or "")


def seller_pseudonyms_from_units(units: Any) -> set[str]:
    """Return seller display names embedded in the account's own units."""
    found:set[str]=set()
    queue=list(units) if isinstance(units,list) else [units]
    while queue:
        value=queue.pop(0)
        if isinstance(value,list):
            queue.extend(value)
        elif isinstance(value,dict):
            if value.get("pseudonym") not in (None,""):
                found.add(str(value["pseudonym"]).strip())
            for key in ("seller","embedded","data"):
                nested=value.get(key)
                if isinstance(nested,(dict,list)):
                    queue.append(nested)
    return {value for value in found if value}


def unit_id_from_units(units: Any,expected_offer_id: str="") -> int | None:
    """Return the safest private unit ID for one of the account's own offers."""
    expected=str(expected_offer_id or "").strip()
    candidates=[]
    queue=list(units) if isinstance(units,list) else [units]
    while queue:
        value=queue.pop(0)
        if isinstance(value,list):
            queue.extend(value)
            continue
        if not isinstance(value,dict):
            continue
        raw_id=value.get("id_unit")
        if raw_id not in (None,""):
            offer_id=str(
                value.get("id_offer") or value.get("offer_id")
                or value.get("sku") or ""
            ).strip()
            condition=str(value.get("condition") or "").strip().upper()
            score=0
            if expected and offer_id==expected:score+=4
            if condition in ("NEW","100"):score+=2
            candidates.append((score,int(raw_id)))
        for key in ("data","embedded","unit"):
            nested=value.get(key)
            if isinstance(nested,(dict,list)):
                queue.append(nested)
    if not candidates:
        return None
    candidates.sort(key=lambda item:item[0],reverse=True)
    best_score=candidates[0][0]
    best_ids=list(dict.fromkeys(
        id_unit for score,id_unit in candidates if score==best_score
    ))
    return best_ids[0] if len(best_ids)==1 else None


def minimum_price_from_units(
    units: Any,expected_offer_id: str="",expected_unit_id: int | None=None,
) -> float | None:
    """Extract the current Smart Pricing minimum from the safest private unit."""
    expected=str(expected_offer_id or "").strip()
    candidates=[]
    queue=list(units) if isinstance(units,list) else [units]
    while queue:
        value=queue.pop(0)
        if isinstance(value,list):
            queue.extend(value)
            continue
        if not isinstance(value,dict):
            continue
        prices=value.get("prices") if isinstance(value.get("prices"),dict) else {}
        raw=(
            prices.get("minimum_price")
            if prices.get("minimum_price") not in (None,"")
            else value.get("minimum_price")
        )
        minimum,_currency=_money(raw)
        if minimum is not None:
            unit_id=value.get("id_unit")
            offer_id=str(
                value.get("id_offer") or value.get("offer_id")
                or value.get("sku") or ""
            ).strip()
            condition=str(value.get("condition") or "").strip().upper()
            score=0
            if expected_unit_id not in (None,"") and str(unit_id)==str(expected_unit_id):
                score+=8
            if expected and offer_id==expected:
                score+=4
            if condition in ("NEW","100"):
                score+=2
            candidates.append((score,minimum))
        for key in ("data","embedded","unit"):
            nested=value.get(key)
            if isinstance(nested,(dict,list)):
                queue.append(nested)
    if not candidates:
        return None
    candidates.sort(key=lambda item:item[0],reverse=True)
    return round(float(candidates[0][1]),2)


def unit_logistics_from_units(
    units: Any,expected_offer_id: str="",expected_unit_id: int | None=None,
) -> dict:
    """Extract handling and transport times from the safest private unit."""
    expected=str(expected_offer_id or "").strip()
    candidates=[]
    queue=list(units) if isinstance(units,list) else [units]
    while queue:
        value=queue.pop(0)
        if isinstance(value,list):
            queue.extend(value)
            continue
        if not isinstance(value,dict):
            continue
        has_logistics=any(value.get(key) not in (None,"") for key in (
            "handling_time","transport_time_min","transport_time_max",
            "delivery_time_min","delivery_time_max",
        ))
        if has_logistics:
            unit_id=value.get("id_unit")
            offer_id=str(
                value.get("id_offer") or value.get("offer_id")
                or value.get("sku") or ""
            ).strip()
            condition=str(value.get("condition") or "").strip().upper()
            score=0
            if expected_unit_id not in (None,"") and str(unit_id)==str(expected_unit_id):
                score+=8
            if expected and offer_id==expected:
                score+=4
            if condition in ("NEW","100"):
                score+=2
            candidates.append((score,{
                "handling_time":_integer(value.get("handling_time")),
                "transport_time_min":_integer(value.get("transport_time_min")),
                "transport_time_max":_integer(value.get("transport_time_max")),
                "delivery_time_min":_integer(value.get("delivery_time_min")),
                "delivery_time_max":_integer(value.get("delivery_time_max")),
            }))
        for key in ("data","embedded","unit"):
            nested=value.get(key)
            if isinstance(nested,(dict,list)):
                queue.append(nested)
    if not candidates:
        return {
            "handling_time":None,"transport_time_min":None,
            "transport_time_max":None,"delivery_time_min":None,
            "delivery_time_max":None,
        }
    candidates.sort(key=lambda item:item[0],reverse=True)
    return candidates[0][1]


def _offer_prices(offer: dict) -> dict:
    prices=offer.get("prices") if isinstance(offer.get("prices"),dict) else {}
    sale,currency=_first_money(
        prices,
        ("sales_price","listing_price","price"),
    )
    if sale is None:
        sale,currency=_first_money(
            offer,
            ("sales_price","listing_price","price"),
        )
    shipping,shipping_currency=_first_money(
        prices,
        ("shipping_cost","shipping_price","shipping_rate","shipping"),
    )
    if shipping is None:
        shipping,shipping_currency=_first_money(
            offer,
            ("shipping_cost","shipping_price","shipping_rate","shipping"),
        )
    total,total_currency=_first_money(
        prices,
        ("total_price","total"),
    )
    if total is None:
        total,total_currency=_first_money(
            offer,
            ("total_price","total"),
        )
    if total is None and sale is not None:
        total=sale+(shipping or 0.0)
    return {
        "price":sale,
        "shipping":shipping,
        "total":total,
        "currency":currency or shipping_currency or total_currency,
    }


def own_prices_from_units(units: Any) -> dict:
    """Extract this account's current price and shipping from a units response."""
    candidates:list[tuple[int,dict]]=[]
    queue=list(units) if isinstance(units,list) else [units]
    while queue:
        value=queue.pop(0)
        if isinstance(value,list):
            queue.extend(value)
            continue
        if not isinstance(value,dict):
            continue
        score=0
        if any(value.get(key) not in (None,"") for key in ("id_offer","offer_id","id_unit","sku")):
            score+=4
        if isinstance(value.get("prices"),dict):
            score+=3
        if any(value.get(key) not in (None,"") for key in (
            "sales_price","listing_price","price","shipping_cost","shipping_rate"
        )):
            score+=2
        parsed=_offer_prices(value)
        if parsed["price"] is not None or parsed["shipping"] is not None:
            candidates.append((score,parsed))
        for nested in value.values():
            if isinstance(nested,(dict,list)):
                queue.append(nested)
    if not candidates:
        return {"price":None,"shipping":None,"total":None,"currency":""}
    candidates.sort(key=lambda item:item[0],reverse=True)
    return candidates[0][1]


def _offers_from_payload(value: Any) -> tuple[list[dict],dict]:
    data=_data(value)
    if isinstance(data,list):
        return [item for item in data if isinstance(item,dict)],{}
    if not isinstance(data,dict):
        return [],{}
    for key in ("offers","offers_rankings","offer_rankings","rankings","units","items"):
        collection=data.get(key)
        if isinstance(collection,list):
            return [item for item in collection if isinstance(item,dict)],data
    # Some versions return one ranking record directly.
    if any(key in data for key in ("rank","buybox_rank","seller","seller_pseudonym")):
        return [data],data
    return [],data


def _same_offer(offer: dict,expected_offer_id: str) -> bool:
    expected=str(expected_offer_id or "").strip()
    if not expected:
        return False
    return any(str(offer.get(key) or "").strip()==expected
               for key in ("id_offer","offer_id","sku","idOffer"))


def _integer(value: Any) -> int | None:
    number=_number(value)
    return int(number) if number is not None else None


def _delivery_times(offer: dict) -> tuple[int | None,int | None]:
    delivery=offer.get("delivery_time")
    delivery=delivery if isinstance(delivery,dict) else {}
    minimum=_number(offer.get("delivery_time_min"))
    maximum=_number(offer.get("delivery_time_max"))
    if minimum is None:
        minimum=_number(delivery.get("min"))
    if maximum is None:
        maximum=_number(delivery.get("max"))
    return (
        int(minimum) if minimum is not None else None,
        int(maximum) if maximum is not None else None,
    )


def buybox_logistics_analysis(
    *,
    status,
    winner_price=None,
    winner_total=None,
    our_price=None,
    our_total=None,
    winner_delivery_min=None,
    winner_delivery_max=None,
    our_delivery_min=None,
    our_delivery_max=None,
) -> dict:
    """Explain a lost Buy Box when the seller is already price-competitive."""
    lost=str(status or "")=="Persa"
    winner_sale=_number(winner_price)
    winner_customer_total=_number(winner_total)
    own_sale=_number(our_price)
    own_customer_total=_number(our_total)
    product_price_leader=(
        lost and own_sale is not None and winner_sale is not None
        and own_sale<=winner_sale+.005
    )
    total_price_leader=(
        lost and own_customer_total is not None and winner_customer_total is not None
        and own_customer_total<=winner_customer_total+.005
    )
    own_min=_integer(our_delivery_min)
    own_max=_integer(our_delivery_max)
    winner_min=_integer(winner_delivery_min)
    winner_max=_integer(winner_delivery_max)
    slower_delivery=bool(
        lost and (
            (own_max is not None and winner_max is not None and own_max>winner_max)
            or (own_min is not None and winner_min is not None and own_min>winner_min)
        )
    )
    if not (product_price_leader or total_price_leader):
        message=""
    elif slower_delivery:
        message=(
            "Prezzo competitivo ma Buy Box persa: la nostra consegna stimata è "
            "più lenta di quella del vincitore."
        )
    elif product_price_leader and not total_price_leader:
        message=(
            "Prezzo prodotto più basso ma Buy Box persa: il totale pagato dal "
            "cliente è più alto, probabilmente per la spedizione."
        )
    else:
        message=(
            "Prezzo totale più basso ma Buy Box persa: verificare giorni di "
            "gestione, gruppo di spedizione e altri indicatori di performance."
        )
    return {
        "product_price_leader":product_price_leader,
        "total_price_leader":total_price_leader,
        "slower_delivery":slower_delivery,
        "message":message,
    }


def parse_buybox_response(
    payload: Any,
    expected_offer_id: str="",
    own_seller_pseudonyms: Any=None,
) -> dict:
    """Normalize current Kaufland Buy Box responses and push-like fixtures.

    Competitor identifiers are intentionally hidden by Kaufland.  Our own
    ranking is therefore found first by exact offer ID, then through the
    explicit ``seller_offer`` record, and finally by the offer whose private
    identifiers are exposed.
    """
    offers,container=_offers_from_payload(payload)
    winner=container.get("winner_offer") if isinstance(container.get("winner_offer"),dict) else None
    seller_offer=container.get("seller_offer") if isinstance(container.get("seller_offer"),dict) else None

    if winner is None:
        winner=next((offer for offer in offers if _rank(offer)==1),offers[0] if offers else None)
    # ``seller_offer`` is the authoritative private record and contains fields
    # such as id_unit/id_offer that can be omitted from the ranked offers list.
    own=seller_offer or next(
        (offer for offer in offers if _same_offer(offer,expected_offer_id)),None
    )
    if own is None:
        own=next((offer for offer in offers
                  if offer.get("id_offer") not in (None,"") or offer.get("id_unit") not in (None,"")),None)
    if isinstance(own_seller_pseudonyms,str):
        own_names={own_seller_pseudonyms.strip().casefold()}
    else:
        own_names={str(value).strip().casefold() for value in (own_seller_pseudonyms or []) if str(value).strip()}
    if own is None and own_names:
        own=next((offer for offer in offers
                  if _seller_name(offer).strip().casefold() in own_names),None)

    winner_prices=_offer_prices(winner or {})
    own_prices=_offer_prices(own or {})
    own_rank=_rank(own or {})
    if own_rank==1:
        status="Vinta"
    elif own_rank is not None:
        status="Persa"
    elif winner:
        status="Non classificata"
    else:
        status="Nessuna Buy Box"

    target_value=container.get("target_price")
    if target_value in (None,"") and own:
        target_value=own.get("target_price")
    if target_value in (None,"") and own and isinstance(own.get("prices"),dict):
        target_value=own["prices"].get("target_price")
    if target_value in (None,"") and seller_offer:
        target_value=seller_offer.get("target_price")
    if target_value in (None,"") and seller_offer and isinstance(seller_offer.get("prices"),dict):
        target_value=seller_offer["prices"].get("target_price")
    target_price,target_currency=_money(target_value)
    own_id_unit=None
    if own and own.get("id_unit") not in (None,""):
        try:
            own_id_unit=int(own["id_unit"])
        except (TypeError,ValueError):
            own_id_unit=None
    delivery_min,delivery_max=_delivery_times(winner or {})
    own_delivery_min,own_delivery_max=_delivery_times(own or {})
    return {
        "status":status,
        "our_rank":own_rank,
        "winner_seller":_seller_name(winner or {}),
        "winner_price":winner_prices["price"],
        "winner_shipping":winner_prices["shipping"],
        "winner_total":winner_prices["total"],
        "our_price":own_prices["price"],
        "our_shipping":own_prices["shipping"],
        "our_total":own_prices["total"],
        "id_unit":own_id_unit,
        "target_price":target_price,
        "currency":winner_prices["currency"] or own_prices["currency"] or target_currency,
        "delivery_min":delivery_min,
        "delivery_max":delivery_max,
        "own_delivery_min":own_delivery_min,
        "own_delivery_max":own_delivery_max,
        "offer_count":len(offers),
    }
