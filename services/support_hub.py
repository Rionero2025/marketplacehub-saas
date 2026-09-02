from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from services.ai_providers import (
    complete_json, complete_text_chain, provider_result_text,
    ensure_schema as ensure_ai_provider_schema, get_profile,
)
from services.db import connect, execute, json_text, now_iso, row, rows
from services.security import decrypt_dict, encrypt_dict

DEFAULT_SLA_HOURS = 24
SAFE_AUTO_CATEGORIES = {
    "tracking",
    "shipping_status",
    "return_instructions",
    "generic_information",
    "invoice_available",
    "acknowledgement",
}
HUMAN_ONLY_CATEGORIES = {
    "refund",
    "compensation",
    "legal",
    "fraud",
    "warranty_dispute",
    "cancellation",
    "damaged_item_with_compensation",
    "unknown",
}


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_iso(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def ensure_schema() -> None:
    ensure_ai_provider_schema()
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS support_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'live',
                external_thread_id TEXT NOT NULL,
                order_ids_json TEXT NOT NULL DEFAULT '[]',
                customer_label TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                api_status TEXT NOT NULL DEFAULT '',
                normalized_status TEXT NOT NULL DEFAULT '',
                reply_needed INTEGER NOT NULL DEFAULT 0,
                reply_needed_since TEXT NOT NULL DEFAULT '',
                sla_deadline TEXT NOT NULL DEFAULT '',
                priority_status TEXT NOT NULL DEFAULT '',
                last_message_at TEXT NOT NULL DEFAULT '',
                last_sender_type TEXT NOT NULL DEFAULT '',
                last_sender_label TEXT NOT NULL DEFAULT '',
                last_message_preview TEXT NOT NULL DEFAULT '',
                message_count INTEGER NOT NULL DEFAULT 0,
                is_read_local INTEGER NOT NULL DEFAULT 0,
                read_at TEXT NOT NULL DEFAULT '',
                auto_ai_enabled INTEGER NOT NULL DEFAULT 0,
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE(marketplace_account_id,environment,external_thread_id)
            );
            CREATE INDEX IF NOT EXISTS idx_support_threads_scope
            ON support_threads(
                seller_id,marketplace_account_id,marketplace,environment,
                normalized_status,reply_needed,sla_deadline,last_message_at
            );
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'live',
                external_thread_id TEXT NOT NULL,
                external_message_id TEXT NOT NULL,
                sender_type TEXT NOT NULL DEFAULT '',
                sender_label TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                attachments_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                synced_at TEXT NOT NULL,
                UNIQUE(
                    marketplace_account_id,environment,external_thread_id,
                    external_message_id
                )
            );
            CREATE INDEX IF NOT EXISTS idx_support_messages_thread
            ON support_messages(
                marketplace_account_id,environment,external_thread_id,sent_at,id
            );
            CREATE TABLE IF NOT EXISTS support_syncs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'live',
                sync_mode TEXT NOT NULL DEFAULT 'incremental',
                status TEXT NOT NULL,
                threads_seen INTEGER NOT NULL DEFAULT 0,
                threads_new INTEGER NOT NULL DEFAULT 0,
                threads_updated INTEGER NOT NULL DEFAULT 0,
                messages_saved INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_support_syncs_scope
            ON support_syncs(
                marketplace_account_id,environment,status,completed_at
            );
            CREATE TABLE IF NOT EXISTS support_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'live',
                external_thread_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_support_actions_scope
            ON support_actions(
                marketplace_account_id,environment,external_thread_id,created_at
            );
            CREATE TABLE IF NOT EXISTS support_ai_settings (
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT 'gpt-5-mini',
                confidence_threshold REAL NOT NULL DEFAULT 0.92,
                sla_hours INTEGER NOT NULL DEFAULT 24,
                auto_batch_limit INTEGER NOT NULL DEFAULT 10,
                allowed_categories_json TEXT NOT NULL DEFAULT '[]',
                instructions TEXT NOT NULL DEFAULT '',
                api_key_encrypted TEXT NOT NULL DEFAULT '',
                ai_profile_id INTEGER REFERENCES ai_provider_profiles(id) ON DELETE SET NULL,
                fallback_profile_ids_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(seller_id,marketplace_account_id)
            );
            CREATE TABLE IF NOT EXISTS support_ai_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER NOT NULL
                    REFERENCES marketplace_accounts(id) ON DELETE CASCADE,
                marketplace TEXT NOT NULL,
                environment TEXT NOT NULL DEFAULT 'live',
                external_thread_id TEXT NOT NULL,
                source_updated_at TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                auto_send_allowed INTEGER NOT NULL DEFAULT 0,
                human_review_required INTEGER NOT NULL DEFAULT 1,
                interim_notice INTEGER NOT NULL DEFAULT 0,
                language TEXT NOT NULL DEFAULT '',
                reply_text TEXT NOT NULL DEFAULT '',
                reasoning TEXT NOT NULL DEFAULT '',
                response_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_support_ai_drafts_scope
            ON support_ai_drafts(
                marketplace_account_id,environment,external_thread_id,created_at
            );
            """
        )
        columns = {
            item["name"] for item in con.execute(
                "PRAGMA table_info(support_ai_settings)"
            ).fetchall()
        }
        if "api_key_encrypted" not in columns:
            con.execute(
                "ALTER TABLE support_ai_settings "
                "ADD COLUMN api_key_encrypted TEXT NOT NULL DEFAULT ''"
            )
        if "auto_batch_limit" not in columns:
            con.execute(
                "ALTER TABLE support_ai_settings "
                "ADD COLUMN auto_batch_limit INTEGER NOT NULL DEFAULT 10"
            )
        if "ai_profile_id" not in columns:
            con.execute(
                "ALTER TABLE support_ai_settings "
                "ADD COLUMN ai_profile_id INTEGER REFERENCES ai_provider_profiles(id) ON DELETE SET NULL"
            )
        if "fallback_profile_ids_json" not in columns:
            con.execute(
                "ALTER TABLE support_ai_settings "
                "ADD COLUMN fallback_profile_ids_json TEXT NOT NULL DEFAULT '[]'"
            )


def json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def normalized_status(
    *, marketplace: str, api_status: str, reply_needed: bool,
    no_reply_needed: bool = False, last_sender_type: str = "",
) -> str:
    market = clean_text(marketplace).lower()
    status = clean_text(api_status).lower()
    sender = clean_text(last_sender_type).upper()
    if market == "kaufland" and status and status != "opened":
        return "closed"
    if reply_needed:
        return "needs_reply"
    if no_reply_needed:
        return "informational"
    if sender in {"SHOP", "SELLER"}:
        return "waiting_customer"
    return "waiting_customer" if market == "kaufland" and status == "opened" else "informational"


def sla_state(
    reply_needed_since: Any, *, reply_needed: bool,
    sla_hours: int = DEFAULT_SLA_HOURS, now: datetime | None = None,
) -> dict[str, Any]:
    if not reply_needed:
        return {
            "deadline": "", "priority_status": "not_required",
            "hours_remaining": None, "overdue": False, "urgent": False,
        }
    start = parse_iso(reply_needed_since)
    if not start:
        return {
            "deadline": "", "priority_status": "reply_needed",
            "hours_remaining": None, "overdue": False, "urgent": False,
        }
    deadline = start + timedelta(hours=max(1, int(sla_hours)))
    reference = now or datetime.now(timezone.utc)
    remaining = (deadline - reference).total_seconds() / 3600
    overdue = remaining < 0
    urgent = not overdue and remaining < 24
    return {
        "deadline": deadline.isoformat(timespec="minutes"),
        "priority_status": "overdue" if overdue else ("urgent" if urgent else "in_time"),
        "hours_remaining": round(remaining, 2),
        "overdue": overdue,
        "urgent": urgent,
    }


def message_fingerprint(raw: Mapping[str, Any]) -> str:
    serialized = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:32]


def upsert_message(
    *, seller_id: int, account_id: int, marketplace: str, environment: str,
    thread_id: str, message_id: str, sender_type: str, sender_label: str,
    body: str, sent_at: str, attachments: Iterable[Mapping[str, Any]] = (),
    raw: Mapping[str, Any] | None = None,
) -> None:
    execute(
        """
        INSERT INTO support_messages(
            seller_id,marketplace_account_id,marketplace,environment,
            external_thread_id,external_message_id,sender_type,sender_label,
            body,sent_at,attachments_json,raw_json,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(
            marketplace_account_id,environment,external_thread_id,
            external_message_id
        ) DO UPDATE SET
            seller_id=excluded.seller_id,marketplace=excluded.marketplace,
            sender_type=excluded.sender_type,sender_label=excluded.sender_label,
            body=excluded.body,sent_at=excluded.sent_at,
            attachments_json=excluded.attachments_json,
            raw_json=excluded.raw_json,synced_at=excluded.synced_at
        """,
        (
            seller_id, account_id, clean_text(marketplace).lower(), environment,
            clean_text(thread_id), clean_text(message_id), clean_text(sender_type),
            clean_text(sender_label), clean_text(body), clean_text(sent_at),
            json_text(list(attachments)), json_text(dict(raw or {})), now_iso(),
        ),
    )


def upsert_thread(
    *, seller_id: int, account_id: int, marketplace: str, environment: str,
    thread_id: str, order_ids: Iterable[str], customer_label: str, topic: str,
    api_status: str, reply_needed: bool, reply_needed_since: str,
    last_message_at: str, last_sender_type: str, last_sender_label: str,
    last_message_preview: str, message_count: int, no_reply_needed: bool = False,
    raw: Mapping[str, Any] | None = None, sla_hours: int = DEFAULT_SLA_HOURS,
) -> str:
    state = normalized_status(
        marketplace=marketplace, api_status=api_status,
        reply_needed=reply_needed, no_reply_needed=no_reply_needed,
        last_sender_type=last_sender_type,
    )
    sla = sla_state(
        reply_needed_since or last_message_at,
        reply_needed=reply_needed,
        sla_hours=sla_hours,
    )
    execute(
        """
        INSERT INTO support_threads(
            seller_id,marketplace_account_id,marketplace,environment,
            external_thread_id,order_ids_json,customer_label,topic,api_status,
            normalized_status,reply_needed,reply_needed_since,sla_deadline,
            priority_status,last_message_at,last_sender_type,last_sender_label,
            last_message_preview,message_count,raw_json,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(marketplace_account_id,environment,external_thread_id)
        DO UPDATE SET
            seller_id=excluded.seller_id,marketplace=excluded.marketplace,
            order_ids_json=excluded.order_ids_json,
            customer_label=excluded.customer_label,topic=excluded.topic,
            api_status=excluded.api_status,
            normalized_status=excluded.normalized_status,
            reply_needed=excluded.reply_needed,
            reply_needed_since=excluded.reply_needed_since,
            sla_deadline=excluded.sla_deadline,
            priority_status=excluded.priority_status,
            last_message_at=excluded.last_message_at,
            last_sender_type=excluded.last_sender_type,
            last_sender_label=excluded.last_sender_label,
            last_message_preview=excluded.last_message_preview,
            message_count=excluded.message_count,
            is_read_local=CASE
                WHEN excluded.last_message_at<>support_threads.last_message_at
                     AND excluded.reply_needed=1 THEN 0
                ELSE support_threads.is_read_local
            END,
            raw_json=excluded.raw_json,synced_at=excluded.synced_at
        """,
        (
            seller_id, account_id, clean_text(marketplace).lower(), environment,
            clean_text(thread_id), json_text(list(dict.fromkeys(
                clean_text(value) for value in order_ids if clean_text(value)
            ))), clean_text(customer_label), clean_text(topic), clean_text(api_status),
            state, int(bool(reply_needed)), clean_text(reply_needed_since),
            sla["deadline"], sla["priority_status"], clean_text(last_message_at),
            clean_text(last_sender_type), clean_text(last_sender_label),
            clean_text(last_message_preview)[:1000], int(message_count or 0),
            json_text(dict(raw or {})), now_iso(),
        ),
    )
    return state


def mark_read(account_id: int, environment: str, thread_id: str, read_value: bool = True) -> None:
    execute(
        """
        UPDATE support_threads SET is_read_local=?,read_at=?
        WHERE marketplace_account_id=? AND environment=? AND external_thread_id=?
        """,
        (
            int(bool(read_value)), now_iso() if read_value else "",
            account_id, environment, clean_text(thread_id),
        ),
    )


def latest_successful_sync(account_id: int, environment: str) -> dict | None:
    return row(
        """
        SELECT * FROM support_syncs
        WHERE marketplace_account_id=? AND environment=?
          AND status IN ('completed','completed_with_errors')
        ORDER BY completed_at DESC,id DESC LIMIT 1
        """,
        (account_id, environment),
    )


def start_sync(
    *, seller_id: int, account_id: int, marketplace: str,
    environment: str, mode: str,
) -> int:
    return execute(
        """
        INSERT INTO support_syncs(
            seller_id,marketplace_account_id,marketplace,environment,
            sync_mode,status,started_at
        ) VALUES(?,?,?,?,?,'running',?)
        """,
        (seller_id, account_id, marketplace, environment, mode, now_iso()),
    )


def finish_sync(
    sync_id: int, *, status: str, seen: int = 0, new: int = 0,
    updated: int = 0, messages: int = 0, errors: Iterable[Mapping[str, Any]] = (),
) -> None:
    execute(
        """
        UPDATE support_syncs SET status=?,threads_seen=?,threads_new=?,
        threads_updated=?,messages_saved=?,errors_json=?,completed_at=? WHERE id=?
        """,
        (
            status, int(seen), int(new), int(updated), int(messages),
            json_text(list(errors)), now_iso(), sync_id,
        ),
    )


def audit_action(
    *, seller_id: int, account_id: int, marketplace: str, environment: str,
    thread_id: str, action_type: str, status: str,
    request_data: Mapping[str, Any] | None = None,
    response_data: Mapping[str, Any] | None = None, error: str = "",
) -> int:
    return execute(
        """
        INSERT INTO support_actions(
            seller_id,marketplace_account_id,marketplace,environment,
            external_thread_id,action_type,status,request_json,response_json,
            error,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seller_id, account_id, marketplace, environment, thread_id,
            action_type, status, json_text(dict(request_data or {})),
            json_text(dict(response_data or {})), clean_text(error), now_iso(),
        ),
    )


def get_ai_settings(seller_id: int, account_id: int) -> dict:
    ensure_schema()
    value = row(
        "SELECT * FROM support_ai_settings WHERE seller_id=? AND marketplace_account_id=?",
        (seller_id, account_id),
    )
    if value:
        value["allowed_categories"] = json_list(value.get("allowed_categories_json"))
        value["fallback_profile_ids"] = [
            int(item) for item in json_list(value.get("fallback_profile_ids_json"))
            if str(item).isdigit()
        ]
        return value
    return {
        "seller_id": seller_id,
        "marketplace_account_id": account_id,
        "enabled": 0,
        "model": "gpt-5-mini",
        "confidence_threshold": 0.92,
        "sla_hours": DEFAULT_SLA_HOURS,
        "auto_batch_limit": 10,
        "allowed_categories": sorted(SAFE_AUTO_CATEGORIES),
        "instructions": "",
        "ai_profile_id": None,
        "fallback_profile_ids": [],
    }


def save_ai_settings(
    *, seller_id: int, account_id: int, enabled: bool, model: str,
    confidence_threshold: float, sla_hours: int,
    allowed_categories: Iterable[str], instructions: str,
    api_key: str = "", auto_batch_limit: int = 10,
    ai_profile_id: int | None = None,
    fallback_profile_ids: Iterable[int] = (),
) -> None:
    current = row(
        "SELECT api_key_encrypted FROM support_ai_settings "
        "WHERE seller_id=? AND marketplace_account_id=?",
        (seller_id, account_id),
    ) or {}
    encrypted_key = clean_text(current.get("api_key_encrypted"))
    if clean_text(api_key):
        encrypted_key = encrypt_dict({"api_key": clean_text(api_key)})
    execute(
        """
        INSERT INTO support_ai_settings(
            seller_id,marketplace_account_id,enabled,model,
            confidence_threshold,sla_hours,auto_batch_limit,allowed_categories_json,
            instructions,api_key_encrypted,ai_profile_id,
            fallback_profile_ids_json,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id,marketplace_account_id) DO UPDATE SET
            enabled=excluded.enabled,model=excluded.model,
            confidence_threshold=excluded.confidence_threshold,
            sla_hours=excluded.sla_hours,
            auto_batch_limit=excluded.auto_batch_limit,
            allowed_categories_json=excluded.allowed_categories_json,
            instructions=excluded.instructions,
            api_key_encrypted=excluded.api_key_encrypted,
            ai_profile_id=excluded.ai_profile_id,
            fallback_profile_ids_json=excluded.fallback_profile_ids_json,
            updated_at=excluded.updated_at
        """,
        (
            seller_id, account_id, int(bool(enabled)), clean_text(model) or "gpt-5-mini",
            max(0.0, min(1.0, float(confidence_threshold))), max(1, int(sla_hours)),
            max(1, min(100, int(auto_batch_limit))),
            json_text(sorted(set(clean_text(x) for x in allowed_categories if clean_text(x)))),
            clean_text(instructions), encrypted_key,
            int(ai_profile_id) if ai_profile_id else None,
            json_text([int(item) for item in fallback_profile_ids if int(item) > 0]),
            now_iso(),
        ),
    )


def stored_ai_api_key(settings: Mapping[str, Any]) -> str:
    encrypted = clean_text(settings.get("api_key_encrypted"))
    if not encrypted:
        return ""
    try:
        return clean_text(decrypt_dict(encrypted).get("api_key"))
    except Exception:
        return ""


def configured_ai_profiles(settings: Mapping[str, Any], seller_id: int) -> list[dict]:
    result: list[dict] = []
    seen: set[int] = set()
    ids = []
    primary = settings.get("ai_profile_id")
    if primary:
        ids.append(primary)
    ids.extend(settings.get("fallback_profile_ids") or json_list(settings.get("fallback_profile_ids_json")))
    for value in ids:
        try:
            profile_id = int(value)
        except (TypeError, ValueError):
            continue
        if profile_id <= 0 or profile_id in seen:
            continue
        profile = get_profile(profile_id, seller_id)
        if profile and bool(profile.get("enabled", 1)):
            result.append(profile)
            seen.add(profile_id)
    return result


def set_thread_auto_ai(account_id: int, environment: str, thread_ids: Iterable[str], enabled: bool) -> int:
    ids = list(dict.fromkeys(clean_text(item) for item in thread_ids if clean_text(item)))
    if not ids:
        return 0
    with connect() as con:
        placeholders = ",".join("?" for _ in ids)
        cursor = con.execute(
            f"""
            UPDATE support_threads SET auto_ai_enabled=?
            WHERE marketplace_account_id=? AND environment=?
              AND external_thread_id IN ({placeholders})
            """,
            (int(bool(enabled)), account_id, environment, *ids),
        )
        return int(cursor.rowcount or 0)


def order_context(
    seller_id: int,
    account_id: int,
    order_ids: Iterable[str],
    *,
    environment: str = "",
) -> list[dict]:
    """Return order rows used by the ticket console and the AI prompt.

    Accounting remains the primary source. For Kaufland tickets we also merge
    the order-unit mirror maintained by the support synchronizer, because a
    ticket can exist before the accounting page has downloaded the order.
    """
    ids = list(dict.fromkeys(clean_text(item) for item in order_ids if clean_text(item)))
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    result: list[dict] = []
    try:
        result.extend(rows(
            f"""
            SELECT order_id,order_line_id,order_created,market_label,raw_status,status_label,
                   supplier,product_title,ean,quantity,tracking,customer_name,
                   supplier_order_number,receipt,note
            FROM accounting_order_lines
            WHERE seller_id=? AND marketplace_account_id=?
              AND order_id IN ({placeholders})
            ORDER BY order_created,row_key
            """,
            (seller_id, account_id, *ids),
        ))
    except Exception:
        pass

    seen = {
        (clean_text(item.get("order_id")), clean_text(item.get("order_line_id")))
        for item in result
    }
    try:
        env_clause = " AND environment=?" if clean_text(environment) else ""
        params: list[Any] = [account_id, *ids]
        if clean_text(environment):
            params.append(clean_text(environment))
        support_rows = rows(
            f"""
            SELECT id_order AS order_id,id_order_unit AS order_line_id,
                   ts_created_iso AS order_created,storefront AS market_label,
                   status AS raw_status,status AS status_label,'' AS supplier,
                   product_title,ean,1 AS quantity,'' AS tracking,
                   COALESCE(NULLIF(buyer_pseudonym,''),buyer_email) AS customer_name,
                   '' AS supplier_order_number,'' AS receipt,'' AS note
            FROM kaufland_support_order_units
            WHERE marketplace_account_id=? AND id_order IN ({placeholders})
            {env_clause}
            ORDER BY ts_created_iso,id_order_unit
            """,
            tuple(params),
        )
        for item in support_rows:
            key = (clean_text(item.get("order_id")), clean_text(item.get("order_line_id")))
            if key not in seen:
                result.append(item)
                seen.add(key)
    except Exception:
        pass
    return result


def strip_html(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(value or ""))
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class AiSuggestion:
    category: str
    confidence: float
    auto_send_allowed: bool
    human_review_required: bool
    interim_notice: bool
    language: str
    reply_text: str
    reasoning: str
    raw: dict[str, Any]


TICKET_AI_JSON_SCHEMA: dict[str, Any] = {
    "name": "ticket_reply_suggestion",
    "description": "Classificazione del ticket e bozza di risposta al cliente.",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(SAFE_AUTO_CATEGORIES | HUMAN_ONLY_CATEGORIES),
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "auto_send_allowed": {"type": "boolean"},
            "human_review_required": {"type": "boolean"},
            "interim_notice": {"type": "boolean"},
            "language": {"type": "string"},
            "reply_text": {"type": "string"},
            "reasoning": {"type": "string"},
        },
        "required": [
            "category", "confidence", "auto_send_allowed",
            "human_review_required", "interim_notice", "language",
            "reply_text", "reasoning",
        ],
    },
}


