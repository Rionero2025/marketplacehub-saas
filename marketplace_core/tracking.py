from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from marketplace_core.accounting import AccountingCore, AccountingPeriod, AccountingScope
from marketplace_core.contracts import JobRequest


@dataclass(frozen=True, slots=True)
class TrackingScope:
    seller_id: int
    account_id: int
    marketplace: str

    @property
    def marketplace_key(self) -> str:
        return str(self.marketplace or "").strip().lower()


class TrackingCore:
    """Tracking/document matching application boundary independent from Streamlit."""

    def orders(self, scope: TrackingScope, period: AccountingPeriod) -> list[dict[str, Any]]:
        return AccountingCore().rows(
            AccountingScope(scope.seller_id, scope.account_id, scope.marketplace_key),
            period=period,
        )

    def build_orders_sync_job(
        self,
        scope: TrackingScope,
        period: AccountingPeriod,
        *,
        full: bool = False,
    ) -> JobRequest:
        return AccountingCore().build_sync_job(
            AccountingScope(scope.seller_id, scope.account_id, scope.marketplace_key),
            period,
            full=full,
        )

    def build_analysis_job(
        self,
        scope: TrackingScope,
        period: AccountingPeriod,
        *,
        file_ids: Sequence[int] = (),
        urls: Sequence[str] = (),
        supplier_choice: str = "Riconoscimento automatico dal file",
    ) -> JobRequest:
        normalized_ids = []
        for value in file_ids:
            item = int(value or 0)
            if item > 0 and item not in normalized_ids:
                normalized_ids.append(item)
        normalized_urls = list(dict.fromkeys(
            str(value or "").strip() for value in urls if str(value or "").strip()
        ))
        if not normalized_ids and not normalized_urls:
            raise ValueError("Seleziona almeno un documento spedizioni o un URL.")
        return JobRequest(
            kind="tracking.documents.analyze",
            seller_id=scope.seller_id,
            payload={
                "account_id": scope.account_id,
                "marketplace": scope.marketplace_key,
                "date_from": period.date_from.isoformat(),
                "date_to": period.date_to.isoformat(),
                "file_ids": normalized_ids,
                "urls": normalized_urls,
                "supplier_choice": str(supplier_choice or "").strip(),
            },
        )

    def analyze_archived_documents(
        self,
        scope: TrackingScope,
        period: AccountingPeriod,
        *,
        file_ids: Sequence[int] = (),
        urls: Sequence[str] = (),
        supplier_choice: str = "Riconoscimento automatico dal file",
        progress=None,
    ) -> dict[str, Any]:
        from services.cecotec_orders import clean_text
        from services.order_tracking import (
            archive_tracking_file,
            archived_tracking_file,
            detect_supplier_from_orders,
            download_tracking_file_from_url,
            match_tracking_rows,
            parse_tracking_document,
            persist_import,
            update_archived_tracking_file_supplier,
        )

        orders = self.orders(scope, period)
        if not orders:
            raise RuntimeError("Non risultano ordini nel periodo selezionato.")

        ids: list[int] = []
        for value in file_ids:
            item = int(value or 0)
            if item > 0 and item not in ids:
                ids.append(item)

        url_values = list(dict.fromkeys(
            str(value or "").strip() for value in urls if str(value or "").strip()
        ))
        total_steps = max(1, len(ids) + len(url_values) + 3)
        done = 0
        if callable(progress):
            progress(done, total_steps, "Preparazione documenti spedizioni…")

        # URL downloads happen in the worker and are archived before parsing.
        for source_url in url_values:
            downloaded = download_tracking_file_from_url(source_url)
            archived = archive_tracking_file(
                seller_id=scope.seller_id,
                account_id=scope.account_id,
                marketplace=scope.marketplace_key,
                file_name=downloaded.file_name,
                content=downloaded.content,
                source_type="url",
                source_url=downloaded.source_url,
                mime_type=downloaded.mime_type,
            )
            file_id = int(archived["id"])
            if file_id not in ids:
                ids.append(file_id)
            done += 1
            if callable(progress):
                progress(done, total_steps, f"Scaricato {downloaded.file_name}")

        payloads: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for file_id in ids:
            archived = archived_tracking_file(
                file_id,
                seller_id=scope.seller_id,
                account_id=scope.account_id,
            )
            if not archived:
                continue
            content = bytes(archived.get("content") or b"")
            digest = clean_text(archived.get("file_sha256")) or hashlib.sha256(content).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            payloads.append({
                "id": int(archived["id"]),
                "name": clean_text(archived.get("file_name")),
                "content": content,
                "sha256": digest,
            })
        if not payloads:
            raise RuntimeError("Non è stato trovato alcun documento spedizioni utilizzabile.")

        all_rows: list[dict[str, Any]] = []
        formats: list[str] = []
        parsed_supplier = ""
        parsed_confidence = 0.0
        for payload in payloads:
            parsed = parse_tracking_document(payload["content"], payload["name"])
            formats.append(parsed.source_format)
            all_rows.extend(parsed.rows)
            if parsed.supplier and parsed.confidence >= parsed_confidence:
                parsed_supplier = parsed.supplier
                parsed_confidence = parsed.confidence
            done += 1
            if callable(progress):
                progress(done, total_steps, f"Analizzato {payload['name']}")

        automatic_label = "Riconoscimento automatico dal file"
        if str(supplier_choice or "").strip() == automatic_label:
            selected_supplier, confidence, ranking = detect_supplier_from_orders(
                all_rows, orders, parsed_supplier
            )
        else:
            selected_supplier = str(supplier_choice or "").strip()
            confidence = 1.0
            ranking = [{"supplier": selected_supplier, "score": 100.0, "reason": "Scelta manuale"}]
        if not selected_supplier:
            raise RuntimeError(
                "Il fornitore non è stato riconosciuto con sufficiente sicurezza. "
                "Selezionalo manualmente e ripeti l'analisi."
            )

        used_ids = [int(item["id"]) for item in payloads]
        update_archived_tracking_file_supplier(used_ids, selected_supplier)
        if callable(progress):
            progress(max(done, total_steps - 2), total_steps, "Abbinamento documenti agli ordini…")
        matches = match_tracking_rows(all_rows, orders, supplier=selected_supplier)
        combined = b"".join(item["content"] for item in payloads)
        import_id = persist_import(
            seller_id=scope.seller_id,
            account_id=scope.account_id,
            marketplace=scope.marketplace_key,
            supplier=selected_supplier,
            file_name="; ".join(item["name"] for item in payloads),
            content=combined,
            source_format="; ".join(sorted(set(formats))),
            matches=matches,
            file_ids=used_ids,
        )
        if callable(progress):
            progress(total_steps, total_steps, "Analisi e abbinamento completati")

        matched = sum(item.get("match_status") == "Abbinato automaticamente" for item in matches)
        ambiguous = sum(str(item.get("match_status") or "").startswith("Ambiguo") for item in matches)
        return {
            "import_id": int(import_id),
            "supplier": selected_supplier,
            "confidence": float(confidence or 0.0),
            "ranking": ranking,
            "file_ids": used_ids,
            "file_names": [item["name"] for item in payloads],
            "token": hashlib.sha256(combined).hexdigest(),
            "total": len(matches),
            "matched": matched,
            "ambiguous": ambiguous,
            "unmatched": len(matches) - matched - ambiguous,
        }

    def import_matches(self, import_id: int) -> list[dict[str, Any]]:
        from services.order_tracking import tracking_matches_for_import
        return tracking_matches_for_import(int(import_id))
