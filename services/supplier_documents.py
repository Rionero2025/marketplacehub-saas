from __future__ import annotations

import io
import json
import math
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

import pandas as pd

from services.accounting import (
    _content_disposition_name,
    _download_public_url,
    _excel_engine,
    accounting_comparison_url_candidates,
)
from services.cecotec_orders import clean_identifier, clean_text, normalize_supplier
from services.db import connect, json_text, now_iso, rows


SUPPORTED_EXTENSIONS = {
    ".xlsx": "excel",
    ".xls": "excel",
    ".pdf": "pdf",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".html": "html",
    ".htm": "html",
}
MAX_SUPPLIER_DOCUMENT_BYTES = 80 * 1024 * 1024

# Strong supplier-order patterns. Numeric-only values are intentionally excluded
# because they are often EANs, prices, postcodes or marketplace order numbers.
STRONG_SUPPLIER_ORDER_PATTERNS = (
    re.compile(r"\bD\d{7,}\b", re.I),                     # Cecotec: D261143542
    re.compile(r"\bZS-\d{3,}/\d{2}/[A-Z0-9-]+\b", re.I), # Forcetop
    re.compile(r"\b[A-Z]{2,8}-\d{4,}(?:/\d+)*(?:-[A-Z0-9]+)?\b", re.I),
)
GENERIC_MARKET_ORDER_PATTERNS = (
    re.compile(r"\bWORTEN\s*[:#-]?\s*([0-9]{7,12}-[A-Z0-9])\b", re.I),
    re.compile(r"\bKAUFLAND\s*[:#-]?\s*([A-Z0-9]{5,20})\b", re.I),
)


