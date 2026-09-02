from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from services.db import row, rows
from services.kaufland import KauflandClient
from services.kaufland_support import (
    response_item,
    send_support_message,
    sync_support,
)
from services.support_hub import (
    audit_action,
    clean_text,
    finish_sync,
    start_sync,
    json_list,
    message_fingerprint,
    upsert_message,
    upsert_thread,
)
from services.marketplace_order_states import verify_order_rows
from services.worten_support import (
    WortenSupportClient,
    send_worten_reply,
    sync_worten_support,
    upsert_worten_thread,
)


def marketplace_environment(marketplace: str, credentials: Mapping[str, Any]) -> str:
    if clean_text(marketplace).lower() == "kaufland":
        return "test" if bool(credentials.get("playground") or credentials.get("test")) else "live"
    return "live"


def create_kaufland_client(credentials: Mapping[str, Any]) -> KauflandClient:
    return KauflandClient(
        clean_text(credentials.get("client_key")),
        clean_text(credentials.get("secret_key")),
        playground=bool(credentials.get("playground") or credentials.get("test")),
    )


def create_worten_client(credentials: Mapping[str, Any]) -> WortenSupportClient:
    return WortenSupportClient(
        api_url=clean_text(credentials.get("api_url") or credentials.get("base_url")),
        api_key=clean_text(credentials.get("api_key")),
        shop_id=credentials.get("shop_id"),
    )


def _kaufland_message_sender(item: Mapping[str, Any]) -> tuple[str, str]:
    sender_type = clean_text(item.get("author_type"))
    sender_label = clean_text(item.get("author_name"))
    return sender_type, sender_label


def mirror_kaufland_support(
    *, seller_id: int, account_id: int, environment: str,
    sla_hours: int = 24,
) -> dict:
    tickets = rows(
        """
        SELECT * FROM kaufland_support_tickets
        WHERE seller_id=? AND marketplace_account_id=? AND environment=?
        ORDER BY ts_updated_iso,id_ticket
        """,
        (seller_id, account_id, environment),
    )
    message_count = 0
    for ticket in tickets:
        ticket_id = clean_text(ticket.get("id_ticket"))
        messages = rows(
            """
            SELECT * FROM kaufland_support_messages
            WHERE marketplace_account_id=? AND environment=? AND id_ticket=?
            ORDER BY ts_created_iso,id
            """,
            (account_id, environment, ticket_id),
        )
        for message in messages:
            message_id = clean_text(message.get("id_ticket_message")) or message_fingerprint(message)
            sender_type, sender_label = _kaufland_message_sender(message)
            upsert_message(
                seller_id=seller_id, account_id=account_id,
                marketplace="kaufland", environment=environment,
                thread_id=ticket_id, message_id=message_id,
                sender_type=sender_type, sender_label=sender_label,
                body=clean_text(message.get("text")),
                sent_at=clean_text(message.get("ts_created_iso")),
                attachments=json_list(message.get("attachments_json")),
                raw=json.loads(message.get("raw_json") or "{}"),
            )
            message_count += 1
        reply_needed = bool(ticket.get("is_seller_responsible")) and clean_text(ticket.get("status")) == "opened"
        last_message_at = clean_text(
            ticket.get("last_message_at") or ticket.get("ts_updated_iso")
            or ticket.get("ts_created_iso")
        )
        raw = json.loads(ticket.get("raw_json") or "{}")
        upsert_thread(
            seller_id=seller_id, account_id=account_id,
            marketplace="kaufland", environment=environment,
            thread_id=ticket_id,
            order_ids=json_list(ticket.get("order_ids_json")),
            customer_label=clean_text(ticket.get("buyer_label")),
            topic=clean_text(ticket.get("topic") or ticket.get("open_reason")),
            api_status=clean_text(ticket.get("status")),
            reply_needed=reply_needed,
            reply_needed_since=last_message_at if reply_needed else "",
            last_message_at=last_message_at,
            last_sender_type=clean_text(ticket.get("last_message_author")),
            last_sender_label=clean_text(ticket.get("last_message_author")),
            last_message_preview=clean_text(ticket.get("last_message_preview")),
            message_count=int(ticket.get("message_count") or len(messages)),
            raw=raw, sla_hours=sla_hours,
        )
    return {"threads": len(tickets), "messages": message_count}


