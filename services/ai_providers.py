from __future__ import annotations

import ast
import html
import json
import os
import re
import time
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

import requests

from services.db import connect, execute, json_text, now_iso, row, rows
from services.security import decrypt_dict, encrypt_dict


_AI_SCHEMA_LOCK = threading.RLock()
_AI_SCHEMA_READY: set[tuple[str, str]] = set()


PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "kind": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5-mini",
        "secret_fields": ["api_key"],
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-5",
        "secret_fields": ["api_key"],
    },
    "gemini": {
        "label": "Google Gemini",
        "kind": "gemini",
        "base_url": "https://generativelanguage.googleapis.com",
        "default_model": "gemini-2.5-flash",
        "secret_fields": ["api_key"],
    },
    "mistral": {
        "label": "Mistral AI",
        "kind": "openai_compatible",
        "base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-small-latest",
        "secret_fields": ["api_key"],
    },
    "xai": {
        "label": "xAI Grok",
        "kind": "openai_compatible",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-3-mini",
        "secret_fields": ["api_key"],
    },
    "deepseek": {
        "label": "DeepSeek",
        "kind": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "secret_fields": ["api_key"],
    },
    "groq": {
        "label": "Groq",
        "kind": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "secret_fields": ["api_key"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "kind": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4.1-mini",
        "secret_fields": ["api_key"],
    },
    "azure_openai": {
        "label": "Azure OpenAI / Microsoft Foundry",
        "kind": "azure_openai",
        "base_url": "",
        "default_model": "gpt-4o-mini",
        "secret_fields": ["api_key"],
    },
    "bedrock": {
        "label": "Amazon Bedrock",
        "kind": "bedrock",
        "base_url": "",
        "default_model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "secret_fields": ["aws_access_key_id", "aws_secret_access_key", "aws_session_token"],
    },
    "ollama": {
        "label": "Ollama / modello locale",
        "kind": "ollama",
        "base_url": "http://localhost:11434",
        "default_model": "llama3.2",
        "secret_fields": ["api_key"],
    },
    "custom_openai": {
        "label": "Endpoint personalizzato compatibile OpenAI",
        "kind": "openai_compatible",
        "base_url": "",
        "default_model": "",
        "secret_fields": ["api_key"],
    },
}


def clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def ensure_schema(*, force: bool = False) -> None:
    # Streamlit reruns frequently. Avoid repeating DDL while long catalog writes
    # are active, but keep the key tied to the actual database target for tests
    # and optional PostgreSQL installations.
    from services import db as db_service

    if db_service.database_engine() == "postgresql":
        public = db_service.database_config_public()
        key = ("postgresql", f"{public.get('postgresql_host')}:{public.get('postgresql_port')}/{public.get('postgresql_database')}")
    else:
        key = ("sqlite", str(db_service.Path(db_service.DB_PATH).resolve()))
    if not force and key in _AI_SCHEMA_READY:
        return
    with _AI_SCHEMA_LOCK:
        if not force and key in _AI_SCHEMA_READY:
            return
        with connect() as con:
            con.executescript(
                """
            CREATE TABLE IF NOT EXISTS ai_provider_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                temperature REAL NOT NULL DEFAULT 0.2,
                max_tokens INTEGER NOT NULL DEFAULT 1200,
                timeout_seconds INTEGER NOT NULL DEFAULT 60,
                retries INTEGER NOT NULL DEFAULT 2,
                daily_request_limit INTEGER NOT NULL DEFAULT 0,
                monthly_request_limit INTEGER NOT NULL DEFAULT 0,
                config_json TEXT NOT NULL DEFAULT '{}',
                secrets_encrypted TEXT NOT NULL DEFAULT '',
                last_test_status TEXT NOT NULL DEFAULT '',
                last_test_message TEXT NOT NULL DEFAULT '',
                last_test_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(seller_id,name)
            );
            CREATE INDEX IF NOT EXISTS idx_ai_provider_profiles_seller
            ON ai_provider_profiles(seller_id,enabled,provider,name);

            CREATE TABLE IF NOT EXISTS ai_provider_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_id INTEGER NOT NULL REFERENCES sellers(id) ON DELETE CASCADE,
                marketplace_account_id INTEGER,
                profile_id INTEGER REFERENCES ai_provider_profiles(id) ON DELETE SET NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_provider_usage_scope
            ON ai_provider_usage(seller_id,profile_id,created_at,status);
                """
            )
        _AI_SCHEMA_READY.add(key)


