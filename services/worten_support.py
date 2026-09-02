from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import requests

from services.db import row, rows
from services.support_hub import (
    audit_action,
    clean_text,
    finish_sync,
    json_list,
    latest_successful_sync,
    message_fingerprint,
    start_sync,
    upsert_message,
    upsert_thread,
)


def normalize_api_url(value: str) -> str:
    base = clean_text(value).rstrip("/")
    if not base:
        raise ValueError("URL API Worten mancante.")
    return base if base.lower().endswith("/api") else base + "/api"


class WortenSupportError(RuntimeError):
    def __init__(self, message: str, status_code: int = 0, payload: Any = None):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.payload = payload


@dataclass
class WortenSupportClient:
    api_url: str
    api_key: str
    shop_id: str | int | None = None
    timeout: float = 45.0

    def __post_init__(self) -> None:
        self.api_url = normalize_api_url(self.api_url)
        self.api_key = clean_text(self.api_key)
        self.shop_id = clean_text(self.shop_id)
        if not self.api_key:
            raise ValueError("API Key Worten mancante.")

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "User-Agent": "MarketplaceHub/1.0 (Support)",
        }

    def _params(self, params: Mapping[str, Any] | None = None, include_shop: bool = True) -> dict[str, Any]:
        result = {key: value for key, value in dict(params or {}).items() if value not in (None, "")}
        if include_shop and self.shop_id:
            result["shop_id"] = self.shop_id
        return result

    def request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        files: Any = None, data: Mapping[str, Any] | None = None,
        include_shop: bool = True,
    ) -> Any:
        url = self.api_url + "/" + path.lstrip("/")
        attempts = [include_shop]
        if include_shop and self.shop_id:
            attempts.append(False)
        response = None
        for attempt_index, with_shop in enumerate(attempts):
            response = requests.request(
                method.upper(), url, headers=self.headers,
                params=self._params(params, with_shop), json=json_body,
                files=files, data=data, timeout=max(3.0, float(self.timeout)),
            )
            if response.status_code != 404 or attempt_index == len(attempts) - 1:
                break
        assert response is not None
        if response.status_code >= 400:
            detail = response.text[:3000]
            try:
                payload = response.json()
            except ValueError:
                payload = detail
            raise WortenSupportError(
                f"Worten API {response.status_code} su {path}: {detail}",
                response.status_code, payload,
            )
        if not response.content:
            return {}
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            return response.json()
        return response.content

    def list_threads(
        self, *, updated_since: str = "", page_token: str = "",
        with_messages: bool = False, limit: int = 50,
    ) -> dict:
        params: dict[str, Any]
        if page_token:
            params = {"page_token": page_token}
        else:
            params = {
                "entity_type": "MMP_ORDER",
                "updated_since": updated_since,
                "with_messages": str(bool(with_messages)).lower(),
                "limit": max(1, min(100, int(limit))),
            }
        # Mirakl exposes shop_id on M11 even when seek pagination is used.
        # Keep it on every page so multi-shop accounts remain scoped correctly.
        return self.request(
            "GET", "/inbox/threads", params=params,
            include_shop=True,
        ) or {}

    def get_thread(self, thread_id: str) -> dict:
        return self.request(
            "GET", f"/inbox/threads/{quote(clean_text(thread_id), safe='')}"
        ) or {}

    def reply_thread(
        self, thread_id: str, *, body: str,
        recipients: Iterable[Mapping[str, Any]], topic: Mapping[str, Any],
        attachments: Iterable[tuple[str, bytes, str]] = (),
    ) -> dict:
        message_input = {
            "body": clean_text(body),
            "to": [dict(item) for item in recipients],
            "topic": dict(topic),
        }
        multipart = [
            (
                "message_input",
                (None, json.dumps(message_input, ensure_ascii=False), "application/json"),
            )
        ]
        for filename, content, mime_type in attachments:
            multipart.append(("files", (filename, content, mime_type)))
        return self.request(
            "POST",
            f"/inbox/threads/{quote(clean_text(thread_id), safe='')}/message",
            files=multipart,
        ) or {}

    def download_attachment(self, attachment_id: str) -> bytes:
        value = self.request(
            "GET", f"/inbox/threads/{quote(clean_text(attachment_id), safe='')}/download"
        )
        return bytes(value or b"")