def sync_account_support(
    *, account: Mapping[str, Any], credentials: Mapping[str, Any],
    full: bool = False, sla_hours: int = 24,
) -> dict:
    marketplace = clean_text(account.get("marketplace")).lower()
    seller_id = int(account["seller_id"])
    account_id = int(account["id"])
    environment = marketplace_environment(marketplace, credentials)
    if marketplace == "kaufland":
        sync_id = start_sync(
            seller_id=seller_id, account_id=account_id,
            marketplace="kaufland", environment=environment,
            mode="full" if full else "incremental",
        )
        client = create_kaufland_client(credentials)
        try:
            result = sync_support(
                client, seller_id, account_id, environment,
                maximum_tickets=None,
            )
            mirrored = mirror_kaufland_support(
                seller_id=seller_id, account_id=account_id,
                environment=environment, sla_hours=sla_hours,
            )
            errors = result.get("errors", [])
            finish_sync(
                sync_id,
                status="completed_with_errors" if errors else "completed",
                seen=result.get("tickets_seen", 0),
                new=0,
                updated=result.get("tickets_saved", 0),
                messages=mirrored["messages"],
                errors=errors,
            )
            return {
                "marketplace": marketplace,
                "environment": environment,
                "threads_seen": result.get("tickets_seen", 0),
                "threads_new": 0,
                "threads_updated": result.get("tickets_saved", 0),
                "messages_saved": mirrored["messages"],
                "errors": errors,
            }
        except Exception as exc:
            finish_sync(sync_id, status="failed", errors=[{"error": str(exc)}])
            raise
    if marketplace == "worten":
        return {
            "marketplace": marketplace,
            "environment": environment,
            **sync_worten_support(
                client=create_worten_client(credentials),
                seller_id=seller_id, account_id=account_id,
                environment=environment, full=full, sla_hours=sla_hours,
            ),
        }
    raise RuntimeError(f"Il connettore ticket per {marketplace.title()} non è ancora disponibile.")


def refresh_thread(
    *, account: Mapping[str, Any], credentials: Mapping[str, Any],
    thread_id: str, sla_hours: int = 24,
) -> dict:
    marketplace = clean_text(account.get("marketplace")).lower()
    seller_id = int(account["seller_id"])
    account_id = int(account["id"])
    environment = marketplace_environment(marketplace, credentials)
    if marketplace == "kaufland":
        client = create_kaufland_client(credentials)
        detail = response_item(client.ticket(thread_id))
        # The existing synchronizer is intentionally reused so message pagination,
        # order-unit context and local audit behavior remain consistent.
        from services.kaufland_support import sync_one_ticket
        sync_one_ticket(client, seller_id, account_id, environment, thread_id)
        mirror_kaufland_support(
            seller_id=seller_id, account_id=account_id,
            environment=environment, sla_hours=sla_hours,
        )
        return detail
    if marketplace == "worten":
        client = create_worten_client(credentials)
        detail = client.get_thread(thread_id)
        upsert_worten_thread(
            seller_id=seller_id, account_id=account_id,
            environment=environment, thread=detail, sla_hours=sla_hours,
        )
        return detail
    raise RuntimeError(f"Connettore ticket non disponibile per {marketplace}.")