def _reset_schema_cache_for_tests() -> None:
    with _AI_SCHEMA_LOCK:
        _AI_SCHEMA_READY.clear()


def provider_options() -> list[tuple[str, str]]:
    return [(key, value["label"]) for key, value in PROVIDER_CATALOG.items()]


def provider_defaults(provider: str) -> dict[str, Any]:
    return dict(PROVIDER_CATALOG.get(clean_text(provider), PROVIDER_CATALOG["custom_openai"]))


def list_profiles(seller_id: int, *, enabled_only: bool = False) -> list[dict]:
    ensure_schema()
    query = "SELECT * FROM ai_provider_profiles WHERE seller_id=?"
    params: list[Any] = [seller_id]
    if enabled_only:
        query += " AND enabled=1"
    query += " ORDER BY enabled DESC,LOWER(name),id"
    return rows(query, params)


def get_profile(profile_id: int, seller_id: int | None = None) -> dict:
    ensure_schema()
    if seller_id is None:
        return row("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)) or {}
    return row(
        "SELECT * FROM ai_provider_profiles WHERE id=? AND seller_id=?",
        (profile_id, seller_id),
    ) or {}


def profile_secrets(profile: Mapping[str, Any]) -> dict[str, str]:
    encrypted = clean_text(profile.get("secrets_encrypted"))
    if not encrypted:
        return {}
    try:
        return {str(k): clean_text(v) for k, v in decrypt_dict(encrypted).items()}
    except Exception:
        return {}


def profile_config(profile: Mapping[str, Any]) -> dict[str, Any]:
    value = profile.get("config_json")
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def save_profile(
    *, seller_id: int, name: str, provider: str, model: str = "",
    base_url: str = "", enabled: bool = True, temperature: float = 0.2,
    max_tokens: int = 1200, timeout_seconds: int = 60, retries: int = 2,
    daily_request_limit: int = 0, monthly_request_limit: int = 0,
    config: Mapping[str, Any] | None = None, secrets: Mapping[str, Any] | None = None,
    profile_id: int | None = None,
) -> int:
    ensure_schema()
    provider = clean_text(provider) or "custom_openai"
    defaults = provider_defaults(provider)
    name = clean_text(name) or defaults["label"]
    model = clean_text(model) or defaults.get("default_model", "")
    base_url = clean_text(base_url) or defaults.get("base_url", "")
    old = get_profile(int(profile_id), seller_id) if profile_id else {}
    encrypted = clean_text(old.get("secrets_encrypted"))
    cleaned_secrets = {
        clean_text(key): clean_text(value)
        for key, value in dict(secrets or {}).items()
        if clean_text(key) and clean_text(value)
    }
    if cleaned_secrets:
        merged = profile_secrets(old)
        merged.update(cleaned_secrets)
        encrypted = encrypt_dict(merged)
    values = (
        seller_id, name, provider, model, base_url, int(bool(enabled)),
        max(0.0, min(2.0, float(temperature))), max(64, int(max_tokens)),
        max(5, int(timeout_seconds)), max(0, min(5, int(retries))),
        max(0, int(daily_request_limit)), max(0, int(monthly_request_limit)),
        json_text(dict(config or {})), encrypted, now_iso(), now_iso(),
    )
    if profile_id:
        execute(
            """
            UPDATE ai_provider_profiles SET
                name=?,provider=?,model=?,base_url=?,enabled=?,temperature=?,
                max_tokens=?,timeout_seconds=?,retries=?,daily_request_limit=?,
                monthly_request_limit=?,config_json=?,secrets_encrypted=?,updated_at=?
            WHERE id=? AND seller_id=?
            """,
            (
                name, provider, model, base_url, int(bool(enabled)),
                max(0.0, min(2.0, float(temperature))), max(64, int(max_tokens)),
                max(5, int(timeout_seconds)), max(0, min(5, int(retries))),
                max(0, int(daily_request_limit)), max(0, int(monthly_request_limit)),
                json_text(dict(config or {})), encrypted, now_iso(), int(profile_id), seller_id,
            ),
        )
        return int(profile_id)
    return execute(
        """
        INSERT INTO ai_provider_profiles(
            seller_id,name,provider,model,base_url,enabled,temperature,max_tokens,
            timeout_seconds,retries,daily_request_limit,monthly_request_limit,
            config_json,secrets_encrypted,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        values,
    )


def delete_profile(profile_id: int, seller_id: int) -> int:
    return execute("DELETE FROM ai_provider_profiles WHERE id=? AND seller_id=?", (profile_id, seller_id))


def _usage_count(profile_id: int, period: str) -> int:
    modifier = "start of day" if period == "day" else "start of month"
    value = row(
        """
        SELECT COUNT(*) AS total FROM ai_provider_usage
        WHERE profile_id=? AND status='success'
          AND datetime(created_at) >= datetime('now', ?)
        """,
        (profile_id, modifier),
    ) or {}
    return int(value.get("total") or 0)


def _check_limits(profile: Mapping[str, Any]) -> None:
    profile_id = int(profile.get("id") or 0)
    daily = int(profile.get("daily_request_limit") or 0)
    monthly = int(profile.get("monthly_request_limit") or 0)
    if profile_id and daily and _usage_count(profile_id, "day") >= daily:
        raise RuntimeError("Limite giornaliero del provider IA raggiunto.")
    if profile_id and monthly and _usage_count(profile_id, "month") >= monthly:
        raise RuntimeError("Limite mensile del provider IA raggiunto.")


def _record_usage(
    profile: Mapping[str, Any], *, purpose: str, status: str, latency_ms: int,
    account_id: int | None = None, input_tokens: int = 0, output_tokens: int = 0,
    error: str = "",
) -> None:
    execute(
        """
        INSERT INTO ai_provider_usage(
            seller_id,marketplace_account_id,profile_id,provider,model,purpose,status,
            input_tokens,output_tokens,latency_ms,error,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(profile.get("seller_id") or 0), account_id,
            int(profile.get("id") or 0) or None, clean_text(profile.get("provider")),
            clean_text(profile.get("model")), clean_text(purpose), status,
            int(input_tokens or 0), int(output_tokens or 0), int(latency_ms or 0),
            clean_text(error), now_iso(),
        ),
    )


def _strip_markdown_fence(value: str) -> str:
    text = str(value or "").strip().lstrip("\ufeff")
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().lower() in {"```", "```json", "```javascript", "```js"}:
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _iter_text_candidates(value: Any) -> list[str]:
    """Extract human/model text from heterogeneous REST response fields."""
    result: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, bytes):
            try:
                visit(item.decode("utf-8", errors="replace"))
            except Exception:
                return
            return
        if isinstance(item, str):
            text = item.strip()
            if text and text not in result:
                result.append(text)
            return
        if isinstance(item, Mapping):
            # Prefer well-known text-bearing keys, but still inspect nested values.
            for key in ("text", "content", "output_text", "value", "arguments", "body"):
                if key in item:
                    visit(item.get(key))
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return result


