from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from xml.etree import ElementTree

import pandas as pd

from services.cecotec_orders import clean_text

SUPPORTED_ORDER_DOCUMENT_EXTENSIONS = {
    ".txt", ".csv", ".tsv", ".rtf",
    ".xlsx", ".xls",
    ".docx", ".doc",
    ".pdf",
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
}


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _excel_text(content: bytes, file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    engine = "xlrd" if suffix == ".xls" else "openpyxl"
    book = pd.ExcelFile(io.BytesIO(content), engine=engine)
    chunks: list[str] = []
    try:
        for sheet_name in book.sheet_names:
            frame = pd.read_excel(book, sheet_name=sheet_name, header=None, dtype=object)
            chunks.append(f"[Foglio {sheet_name}]")
            for row in frame.fillna("").astype(str).itertuples(index=False, name=None):
                line = "\t".join(clean_text(value) for value in row if clean_text(value))
                if line:
                    chunks.append(line)
    finally:
        close = getattr(book, "close", None)
        if callable(close):
            close()
    return "\n".join(chunks)


def _docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            parts = []
            for name in archive.namelist():
                if not name.startswith("word/") or not name.endswith(".xml"):
                    continue
                if not any(token in name for token in ("document.xml", "header", "footer", "footnotes", "endnotes")):
                    continue
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except Exception:
                    continue
                text_nodes = []
                for node in root.iter():
                    if node.tag.endswith("}t") and node.text:
                        text_nodes.append(node.text)
                    elif node.tag.endswith("}tab"):
                        text_nodes.append("\t")
                    elif node.tag.endswith("}br") or node.tag.endswith("}cr"):
                        text_nodes.append("\n")
                if text_nodes:
                    parts.append(" ".join(text_nodes))
            return "\n".join(parts)
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Il file Word DOCX non è valido o è danneggiato.") from exc


def _legacy_doc_text(content: bytes) -> str:
    """Best-effort extraction for legacy .doc files.

    Binary Word files do not have a stable pure-Python text contract. For the
    Packlink workflow we only need order identifiers, therefore printable ASCII
    and UTF-16LE runs are enough for many legacy documents. DOCX remains the
    recommended Word format.
    """
    ascii_runs = [match.decode("cp1252", errors="ignore") for match in re.findall(rb"[\x20-\x7e]{4,}", content)]
    utf16_runs = []
    for match in re.findall(rb"(?:[\x20-\x7e]\x00){4,}", content):
        try:
            utf16_runs.append(match.decode("utf-16le", errors="ignore"))
        except Exception:
            pass
    return "\n".join(ascii_runs + utf16_runs)


def _ocr_image_text(content: bytes) -> str:
    try:
        from PIL import Image
        import pytesseract
    except Exception as exc:  # pragma: no cover - optional runtime dependency
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
            "Tesseract OCR non è installato. È necessario per leggere screenshot e PDF scansiti."
        )
    pytesseract.pytesseract.tesseract_cmd = executable
    image = Image.open(io.BytesIO(content))
    # Order identifiers are mostly digits/ASCII. English OCR is intentionally a
    # safe fallback even when language packs ITA/POR are not installed.
    try:
        return pytesseract.image_to_string(image, lang="ita+por+eng")
    except Exception:
        return pytesseract.image_to_string(image, lang="eng")


def _pdf_text(content: bytes) -> tuple[str, int]:
    try:
        import fitz
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF non è installato: impossibile leggere il PDF.") from exc
    document = fitz.open(stream=content, filetype="pdf")
    parts: list[str] = []
    ocr_pages = 0
    try:
        for page_index, page in enumerate(document):
            text = page.get_text("text") or ""
            if len(re.sub(r"\s+", "", text)) < 15:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                    text = _ocr_image_text(pix.tobytes("png"))
                    ocr_pages += 1
                except Exception:
                    pass
            if text.strip():
                parts.append(f"[Pagina {page_index + 1}]\n{text}")
    finally:
        document.close()
    return "\n".join(parts), ocr_pages


