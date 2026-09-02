from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import requests

from services.catalog_intelligence.models import (
    Capability,
    TaxonomyAttribute,
    TaxonomyBundle,
    TaxonomyCategory,
)
from services.catalog_intelligence.utils import (
    as_list,
    bool_value,
    clean_text,
    first_value,
    int_value,
)


class MiraklCatalogError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(slots=True)
class MiraklCatalogClient:
    api_url: str
    api_key: str
    shop_id: str = ""
    timeout: float = 45.0

    @property
    def api_base(self) -> str:
        base = clean_text(self.api_url).rstrip("/")
        if not base:
            raise ValueError("URL API Mirakl/Worten mancante.")
        return base if base.lower().endswith("/api") else f"{base}/api"

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        if not clean_text(self.api_key):
            raise ValueError("API Key Mirakl/Worten mancante.")
        return {
            "Authorization": clean_text(self.api_key),
            "Accept": accept,
            "User-Agent": "MarketplaceHub/246 CatalogIntelligence",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_payload: Mapping[str, Any] | list[Any] | None = None,
        data: Mapping[str, Any] | bytes | None = None,
        files: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> requests.Response:
        url = f"{self.api_base}/{path.lstrip('/')}"
        query = {
            str(key): value
            for key, value in dict(params or {}).items()
            if value not in (None, "", [])
        }
        try:
            response = requests.request(
                clean_text(method).upper() or "GET",
                url,
                params=query,
                json=json_payload,
                data=data,
                files=files,
                headers=self._headers(accept=accept),
                timeout=max(3.0, float(self.timeout)),
            )
        except requests.RequestException as exc:
            raise MiraklCatalogError(f"Errore di rete Mirakl: {exc}") from exc
        if response.status_code >= 400:
            payload: Any
            try:
                payload = response.json() if response.content else {}
            except ValueError:
                payload = response.text[:4000]
            detail = clean_text(payload) if not isinstance(payload, Mapping) else clean_text(
                payload.get("message") or payload.get("error") or payload.get("errors") or payload
            )
            raise MiraklCatalogError(
                f"Mirakl HTTP {response.status_code}: {detail[:2000]}",
                status_code=int(response.status_code),
                payload=payload,
            )
        return response

    def request_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        method: str = "GET",
        json_payload: Mapping[str, Any] | list[Any] | None = None,
        data: Mapping[str, Any] | bytes | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        response = self.request(
            method,
            path,
            params=params,
            json_payload=json_payload,
            data=data,
            files=files,
        )
        try:
            return response.json() if response.content else {}
        except ValueError:
            return {"http_status": int(response.status_code), "text": response.text[:4000]}

    def request_bytes(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "text/csv,application/octet-stream,*/*",
    ) -> bytes:
        return bytes(self.request("GET", path, params=params, accept=accept).content or b"")

    def hierarchies(self, *, hierarchy: str = "", max_level: int | None = None) -> Any:
        params: dict[str, Any] = {}
        if hierarchy:
            params["hierarchy"] = hierarchy
        if max_level is not None:
            params["max_level"] = max(0, int(max_level))
        return self.request_json("/hierarchies", params=params)

    def product_attributes(
        self,
        *,
        hierarchy: str = "",
        max_level: int | None = None,
        channels: Iterable[str] = (),
        with_roles: bool = True,
    ) -> Any:
        params: dict[str, Any] = {"with_roles": str(bool(with_roles)).lower()}
        if hierarchy:
            params["hierarchy"] = hierarchy
        if max_level is not None:
            params["max_level"] = max(0, int(max_level))
        channel_values = [clean_text(value) for value in channels if clean_text(value)]
        if channel_values:
            params["channels"] = channel_values
        return self.request_json("/products/attributes", params=params)

    def value_lists(self, *, code: str = "") -> Any:
        params: dict[str, Any] = {}
        if code:
            params["code"] = code
        if clean_text(self.shop_id):
            params["shop_id"] = clean_text(self.shop_id)
        return self.request_json("/values_lists", params=params)

    def offer_states(self) -> Any:
        return self.request_json("/offers/states")

    def logistic_classes(self) -> Any:
        return self.request_json("/shipping/logistic_classes")


    def product_offers(self, *, ean: str, max_results: int = 20) -> Any:
        """P11 read-only duplicate check for a product reference."""
        token = clean_text(ean)
        if not token:
            raise ValueError("EAN obbligatorio per il controllo prodotto Mirakl.")
        params: dict[str, Any] = {
            "product_references": f"EAN|{token}",
            "max": max(1, min(100, int(max_results))),
        }
        if clean_text(self.shop_id):
            params["shop_id"] = clean_text(self.shop_id)
        return self.request_json("/products/offers", params=params)

    def import_products(
        self,
        csv_bytes: bytes,
        *,
        filename: str = "products.csv",
        import_mode: str = "NORMAL",
        operator_format: bool = True,
    ) -> Any:
        """P41 product import with current and legacy Mirakl compatibility.

        Current Mirakl product imports use multipart fields ``operator_format``
        and ``shop``.  Some older Worten deployments accepted ``shop_id`` in
        the query string instead.  We use the documented form first and retry
        the legacy shape only after an explicit HTTP 400/422 response, which
        cannot represent a successfully-created import.
        """
        mode = clean_text(import_mode).upper() or "NORMAL"
        if mode not in {"NORMAL", "REPLACE"}:
            raise ValueError("Modalità import prodotto Mirakl non valida: usa NORMAL o REPLACE.")
        if not isinstance(csv_bytes, (bytes, bytearray)) or not csv_bytes:
            raise ValueError("Il file prodotto Mirakl è vuoto.")
        file_part = {
            "file": (clean_text(filename) or "products.csv", bytes(csv_bytes), "text/csv")
        }
        form: dict[str, Any] = {
            "operator_format": "true" if bool(operator_format) else "false",
        }
        if clean_text(self.shop_id):
            form["shop"] = clean_text(self.shop_id)
        try:
            return self.request_json(
                "/products/imports",
                method="POST",
                data=form,
                files=file_part,
            )
        except MiraklCatalogError as exc:
            if exc.status_code not in {400, 422}:
                raise
            legacy_params: dict[str, Any] = {"import_mode": mode}
            if clean_text(self.shop_id):
                legacy_params["shop_id"] = clean_text(self.shop_id)
            return self.request_json(
                "/products/imports",
                method="POST",
                params=legacy_params,
                files=file_part,
            )

    def product_import_status(self, import_id: str) -> Any:
        token = clean_text(import_id)
        if not token:
            raise ValueError("Import ID prodotto Mirakl mancante.")
        params = {"shop_id": clean_text(self.shop_id)} if clean_text(self.shop_id) else None
        return self.request_json(f"/products/imports/{token}", params=params)

    def product_import_report(self, import_id: str, report: str) -> bytes:
        token = clean_text(import_id)
        report_key = clean_text(report).lower()
        paths = {
            "error": "error_report",
            "integration": "integration_report",
            "transformed": "transformed_file",
            "transformation_error": "transformation_error_report",
        }
        if report_key not in paths:
            raise ValueError(f"Report prodotto Mirakl non supportato: {report}")
        params = {"shop_id": clean_text(self.shop_id)} if clean_text(self.shop_id) else None
        return self.request_bytes(f"/products/imports/{token}/{paths[report_key]}", params=params)

    def import_offers(
        self,
        csv_bytes: bytes,
        *,
        filename: str = "offers.csv",
        import_mode: str = "NORMAL",
        operator_format: bool = True,
        with_products: bool = False,
    ) -> Any:
        """OF01 offer import, preferring the proven Worten query shape."""
        mode = clean_text(import_mode).upper() or "NORMAL"
        if mode not in {"NORMAL", "REPLACE"}:
            raise ValueError("Modalità import offerta Mirakl non valida: usa NORMAL o REPLACE.")
        if not isinstance(csv_bytes, (bytes, bytearray)) or not csv_bytes:
            raise ValueError("Il file offerte Mirakl è vuoto.")
        file_part = {
            "file": (clean_text(filename) or "offers.csv", bytes(csv_bytes), "text/csv")
        }
        params: dict[str, Any] = {"import_mode": mode}
        if clean_text(self.shop_id):
            params["shop_id"] = clean_text(self.shop_id)
        try:
            return self.request_json(
                "/offers/imports",
                method="POST",
                params=params,
                files=file_part,
            )
        except MiraklCatalogError as exc:
            if exc.status_code not in {400, 422}:
                raise
            form: dict[str, Any] = {
                "import_mode": mode,
                "operator_format": "true" if bool(operator_format) else "false",
                "with_products": "true" if bool(with_products) else "false",
            }
            if clean_text(self.shop_id):
                form["shop"] = clean_text(self.shop_id)
            return self.request_json(
                "/offers/imports",
                method="POST",
                data=form,
                files=file_part,
            )

    def offer_import_status(self, import_id: str) -> Any:
        token = clean_text(import_id)
        if not token:
            raise ValueError("Import ID offerta Mirakl mancante.")
        params = {"shop_id": clean_text(self.shop_id)} if clean_text(self.shop_id) else None
        return self.request_json(f"/offers/imports/{token}", params=params)

    def offer_import_error_report(self, import_id: str) -> bytes:
        token = clean_text(import_id)
        params = {"shop_id": clean_text(self.shop_id)} if clean_text(self.shop_id) else None
        return self.request_bytes(f"/offers/imports/{token}/error_report", params=params)


def _extract_list(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys + ("data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = value.get("data")
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
    # Some Mirakl versions return a dictionary keyed by code.
    records: list[dict[str, Any]] = []
    for key, value in payload.items():
        if isinstance(value, Mapping):
            item = dict(value)
            item.setdefault("code", clean_text(key))
            records.append(item)
    return records


def _localized(value: Any, locale: str = "") -> str:
    if isinstance(value, Mapping):
        preferred = clean_text(locale)
        candidates = (
            preferred,
            preferred.replace("-", "_"),
            preferred.replace("_", "-"),
            preferred.split("-")[0] if preferred else "",
            preferred.split("_")[0] if preferred else "",
            "pt_PT",
            "pt-PT",
            "en_GB",
            "en-US",
            "en",
        )
        for key in candidates:
            if key and clean_text(value.get(key)):
                return clean_text(value.get(key))
        for item in value.values():
            if clean_text(item):
                return clean_text(item)
        return ""
    return clean_text(value)


def _category_code(raw: Mapping[str, Any]) -> str:
    return clean_text(first_value(raw, "code", "hierarchy_code", "category_code", "id"))


def _category_label(raw: Mapping[str, Any], locale: str = "") -> str:
    for key in ("label", "labels", "name", "title", "description"):
        if key in raw:
            label = _localized(raw.get(key), locale)
            if label:
                return label
    return _category_code(raw)


def _flatten_hierarchies(
    payload: Any,
    *,
    locale: str = "",
) -> list[dict[str, Any]]:
    roots = _extract_list(payload, "hierarchies", "categories")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(raw: Mapping[str, Any], parent_code: str = "", level: int = 0) -> None:
        item = dict(raw)
        code = _category_code(item)
        if not code:
            return
        item.setdefault("parent_code", parent_code)
        item.setdefault("level", level)
        item.setdefault("normalized_label", _category_label(item, locale))
        if code not in seen:
            result.append(item)
            seen.add(code)
        children = item.get("children") or item.get("child_hierarchies") or item.get("sub_categories")
        for child in as_list(children):
            if isinstance(child, Mapping):
                visit(child, code, level + 1)

    for root in roots:
        visit(root, clean_text(first_value(root, "parent_code", "parent", "parent_id")), int_value(root.get("level")))
    return result


def parse_categories(payload: Any, *, locale: str = "") -> list[TaxonomyCategory]:
    raw_items = _flatten_hierarchies(payload, locale=locale)
    raw_by_code = {_category_code(item): item for item in raw_items if _category_code(item)}
    parent_codes = {
        clean_text(first_value(item, "parent_code", "parent", "parent_id"))
        for item in raw_items
        if clean_text(first_value(item, "parent_code", "parent", "parent_id"))
    }
    path_cache: dict[str, str] = {}

    def path_for(code: str, stack: set[str] | None = None) -> str:
        if code in path_cache:
            return path_cache[code]
        stack = set(stack or ())
        if code in stack:
            return _category_label(raw_by_code.get(code, {}), locale) or code
        stack.add(code)
        raw = raw_by_code.get(code, {})
        label = _category_label(raw, locale) or code
        parent = clean_text(first_value(raw, "parent_code", "parent", "parent_id"))
        if parent and parent in raw_by_code and parent != code:
            prefix = path_for(parent, stack)
            result = f"{prefix} > {label}" if prefix else label
        else:
            result = label
        path_cache[code] = result
        return result

    categories: list[TaxonomyCategory] = []
    for code, raw in raw_by_code.items():
        explicit_leaf = first_value(raw, "leaf", "is_leaf", default=None)
        is_leaf = code not in parent_codes if explicit_leaf in (None, "") else bool_value(explicit_leaf)
        categories.append(
            TaxonomyCategory(
                external_id=code,
                parent_external_id=clean_text(
                    first_value(raw, "parent_code", "parent", "parent_id")
                ),
                code=code,
                label=_category_label(raw, locale) or code,
                path=path_for(code),
                level=int_value(first_value(raw, "level", "depth", default=0)),
                is_leaf=is_leaf,
                product_type=clean_text(first_value(raw, "product_type", default=code)),
                raw=dict(raw),
            )
        )
    return sorted(categories, key=lambda item: (item.path.lower(), item.external_id))


def parse_value_lists(payload: Any, *, locale: str = "") -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in _extract_list(payload, "values_lists", "value_lists", "lists"):
        list_code = clean_text(first_value(raw, "code", "list_code", "id"))
        if not list_code:
            continue
        values: list[dict[str, Any]] = []
        source_values = raw.get("values") or raw.get("items") or raw.get("entries") or []
        if isinstance(source_values, Mapping):
            source_values = [
                {"code": code, "label": label} if not isinstance(label, Mapping) else {"code": code, **dict(label)}
                for code, label in source_values.items()
            ]
        for item in as_list(source_values):
            if isinstance(item, Mapping):
                record = dict(item)
                code = clean_text(first_value(record, "code", "value", "id", "key"))
                if not code:
                    continue
                label = ""
                for field in ("label", "labels", "name", "description"):
                    if field in record:
                        label = _localized(record.get(field), locale)
                        if label:
                            break
                record["code"] = code
                record["label"] = label or code
                values.append(record)
            elif clean_text(item):
                values.append({"code": clean_text(item), "label": clean_text(item)})
        result[list_code] = values
    return result


def _attribute_categories(raw: Mapping[str, Any]) -> list[str]:
    values = (
        raw.get("hierarchy_codes")
        or raw.get("hierarchies")
        or raw.get("categories")
        or raw.get("category_codes")
    )
    if values is None:
        value = first_value(raw, "hierarchy_code", "hierarchy", "category_code")
        return [clean_text(value)] if clean_text(value) else [""]
    result: list[str] = []
    for item in as_list(values):
        if isinstance(item, Mapping):
            code = clean_text(first_value(item, "code", "hierarchy_code", "id"))
        else:
            code = clean_text(item)
        if code:
            result.append(code)
    return result or [""]


def parse_attributes(
    payload: Any,
    *,
    value_lists: Mapping[str, list[dict[str, Any]]] | None = None,
    locale: str = "",
) -> list[TaxonomyAttribute]:
    lists = dict(value_lists or {})
    result: list[TaxonomyAttribute] = []
    seen: set[tuple[str, str]] = set()
    for raw in _extract_list(payload, "attributes", "product_attributes"):
        external_id = clean_text(first_value(raw, "code", "attribute_code", "id", "name"))
        if not external_id:
            continue
        requirement = clean_text(
            first_value(raw, "requirement_level", "requirement", "required", default="OPTIONAL")
        ).upper()
        required = requirement in {"REQUIRED", "MANDATORY", "TRUE", "1"} or bool_value(
            raw.get("required")
        )
        if required:
            requirement = "REQUIRED"
        elif requirement not in {"OPTIONAL", "CONDITIONAL", "RECOMMENDED"}:
            requirement = "OPTIONAL"
        value_list_code = clean_text(
            first_value(raw, "value_list_code", "list_code", "value_list", "values_list")
        )
        constraints: dict[str, Any] = {}
        for key in (
            "min_length",
            "max_length",
            "minimum",
            "maximum",
            "min",
            "max",
            "pattern",
            "regex",
            "precision",
            "scale",
            "default_value",
        ):
            if raw.get(key) not in (None, ""):
                constraints[key] = raw.get(key)
        label = ""
        for field in ("label", "labels", "name", "description"):
            if field in raw:
                label = _localized(raw.get(field), locale)
                if label:
                    break
        conditions: list[dict[str, Any]] = []
        raw_conditions = raw.get("conditions") or raw.get("condition")
        if isinstance(raw_conditions, Mapping):
            conditions.append(dict(raw_conditions))
        elif isinstance(raw_conditions, list):
            conditions.extend(dict(item) for item in raw_conditions if isinstance(item, Mapping))
        for category_code in _attribute_categories(raw):
            key = (category_code, external_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(
                TaxonomyAttribute(
                    external_id=external_id,
                    category_external_id=category_code,
                    code=external_id,
                    label=label or external_id,
                    data_type=clean_text(
                        first_value(raw, "type", "data_type", "value_type", default="TEXT")
                    ).upper(),
                    requirement_level=requirement,
                    required=required,
                    multiple=bool_value(
                        first_value(raw, "multiple", "is_multiple", "multi_value", default=False)
                    ),
                    variant=bool_value(
                        first_value(raw, "variant", "is_variant", "variant_attribute", default=False)
                    ),
                    unit=clean_text(first_value(raw, "unit", "measurement_unit")),
                    locale=clean_text(locale),
                    value_list_code=value_list_code,
                    constraints=constraints,
                    values=list(lists.get(value_list_code, [])),
                    conditions=conditions,
                    raw=dict(raw),
                )
            )
    return result


def discover_capabilities(client: MiraklCatalogClient) -> list[Capability]:
    capabilities: list[Capability] = []

    def check(key: str, callback) -> Any:
        try:
            value = callback()
            count = len(_extract_list(value, "hierarchies", "attributes", "values_lists", "offer_states", "logistic_classes"))
            capabilities.append(
                Capability(key=key, supported=True, details={"count": count})
            )
            return value
        except MiraklCatalogError as exc:
            capabilities.append(
                Capability(
                    key=key,
                    supported=False,
                    status_code=exc.status_code,
                    message=clean_text(exc)[:1000],
                )
            )
            return None
        except Exception as exc:
            capabilities.append(Capability(key=key, supported=False, message=clean_text(exc)[:1000]))
            return None

    hierarchies = check("hierarchies_h11", lambda: client.hierarchies(max_level=1))
    check("attributes_pm11", lambda: client.product_attributes(max_level=1))
    check("value_lists_vl11", client.value_lists)
    check("offer_states", client.offer_states)
    check("logistic_classes", client.logistic_classes)
    capabilities.append(
        Capability(
            key="connection",
            supported=hierarchies is not None,
            message="Connessione verificata tramite H11." if hierarchies is not None else "H11 non accessibile.",
        )
    )
    # P41/OF01 are write endpoints and are never called by discovery.
    capabilities.extend(
        [
            Capability(
                key="product_import_p41",
                supported=hierarchies is not None,
                message="Endpoint di scrittura non provato durante il controllo in sola lettura.",
                details={"probe": "documentation_only", "path": "/api/products/imports"},
            ),
            Capability(
                key="offer_import_of01",
                supported=hierarchies is not None,
                message="Endpoint di scrittura non provato durante il controllo in sola lettura.",
                details={"probe": "documentation_only", "path": "/api/offers/imports"},
            ),
        ]
    )
    return capabilities


def _collect_locales(payloads: Iterable[Any]) -> list[dict[str, Any]]:
    locales: set[str] = set()

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                token = clean_text(key)
                if len(token) in {2, 5} and ("-" in token or "_" in token or len(token) == 2):
                    if token.replace("-", "").replace("_", "").isalpha():
                        locales.add(token)
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    for payload in payloads:
        inspect(payload)
    return [{"code": code, "label": code, "storefront": "worten"} for code in sorted(locales)]


def sync_taxonomy(
    client: MiraklCatalogClient,
    *,
    locale: str = "pt_PT",
    hierarchy: str = "",
    max_level: int | None = None,
) -> TaxonomyBundle:
    hierarchy_payload = client.hierarchies(hierarchy=hierarchy, max_level=max_level)
    attribute_payload = client.product_attributes(
        hierarchy=hierarchy,
        max_level=max_level,
        with_roles=True,
    )
    value_payload = client.value_lists()
    value_lists = parse_value_lists(value_payload, locale=locale)
    categories = parse_categories(hierarchy_payload, locale=locale)
    attributes = parse_attributes(
        attribute_payload,
        value_lists=value_lists,
        locale=locale,
    )
    scope_hierarchy = clean_text(hierarchy) or "all"
    locales = _collect_locales((hierarchy_payload, attribute_payload, value_payload))
    if locale and locale not in {item["code"] for item in locales}:
        locales.append({"code": locale, "label": locale, "storefront": "worten"})
    return TaxonomyBundle(
        marketplace="worten",
        scope_key=f"{scope_hierarchy}:{clean_text(locale) or '*'}",
        storefront="pt",
        locale=clean_text(locale),
        categories=categories,
        attributes=attributes,
        locales=locales,
        metadata={
            "source": "Mirakl H11/PM11/VL11",
            "hierarchy": scope_hierarchy,
            "shop_id": clean_text(client.shop_id),
            "category_count": len(categories),
            "attribute_count": len(attributes),
            "value_list_count": len(value_lists),
            "value_count": sum(len(values) for values in value_lists.values()),
        },
    )


__all__ = [
    "MiraklCatalogClient",
    "MiraklCatalogError",
    "discover_capabilities",
    "parse_attributes",
    "parse_categories",
    "parse_value_lists",
    "sync_taxonomy",
]