def _json_objects_from_value(value: Any) -> list[dict]:
    """Return JSON objects found in mappings, strings, arrays or SDK-style blocks."""
    objects: list[dict] = []

    def add(item: Any, depth: int = 0) -> None:
        if depth > 6 or item is None:
            return
        if isinstance(item, Mapping):
            mapping = dict(item)
            # API/SDK wrapper objects (content blocks, messages, choices) are not
            # themselves the requested JSON result. Inspect their payload first.
            wrapper_type = clean_text(mapping.get("type")).lower()
            wrapper_keys = {
                "type", "text", "content", "parsed", "output_parsed", "value",
                "arguments", "body", "message", "choices", "output", "tool_calls",
                "annotations", "role", "id", "status", "index", "finish_reason",
            }
            is_wrapper = bool(
                wrapper_type in {"output_text", "text", "message", "refusal"}
                or set(mapping).issubset(wrapper_keys)
            )
            if is_wrapper:
                for key in (
                    "parsed", "output_parsed", "text", "content", "value",
                    "arguments", "body", "message", "choices", "output", "tool_calls",
                ):
                    if key in mapping:
                        add(mapping.get(key), depth + 1)
                        if objects:
                            return
                return
            objects.append(mapping)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                add(child, depth + 1)
            return
        if isinstance(item, bytes):
            add(item.decode("utf-8", errors="replace"), depth + 1)
            return
        if not isinstance(item, str):
            return

        text = html.unescape(_strip_markdown_fence(item)).strip()
        if not text:
            return

        # Exact JSON first. It may itself contain a JSON-encoded string.
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if parsed is not None:
            add(parsed, depth + 1)
            if objects:
                return

        # Some gateways return Python-like dict reprs with single quotes.
        try:
            literal = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            literal = None
        if literal is not None:
            add(literal, depth + 1)
            if objects:
                return

        decoder = json.JSONDecoder()
        # Parse every balanced JSON object/array embedded after explanatory prose.
        for pos, char in enumerate(text):
            if char not in "{[":
                continue
            try:
                parsed, _end = decoder.raw_decode(text[pos:])
            except (json.JSONDecodeError, TypeError):
                continue
            add(parsed, depth + 1)
            if objects:
                return

        # Last conservative repair: remove trailing commas before } or ].
        repaired = re.sub(r",\s*([}\]])", r"\1", text)
        if repaired != text:
            try:
                add(json.loads(repaired), depth + 1)
            except (json.JSONDecodeError, TypeError):
                pass

    add(value)
    return objects