def _extract_response_json(response: Any) -> dict:
    text = clean_text(getattr(response, "output_text", ""))
    if not text and isinstance(response, dict):
        text = clean_text(response.get("output_text"))
    if not text:
        raise RuntimeError("Il modello IA non ha restituito testo utilizzabile.")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("La risposta IA non è un JSON valido.") from exc


def generate_ai_suggestion(
    *, thread: Mapping[str, Any], messages: Iterable[Mapping[str, Any]],
    order_rows: Iterable[Mapping[str, Any]], seller_instructions: str = "",
    profiles: Iterable[Mapping[str, Any]] | None = None,
    account_id: int | None = None,
    api_key: str = "", model: str = "gpt-5-mini",
) -> AiSuggestion:
    conversation = [
        {
            "sender_type": clean_text(item.get("sender_type")),
            "sender": clean_text(item.get("sender_label")),
            "date": clean_text(item.get("sent_at")),
            "body": clean_text(item.get("body")),
        }
        for item in messages
    ]
    order_context_rows = [dict(item) for item in order_rows]
    instructions = (
        "Sei l'assistente ticket di un venditore marketplace. Classifica il messaggio e "
        "prepara una risposta professionale nella lingua del cliente. Usa solo i dati "
        "forniti. Non inventare tracking, date, rimborsi, autorizzazioni o condizioni. "
        "L'invio automatico è consentito soltanto per tracking già disponibile, stato "
        "spedizione, istruzioni generiche di reso già definite, fattura già disponibile, "
        "conferma ricezione e domande informative senza conseguenze economiche. Imposta "
        "human_review_required=true per rimborsi, sconti, risarcimenti, contestazioni, "
        "frodi, garanzie controverse, annullamenti, danni con richieste economiche, dati "
        "incerti o bassa confidenza. La risposta deve essere testo semplice, senza HTML. "
        "Restituisci esclusivamente un oggetto JSON con queste chiavi: category, confidence "
        "(numero 0-1), auto_send_allowed, human_review_required, interim_notice, language, "
        "reply_text, reasoning. category deve essere una tra: "
        + ", ".join(sorted(SAFE_AUTO_CATEGORIES | HUMAN_ONLY_CATEGORIES)) + "."
    )
    if clean_text(seller_instructions):
        instructions += "\nIstruzioni specifiche del Seller:\n" + clean_text(seller_instructions)
    payload = {
        "thread": {
            "marketplace": thread.get("marketplace"),
            "topic": thread.get("topic"),
            "orders": json_list(thread.get("order_ids_json")),
            "customer": thread.get("customer_label"),
            "reply_needed": bool(thread.get("reply_needed")),
        },
        "conversation": conversation,
        "order_context": order_context_rows,
    }
    selected_profiles = [dict(item) for item in (profiles or []) if item]
    if not selected_profiles and clean_text(api_key):
        selected_profiles = [{
            "id": 0,
            "seller_id": int(thread.get("seller_id") or 0),
            "name": "OpenAI legacy",
            "provider": "openai",
            "model": clean_text(model) or "gpt-5-mini",
            "base_url": "https://api.openai.com/v1",
            "enabled": 1,
            "temperature": 0.2,
            "max_tokens": 1200,
            "timeout_seconds": 60,
            "retries": 2,
            "secrets_encrypted": encrypt_dict({"api_key": clean_text(api_key)}),
            "config_json": "{}",
        }]
    if not selected_profiles:
        raise RuntimeError("Seleziona almeno un profilo IA attivo oppure configura una chiave OpenAI legacy.")
    structured_error = ""
    try:
        data, used_profile, provider_result = complete_json(
            selected_profiles,
            system=instructions,
            prompt=json.dumps(payload, ensure_ascii=False, default=str),
            purpose="ticket_reply",
            account_id=account_id,
            json_schema=TICKET_AI_JSON_SCHEMA,
        )
    except Exception as exc:
        # Never leave the operator without a draft merely because a provider or
        # gateway ignored Structured Outputs. Retry in plain-text mode and mark the
        # result as requiring human review, so it can never be auto-sent.
        structured_error = clean_text(exc)
        fallback_system = (
            "Sei l'assistente ticket di un venditore marketplace. Scrivi esclusivamente "
            "il testo della risposta da inviare al cliente, nella lingua del cliente. "
            "Non usare JSON, etichette, markdown o spiegazioni interne. Non inventare "
            "tracking, date, rimborsi, autorizzazioni o informazioni non presenti nei dati."
        )
        if clean_text(seller_instructions):
            fallback_system += "\nIstruzioni specifiche del Seller:\n" + clean_text(seller_instructions)
        provider_result, used_profile = complete_text_chain(
            selected_profiles,
            system=fallback_system,
            prompt=json.dumps(payload, ensure_ascii=False, default=str),
            purpose="ticket_reply_text_fallback",
            account_id=account_id,
        )
        reply_text = strip_html(provider_result_text(provider_result))
        if not reply_text:
            raise RuntimeError(structured_error or "Il provider IA non ha restituito una bozza utilizzabile.")
        data = {
            "category": "unknown",
            "confidence": 0.35,
            "auto_send_allowed": False,
            "human_review_required": True,
            "interim_notice": False,
            "language": "",
            "reply_text": reply_text,
            "reasoning": (
                "Bozza generata in modalità testo perché il provider non ha rispettato "
                "il formato strutturato. Controllo umano obbligatorio."
            ),
        }
    category = clean_text(data.get("category")) or "unknown"
    human = bool(data.get("human_review_required")) or category in HUMAN_ONLY_CATEGORIES
    auto_allowed = bool(data.get("auto_send_allowed")) and not human and category in SAFE_AUTO_CATEGORIES
    raw = dict(data)
    raw["provider"] = used_profile.get("provider")
    raw["provider_profile"] = used_profile.get("name")
    raw["model"] = provider_result.model
    if structured_error:
        raw["structured_output_error"] = structured_error
        raw["text_fallback_used"] = True
    return AiSuggestion(
        category=category,
        confidence=max(0.0, min(1.0, float(data.get("confidence") or 0))),
        auto_send_allowed=auto_allowed,
        human_review_required=human,
        interim_notice=bool(data.get("interim_notice")),
        language=clean_text(data.get("language")),
        reply_text=strip_html(clean_text(data.get("reply_text"))),
        reasoning=clean_text(data.get("reasoning")),
        raw=raw,
    )


