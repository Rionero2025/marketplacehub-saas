from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import re
import socket
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import unquote, urljoin, urlparse

import requests

import pandas as pd

from services.cecotec_orders import clean_identifier, clean_text, parse_composite_sku
from services.db import connect, execute, now_iso, rows
from services.tracking_shipping_rules import (
    CANCELLED_FILE_STATUSES,
    SHIPPED_FILE_STATUSES,
    WAITING_FILE_STATUSES,
    WAREHOUSE_READY_STATUSES,
    apply_worten_eligibility,
    normalize_status,
    split_tracking_numbers,
)


READY_FILE_STATUSES = SHIPPED_FILE_STATUSES | WAREHOUSE_READY_STATUSES


@dataclass(frozen=True)
class ParsedTrackingFile:
    supplier: str
    confidence: float
    source_format: str
    rows: list[dict[str, Any]]
    notes: list[str]


@dataclass(frozen=True)
class DownloadedTrackingFile:
    file_name: str
    content: bytes
    source_url: str
    mime_type: str


SUPPORTED_TRACKING_FILE_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".txt", ".pdf"
}
TRACKING_FILE_MAX_BYTES = 25 * 1024 * 1024
TRACKING_URL_MAX_REDIRECTS = 5


def ensure_schema() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS tracking_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                supplier TEXT NOT NULL DEFAULT '',
                file_name TEXT NOT NULL DEFAULT '',
                file_sha256 TEXT NOT NULL DEFAULT '',
                source_format TEXT NOT NULL DEFAULT '',
                total_rows INTEGER NOT NULL DEFAULT 0,
                matched_rows INTEGER NOT NULL DEFAULT 0,
                ambiguous_rows INTEGER NOT NULL DEFAULT 0,
                unmatched_rows INTEGER NOT NULL DEFAULT 0,
                ready_rows INTEGER NOT NULL DEFAULT 0,
                waiting_rows INTEGER NOT NULL DEFAULT 0,
                cancelled_rows INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tracking_imports_scope
            ON tracking_imports(seller_id,marketplace_account_id,marketplace,created_at);

            CREATE TABLE IF NOT EXISTS tracking_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                import_id INTEGER REFERENCES tracking_imports(id) ON DELETE CASCADE,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                supplier TEXT NOT NULL DEFAULT '',
                source_row INTEGER NOT NULL DEFAULT 0,
                source_reference TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                order_line_ids_json TEXT NOT NULL DEFAULT '[]',
                customer_name_file TEXT NOT NULL DEFAULT '',
                customer_name_order TEXT NOT NULL DEFAULT '',
                product_file TEXT NOT NULL DEFAULT '',
                file_status TEXT NOT NULL DEFAULT '',
                operational_status TEXT NOT NULL DEFAULT '',
                tracking TEXT NOT NULL DEFAULT '',
                carrier TEXT NOT NULL DEFAULT '',
                match_status TEXT NOT NULL DEFAULT '',
                match_score REAL NOT NULL DEFAULT 0,
                match_reason TEXT NOT NULL DEFAULT '',
                api_status TEXT NOT NULL DEFAULT '',
                api_message TEXT NOT NULL DEFAULT '',
                api_sent_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tracking_matches_scope
            ON tracking_matches(
                seller_id,marketplace_account_id,marketplace,order_id,created_at
            );

            CREATE TABLE IF NOT EXISTS tracking_source_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                supplier TEXT NOT NULL DEFAULT '',
                file_name TEXT NOT NULL,
                file_sha256 TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'upload'
                    CHECK(source_type IN ('upload','url')),
                source_url TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                content BLOB NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL DEFAULT '',
                use_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(
                    seller_id,marketplace_account_id,marketplace,file_sha256
                )
            );
            CREATE INDEX IF NOT EXISTS idx_tracking_source_files_scope
            ON tracking_source_files(
                seller_id,marketplace_account_id,marketplace,created_at
            );

            CREATE TABLE IF NOT EXISTS tracking_import_files (
                import_id INTEGER NOT NULL
                    REFERENCES tracking_imports(id) ON DELETE CASCADE,
                tracking_file_id INTEGER NOT NULL
                    REFERENCES tracking_source_files(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(import_id,tracking_file_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tracking_import_files_file
            ON tracking_import_files(tracking_file_id,import_id);
            """
        )



def _safe_tracking_file_name(value: object, default: str = "spedizioni") -> str:
    raw = unquote(clean_text(value)).replace("\\", "/").rsplit("/", 1)[-1]
    raw = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", raw).strip(" .")
    if not raw:
        raw = default
    suffix = Path(raw).suffix.lower()
    if suffix not in SUPPORTED_TRACKING_FILE_EXTENSIONS:
        return raw
    return f"{Path(raw).stem[:160]}{suffix}"


def _extension_from_content_type(content_type: object) -> str:
    normalized = clean_text(content_type).split(";", 1)[0].strip().lower()
    mapping = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-excel": ".xls",
        "text/csv": ".csv",
        "text/tab-separated-values": ".tsv",
        "text/plain": ".txt",
        "application/pdf": ".pdf",
    }
    if normalized in mapping:
        return mapping[normalized]
    guessed = mimetypes.guess_extension(normalized) or ""
    return guessed if guessed in SUPPORTED_TRACKING_FILE_EXTENSIONS else ""


def _content_disposition_file_name(value: object) -> str:
    header = clean_text(value)
    if not header:
        return ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", header, flags=re.I)
    if match:
        return unquote(match.group(1).strip().strip('"'))
    match = re.search(r"filename\s*=\s*(?:\"([^\"]+)\"|([^;]+))", header, flags=re.I)
    if not match:
        return ""
    return clean_text(match.group(1) or match.group(2)).strip('"')


def validate_tracking_file_url(
    url: object,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> str:
    """Validate a public HTTP(S) URL and reject local/private destinations."""
    value = clean_text(url)
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("L'URL deve iniziare con http:// oppure https://.")
    if not parsed.hostname:
        raise ValueError("L'URL non contiene un host valido.")
    if parsed.username or parsed.password:
        raise ValueError("Non sono ammesse credenziali incorporate nell'URL.")
    try:
        addresses = resolver(parsed.hostname, parsed.port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Impossibile risolvere l'host del file: {exc}") from exc
    if not addresses:
        raise ValueError("L'host del file non restituisce alcun indirizzo IP.")
    for item in addresses:
        address = item[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("L'host del file restituisce un indirizzo non valido.") from exc
        if not ip.is_global:
            raise ValueError(
                "L'URL punta a una rete locale, privata o riservata e non può essere scaricato."
            )
    return value


def download_tracking_file_from_url(
    url: object,
    *,
    timeout: float = 30.0,
    max_bytes: int = TRACKING_FILE_MAX_BYTES,
    session: requests.Session | None = None,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> DownloadedTrackingFile:
    """Download one public shipment file with redirect and size protections."""
    client = session or requests.Session()
    current_url = validate_tracking_file_url(url, resolver=resolver)
    response = None
    for _ in range(TRACKING_URL_MAX_REDIRECTS + 1):
        response = client.get(
            current_url,
            stream=True,
            allow_redirects=False,
            timeout=max(5.0, float(timeout)),
            headers={"User-Agent": "MarketplaceHub/1.0 shipment-file-import"},
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = clean_text(response.headers.get("Location"))
            response.close()
            if not location:
                raise ValueError("Il server ha restituito un reindirizzamento senza destinazione.")
            current_url = validate_tracking_file_url(
                urljoin(current_url, location), resolver=resolver
            )
            continue
        break
    else:  # pragma: no cover - loop is bounded explicitly
        raise ValueError("Troppi reindirizzamenti durante il download del file.")
    if response is None:
        raise ValueError("Il file non è stato scaricato.")
    try:
        response.raise_for_status()
        declared_size = int(clean_text(response.headers.get("Content-Length")) or 0)
        if declared_size > max_bytes:
            raise ValueError(
                f"Il file supera il limite consentito di {max_bytes // (1024 * 1024)} MB."
            )
        buffer = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise ValueError(
                    f"Il file supera il limite consentito di {max_bytes // (1024 * 1024)} MB."
                )
        content = bytes(buffer)
        if not content:
            raise ValueError("Il file scaricato è vuoto.")
        mime_type = clean_text(response.headers.get("Content-Type")).split(";", 1)[0]
        header_name = _content_disposition_file_name(
            response.headers.get("Content-Disposition")
        )
        url_name = Path(urlparse(current_url).path).name
        file_name = _safe_tracking_file_name(header_name or url_name)
        suffix = Path(file_name).suffix.lower()
        if suffix not in SUPPORTED_TRACKING_FILE_EXTENSIONS:
            inferred = _extension_from_content_type(mime_type)
            if not inferred:
                raise ValueError(
                    "Il formato del file URL non è riconosciuto. Usa XLSX, XLS, CSV, TSV, TXT o PDF."
                )
            file_name = f"{Path(file_name).stem or 'spedizioni'}{inferred}"
        return DownloadedTrackingFile(
            file_name=file_name,
            content=content,
            source_url=current_url,
            mime_type=mime_type,
        )
    finally:
        response.close()


def archive_tracking_file(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    file_name: str,
    content: bytes,
    source_type: str = "upload",
    source_url: str = "",
    mime_type: str = "",
    supplier: str = "",
) -> dict[str, Any]:
    """Persist a shipment source file in SQLite and deduplicate by SHA-256."""
    ensure_schema()
    payload = bytes(content or b"")
    if not payload:
        raise ValueError("Il file spedizioni è vuoto.")
    if len(payload) > TRACKING_FILE_MAX_BYTES:
        raise ValueError(
            f"Il file supera il limite consentito di {TRACKING_FILE_MAX_BYTES // (1024 * 1024)} MB."
        )
    normalized_source = clean_text(source_type).lower() or "upload"
    if normalized_source not in {"upload", "url"}:
        raise ValueError("Origine file non valida.")
    safe_name = _safe_tracking_file_name(file_name)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_TRACKING_FILE_EXTENSIONS:
        raise ValueError("Formato non supportato. Usa XLSX, XLS, CSV, TSV, TXT o PDF.")
    digest = hashlib.sha256(payload).hexdigest()
    scope = (
        int(seller_id), int(account_id), clean_text(marketplace).lower(), digest
    )
    existing = rows(
        """SELECT * FROM tracking_source_files
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND file_sha256=? LIMIT 1""",
        scope,
    )
    if existing:
        item = existing[0]
        with connect() as con:
            con.execute(
                """UPDATE tracking_source_files
                SET file_name=?,supplier=CASE WHEN TRIM(?)<>'' THEN ? ELSE supplier END,
                    source_type=CASE WHEN ?='url' THEN 'url' ELSE source_type END,
                    source_url=CASE WHEN TRIM(?)<>'' THEN ? ELSE source_url END,
                    mime_type=CASE WHEN TRIM(?)<>'' THEN ? ELSE mime_type END
                WHERE id=?""",
                (
                    safe_name, supplier, supplier, normalized_source,
                    source_url, source_url, mime_type, mime_type, int(item["id"]),
                ),
            )
        return archived_tracking_file(
            int(item["id"]), seller_id=seller_id, account_id=account_id
        ) or item
    file_id = execute(
        """INSERT INTO tracking_source_files(
            seller_id,marketplace_account_id,marketplace,supplier,file_name,
            file_sha256,source_type,source_url,mime_type,size_bytes,content,
            created_at,last_used_at,use_count
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            int(seller_id), int(account_id), clean_text(marketplace).lower(),
            clean_text(supplier), safe_name, digest, normalized_source,
            clean_text(source_url), clean_text(mime_type), len(payload),
            payload, now_iso(), "", 0,
        ),
    )
    return archived_tracking_file(
        file_id, seller_id=seller_id, account_id=account_id
    ) or {"id": file_id, "file_name": safe_name, "content": payload}


def archived_tracking_files(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_schema()
    return rows(
        """SELECT id,seller_id,marketplace_account_id,marketplace,supplier,
            file_name,file_sha256,source_type,source_url,mime_type,size_bytes,
            created_at,last_used_at,use_count
        FROM tracking_source_files
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        ORDER BY id DESC LIMIT ?""",
        (
            int(seller_id), int(account_id), clean_text(marketplace).lower(),
            max(1, int(limit)),
        ),
    )


def archived_tracking_file(
    file_id: int,
    *,
    seller_id: int,
    account_id: int,
) -> dict[str, Any] | None:
    ensure_schema()
    found = rows(
        """SELECT * FROM tracking_source_files
        WHERE id=? AND seller_id=? AND marketplace_account_id=? LIMIT 1""",
        (int(file_id), int(seller_id), int(account_id)),
    )
    return found[0] if found else None


def update_archived_tracking_file_supplier(
    file_ids: Sequence[int], supplier: object
) -> None:
    normalized_ids = sorted({int(item) for item in file_ids if int(item) > 0})
    if not normalized_ids:
        return
    placeholders = ",".join("?" for _ in normalized_ids)
    with connect() as con:
        con.execute(
            f"UPDATE tracking_source_files SET supplier=? WHERE id IN ({placeholders})",
            (clean_text(supplier), *normalized_ids),
        )


def delete_archived_tracking_files(
    file_ids: Sequence[int],
    *,
    seller_id: int,
    account_id: int,
) -> int:
    normalized_ids = sorted({int(item) for item in file_ids if int(item) > 0})
    if not normalized_ids:
        return 0
    placeholders = ",".join("?" for _ in normalized_ids)
    with connect() as con:
        cursor = con.execute(
            f"""DELETE FROM tracking_source_files
            WHERE seller_id=? AND marketplace_account_id=?
              AND id IN ({placeholders})""",
            (int(seller_id), int(account_id), *normalized_ids),
        )
        return max(0, int(cursor.rowcount or 0))



def _norm(value: object) -> str:
    text = clean_text(value)
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", " ", normalized.upper()).strip()


def normalized_customer_name(value: object) -> str:
    tokens = [token for token in _norm(value).split() if token]
    return " ".join(sorted(tokens))


def _digits(value: object) -> str:
    return re.sub(r"\D+", "", clean_text(value))


def _extract_eans(*values: object) -> list[str]:
    found: list[str] = []
    for value in values:
        for token in re.findall(r"(?<!\d)(\d{8,14})(?!\d)", clean_text(value)):
            if token not in found:
                found.append(token)
    return found


def _to_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    try:
        parsed = pd.to_datetime(value, errors="coerce", utc=True)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def classify_file_status(
    status: object,
    tracking: object = "",
    carrier: object = "",
    supplier: object = "",
) -> str:
    normalized = normalize_status(status)
    tracking_text = clean_text(tracking)
    carrier_text = clean_text(carrier)
    supplier_text = clean_text(supplier).casefold()
    is_cecotec = supplier_text == "cecotec" or supplier_text.startswith("cecotec ")
    if normalized in CANCELLED_FILE_STATUSES:
        return "Annullata nel file"
    if normalized in WAITING_FILE_STATUSES:
        if is_cecotec and tracking_text and carrier_text:
            return f"{normalized} · tracking disponibile"
        return "In attesa di spedizione"
    if normalized in WAREHOUSE_READY_STATUSES:
        return "Spedita · tracking disponibile" if tracking_text and carrier_text else "In attesa di spedizione"
    if normalized in SHIPPED_FILE_STATUSES:
        return "Spedita · tracking disponibile" if tracking_text else "Spedita · tracking mancante"
    if tracking_text and carrier_text:
        return "Spedita · tracking disponibile"
    return "Stato da verificare"


def _cecotec_row(values: Sequence[Any], row_number: int) -> dict[str, Any]:
    def cell(index: int) -> Any:
        return values[index] if index < len(values) else ""

    tracking = clean_text(cell(3))
    carrier = clean_text(cell(6)).replace("_", " ")
    status = clean_text(cell(8)) or clean_text(cell(28))
    product = clean_text(cell(9))
    customer = clean_text(cell(2))
    eans = _extract_eans(product)
    return {
        "source_row": row_number,
        "source_reference": clean_text(cell(0)) or clean_text(cell(18)),
        "supplier_order_reference": clean_text(cell(16)),
        "marketplace_order_reference": clean_text(cell(12)),
        "customer_name": customer,
        "tracking": tracking,
        "carrier": carrier,
        "file_status": status,
        "operational_status": classify_file_status(status, tracking, carrier, "Cecotec"),
        "product": product,
        "product_code": clean_text(product.split("(", 1)[0]) if product else "",
        "eans": eans,
        "email": clean_text(cell(19)),
        "phone": _digits(cell(14)),
        "address": clean_text(cell(20)),
        "created_at": clean_text(cell(21)),
        "shipped_at": clean_text(cell(25)),
        "raw": list(values),
    }


def _is_probable_cecotec(frame: pd.DataFrame) -> bool:
    if frame.shape[1] < 20:
        return False
    sample = frame.head(50).fillna("").astype(str)
    statuses = " ".join(sample.iloc[:, 8].tolist()).upper() if frame.shape[1] > 8 else ""
    references = sample.iloc[:, 0].tolist() if frame.shape[1] else []
    ref_hits = sum(bool(re.match(r"^D\d+", value.strip(), re.I)) for value in references)
    status_hits = sum(token in statuses for token in ("WAITING LABEL", "IN TRANSIT", "CANCELLED", "SENT TO WAREHOUSE"))
    return ref_hits >= 2 and status_hits >= 1


def _read_spreadsheet(content: bytes, file_name: str) -> pd.DataFrame:
    suffix = Path(file_name).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(io.BytesIO(content), header=None, dtype=object)
    if suffix in {".csv", ".tsv", ".txt"}:
        separator = "\t" if suffix == ".tsv" else None
        return pd.read_csv(
            io.BytesIO(content),
            header=None,
            dtype=object,
            sep=separator,
            engine="python",
            encoding_errors="replace",
        )
    raise ValueError(f"Formato tabellare non supportato: {suffix or file_name}")


def _read_pdf_text(content: bytes) -> list[str]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyMuPDF non è installato: impossibile leggere il PDF.") from exc
    document = fitz.open(stream=content, filetype="pdf")
    try:
        lines: list[str] = []
        for page in document:
            lines.extend(line.strip() for line in page.get_text("text").splitlines() if line.strip())
        return lines
    finally:
        document.close()


def _parse_generic_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    values = frame.fillna("").astype(str)
    if values.empty:
        return []
    first = [_norm(value).replace(" ", "_") for value in values.iloc[0].tolist()]
    aliases = {
        "customer_name": {"CUSTOMER_NAME", "CUSTOMER", "CLIENTE", "NOME_CLIENTE", "DESTINATARIO"},
        "tracking": {"TRACKING", "TRACKING_NUMBER", "TRACKING_CODE", "NUMERO_TRACKING"},
        "carrier": {"CARRIER", "CORRIERE", "TRANSPORTER", "SPEDIZIONIERE"},
        "status": {"STATUS", "STATO", "SHIPMENT_STATUS"},
        "product": {"PRODUCT", "PRODOTTO", "DESCRIPTION", "DESCRIZIONE"},
        "ean": {"EAN", "GTIN", "BARCODE"},
        "sku": {"SKU", "PRODUCT_CODE", "CODICE_PRODOTTO"},
        "order": {"ORDER_ID", "NUMERO_ORDINE", "ORDINE", "MARKETPLACE_ORDER"},
        "email": {"EMAIL", "E_MAIL"},
        "phone": {"PHONE", "TELEFONO", "MOBILE"},
        "address": {"ADDRESS", "INDIRIZZO"},
    }
    mapping: dict[str, int] = {}
    for index, header in enumerate(first):
        for field, names in aliases.items():
            if header in names and field not in mapping:
                mapping[field] = index
    has_header = len(mapping) >= 2
    data = values.iloc[1:] if has_header else values
    output: list[dict[str, Any]] = []
    for offset, (_, row) in enumerate(data.iterrows(), start=2 if has_header else 1):
        def get(field: str) -> str:
            index = mapping.get(field)
            return clean_text(row.iloc[index]) if index is not None and index < len(row) else ""

        customer = get("customer_name")
        tracking = get("tracking")
        carrier = get("carrier")
        status = get("status")
        product = get("product")
        eans = _extract_eans(get("ean"), product)
        if not any((customer, tracking, carrier, status, product, get("order"))):
            continue
        output.append({
            "source_row": offset,
            "source_reference": get("order") or f"riga-{offset}",
            "supplier_order_reference": "",
            "marketplace_order_reference": get("order"),
            "customer_name": customer,
            "tracking": tracking,
            "carrier": carrier,
            "file_status": status,
            "operational_status": classify_file_status(status, tracking, carrier),
            "product": product,
            "product_code": get("sku"),
            "eans": eans,
            "email": get("email"),
            "phone": _digits(get("phone")),
            "address": get("address"),
            "created_at": "",
            "shipped_at": "",
            "raw": row.tolist(),
        })
    return output


def parse_tracking_document(content: bytes, file_name: str) -> ParsedTrackingFile:
    suffix = Path(file_name).suffix.lower()
    notes: list[str] = []
    if suffix in {".xlsx", ".xls", ".csv", ".tsv", ".txt"}:
        frame = _read_spreadsheet(content, file_name)
        if _is_probable_cecotec(frame):
            parsed = [_cecotec_row(row.tolist(), index + 1) for index, (_, row) in enumerate(frame.iterrows())]
            parsed = [item for item in parsed if any((item["customer_name"], item["tracking"], item["product"]))]
            return ParsedTrackingFile("Cecotec", 1.0, "cecotec-expedition", parsed, notes)
        return ParsedTrackingFile("", 0.0, "tabellare-generico", _parse_generic_frame(frame), notes)
    if suffix == ".pdf":
        lines = _read_pdf_text(content)
        notes.append("PDF letto come testo; per file scansionati può essere necessaria una mappatura manuale.")
        frame = pd.DataFrame([[line] for line in lines])
        return ParsedTrackingFile("", 0.0, "pdf-testo", _parse_generic_frame(frame), notes)
    raise ValueError("Formato non supportato. Usa XLSX, XLS, CSV, TSV, TXT o PDF.")


def _order_raw(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("raw_json")
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _order_contact_fields(item: Mapping[str, Any]) -> tuple[str, str, str]:
    raw = _order_raw(item)
    text = json.dumps(raw, ensure_ascii=False)
    emails = re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, flags=re.I)
    phones = re.findall(r"(?<!\d)\+?\d[\d\s().-]{6,}\d(?!\d)", text)
    addresses: list[str] = []
    for key in ("shipping_address", "delivery_address", "address"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            addresses.append(" ".join(clean_text(child) for child in value.values()))
    return (
        clean_text(emails[0]) if emails else "",
        _digits(phones[0]) if phones else "",
        clean_text(" ".join(addresses)),
    )


def _line_product_tokens(item: Mapping[str, Any]) -> set[str]:
    parsed = parse_composite_sku(clean_text(item.get("composite_sku")))
    values = [
        item.get("ean"), item.get("composite_sku"), item.get("product_title"),
        parsed.product_code, parsed.supplier,
    ]
    tokens: set[str] = set()
    for value in values:
        normalized = _norm(value)
        tokens.update(token for token in normalized.split() if len(token) >= 3)
    return tokens


def supplier_names_from_orders(orders: Sequence[Mapping[str, Any]]) -> list[str]:
    names = {clean_text(item.get("supplier")) for item in orders if clean_text(item.get("supplier"))}
    return sorted(names, key=str.casefold)


def detect_supplier_from_orders(
    file_rows: Sequence[Mapping[str, Any]],
    orders: Sequence[Mapping[str, Any]],
    parsed_supplier: str = "",
) -> tuple[str, float, list[dict[str, Any]]]:
    if parsed_supplier:
        return parsed_supplier, 1.0, [{"supplier": parsed_supplier, "score": 100.0, "reason": "Formato file riconosciuto"}]
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for order in orders:
        supplier = clean_text(order.get("supplier"))
        if supplier:
            grouped[supplier].append(order)
    ranking: list[dict[str, Any]] = []
    file_eans = {ean for row in file_rows for ean in row.get("eans", [])}
    file_tokens = set()
    for row in file_rows:
        file_tokens.update(token for token in _norm(row.get("product")).split() if len(token) >= 3)
        file_tokens.update(token for token in _norm(row.get("product_code")).split() if len(token) >= 3)
    for supplier, supplier_orders in grouped.items():
        supplier_eans = {clean_identifier(item.get("ean")) for item in supplier_orders if clean_identifier(item.get("ean"))}
        supplier_tokens = set().union(*(_line_product_tokens(item) for item in supplier_orders)) if supplier_orders else set()
        ean_hits = len(file_eans & supplier_eans)
        token_hits = len(file_tokens & supplier_tokens)
        score = ean_hits * 20 + token_hits
        ranking.append({
            "supplier": supplier,
            "score": float(score),
            "ean_hits": ean_hits,
            "product_hits": token_hits,
            "reason": f"EAN: {ean_hits}; codici/prodotti: {token_hits}",
        })
    ranking.sort(key=lambda item: (-item["score"], item["supplier"].casefold()))
    if not ranking or ranking[0]["score"] <= 0:
        return "", 0.0, ranking
    top = ranking[0]
    second = ranking[1]["score"] if len(ranking) > 1 else 0.0
    confidence = min(1.0, 0.5 + (top["score"] - second) / max(1.0, top["score"]) * 0.5)
    return str(top["supplier"]), confidence, ranking


def _group_orders(order_lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in order_lines:
        order_id = clean_text(item.get("order_id"))
        if order_id:
            grouped[order_id].append(dict(item))
    output: list[dict[str, Any]] = []
    for order_id, lines in grouped.items():
        first = lines[0]
        customer_names = [clean_text(item.get("customer_name")) for item in lines if clean_text(item.get("customer_name"))]
        customer = Counter(customer_names).most_common(1)[0][0] if customer_names else ""
        email, phone, address = _order_contact_fields(first)
        output.append({
            "order_id": order_id,
            "customer_name": customer,
            "customer_norm": normalized_customer_name(customer),
            "email": email,
            "phone": phone,
            "address": address,
            "raw_status": clean_text(first.get("raw_status")),
            "status_label": clean_text(first.get("status_label")),
            "market_label": clean_text(first.get("market_label")),
            "supplier": clean_text(first.get("supplier")),
            "order_created": clean_text(first.get("order_created")),
            "lines": lines,
            "line_ids": [clean_text(item.get("order_line_id")) for item in lines if clean_text(item.get("order_line_id"))],
            "row_keys": [clean_text(item.get("row_key")) for item in lines if clean_text(item.get("row_key"))],
            "eans": {clean_identifier(item.get("ean")) for item in lines if clean_identifier(item.get("ean"))},
            "product_tokens": set().union(*(_line_product_tokens(item) for item in lines)),
        })
    return output


def _similarity(left: object, right: object) -> float:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _match_score(file_row: Mapping[str, Any], order: Mapping[str, Any]) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    direct_reference = clean_text(file_row.get("marketplace_order_reference"))
    if direct_reference and direct_reference.upper() == clean_text(order.get("order_id")).upper():
        score += 100
        reasons.append("numero ordine esatto")

    file_customer = normalized_customer_name(file_row.get("customer_name"))
    order_customer = clean_text(order.get("customer_norm"))
    if file_customer and order_customer:
        if file_customer == order_customer:
            score += 70
            reasons.append("Customer Name esatto")
        else:
            name_similarity = _similarity(file_customer, order_customer)
            if name_similarity >= 0.82:
                score += 45 * name_similarity
                reasons.append(f"Customer Name simile {name_similarity:.0%}")

    file_email = clean_text(file_row.get("email")).casefold()
    order_email = clean_text(order.get("email")).casefold()
    if file_email and order_email and file_email == order_email:
        score += 30
        reasons.append("email esatta")

    file_phone = _digits(file_row.get("phone"))
    order_phone = _digits(order.get("phone"))
    if file_phone and order_phone and (file_phone.endswith(order_phone) or order_phone.endswith(file_phone)):
        score += 25
        reasons.append("telefono corrispondente")

    file_eans = set(file_row.get("eans") or [])
    ean_hits = file_eans & set(order.get("eans") or set())
    if ean_hits:
        score += 45
        reasons.append("EAN corrispondente")

    file_tokens = {token for token in _norm(file_row.get("product")).split() if len(token) >= 3}
    file_tokens.update(token for token in _norm(file_row.get("product_code")).split() if len(token) >= 3)
    order_tokens = set(order.get("product_tokens") or set())
    token_hits = file_tokens & order_tokens
    if token_hits:
        score += min(30, len(token_hits) * 5)
        reasons.append(f"prodotto/SKU compatibile ({len(token_hits)} riscontri)")

    file_date = _to_datetime(file_row.get("created_at"))
    order_date = _to_datetime(order.get("order_created"))
    if file_date and order_date:
        difference = abs((file_date.date() - order_date.date()).days)
        if difference <= 14:
            score += max(0, 10 - difference * 0.5)
            reasons.append("date compatibili")
    return score, reasons


def match_tracking_rows(
    file_rows: Sequence[Mapping[str, Any]],
    order_lines: Sequence[Mapping[str, Any]],
    *,
    supplier: str = "",
) -> list[dict[str, Any]]:
    normalized_supplier = _norm(supplier)
    filtered_lines = [
        dict(item) for item in order_lines
        if not normalized_supplier or _norm(item.get("supplier")) == normalized_supplier
    ]
    orders = _group_orders(filtered_lines)
    output: list[dict[str, Any]] = []
    for file_row in file_rows:
        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        for order in orders:
            score, reasons = _match_score(file_row, order)
            if score > 0:
                ranked.append((score, order, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]["order_id"]))
        best = ranked[0] if ranked else None
        second_score = ranked[1][0] if len(ranked) > 1 else -1
        match_status = "Non abbinato"
        matched_order: dict[str, Any] | None = None
        reason = "nessun ordine compatibile"
        score = 0.0
        if best:
            score, candidate, reasons = best
            unique_customer = "Customer Name esatto" in reasons and (second_score < 0 or score - second_score >= 15)
            strong = score >= 75 or unique_customer
            ambiguous = second_score >= 0 and score - second_score < 15
            if strong and not ambiguous:
                match_status = "Abbinato automaticamente"
                matched_order = candidate
            elif score >= 45:
                match_status = "Ambiguo · verifica manuale"
                matched_order = candidate
            reason = "; ".join(reasons) or "compatibilità parziale"
        item = dict(file_row)
        item.update({
            "match_status": match_status,
            "match_score": round(float(score), 1),
            "match_reason": reason,
            "order_id": clean_text(matched_order.get("order_id")) if matched_order else "",
            "customer_name_order": clean_text(matched_order.get("customer_name")) if matched_order else "",
            "marketplace_status": clean_text(matched_order.get("raw_status")) if matched_order else "",
            "market_label": clean_text(matched_order.get("market_label")) if matched_order else "",
            "order_line_ids": list(matched_order.get("line_ids") or []) if matched_order else [],
            "row_keys": list(matched_order.get("row_keys") or []) if matched_order else [],
            "supplier": supplier or clean_text(matched_order.get("supplier")) if matched_order else supplier,
        })
        output.append(item)
    return output


def order_tracking_rows(
    matches: Sequence[Mapping[str, Any]],
    *,
    marketplace: str,
    orders: Sequence[Mapping[str, Any]] | None = None,
    supplier: str = "",
    include_without_tracking: bool = False,
) -> list[dict[str, Any]]:
    """Build one operational tracking row per marketplace order.

    By default the function preserves the historical behavior and returns only
    matched orders that already have a tracking number.  When
    ``include_without_tracking`` is true, every order in ``orders`` belonging to
    the selected supplier is included as well.  This allows the UI's
    "Visualizza tutti gli ordini" action to show orders still waiting for a
    shipment file or tracking number, while already shipped orders can still be
    partitioned into the separate history table.
    """
    normalized_supplier = clean_text(supplier).casefold()

    grouped_matches: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in matches:
        order_id = clean_text(item.get("order_id"))
        if order_id:
            grouped_matches[order_id].append(item)

    grouped_orders: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in orders or []:
        order_id = clean_text(item.get("order_id"))
        if not order_id:
            continue
        item_supplier = clean_text(item.get("supplier"))
        if normalized_supplier and item_supplier and item_supplier.casefold() != normalized_supplier:
            continue
        grouped_orders[order_id].append(item)

    order_ids = set(grouped_matches)
    if include_without_tracking:
        order_ids.update(grouped_orders)

    result: list[dict[str, Any]] = []
    for order_id in sorted(order_ids):
        items = grouped_matches.get(order_id, [])
        order_lines = grouped_orders.get(order_id, [])
        first_match = items[0] if items else {}
        first_order = order_lines[0] if order_lines else {}
        first = first_match or first_order

        tracking_numbers = list(dict.fromkeys(
            tracking
            for item in items
            for tracking in split_tracking_numbers(item.get("tracking"))
            if tracking
        ))
        if not tracking_numbers:
            tracking_numbers = list(dict.fromkeys(
                tracking
                for item in order_lines
                for tracking in split_tracking_numbers(item.get("tracking"))
                if tracking
            ))

        carriers = list(dict.fromkeys(
            clean_text(item.get("carrier"))
            for item in items
            if clean_text(item.get("carrier"))
        ))
        statuses = [
            clean_text(item.get("file_status"))
            for item in items
            if clean_text(item.get("file_status"))
        ]
        file_status = statuses[0] if statuses else ""
        carrier = carriers[0] if len(carriers) == 1 else " / ".join(carriers)
        tracking = " / ".join(tracking_numbers)

        if not tracking and not include_without_tracking:
            continue

        marketplace_status = clean_text(
            first_match.get("marketplace_status")
            or first_order.get("raw_status")
        )
        customer_name = clean_text(
            first_match.get("customer_name_order")
            or first_match.get("customer_name")
            or first_order.get("customer_name")
        )
        market_label = clean_text(
            first_match.get("market_label")
            or first_order.get("market_label")
        ) or marketplace.title()
        supplier_name = clean_text(
            first_match.get("supplier")
            or first_order.get("supplier")
            or supplier
        )
        order_line_ids = list(dict.fromkeys(
            [
                clean_text(line)
                for item in items
                for line in item.get("order_line_ids", [])
                if clean_text(line)
            ]
            + [
                clean_text(item.get("order_line_id"))
                for item in order_lines
                if clean_text(item.get("order_line_id"))
            ]
        ))
        row_keys = list(dict.fromkeys(
            [
                clean_text(key)
                for item in items
                for key in item.get("row_keys", [])
                if clean_text(key)
            ]
            + [
                clean_text(item.get("row_key"))
                for item in order_lines
                if clean_text(item.get("row_key"))
            ]
        ))

        if tracking:
            status_label = classify_file_status(
                file_status, tracking, carrier, supplier_name
            )
        elif file_status:
            status_label = classify_file_status(
                file_status, tracking, carrier, supplier_name
            )
        else:
            status_label = "In attesa di tracciabilità"

        row: dict[str, Any] = {
            "Aggiorna": False,
            "Ordine": order_id,
            "Customer Name": customer_name,
            "Market": market_label,
            "Fornitore": supplier_name,
            "Tracking": tracking,
            "Corriere": carrier,
            "Stato file": status_label,
            "Stato file originale": file_status,
            "Stato marketplace": marketplace_status,
            "Stato operativo": status_label,
            "Unità/righe": len(set(order_line_ids)),
            "Righe file": len(items),
            "Invio consentito": "No",
            "Problemi": "",
            "order_line_ids": order_line_ids,
            "row_keys": row_keys,
            "tracking_numbers": tracking_numbers,
            "marketplace_status": marketplace_status,
            "file_status": file_status,
            "carrier": carrier,
            "tracking": tracking,
            "waiting_for_tracking": not bool(tracking),
        }

        if marketplace.lower() == "worten":
            row = apply_worten_eligibility(row)
            if not tracking and not row.get("already_shipped"):
                row["Invio consentito"] = "No"
                row["api_allowed"] = False
                row["Stato operativo"] = "In attesa di tracciabilità"
                row["Problemi"] = (
                    "tracking non ancora disponibile; attendere o caricare un "
                    "documento spedizioni aggiornato"
                )
        else:
            status = normalize_status(row["Stato marketplace"])
            source = normalize_status(file_status)
            problems: list[str] = []
            allowed = True
            already_shipped = status in {"SENT", "SHIPPED", "RECEIVED", "DELIVERED", "CLOSED"}
            if already_shipped:
                allowed = False
                problems.append("ordine già spedito sul marketplace")
            elif status not in {"NEED TO BE SENT", "NEED_TO_BE_SENT"}:
                allowed = False
                problems.append(f"stato marketplace {status or 'non disponibile'} non spedibile")
            cecotec_waiting_ready = (
                source in WAITING_FILE_STATUSES
                and supplier_name.casefold() == "cecotec"
                and bool(tracking)
                and bool(carrier)
                and len(tracking_numbers) == 1
            )
            if source in CANCELLED_FILE_STATUSES:
                allowed = False
                problems.append("spedizione annullata nel file")
            elif source in WAITING_FILE_STATUSES and not cecotec_waiting_ready:
                allowed = False
                problems.append("spedizione ancora in attesa nel file")
            elif source and source not in READY_FILE_STATUSES and not cecotec_waiting_ready:
                allowed = False
                problems.append(f"stato file {source} da verificare")
            if len(tracking_numbers) > 1:
                allowed = False
                problems.append("più tracking differenti sullo stesso ordine")
            if not tracking:
                allowed = False
                problems.append("tracking non ancora disponibile")
                row["Stato operativo"] = "In attesa di tracciabilità"
            if not carrier:
                allowed = False
                problems.append("corriere mancante")
            if cecotec_waiting_ready and allowed:
                row["Stato operativo"] = "Pronta per l'invio"
            row["Invio consentito"] = "Sì" if allowed else "No"
            row["Problemi"] = "; ".join(dict.fromkeys(problems))
            row["api_allowed"] = allowed
            row["already_shipped"] = already_shipped
        result.append(row)

    result.sort(
        key=lambda item: (
            bool(item.get("already_shipped")),
            item.get("Invio consentito") != "Sì",
            bool(item.get("waiting_for_tracking")),
            item.get("Ordine", ""),
        )
    )
    return result



def partition_shipping_rows(
    shipping_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split rows that still need attention from orders already shipped.

    The active table must never contain an order that the marketplace reports
    as shipped/received/closed or that Marketplace Hub successfully transmitted
    in an earlier run.  Those rows remain available in a separate read-only
    history table.
    """
    active: list[dict[str, Any]] = []
    shipped: list[dict[str, Any]] = []
    for source in shipping_rows:
        item = dict(source)
        if bool(item.get("already_shipped")):
            shipped.append(item)
        else:
            active.append(item)
    return active, shipped

def persist_import(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    supplier: str,
    file_name: str,
    content: bytes,
    source_format: str,
    matches: Sequence[Mapping[str, Any]],
    file_ids: Sequence[int] | None = None,
) -> int:
    ensure_schema()
    total = len(matches)
    matched = sum(item.get("match_status") == "Abbinato automaticamente" for item in matches)
    ambiguous = sum(str(item.get("match_status", "")).startswith("Ambiguo") for item in matches)
    unmatched = total - matched - ambiguous
    ready = sum(item.get("operational_status") == "Spedita · tracking disponibile" for item in matches)
    waiting = sum(item.get("operational_status") == "In attesa di spedizione" for item in matches)
    cancelled = sum(item.get("operational_status") == "Annullata nel file" for item in matches)
    import_id = execute(
        """INSERT INTO tracking_imports(
            seller_id,marketplace_account_id,marketplace,supplier,file_name,file_sha256,
            source_format,total_rows,matched_rows,ambiguous_rows,unmatched_rows,
            ready_rows,waiting_rows,cancelled_rows,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            seller_id, account_id, marketplace.lower(), supplier, file_name,
            hashlib.sha256(content).hexdigest(), source_format, total, matched,
            ambiguous, unmatched, ready, waiting, cancelled, now_iso(),
        ),
    )
    with connect() as con:
        con.executemany(
            """INSERT INTO tracking_matches(
                import_id,seller_id,marketplace_account_id,marketplace,supplier,
                source_row,source_reference,order_id,order_line_ids_json,
                customer_name_file,customer_name_order,product_file,file_status,
                operational_status,tracking,carrier,match_status,match_score,
                match_reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    import_id, seller_id, account_id, marketplace.lower(), supplier,
                    int(item.get("source_row") or 0), clean_text(item.get("source_reference")),
                    clean_text(item.get("order_id")), json.dumps(item.get("order_line_ids") or []),
                    clean_text(item.get("customer_name")), clean_text(item.get("customer_name_order")),
                    clean_text(item.get("product")), clean_text(item.get("file_status")),
                    clean_text(item.get("operational_status")), clean_text(item.get("tracking")),
                    clean_text(item.get("carrier")), clean_text(item.get("match_status")),
                    float(item.get("match_score") or 0), clean_text(item.get("match_reason")),
                    now_iso(),
                )
                for item in matches
            ],
        )
        normalized_file_ids = []
        for file_id in file_ids or []:
            value = int(file_id or 0)
            if value > 0 and value not in normalized_file_ids:
                normalized_file_ids.append(value)
        if normalized_file_ids:
            con.executemany(
                """INSERT INTO tracking_import_files(
                    import_id,tracking_file_id,position
                ) VALUES(?,?,?) ON CONFLICT DO NOTHING""",
                [
                    (import_id, file_id, position)
                    for position, file_id in enumerate(normalized_file_ids, start=1)
                ],
            )
            placeholders = ",".join("?" for _ in normalized_file_ids)
            con.execute(
                f"""UPDATE tracking_source_files
                SET supplier=CASE WHEN TRIM(?)<>'' THEN ? ELSE supplier END,
                    last_used_at=?,use_count=use_count+1
                WHERE id IN ({placeholders})""",
                (supplier, supplier, now_iso(), *normalized_file_ids),
            )
    return import_id


def recent_imports(seller_id: int, account_id: int, marketplace: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_schema()
    return rows(
        """SELECT * FROM tracking_imports
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        ORDER BY id DESC LIMIT ?""",
        (seller_id, account_id, marketplace.lower(), max(1, int(limit))),
    )



def successful_api_orders(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    order_ids: Sequence[str] | None = None,
) -> dict[str, str]:
    """Return orders already sent successfully by the tracking workflow.

    The result is persisted in ``tracking_matches`` and therefore survives
    Streamlit reruns, program restarts and later file imports.
    """
    ensure_schema()
    params: list[Any] = [int(seller_id), int(account_id), clean_text(marketplace).lower()]
    condition = ""
    normalized_ids = [clean_text(item) for item in (order_ids or []) if clean_text(item)]
    if normalized_ids:
        placeholders = ",".join("?" for _ in normalized_ids)
        condition = f" AND order_id IN ({placeholders})"
        params.extend(normalized_ids)
    found = rows(
        f"""SELECT order_id,MAX(api_sent_at) AS api_sent_at
        FROM tracking_matches
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
          AND api_status='success' AND TRIM(order_id)<>''{condition}
        GROUP BY order_id""",
        tuple(params),
    )
    return {
        clean_text(item.get("order_id")): clean_text(item.get("api_sent_at"))
        for item in found
        if clean_text(item.get("order_id"))
    }


def mark_rows_already_sent(
    shipping_rows: Sequence[Mapping[str, Any]],
    sent_history: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Disable rows already transmitted in a previous successful API action."""
    output: list[dict[str, Any]] = []
    for source in shipping_rows:
        item = dict(source)
        order_id = clean_text(item.get("Ordine") or item.get("order_id"))
        sent_at = clean_text(sent_history.get(order_id))
        if sent_at:
            item["Invio consentito"] = "No"
            item["api_allowed"] = False
            item["already_shipped"] = True
            item["api_sent_at"] = sent_at
            problem = f"già spedito dal programma il {sent_at.replace('T', ' ')[:16]}"
            existing = clean_text(item.get("Problemi"))
            item["Problemi"] = "; ".join(dict.fromkeys(
                value for value in (existing, problem) if value
            ))
        output.append(item)
    return output

def record_api_result(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    order_id: str,
    success: bool,
    message: str,
) -> None:
    ensure_schema()
    execute(
        """UPDATE tracking_matches SET api_status=?,api_message=?,api_sent_at=?
        WHERE id=(
            SELECT id FROM tracking_matches
            WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND order_id=?
            ORDER BY id DESC LIMIT 1
        )""",
        (
            "success" if success else "error", clean_text(message), now_iso(),
            seller_id, account_id, marketplace.lower(), order_id,
        ),
    )


def mark_accounting_order_shipped(
    *,
    account_id: int,
    marketplace: str,
    order_id: str,
    tracking: str,
    carrier: str,
) -> int:
    status = "SHIPPED" if marketplace.lower() == "worten" else "sent"
    label = "Spedito"
    with connect() as con:
        cursor = con.execute(
            """UPDATE accounting_order_lines SET
                raw_status=?,status_label=?,tracking=?,note=CASE
                    WHEN TRIM(note)='' THEN ?
                    ELSE note || '; ' || ? END,
                synced_at=?
            WHERE marketplace_account_id=? AND marketplace=? AND order_id=?""",
            (
                status, label, clean_text(f"{carrier} · {tracking}"),
                "Tracking inviato al marketplace",
                "Tracking inviato al marketplace",
                now_iso(), account_id, marketplace.lower(), order_id,
            ),
        )
        return max(0, int(cursor.rowcount or 0))
