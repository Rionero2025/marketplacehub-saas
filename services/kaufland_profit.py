from __future__ import annotations

from collections.abc import Mapping
import math


COUNTRY_COST_COLUMNS={
    "pt":"cost_pt","it":"cost_it","fr":"cost_fr","de":"cost_de",
    "at":"cost_zone3","be":"cost_zone3","lu":"cost_zone3","nl":"cost_zone3",
    "gb":"cost_zone3","sk":"cost_zone4","si":"cost_zone4","pl":"cost_zone4",
    "cz":"cost_zone4","bg":"cost_zone4","hr":"cost_zone4","ee":"cost_zone4",
    "gr":"cost_zone4","hu":"cost_zone4","lv":"cost_zone4","lt":"cost_zone4",
    "ro":"cost_zone4","dk":"cost_zone5","fi":"cost_zone5","ie":"cost_zone5",
    "se":"cost_zone5","ch":"cost_zone6","no":"cost_zone6","sm":"cost_zone6",
}


def _number(value,default=0.0) -> float:
    try:
        number=float(str(value).strip().replace(",","."))
        return number if math.isfinite(number) else float(default)
    except (TypeError,ValueError):
        return float(default)


def product_costs(product: Mapping,storefront: str) -> dict[str,float]:
    """Rebuild the exact destination cost used by Kaufland publication."""
    source_purchase=max(0.0,_number(product.get("cost")))
    country_column=COUNTRY_COST_COLUMNS.get(str(storefront or "").lower())
    national=max(0.0,_number(product.get(country_column))) if country_column else 0.0
    purchase=national if national>0 else source_purchase
    total_from_view=max(0.0,_number(product.get("total_cost")))
    shipping_from_view=max(0.0,_number(product.get("shipping_cost")))
    # Saved views calculate total_cost from the reference-country purchase
    # price. Publication then retains only that shipping portion and replaces
    # the purchase price with the destination-country Cecotec value.
    shipping=max(0.0,total_from_view-source_purchase) if total_from_view>0 else shipping_from_view
    return {
        "purchase_cost_eur":round(purchase,2),
        "shipping_cost_eur":round(shipping,2),
        "total_cost_eur":round(purchase+shipping,2),
    }


def price_financials(
    *,
    sales_price,
    customer_shipping=0.0,
    total_cost_eur,
    commission_pct,
    commission_fixed_eur=0.0,
    currency="EUR",
    eur_multiplier=1.0,
    low_margin_threshold=10.0,
) -> dict:
    """Evaluate one proposed storefront price against the supplier total cost."""
    price=_number(sales_price)
    shipping=max(0.0,_number(customer_shipping))
    cost=max(0.0,_number(total_cost_eur))
    commission=max(0.0,_number(commission_pct))
    fixed_commission=max(0.0,_number(commission_fixed_eur))
    multiplier=_number(eur_multiplier,1.0)
    if multiplier<=0:multiplier=1.0
    currency=str(currency or "EUR").upper()
    if price<=0 or cost<=0:
        return {
            "sales_price":price or None,
            "sales_price_eur":None,
            "customer_shipping":round(shipping,2),
            "customer_total":None,
            "customer_total_eur":None,
            "commission_eur":None,
            "commission_fixed_eur":round(fixed_commission,2),
            "profit_eur":None,
            "profit_pct":None,
            "profit_status":"Non calcolabile",
            "margin_alert":"red",
            "can_update":False,
            "requires_confirmation":False,
        }
    customer_total=price+shipping
    price_eur=price/multiplier if currency!="EUR" else price
    customer_total_eur=(
        customer_total/multiplier if currency!="EUR" else customer_total
    )
    # Kaufland calculates the variable sales commission on the gross amount
    # paid by the customer, including the marketplace shipping charge.
    commission_eur=customer_total_eur*commission/100+fixed_commission
    profit=customer_total_eur-commission_eur-cost
    percentage=profit/cost*100
    displayed_profit=round(profit,2)
    displayed_percentage=round(percentage,2)
    if displayed_profit<0:
        status="Perdita";alert="red";can_update=False;confirmation=False
    elif displayed_percentage<float(low_margin_threshold):
        status="Margine basso";alert="yellow";can_update=True;confirmation=True
    else:
        status="Margine adeguato";alert="green";can_update=True;confirmation=False
    return {
        "sales_price":round(price,2),
        "sales_price_eur":round(price_eur,2),
        "customer_shipping":round(shipping,2),
        "customer_total":round(customer_total,2),
        "customer_total_eur":round(customer_total_eur,2),
        "commission_eur":round(commission_eur,2),
        "commission_fixed_eur":round(fixed_commission,2),
        "profit_eur":displayed_profit,
        "profit_pct":displayed_percentage,
        "profit_status":status,
        "margin_alert":alert,
        "can_update":can_update,
        "requires_confirmation":confirmation,
    }



