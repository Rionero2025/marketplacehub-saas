from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from marketplace_hub_core.auth.models import AuthenticatedSession, AuthRealm
from marketplace_hub_core.catalogs.artifacts import (
    MAX_REMOTE_CATALOG_SOURCE_BYTES,
    CatalogArtifactEncodingError,
    EncodedCatalogArtifact,
    encode_catalog_artifact,
)
from marketplace_hub_core.catalogs.fetching import (
    CatalogFetchError,
    CatalogFetchLimitError,
    CatalogFetchResponseError,
    CatalogFetchSecurityError,
    CatalogFetchTimeoutError,
    CatalogFetchValidationError,
    DownloadedCatalogFile,
    validate_catalog_url,
)
from marketplace_hub_core.catalogs.innpro import (
    InnproLimitError,
    InnproValidationError,
    parse_innpro_iof,
)
from marketplace_hub_core.catalogs.parsing import (
    CatalogFileLimitError,
    CatalogFileValidationError,
    parse_catalog,
)
from marketplace_hub_core.catalogs.repository import (
    CatalogRefreshJobNotFoundError,
    CatalogSourceRevisionMismatchError,
    SqlCatalogsRepository,
)
from marketplace_hub_core.marketplace_connections.security import decrypt_credentials
from marketplace_hub_core.seller_settings.security import (
    CredentialStorageUnavailableError,
    encrypt_credentials,
)
from marketplace_hub_core.tenancy.service import (
    SellerNotAccessibleError,
    WorkspacePermissionError,
    WorkspaceService,
)


class CatalogValidationError(ValueError):
    pass


class CatalogQueueUnavailableError(RuntimeError):
    pass


JOB_MESSAGES = {
    "queued": "Importazione listino in coda.",
    "running": "Download del listino in corso.",
    "done": "Listino aggiornato.",
    "queue_unavailable": "Il servizio di importazione non è disponibile. Riprova tra poco.",
    "permission_revoked": "L’accesso al catalogo non è più autorizzato.",
    "source_unavailable": "La sorgente del listino non è disponibile.",
    "source_changed": "La configurazione del listino è cambiata. Avvia un nuovo aggiornamento.",
    "download_failed": "Non è stato possibile scaricare il listino.",
    "invalid_feed": "Il contenuto scaricato non è un listino supportato.",
    "file_too_large": (
        "Il listino supera il limite consentito: 20 MiB per i feed generici, "
        "200 MiB per i feed URL InnPro."
    ),
    "timeout": "Il download del listino ha superato il tempo massimo.",
    "refresh_failed": "L’aggiornamento del listino non è riuscito.",
    "worker_interrupted": (
        "Aggiornamento listino interrotto dal worker. Puoi avviarlo di nuovo."
    ),
}

INTERRUPTED_QUEUE_STATES = frozenset(
    {"failed", "stopped", "canceled", "finished", "stale"}
)
MISSING_QUEUE_GRACE = timedelta(seconds=30)