def _json_from_text(text: str) -> dict:
    objects = _json_objects_from_value(text)
    if objects:
        return objects[0]
    if not clean_text(text):
        raise RuntimeError("Il provider IA non ha restituito testo JSON utilizzabile.")
    raise RuntimeError("Il provider IA non ha restituito JSON valido.")


def provider_result_text(result: "ProviderResult") -> str:
    """Return the best readable text from Responses, Chat Completions or compatible APIs."""
    raw = result.raw if isinstance(result.raw, Mapping) else {}
    candidates: list[str] = []

    def extend(value: Any) -> None:
        for text in _iter_text_candidates(value):
            if text not in candidates:
                candidates.append(text)

    extend(raw.get("output_text"))
    for item in raw.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, Mapping):
                continue
            if clean_text(content.get("type")) == "refusal":
                extend(content.get("refusal"))
            else:
                extend(content)
    for choice in raw.get("choices") or []:
        if isinstance(choice, Mapping):
            extend((choice.get("message") or {}).get("content"))
    extend(result.text)
    return next((item for item in candidates if clean_text(item)), "")


def _json_from_provider_result(result: "ProviderResult") -> dict:
    """Read structured JSON from all known OpenAI and compatible response shapes."""
    raw = result.raw if isinstance(result.raw, Mapping) else {}
    direct_values: list[Any] = []

    for key in ("parsed", "output_parsed"):
        if key in raw:
            direct_values.append(raw.get(key))
    direct_values.append(raw.get("output_text"))

    for item in raw.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        direct_values.extend([item.get("parsed"), item.get("content")])

    # Chat Completions SDK/REST shapes, including content arrays and tool arguments.
    for choice in raw.get("choices") or []:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message") or {}
        if isinstance(message, Mapping):
            direct_values.extend([message.get("parsed"), message.get("content")])
            for tool_call in message.get("tool_calls") or []:
                if isinstance(tool_call, Mapping):
                    direct_values.append((tool_call.get("function") or {}).get("arguments"))
        direct_values.append(choice.get("text"))

    direct_values.append(result.text)
    errors: list[str] = []
    for candidate in direct_values:
        if candidate is None:
            continue
        objects = _json_objects_from_value(candidate)
        if objects:
            return objects[0]
        for text in _iter_text_candidates(candidate):
            try:
                return _json_from_text(text)
            except Exception as exc:
                errors.append(str(exc))

    status = clean_text(raw.get("status"))
    incomplete = raw.get("incomplete_details") or {}
    reason = clean_text(incomplete.get("reason") if isinstance(incomplete, Mapping) else "")
    suffix = ""
    if status:
        suffix += f" Stato risposta: {status}."
    if reason:
        suffix += f" Motivo: {reason}."
    raise RuntimeError(
        "Il provider IA non ha restituito JSON valido." + suffix
        + ((" " + " | ".join(dict.fromkeys(errors))) if errors else "")
    )


@dataclass
class ProviderResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] | None = None


def _post_json(url: str, *, headers: Mapping[str, str], payload: Mapping[str, Any], timeout: int) -> dict:
    response = requests.post(url, headers=dict(headers), json=dict(payload), timeout=timeout)
    if response.status_code >= 400:
        detail = response.text[:1000]
        raise RuntimeError(f"API IA {response.status_code}: {detail}")
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError("Il provider IA ha restituito una risposta non JSON.") from exc
    return value if isinstance(value, dict) else {"data": value}



