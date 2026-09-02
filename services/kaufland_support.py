from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable

from services.db import connect, execute, json_text, now_iso, row, rows


ProgressCallback = Callable[[int, int, str], None]

KAUFLAND_TICKET_STATUSES = (
    "opened",
    "buyer_closed",
    "seller_closed",
    "both_closed",
    "customer_service_closed_final",
)

VALID_TICKET_MIME_TYPES = {
    "text/plain",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/tiff",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
}
TICKET_MIME_BY_SUFFIX = {
    ".txt": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}
MAX_TICKET_ATTACHMENT_BYTES = 20 * 1024 * 1024
TICKET_REASONS = {
    "product_not_as_described",
    "product_defect",
    "product_not_delivered",
    "product_return",
    "contact_other",
}


def response_data(response) -> list[dict]:
    if isinstance(response, dict):
        data = response.get("data", [])
        if isinstance(data, dict):
            return [data]
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []


def response_item(response) -> dict:
    if not isinstance(response, dict):
        return {}
    data = response.get("data", response)
    if isinstance(data, list):
        return data[0] if data and isinstance(data[0], dict) else {}
    return data if isinstance(data, dict) else {}


def _pagination_total(response, fallback: int) -> int:
    if not isinstance(response, dict):
        return fallback
    pagination = response.get("pagination") or {}
    try:
        return max(fallback, int(pagination.get("total", fallback)))
    except (TypeError, ValueError):
        return fallback


def fetch_tickets(
    client, maximum: int | None = None,
    statuses: tuple[str, ...] = KAUFLAND_TICKET_STATUSES,
) -> list[dict]:
    """Download every requested ticket status using Kaufland's 30-row limit."""
    cap = None if maximum is None else max(1, int(maximum))
    by_id: dict[str, dict] = {}
    for status in statuses:
        offset = 0
        while True:
            response = client.tickets(status=status, limit=30, offset=offset)
            page = response_data(response)
            for item in page:
                normalized = dict(item)
                normalized["status"] = _as_text(
                    normalized.get("status") or status
                ).lower()
                key = _as_text(normalized.get("id_ticket"))
                if key:
                    by_id[key] = normalized
            total = _pagination_total(response, offset + len(page))
            if not page or len(page) < 30 or offset + len(page) >= total:
                break
            offset += len(page)
    result = sorted(
        by_id.values(),
        key=lambda item: (
            _as_text(item.get("ts_updated_iso")),
            _as_text(item.get("ts_created_iso")),
            _as_text(item.get("id_ticket")),
        ),
        reverse=True,
    )
    return result if cap is None else result[:cap]


def fetch_ticket_messages(client, id_ticket: str, maximum: int = 500) -> list[dict]:
    maximum = max(1, int(maximum))
    result: list[dict] = []
    offset = 0
    while len(result) < maximum:
        page_limit = min(30, maximum - len(result))
        response = client.ticket_messages(
            id_ticket, limit=page_limit, offset=offset
        )
        page = response_data(response)
        result.extend(page)
        total = _pagination_total(response, len(result))
        if not page or len(page) < page_limit or len(result) >= total:
            break
        offset += len(page)
    return result[:maximum]


def fetch_all_ticket_messages(
    client, maximum: int | None = None
) -> list[dict]:
    """Download the global ticket-message collection in 30-row pages."""
    cap = None if maximum is None else max(1, int(maximum))
    result: list[dict] = []
    offset = 0
    while cap is None or len(result) < cap:
        page_limit = 30 if cap is None else min(30, cap - len(result))
        response = client.ticket_messages("", limit=page_limit, offset=offset)
        page = response_data(response)
        result.extend(page)
        total = _pagination_total(response, len(result))
        if not page or len(page) < page_limit or len(result) >= total:
            break
        offset += len(page)
    return result if cap is None else result[:cap]