def _participant_type(value: Any) -> str:
    if isinstance(value, dict):
        return clean_text(value.get("type")).upper()
    return ""


def _participant_label(value: Any) -> str:
    if not isinstance(value, dict):
        return clean_text(value)
    return clean_text(
        value.get("display_name") or value.get("name") or value.get("email")
        or value.get("id") or value.get("type")
    )


def _message_sender(message: Mapping[str, Any]) -> tuple[str, str]:
    sender = message.get("from") or message.get("sender") or {}
    return _participant_type(sender), _participant_label(sender)


def _message_body(message: Mapping[str, Any]) -> str:
    return clean_text(message.get("body") or message.get("text") or message.get("content"))


def _message_date(message: Mapping[str, Any]) -> str:
    return clean_text(
        message.get("date_created") or message.get("date")
        or message.get("sent_at") or message.get("created_at")
    )


def _attachments(message: Mapping[str, Any]) -> list[dict]:
    values = message.get("attachments") or message.get("files") or []
    if not isinstance(values, list):
        return []
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        result.append({
            "id": clean_text(item.get("id") or item.get("attachment_id")),
            "name": clean_text(item.get("name") or item.get("filename")),
            "mime_type": clean_text(item.get("mime_type") or item.get("content_type")),
            "size": item.get("size"),
        })
    return result


def thread_order_ids(thread: Mapping[str, Any]) -> list[str]:
    """Extract order identifiers from both current and legacy Mirakl shapes."""
    values: list[Any] = []
    singular = thread.get("entity")
    if isinstance(singular, dict):
        values.append(singular)
    plural = thread.get("entities") or []
    if isinstance(plural, dict):
        values.append(plural)
    elif isinstance(plural, list):
        values.extend(plural)
    result: list[str] = []
    for entity in values:
        if not isinstance(entity, dict):
            continue
        if clean_text(entity.get("type")).upper() != "MMP_ORDER":
            continue
        value = clean_text(entity.get("id") or entity.get("entity_id"))
        if value:
            result.append(value)
    return list(dict.fromkeys(result))


def thread_customer_label(thread: Mapping[str, Any]) -> str:
    organization = thread.get("customer_organization") or {}
    if isinstance(organization, dict):
        label = clean_text(organization.get("display_name"))
        if label:
            return label
    participants = list(thread.get("current_participants") or []) + list(thread.get("authorized_participants") or [])
    for item in participants:
        if _participant_type(item) in {"CUSTOMER", "CUSTOMER_USER", "BUYER"}:
            return _participant_label(item)
    return ""


def thread_recipients(thread: Mapping[str, Any]) -> list[dict]:
    """Return only external participants that actually belong to the thread.

    M12 requires participant identifiers from M10. Current participants are
    preferred; authorized participants are used only as a fallback. This avoids
    sending a customer reply to every theoretically authorized operator.
    """
    current = [item for item in (thread.get("current_participants") or []) if isinstance(item, dict)]
    authorized = [item for item in (thread.get("authorized_participants") or []) if isinstance(item, dict)]
    source = current or authorized

    def external(item: Mapping[str, Any]) -> bool:
        return _participant_type(item) not in {"", "SHOP", "SELLER"}

    ordered: list[Mapping[str, Any]] = []
    last_sender = (thread.get("metadata") or {}).get("last_sender") if isinstance(thread.get("metadata"), dict) else None
    if isinstance(last_sender, dict) and external(last_sender):
        last_type = _participant_type(last_sender)
        last_id = clean_text(last_sender.get("id"))
        matching = next((
            item for item in current + authorized
            if _participant_type(item) == last_type
            and (not last_id or clean_text(item.get("id")) == last_id)
        ), last_sender)
        ordered.append(matching)
    ordered.extend(item for item in source if external(item))

    # If current participants contains only the shop, use authorized participants.
    if not any(external(item) for item in ordered):
        ordered.extend(item for item in authorized if external(item))

    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in ordered:
        participant_type = _participant_type(item)
        if participant_type not in {"CUSTOMER", "CUSTOMER_USER", "BUYER", "OPERATOR"}:
            continue
        participant_id = clean_text(item.get("id"))
        key = (participant_type, participant_id)
        if key in seen:
            continue
        seen.add(key)
        recipient = {"type": participant_type}
        if participant_id:
            recipient["id"] = participant_id
        result.append(recipient)
    return result