def save_ai_draft(
    *, seller_id: int, account_id: int, marketplace: str, environment: str,
    thread_id: str, source_updated_at: str, suggestion: AiSuggestion,
) -> int:
    response_hash = hashlib.sha256(
        suggestion.reply_text.encode("utf-8")
    ).hexdigest()
    return execute(
        """
        INSERT INTO support_ai_drafts(
            seller_id,marketplace_account_id,marketplace,environment,
            external_thread_id,source_updated_at,category,confidence,
            auto_send_allowed,human_review_required,interim_notice,language,
            reply_text,reasoning,response_hash,raw_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            seller_id, account_id, marketplace, environment, thread_id,
            source_updated_at, suggestion.category, suggestion.confidence,
            int(suggestion.auto_send_allowed), int(suggestion.human_review_required),
            int(suggestion.interim_notice), suggestion.language,
            suggestion.reply_text, suggestion.reasoning, response_hash,
            json_text(suggestion.raw), now_iso(),
        ),
    )


def duplicate_sent_response(account_id: int, environment: str, thread_id: str, reply_text: str) -> bool:
    digest = hashlib.sha256(clean_text(reply_text).encode("utf-8")).hexdigest()
    return bool(row(
        """
        SELECT id FROM support_ai_drafts
        WHERE marketplace_account_id=? AND environment=? AND external_thread_id=?
          AND response_hash=? AND status='sent' LIMIT 1
        """,
        (account_id, environment, thread_id, digest),
    ))