def fetch_order_units(client, maximum: int = 250) -> list[dict]:
    maximum = max(1, int(maximum))
    result: list[dict] = []
    offset = 0
    while len(result) < maximum:
        page_limit = min(100, maximum - len(result))
        response = client.order_units(limit=page_limit, offset=offset)
        page = response_data(response)
        result.extend(page)
        total = _pagination_total(response, len(result))
        if not page or len(page) < page_limit or len(result) >= total:
            break
        offset += len(page)
    return result[:maximum]


def _as_text(value) -> str:
    return "" if value is None else str(value).strip()


def _order_unit_ids(ticket: dict) -> list[str]:
    values = ticket.get("ids_order_units") or []
    if not isinstance(values, (list, tuple)):
        values = [values]
    return list(dict.fromkeys(_as_text(value) for value in values if _as_text(value)))


def _message_id(message: dict) -> str:
    value = _as_text(message.get("id_ticket_message") or message.get("id_message"))
    if value:
        return value
    fingerprint = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return "generated-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]


def _author_fields(message: dict) -> tuple[str, str]:
    author = message.get("author") or {}
    if isinstance(author, str):
        return author, author
    if not isinstance(author, dict):
        return "", ""
    author_type = _as_text(
        author.get("type") or author.get("role") or author.get("author_type")
    )
    author_name = _as_text(
        author.get("name")
        or author.get("display_name")
        or author.get("pseudonym")
        or author.get("email")
        or author_type
    )
    return author_type, author_name


def _attachments(message: dict) -> list[dict]:
    values = (
        message.get("ticket_message_files")
        or message.get("files")
        or message.get("attachments")
        or []
    )
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        result.append({
            key: value for key, value in item.items()
            if key not in {"data", "content", "base64"}
        })
    return result


def _safe_message_raw(message: dict) -> dict:
    safe = dict(message)
    for key in ("ticket_message_files", "files", "attachments"):
        if key in safe:
            safe[key] = _attachments(message)
    return safe


def _product_ean(product: dict) -> str:
    values = product.get("eans") or product.get("ean") or []
    if isinstance(values, dict):
        values = list(values.values())
    if not isinstance(values, (list, tuple)):
        values = [values]
    return next((_as_text(value) for value in values if _as_text(value)), "")


def normalize_order_unit(raw: dict) -> dict:
    product = raw.get("product") or {}
    buyer = raw.get("buyer") or {}
    if not isinstance(product, dict):
        product = {}
    if not isinstance(buyer, dict):
        buyer = {}
    safe_raw = {
        key: value for key, value in raw.items()
        if key not in {"billing_address", "shipping_address"}
    }
    return {
        "id_order_unit": _as_text(raw.get("id_order_unit")),
        "id_order": _as_text(raw.get("id_order")),
        "status": _as_text(raw.get("status")),
        "storefront": _as_text(raw.get("storefront")).lower(),
        "id_offer": _as_text(raw.get("id_offer")),
        "ean": _product_ean(product),
        "product_title": _as_text(product.get("title")),
        "product_url": _as_text(product.get("url")),
        "product_image": _as_text(
            product.get("main_picture") or product.get("picture")
        ),
        "currency": _as_text(raw.get("currency")).upper(),
        "price": cents_to_money(raw.get("price")),
        "shipping_rate": cents_to_money(raw.get("shipping_rate")),
        "buyer_id": _as_text(buyer.get("id_buyer")),
        "buyer_email": _as_text(buyer.get("email")),
        "buyer_pseudonym": _as_text(
            buyer.get("pseudonym") or buyer.get("name")
        ),
        "ts_created_iso": _as_text(raw.get("ts_created_iso")),
        "ts_updated_iso": _as_text(raw.get("ts_updated_iso")),
        "raw": safe_raw,
    }