def minimum_price_candidate_status(
    *,
    target_price,
    listing_price,
    profit_eur,
    margin_alert,
) -> dict[str,object]:
    """Classify one Smart Pricing minimum-price candidate for batch updates.

    A target above the unchanged listing price is not missing data: it is a
    complete row that cannot be applied while the action is limited to the
    minimum price. Keeping this case separate prevents misleading counters.
    """
    target=round(_number(target_price),2)
    listing=round(_number(listing_price),2)
    if target<=0:
        return {
            "category":"invalid_target",
            "actionable":False,
            "reason":"Prezzo obiettivo Buy Box assente o non positivo",
        }
    if profit_eur is None:
        return {
            "category":"missing_economics",
            "actionable":False,
            "reason":"Costo totale o dati economici mancanti",
        }
    if listing>0 and target>listing:
        return {
            "category":"above_listing",
            "actionable":False,
            "reason":(
                f"Obiettivo {target:.2f} superiore al prezzo principale "
                f"invariato {listing:.2f}"
            ),
        }
    profit=_number(profit_eur)
    if profit<0:
        return {
            "category":"loss",
            "actionable":True,
            "reason":"Allineamento in perdita",
        }
    if str(margin_alert or "").lower()=="yellow":
        return {
            "category":"low_margin",
            "actionable":True,
            "reason":"Margine inferiore al 10%",
        }
    return {
        "category":"safe",
        "actionable":True,
        "reason":"Margine almeno del 10%",
    }

def buybox_row_tone(status,profit_eur=None,profit_pct=None) -> str:
    """Return the visual tone used by the interactive Buy Box table."""
    status=str(status or "").strip()
    if status=="Vinta":
        return "green"
    if status=="Persa":
        return "red"
    try:
        profit=float(profit_eur)
    except (TypeError,ValueError):
        profit=None
    try:
        percentage=float(profit_pct)
    except (TypeError,ValueError):
        percentage=None
    if profit is not None and profit<0:
        return "red"
    if percentage is not None and percentage<10:
        return "yellow"
    if status in ("Oltre top 10","Nessuna Buy Box","Non classificata"):
        return "yellow"
    return "neutral"


def buybox_margin_tone(profit_eur=None,profit_pct=None) -> str:
    """Return the cell tone for the economic result of Buy Box alignment."""
    try:
        profit=float(profit_eur)
    except (TypeError,ValueError):
        return "neutral"
    try:
        percentage=float(profit_pct)
    except (TypeError,ValueError):
        return "neutral"
    if profit<0:
        return "red"
    if percentage<10:
        return "yellow"
    return "green"


def buybox_financials(
    *,
    total_cost_eur,
    commission_pct,
    commission_fixed_eur=0.0,
    currency,
    eur_multiplier,
    status,
    target_price=None,
    winner_price=None,
    winner_total=None,
    our_price=None,
    our_shipping=None,
) -> dict:
    """Calculate profit/loss at the sales price needed for the Buy Box.

    Kaufland's target price is a product sales price excluding shipping.
    When it is unavailable, the function estimates a competitive sales price
    from the winning customer total and our shipping charge.
    """
    cost=max(0.0,_number(total_cost_eur))
    commission=max(0.0,_number(commission_pct))
    multiplier=_number(eur_multiplier,1.0)
    if multiplier<=0:multiplier=1.0
    currency=str(currency or "EUR").upper()
    current_status=str(status or "")

    target=_number(target_price)
    source=""
    if current_status=="Vinta":
        target=_number(our_price) or _number(winner_price)
        source="Prezzo vincente attuale"
    elif target>0:
        source="Obiettivo Kaufland"
    else:
        winning_total=_number(winner_total)
        own_shipping=max(0.0,_number(our_shipping))
        if winning_total>0:
            target=max(0.0,winning_total-own_shipping-0.01)
            source="Stima dal totale vincente"
        elif _number(winner_price)>0:
            target=max(0.0,_number(winner_price)-0.01)
            source="Stima dal prezzo vincente"
    if target<=0 or cost<=0:
        return {
            "target_sales_price":target or None,
            "target_sales_price_eur":None,
            "target_source":source or "Non disponibile",
            "target_commission_eur":None,
            "profit_eur":None,
            "profit_pct":None,
            "profit_status":"Non calcolabile",
        }

    evaluated=price_financials(
        sales_price=target,total_cost_eur=cost,commission_pct=commission,
        customer_shipping=our_shipping,
        commission_fixed_eur=commission_fixed_eur,
        currency=currency,eur_multiplier=multiplier,
    )
    profit=evaluated["profit_eur"]
    percentage=evaluated["profit_pct"]
    if profit>0.004:profit_status="Guadagno"
    elif profit<-.004:profit_status="Perdita"
    else:profit_status="Pareggio"
    return {
        "target_sales_price":round(target,2),
        "target_sales_price_eur":evaluated["sales_price_eur"],
        "target_source":source,
        "target_commission_eur":evaluated["commission_eur"],
        "profit_eur":profit,
        "profit_pct":percentage,
        "profit_status":profit_status,
    }