def _openai_native(
    profile: Mapping[str, Any], system: str, prompt: str,
    *, json_schema: Mapping[str, Any] | None = None,
) -> ProviderResult:
    """Use the OpenAI Responses API for native OpenAI profiles.

    Newer OpenAI models do not consistently accept the legacy Chat Completions
    parameter ``max_tokens``. The Responses API uses ``max_output_tokens`` and
    avoids model-specific parameter mismatches.
    """
    api_key = clean_text(profile_secrets(profile).get("api_key"))
    if not api_key:
        raise RuntimeError("API key OpenAI non configurata.")
    base = clean_text(profile.get("base_url")).rstrip("/") or "https://api.openai.com/v1"
    model_name = clean_text(profile.get("model"))
    payload: dict[str, Any] = {
        "model": model_name,
        "instructions": system,
        "input": prompt,
        "max_output_tokens": max(512, int(profile.get("max_tokens") or 1200)),
        "store": False,
    }
    # Reasoning models can otherwise spend the whole output budget internally and
    # leave no customer-facing JSON. Ticket classification does not need deep
    # reasoning, so use the smallest supported effort.
    if model_name.lower().startswith(("gpt-5", "o1", "o3", "o4")):
        payload["reasoning"] = {"effort": "minimal"}
    if json_schema:
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": clean_text(json_schema.get("name")) or "marketplace_hub_output",
                "description": clean_text(json_schema.get("description")),
                "schema": dict(json_schema.get("schema") or {}),
                "strict": bool(json_schema.get("strict", True)),
            }
        }
    request_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    value = _post_json(
        base + "/responses",
        headers=request_headers,
        payload=payload,
        timeout=int(profile.get("timeout_seconds") or 60),
    )
    # Retry once when the model consumed the output allowance before emitting the
    # structured answer. This is common with reasoning models and small limits.
    incomplete = value.get("incomplete_details") or {}
    if (
        clean_text(value.get("status")) == "incomplete"
        and isinstance(incomplete, Mapping)
        and clean_text(incomplete.get("reason")) == "max_output_tokens"
    ):
        retry_payload = dict(payload)
        retry_payload["max_output_tokens"] = min(
            8000, max(2400, int(payload["max_output_tokens"]) * 2)
        )
        value = _post_json(
            base + "/responses",
            headers=request_headers,
            payload=retry_payload,
            timeout=int(profile.get("timeout_seconds") or 60),
        )
    text_parts: list[str] = []
    direct = clean_text(value.get("output_text"))
    if direct:
        text_parts.append(direct)
    for item in value.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, Mapping):
                continue
            if clean_text(content.get("type")) in {"output_text", "text"}:
                candidate = clean_text(content.get("text"))
                if candidate and candidate not in text_parts:
                    text_parts.append(candidate)
    # Keep a single canonical text. Structured candidates remain available in raw.
    canonical_text = next((part for part in text_parts if part), "")
    usage = value.get("usage") or {}
    return ProviderResult(
        text=canonical_text,
        provider="openai",
        model=clean_text(profile.get("model")),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        raw=value,
    )

