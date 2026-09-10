from __future__ import annotations

from uuid import UUID

from marketplace_hub_core.auth.models import AuthenticatedSession
from marketplace_hub_core.catalogs.parsing import parse_catalog
from marketplace_hub_core.catalogs.repository import SqlCatalogsRepository
from marketplace_hub_core.tenancy.service import WorkspaceService


class CatalogValidationError(ValueError):
    pass


class CatalogsService:
    def __init__(self, repository: SqlCatalogsRepository, workspace: WorkspaceService) -> None:
        self.repository = repository
        self.workspace = workspace

    def _seller(
        self, principal: AuthenticatedSession, seller_id: UUID, *, write: bool = False,
    ) -> dict:
        return self.workspace.require_seller(
            principal, seller_id, permission="CATALOG", write=write,
        )

    def authorize_upload(self, principal: AuthenticatedSession, seller_id: UUID) -> dict:
        """Authorize before consuming a potentially large multipart request body."""
        return self._seller(principal, seller_id, write=True)

    def authorize_upload_supplier(
        self, principal: AuthenticatedSession, seller_id: UUID, supplier_id: UUID,
    ) -> dict:
        """Recheck the supplier scope before parsing the uploaded catalog contents."""
        seller = self._seller(principal, seller_id, write=True)
        self.repository.supplier(UUID(seller["organization_id"]), seller_id, supplier_id)
        return seller

    def read(self, principal: AuthenticatedSession, seller_id: UUID) -> dict:
        seller = self._seller(principal, seller_id)
        result = self.repository.dashboard(seller_id=seller_id, organization_id=UUID(
            seller["organization_id"]
        ))
        result.update(
            seller_id=str(seller_id),
            can_manage="CATALOG" in seller["write_permissions"],
        )
        return result

    @staticmethod
    def _name(value: str, *, label: str) -> str:
        value = value.strip()
        if not value:
            raise CatalogValidationError(f"Indica il nome del {label}.")
        if len(value) > 200:
            raise CatalogValidationError(f"Il nome del {label} è troppo lungo.")
        return value

    def add_supplier(
        self, principal: AuthenticatedSession, seller_id: UUID, *, name: str, notes: str,
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        name = self._name(name, label="fornitore")
        notes = notes.strip()
        if len(notes) > 5_000:
            raise CatalogValidationError("Le note del fornitore sono troppo lunghe.")
        supplier_id = self.repository.add_supplier(
            UUID(seller["organization_id"]), seller_id, name=name, notes=notes,
        )
        result = self.read(principal, seller_id)
        result["created_supplier_id"] = str(supplier_id)
        return result

    def delete_supplier(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        supplier_id: UUID,
        confirmation: str,
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        deleted = self.repository.delete_supplier(
            UUID(seller["organization_id"]),
            seller_id,
            supplier_id,
            confirmation=confirmation,
        )
        result = self.read(principal, seller_id)
        result["deleted"] = {"kind": "supplier", **deleted}
        return result

    def add_price_list(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        *,
        supplier_id: UUID,
        name: str,
        file_name: str,
        media_type: str,
        content: bytes,
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        name = self._name(name, label="listino")
        file_format, normalized = parse_catalog(file_name, content)
        safe_media_type = (media_type or "application/octet-stream").strip()
        if len(safe_media_type) > 200 or "\r" in safe_media_type or "\n" in safe_media_type:
            safe_media_type = "application/octet-stream"
        price_list_id = self.repository.add_price_list(
            UUID(seller["organization_id"]),
            seller_id,
            supplier_id,
            name=name,
            original_filename=file_name,
            media_type=safe_media_type,
            file_format=file_format,
            artifact=content,
            normalized_products=normalized,
        )
        return self.repository.detail(
            UUID(seller["organization_id"]), seller_id, price_list_id, limit=100,
        )

    def detail(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        limit: int,
    ) -> dict:
        seller = self._seller(principal, seller_id)
        return self.repository.detail(
            UUID(seller["organization_id"]), seller_id, price_list_id, limit=limit,
        )

    def delete_price_list(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        price_list_id: UUID,
        confirmation: str,
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        if confirmation != "ELIMINA":
            raise CatalogValidationError("Scrivi ELIMINA per confermare la rimozione.")
        deleted = self.repository.delete_price_list(
            UUID(seller["organization_id"]), seller_id, price_list_id,
        )
        result = self.read(principal, seller_id)
        result["deleted"] = {"kind": "price_list", **deleted}
        return result