def cents_to_money(value):
    if value in (None, ""):
        return None
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def encode_ticket_attachment(
    filename: str, mime_type: str, content: bytes
) -> dict:
    clean_name = Path(str(filename or "")).name.strip()
    mime = _as_text(mime_type).lower()
    if mime not in VALID_TICKET_MIME_TYPES:
        mime = TICKET_MIME_BY_SUFFIX.get(Path(clean_name).suffix.lower(), mime)
    data = bytes(content or b"")
    if not clean_name:
        raise ValueError("Nome dell’allegato mancante.")
    if mime not in VALID_TICKET_MIME_TYPES:
        raise ValueError(f"Formato allegato non supportato: {mime or 'sconosciuto'}.")
    if not data:
        raise ValueError(f"L’allegato {clean_name} è vuoto.")
    if len(data) > MAX_TICKET_ATTACHMENT_BYTES:
        raise ValueError(
            f"L’allegato {clean_name} supera il limite prudenziale di 20 MB."
        )
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "filename": clean_name,
        "mime_type": mime,
        "data": f"data:{mime};base64,{encoded}",
    }


def upsert_order_unit(
    seller_id: int, account_id: int, environment: str, item: dict
) -> None:
    execute(
        """
        INSERT INTO kaufland_support_order_units(
            seller_id,marketplace_account_id,environment,id_order_unit,id_order,
            status,storefront,id_offer,ean,product_title,product_url,product_image,
            currency,price,shipping_rate,buyer_id,buyer_email,buyer_pseudonym,
            ts_created_iso,ts_updated_iso,raw_json,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,id_order_unit) DO UPDATE SET
            seller_id=excluded.seller_id,id_order=excluded.id_order,
            status=excluded.status,storefront=excluded.storefront,
            id_offer=excluded.id_offer,ean=excluded.ean,
            product_title=excluded.product_title,product_url=excluded.product_url,
            product_image=excluded.product_image,currency=excluded.currency,
            price=excluded.price,shipping_rate=excluded.shipping_rate,
            buyer_id=excluded.buyer_id,buyer_email=excluded.buyer_email,
            buyer_pseudonym=excluded.buyer_pseudonym,
            ts_created_iso=excluded.ts_created_iso,
            ts_updated_iso=excluded.ts_updated_iso,raw_json=excluded.raw_json,
            synced_at=excluded.synced_at
        """,
        (
            seller_id, account_id, environment, item["id_order_unit"],
            item["id_order"], item["status"], item["storefront"], item["id_offer"],
            item["ean"], item["product_title"], item["product_url"],
            item["product_image"], item["currency"], item["price"],
            item["shipping_rate"], item["buyer_id"], item["buyer_email"],
            item["buyer_pseudonym"], item["ts_created_iso"], item["ts_updated_iso"],
            json_text(item["raw"]), now_iso(),
        ),
    )


def upsert_messages(
    seller_id: int, account_id: int, environment: str, id_ticket: str,
    messages: list[dict],
) -> None:
    synced = now_iso()
    with connect() as con:
        for message in messages:
            author_type, author_name = _author_fields(message)
            con.execute(
                """
                INSERT INTO kaufland_support_messages(
                    seller_id,marketplace_account_id,environment,id_ticket,
                    id_ticket_message,author_type,author_name,text,ts_created_iso,
                    attachments_json,raw_json,synced_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(
                    marketplace_account_id,environment,id_ticket,id_ticket_message
                ) DO UPDATE SET
                    seller_id=excluded.seller_id,author_type=excluded.author_type,
                    author_name=excluded.author_name,text=excluded.text,
                    ts_created_iso=excluded.ts_created_iso,
                    attachments_json=excluded.attachments_json,
                    raw_json=excluded.raw_json,synced_at=excluded.synced_at
                """,
                (
                    seller_id, account_id, environment, id_ticket,
                    _message_id(message), author_type, author_name,
                    _as_text(message.get("text")),
                    _as_text(message.get("ts_created_iso")),
                    json_text(_attachments(message)),
                    json_text(_safe_message_raw(message)), synced,
                ),
            )