class CatalogsService:
    def __init__(
        self,
        repository: SqlCatalogsRepository,
        workspace: WorkspaceService,
        queue=None,
        master_key: SecretStr | None = None,
        fetcher=None,
    ) -> None:
        self.repository = repository
        self.workspace = workspace
        self.queue = queue
        self.master_key = master_key or SecretStr("")
        self.fetcher = fetcher

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
        organization_id = UUID(seller["organization_id"])
        result = self.repository.dashboard(
            seller_id=seller_id,
            organization_id=organization_id,
        )
        if self._recover_dashboard_jobs(organization_id, seller_id, result):
            result = self.repository.dashboard(
                seller_id=seller_id,
                organization_id=organization_id,
            )
        result.update(
            seller_id=str(seller_id),
            can_manage="CATALOG" in seller["write_permissions"],
        )
        return result

    @staticmethod
    def _older_than_missing_grace(job: dict) -> bool:
        value = job.get("created_at")
        try:
            created_at = (
                datetime.fromisoformat(value)
                if isinstance(value, str)
                else value
            )
            if not isinstance(created_at, datetime):
                return False
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            return datetime.now(UTC) - created_at.astimezone(UTC) > MISSING_QUEUE_GRACE
        except (TypeError, ValueError, OverflowError):
            return False

    def _recover_job(
        self,
        organization_id: UUID,
        seller_id: UUID,
        job: dict | None,
    ) -> dict | None:
        if (
            job is None
            or job.get("status") not in {"queued", "running"}
            or self.queue is None
            or not hasattr(self.queue, "state")
        ):
            return job
        try:
            state = str(self.queue.state(job["id"])).casefold()
        except Exception:
            # A temporary Redis failure is not evidence that the worker stopped.
            return job
        interrupted = state in INTERRUPTED_QUEUE_STATES
        missing = state == "missing" and self._older_than_missing_grace(job)
        if not interrupted and not missing:
            return job
        try:
            self.repository.fail_job(
                organization_id,
                seller_id,
                UUID(str(job["id"])),
                error_code="worker_interrupted",
                message=JOB_MESSAGES["worker_interrupted"],
            )
        except CatalogRefreshJobNotFoundError:
            # The worker may have completed between the RQ check and the DB update.
            pass
        return self.repository.job(
            organization_id,
            seller_id,
            UUID(str(job["id"])),
        )

    def _recover_dashboard_jobs(
        self,
        organization_id: UUID,
        seller_id: UUID,
        dashboard: dict,
    ) -> bool:
        recovered = False
        for price_list in dashboard.get("price_lists", []):
            job = price_list.get("latest_job")
            current = self._recover_job(organization_id, seller_id, job)
            if (
                current is not None
                and job is not None
                and current.get("status") != job.get("status")
            ):
                recovered = True
        return recovered

    @staticmethod
    def _name(value: str, *, label: str) -> str:
        value = value.strip()
        if not value:
            raise CatalogValidationError(f"Indica il nome del {label}.")
        if len(value) > 200:
            raise CatalogValidationError(f"Il nome del {label} è troppo lungo.")
        return value

    @staticmethod
    def _feed_identity(provider: str, feed_role: str) -> tuple[str, str]:
        provider = provider.strip().casefold()
        feed_role = feed_role.strip().casefold()
        if provider == "generic" and feed_role == "standard":
            return provider, feed_role
        if provider == "innpro" and feed_role in {"full", "light"}:
            return provider, feed_role
        raise CatalogValidationError(
            "La tipologia del feed non è valida: usa generic/standard oppure "
            "innpro/full o innpro/light."
        )

    @staticmethod
    def _parse_feed(
        provider: str,
        feed_role: str,
        file_name: str,
        source: bytes | Path,
    ) -> tuple[str, Sequence[dict[str, Any]]]:
        if provider == "generic":
            if not isinstance(source, bytes):
                try:
                    source = source.read_bytes()
                except OSError as exc:
                    raise CatalogFileValidationError(
                        "Il listino scaricato non può essere letto."
                    ) from exc
            return parse_catalog(file_name, source)
        try:
            parsed = parse_innpro_iof(source, expected_role=feed_role)
        except InnproLimitError as exc:
            raise CatalogFileLimitError(str(exc)) from None
        except InnproValidationError as exc:
            raise CatalogFileValidationError(str(exc)) from None
        return parsed.file_format, parsed.rows

    @staticmethod
    def _close_parsed_rows(rows: object | None) -> None:
        close = getattr(rows, "close", None)
        if close is not None:
            close()

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
        provider: str = "generic",
        feed_role: str = "standard",
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        name = self._name(name, label="listino")
        provider, feed_role = self._feed_identity(provider, feed_role)
        file_format, normalized = self._parse_feed(
            provider, feed_role, file_name, content,
        )
        try:
            encoded = encode_catalog_artifact(content)
            safe_media_type = (media_type or "application/octet-stream").strip()
            if (
                len(safe_media_type) > 200
                or "\r" in safe_media_type
                or "\n" in safe_media_type
            ):
                safe_media_type = "application/octet-stream"
            price_list_id = self.repository.add_price_list(
                UUID(seller["organization_id"]),
                seller_id,
                supplier_id,
                name=name,
                original_filename=file_name,
                media_type=safe_media_type,
                file_format=file_format,
                artifact=encoded.content,
                artifact_encoding=encoded.encoding,
                artifact_size=encoded.raw_size,
                artifact_sha256=encoded.raw_sha256,
                normalized_products=normalized,
                provider=provider,
                feed_role=feed_role,
            )
            return self.repository.detail(
                UUID(seller["organization_id"]), seller_id, price_list_id, limit=100,
            )
        finally:
            self._close_parsed_rows(normalized)

    @staticmethod
    def _remote_source(url: str, username: str, password: str) -> tuple[str, dict[str, str]]:
        value = url.strip()
        try:
            parsed = validate_catalog_url(value)
        except CatalogFetchError as exc:
            raise CatalogValidationError(str(exc)) from None
        username = username.strip()
        if (
            len(username) > 500
            or len(password) > 4_096
            or ":" in username
            or any(
                unicodedata.category(character).startswith("C")
                for character in username + password
            )
        ):
            raise CatalogValidationError("Le credenziali del feed non sono valide.")
        if bool(username) != bool(password):
            raise CatalogValidationError("Indica sia username sia password, oppure lasciali vuoti.")
        return parsed.host, {"url": value, "username": username, "password": password}

    def _queue_job(self, organization_id: UUID, seller_id: UUID, result: dict) -> None:
        if not result.get("created_job"):
            return
        job_id = UUID(str(result["job_id"]))
        try:
            if self.queue is None:
                raise RuntimeError
            self.queue.enqueue(job_id)
        except Exception:
            self.repository.fail_job(
                organization_id,
                seller_id,
                job_id,
                error_code="queue_unavailable",
                message=JOB_MESSAGES["queue_unavailable"],
            )
            raise CatalogQueueUnavailableError(JOB_MESSAGES["queue_unavailable"]) from None

    def add_url_price_list(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        *,
        supplier_id: UUID,
        name: str,
        url: str,
        username: str = "",
        password: str = "",
        provider: str = "generic",
        feed_role: str = "standard",
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        organization_id = UUID(seller["organization_id"])
        name = self._name(name, label="listino")
        provider, feed_role = self._feed_identity(provider, feed_role)
        source_host, source = self._remote_source(url, username, password)
        encrypted = encrypt_credentials(source, self.master_key)
        created = self.repository.create_url_price_list(
            organization_id,
            seller_id,
            supplier_id,
            name=name,
            source_config_encrypted=encrypted,
            source_host=source_host,
            requested_by=principal.user_id,
            realm=principal.realm.value,
            provider=provider,
            feed_role=feed_role,
        )
        self._queue_job(organization_id, seller_id, created)
        return {
            "price_list": self.repository.detail(
                organization_id, seller_id, UUID(str(created["price_list_id"])), limit=100,
            )["price_list"],
            "job": self.repository.job(
                organization_id, seller_id, UUID(str(created["job_id"])),
            ),
        }

    def update_url_price_list(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        price_list_id: UUID,
        *,
        url: str,
        credentials_mode: str,
        expected_config_revision: int,
        username: str = "",
        password: str = "",
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        organization_id = UUID(seller["organization_id"])
        if credentials_mode not in {"keep", "replace", "remove"}:
            raise CatalogValidationError("La gestione delle credenziali non è valida.")
        if credentials_mode != "replace" and (username or password):
            raise CatalogValidationError(
                "Username e password sono ammessi solo quando sostituisci le credenziali."
            )

        if credentials_mode == "keep":
            stored = self.repository.url_source_configuration(
                organization_id,
                seller_id,
                price_list_id,
                expected_config_revision=expected_config_revision,
            )
            encrypted = stored["source_config_encrypted"]
            if isinstance(encrypted, bytes):
                encrypted = encrypted.decode("ascii")
            current = decrypt_credentials(encrypted, self.master_key)
            source_host, source = self._remote_source(
                url,
                current.get("username", ""),
                current.get("password", ""),
            )
            if source_host != stored["source_host"]:
                raise CatalogValidationError(
                    "Non puoi conservare le credenziali quando cambi il server del feed. "
                    "Sostituiscile o rimuovile."
                )
        elif credentials_mode == "replace":
            if not username or not password:
                raise CatalogValidationError(
                    "Indica sia username sia password per sostituire le credenziali."
                )
            source_host, source = self._remote_source(url, username, password)
        else:
            source_host, source = self._remote_source(url, "", "")

        encrypted = encrypt_credentials(source, self.master_key)
        self.repository.update_url_source(
            organization_id,
            seller_id,
            price_list_id,
            source_config_encrypted=encrypted,
            source_host=source_host,
            expected_config_revision=expected_config_revision,
        )
        return {
            "price_list": self.repository.detail(
                organization_id, seller_id, price_list_id, limit=100,
            )["price_list"],
        }

    def refresh_price_list(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        price_list_id: UUID,
    ) -> dict:
        seller = self._seller(principal, seller_id, write=True)
        organization_id = UUID(seller["organization_id"])
        current = self.repository.detail(
            organization_id,
            seller_id,
            price_list_id,
            limit=1,
        )["price_list"]
        self._recover_job(
            organization_id,
            seller_id,
            current.get("latest_job"),
        )
        created = self.repository.create_refresh_job(
            organization_id,
            seller_id,
            price_list_id,
            requested_by=principal.user_id,
            realm=principal.realm.value,
        )
        self._queue_job(organization_id, seller_id, created)
        return {
            "price_list": self.repository.detail(
                organization_id, seller_id, price_list_id, limit=100,
            )["price_list"],
            "job": self.repository.job(
                organization_id, seller_id, UUID(str(created["job_id"])),
            ),
        }

    def read_refresh_job(
        self,
        principal: AuthenticatedSession,
        seller_id: UUID,
        price_list_id: UUID,
        job_id: UUID,
    ) -> dict:
        seller = self._seller(principal, seller_id)
        organization_id = UUID(seller["organization_id"])
        job = self.repository.job(organization_id, seller_id, job_id)
        if str(job.get("price_list_id")) != str(price_list_id):
            raise CatalogRefreshJobNotFoundError("Importazione listino non disponibile.")
        job = self._recover_job(organization_id, seller_id, job) or job
        return {"job": job}

    def run_refresh_job(self, job_id: UUID) -> None:
        """Run one URL refresh. The worker receives no URL or credentials."""
        source = self.repository.source_for_job(job_id)
        organization_id = UUID(str(source["organization_id"]))
        seller_id = UUID(str(source["seller_id"]))
        if not self.repository.claim_job(organization_id, seller_id, job_id):
            return
        principal = AuthenticatedSession(
            session_id=UUID(int=0),
            user_id=UUID(str(source["requested_by"])),
            login="",
            display_name="",
            realm=AuthRealm(str(source["realm"])),
            expires_at=datetime.max.replace(tzinfo=UTC),
        )

        def authorize() -> None:
            self._seller(principal, seller_id, write=True)

        def fail(code: str) -> None:
            self.repository.fail_job(
                organization_id,
                seller_id,
                job_id,
                error_code=code,
                message=JOB_MESSAGES[code],
            )

        downloaded: Any | None = None
        normalized: Sequence[dict[str, Any]] | None = None
        try:
            authorize()
            if int(source["current_source_config_revision"]) != int(
                source["source_config_revision"]
            ):
                fail("source_changed")
                return
            encrypted = source["source_config_encrypted"]
            if isinstance(encrypted, bytes):
                encrypted = encrypted.decode("ascii")
            config = decrypt_credentials(encrypted, self.master_key)
            provider = str(source.get("provider", "generic"))
            feed_role = str(source.get("feed_role", "standard"))
            provider, feed_role = self._feed_identity(provider, feed_role)
            fetcher = self.fetcher

            def progress(processed_bytes: int, total_bytes: int | None = None) -> None:
                authorize()
                safe_total = (
                    max(processed_bytes, total_bytes) if total_bytes is not None else None
                )
                self.repository.job_progress(
                    organization_id,
                    seller_id,
                    job_id,
                    processed_bytes=processed_bytes,
                    total_bytes=safe_total,
                    message=JOB_MESSAGES["running"],
                )

            if fetcher is None:
                if provider == "innpro":
                    from marketplace_hub_core.catalogs.fetching import fetch_catalog_to_file

                    downloaded = fetch_catalog_to_file(
                        config.get("url", ""),
                        username=config.get("username", ""),
                        password=config.get("password", ""),
                        on_progress=progress,
                        maximum_bytes=MAX_REMOTE_CATALOG_SOURCE_BYTES,
                        deadline_seconds=300,
                    )
                else:
                    from marketplace_hub_core.catalogs.fetching import fetch_catalog

                    downloaded = fetch_catalog(
                        config.get("url", ""),
                        username=config.get("username", ""),
                        password=config.get("password", ""),
                        on_progress=progress,
                    )
            else:
                downloaded = fetcher(
                    config.get("url", ""),
                    username=config.get("username", ""),
                    password=config.get("password", ""),
                    on_progress=progress,
                )

            downloaded_source = (
                downloaded.path
                if isinstance(downloaded, DownloadedCatalogFile)
                else downloaded.content
            )
            file_format, normalized = self._parse_feed(
                provider,
                feed_role,
                downloaded.file_name,
                downloaded_source,
            )
            encoded: EncodedCatalogArtifact = encode_catalog_artifact(downloaded_source)
            authorize()
            activated = self.repository.activate_remote_version(
                job_id,
                expected_config_revision=int(source["source_config_revision"]),
                original_filename=downloaded.file_name,
                media_type=downloaded.media_type,
                file_format=file_format,
                artifact=encoded.content,
                artifact_encoding=encoded.encoding,
                artifact_size=encoded.raw_size,
                artifact_sha256=encoded.raw_sha256,
                normalized_products=normalized,
            )
            # The repository atomically finishes the job together with activation.
            if not activated.get("duplicate"):
                return
        except (SellerNotAccessibleError, WorkspacePermissionError):
            fail("permission_revoked")
        except CredentialStorageUnavailableError:
            fail("source_unavailable")
        except CatalogSourceRevisionMismatchError:
            fail("source_changed")
        except CatalogFileLimitError:
            fail("file_too_large")
        except CatalogFileValidationError:
            fail("invalid_feed")
        except CatalogArtifactEncodingError:
            fail("file_too_large")
        except (TimeoutError, CatalogFetchTimeoutError):
            fail("timeout")
        except CatalogFetchLimitError:
            fail("file_too_large")
        except (CatalogFetchValidationError, CatalogFetchSecurityError,
                CatalogFetchResponseError):
            fail("invalid_feed")
        except CatalogFetchError:
            fail("download_failed")
        except Exception as exc:
            code = getattr(exc, "code", "download_failed")
            if code not in JOB_MESSAGES:
                code = "refresh_failed"
            fail(code)
        finally:
            self._close_parsed_rows(normalized)
            if isinstance(downloaded, DownloadedCatalogFile):
                downloaded.cleanup()

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
