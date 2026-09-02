from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marketplace_core.contracts import JobRequest
from services.shared_cache import cache_delete, cache_get_or_set


@dataclass(frozen=True, slots=True)
class PacklinkScope:
    seller_id: int


class PacklinkCore:
    """UI-agnostic Packlink application boundary.

    Network profile reads are cached and shipment synchronization is expressed as
    a persistent job so Streamlit, FastAPI and dedicated workers can reuse the
    same use-case without carrying API secrets in the job payload.
    """

    PROFILE_NAMESPACE = "packlink.profile"
    PROFILE_TTL_SECONDS = 300.0

    @staticmethod
    def _client(scope: PacklinkScope):
        from services.packlink import (
            PacklinkClient,
            integration_credentials,
            integration_for_seller,
        )

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
            return {
                "warehouses": client.warehouses(),
                "parcels": client.parcels(),
                "error": "",
            }

        try:
            value = cache_get_or_set(
                self.PROFILE_NAMESPACE,
                key,
                load,
                ttl_seconds=self.PROFILE_TTL_SECONDS,
            )
            return dict(value or {})
        except Exception as exc:
            return {"warehouses": [], "parcels": [], "error": str(exc)}

    def build_sync_shipments_job(self, scope: PacklinkScope) -> JobRequest:
        return JobRequest(
            kind="packlink.shipments.sync",
            seller_id=scope.seller_id,
            payload={},
        )

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