def upsert_worten_thread(
    *, seller_id: int, account_id: int, environment: str,
    thread: Mapping[str, Any], sla_hours: int = 24,
) -> tuple[str, int]:
    thread_id = clean_text(thread.get("id") or thread.get("thread_id"))
    if not thread_id:
        raise ValueError("Thread Worten senza ID.")
    messages = [item for item in (thread.get("messages") or []) if isinstance(item, dict)]
    saved_messages = 0
    for message in messages:
        sender_type, sender_label = _message_sender(message)
        message_id = clean_text(message.get("id") or message.get("message_id")) or message_fingerprint(message)
        upsert_message(
            seller_id=seller_id, account_id=account_id, marketplace="worten",
            environment=environment, thread_id=thread_id, message_id=message_id,
            sender_type=sender_type, sender_label=sender_label,
            body=_message_body(message), sent_at=_message_date(message),
            attachments=_attachments(message), raw=message,
        )
        saved_messages += 1
    metadata = thread.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    last_sender = metadata.get("last_sender") or {}
    last_sender_type = _participant_type(last_sender)
    last_sender_label = _participant_label(last_sender)
    last_date = clean_text(metadata.get("last_message_date"))
    if messages:
        ordered = sorted(messages, key=_message_date)
        latest = ordered[-1]
        last_sender_type, last_sender_label = _message_sender(latest)
        last_date = _message_date(latest) or last_date
        preview = _message_body(latest)
    else:
        preview = ""
    reply_since = clean_text(metadata.get("shop_reply_needed_since"))
    no_reply_raw = thread.get("no_store_reply_needed")
    if isinstance(no_reply_raw, (list, tuple, set, dict)):
        no_reply_needed = bool(len(no_reply_raw))
    else:
        no_reply_needed = bool(no_reply_raw)
    topic = thread.get("topic") or {}
    topic_value = clean_text(
        (topic.get("value") or topic.get("label") or topic.get("type"))
        if isinstance(topic, dict) else topic
    )
    state = upsert_thread(
        seller_id=seller_id, account_id=account_id, marketplace="worten",
        environment=environment, thread_id=thread_id,
        order_ids=thread_order_ids(thread), customer_label=thread_customer_label(thread),
        topic=topic_value, api_status="open", reply_needed=bool(reply_since),
        reply_needed_since=reply_since, last_message_at=last_date,
        last_sender_type=last_sender_type, last_sender_label=last_sender_label,
        last_message_preview=preview, message_count=int(metadata.get("total_count") or len(messages)),
        no_reply_needed=no_reply_needed, raw=thread, sla_hours=sla_hours,
    )
    return state, saved_messages


def incremental_updated_since(value: str, overlap_minutes: int = 5) -> str:
    """Return an overlap timestamp to avoid losing boundary updates."""
    from services.support_hub import parse_iso

    parsed = parse_iso(value)
    if not parsed:
        return ""
    return (parsed - timedelta(minutes=max(0, int(overlap_minutes)))).isoformat(timespec="seconds")