def upsert_ticket(
    seller_id: int, account_id: int, environment: str, ticket: dict
) -> None:
    id_ticket = _as_text(ticket.get("id_ticket"))
    unit_ids = _order_unit_ids(ticket)
    units = rows(
        """
        SELECT * FROM kaufland_support_order_units
        WHERE marketplace_account_id=? AND environment=?
          AND id_order_unit IN ({})
        """.format(",".join("?" for _ in unit_ids) or "''"),
        (account_id, environment, *unit_ids),
    )
    messages = rows(
        """
        SELECT author_type,author_name,text,ts_created_iso
        FROM kaufland_support_messages
        WHERE marketplace_account_id=? AND environment=? AND id_ticket=?
        ORDER BY ts_created_iso,id
        """,
        (account_id, environment, id_ticket),
    )
    last = messages[-1] if messages else {}
    orders = list(dict.fromkeys(
        item["id_order"] for item in units if item.get("id_order")
    ))
    storefronts = list(dict.fromkeys(
        item["storefront"] for item in units if item.get("storefront")
    ))
    buyer_label = next(
        (
            item.get("buyer_pseudonym") or item.get("buyer_email")
            for item in units
            if item.get("buyer_pseudonym") or item.get("buyer_email")
        ),
        "",
    )
    execute(
        """
        INSERT INTO kaufland_support_tickets(
            seller_id,marketplace_account_id,environment,id_ticket,
            ids_order_units_json,id_buyer,ts_created_iso,ts_updated_iso,status,
            open_reason,topic,is_seller_responsible,fulfillment_type,
            order_ids_json,storefronts_json,buyer_label,message_count,
            last_message_at,last_message_author,last_message_preview,raw_json,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,id_ticket) DO UPDATE SET
            seller_id=excluded.seller_id,
            ids_order_units_json=excluded.ids_order_units_json,
            id_buyer=excluded.id_buyer,
            ts_created_iso=excluded.ts_created_iso,
            is_read_local=CASE
                WHEN excluded.ts_updated_iso<>kaufland_support_tickets.ts_updated_iso
                     AND excluded.is_seller_responsible=1 THEN 0
                ELSE kaufland_support_tickets.is_read_local
            END,
            read_at=CASE
                WHEN excluded.ts_updated_iso<>kaufland_support_tickets.ts_updated_iso
                     AND excluded.is_seller_responsible=1 THEN ''
                ELSE kaufland_support_tickets.read_at
            END,
            ts_updated_iso=excluded.ts_updated_iso,status=excluded.status,
            open_reason=excluded.open_reason,topic=excluded.topic,
            is_seller_responsible=excluded.is_seller_responsible,
            fulfillment_type=excluded.fulfillment_type,
            order_ids_json=excluded.order_ids_json,
            storefronts_json=excluded.storefronts_json,
            buyer_label=excluded.buyer_label,message_count=excluded.message_count,
            last_message_at=excluded.last_message_at,
            last_message_author=excluded.last_message_author,
            last_message_preview=excluded.last_message_preview,
            raw_json=excluded.raw_json,synced_at=excluded.synced_at
        """,
        (
            seller_id, account_id, environment, id_ticket, json_text(unit_ids),
            _as_text(ticket.get("id_buyer")),
            _as_text(ticket.get("ts_created_iso")),
            _as_text(ticket.get("ts_updated_iso")),
            _as_text(ticket.get("status")).lower(),
            _as_text(ticket.get("open_reason")),
            _as_text(ticket.get("topic")),
            int(bool(ticket.get("is_seller_responsible"))),
            _as_text(ticket.get("fulfillment_type")), json_text(orders),
            json_text(storefronts), buyer_label, len(messages),
            _as_text(last.get("ts_created_iso")),
            _as_text(last.get("author_name") or last.get("author_type")),
            _as_text(last.get("text"))[:300], json_text(ticket), now_iso(),
        ),
    )