def _openai_native_json_fallback(
    profile: Mapping[str, Any], system: str, prompt: str,
    json_schema: Mapping[str, Any],
) -> ProviderResult:
    """Fallback to Chat Completions Structured Outputs for native OpenAI."""
    api_key = clean_text(profile_secrets(profile).get("api_key"))
    base = clean_text(profile.get("base_url")).rstrip("/") or "https://api.openai.com/v1"
    schema_name = clean_text(json_schema.get("name")) or "marketplace_hub_output"
    payload: dict[str, Any] = {
        "model": clean_text(profile.get("model")),
        "messages": [
            {"role": "developer", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": int(profile.get("max_tokens") or 1200),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "description": clean_text(json_schema.get("description")),
                "schema": dict(json_schema.get("schema") or {}),
                "strict": bool(json_schema.get("strict", True)),
            },
        },
    }
    value = _post_json(
        base + "/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        payload=payload,
        timeout=int(profile.get("timeout_seconds") or 60),
    )
    choice = (value.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = clean_text(message.get("content"))
    usage = value.get("usage") or {}
    return ProviderResult(
        text=text,
        provider="openai",
        model=clean_text(profile.get("model")),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        raw=value,
    )


def _openai_compatible(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    secrets = profile_secrets(profile)
    api_key = clean_text(secrets.get("api_key"))
    provider = clean_text(profile.get("provider"))
    if not api_key and provider != "ollama":
        raise RuntimeError("API key non configurata per il provider IA.")
    base = clean_text(profile.get("base_url")).rstrip("/")
    if not base:
        raise RuntimeError("Base URL del provider IA non configurato.")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    config = profile_config(profile)
    extra_headers = config.get("headers")
    if isinstance(extra_headers, Mapping):
        headers.update({clean_text(k): clean_text(v) for k, v in extra_headers.items() if clean_text(k)})
    if provider == "openrouter":
        if clean_text(config.get("site_url")):
            headers["HTTP-Referer"] = clean_text(config.get("site_url"))
        if clean_text(config.get("app_name")):
            headers["X-Title"] = clean_text(config.get("app_name"))
    payload = {
        "model": clean_text(profile.get("model")),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": float(profile.get("temperature") or 0.2),
        "max_tokens": int(profile.get("max_tokens") or 1200),
    }
    try:
        value = _post_json(
            base + "/chat/completions", headers=headers, payload=payload,
            timeout=int(profile.get("timeout_seconds") or 60),
        )
    except RuntimeError as exc:
        message = str(exc).lower()
        if "max_tokens" not in message or "max_completion_tokens" not in message:
            raise
        payload = dict(payload)
        payload["max_completion_tokens"] = payload.pop("max_tokens")
        value = _post_json(
            base + "/chat/completions", headers=headers, payload=payload,
            timeout=int(profile.get("timeout_seconds") or 60),
        )
    choices = value.get("choices") or []
    text = clean_text(((choices[0] if choices else {}).get("message") or {}).get("content"))
    usage = value.get("usage") or {}
    return ProviderResult(
        text=text, provider=provider, model=clean_text(profile.get("model")),
        input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        raw=value,
    )


def _anthropic(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    api_key = clean_text(profile_secrets(profile).get("api_key"))
    if not api_key:
        raise RuntimeError("API key Anthropic non configurata.")
    value = _post_json(
        clean_text(profile.get("base_url")).rstrip("/") + "/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": clean_text(profile_config(profile).get("anthropic_version")) or "2023-06-01",
        },
        payload={
            "model": clean_text(profile.get("model")),
            "max_tokens": int(profile.get("max_tokens") or 1200),
            "temperature": float(profile.get("temperature") or 0.2),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=int(profile.get("timeout_seconds") or 60),
    )
    content = value.get("content") or []
    text = "\n".join(clean_text(item.get("text")) for item in content if isinstance(item, Mapping))
    usage = value.get("usage") or {}
    return ProviderResult(
        text=text, provider="anthropic", model=clean_text(profile.get("model")),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0), raw=value,
    )


def _gemini(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    api_key = clean_text(profile_secrets(profile).get("api_key"))
    if not api_key:
        raise RuntimeError("API key Gemini non configurata.")
    base = clean_text(profile.get("base_url")).rstrip("/")
    model = clean_text(profile.get("model"))
    url = f"{base}/v1beta/models/{model}:generateContent?{urlencode({'key': api_key})}"
    value = _post_json(
        url,
        headers={"Content-Type": "application/json"},
        payload={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": float(profile.get("temperature") or 0.2),
                "maxOutputTokens": int(profile.get("max_tokens") or 1200),
                "responseMimeType": "application/json",
            },
        },
        timeout=int(profile.get("timeout_seconds") or 60),
    )
    candidates = value.get("candidates") or []
    parts = (((candidates[0] if candidates else {}).get("content") or {}).get("parts") or [])
    text = "\n".join(clean_text(item.get("text")) for item in parts if isinstance(item, Mapping))
    usage = value.get("usageMetadata") or {}
    return ProviderResult(
        text=text, provider="gemini", model=model,
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0), raw=value,
    )


def _azure(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    secrets = profile_secrets(profile)
    api_key = clean_text(secrets.get("api_key"))
    if not api_key:
        raise RuntimeError("API key Azure OpenAI non configurata.")
    base = clean_text(profile.get("base_url")).rstrip("/")
    if not base:
        raise RuntimeError("Endpoint Azure OpenAI non configurato.")
    config = profile_config(profile)
    api_version = clean_text(config.get("api_version")) or "2024-10-21"
    deployment = clean_text(config.get("deployment")) or clean_text(profile.get("model"))
    if "/openai/v1" in base:
        url = base + "/chat/completions"
        payload_model = deployment
    else:
        url = f"{base}/openai/deployments/{deployment}/chat/completions?{urlencode({'api-version': api_version})}"
        payload_model = deployment
    value = _post_json(
        url,
        headers={"Content-Type": "application/json", "api-key": api_key},
        payload={
            "model": payload_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": float(profile.get("temperature") or 0.2),
            "max_tokens": int(profile.get("max_tokens") or 1200),
        },
        timeout=int(profile.get("timeout_seconds") or 60),
    )
    choices = value.get("choices") or []
    text = clean_text(((choices[0] if choices else {}).get("message") or {}).get("content"))
    usage = value.get("usage") or {}
    return ProviderResult(
        text=text, provider="azure_openai", model=deployment,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0), raw=value,
    )


def _bedrock(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("Installa boto3 per usare Amazon Bedrock.") from exc
    secrets = profile_secrets(profile)
    config = profile_config(profile)
    kwargs: dict[str, Any] = {
        "region_name": clean_text(config.get("region")) or "eu-west-1",
    }
    if clean_text(secrets.get("aws_access_key_id")):
        kwargs["aws_access_key_id"] = clean_text(secrets.get("aws_access_key_id"))
        kwargs["aws_secret_access_key"] = clean_text(secrets.get("aws_secret_access_key"))
        if clean_text(secrets.get("aws_session_token")):
            kwargs["aws_session_token"] = clean_text(secrets.get("aws_session_token"))
    client = boto3.client("bedrock-runtime", **kwargs)
    value = client.converse(
        modelId=clean_text(profile.get("model")),
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={
            "temperature": float(profile.get("temperature") or 0.2),
            "maxTokens": int(profile.get("max_tokens") or 1200),
        },
    )
    blocks = (((value.get("output") or {}).get("message") or {}).get("content") or [])
    text = "\n".join(clean_text(item.get("text")) for item in blocks if isinstance(item, Mapping))
    usage = value.get("usage") or {}
    return ProviderResult(
        text=text, provider="bedrock", model=clean_text(profile.get("model")),
        input_tokens=int(usage.get("inputTokens") or 0),
        output_tokens=int(usage.get("outputTokens") or 0), raw=value,
    )


def _ollama(profile: Mapping[str, Any], system: str, prompt: str) -> ProviderResult:
    base = clean_text(profile.get("base_url")).rstrip("/") or "http://localhost:11434"
    headers = {"Content-Type": "application/json"}
    api_key = clean_text(profile_secrets(profile).get("api_key"))
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    value = _post_json(
        base + "/api/chat", headers=headers,
        payload={
            "model": clean_text(profile.get("model")),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": float(profile.get("temperature") or 0.2)},
        },
        timeout=int(profile.get("timeout_seconds") or 120),
    )
    text = clean_text((value.get("message") or {}).get("content"))
    return ProviderResult(
        text=text, provider="ollama", model=clean_text(profile.get("model")),
        input_tokens=int(value.get("prompt_eval_count") or 0),
        output_tokens=int(value.get("eval_count") or 0), raw=value,
    )


def complete_text(
    profile: Mapping[str, Any], *, system: str, prompt: str,
    purpose: str = "generic", account_id: int | None = None,
    json_schema: Mapping[str, Any] | None = None,
) -> ProviderResult:
    _check_limits(profile)
    provider = clean_text(profile.get("provider"))
    kind = provider_defaults(provider).get("kind")
    retries = int(profile.get("retries") or 0)
    started = time.monotonic()
    last_error = ""
    for attempt in range(retries + 1):
        try:
            if provider == "openai":
                result = _openai_native(profile, system, prompt, json_schema=json_schema)
            elif kind == "anthropic":
                result = _anthropic(profile, system, prompt)
            elif kind == "gemini":
                result = _gemini(profile, system, prompt)
            elif kind == "azure_openai":
                result = _azure(profile, system, prompt)
            elif kind == "bedrock":
                result = _bedrock(profile, system, prompt)
            elif kind == "ollama":
                result = _ollama(profile, system, prompt)
            else:
                result = _openai_compatible(profile, system, prompt)
            latency = int((time.monotonic() - started) * 1000)
            _record_usage(
                profile, purpose=purpose, status="success", latency_ms=latency,
                account_id=account_id, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
            return result
        except Exception as exc:
            last_error = str(exc)
            if attempt < retries:
                time.sleep(min(4.0, 0.5 * (2 ** attempt)))
    latency = int((time.monotonic() - started) * 1000)
    _record_usage(
        profile, purpose=purpose, status="error", latency_ms=latency,
        account_id=account_id, error=last_error,
    )
    raise RuntimeError(last_error or "Richiesta al provider IA non riuscita.")


def complete_text_chain(
    profiles: Iterable[Mapping[str, Any]], *, system: str, prompt: str,
    purpose: str = "generic", account_id: int | None = None,
) -> tuple[ProviderResult, dict]:
    """Try enabled profiles in order and return the first usable text result."""
    errors: list[str] = []
    for profile in profiles:
        if not profile or not bool(profile.get("enabled", 1)):
            continue
        try:
            result = complete_text(
                profile, system=system, prompt=prompt, purpose=purpose,
                account_id=account_id,
            )
            if provider_result_text(result):
                return result, dict(profile)
            raise RuntimeError("Il provider IA non ha restituito testo utilizzabile.")
        except Exception as exc:
            errors.append(f"{clean_text(profile.get('name')) or profile.get('provider')}: {exc}")
    raise RuntimeError("Nessun provider IA ha restituito testo utilizzabile. " + " | ".join(errors))


def complete_json(
    profiles: Iterable[Mapping[str, Any]], *, system: str, prompt: str,
    purpose: str = "generic", account_id: int | None = None,
    json_schema: Mapping[str, Any] | None = None,
) -> tuple[dict, dict, ProviderResult]:
    errors: list[str] = []
    for profile in profiles:
        if not profile or not bool(profile.get("enabled", 1)):
            continue
        try:
            result = complete_text(
                profile, system=system, prompt=prompt, purpose=purpose,
                account_id=account_id, json_schema=json_schema,
            )
            try:
                parsed = _json_from_provider_result(result)
            except Exception as first_exc:
                # A few OpenAI model/account combinations return a completed
                # Responses object whose convenience text is empty or split into
                # multiple blocks. Retry once through Chat Completions Structured
                # Outputs before moving to the next configured provider.
                if clean_text(profile.get("provider")) == "openai" and json_schema:
                    fallback_result = _openai_native_json_fallback(
                        profile, system, prompt, json_schema
                    )
                    parsed = _json_from_provider_result(fallback_result)
                    result = fallback_result
                else:
                    raise first_exc
            return parsed, dict(profile), result
        except Exception as exc:
            errors.append(f"{clean_text(profile.get('name')) or profile.get('provider')}: {exc}")
    raise RuntimeError("Nessun provider IA ha risposto correttamente. " + " | ".join(errors))


def test_profile(profile_id: int, seller_id: int) -> dict:
    profile = get_profile(profile_id, seller_id)
    if not profile:
        raise RuntimeError("Profilo IA non trovato.")
    started = time.monotonic()
    try:
        result = complete_text(
            profile,
            system="Rispondi in modo sintetico.",
            prompt='Restituisci esclusivamente questo JSON: {"ok": true, "message": "connessione riuscita"}',
            purpose="connection_test",
        )
        message = f"Connessione riuscita con {result.provider} / {result.model}."
        status = "success"
    except Exception as exc:
        message = str(exc)
        status = "error"
    execute(
        """
        UPDATE ai_provider_profiles
        SET last_test_status=?,last_test_message=?,last_test_at=?,updated_at=?
        WHERE id=? AND seller_id=?
        """,
        (status, message, now_iso(), now_iso(), profile_id, seller_id),
    )
    return {
        "status": status, "message": message,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def usage_summary(seller_id: int, days: int = 30) -> list[dict]:
    return rows(
        """
        SELECT provider,model,status,COUNT(*) AS requests,
               SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,
               AVG(latency_ms) AS avg_latency_ms
        FROM ai_provider_usage
        WHERE seller_id=? AND datetime(created_at)>=datetime('now', ?)
        GROUP BY provider,model,status
        ORDER BY requests DESC,provider,model
        """,
        (seller_id, f"-{max(1, int(days))} days"),
    )
