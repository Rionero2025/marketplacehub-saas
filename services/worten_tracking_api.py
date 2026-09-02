from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import quote
import re

import requests

from services.tracking_shipping_rules import (
    canonical_marketplace_status,
    evaluate_worten_shipment,
    split_tracking_numbers,
)


@dataclass(frozen=True)
class VersandResult:
    order_id: str
    success: bool
    tracking_updated: bool
    shipment_validated: bool
    message: str


@dataclass(frozen=True)
class WortenCarrier:
    """Carrier values to send to Mirakl tracking APIs."""

    name: str
    code: str | None = None
    standard_code: str | None = None


class WortenApiError(RuntimeError):
    """HTTP error returned by the Worten/Mirakl API."""

    def __init__(self, status_code: int, path: str, body: str = "") -> None:
        self.status_code = int(status_code)
        self.path = path
        self.body = body
        super().__init__(
            f"Worten API {self.status_code} su {path}: {body[:1000]}".rstrip()
        )


_CARRIER_ALIASES: dict[str, WortenCarrier] = {
    # These values normalize the supplier file labels only.  A carrier code is
    # never guessed: the exact tenant-specific code is read from Mirakl SH21.
    "MRW": WortenCarrier("MRW"),
    "SEUR": WortenCarrier("SEUR"),
    "GLS": WortenCarrier("GLS"),
    "GLS NACIONAL": WortenCarrier("GLS"),
    "GLS NATIONAL": WortenCarrier("GLS"),
    "GLS ITALIA": WortenCarrier("GLS"),
    "DHL": WortenCarrier("DHL"),
    "DHL PAKET": WortenCarrier("DHL"),
    "DPD": WortenCarrier("DPD"),
    "DPD FRANCE": WortenCarrier("DPD"),
    "UPS": WortenCarrier("UPS"),
    "TIPSA": WortenCarrier("TIPSA"),
    "DSV": WortenCarrier("DSV"),
    "DSV ITALIA": WortenCarrier("DSV"),
    "DSV_ITALIA": WortenCarrier("DSV"),
    "ENVIALIA": WortenCarrier("ENVIALIA"),
    "ONTIME LOGISTICS": WortenCarrier("ONTIME LOGISTICS"),
    "RHENUS XL": WortenCarrier("RHENUS XL"),
}


def _carrier_from_api_item(item: Mapping[str, Any]) -> WortenCarrier:
    """Normalize one SH21 carrier without assuming fixed response key names."""

    # Only documented carrier-code fields are accepted.  Never treat a generic
    # object id as a carrier code: Mirakl rejects guessed codes with HTTP 400.
    code = str(
        item.get("code")
        or item.get("carrier_code")
        or ""
    ).strip()
    name = str(
        item.get("label")
        or item.get("name")
        or item.get("carrier_name")
        or code
        or ""
    ).strip()
    standard_code = str(
        item.get("standard_code")
        or item.get("carrier_standard_code")
        or item.get("standardCode")
        or ""
    ).strip() or None
    return WortenCarrier(name=name, code=code or None, standard_code=standard_code)


def _carrier_match_values(carrier: WortenCarrier) -> set[str]:
    return {
        value
        for value in (
            _normalized_carrier_key(carrier.name),
            _normalized_carrier_key(carrier.code),
            _normalized_carrier_key(carrier.standard_code),
        )
        if value
    }


def _invalid_carrier_code_error(exc: Exception) -> bool:
    if not isinstance(exc, WortenApiError) or exc.status_code != 400:
        return False
    body = f"{exc.body} {exc}".casefold()
    return (
        "carriercode" in body
        or "carrier_code" in body
        or ("carrier" in body and ("not found" in body or "invalid" in body))
    )


_FINAL_SHIPMENT_STATES = {
    "SHIPPED",
    "RECEIVED",
    "DELIVERED",
    "CANCELLED",
    "CANCELED",
    "REFUNDED",
    "CLOSED",
    "RETURNED",
}


def _normalize_mirakl_instance_url(value: object) -> str:
    """Return the Mirakl instance root without a trailing ``/api``."""

    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if url.casefold().endswith("/api"):
        url = url[:-4].rstrip("/")
    return url


def _normalized_carrier_key(value: object) -> str:
    text = str(value or "").strip().upper().replace("_", " ")
    return re.sub(r"\s+", " ", text)


