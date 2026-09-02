from __future__ import annotations

import json
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from services.db import DATA_DIR, connect, execute, json_text, now_iso, rows
except Exception:  # pragma: no cover - keeps pure helpers importable in isolated tests
    DATA_DIR = Path("data")
    connect = None  # type: ignore[assignment]
    execute = None  # type: ignore[assignment]
    rows = None  # type: ignore[assignment]

    def json_text(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def now_iso() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class InnproSkuInfo:
    raw: str
    supplier: str
    product_code: str
    ean: str
    is_innpro: bool
    valid_ean: bool
    exportable: bool
    error: str = ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", text).strip()


def clean_identifier(value: Any) -> str:
    text = clean_text(value)
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        return text[:-2]
    return text


def normalize_supplier(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def is_valid_ean13(value: Any) -> bool:
    """Return True only for a syntactically and checksum-valid EAN-13."""
    ean = clean_identifier(value)
    if not re.fullmatch(r"\d{13}", ean):
        return False
    digits = [int(char) for char in ean]
    weighted = sum(digits[:12:2]) + 3 * sum(digits[1:12:2])
    expected_check = (10 - (weighted % 10)) % 10
    return digits[12] == expected_check


def parse_innpro_composite_sku(value: Any) -> InnproSkuInfo:
    """Parse ``supplier_product-code_cost_minimum`` and identify INNPRO EANs.

    ``rsplit`` deliberately allows underscores inside the supplier portion,
    matching Marketplace Hub's existing composite-SKU contract.
    """
    raw = clean_text(value)
    if not raw:
        return InnproSkuInfo(raw, "", "", "", False, False, False, "SKU vuoto")

    parts = raw.rsplit("_", 3)
    if len(parts) != 4:
        return InnproSkuInfo(
            raw,
            "",
            "",
            "",
            False,
            False,
            False,
            "Formato SKU non valido: atteso fornitore_codice_costo_prezzominimo",
        )

    supplier = clean_text(parts[0])
    product_code = clean_identifier(parts[1])
    supplier_key = normalize_supplier(supplier)
    innpro = "innpro" in supplier_key

    if not innpro:
        return InnproSkuInfo(
            raw,
            supplier,
            product_code,
            "",
            False,
            False,
            False,
            "SKU non appartenente a INNPRO",
        )

    if not is_valid_ean13(product_code):
        return InnproSkuInfo(
            raw,
            supplier,
            product_code,
            product_code,
            True,
            False,
            False,
            "Il secondo valore dello SKU INNPRO non è un EAN-13 valido",
        )

    return InnproSkuInfo(
        raw,
        supplier,
        product_code,
        product_code,
        True,
        True,
        True,
        "",
    )


def positive_quantity(value: Any) -> int:
    try:
        quantity = int(float(value))
    except (TypeError, ValueError):
        return 0
    return quantity if quantity > 0 else 0


def analyze_innpro_order_line(item: Mapping[str, Any]) -> dict[str, Any]:
    sku = item.get("composite_sku") or item.get("sku") or ""
    info = parse_innpro_composite_sku(sku)
    quantity = positive_quantity(item.get("quantity", 1))
    exportable = info.exportable and quantity > 0
    error = info.error
    if info.exportable and quantity <= 0:
        error = "Quantità ordine non valida"

    return {
        "row_key": clean_text(item.get("row_key") or item.get("id") or ""),
        "order_id": clean_text(item.get("order_id") or item.get("id_order") or ""),
        "order_line_id": clean_text(
            item.get("order_line_id") or item.get("id_order_unit") or ""
        ),
        "supplier": info.supplier,
        "composite_sku": info.raw,
        "ean": info.ean,
        "quantity": quantity,
        "is_innpro": info.is_innpro,
        "valid_ean": info.valid_ean,
        "exportable": exportable,
        "error": error,
    }


def aggregate_innpro_order_lines(
    lines: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate valid selected lines by EAN and return excluded-line details."""
    quantities: OrderedDict[str, int] = OrderedDict()
    excluded: list[dict[str, Any]] = []

    for item in lines:
        analysis = analyze_innpro_order_line(item)
        if not analysis["exportable"]:
            excluded.append(analysis)
            continue
        ean = analysis["ean"]
        quantities[ean] = quantities.get(ean, 0) + int(analysis["quantity"])

    aggregated = [
        {"EAN": ean, "Quantita": quantity}
        for ean, quantity in quantities.items()
    ]
    return aggregated, excluded


def export_innpro_csv_bytes(lines: Iterable[Mapping[str, Any]]) -> bytes:
    """Build the exact INNPRO upload format: no header, ``EAN;quantity``."""
    aggregated, _ = aggregate_innpro_order_lines(lines)
    # The INNPRO sample has CRLF between rows and deliberately no trailing
    # newline after the final row. Keeping that byte contract avoids subtle
    # differences with supplier-side importers that compare the raw upload.
    rows = [
        f'{item["EAN"]};{int(item["Quantita"])}'
        for item in aggregated
    ]
    return "\r\n".join(rows).encode("utf-8")


def default_innpro_file_name(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    stamp = current.strftime("%Y%m%d_%H%M%S")
    return f"order_EANs_{stamp}.csv"


def _require_db() -> None:
    if execute is None or rows is None or connect is None:
        raise RuntimeError("I servizi database del progetto non sono disponibili.")


def ensure_innpro_schema() -> None:
    """Create the persistent INNPRO export archive schema.

    History is keyed by marketplace order number, as requested by the workflow:
    once at least one valid INNPRO line from an order is written to a supplier
    file, that marketplace order is considered generated.
    """
    _require_db()
    statements = (
        """
        CREATE TABLE IF NOT EXISTS innpro_order_exports(
            export_id TEXT PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            marketplace_account_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            selected_rows INTEGER NOT NULL DEFAULT 0,
            exported_rows INTEGER NOT NULL DEFAULT 0,
            unique_eans INTEGER NOT NULL DEFAULT 0,
            total_quantity INTEGER NOT NULL DEFAULT 0,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS innpro_order_export_orders(
            export_id TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            marketplace_account_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            order_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(export_id, order_id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_innpro_export_orders_lookup
        ON innpro_order_export_orders(
            seller_id, marketplace_account_id, marketplace, order_id, created_at
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_innpro_exports_history
        ON innpro_order_exports(
            seller_id, marketplace_account_id, marketplace, created_at
        )
        """,
    )
    for statement in statements:
        execute(statement)


def apply_duplicate_order_choice(
    selected_lines: Sequence[Mapping[str, Any]],
    previously_generated_order_ids: Sequence[str] | set[str],
    choice: str,
) -> list[Mapping[str, Any]]:
    """Apply duplicate handling at marketplace-order level, not row level."""
    normalized_choice = clean_text(choice).lower()
    if normalized_choice not in {"new_only", "all"}:
        raise ValueError("Scelta di generazione INNPRO non valida.")
    selected = list(selected_lines)
    if normalized_choice == "all":
        return selected
    generated = {
        clean_text(value)
        for value in previously_generated_order_ids
        if clean_text(value)
    }
    return [
        item
        for item in selected
        if clean_text(item.get("order_id") or item.get("id_order")) not in generated
    ]


def previous_exports_for_orders(
    seller_id: int,
    account_id: int,
    marketplace: str,
    order_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Return the latest archived INNPRO export for each requested order number."""
    _require_db()
    identifiers = list(
        dict.fromkeys(clean_text(value) for value in order_ids if clean_text(value))
    )
    if not identifiers:
        return []

    found: list[dict[str, Any]] = []
    for start in range(0, len(identifiers), 700):
        chunk = identifiers[start:start + 700]
        placeholders = ",".join("?" for _ in chunk)
        found.extend(rows(
            f"""SELECT o.order_id,o.created_at,e.export_id,e.file_name,e.file_path,
            e.selected_rows,e.exported_rows,e.unique_eans,e.total_quantity
            FROM innpro_order_export_orders o
            JOIN innpro_order_exports e ON e.export_id=o.export_id
            WHERE o.seller_id=? AND o.marketplace_account_id=? AND o.marketplace=?
            AND o.order_id IN ({placeholders})
            ORDER BY o.created_at DESC""",
            (seller_id, account_id, clean_text(marketplace).lower(), *chunk),
        ))

    found.sort(key=lambda item: clean_text(item.get("created_at")), reverse=True)
    latest: dict[str, dict[str, Any]] = {}
    for item in found:
        order_id = clean_text(item.get("order_id"))
        if order_id and order_id not in latest:
            latest[order_id] = item
    return [latest[value] for value in identifiers if value in latest]


def innpro_export_history(
    seller_id: int,
    account_id: int | None = None,
    marketplace: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    _require_db()
    clauses = ["e.seller_id=?"]
    params: list[Any] = [seller_id]
    if account_id is not None:
        clauses.append("e.marketplace_account_id=?")
        params.append(account_id)
    if marketplace:
        clauses.append("e.marketplace=?")
        params.append(clean_text(marketplace).lower())
    params.append(max(1, int(limit)))
    return rows(
        f"""SELECT e.*,COUNT(o.order_id) AS order_count
        FROM innpro_order_exports e
        LEFT JOIN innpro_order_export_orders o ON o.export_id=e.export_id
        WHERE {' AND '.join(clauses)}
        GROUP BY e.export_id,e.seller_id,e.marketplace_account_id,e.marketplace,
        e.file_name,e.file_path,e.selected_rows,e.exported_rows,e.unique_eans,
        e.total_quantity,e.details_json,e.created_at
        ORDER BY e.created_at DESC LIMIT ?""",
        tuple(params),
    )


def _archive_path(file_path: Any) -> Path:
    raw = clean_text(file_path)
    if not raw:
        raise FileNotFoundError("Percorso file INNPRO assente nello storico.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = DATA_DIR / candidate
    resolved = candidate.resolve()
    data_root = DATA_DIR.resolve()
    if resolved != data_root and data_root not in resolved.parents:
        raise RuntimeError("Percorso archivio INNPRO non valido.")
    return resolved


def read_innpro_export_bytes(file_path: Any) -> bytes:
    path = _archive_path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"File INNPRO non trovato nell’archivio: {path.name}")
    return path.read_bytes()


def save_innpro_export(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    file_name: str,
    file_bytes: bytes,
    selected_lines: Sequence[Mapping[str, Any]],
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Persist the generated CSV and the marketplace order numbers it contains."""
    _require_db()
    selected = list(selected_lines)
    aggregated, excluded = aggregate_innpro_order_lines(selected)
    analyses = [analyze_innpro_order_line(item) for item in selected]
    exportable_analyses = [item for item in analyses if item["exportable"]]
    if not aggregated or not exportable_analyses or not file_bytes:
        raise ValueError("Nessuna riga INNPRO valida da archiviare.")

    order_ids = list(dict.fromkeys(
        clean_text(item.get("order_id"))
        for item in exportable_analyses
        if clean_text(item.get("order_id"))
    ))
    if not order_ids:
        raise ValueError("Numero ordine marketplace mancante: impossibile registrare lo storico.")

    export_id = uuid.uuid4().hex
    safe_name = Path(clean_text(file_name)).name or default_innpro_file_name()
    if not safe_name.lower().endswith(".csv"):
        safe_name += ".csv"
    marketplace_key = clean_text(marketplace).lower()
    archive_root = Path(base_dir) if base_dir is not None else DATA_DIR / "innpro_orders"
    target_dir = archive_root / str(seller_id) / marketplace_key / str(account_id) / export_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / safe_name
    temp_path = target_dir / f".{safe_name}.tmp"
    temp_path.write_bytes(bytes(file_bytes))
    temp_path.replace(target_path)

    if base_dir is None:
        stored_path = str(target_path.relative_to(DATA_DIR))
    else:
        stored_path = str(target_path)

    created_at = now_iso()
    details = {
        "order_ids": order_ids,
        "aggregated": aggregated,
        "excluded": excluded,
    }
    try:
        with connect() as con:
            con.execute(
                """INSERT INTO innpro_order_exports(
                export_id,seller_id,marketplace_account_id,marketplace,file_name,file_path,
                selected_rows,exported_rows,unique_eans,total_quantity,details_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    export_id, seller_id, account_id, marketplace_key, safe_name, stored_path,
                    len(selected), len(exportable_analyses), len(aggregated),
                    sum(int(item["Quantita"]) for item in aggregated),
                    json_text(details), created_at,
                ),
            )
            con.executemany(
                """INSERT INTO innpro_order_export_orders(
                export_id,seller_id,marketplace_account_id,marketplace,order_id,created_at
                ) VALUES(?,?,?,?,?,?)""",
                [
                    (export_id, seller_id, account_id, marketplace_key, order_id, created_at)
                    for order_id in order_ids
                ],
            )
    except Exception:
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return {
        "export_id": export_id,
        "seller_id": seller_id,
        "marketplace_account_id": account_id,
        "marketplace": marketplace_key,
        "file_name": safe_name,
        "file_path": stored_path,
        "selected_rows": len(selected),
        "exported_rows": len(exportable_analyses),
        "unique_eans": len(aggregated),
        "total_quantity": sum(int(item["Quantita"]) for item in aggregated),
        "order_count": len(order_ids),
        "created_at": created_at,
    }