def sync_worten_support(
    *, client: WortenSupportClient, seller_id: int, account_id: int,
    environment: str = "live", full: bool = False, sla_hours: int = 24,
) -> dict:
    sync_id = start_sync(
        seller_id=seller_id, account_id=account_id, marketplace="worten",
        environment=environment, mode="full" if full else "incremental",
    )
    errors: list[dict] = []
    previous_ids = {
        item["external_thread_id"]
        for item in rows(
            "SELECT external_thread_id FROM support_threads WHERE marketplace_account_id=? AND environment=?",
            (account_id, environment),
        )
    }
    latest = latest_successful_sync(account_id, environment)
    updated_since = "" if full or not latest else incremental_updated_since(clean_text(latest.get("completed_at")))
    page_token = ""
    seen = 0
    new_count = 0
    updated_count = 0
    messages_saved = 0
    processed: set[str] = set()
    try:
        while True:
            page = client.list_threads(updated_since=updated_since, page_token=page_token)
            data = page.get("data") or []
            if not isinstance(data, list):
                data = []
            for summary in data:
                if not isinstance(summary, dict):
                    continue
                thread_id = clean_text(summary.get("id"))
                if not thread_id or thread_id in processed:
                    continue
                processed.add(thread_id)
                seen += 1
                try:
                    detail = client.get_thread(thread_id)
                    upsert_worten_thread(
                        seller_id=seller_id, account_id=account_id,
                        environment=environment, thread=detail, sla_hours=sla_hours,
                    )
                    messages_saved += len(detail.get("messages") or [])
                    if thread_id in previous_ids:
                        updated_count += 1
                    else:
                        new_count += 1
                except Exception as exc:
                    errors.append({"thread_id": thread_id, "error": str(exc)})
            page_token = clean_text(page.get("next_page_token"))
            if not page_token:
                break
        finish_sync(
            sync_id, status="completed_with_errors" if errors else "completed",
            seen=seen, new=new_count, updated=updated_count,
            messages=messages_saved, errors=errors,
        )
    except Exception as exc:
        errors.append({"phase": "list_threads", "error": str(exc)})
        finish_sync(sync_id, status="failed", seen=seen, new=new_count,
                    updated=updated_count, messages=messages_saved, errors=errors)
        raise
    return {
        "threads_seen": seen,
        "threads_new": new_count,
        "threads_updated": updated_count,
        "messages_saved": messages_saved,
        "errors": errors,
    }


def send_worten_reply(
    *, client: WortenSupportClient, seller_id: int, account_id: int,
    environment: str, thread_id: str, body: str,
    attachments: Iterable[tuple[str, bytes, str]] = (),
) -> dict:
    current = client.get_thread(thread_id)
    recipients = thread_recipients(current)
    if not recipients:
        raise RuntimeError("Worten non ha restituito destinatari autorizzati per il thread.")
    topic = current.get("topic") or {"type": "FREE_TEXT", "value": "Risposta al cliente"}
    request_summary = {
        "body": clean_text(body),
        "recipients": recipients,
        "topic": topic,
        "attachments": [name for name, _, _ in attachments],
    }
    try:
        response = client.reply_thread(
            thread_id, body=body, recipients=recipients,
            topic=topic, attachments=attachments,
        )
        audit_action(
            seller_id=seller_id, account_id=account_id, marketplace="worten",
            environment=environment, thread_id=thread_id,
            action_type="send_message", status="success",
            request_data=request_summary, response_data=response,
        )
        refreshed = client.get_thread(thread_id)
        upsert_worten_thread(
            seller_id=seller_id, account_id=account_id,
            environment=environment, thread=refreshed,
        )
        return response
    except Exception as exc:
        audit_action(
            seller_id=seller_id, account_id=account_id, marketplace="worten",
            environment=environment, thread_id=thread_id,
            action_type="send_message", status="failed",
            request_data=request_summary, error=str(exc),
        )
        raise