def ensure_supplier_document_schema() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounting_supplier_document_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                source_names TEXT NOT NULL DEFAULT '',
                document_count INTEGER NOT NULL DEFAULT 0,
                references_found INTEGER NOT NULL DEFAULT 0,
                matched_rows INTEGER NOT NULL DEFAULT 0,
                updated_rows INTEGER NOT NULL DEFAULT 0,
                conflicts INTEGER NOT NULL DEFAULT 0,
                unmatched_rows INTEGER NOT NULL DEFAULT 0,
                ambiguous_rows INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_supplier_docs_scope
            ON accounting_supplier_document_imports(
                seller_id,marketplace_account_id,marketplace,created_at
            );
            """
        )


def _normalized_order_id(value: Any) -> str:
    value = clean_text(value).upper()
    value = re.sub(r"^(?:WORTEN|KAUFLAND)\s*[:#-]?\s*", "", value, flags=re.I)
    return re.sub(r"\s+", "", value)


def _multiline_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t \f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _document_extension(file_name: Any) -> str:
    return Path(clean_text(file_name)).suffix.lower()


def _signature_kind(content: bytes, file_name: Any = "", content_type: Any = "") -> str:
    extension = _document_extension(file_name)
    if extension in SUPPORTED_EXTENSIONS:
        return SUPPORTED_EXTENSIONS[extension]
    content_type = clean_text(content_type).lower().split(";", 1)[0]
    if content.startswith(b"PK\x03\x04") or content.startswith(b"\xd0\xcf\x11\xe0"):
        return "excel"
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"\xff\xd8\xff") or content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if content[:12].startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image"
    stripped = content.lstrip()[:200].lower()
    if content_type in {"text/html", "application/xhtml+xml"} or stripped.startswith((b"<!doctype html", b"<html", b"<table")):
        return "html"
    return ""


def _safe_generic_file_name(headers: Mapping[str, Any], final_url: str, kind: str) -> str:
    name = _content_disposition_name(headers.get("content-disposition"))
    if not name:
        name = Path(unquote(urlparse(final_url).path)).name
    name = re.sub(r"[\\/:*?\"<>|]+", "_", clean_text(name)).strip(" .")
    extension_by_kind = {"excel": ".xlsx", "pdf": ".pdf", "image": ".jpg", "html": ".html"}
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        name = f"{Path(name).stem or 'documento_fornitore'}{extension_by_kind.get(kind, '.bin')}"
    return name


def download_supplier_document_url(value: Any, *, max_bytes: int = MAX_SUPPLIER_DOCUMENT_BYTES) -> dict[str, Any]:
    """Download a public supplier document from a direct/Drive/Sheets URL."""
    original = clean_text(value)
    if not original:
        raise ValueError("Inserisci un URL.")
    failures: list[str] = []
    # Reuse Google export/download candidates; for ordinary URLs the original is first.
    for candidate in accounting_comparison_url_candidates(original):
        try:
            content, headers, final_url = _download_public_url(
                candidate,
                max_bytes=max_bytes,
                timeout=(12.0, 120.0),
                accept=(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                    "application/vnd.ms-excel,application/pdf,image/*,text/html,"
                    "application/octet-stream;q=0.9,*/*;q=0.5"
                ),
                user_agent="MarketplaceHub/1.0 (+supplier document import)",
            )
            kind = _signature_kind(content, final_url, headers.get("content-type"))
            if not kind:
                raise ValueError("formato non supportato; usa Excel, PDF, JPG/PNG/WEBP o HTML")
            file_name = _safe_generic_file_name(headers, final_url, kind)
            return {
                "content": content,
                "file_name": file_name,
                "kind": kind,
                "input_url": original,
                "resolved_url": final_url,
                "size_bytes": len(content),
                "source": "URL",
            }
        except Exception as exc:
            failures.append(str(exc))
    detail = failures[-1] if failures else "nessun tentativo disponibile"
    raise ValueError(f"Impossibile scaricare il documento: {detail}")


def _known_order_hits(text: Any, known_orders: Mapping[str, str]) -> list[str]:
    normalized_text = clean_text(text).upper()
    compact = re.sub(r"\s+", "", normalized_text)
    hits: list[str] = []
    for normalized, original in known_orders.items():
        if normalized and normalized in compact:
            hits.append(original)
    for pattern in GENERIC_MARKET_ORDER_PATTERNS:
        for match in pattern.finditer(normalized_text):
            order_id = _normalized_order_id(match.group(1))
            original = known_orders.get(order_id)
            if original and original not in hits:
                hits.append(original)
    return hits


def _strong_supplier_candidates(text: Any, excluded: Iterable[str] = ()) -> list[str]:
    source = clean_text(text).upper()
    excluded_set = {_normalized_order_id(value) for value in excluded}
    candidates: list[str] = []
    for pattern in STRONG_SUPPLIER_ORDER_PATTERNS:
        for match in pattern.finditer(source):
            value = clean_text(match.group(0)).upper()
            if _normalized_order_id(value) in excluded_set:
                continue
            if value not in candidates:
                candidates.append(value)
    return candidates


def _line_number(value: Any) -> str:
    text = clean_text(value)
    if re.fullmatch(r"0*\d{1,5}", text):
        return str(int(text))
    return ""


def _cecotec_reference(base: Any, line: Any) -> str:
    base_text = clean_text(base).upper()
    line_text = _line_number(line)
    if re.fullmatch(r"D\d{7,}", base_text, re.I) and line_text:
        return f"{base_text}-{line_text}"
    return base_text


def _supplier_name_from_reference(reference: Any, hint: Any = "") -> str:
    hint_text = clean_text(hint)
    if hint_text and hint_text.lower() not in {"automatico", "auto"}:
        return hint_text
    reference = clean_text(reference).upper()
    if re.match(r"^D\d{7,}(?:-\d+)?$", reference):
        return "Cecotec"
    if reference.startswith("ZS-"):
        return "Forcetop"
    return ""


def _row_excerpt(cells: Sequence[Any], limit: int = 400) -> str:
    text = " | ".join(clean_text(value) for value in cells if clean_text(value))
    return text[:limit]


def _normalized_header(value: Any) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9à-ÿ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tabular_header_indexes(frame: pd.DataFrame) -> tuple[dict[str, int], int]:
    aliases = {
        "market_order": {
            "num ordine market", "numero ordine market", "numero ordine marketplace",
            "ordine marketplace", "marketplace order", "market order", "customer order",
            "riferimento marketplace", "reference marketplace", "comment", "comments",
            "nota", "note", "reference",
        },
        "supplier_order": {
            "n ordine fornitore", "num ordine fornitore", "numero ordine fornitore",
            "ordine fornitore", "supplier order", "supplier order number",
            "purchase order", "purchase order number", "numero pedido", "pedido",
            "order supplier", "order number supplier",
        },
        "article": {"article", "articolo", "sku", "codice articolo", "product code", "ean", "gtin"},
        "line": {"line", "linea", "riga", "posizione", "position", "order line"},
    }
    indexes: dict[str, int] = {}
    # Pandas HTML tables often expose real headers as DataFrame columns.
    for index, column in enumerate(frame.columns):
        key = _normalized_header(column)
        for field, names in aliases.items():
            if key in names and field not in indexes:
                indexes[field] = index
    header_row = -1
    if "supplier_order" in indexes or "market_order" in indexes:
        return indexes, header_row
    for row_position in range(min(20, len(frame))):
        cells = list(frame.iloc[row_position].tolist())
        local: dict[str, int] = {}
        for index, cell in enumerate(cells):
            key = _normalized_header(cell)
            for field, names in aliases.items():
                if key in names and field not in local:
                    local[field] = index
        if "supplier_order" in local or "market_order" in local:
            return local, row_position
    return indexes, header_row


def _parse_tabular_frame(
    frame: pd.DataFrame,
    *,
    document_name: str,
    location_prefix: str,
    known_orders: Mapping[str, str],
    supplier_hint: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if frame is None or frame.empty:
        return results
    frame = frame.where(pd.notna(frame), None)
    header_indexes, header_row = _tabular_header_indexes(frame)
    for row_index, raw_row in frame.iterrows():
        try:
            numeric_row_index = int(row_index)
        except Exception:
            numeric_row_index = len(results)
        if header_row >= 0 and numeric_row_index <= header_row:
            continue
        cells = list(raw_row.tolist())
        row_text = _row_excerpt(cells, limit=2000)
        order_hits = _known_order_hits(row_text, known_orders)
        if not order_hits and "market_order" in header_indexes:
            idx = header_indexes["market_order"]
            if idx < len(cells):
                order_hits = _known_order_hits(cells[idx], known_orders)
        if not order_hits:
            continue

        # Cecotec drop-shipping response: A=base order, B=line, C=article,
        # P/comment contains "WORTEN <marketplace order>".
        first = clean_text(cells[0]) if cells else ""
        second = clean_text(cells[1]) if len(cells) > 1 else ""
        article = clean_text(cells[2]) if len(cells) > 2 else ""
        supplier_reference = _cecotec_reference(first, second)
        method = "Cecotec: ordine base + numero riga"
        confidence = 100

        # Generic structured supplier files: accept the value under an explicit
        # supplier-order header even when it is purely alphabetic (e.g. TNFILEWPJ).
        if not supplier_reference and "supplier_order" in header_indexes:
            idx = header_indexes["supplier_order"]
            candidate = clean_text(cells[idx]) if idx < len(cells) else ""
            if candidate and _normalized_order_id(candidate) not in {
                _normalized_order_id(value) for value in order_hits
            }:
                line_value = ""
                if "line" in header_indexes and header_indexes["line"] < len(cells):
                    line_value = cells[header_indexes["line"]]
                supplier_reference = _cecotec_reference(candidate, line_value) or candidate
                method = "Colonna numero ordine fornitore"
                confidence = 98
        if "article" in header_indexes and header_indexes["article"] < len(cells):
            article = clean_text(cells[header_indexes["article"]]) or article

        if not supplier_reference:
            candidates = _strong_supplier_candidates(row_text, excluded=order_hits)
            supplier_reference = candidates[0] if len(candidates) == 1 else ""
            method = "Numero ordine fornitore nella stessa riga"
            confidence = 90 if supplier_reference else 0
        if not supplier_reference:
            continue
        for order_id in order_hits:
            results.append({
                "document_name": document_name,
                "location": f"{location_prefix} · riga {numeric_row_index + 1}",
                "order_id": order_id,
                "supplier_order_number": supplier_reference,
                "supplier": _supplier_name_from_reference(supplier_reference, supplier_hint),
                "article": article,
                "ean": article if re.fullmatch(r"\d{8,14}", article) else "",
                "method": method,
                "confidence": confidence,
                "excerpt": _row_excerpt(cells),
            })
    return results


def _extract_excel_references(
    content: bytes,
    file_name: str,
    known_orders: Mapping[str, str],
    supplier_hint: str,
) -> tuple[list[dict[str, Any]], str]:
    book = pd.ExcelFile(io.BytesIO(content), engine=_excel_engine(file_name))
    references: list[dict[str, Any]] = []
    try:
        for sheet_name in book.sheet_names:
            frame = pd.read_excel(book, sheet_name=sheet_name, header=None, dtype=object)
            references.extend(_parse_tabular_frame(
                frame,
                document_name=file_name,
                location_prefix=f"foglio {clean_text(sheet_name)}",
                known_orders=known_orders,
                supplier_hint=supplier_hint,
            ))
    finally:
        close = getattr(book, "close", None)
        if callable(close):
            close()
    return references, f"{len(references)} riferimenti estratti da Excel"


def _ocr_image_text(content: bytes) -> str:
    try:
        from PIL import Image
        import pytesseract
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("OCR immagini non disponibile: installa Pillow e pytesseract.") from exc
    executable = shutil.which("tesseract")
    if not executable:
        for candidate in (
            r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
            r"C:\\Program Files (x86)\\Tesseract-OCR\\tesseract.exe",
        ):
            if Path(candidate).exists():
                executable = candidate
                break
    if not executable:
        raise RuntimeError(
            "Tesseract OCR non è installato. Installa Tesseract-OCR oppure usa Excel/PDF testuale/HTML."
        )
    pytesseract.pytesseract.tesseract_cmd = executable
    image = Image.open(io.BytesIO(content))
    try:
        return _multiline_text(pytesseract.image_to_string(image, lang="por+ita+eng"))
    except Exception:
        return _multiline_text(pytesseract.image_to_string(image))


def _extract_pdf_text(content: bytes) -> tuple[str, str]:
    text_parts: list[str] = []
    ocr_pages = 0
    try:
        import fitz  # PyMuPDF
        document = fitz.open(stream=content, filetype="pdf")
        try:
            for page_index, page in enumerate(document):
                page_text = _multiline_text(page.get_text("text"))
                if len(page_text) < 30 and page_index < 12:
                    try:
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        page_text = _ocr_image_text(pix.tobytes("png"))
                        ocr_pages += 1
                    except Exception:
                        pass
                if page_text:
                    text_parts.append(f"[Pagina {page_index + 1}]\n{page_text}")
        finally:
            document.close()
    except Exception:
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            for page_index, page in enumerate(reader.pages):
                page_text = _multiline_text(page.extract_text())
                if page_text:
                    text_parts.append(f"[Pagina {page_index + 1}]\n{page_text}")
        except Exception as exc:
            raise RuntimeError("Il PDF non può essere letto.") from exc
    return "\n".join(text_parts), f"PDF letto; {ocr_pages} pagine elaborate con OCR"


def _extract_html(content: bytes, file_name: str, known_orders: Mapping[str, str], supplier_hint: str) -> tuple[list[dict[str, Any]], str, str]:
    decoded = content.decode("utf-8", errors="replace")
    references: list[dict[str, Any]] = []
    try:
        tables = pd.read_html(io.StringIO(decoded))
    except Exception:
        tables = []
    for index, table in enumerate(tables, start=1):
        references.extend(_parse_tabular_frame(
            table,
            document_name=file_name,
            location_prefix=f"tabella HTML {index}",
            known_orders=known_orders,
            supplier_hint=supplier_hint,
        ))
    try:
        from lxml import html
        root = html.fromstring(decoded)
        text = _multiline_text("\n".join(root.itertext()))
    except Exception:
        text = _multiline_text(re.sub(r"<[^>]+>", "\n", decoded))
    return references, text, f"{len(tables)} tabelle HTML lette"


def _nearest_supplier_reference(context: str, order_id: str, global_candidates: Sequence[str]) -> str:
    candidates = _strong_supplier_candidates(context, excluded=[order_id])
    if candidates:
        return candidates[0]
    if len(global_candidates) == 1:
        return global_candidates[0]
    return ""


def _parse_text_references(
    text: str,
    *,
    document_name: str,
    known_orders: Mapping[str, str],
    supplier_hint: str,
    source_label: str,
) -> list[dict[str, Any]]:
    text = _multiline_text(text)
    if not text:
        return []
    global_candidates = _strong_supplier_candidates(text, excluded=known_orders.values())
    lines = [clean_text(line) for line in re.split(r"[\r\n]+", text) if clean_text(line)]
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, line in enumerate(lines):
        hits = _known_order_hits(line, known_orders)
        if not hits:
            continue
        context_lines = lines[max(0, index - 2): min(len(lines), index + 3)]
        context = " | ".join(context_lines)
        for order_id in hits:
            supplier_reference = _nearest_supplier_reference(context, order_id, global_candidates)
            if not supplier_reference:
                continue
            # If a Cecotec base order is global and a 4-digit row number is
            # visible beside the marketplace reference, preserve the familiar
            # Dxxxxxxxxx-N format used by the accounting workbook.
            if re.fullmatch(r"D\d{7,}", supplier_reference, re.I):
                line_match = re.search(r"(?:^|\s)(0{0,3}\d{1,4})(?:\s|$)", line)
                if line_match:
                    supplier_reference = _cecotec_reference(supplier_reference, line_match.group(1))
            key = (_normalized_order_id(order_id), supplier_reference.upper())
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "document_name": document_name,
                "location": f"{source_label} · linea {index + 1}",
                "order_id": order_id,
                "supplier_order_number": supplier_reference,
                "supplier": _supplier_name_from_reference(supplier_reference, supplier_hint),
                "article": "",
                "ean": "",
                "method": "Numero marketplace e ordine fornitore nello stesso contesto",
                "confidence": 82,
                "excerpt": context[:400],
            })
    return results


def parse_supplier_document(
    document: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    *,
    supplier_hint: str = "Automatico",
) -> dict[str, Any]:
    content = document.get("content")
    if not isinstance(content, (bytes, bytearray)) or not content:
        raise ValueError("Documento vuoto.")
    content = bytes(content)
    file_name = clean_text(document.get("file_name")) or "documento_fornitore"
    kind = clean_text(document.get("kind")) or _signature_kind(content, file_name)
    if not kind:
        raise ValueError(f"Formato non riconosciuto per {file_name}.")
    known_orders = {
        _normalized_order_id(item.get("order_id")): clean_text(item.get("order_id"))
        for item in records if clean_text(item.get("order_id"))
    }
    references: list[dict[str, Any]] = []
    details = ""
    if kind == "excel":
        references, details = _extract_excel_references(content, file_name, known_orders, supplier_hint)
    elif kind == "html":
        table_refs, text, details = _extract_html(content, file_name, known_orders, supplier_hint)
        references.extend(table_refs)
        references.extend(_parse_text_references(
            text, document_name=file_name, known_orders=known_orders,
            supplier_hint=supplier_hint, source_label="HTML",
        ))
    elif kind == "pdf":
        text, details = _extract_pdf_text(content)
        references = _parse_text_references(
            text, document_name=file_name, known_orders=known_orders,
            supplier_hint=supplier_hint, source_label="PDF",
        )
    elif kind == "image":
        text = _ocr_image_text(content)
        details = "Immagine letta tramite OCR"
        references = _parse_text_references(
            text, document_name=file_name, known_orders=known_orders,
            supplier_hint=supplier_hint, source_label="OCR",
        )
    else:
        raise ValueError(f"Formato {kind} non supportato.")

    # De-duplicate references generated both by tables and raw text.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in references:
        key = (_normalized_order_id(item.get("order_id")), clean_text(item.get("supplier_order_number")).upper())
        if not all(key):
            continue
        current = unique.get(key)
        if current is None or int(item.get("confidence") or 0) > int(current.get("confidence") or 0):
            unique[key] = item
    return {
        "file_name": file_name,
        "kind": kind,
        "source": clean_text(document.get("source")) or "File",
        "size_bytes": len(content),
        "details": details,
        "references": list(unique.values()),
    }


def _record_candidate_for_reference(
    candidates: Sequence[Mapping[str, Any]], reference: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, str]:
    if not candidates:
        return None, "Ordine marketplace non presente nella Contabilità"
    if len(candidates) == 1:
        return candidates[0], "Numero ordine marketplace esatto"
    ean = clean_identifier(reference.get("ean"))
    if ean:
        matches = [item for item in candidates if clean_identifier(item.get("ean")) == ean]
        if len(matches) == 1:
            return matches[0], "Ordine + EAN esatto"
    article = clean_text(reference.get("article")).lower()
    if article:
        matches = [
            item for item in candidates
            if article in clean_text(item.get("composite_sku")).lower()
            or article == clean_text(item.get("ean")).lower()
        ]
        if len(matches) == 1:
            return matches[0], "Ordine + codice articolo"
    return None, "Ordine con più righe: EAN/codice articolo insufficiente"


def analyze_supplier_documents(
    records: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
    *,
    marketplace: str,
    supplier_hint: str = "Automatico",
) -> dict[str, Any]:
    by_order: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_order.setdefault(_normalized_order_id(record.get("order_id")), []).append(record)

    document_results: list[dict[str, Any]] = []
    all_references: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for document in documents:
        try:
            parsed = parse_supplier_document(document, records, supplier_hint=supplier_hint)
            document_results.append({
                "Documento": parsed["file_name"],
                "Formato": parsed["kind"].upper(),
                "Origine": parsed["source"],
                "Dimensione KB": round(parsed["size_bytes"] / 1024, 1),
                "Riferimenti": len(parsed["references"]),
                "Dettaglio": parsed["details"],
            })
            all_references.extend(parsed["references"])
        except Exception as exc:
            name = clean_text(document.get("file_name")) or "documento"
            parse_errors.append({"Documento": name, "Errore": str(exc)})
            document_results.append({
                "Documento": name,
                "Formato": clean_text(document.get("kind")).upper(),
                "Origine": clean_text(document.get("source")) or "File",
                "Dimensione KB": round(len(document.get("content") or b"") / 1024, 1),
                "Riferimenti": 0,
                "Dettaglio": f"Errore: {exc}",
            })

    proposals: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    updates_by_key: dict[str, dict[str, Any]] = {}

    for reference in all_references:
        order_id = clean_text(reference.get("order_id"))
        candidates = by_order.get(_normalized_order_id(order_id), [])
        selected, match_method = _record_candidate_for_reference(candidates, reference)
        base = {
            "Documento": reference.get("document_name"),
            "Ordine marketplace": order_id,
            "N Ordine Fornitore": reference.get("supplier_order_number"),
            "Fornitore rilevato": reference.get("supplier") or supplier_hint,
            "Codice articolo": reference.get("article"),
            "Posizione": reference.get("location"),
            "Metodo estrazione": reference.get("method"),
            "Confidenza": reference.get("confidence"),
            "Estratto": reference.get("excerpt"),
        }
        if selected is None:
            event = {**base, "Esito": match_method}
            if candidates:
                ambiguous.append(event)
            else:
                unmatched.append(event)
            continue
        row_key = clean_text(selected.get("row_key"))
        incoming = clean_text(reference.get("supplier_order_number"))
        current = clean_text(selected.get("supplier_order_number"))
        proposal = {
            **base,
            "row_key": row_key,
            "Prodotto": clean_text(selected.get("product_title")),
            "EAN contabilità": clean_text(selected.get("ean")),
            "Valore attuale": current,
            "Metodo abbinamento": match_method,
        }
        if current and current.casefold() == incoming.casefold():
            proposal["Esito"] = "Già presente e uguale"
        elif current:
            proposal["Esito"] = "Conflitto: valore già presente"
            conflicts.append(proposal)
        else:
            proposal["Esito"] = "Pronto da compilare"
            existing = updates_by_key.get(row_key)
            if existing and clean_text(existing.get("supplier_order_number")).casefold() != incoming.casefold():
                proposal["Esito"] = "Conflitto tra documenti"
                conflicts.append(proposal)
                updates_by_key.pop(row_key, None)
            else:
                updates_by_key[row_key] = {
                    "row_key": row_key,
                    "supplier_order_number": incoming,
                    "document_name": reference.get("document_name"),
                    "source_location": reference.get("location"),
                }
        proposals.append(proposal)

    return {
        "summary": {
            "documents": len(documents),
            "parsed_documents": len(documents) - len(parse_errors),
            "references_found": len(all_references),
            "matched_rows": len(proposals),
            "update_rows": len(updates_by_key),
            "conflicts": len(conflicts),
            "unmatched_rows": len(unmatched),
            "ambiguous_rows": len(ambiguous),
            "parse_errors": len(parse_errors),
        },
        "documents": document_results,
        "references": all_references,
        "proposals": proposals,
        "updates": list(updates_by_key.values()),
        "conflicts": conflicts,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "errors": parse_errors,
    }


def apply_supplier_document_updates(
    seller_id: int,
    account_id: int,
    marketplace: str,
    updates: Iterable[Mapping[str, Any]],
    *,
    source_names: Sequence[str],
    analysis_summary: Mapping[str, Any],
    replace_existing: bool = False,
) -> dict[str, int]:
    update_items = list(updates)
    updated = skipped = 0
    with connect() as con:
        for item in update_items:
            row_key = clean_text(item.get("row_key"))
            incoming = clean_text(item.get("supplier_order_number"))
            if not row_key or not incoming:
                continue
            current_row = con.execute(
                """SELECT supplier_order_number FROM accounting_order_lines
                WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND row_key=?""",
                (seller_id, account_id, clean_text(marketplace).lower(), row_key),
            ).fetchone()
            if current_row is None:
                skipped += 1
                continue
            current = clean_text(current_row["supplier_order_number"])
            if current and not replace_existing:
                skipped += 1
                continue
            con.execute(
                """UPDATE accounting_order_lines SET supplier_order_number=?
                WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND row_key=?""",
                (incoming, seller_id, account_id, clean_text(marketplace).lower(), row_key),
            )
            updated += 1
        con.execute(
            """INSERT INTO accounting_supplier_document_imports(
                seller_id,marketplace_account_id,marketplace,source_names,
                document_count,references_found,matched_rows,updated_rows,
                conflicts,unmatched_rows,ambiguous_rows,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seller_id, account_id, clean_text(marketplace).lower(),
                " | ".join(clean_text(value) for value in source_names if clean_text(value))[:4000],
                int(analysis_summary.get("documents") or 0),
                int(analysis_summary.get("references_found") or 0),
                int(analysis_summary.get("matched_rows") or 0),
                updated,
                int(analysis_summary.get("conflicts") or 0),
                int(analysis_summary.get("unmatched_rows") or 0),
                int(analysis_summary.get("ambiguous_rows") or 0),
                json_text(dict(analysis_summary)),
                now_iso(),
            ),
        )
    return {"updated_rows": updated, "skipped_rows": skipped}