def normalize_worten_carrier(value: object) -> WortenCarrier:
    """Return the safest carrier representation for Mirakl."""

    original = str(value or "").strip()
    key = _normalized_carrier_key(original)
    if not key:
        return WortenCarrier("")
    mapped = _CARRIER_ALIASES.get(key)
    if mapped:
        return mapped
    return WortenCarrier(original or key)


def _is_prevalidated(row: Mapping[str, object]) -> bool:
    if row.get("api_allowed") is True:
        return True
    value = str(row.get("Invio consentito") or "").strip().casefold()
    return value in {"sì", "si", "yes", "true", "1"}


def _response_json(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            return {}
        try:
            import json

            payload = json.loads(text)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}


def _shipment_id(item: Mapping[str, Any]) -> str:
    return str(
        item.get("id")
        or item.get("shipment_id")
        or item.get("shipmentId")
        or ""
    ).strip()


def _shipment_state(item: Mapping[str, Any]) -> str:
    raw = (
        item.get("shipment_state_code")
        or item.get("state_code")
        or item.get("status")
        or item.get("state")
        or ""
    )
    if isinstance(raw, Mapping):
        raw = raw.get("code") or raw.get("value") or raw.get("name") or ""
    return str(raw).strip().upper()


def _shipment_errors(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = (
        payload.get("shipment_errors")
        or payload.get("errors")
        or payload.get("deletion_errors")
        or []
    )
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _format_shipment_errors(errors: Iterable[Mapping[str, Any]]) -> str:
    messages: list[str] = []
    for item in errors:
        code = str(item.get("code") or item.get("error_code") or "").strip()
        message = str(
            item.get("message")
            or item.get("error_message")
            or item.get("description")
            or item
        ).strip()
        messages.append(f"{code}: {message}" if code else message)
    return "; ".join(messages)


class WortenTrackingClient:
    """Mirakl client supporting both legacy orders and multiple shipments.

    Newer Worten order pages expose a distinct ``Spedizione 1`` resource.  On
    those instances, tracking and shipping must use ST23/ST24 on
    ``/api/shipments`` instead of OR23/OR24 on ``/api/orders``.  The client
    detects the available shipment for the selected order and uses the correct
    workflow automatically; legacy order-level endpoints remain as fallback.
    """

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        shop_id: str | int | None = None,
        timeout: int = 45,
        session: requests.Session | None = None,
    ) -> None:
        if not api_url or not api_key:
            raise ValueError("API URL e API key Worten sono obbligatori")
        self.api_url = _normalize_mirakl_instance_url(api_url)
        self.api_key = api_key
        self.shop_id = str(shop_id).strip() if shop_id not in (None, "") else None
        self.timeout = timeout
        self.session = session or requests.Session()
        self._registered_carriers: list[WortenCarrier] | None = None
        self._carrier_lookup_error: str = ""

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MarketplaceHub/1.0 (Worten tracking)",
        }

    @property
    def params(self) -> dict[str, str]:
        return {"shop_id": self.shop_id} if self.shop_id else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        include_shop_id: bool = True,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        request_params: dict[str, Any] = {}
        if include_shop_id and self.shop_id:
            request_params["shop_id"] = self.shop_id
        if params:
            request_params.update(dict(params))
        response = self.session.request(
            method,
            f"{self.api_url}{path}",
            headers=self.headers,
            params=request_params,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code not in {200, 201, 202, 204}:
            body = str(getattr(response, "text", "") or "").strip()
            raise WortenApiError(response.status_code, path, body)
        return response

    def _request_with_optional_shop_retry(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[requests.Response, bool]:
        """Retry a 404 once without ``shop_id``.

        A stale or incorrect Shop ID can make a valid order invisible and Mirakl
        then returns 404.  Retrying without the parameter lets Mirakl use the
        default shop associated with the API key.
        """

        attempts = [True, False] if self.shop_id else [False]
        last_error: WortenApiError | None = None
        for include_shop_id in attempts:
            try:
                return (
                    self._request(
                        method,
                        path,
                        include_shop_id=include_shop_id,
                        params=params,
                        **kwargs,
                    ),
                    include_shop_id,
                )
            except WortenApiError as exc:
                last_error = exc
                if exc.status_code != 404 or not include_shop_id:
                    raise
        assert last_error is not None
        raise last_error

    def list_orders_by_ids(
        self, order_ids: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """OR11: read current order metadata for up to 100 IDs per request.

        The tracking page uses this lightweight refresh to obtain the exact
        ``shipping_deadline`` and the current marketplace state without
        downloading the complete order history again.
        """
        unique_ids = list(dict.fromkeys(
            str(value or "").strip() for value in order_ids if str(value or "").strip()
        ))
        found: dict[str, dict[str, Any]] = {}
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start:start + 100]
            response, _ = self._request_with_optional_shop_retry(
                "GET",
                "/api/orders",
                params={
                    "order_ids": ",".join(chunk),
                    "max": 100,
                    "offset": 0,
                },
            )
            payload = _response_json(response)
            raw_orders = payload.get("orders") or []
            if not isinstance(raw_orders, list):
                continue
            for raw in raw_orders:
                if not isinstance(raw, Mapping):
                    continue
                order_id = str(
                    raw.get("order_id")
                    or raw.get("commercial_id")
                    or raw.get("id")
                    or ""
                ).strip()
                if order_id:
                    found[order_id] = dict(raw)
        return found

    def list_registered_carriers(self, *, refresh: bool = False) -> list[WortenCarrier]:
        """SH21: read the exact carrier codes enabled on this Worten tenant.

        Mirakl recommends calling this resource at most once per day.  A client
        instance caches the response for the complete operation, so a batch of
        orders performs one lookup only.
        """

        if self._registered_carriers is not None and not refresh:
            return list(self._registered_carriers)
        try:
            response, _ = self._request_with_optional_shop_retry(
                "GET", "/api/shipping/carriers"
            )
            payload = _response_json(response)
            raw = payload.get("carriers") or payload.get("data") or []
            carriers = [
                _carrier_from_api_item(item)
                for item in raw
                if isinstance(item, Mapping)
            ]
            self._registered_carriers = [
                item for item in carriers if item.name or item.code
            ]
            self._carrier_lookup_error = ""
        except Exception as exc:
            # OR23/ST23 explicitly support unregistered carriers.  A temporary
            # SH21 problem must not block shipping; the payload will omit the
            # carrier_code and use carrier_name instead.
            self._registered_carriers = []
            self._carrier_lookup_error = str(exc)
        return list(self._registered_carriers)

    def resolve_carrier(
        self,
        carrier_name: str,
        *,
        candidate_code: str | None = None,
        candidate_standard_code: str | None = None,
    ) -> tuple[WortenCarrier, bool]:
        """Return the exact SH21 carrier or an unregistered-carrier fallback."""

        normalized = normalize_worten_carrier(carrier_name)
        wanted = {
            value
            for value in (
                _normalized_carrier_key(carrier_name),
                _normalized_carrier_key(normalized.name),
                _normalized_carrier_key(candidate_code),
                _normalized_carrier_key(candidate_standard_code),
            )
            if value
        }
        carriers = self.list_registered_carriers()
        exact: list[WortenCarrier] = []
        partial: list[WortenCarrier] = []
        for item in carriers:
            values = _carrier_match_values(item)
            if wanted & values:
                exact.append(item)
                continue
            # Accept a label such as "MRW Portugal" for the source name "MRW"
            # only when it identifies a single registered carrier.
            if any(
                source and target and (source in target or target in source)
                for source in wanted
                for target in values
            ):
                partial.append(item)
        if len(exact) == 1:
            return exact[0], True
        if len(exact) > 1:
            exact.sort(key=lambda item: (item.name.casefold(), item.code or ""))
            return exact[0], True
        if len(partial) == 1:
            return partial[0], True

        # Not registered on this tenant: Mirakl requires carrier_name and must
        # not receive a guessed carrier_code (the exact cause of the MRW 400).
        fallback_name = normalized.name or str(carrier_name or "").strip()
        return WortenCarrier(fallback_name), False

    def _tracking_values(
        self,
        *,
        tracking_number: str,
        carrier_name: str,
        carrier_code: str | None = None,
        carrier_standard_code: str | None = None,
        carrier_url: str | None = None,
        force_unregistered: bool = False,
    ) -> tuple[dict[str, str], WortenCarrier, bool]:
        tracking = tracking_number.strip()
        if not tracking:
            raise ValueError("Numero di tracking obbligatorio")
        if not str(carrier_name or "").strip():
            raise ValueError("Corriere obbligatorio")

        if force_unregistered:
            carrier = normalize_worten_carrier(carrier_name)
            registered = False
        else:
            carrier, registered = self.resolve_carrier(
                carrier_name,
                candidate_code=carrier_code,
                candidate_standard_code=carrier_standard_code,
            )
        name = carrier.name.strip() or str(carrier_name or "").strip()
        payload: dict[str, str] = {"tracking_number": tracking}
        if registered and carrier.code:
            # Registered carrier: SH21's exact code is authoritative.
            payload["carrier_code"] = carrier.code
            if name:
                payload["carrier_name"] = name
            if carrier.standard_code:
                payload["carrier_standard_code"] = carrier.standard_code
        else:
            # Unregistered carrier: OR23/ST23 require the name, while a guessed
            # code is rejected with "No carrier with code ... found".
            payload["carrier_name"] = name
            if carrier_url:
                payload["carrier_url"] = carrier_url.strip()
        return payload, carrier, registered

    # --- Legacy order-level OR23 / OR24 ---------------------------------
    def update_tracking(
        self,
        *,
        order_id: str,
        tracking_number: str,
        carrier_name: str,
        carrier_code: str | None = None,
        carrier_standard_code: str | None = None,
        carrier_url: str | None = None,
    ) -> None:
        payload, _, registered = self._tracking_values(
            tracking_number=tracking_number,
            carrier_name=carrier_name,
            carrier_code=carrier_code,
            carrier_standard_code=carrier_standard_code,
            carrier_url=carrier_url,
        )
        path = f"/api/orders/{quote(order_id, safe='')}/tracking"
        try:
            self._request_with_optional_shop_retry("PUT", path, json=payload)
        except WortenApiError as exc:
            if not registered or not _invalid_carrier_code_error(exc):
                raise
            # Carrier configuration can change after SH21 was cached.  Retry
            # once using Mirakl's documented unregistered-carrier payload.
            fallback, _, _ = self._tracking_values(
                tracking_number=tracking_number,
                carrier_name=carrier_name,
                carrier_url=carrier_url,
                force_unregistered=True,
            )
            self._request_with_optional_shop_retry("PUT", path, json=fallback)

    def validate_shipment(self, *, order_id: str) -> None:
        self._request_with_optional_shop_retry(
            "PUT",
            f"/api/orders/{quote(order_id, safe='')}/ship",
        )

    # --- Multiple-shipment ST11 / ST23 / ST24 ---------------------------
    def list_shipments_for_order(
        self,
        *,
        order_id: str,
    ) -> tuple[list[dict[str, Any]], bool] | None:
        """Return shipments and whether the successful request used shop_id.

        ``None`` means the multiple-shipment resource is not available on the
        current instance and the caller should fall back to legacy OR23/OR24.
        """

        attempts = [True, False] if self.shop_id else [False]
        for include_shop_id in attempts:
            try:
                response = self._request(
                    "GET",
                    "/api/shipments",
                    include_shop_id=include_shop_id,
                    params={"order_id": order_id, "limit": 100},
                )
            except WortenApiError as exc:
                if exc.status_code in {404, 405}:
                    continue
                raise
            payload = _response_json(response)
            raw = payload.get("data") or payload.get("shipments") or []
            shipments = [dict(item) for item in raw if isinstance(item, Mapping)]
            return shipments, include_shop_id
        return None

    def _select_shipment(
        self,
        *,
        order_id: str,
        shipments: Iterable[Mapping[str, Any]],
    ) -> tuple[str, str]:
        candidates: list[tuple[str, str]] = []
        all_shipments: list[tuple[str, str]] = []
        for item in shipments:
            item_order = str(item.get("order_id") or item.get("orderId") or "").strip()
            if item_order and item_order != order_id:
                continue
            shipment_id = _shipment_id(item)
            if not shipment_id:
                continue
            state = _shipment_state(item)
            all_shipments.append((shipment_id, state))
            if state not in _FINAL_SHIPMENT_STATES:
                candidates.append((shipment_id, state))

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError(
                "L'ordine contiene più spedizioni ancora aperte. Associa il tracking "
                "alla singola spedizione prima dell'invio."
            )
        if len(all_shipments) == 1 and not all_shipments[0][1]:
            return all_shipments[0]
        if all_shipments:
            states = ", ".join(state or "senza stato" for _, state in all_shipments)
            raise RuntimeError(
                f"La spedizione dell'ordine risulta già conclusa o non aggiornabile ({states})."
            )
        raise RuntimeError("Nessuna spedizione Mirakl trovata per l'ordine selezionato.")

    def update_multiple_shipment_tracking(
        self,
        *,
        shipment_id: str,
        tracking_number: str,
        carrier_name: str,
        include_shop_id: bool,
        carrier_code: str | None = None,
        carrier_standard_code: str | None = None,
        carrier_url: str | None = None,
    ) -> None:
        tracking_payload, _, registered = self._tracking_values(
            tracking_number=tracking_number,
            carrier_name=carrier_name,
            carrier_code=carrier_code,
            carrier_standard_code=carrier_standard_code,
            carrier_url=carrier_url,
        )

        def send(payload: dict[str, str]) -> requests.Response:
            payload = dict(payload)
            # ST23 calls the URL field ``tracking_url`` rather than ``carrier_url``.
            if "carrier_url" in payload:
                payload["tracking_url"] = payload.pop("carrier_url")
            return self._request(
                "POST",
                "/api/shipments/tracking",
                include_shop_id=include_shop_id,
                json={"shipments": [{"id": shipment_id, "tracking": payload}]},
            )

        try:
            response = send(tracking_payload)
        except WortenApiError as exc:
            if not registered or not _invalid_carrier_code_error(exc):
                raise
            fallback, _, _ = self._tracking_values(
                tracking_number=tracking_number,
                carrier_name=carrier_name,
                carrier_url=carrier_url,
                force_unregistered=True,
            )
            response = send(fallback)
        errors = _shipment_errors(_response_json(response))
        if errors:
            raise RuntimeError(
                "Worten non ha accettato il tracking della spedizione: "
                + _format_shipment_errors(errors)
            )

    def validate_multiple_shipment(
        self,
        *,
        shipment_id: str,
        include_shop_id: bool,
    ) -> None:
        response = self._request(
            "PUT",
            "/api/shipments/ship",
            include_shop_id=include_shop_id,
            json={"shipments": [{"id": shipment_id}]},
        )
        errors = _shipment_errors(_response_json(response))
        if errors:
            raise RuntimeError(
                "Worten non ha confermato la spedizione: "
                + _format_shipment_errors(errors)
            )

    def _ship_using_multiple_shipments(
        self,
        *,
        order_id: str,
        tracking_number: str,
        carrier_name: str,
        carrier_code: str | None,
        carrier_standard_code: str | None,
        carrier_url: str | None,
    ) -> VersandResult | None:
        discovered = self.list_shipments_for_order(order_id=order_id)
        if discovered is None:
            return None
        shipments, include_shop_id = discovered
        if not shipments:
            # Some legacy instances expose ST11 but do not create shipment
            # resources. In that case use OR23/OR24.
            return None
        shipment_id, _ = self._select_shipment(
            order_id=order_id,
            shipments=shipments,
        )
        tracking_done = False
        try:
            self.update_multiple_shipment_tracking(
                shipment_id=shipment_id,
                tracking_number=tracking_number,
                carrier_name=carrier_name,
                include_shop_id=include_shop_id,
                carrier_code=carrier_code,
                carrier_standard_code=carrier_standard_code,
                carrier_url=carrier_url,
            )
            tracking_done = True
            self.validate_multiple_shipment(
                shipment_id=shipment_id,
                include_shop_id=include_shop_id,
            )
            return VersandResult(
                order_id=order_id,
                success=True,
                tracking_updated=True,
                shipment_validated=True,
                message=(
                    f"tracking {tracking_number} e corriere {carrier_name} inviati alla "
                    f"spedizione {shipment_id}; ordine contrassegnato come spedito"
                ),
            )
        except Exception as exc:
            return VersandResult(
                order_id=order_id,
                success=False,
                tracking_updated=tracking_done,
                shipment_validated=False,
                message=str(exc),
            )

    def ship_order(
        self,
        *,
        order_id: str,
        marketplace_status: str,
        file_status: str,
        tracking_number: str,
        carrier_name: str,
        carrier_code: str | None = None,
        carrier_standard_code: str | None = None,
        carrier_url: str | None = None,
        supplier: str = "",
        prevalidated: bool = False,
    ) -> VersandResult:
        """Send tracking and confirm shipment with the API supported by Worten."""

        tracking = tracking_number.strip()
        carrier = normalize_worten_carrier(carrier_name)
        market = canonical_marketplace_status(marketplace_status)
        tracking_values = list(dict.fromkeys(split_tracking_numbers(tracking)))

        if prevalidated:
            if market != "SHIPPING":
                return VersandResult(
                    order_id, False, False, False,
                    f"stato marketplace {market or 'non disponibile'} non compatibile con l'aggiornamento spedizione",
                )
            if len(tracking_values) != 1:
                return VersandResult(
                    order_id, False, False, False,
                    "deve essere presente un solo numero di tracking per ordine",
                )
            if not tracking:
                return VersandResult(order_id, False, False, False, "numero di tracking non disponibile")
            if not carrier.name:
                return VersandResult(order_id, False, False, False, "corriere non disponibile")
        else:
            eligibility = evaluate_worten_shipment(
                marketplace_status=marketplace_status,
                file_status=file_status,
                tracking=tracking,
                carrier=carrier.name,
                supplier=supplier,
            )
            if not eligibility.allowed:
                return VersandResult(order_id, False, False, False, eligibility.reason)

        # Worten currently presents orders as ``Spedizione 1``. Detect and use
        # the multiple-shipment workflow first; legacy OR23/OR24 is retained for
        # accounts where ST11 is unavailable or no shipment resource exists.
        try:
            multiple_result = self._ship_using_multiple_shipments(
                order_id=order_id,
                tracking_number=tracking,
                carrier_name=carrier.name,
                carrier_code=carrier_code,
                carrier_standard_code=carrier_standard_code,
                carrier_url=carrier_url,
            )
            if multiple_result is not None:
                return multiple_result
        except Exception as exc:
            return VersandResult(order_id, False, False, False, str(exc))

        tracking_done = False
        try:
            self.update_tracking(
                order_id=order_id,
                tracking_number=tracking,
                carrier_name=carrier.name,
                carrier_code=carrier_code,
                carrier_standard_code=carrier_standard_code,
                carrier_url=carrier_url,
            )
            tracking_done = True
            self.validate_shipment(order_id=order_id)
            return VersandResult(
                order_id,
                True,
                True,
                True,
                f"tracking {tracking} e corriere {carrier.name} inviati; ordine contrassegnato come spedito",
            )
        except Exception as exc:
            return VersandResult(order_id, False, tracking_done, False, str(exc))


def ship_selected_worten_orders(
    client: WortenTrackingClient,
    rows: Iterable[Mapping[str, object]],
) -> list[VersandResult]:
    """Send tracking/carrier and shipment confirmation for selected rows."""

    results: list[VersandResult] = []
    seen_orders: set[str] = set()
    for row in rows:
        selected = bool(row.get("Aggiorna") or row.get("selected") or row.get("Seleziona"))
        if not selected:
            continue
        order_id = str(row.get("Ordine") or row.get("order_id") or "").strip()
        if not order_id or order_id in seen_orders:
            continue
        seen_orders.add(order_id)

        carrier_name = str(row.get("Corriere") or row.get("carrier") or "").strip()
        carrier = normalize_worten_carrier(carrier_name)
        results.append(
            client.ship_order(
                order_id=order_id,
                marketplace_status=str(
                    row.get("marketplace_status")
                    or row.get("raw_marketplace_status")
                    or row.get("Stato marketplace")
                    or ""
                ),
                file_status=str(
                    row.get("file_status")
                    or row.get("raw_file_status")
                    or row.get("Stato file originale")
                    or row.get("Stato file")
                    or ""
                ),
                tracking_number=str(row.get("Tracking") or row.get("tracking") or "").strip(),
                carrier_name=carrier.name,
                carrier_code=(str(row.get("carrier_code") or "").strip() or None),
                carrier_standard_code=(
                    str(row.get("carrier_standard_code") or "").strip() or None
                ),
                carrier_url=(str(row.get("carrier_url") or "").strip() or None),
                supplier=str(row.get("Fornitore") or row.get("supplier") or "").strip(),
                prevalidated=_is_prevalidated(row),
            )
        )
    return results
