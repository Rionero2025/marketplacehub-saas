import csv
import io
import json
import unicodedata
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from marketplace_hub_core.orders.projections import number

# Only fields already exposed by the public order table/detail; never raw API data.
COLUMNS = [
    ("Ordine", "order_id", "text"), ("Unità / riga", "external_line_id", "text"),
    ("Marketplace", "marketplace", "text"), ("Nazione", "storefront", "text"),
    ("Data ordine (Italia)", "created_at", "date"), ("Prodotto", "product_name", "text"),
    ("EAN", "ean", "text"), ("SKU composto", "sku", "text"), ("Quantità", "quantity", "number"),
    ("Stato codice", "status", "text"), ("Stato", "status_label", "text"),
    ("Valuta originale", "currency", "text"),
    ("Venduto EUR", "sale_amount_eur", "number"),
    ("Spedizione EUR", "shipping_amount_eur", "number"),
    ("Commissione EUR", "commission_amount_eur", "number"),
    ("Commissione %", "commission_rate", "number"),
    ("Netto da ricevere EUR", "payout_amount_eur", "number"),
    ("Costo acquisto EUR", "purchase_cost_eur", "number"),
    ("Utile EUR", "profit_amount_eur", "number"), ("Utile sul costo %", "profit_pct", "number"),
    ("Fonte costo", "purchase_cost_source", "text"),
    ("Venduto valuta originale", "sale_amount", "number"),
    ("Spedizione valuta originale", "shipping_amount", "number"),
    ("Commissione valuta originale", "commission_amount", "number"),
    ("Netto valuta originale", "payout_amount", "number"),
    ("Corriere", "details.carrier", "text"), ("Tracking", "details.tracking", "text"),
    ("Fonte commissione", "details.commission_source", "text"),
    ("Fonte netto", "details.payout_source", "text"),
    ("Spedito (Italia)", "details.shipped_at", "date"),
    ("Ricevuto (Italia)", "details.received_at", "date"),
    ("Rilascio pagamento API (Italia)", "details.released_at", "date"),
    ("Data pagamento (Italia)", "details.payment_due_at", "date"),
    ("Giorni al pagamento", "details.payment_days_remaining", "number"),
    ("Pagamento disponibile", "details.payment_available", "text"),
    ("Data pagamento definitiva", "details.payment_date_final", "text"),
    ("Stato pagamento", "details.payment_status", "text"),
    ("Regola pagamento", "details.payment_rule", "text"),
    ("Fonte data pagamento", "details.payment_source", "text"),
    ("Ritardo ticket (giorni)", "details.ticket_delay_days", "number"),
    ("Ticket aperto", "details.ticket_open", "text"),
    ("Numero ticket", "details.ticket_count", "number"),
    ("Ticket aperti", "details.open_ticket_count", "number"),
    ("ID ticket", "details.ticket_ids", "text"),
    ("Ultima verifica dettagli (Italia)", "details.detail_checked_at", "date"),
    ("Cambio", "details.fx.rate", "number"), ("Data cambio", "details.fx.date", "text"),
    ("Fonte cambio", "details.fx.source", "text"), ("Avvisi", "monetary_warnings", "text"),
]


def safe_text(value):
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    while stripped and unicodedata.category(stripped[0]) in {"Cf", "Cc", "Zs", "Zl", "Zp"}:
        stripped = stripped[1:].lstrip()
    prefix = text[:len(text) - len(stripped)]
    if stripped.startswith(("=", "+", "-", "@")) or any(c in prefix for c in "\t\r\n"):
        return "'" + text
    return text


def csv_cell(item, path, kind):
    value = item
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    if kind == "number":
        parsed = number(value)
        return format(parsed, "f") if parsed is not None else ""
    if kind == "date":
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M:%S")
        except (ValueError, TypeError):
            return ""
    if isinstance(value, list):
        value = " | ".join(str(entry) for entry in value)
    return safe_text(value)


def export_csv(partitions, guard):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    guard()
    writer.writerow([column[0] for column in COLUMNS])
    yield b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
    try:
        for batch in partitions:
            guard()
            buffer.seek(0)
            buffer.truncate(0)
            for row in batch:
                item = json.loads(row[0])
                writer.writerow([csv_cell(item, path, kind) for _, path, kind in COLUMNS])
            yield buffer.getvalue().encode("utf-8")
    finally:
        if hasattr(partitions, "close"):
            partitions.close()
