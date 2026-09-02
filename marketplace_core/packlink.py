from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from marketplace_core.contracts import JobRequest
from services.shared_cache import cache_delete, cache_get_or_set


@dataclass(frozen=True, slots=True)
class PacklinkScope:
    seller_id: int


class PacklinkCore:
    """UI-agnostic Packlink boundary for profile, sync and mass operations."""

    PROFILE_NAMESPACE = "packlink.profile"
    PROFILE_TTL_SECONDS = 300.0

    @staticmethod
    def _client(scope: PacklinkScope):
        from services.packlink import PacklinkClient, integration_credentials, integration_for_seller

        integration = integration_for_seller(scope.seller_id, include_inactive=False)
        if not integration:
            raise RuntimeError("Packlink PRO non è configurato o attivo per questo Seller.")
        credentials = integration_credentials(integration)
        api_key = str(credentials.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("API key Packlink PRO mancante.")
        return PacklinkClient(api_key, seller_id=scope.seller_id)

    def profile(self, scope: PacklinkScope, *, force: bool = False) -> dict[str, Any]:
        key = str(int(scope.seller_id))
        if force:
            cache_delete(self.PROFILE_NAMESPACE, key)

        def load() -> dict[str, Any]:
            client = self._client(scope)
            return {"warehouses": client.warehouses(), "parcels": client.parcels(), "error": ""}

        try:
            value = cache_get_or_set(
                self.PROFILE_NAMESPACE, key, load, ttl_seconds=self.PROFILE_TTL_SECONDS
            )
            return dict(value or {})
        except Exception as exc:
            return {"warehouses": [], "parcels": [], "error": str(exc)}

    def build_sync_shipments_job(self, scope: PacklinkScope) -> JobRequest:
        return JobRequest(kind="packlink.shipments.sync", seller_id=scope.seller_id, payload={})

    def synchronize_shipments(self, scope: PacklinkScope, *, progress=None) -> dict[str, Any]:
        from services.packlink import sync_shipments

        if callable(progress):
            progress(0, 2, "Connessione a Packlink PRO…")
        client = self._client(scope)
        if callable(progress):
            progress(1, 2, "Lettura e salvataggio spedizioni Packlink…")
        result = sync_shipments(scope.seller_id, client)
        if callable(progress):
            progress(2, 2, "Spedizioni Packlink aggiornate")
        return dict(result)

    @staticmethod
    def _task_identity(task: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "account_id": int(task.get("account_id") or task.get("marketplace_account_id") or 0),
            "marketplace": str(task.get("marketplace") or "").strip().lower(),
            "order_id": str(task.get("order_id") or "").strip(),
            "order_key": str(task.get("order_key") or "").strip(),
        }

    @staticmethod
    def _order_index(seller_id: int, tasks: Sequence[Mapping[str, Any]]) -> dict[tuple[int, str, str], dict[str, Any]]:
        from services.cecotec_orders import cached_orders
        from services.packlink import group_marketplace_orders

        scopes = {
            (int(task.get("account_id") or task.get("marketplace_account_id") or 0),
             str(task.get("marketplace") or "").strip().lower())
            for task in tasks
        }
        result: dict[tuple[int, str, str], dict[str, Any]] = {}
        for account_id, marketplace in scopes:
            if account_id <= 0 or not marketplace:
                continue
            records = cached_orders(int(seller_id), account_id, marketplace)
            grouped = group_marketplace_orders(
                records, account_id=account_id, marketplace=marketplace, account_name=""
            )
            for order in grouped:
                result[(account_id, marketplace, str(order.get("order_id") or "").strip())] = dict(order)
        return result

    def build_mass_quotes_job(
        self,
        scope: PacklinkScope,
        tasks: Sequence[Mapping[str, Any]],
        *,
        origin_country: str,
        origin_zip: str,
        source: str = "PRO",
        max_workers: int = 6,
    ) -> JobRequest:
        safe_tasks = []
        for raw in tasks:
            item = self._task_identity(raw)
            item["package"] = dict(raw.get("package") or {})
            safe_tasks.append(item)
        return JobRequest(
            kind="packlink.quotes.mass",
            seller_id=scope.seller_id,
            payload={
                "origin_country": str(origin_country or "").strip().upper(),
                "origin_zip": str(origin_zip or "").strip(),
                "source": str(source or "PRO").strip(),
                "max_workers": max(1, min(12, int(max_workers or 6))),
                "tasks": safe_tasks,
            },
        )

    def quote_many(
        self,
        scope: PacklinkScope,
        tasks: Sequence[Mapping[str, Any]],
        *,
        origin_country: str,
        origin_zip: str,
        source: str = "PRO",
        max_workers: int = 6,
        progress=None,
    ) -> dict[str, Any]:
        from services.packlink import (
            clean_text, package_payload, packlink_package_signature, save_package_profile,
        )
        from services.packlink_mass import save_quote_result

        task_list = [dict(item) for item in tasks]
        order_index = self._order_index(scope.seller_id, task_list)
        base_client = self._client(scope)
        workers = max(1, min(12, int(max_workers or 6)))

        def one(task: dict[str, Any]) -> dict[str, Any]:
            from services.packlink import PacklinkClient
            client = PacklinkClient(
                base_client.api_key, base_url=base_client.base_url,
                timeout=base_client.timeout, seller_id=scope.seller_id,
            )
            identity = self._task_identity(task)
            key = (identity["account_id"], identity["marketplace"], identity["order_id"])
            order = order_index.get(key)
            package = package_payload(task.get("package") or {})
            if not order:
                error = "Ordine non trovato nella cache persistente"
                save_quote_result(
                    scope.seller_id, order_key=identity["order_key"], package=package,
                    origin_country=origin_country, origin_zip=origin_zip,
                    destination_country="", destination_zip="", services=[], error=error,
                )
                return {**identity, "ok": False, "error": error}
            try:
                services = client.shipping_services(
                    from_country=origin_country,
                    from_zip=origin_zip,
                    to_country=clean_text(order.get("country_code")),
                    to_zip=clean_text(order.get("postal_code")),
                    packages=[package], source=source,
                )
                services.sort(key=lambda item: (
                    float(item.get("price") or 10**9), clean_text(item.get("carrier"))
                ))
                try:
                    save_package_profile(scope.seller_id, package, increment_use=False)
                except Exception:
                    pass
                save_quote_result(
                    scope.seller_id, order_key=identity["order_key"], package=package,
                    origin_country=origin_country, origin_zip=origin_zip,
                    destination_country=clean_text(order.get("country_code")),
                    destination_zip=clean_text(order.get("postal_code")),
                    services=services, error="",
                )
                return {
                    **identity, "ok": True, "service_count": len(services),
                    "package_signature": packlink_package_signature(package),
                }
            except Exception as exc:
                save_quote_result(
                    scope.seller_id, order_key=identity["order_key"], package=package,
                    origin_country=origin_country, origin_zip=origin_zip,
                    destination_country=clean_text(order.get("country_code")),
                    destination_zip=clean_text(order.get("postal_code")),
                    services=[], error=str(exc),
                )
                return {**identity, "ok": False, "error": str(exc)}

        items: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="packlink-quote") as pool:
            futures = [pool.submit(one, task) for task in task_list]
            for done, future in enumerate(as_completed(futures), 1):
                item = future.result()
                items.append(item)
                if callable(progress):
                    progress(done, len(task_list), item)
        ok = sum(bool(item.get("ok")) for item in items)
        return {
            "total": len(task_list), "successful": ok,
            "errors": len(task_list) - ok,
            "order_keys": [str(item.get("order_key") or "") for item in items],
        }

    def quote_results(
        self, scope: PacklinkScope, tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        from services.packlink_mass import quote_result

        result: dict[str, dict[str, Any]] = {}
        for task in tasks:
            order_key = str(task.get("order_key") or "").strip()
            package = dict(task.get("package") or {})
            if not order_key or not package:
                continue
            item = quote_result(scope.seller_id, order_key=order_key, package=package)
            if item:
                result[order_key] = item
        return result

    def build_mass_drafts_job(
        self,
        scope: PacklinkScope,
        tasks: Sequence[Mapping[str, Any]],
        *,
        sender: Mapping[str, Any],
        warehouse_id: str = "",
        max_workers: int = 2,
    ) -> JobRequest:
        safe_tasks = []
        for raw in tasks:
            item = self._task_identity(raw)
            item.update({
                "package": dict(raw.get("package") or {}),
                "service": dict(raw.get("service") or {}),
                "declared_value": float(raw.get("declared_value") or 0),
                "forced": bool(raw.get("forced")),
            })
            safe_tasks.append(item)
        return JobRequest(
            kind="packlink.drafts.mass",
            seller_id=scope.seller_id,
            payload={
                "tasks": safe_tasks,
                "sender": dict(sender or {}),
                "warehouse_id": str(warehouse_id or "").strip(),
                "max_workers": max(1, min(4, int(max_workers or 2))),
            },
        )

    def create_drafts_many(
        self,
        scope: PacklinkScope,
        tasks: Sequence[Mapping[str, Any]],
        *,
        sender: Mapping[str, Any],
        warehouse_id: str,
        job_id: str,
        max_workers: int = 2,
        progress=None,
    ) -> dict[str, Any]:
        from services.packlink import (
            build_packlink_draft_payload, clean_text, integration_for_seller,
            packlink_destination_address, packlink_ready_for_payment_validation,
            remember_package_for_order, save_order_draft, validate_packlink_destination_against_order,
        )
        from services.packlink_mass import begin_draft_attempt, finish_draft_attempt, mark_draft_uncertain

        task_list = [dict(item) for item in tasks]
        order_index = self._order_index(scope.seller_id, task_list)
        base_client = self._client(scope)
        integration = integration_for_seller(scope.seller_id, include_inactive=True)
        if not integration:
            raise RuntimeError("Configurazione Packlink PRO non disponibile.")
        workers = max(1, min(4, int(max_workers or 2)))

        def one(task: dict[str, Any]) -> dict[str, Any]:
            from services.packlink import PacklinkClient
            client = PacklinkClient(
                base_client.api_key, base_url=base_client.base_url,
                timeout=base_client.timeout, seller_id=scope.seller_id,
            )
            identity = self._task_identity(task)
            key = (identity["account_id"], identity["marketplace"], identity["order_id"])
            order = order_index.get(key)
            service = dict(task.get("service") or {})
            package = dict(task.get("package") or {})
            forced = bool(task.get("forced"))
            if not order:
                return {**identity, "status": "error", "error": "Ordine non trovato nella cache persistente"}
            try:
                payload = build_packlink_draft_payload(
                    integration=integration, order=order, sender=sender, package=package,
                    service=service, declared_value=float(task.get("declared_value") or 0),
                    warehouse_id=warehouse_id,
                )
                sender_phone = clean_text(
                    payload.get("from", {}).get("phone")
                    if isinstance(payload.get("from"), Mapping) else ""
                )
                payload["to"] = packlink_destination_address(order, fallback_phone=sender_phone)
                validate_packlink_destination_against_order(payload, order)
                missing = packlink_ready_for_payment_validation(payload)
                if missing:
                    return {
                        **identity, "status": "error",
                        "error": "Dati mancanti: " + ", ".join(missing),
                    }
            except Exception as exc:
                return {**identity, "status": "error", "error": str(exc)}

            guard = begin_draft_attempt(
                seller_id=scope.seller_id,
                marketplace_account_id=identity["account_id"],
                marketplace=identity["marketplace"], order_id=identity["order_id"],
                job_id=job_id, forced=forced,
            )
            if not guard.get("allowed"):
                status = "duplicate" if guard.get("status") == "created" else "blocked"
                return {
                    **identity, "status": status,
                    "reference": str(guard.get("shipment_reference") or ""),
                    "error": str(guard.get("reason") or ""),
                }
            operation_key = str(guard.get("operation_key") or "")
            try:
                response = client.create_draft(payload)
                submitted = (
                    response.get("submitted_payload")
                    if isinstance(response, Mapping) and isinstance(response.get("submitted_payload"), Mapping)
                    else payload
                )
                save_order_draft(
                    scope.seller_id, order, service, submitted, response, forced=forced
                )
                remember_package_for_order(scope.seller_id, order, package)
                reference = clean_text(response.get("reference"))
                finish_draft_attempt(
                    seller_id=scope.seller_id,
                    marketplace_account_id=identity["account_id"],
                    marketplace=identity["marketplace"], order_id=identity["order_id"],
                    operation_key=operation_key, shipment_reference=reference,
                )
                return {
                    **identity, "status": "created", "reference": reference,
                    "carrier": clean_text(service.get("carrier")),
                    "service": clean_text(service.get("service")), "error": "",
                }
            except Exception as exc:
                mark_draft_uncertain(
                    seller_id=scope.seller_id,
                    marketplace_account_id=identity["account_id"],
                    marketplace=identity["marketplace"], order_id=identity["order_id"],
                    operation_key=operation_key, error=str(exc),
                )
                return {
                    **identity, "status": "uncertain", "reference": "",
                    "carrier": clean_text(service.get("carrier")),
                    "service": clean_text(service.get("service")),
                    "error": str(exc),
                }

        items: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="packlink-draft") as pool:
            futures = [pool.submit(one, task) for task in task_list]
            for done, future in enumerate(as_completed(futures), 1):
                item = future.result()
                items.append(item)
                if callable(progress):
                    progress(done, len(task_list), item)
        created = sum(item.get("status") == "created" for item in items)
        duplicates = sum(item.get("status") == "duplicate" for item in items)
        uncertain = sum(item.get("status") == "uncertain" for item in items)
        errors = len(items) - created - duplicates - uncertain
        return {
            "total": len(items), "created": created, "duplicates": duplicates,
            "uncertain": uncertain, "errors": errors, "items": items,
        }