def refresh_order_context_states(
    *,
    account: Mapping[str, Any],
    credentials: Mapping[str, Any],
    context_rows: Iterable[Mapping[str, Any]],
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Read the current marketplace state for orders linked to a ticket.

    This gives both manual operators and the AI the same live information used
    by order creation and shipment confirmation. Unknown or rate-limited rows
    remain explicitly unverified instead of being guessed.
    """
    marketplace = clean_text(account.get("marketplace")).lower()
    values = [dict(item) for item in context_rows]
    if not values:
        return [], [], []
    return verify_order_rows(
        marketplace=marketplace,
        credentials=credentials,
        rows=values,
        force_refresh=force_refresh,
    )

def send_thread_reply(
    *, account: Mapping[str, Any], credentials: Mapping[str, Any],
    thread_id: str, body: str, attachments: Iterable[tuple[str, bytes, str]] = (),
    interim_notice: bool = False,
) -> dict:
    marketplace = clean_text(account.get("marketplace")).lower()
    seller_id = int(account["seller_id"])
    account_id = int(account["id"])
    environment = marketplace_environment(marketplace, credentials)
    if marketplace == "kaufland":
        encoded = []
        if attachments:
            from services.kaufland_support import encode_ticket_attachment
            for filename, content, mime_type in attachments:
                encoded.append(encode_ticket_attachment(filename, mime_type, content))
        return send_support_message(
            create_kaufland_client(credentials), seller_id, account_id,
            environment, thread_id, body, interim_notice, encoded,
        )
    if marketplace == "worten":
        return send_worten_reply(
            client=create_worten_client(credentials),
            seller_id=seller_id, account_id=account_id,
            environment=environment, thread_id=thread_id,
            body=body, attachments=attachments,
        )
    raise RuntimeError(f"Invio messaggi non disponibile per {marketplace}.")


def close_thread(
    *, account: Mapping[str, Any], credentials: Mapping[str, Any],
    thread_id: str, sla_hours: int = 24,
) -> dict:
    """Close a Kaufland ticket and mirror the final state locally.

    Mirakl/Worten threads do not expose a universal seller-side close endpoint,
    so this action is intentionally available only for Kaufland.
    """
    marketplace = clean_text(account.get("marketplace")).lower()
    seller_id = int(account["seller_id"])
    account_id = int(account["id"])
    environment = marketplace_environment(marketplace, credentials)
    if marketplace != "kaufland":
        raise RuntimeError("La chiusura API esplicita è disponibile soltanto per i ticket Kaufland.")
    client = create_kaufland_client(credentials)
    try:
        response = client.close_ticket(thread_id) or {}
        audit_action(
            seller_id=seller_id, account_id=account_id, marketplace="kaufland",
            environment=environment, thread_id=thread_id,
            action_type="close_ticket", status="success",
            request_data={"id_ticket": clean_text(thread_id)},
            response_data=response if isinstance(response, dict) else {},
        )
        refresh_thread(
            account=account, credentials=credentials, thread_id=thread_id,
            sla_hours=sla_hours,
        )
        return response if isinstance(response, dict) else {}
    except Exception as exc:
        audit_action(
            seller_id=seller_id, account_id=account_id, marketplace="kaufland",
            environment=environment, thread_id=thread_id,
            action_type="close_ticket", status="failed",
            request_data={"id_ticket": clean_text(thread_id)}, error=str(exc),
        )
        raise


def thread_still_needs_reply(
    *, account: Mapping[str, Any], credentials: Mapping[str, Any],
    thread_id: str, expected_updated_at: str = "",
) -> tuple[bool, str]:
    marketplace = clean_text(account.get("marketplace")).lower()
    if marketplace == "kaufland":
        detail = response_item(create_kaufland_client(credentials).ticket(thread_id))
        if clean_text(detail.get("status")) != "opened":
            return False, "Il ticket non è più aperto."
        if not bool(detail.get("is_seller_responsible")):
            return False, "Kaufland non richiede più una risposta del Seller."
        current_updated = clean_text(detail.get("ts_updated_iso"))
        if expected_updated_at and current_updated and current_updated != expected_updated_at:
            return False, "Il ticket è cambiato dopo la generazione della bozza IA."
        return True, ""
    if marketplace == "worten":
        detail = create_worten_client(credentials).get_thread(thread_id)
        metadata = detail.get("metadata") or {}
        if not clean_text(metadata.get("shop_reply_needed_since")):
            return False, "Worten non richiede più una risposta del Seller."
        current_updated = clean_text(detail.get("date_updated"))
        if expected_updated_at and current_updated and current_updated != expected_updated_at:
            return False, "Il thread è cambiato dopo la generazione della bozza IA."
        return True, ""
    return False, "Marketplace non supportato."