def supplier_document_import_history(seller_id: int, account_id: int, marketplace: str) -> list[dict[str, Any]]:
    return rows(
        """SELECT * FROM accounting_supplier_document_imports
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        ORDER BY id DESC LIMIT 100""",
        (seller_id, account_id, clean_text(marketplace).lower()),
    )


def supplier_document_report_bytes(analysis: Mapping[str, Any]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        sheets = (
            ("Abbinamenti", analysis.get("proposals") or []),
            ("Conflitti", analysis.get("conflicts") or []),
            ("Non abbinati", analysis.get("unmatched") or []),
            ("Ambigui", analysis.get("ambiguous") or []),
            ("Documenti", analysis.get("documents") or []),
            ("Errori", analysis.get("errors") or []),
        )
        for sheet_name, items in sheets:
            frame = pd.DataFrame(items)
            if frame.empty:
                frame = pd.DataFrame({"Esito": ["Nessun dato"]})
            frame.drop(columns=["row_key"], errors="ignore").to_excel(
                writer, sheet_name=sheet_name[:31], index=False
            )
            worksheet = writer.sheets[sheet_name[:31]]
            worksheet.freeze_panes(1, 0)
            worksheet.autofilter(0, 0, max(0, len(frame)), max(0, len(frame.columns) - 1))
            for col_index, column in enumerate(frame.columns):
                width = min(55, max(12, len(str(column)) + 2))
                worksheet.set_column(col_index, col_index, width)
    return output.getvalue()