def extract_order_document_text(content: bytes, file_name: str) -> tuple[str, str]:
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_ORDER_DOCUMENT_EXTENSIONS:
        raise ValueError(f"Formato non supportato: {suffix or file_name}")
    if suffix in {".txt", ".csv", ".tsv", ".rtf"}:
        return _decode_text(content), "testo"
    if suffix in {".xlsx", ".xls"}:
        return _excel_text(content, file_name), "Excel"
    if suffix == ".docx":
        return _docx_text(content), "Word DOCX"
    if suffix == ".doc":
        return _legacy_doc_text(content), "Word DOC (lettura compatibilità)"
    if suffix == ".pdf":
        text, ocr_pages = _pdf_text(content)
        return text, f"PDF · OCR {ocr_pages} pagine"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        return _ocr_image_text(content), "immagine OCR"
    raise ValueError(f"Formato non supportato: {suffix or file_name}")


def normalize_order_identifier(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", clean_text(value).upper())


def _line_matches_order(line: str, order_id: str) -> bool:
    raw = clean_text(order_id)
    if not raw:
        return False
    # Exact representation first, with boundaries only around alphanumeric
    # characters so IDs containing dashes/slashes remain matchable.
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(raw)}(?![A-Za-z0-9])", line, re.IGNORECASE):
        return True
    normalized_order = normalize_order_identifier(raw)
    if len(normalized_order) < 5:
        return False
    normalized_line = normalize_order_identifier(line)
    return normalized_order in normalized_line


def _candidate_tokens(text: str) -> list[str]:
    found: list[str] = []
    patterns = (
        r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9._/#-]{5,}(?![A-Za-z0-9])",
        r"(?<!\d)\d{6,}(?!\d)",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text):
            value = clean_text(match).strip(".,;:()[]{}")
            if not value:
                continue
            normalized = normalize_order_identifier(value)
            if len(normalized) < 6:
                continue
            # Ignore obvious ISO dates / timestamps.
            if re.fullmatch(r"20\d{6}", normalized):
                continue
            if value not in found:
                found.append(value)
    return found[:500]


def match_order_documents(
    documents: Sequence[tuple[str, bytes]],
    orders: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Read uploaded documents and match their order numbers to cached orders."""
    order_rows = [dict(item) for item in orders if clean_text(item.get("order_key")) and clean_text(item.get("order_id"))]
    matches_by_key: dict[str, dict[str, Any]] = {}
    file_reports: list[dict[str, Any]] = []
    errors: list[str] = []
    all_candidates: list[str] = []
    matched_candidate_norms: set[str] = set()

    for file_name, content in documents:
        try:
            text, method = extract_order_document_text(content, file_name)
        except Exception as exc:
            errors.append(f"{file_name}: {exc}")
            file_reports.append({"file": file_name, "method": "errore", "matches": 0, "error": str(exc)})
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        file_match_count = 0
        for order in order_rows:
            order_id = clean_text(order.get("order_id"))
            if any(_line_matches_order(line, order_id) for line in lines):
                key = clean_text(order.get("order_key"))
                if key not in matches_by_key:
                    matches_by_key[key] = {
                        "order_key": key,
                        "order_id": order_id,
                        "marketplace": clean_text(order.get("marketplace")),
                        "account_name": clean_text(order.get("account_name")),
                        "source_files": [],
                    }
                if file_name not in matches_by_key[key]["source_files"]:
                    matches_by_key[key]["source_files"].append(file_name)
                file_match_count += 1
                matched_candidate_norms.add(normalize_order_identifier(order_id))
        candidates = _candidate_tokens(text)
        for candidate in candidates:
            if candidate not in all_candidates:
                all_candidates.append(candidate)
        file_reports.append({"file": file_name, "method": method, "matches": file_match_count, "error": ""})

    unmatched_candidates = [
        value for value in all_candidates
        if normalize_order_identifier(value) not in matched_candidate_norms
    ][:200]
    matches = sorted(
        matches_by_key.values(),
        key=lambda item: (item.get("marketplace", ""), item.get("order_id", "")),
    )
    return {
        "matches": matches,
        "matched_order_keys": [item["order_key"] for item in matches],
        "matched_order_ids": [item["order_id"] for item in matches],
        "unmatched_candidates": unmatched_candidates,
        "files": file_reports,
        "errors": errors,
    }