def mark_ticket_read(
    account_id: int, environment: str, id_ticket: str, is_read: bool = True
) -> None:
    execute(
        """
        UPDATE kaufland_support_tickets
        SET is_read_local=?,read_at=?
        WHERE marketplace_account_id=? AND environment=? AND id_ticket=?
        """,
        (
            int(bool(is_read)), now_iso() if is_read else "",
            account_id, environment, _as_text(id_ticket),
        ),
    )


def sync_one_ticket(
    client, seller_id: int, account_id: int, environment: str, id_ticket: str
) -> dict:
    listed = response_item(client.ticket(id_ticket))
    if not listed:
        raise RuntimeError(f"Il ticket {id_ticket} non è stato restituito da Kaufland.")
    messages = fetch_ticket_messages(client, id_ticket)
    upsert_messages(seller_id, account_id, environment, id_ticket, messages)
    unit_count = 0
    errors = []
    for unit_id in _order_unit_ids(listed):
        try:
            unit = normalize_order_unit(
                response_item(client.order_unit(unit_id))
            )
            if unit["id_order_unit"]:
                upsert_order_unit(seller_id, account_id, environment, unit)
                unit_count += 1
        except Exception as error:
            errors.append({"order_unit": unit_id, "error": str(error)})
    upsert_ticket(seller_id, account_id, environment, listed)
    return {
        "ticket": id_ticket, "messages": len(messages),
        "order_units": unit_count, "errors": errors,
    }


def sync_recent_order_units(
    client, seller_id: int, account_id: int, environment: str,
    maximum: int = 250,
) -> dict:
    raw_units = fetch_order_units(client, maximum)
    saved = 0
    errors = []
    for raw in raw_units:
        try:
            item = normalize_order_unit(raw)
            if not item["id_order_unit"]:
                raise ValueError("ID unità ordine mancante.")
            upsert_order_unit(seller_id, account_id, environment, item)
            saved += 1
        except Exception as error:
            errors.append({
                "order_unit": _as_text(raw.get("id_order_unit")),
                "error": str(error),
            })
    return {"seen": len(raw_units), "saved": saved, "errors": errors}


def _audit_action(
    seller_id: int, account_id: int, environment: str, action_type: str,
    status: str, id_ticket: str = "", order_unit_ids=None,
    request_summary=None, response=None, error: str = "",
) -> int:
    return execute(
        """
        INSERT INTO kaufland_support_actions(
            seller_id,marketplace_account_id,environment,id_ticket,
            order_unit_ids_json,action_type,status,request_summary_json,
            response_json,error,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seller_id, account_id, environment, _as_text(id_ticket),
            json_text(list(order_unit_ids or [])), action_type, status,
            json_text(request_summary or {}), json_text(response or {}),
            _as_text(error), now_iso(),
        ),
    )


def send_support_message(
    client, seller_id: int, account_id: int, environment: str,
    id_ticket: str, text: str, interim_notice: bool = False,
    attachments: list[dict] | None = None,
) -> dict:
    message = str(text or "").strip()
    if not message:
        raise ValueError("Scrivi il messaggio da inviare.")
    files = list(attachments or [])
    summary = {
        "text": message,
        "interim_notice": bool(interim_notice),
        "attachments": [
            {
                "filename": item.get("filename", ""),
                "mime_type": item.get("mime_type", ""),
            }
            for item in files
        ],
    }
    try:
        response = client.send_ticket_message(
            id_ticket, message, interim_notice, files
        )
        _audit_action(
            seller_id, account_id, environment,
            "interim_notice" if interim_notice else "send_message",
            "success", id_ticket=id_ticket, request_summary=summary,
            response=response,
        )
    except Exception as error:
        _audit_action(
            seller_id, account_id, environment,
            "interim_notice" if interim_notice else "send_message",
            "failed", id_ticket=id_ticket, request_summary=summary,
            error=str(error),
        )
        raise
    sync_error = ""
    try:
        sync_one_ticket(client, seller_id, account_id, environment, id_ticket)
    except Exception as error:
        sync_error = str(error)
    return {"response": response, "sync_error": sync_error}


def close_support_ticket(
    client, seller_id: int, account_id: int, environment: str, id_ticket: str
) -> dict:
    try:
        response = client.close_ticket(id_ticket)
        _audit_action(
            seller_id, account_id, environment, "close_ticket", "success",
            id_ticket=id_ticket, response=response,
        )
    except Exception as error:
        _audit_action(
            seller_id, account_id, environment, "close_ticket", "failed",
            id_ticket=id_ticket, error=str(error),
        )
        raise
    sync_error = ""
    try:
        sync_one_ticket(client, seller_id, account_id, environment, id_ticket)
    except Exception as error:
        sync_error = str(error)
    return {"response": response, "sync_error": sync_error}


def open_support_ticket(
    client, seller_id: int, account_id: int, environment: str,
    order_unit_ids: list[int], reason: str, message: str,
) -> dict:
    ids = list(dict.fromkeys(int(value) for value in order_unit_ids))
    clean_reason = _as_text(reason)
    clean_message = str(message or "").strip()
    if not ids:
        raise ValueError("Seleziona almeno un’unità ordine.")
    if clean_reason not in TICKET_REASONS:
        raise ValueError("Motivo di apertura ticket non valido.")
    if not clean_message:
        raise ValueError("Scrivi il messaggio iniziale.")
    summary = {
        "order_unit_ids": ids, "reason": clean_reason,
        "message": clean_message,
    }
    try:
        response = client.open_ticket(ids, clean_reason, clean_message)
        opened = response_item(response)
        id_ticket = ""
        if isinstance(response, dict):
            id_ticket = _as_text(
                opened.get("id_ticket") or response.get("id_ticket")
            )
        _audit_action(
            seller_id, account_id, environment, "open_ticket", "success",
            id_ticket=id_ticket, order_unit_ids=ids,
            request_summary=summary, response=response,
        )
    except Exception as error:
        _audit_action(
            seller_id, account_id, environment, "open_ticket", "failed",
            order_unit_ids=ids, request_summary=summary, error=str(error),
        )
        raise
    sync_error = ""
    if id_ticket:
        try:
            sync_one_ticket(client, seller_id, account_id, environment, id_ticket)
        except Exception as error:
            sync_error = str(error)
    return {
        "id_ticket": id_ticket, "response": response, "sync_error": sync_error
    }


def sync_support(
    client, seller_id: int, account_id: int, environment: str,
    maximum_tickets: int | None = None,
    progress: ProgressCallback | None = None,
) -> dict:
    started = now_iso()
    sync_id = execute(
        """
        INSERT INTO kaufland_support_syncs(
            seller_id,marketplace_account_id,environment,status,started_at
        ) VALUES(?,?,?,?,?)
        """,
        (seller_id, account_id, environment, "running", started),
    )
    try:
        tickets = fetch_tickets(client, maximum_tickets)
    except Exception as error:
        execute(
            """
            UPDATE kaufland_support_syncs SET status='failed',errors_json=?,
            completed_at=? WHERE id=?
            """,
            (json_text([{"phase": "tickets", "error": str(error)}]), now_iso(), sync_id),
        )
        raise
    errors: list[dict] = []
    messages_saved = 0
    units_saved = 0
    tickets_saved = 0
    messages_by_ticket: dict[str, list[dict]] = {}
    use_global_messages = False
    try:
        all_messages = fetch_all_ticket_messages(client)
        for message in all_messages:
            message_ticket = _as_text(message.get("id_ticket"))
            if message_ticket:
                messages_by_ticket.setdefault(message_ticket, []).append(message)
        use_global_messages = bool(all_messages) and bool(messages_by_ticket)
    except Exception:
        # Older/limited accounts can reject the unfiltered endpoint. The
        # reliable per-ticket fallback below still downloads every message.
        use_global_messages = False
    total = max(1, len(tickets))
    for index, listed_ticket in enumerate(tickets, start=1):
        id_ticket = _as_text(listed_ticket.get("id_ticket"))
        if progress:
            progress(index - 1, total, f"Ticket {id_ticket or index}")
        if not id_ticket:
            errors.append({"ticket": "", "error": "ID ticket mancante"})
            continue
        detail = listed_ticket
        try:
            messages = (
                messages_by_ticket.get(id_ticket, [])
                if use_global_messages
                else fetch_ticket_messages(client, id_ticket)
            )
            upsert_messages(seller_id, account_id, environment, id_ticket, messages)
            messages_saved += len(messages)
        except Exception as error:
            errors.append({"ticket": id_ticket, "phase": "messages", "error": str(error)})
        for unit_id in _order_unit_ids(detail):
            cached = row(
                """
                SELECT id FROM kaufland_support_order_units
                WHERE marketplace_account_id=? AND environment=? AND id_order_unit=?
                """,
                (account_id, environment, unit_id),
            )
            if cached:
                continue
            try:
                unit_response = client.order_unit(unit_id)
                unit = normalize_order_unit(response_item(unit_response))
                if unit["id_order_unit"]:
                    upsert_order_unit(
                        seller_id, account_id, environment, unit
                    )
                    units_saved += 1
            except Exception as error:
                errors.append({
                    "ticket": id_ticket, "order_unit": unit_id,
                    "phase": "order_unit", "error": str(error),
                })
        try:
            upsert_ticket(seller_id, account_id, environment, detail)
            tickets_saved += 1
        except Exception as error:
            errors.append({"ticket": id_ticket, "phase": "save", "error": str(error)})
        if progress:
            progress(index, total, f"Ticket {id_ticket}")
    completed = now_iso()
    execute(
        """
        UPDATE kaufland_support_syncs SET status=?,tickets_seen=?,
        tickets_saved=?,messages_saved=?,order_units_saved=?,errors_json=?,
        completed_at=? WHERE id=?
        """,
        (
            "completed_with_errors" if errors else "completed", len(tickets),
            tickets_saved, messages_saved, units_saved, json_text(errors),
            completed, sync_id,
        ),
    )
    return {
        "tickets_seen": len(tickets), "tickets_saved": tickets_saved,
        "messages_saved": messages_saved, "order_units_saved": units_saved,
        "errors": errors, "started_at": started, "completed_at": completed,
    }


def parse_iso(value: str) -> datetime | None:
    text = _as_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def add_business_hours(start: datetime, hours: int = 48) -> datetime:
    current = start
    remaining = max(0, int(hours))
    while remaining:
        current += timedelta(hours=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def ticket_sla(ticket: dict, now: datetime | None = None) -> dict:
    if not bool(ticket.get("is_seller_responsible")) or ticket.get("status") != "opened":
        return {"label": "Non in carico", "deadline": "", "overdue": False, "hours": None}
    start = parse_iso(
        ticket.get("last_message_at")
        or ticket.get("ts_updated_iso")
        or ticket.get("ts_created_iso")
    )
    if not start:
        return {"label": "Da rispondere", "deadline": "", "overdue": False, "hours": None}
    deadline = add_business_hours(start, 48)
    reference = now or datetime.now(timezone.utc)
    delta_hours = (deadline - reference).total_seconds() / 3600
    overdue = delta_hours < 0
    return {
        "label": "Scaduto" if overdue else "Da rispondere",
        "deadline": deadline.isoformat(timespec="minutes"),
        "overdue": overdue,
        "hours": round(delta_hours, 1),
    }
