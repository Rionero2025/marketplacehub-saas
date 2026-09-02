from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import streamlit as st

from services.ai_providers import (
    PROVIDER_CATALOG,
    list_profiles,
    profile_config,
    profile_secrets,
    provider_defaults,
    save_profile,
    test_profile,
)
from services.db import execute, now_iso, row, rows
from services.security import decrypt_dict, masked
from services.session import bootstrap, seller_selector
from services.support_connectors import (
    close_thread,
    create_worten_client,
    marketplace_environment,
    refresh_order_context_states,
    refresh_thread,
    send_thread_reply,
    sync_account_support,
    thread_still_needs_reply,
)
from services.support_hub import (
    HUMAN_ONLY_CATEGORIES,
    SAFE_AUTO_CATEGORIES,
    clean_text,
    configured_ai_profiles,
    duplicate_sent_response,
    ensure_schema,
    generate_ai_suggestion,
    get_ai_settings,
    json_list,
    mark_read,
    order_context,
    parse_iso,
    save_ai_draft,
    save_ai_settings,
    set_thread_auto_ai,
    stored_ai_api_key,
    strip_html,
)
from services.wysiwyg import allow_legacy_components_with_modern_streamlit


STATUS_LABELS = {
    "needs_reply": "Da rispondere",
    "waiting_customer": "In attesa del cliente",
    "informational": "Informativo",
    "closed": "Chiuso",
}
PRIORITY_LABELS = {
    "overdue": "🔴 Scaduto",
    "urgent": "🟠 Prioritario <24h",
    "in_time": "🟢 In tempo",
    "reply_needed": "🟡 Da rispondere",
    "not_required": "—",
}
MARKET_LABELS = {"kaufland": "Kaufland", "worten": "Worten"}


def _get_openai_key(settings: dict | None = None) -> str:
    if settings:
        stored = stored_ai_api_key(settings)
        if stored:
            return stored
    try:
        return clean_text(st.secrets.get("OPENAI_API_KEY", "")) or clean_text(os.getenv("OPENAI_API_KEY"))
    except Exception:
        return clean_text(os.getenv("OPENAI_API_KEY"))


def _configured_profiles(settings: dict, seller_id: int) -> list[dict]:
    return configured_ai_profiles(settings, seller_id)


def _ai_available(settings: dict, seller_id: int) -> bool:
    # Le credenziali IA vengono gestite esclusivamente tramite i profili provider.
    # Il vecchio campo OpenAI per-account resta leggibile solo per compatibilità
    # con database storici, ma non viene più usato dalla UI o dalle nuove chiamate.
    return bool(_configured_profiles(settings, seller_id))


def _format_local(value: Any) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return "—"
    return parsed.astimezone().strftime("%d/%m/%Y %H:%M")


def _thread_source_updated(thread: dict) -> str:
    try:
        raw = json.loads(thread.get("raw_json") or "{}")
    except json.JSONDecodeError:
        raw = {}
    return clean_text(
        raw.get("date_updated") or raw.get("ts_updated_iso")
        or thread.get("last_message_at")
    )


def _safe_json(value: Any) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _account_label(account: dict) -> str:
    return (
        f"{MARKET_LABELS.get(account['marketplace'], account['marketplace'].title())} · "
        f"{account['account_name']} · ID {account['id']}"
    )


def _editor(label: str, value: str, key: str) -> str:
    """Render Quill while shielding it from Streamlit's empty-HTML check."""
    toolbar = [
        [{"header": [1, 2, 3, False]}],
        ["bold", "italic", "underline", "strike"],
        [{"color": []}, {"background": []}],
        [{"script": "sub"}, {"script": "super"}],
        [{"list": "ordered"}, {"list": "bullet"}],
        [{"indent": "-1"}, {"indent": "+1"}],
        [{"align": []}],
        ["blockquote", "code-block"],
        ["link"],
        ["clean"],
    ]

    try:
        # The guard must be active during import too: some component builds
        # capture st.html while their module is initialised.
        with allow_legacy_components_with_modern_streamlit(st):
            from streamlit_quill import st_quill

            try:
                html = st_quill(
                    value=value or "",
                    html=True,
                    toolbar=toolbar,
                    key=key,
                    placeholder="Scrivi o modifica la risposta…",
                )
            except TypeError:
                # Older builds expose the standard toolbar but do not accept a
                # custom toolbar argument.
                html = st_quill(
                    value=value or "",
                    html=True,
                    key=key,
                    placeholder="Scrivi o modifica la risposta…",
                )
        return html or ""
    except ModuleNotFoundError as exc:
        st.error(
            "Editor WYSIWYG non disponibile: il modulo streamlit_quill non è "
            "installato nello stesso Python usato da Marketplace Hub. Chiudi il "
            "programma ed esegui RIPARA_DIPENDENZE_WINDOWS.bat."
        )
        st.caption(f"Dettaglio tecnico editor: {type(exc).__name__}: {exc}")
    except Exception as exc:
        st.error(
            "Il componente WYSIWYG non è riuscito a caricarsi. La compatibilità "
            "con Streamlit moderno è stata applicata, ma il componente ha "
            "restituito un errore diverso."
        )
        st.caption(f"Dettaglio tecnico editor: {type(exc).__name__}: {exc}")

    return st.text_area(
        label,
        value=strip_html(value or ""),
        height=260,
        key=f"{key}_runtime_fallback",
    )


def _inline_provider_setup(seller_id: int, scope: str) -> None:
    """Configure and verify AI providers directly from the Ticket page."""
    st.markdown("**Collega un provider IA**")
    st.caption(
        "Spunta il servizio che vuoi collegare. Si aprirà il relativo riquadro per "
        "API key, modello, endpoint e verifica della connessione."
    )
    profiles = list_profiles(seller_id)
    by_provider: dict[str, dict] = {}
    for item in profiles:
        key = clean_text(item.get("provider"))
        if key and key not in by_provider:
            by_provider[key] = item

    provider_keys = list(PROVIDER_CATALOG)
    cols = st.columns(3)
    selected: list[str] = []
    for idx, provider_key in enumerate(provider_keys):
        existing = by_provider.get(provider_key)
        label = PROVIDER_CATALOG[provider_key]["label"]
        checked = cols[idx % 3].checkbox(
            label,
            value=bool(existing and existing.get("enabled")),
            key=f"support_provider_check_{scope}_{provider_key}",
        )
        if checked:
            selected.append(provider_key)

    for provider_key in selected:
        defaults = provider_defaults(provider_key)
        existing = by_provider.get(provider_key, {})
        existing_config = profile_config(existing)
        existing_secrets = profile_secrets(existing)
        with st.expander(f"Configura {defaults['label']}", expanded=True):
            c1, c2 = st.columns(2)
            profile_name = c1.text_input(
                "Nome profilo",
                value=clean_text(existing.get("name")) or f"{defaults['label']} Ticket",
                key=f"support_provider_name_{scope}_{provider_key}",
            )
            model_name = c2.text_input(
                "Modello",
                value=clean_text(existing.get("model")) or clean_text(defaults.get("default_model")),
                key=f"support_provider_model_{scope}_{provider_key}",
            )
            base_url = st.text_input(
                "Base URL / endpoint",
                value=clean_text(existing.get("base_url")) or clean_text(defaults.get("base_url")),
                key=f"support_provider_url_{scope}_{provider_key}",
            )
            secrets: dict[str, str] = {}
            config = dict(existing_config)
            if provider_key == "bedrock":
                a, b = st.columns(2)
                secrets["aws_access_key_id"] = a.text_input(
                    "AWS Access Key ID", type="password",
                    placeholder=masked(existing_secrets.get("aws_access_key_id", "")),
                    key=f"support_provider_aws_id_{scope}_{provider_key}",
                )
                secrets["aws_secret_access_key"] = b.text_input(
                    "AWS Secret Access Key", type="password",
                    placeholder=masked(existing_secrets.get("aws_secret_access_key", "")),
                    key=f"support_provider_aws_secret_{scope}_{provider_key}",
                )
                secrets["aws_session_token"] = st.text_input(
                    "AWS Session Token facoltativo", type="password",
                    placeholder=masked(existing_secrets.get("aws_session_token", "")),
                    key=f"support_provider_aws_session_{scope}_{provider_key}",
                )
                config["region"] = st.text_input(
                    "Regione AWS", value=clean_text(config.get("region")) or "eu-west-1",
                    key=f"support_provider_region_{scope}_{provider_key}",
                )
            else:
                secrets["api_key"] = st.text_input(
                    "API Key / chiave segreta", type="password",
                    placeholder=masked(existing_secrets.get("api_key", "")),
                    help="Lascia vuoto per mantenere la chiave già cifrata.",
                    key=f"support_provider_key_{scope}_{provider_key}",
                )
            if provider_key == "azure_openai":
                a, b = st.columns(2)
                config["deployment"] = a.text_input(
                    "Deployment Azure",
                    value=clean_text(config.get("deployment")) or model_name,
                    key=f"support_provider_deployment_{scope}_{provider_key}",
                )
                config["api_version"] = b.text_input(
                    "API version", value=clean_text(config.get("api_version")) or "2024-10-21",
                    key=f"support_provider_api_version_{scope}_{provider_key}",
                )
            if provider_key == "anthropic":
                config["anthropic_version"] = st.text_input(
                    "Anthropic version",
                    value=clean_text(config.get("anthropic_version")) or "2023-06-01",
                    key=f"support_provider_anthropic_version_{scope}_{provider_key}",
                )
            action_cols = st.columns(2)
            if action_cols[0].button(
                "Salva configurazione", type="primary", use_container_width=True,
                key=f"support_provider_save_{scope}_{provider_key}",
            ):
                try:
                    saved_id = save_profile(
                        seller_id=seller_id,
                        profile_id=int(existing["id"]) if existing else None,
                        name=profile_name, provider=provider_key, model=model_name,
                        base_url=base_url, enabled=True, temperature=0.2,
                        max_tokens=1200, timeout_seconds=60, retries=2,
                        config=config, secrets=secrets,
                    )
                    st.success(f"Profilo salvato · ID {saved_id}.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Salvataggio non riuscito: {exc}")
            if action_cols[1].button(
                "Salva e verifica connessione", use_container_width=True,
                key=f"support_provider_test_{scope}_{provider_key}",
            ):
                try:
                    saved_id = save_profile(
                        seller_id=seller_id,
                        profile_id=int(existing["id"]) if existing else None,
                        name=profile_name, provider=provider_key, model=model_name,
                        base_url=base_url, enabled=True, temperature=0.2,
                        max_tokens=1200, timeout_seconds=60, retries=2,
                        config=config, secrets=secrets,
                    )
                    with st.spinner(f"Verifica {defaults['label']} in corso…"):
                        result = test_profile(int(saved_id), seller_id)
                    if result.get("status") == "success":
                        st.success(
                            f"Configurazione salvata e connessione verificata · "
                            f"{result.get('latency_ms', 0)} ms"
                        )
                    else:
                        st.error(clean_text(result.get("message")) or "Connessione non riuscita.")
                except Exception as exc:
                    st.error(f"Verifica non riuscita: {exc}")


def _attachments_from_uploads(uploaded) -> list[tuple[str, bytes, str]]:
    result = []
    for item in uploaded or []:
        result.append((item.name, item.getvalue(), clean_text(item.type) or "application/octet-stream"))
    return result


def _thread_rows(account_id: int, environment: str) -> list[dict]:
    return rows(
        """
        SELECT * FROM support_threads
        WHERE marketplace_account_id=? AND environment=?
        ORDER BY
          CASE priority_status WHEN 'overdue' THEN 0 WHEN 'urgent' THEN 1 ELSE 2 END,
          reply_needed DESC,last_message_at DESC,id DESC
        """,
        (account_id, environment),
    )


def _message_rows(account_id: int, environment: str, thread_id: str) -> list[dict]:
    return rows(
        """
        SELECT * FROM support_messages
        WHERE marketplace_account_id=? AND environment=? AND external_thread_id=?
        ORDER BY sent_at,id
        """,
        (account_id, environment, thread_id),
    )


def _display_sender(message: dict) -> tuple[str, str]:
    sender_type = clean_text(message.get("sender_type")).upper()
    if sender_type in {"SHOP", "SELLER"}:
        return "assistant", "Seller"
    if sender_type in {"OPERATOR", "CUSTOMER_SERVICE"}:
        return "assistant", "Marketplace"
    return "user", clean_text(message.get("sender_label")) or "Cliente"


def _save_draft_to_state(scope: str, thread_id: str, suggestion) -> None:
    st.session_state[f"support_draft_{scope}_{thread_id}"] = suggestion.reply_text
    st.session_state[f"support_suggestion_{scope}_{thread_id}"] = {
        "category": suggestion.category,
        "confidence": suggestion.confidence,
        "auto_send_allowed": suggestion.auto_send_allowed,
        "human_review_required": suggestion.human_review_required,
        "interim_notice": suggestion.interim_notice,
        "language": suggestion.language,
        "reasoning": suggestion.reasoning,
    }


def _execute_auto_replies(
    *, seller_id: int, account_id: int, account: dict, credentials: dict,
    marketplace: str, environment: str, settings: dict,
    threads: list[dict], limit: int = 20,
) -> list[dict]:
    results: list[dict] = []
    profiles = _configured_profiles(settings, seller_id)
    if not profiles or not bool(settings.get("enabled")):
        return results
    allowed_categories = set(settings.get("allowed_categories") or [])
    candidates = [
        item for item in threads
        if item.get("auto_ai_enabled") and item.get("reply_needed")
        and item.get("normalized_status") == "needs_reply"
    ][:max(1, int(limit))]
    for thread in candidates:
        thread_id = clean_text(thread["external_thread_id"])
        messages = _message_rows(account_id, environment, thread_id)
        base_context = order_context(
            seller_id, account_id, json_list(thread.get("order_ids_json")),
            environment=environment,
        )
        context, _context_audit, context_errors = refresh_order_context_states(
            account=account, credentials=credentials, context_rows=base_context,
            force_refresh=True,
        )
        context = context or base_context
        try:
            suggestion = generate_ai_suggestion(
                profiles=profiles, api_key="",
                model="", account_id=account_id,
                thread=thread, messages=messages, order_rows=context,
                seller_instructions=clean_text(settings.get("instructions")),
            )
            draft_id = save_ai_draft(
                seller_id=seller_id, account_id=account_id,
                marketplace=marketplace, environment=environment,
                thread_id=thread_id,
                source_updated_at=_thread_source_updated(thread),
                suggestion=suggestion,
            )
            if context_errors and json_list(thread.get("order_ids_json")):
                results.append({
                    "Ticket": thread_id, "Esito": "Bozza soltanto",
                    "Motivo": "stato ordine live non verificato: " + " · ".join(context_errors[:2]),
                })
                continue
            if (
                suggestion.category not in allowed_categories
                or not suggestion.auto_send_allowed
                or suggestion.human_review_required
                or suggestion.confidence < float(settings.get("confidence_threshold") or 0.92)
            ):
                results.append({
                    "Ticket": thread_id, "Esito": "Bozza soltanto",
                    "Motivo": f"{suggestion.category} · confidenza {suggestion.confidence:.0%}",
                })
                continue
            if duplicate_sent_response(account_id, environment, thread_id, suggestion.reply_text):
                results.append({"Ticket": thread_id, "Esito": "Saltato", "Motivo": "Risposta identica già inviata"})
                continue
            valid, reason = thread_still_needs_reply(
                account=account, credentials=credentials, thread_id=thread_id,
                expected_updated_at=_thread_source_updated(thread),
            )
            if not valid:
                results.append({"Ticket": thread_id, "Esito": "Saltato", "Motivo": reason})
                continue
            send_thread_reply(
                account=account, credentials=credentials,
                thread_id=thread_id, body=suggestion.reply_text,
                interim_notice=suggestion.interim_notice,
            )
            execute(
                "UPDATE support_ai_drafts SET status='sent',sent_at=? WHERE id=?",
                (now_iso(), draft_id),
            )
            results.append({"Ticket": thread_id, "Esito": "Inviato", "Motivo": suggestion.category})
        except Exception as exc:
            results.append({"Ticket": thread_id, "Esito": "Errore", "Motivo": str(exc)})
    return results

bootstrap()
ensure_schema()
st.title("Ticket e messaggi marketplace")
st.caption(
    "Console centralizzata per Kaufland e Worten: ticket divisi per stato, SLA di risposta, "
    "conversazioni complete, risposta via API e bozze IA completamente modificabili."
)

seller_id = seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND active=1 AND marketplace IN ('kaufland','worten')
    ORDER BY marketplace,account_name
    """,
    (seller_id,),
)
if not accounts:
    st.info("Configura almeno un account Kaufland o Worten per questo Seller.")
    st.stop()

account_map = {_account_label(item): item for item in accounts}
account_label = st.selectbox("Account marketplace", list(account_map), key="support_account")
account = account_map[account_label]
credentials = decrypt_dict(account["credentials_encrypted"])
marketplace = clean_text(account["marketplace"]).lower()
environment = marketplace_environment(marketplace, credentials)
account_id = int(account["id"])
scope = f"{seller_id}_{account_id}_{environment}"
settings = get_ai_settings(seller_id, account_id)

sync_status = row(
    """
    SELECT * FROM support_syncs
    WHERE marketplace_account_id=? AND environment=?
    ORDER BY id DESC LIMIT 1
    """,
    (account_id, environment),
)
status_cols = st.columns([2, 2, 2, 2])
status_cols[0].metric("Marketplace", MARKET_LABELS.get(marketplace, marketplace.title()))
status_cols[1].metric("Ambiente", "Playground" if environment == "test" else "Produzione")
status_cols[2].metric(
    "Ultima sincronizzazione",
    _format_local(sync_status.get("completed_at")) if sync_status else "Mai",
)
status_cols[3].metric(
    "Modalità IA automatica",
    "Attiva" if settings.get("enabled") else "Disattivata",
)

sync_col, full_col, confirm_col = st.columns([2, 2, 3])
if sync_col.button("Sincronizza ticket nuovi e modificati", type="primary", use_container_width=True):
    try:
        with st.spinner("Sincronizzazione ticket e messaggi in corso…"):
            result = sync_account_support(
                account=account, credentials=credentials, full=False,
                sla_hours=int(settings.get("sla_hours") or 24),
            )
        st.success(
            f"Sincronizzazione completata: {result.get('threads_seen', 0)} ticket rilevati, "
            f"{result.get('threads_new', 0)} nuovi, {result.get('threads_updated', 0)} aggiornati, "
            f"{result.get('messages_saved', 0)} messaggi salvati."
        )
        fresh_threads = _thread_rows(account_id, environment)
        auto_results = _execute_auto_replies(
            seller_id=seller_id, account_id=account_id, account=account,
            credentials=credentials, marketplace=marketplace,
            environment=environment, settings=settings, threads=fresh_threads,
            limit=int(settings.get("auto_batch_limit") or 10),
        )
        if auto_results:
            st.session_state[f"support_auto_results_{scope}"] = auto_results
        st.rerun()
    except Exception as exc:
        st.error(f"Sincronizzazione non riuscita: {exc}")

full_confirm = confirm_col.checkbox(
    "Confermo la risincronizzazione completa",
    key=f"support_full_confirm_{scope}",
)
if full_col.button(
    "Risincronizzazione completa",
    use_container_width=True,
    disabled=not full_confirm,
):
    try:
        with st.spinner("Risincronizzazione completa in corso…"):
            result = sync_account_support(
                account=account, credentials=credentials, full=True,
                sla_hours=int(settings.get("sla_hours") or 24),
            )
        st.success(
            f"Risincronizzazione completata: {result.get('threads_seen', 0)} ticket, "
            f"{result.get('messages_saved', 0)} messaggi."
        )
        fresh_threads = _thread_rows(account_id, environment)
        auto_results = _execute_auto_replies(
            seller_id=seller_id, account_id=account_id, account=account,
            credentials=credentials, marketplace=marketplace,
            environment=environment, settings=settings, threads=fresh_threads,
            limit=int(settings.get("auto_batch_limit") or 10),
        )
        if auto_results:
            st.session_state[f"support_auto_results_{scope}"] = auto_results
        st.rerun()
    except Exception as exc:
        st.error(f"Risincronizzazione non riuscita: {exc}")

all_threads = _thread_rows(account_id, environment)
if not all_threads:
    st.info("Non ci sono ticket salvati. Premi Sincronizza ticket nuovi e modificati.")
    st.stop()

counts = {
    key: sum(1 for item in all_threads if item.get("normalized_status") == key)
    for key in STATUS_LABELS
}
priority_count = sum(
    1 for item in all_threads
    if item.get("reply_needed") and item.get("priority_status") in {"urgent", "overdue"}
)
unread_count = sum(1 for item in all_threads if not item.get("is_read_local"))
overdue_count = sum(1 for item in all_threads if item.get("priority_status") == "overdue")
metric_cols = st.columns(7)
metric_cols[0].metric("Da rispondere", counts["needs_reply"])
metric_cols[1].metric("Prioritari <24h", priority_count)
metric_cols[2].metric("Scaduti", overdue_count)
metric_cols[3].metric("In attesa cliente", counts["waiting_customer"])
metric_cols[4].metric("Informativi", counts["informational"])
metric_cols[5].metric("Chiusi", counts["closed"])
metric_cols[6].metric("Da leggere", unread_count)

status_key = f"support_status_filter_{scope}"
priority_key = f"support_priority_filter_{scope}"
read_key = f"support_read_filter_{scope}"

def _apply_quick_view(status: str = "Tutti", priority: str = "Tutte", read: str = "Tutti") -> None:
    st.session_state[status_key] = status
    st.session_state[priority_key] = priority
    st.session_state[read_key] = read
    st.rerun()

st.markdown("**Viste rapide per stato**")
quick = st.columns(8)
if quick[0].button("Tutti", use_container_width=True):
    _apply_quick_view()
if quick[1].button("Da rispondere", use_container_width=True):
    _apply_quick_view("Da rispondere")
if quick[2].button("Prioritari", use_container_width=True):
    _apply_quick_view("Da rispondere", "Prioritari <24h")
if quick[3].button("Scaduti", use_container_width=True):
    _apply_quick_view("Da rispondere", "Scaduti")
if quick[4].button("Attesa cliente", use_container_width=True):
    _apply_quick_view("In attesa del cliente")
if quick[5].button("Informativi", use_container_width=True):
    _apply_quick_view("Informativi")
if quick[6].button("Chiusi", use_container_width=True):
    _apply_quick_view("Chiusi")
if quick[7].button("Da leggere", use_container_width=True):
    _apply_quick_view(read="Da leggere")

if settings.get("enabled"):
    st.warning(
        "Invio automatico IA attivo per questo account: vengono elaborate soltanto le "
        "categorie autorizzate e i ticket sui quali hai attivato esplicitamente l'IA. "
        f"Limite per esecuzione: {int(settings.get('auto_batch_limit') or 10)} ticket."
    )
else:
    st.info("IA automatica disattivata: le risposte IA vengono preparate soltanto come bozze modificabili.")

st.subheader("Lista ticket")
filter_cols = st.columns([1.5, 1.4, 1.4, 2.2])
status_filter = filter_cols[0].selectbox(
    "Stato",
    ["Tutti", "Da rispondere", "In attesa del cliente", "Informativi", "Chiusi"],
    key=status_key,
)
priority_filter = filter_cols[1].selectbox(
    "Priorità",
    ["Tutte", "Prioritari <24h", "Scaduti", "In tempo"],
    key=priority_key,
)
read_filter = filter_cols[2].selectbox(
    "Lettura", ["Tutti", "Da leggere", "Letti"],
    key=read_key,
)
search_value = filter_cols[3].text_input(
    "Cerca ticket, ordine, cliente o messaggio",
    key=f"support_search_{scope}",
)

date_cols = st.columns([1, 1, 1])
date_from = date_cols[0].date_input(
    "Aggiornati dal", value=date.today() - timedelta(days=90),
    key=f"support_date_from_{scope}",
)
date_to = date_cols[1].date_input(
    "Aggiornati al", value=date.today(),
    key=f"support_date_to_{scope}",
)
only_auto = date_cols[2].checkbox(
    "Solo ticket con IA automatica attiva",
    key=f"support_only_auto_{scope}",
)

status_reverse = {value: key for key, value in STATUS_LABELS.items()}
filtered = []
search_lower = clean_text(search_value).lower()
for item in all_threads:
    if status_filter != "Tutti" and item.get("normalized_status") != status_reverse[status_filter]:
        continue
    priority = item.get("priority_status")
    if priority_filter == "Prioritari <24h" and priority not in {"urgent", "overdue"}:
        continue
    if priority_filter == "Scaduti" and priority != "overdue":
        continue
    if priority_filter == "In tempo" and priority != "in_time":
        continue
    if read_filter == "Da leggere" and item.get("is_read_local"):
        continue
    if read_filter == "Letti" and not item.get("is_read_local"):
        continue
    if only_auto and not item.get("auto_ai_enabled"):
        continue
    updated = parse_iso(item.get("last_message_at"))
    if updated:
        local_date = updated.astimezone().date()
        if local_date < date_from or local_date > date_to:
            continue
    haystack = " ".join([
        clean_text(item.get("external_thread_id")),
        " ".join(json_list(item.get("order_ids_json"))),
        clean_text(item.get("customer_label")),
        clean_text(item.get("topic")),
        clean_text(item.get("last_message_preview")),
    ]).lower()
    if search_lower and search_lower not in haystack:
        continue
    filtered.append(item)

selection_key = f"support_selected_{scope}"
selected_ids = set(st.session_state.get(selection_key, []))
visible_ids = [clean_text(item["external_thread_id"]) for item in filtered]
button_cols = st.columns(4)
if button_cols[0].button("Seleziona tutti visibili", use_container_width=True):
    selected_ids.update(visible_ids)
    st.session_state[selection_key] = sorted(selected_ids)
    st.rerun()
if button_cols[1].button("Seleziona prioritari", use_container_width=True):
    selected_ids.update(
        clean_text(item["external_thread_id"])
        for item in filtered
        if item.get("priority_status") in {"urgent", "overdue"}
    )
    st.session_state[selection_key] = sorted(selected_ids)
    st.rerun()
if button_cols[2].button("Deseleziona tutti", use_container_width=True):
    st.session_state[selection_key] = []
    st.rerun()
if button_cols[3].button("Aggiorna vista", use_container_width=True):
    st.rerun()

display_rows = []
for item in filtered:
    thread_id = clean_text(item["external_thread_id"])
    display_rows.append({
        "Seleziona": thread_id in selected_ids,
        "Ticket / Thread": thread_id,
        "Stato": STATUS_LABELS.get(item.get("normalized_status"), item.get("normalized_status")),
        "Priorità": PRIORITY_LABELS.get(item.get("priority_status"), item.get("priority_status")),
        "Scadenza SLA": _format_local(item.get("sla_deadline")),
        "Ordine": ", ".join(json_list(item.get("order_ids_json"))) or "—",
        "Cliente": clean_text(item.get("customer_label")) or "—",
        "Argomento": clean_text(item.get("topic")) or "—",
        "Ultimo messaggio": clean_text(item.get("last_message_preview"))[:180],
        "Messaggi": int(item.get("message_count") or 0),
        "Aggiornato": _format_local(item.get("last_message_at")),
        "IA automatica": bool(item.get("auto_ai_enabled")),
        "Letto": bool(item.get("is_read_local")),
    })

frame = pd.DataFrame(display_rows)
if not frame.empty:
    edited = st.data_editor(
        frame,
        hide_index=True,
        use_container_width=True,
        height=min(620, 84 + len(frame) * 35),
        disabled=[column for column in frame.columns if column != "Seleziona"],
        column_config={
            "Seleziona": st.column_config.CheckboxColumn("Seleziona"),
            "Ultimo messaggio": st.column_config.TextColumn(width="large"),
            "Scadenza SLA": st.column_config.TextColumn(width="medium"),
        },
        key=f"support_editor_{scope}_{len(frame)}",
    )
    selected_ids = set(
        edited.loc[edited["Seleziona"].fillna(False), "Ticket / Thread"].astype(str)
    )
    st.session_state[selection_key] = sorted(selected_ids)
else:
    st.info("Nessun ticket corrisponde ai filtri selezionati.")

selected_threads = [item for item in all_threads if clean_text(item["external_thread_id"]) in selected_ids]
st.caption(f"Ticket visibili: {len(filtered)} · selezionati: {len(selected_threads)}")

auto_cols = st.columns(2)
if auto_cols[0].button(
    "Attiva risposte automatiche con IA sui selezionati",
    disabled=not selected_threads,
    use_container_width=True,
):
    updated = set_thread_auto_ai(account_id, environment, selected_ids, True)
    st.success(f"IA automatica attivata su {updated} ticket.")
    st.rerun()
if auto_cols[1].button(
    "Disattiva IA automatica sui selezionati",
    disabled=not selected_threads,
    use_container_width=True,
):
    updated = set_thread_auto_ai(account_id, environment, selected_ids, False)
    st.success(f"IA automatica disattivata su {updated} ticket.")
    st.rerun()

with st.expander("Impostazioni IA e SLA", expanded=False):
    _inline_provider_setup(seller_id, scope)
    st.divider()
    ai_enabled = st.checkbox(
        "Abilita l'invio automatico IA per questo account",
        value=bool(settings.get("enabled")),
        key=f"support_ai_enabled_{scope}",
    )
    available_profiles = list_profiles(seller_id, enabled_only=True)
    profile_labels = {
        int(item["id"]): f"{item['name']} · {item['provider']} · {item['model']}"
        for item in available_profiles
    }
    profile_ids = list(profile_labels)
    current_primary = int(settings.get("ai_profile_id") or 0)
    if profile_ids:
        if current_primary not in profile_ids:
            current_primary = profile_ids[0]
        primary_profile_id = st.selectbox(
            "Profilo IA principale",
            profile_ids,
            index=profile_ids.index(current_primary),
            format_func=lambda value: profile_labels.get(value, str(value)),
            key=f"support_ai_primary_profile_{scope}",
        )
        fallback_profile_ids = st.multiselect(
            "Profili IA di riserva, in ordine di selezione",
            [value for value in profile_ids if value != primary_profile_id],
            default=[
                value for value in settings.get("fallback_profile_ids", [])
                if value in profile_ids and value != primary_profile_id
            ],
            format_func=lambda value: profile_labels.get(value, str(value)),
            key=f"support_ai_fallback_profiles_{scope}",
        )
    else:
        primary_profile_id = None
        fallback_profile_ids = []
        st.warning(
            "Nessun profilo IA attivo. Spunta un provider nel riquadro superiore, "
            "inserisci le credenziali e premi Salva e verifica connessione."
        )
    st.caption(
        "API key, modello ed endpoint si configurano una sola volta nei profili provider "
        "del riquadro superiore. Le credenziali restano cifrate nel database."
    )
    threshold = st.slider(
        "Confidenza minima per invio automatico",
        min_value=0.50, max_value=1.00,
        value=float(settings.get("confidence_threshold") or 0.92),
        step=0.01,
        key=f"support_ai_threshold_{scope}",
    )
    settings_cols = st.columns(2)
    sla_hours = settings_cols[0].number_input(
        "SLA calcolato per risposta (ore)", min_value=1, max_value=168,
        value=int(settings.get("sla_hours") or 24), step=1,
        key=f"support_sla_hours_{scope}",
    )
    auto_batch_limit = settings_cols[1].number_input(
        "Massimo risposte automatiche per esecuzione",
        min_value=1, max_value=100,
        value=int(settings.get("auto_batch_limit") or 10), step=1,
        key=f"support_ai_batch_limit_{scope}",
    )
    allowed = st.multiselect(
        "Categorie autorizzate all'invio automatico",
        sorted(SAFE_AUTO_CATEGORIES),
        default=[x for x in settings.get("allowed_categories", []) if x in SAFE_AUTO_CATEGORIES],
        key=f"support_ai_categories_{scope}",
    )
    instructions = st.text_area(
        "Istruzioni specifiche del Seller",
        value=clean_text(settings.get("instructions")),
        height=120,
        key=f"support_ai_instructions_{scope}",
    )
    if st.button("Salva impostazioni IA e SLA", type="primary"):
        save_ai_settings(
            seller_id=seller_id, account_id=account_id,
            enabled=ai_enabled,
            model="",
            confidence_threshold=threshold, sla_hours=int(sla_hours),
            allowed_categories=allowed, instructions=instructions,
            api_key="", auto_batch_limit=int(auto_batch_limit),
            ai_profile_id=int(primary_profile_id) if primary_profile_id else None,
            fallback_profile_ids=fallback_profile_ids,
        )
        st.success("Impostazioni salvate.")
        st.rerun()
    if not _ai_available(settings, seller_id):
        st.warning("Nessun profilo IA attivo configurato per questo account.")

# L’IA automatica viene eseguita dopo le sincronizzazioni e può essere avviata anche manualmente.
if st.button(
    "Esegui ora le risposte automatiche IA selezionate",
    type="primary",
    disabled=not selected_threads or not bool(settings.get("enabled")) or not _ai_available(settings, seller_id),
    use_container_width=True,
):
    results = _execute_auto_replies(
        seller_id=seller_id, account_id=account_id, account=account,
        credentials=credentials, marketplace=marketplace,
        environment=environment, settings=settings, threads=selected_threads,
        limit=int(settings.get("auto_batch_limit") or 10),
    )
    st.session_state[f"support_auto_results_{scope}"] = results
    st.rerun()

auto_results = st.session_state.get(f"support_auto_results_{scope}")
if auto_results:
    st.markdown("**Esito ultimo invio automatico IA**")
    st.dataframe(auto_results, hide_index=True, use_container_width=True)

st.divider()
st.subheader("Conversazione e risposta")
open_candidates = filtered or all_threads
thread_map = {
    f"{item['external_thread_id']} · {STATUS_LABELS.get(item['normalized_status'], item['normalized_status'])} · "
    f"{', '.join(json_list(item.get('order_ids_json'))) or 'senza ordine'}": item
    for item in open_candidates
}
chosen_label = st.selectbox("Apri ticket", list(thread_map), key=f"support_open_thread_{scope}")
thread = thread_map[chosen_label]
thread_id = clean_text(thread["external_thread_id"])
mark_read(account_id, environment, thread_id, True)

header_cols = st.columns(4)
header_cols[0].metric("Stato", STATUS_LABELS.get(thread.get("normalized_status"), "—"))
header_cols[1].metric("Priorità", PRIORITY_LABELS.get(thread.get("priority_status"), "—"))
header_cols[2].metric("Scadenza SLA", _format_local(thread.get("sla_deadline")))
header_cols[3].metric("Messaggi", int(thread.get("message_count") or 0))
st.caption(
    f"Cliente: **{clean_text(thread.get('customer_label')) or '—'}** · "
    f"Ordine/i: **{', '.join(json_list(thread.get('order_ids_json'))) or '—'}** · "
    f"Argomento: **{clean_text(thread.get('topic')) or '—'}**"
)

action_cols = st.columns(3 if marketplace == "kaufland" else 2)
if action_cols[0].button("Aggiorna questo ticket dall'API", use_container_width=True):
    try:
        refresh_thread(
            account=account, credentials=credentials,
            thread_id=thread_id, sla_hours=int(settings.get("sla_hours") or 24),
        )
        st.success("Ticket aggiornato.")
        st.rerun()
    except Exception as exc:
        st.error(f"Aggiornamento non riuscito: {exc}")
if action_cols[1].button("Segna come da leggere", use_container_width=True):
    mark_read(account_id, environment, thread_id, False)
    st.rerun()
if marketplace == "kaufland":
    close_confirm = action_cols[2].checkbox(
        "Conferma chiusura", key=f"support_close_confirm_{scope}_{thread_id}",
        disabled=thread.get("normalized_status") == "closed",
    )
    if action_cols[2].button(
        "Chiudi ticket Kaufland", use_container_width=True,
        disabled=not close_confirm or thread.get("normalized_status") == "closed",
        key=f"support_close_{scope}_{thread_id}",
    ):
        try:
            close_thread(
                account=account, credentials=credentials, thread_id=thread_id,
                sla_hours=int(settings.get("sla_hours") or 24),
            )
            st.success("Ticket Kaufland chiuso correttamente.")
            st.rerun()
        except Exception as exc:
            st.error(f"Chiusura ticket non riuscita: {exc}")

messages = _message_rows(account_id, environment, thread_id)
for message in messages:
    role, sender = _display_sender(message)
    with st.chat_message(role):
        st.markdown(f"**{sender}** · {_format_local(message.get('sent_at'))}")
        st.write(clean_text(message.get("body")) or "—")
        attachments = json_list(message.get("attachments_json"))
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            name = clean_text(attachment.get("name") or attachment.get("filename")) or "Allegato"
            attachment_id = clean_text(attachment.get("id") or attachment.get("attachment_id"))
            st.caption(f"Allegato: {name}")
            if marketplace == "worten" and attachment_id:
                cache_key = f"support_attachment_{scope}_{attachment_id}"
                if st.button(f"Prepara download · {name}", key=f"prepare_{cache_key}"):
                    try:
                        st.session_state[cache_key] = create_worten_client(credentials).download_attachment(attachment_id)
                    except Exception as exc:
                        st.error(f"Download allegato non riuscito: {exc}")
                if cache_key in st.session_state:
                    st.download_button(
                        f"Scarica {name}", st.session_state[cache_key],
                        file_name=name, key=f"download_{cache_key}",
                    )

base_context_rows = order_context(
    seller_id, account_id, json_list(thread.get("order_ids_json")),
    environment=environment,
)
context_token = "|".join(sorted(
    f"{clean_text(item.get('order_id'))}:{clean_text(item.get('order_line_id'))}:"
    f"{clean_text(item.get('raw_status'))}"
    for item in base_context_rows
))
context_cache_key = (
    f"support_live_order_context_v177_{scope}_{thread_id}_"
    f"{abs(hash(context_token))}"
)
with st.expander("Dati ordine collegato", expanded=False):
    refresh_context = st.button(
        "Verifica ora lo stato ordine via API",
        use_container_width=True,
        key=f"support_refresh_order_state_{scope}_{thread_id}",
        disabled=not base_context_rows,
    )
    cached_context = st.session_state.get(context_cache_key)
    if refresh_context or not isinstance(cached_context, dict):
        if base_context_rows:
            with st.spinner("Verifica dello stato attuale dell'ordine sul marketplace…"):
                live_context_rows, live_context_audit, live_context_errors = (
                    refresh_order_context_states(
                        account=account, credentials=credentials,
                        context_rows=base_context_rows,
                        force_refresh=refresh_context,
                    )
                )
            cached_context = {
                "rows": live_context_rows or base_context_rows,
                "audit": live_context_audit,
                "errors": live_context_errors,
            }
        else:
            cached_context = {"rows": [], "audit": [], "errors": []}
        st.session_state[context_cache_key] = cached_context
    context_rows = [dict(item) for item in (cached_context.get("rows") or [])]
    context_errors = [clean_text(item) for item in (cached_context.get("errors") or []) if clean_text(item)]
    if context_rows:
        display_context = []
        for item in context_rows:
            display_context.append({
                "Ordine": clean_text(item.get("order_id")),
                "Riga/unità": clean_text(item.get("order_line_id")),
                "Stato API attuale": clean_text(item.get("live_raw_status") or item.get("raw_status")),
                "Descrizione": clean_text(item.get("live_status_label") or item.get("status_label")),
                "Macro-stato": clean_text(item.get("live_macro_status") or item.get("normalized_status")),
                "Spedito": "Sì" if item.get("live_already_shipped") else "No",
                "Cancellato": "Sì" if item.get("live_cancelled") else "No",
                "Reso": "Sì" if item.get("live_returned") else "No",
                "Tracking": clean_text(item.get("tracking")),
                "Prodotto": clean_text(item.get("product_title")),
                "Cliente": clean_text(item.get("customer_name")),
                "Verificato": "Sì" if item.get("live_verified") else "No",
                "Motivo": clean_text(item.get("live_reason")),
            })
        st.dataframe(display_context, hide_index=True, use_container_width=True)
        if context_errors:
            st.warning(
                "Verifica stato ordine incompleta: " + " · ".join(context_errors[:3])
            )
        else:
            st.caption(
                "Lo stato mostrato è stato letto dal marketplace ed è utilizzato anche "
                "dal suggerimento IA e dalle risposte automatiche."
            )
    else:
        st.info("Nessun dettaglio ordine disponibile nel database locale.")

state_draft_key = f"support_draft_{scope}_{thread_id}"
if st.button(
    "Genera suggerimento IA",
    disabled=not _ai_available(settings, seller_id),
    key=f"support_generate_ai_{scope}_{thread_id}",
):
    try:
        # Re-read the marketplace order state immediately before asking the IA.
        # The suggestion must not rely on a stale local SHIPPING/SHIPPED/CANCELED value.
        current_context, _current_audit, current_context_errors = refresh_order_context_states(
            account=account, credentials=credentials, context_rows=base_context_rows,
            force_refresh=True,
        )
        context_rows = current_context or context_rows
        if current_context_errors and json_list(thread.get("order_ids_json")):
            st.warning(
                "Alcuni stati ordine non sono stati verificati live; la bozza IA li "
                "riceverà come non verificati: " + " · ".join(current_context_errors[:2])
            )
        suggestion = generate_ai_suggestion(
            profiles=_configured_profiles(settings, seller_id),
            api_key="", model="",
            account_id=account_id,
            thread=thread, messages=messages, order_rows=context_rows,
            seller_instructions=clean_text(settings.get("instructions")),
        )
        _save_draft_to_state(scope, thread_id, suggestion)
        save_ai_draft(
            seller_id=seller_id, account_id=account_id,
            marketplace=marketplace, environment=environment,
            thread_id=thread_id,
            source_updated_at=_thread_source_updated(thread),
            suggestion=suggestion,
        )
        st.success("Bozza IA generata. Puoi modificarla liberamente prima dell'invio.")
        st.rerun()
    except Exception as exc:
        st.error(f"Generazione IA non riuscita: {exc}")

suggestion_data = st.session_state.get(f"support_suggestion_{scope}_{thread_id}")
if suggestion_data:
    st.info(
        f"Categoria IA: {suggestion_data['category']} · "
        f"confidenza {suggestion_data['confidence']:.0%} · "
        f"controllo umano: {'sì' if suggestion_data['human_review_required'] else 'no'}. "
        f"{suggestion_data['reasoning']}"
    )

reply_value = _editor(
    "Risposta",
    st.session_state.get(state_draft_key, ""),
    key=f"support_reply_editor_{scope}_{thread_id}",
)
manual_interim = False
if marketplace == "kaufland":
    manual_interim = st.checkbox(
        "Avviso provvisorio: mantieni il ticket sotto la nostra responsabilità",
        value=bool(suggestion_data and suggestion_data.get("interim_notice")),
        key=f"support_interim_{scope}_{thread_id}",
    )
uploaded_reply_files = st.file_uploader(
    "Allegati alla risposta",
    accept_multiple_files=True,
    key=f"support_reply_files_{scope}_{thread_id}",
)
st.caption("Editor WYSIWYG completo: titoli, grassetto, corsivo, sottolineato, colori, elenchi, allineamento, citazioni e link. Il marketplace riceve testo pulito compatibile con le API. Nessun messaggio viene inviato automaticamente se la modalità IA automatica è disattivata.")
if st.button(
    "Invia risposta al ticket",
    type="primary",
    disabled=not clean_text(reply_value),
    use_container_width=True,
    key=f"support_send_{scope}_{thread_id}",
):
    try:
        result = send_thread_reply(
            account=account, credentials=credentials,
            thread_id=thread_id, body=strip_html(reply_value),
            attachments=_attachments_from_uploads(uploaded_reply_files),
            interim_notice=manual_interim,
        )
        st.session_state[state_draft_key] = ""
        st.success("Risposta inviata correttamente via API.")
        st.rerun()
    except Exception as exc:
        st.error(f"Invio risposta non riuscito: {exc}")

with st.expander("Storico azioni e risposte IA", expanded=False):
    action_rows = rows(
        """
        SELECT action_type,status,error,created_at,request_json,response_json
        FROM support_actions
        WHERE marketplace_account_id=? AND environment=? AND external_thread_id=?
        ORDER BY id DESC LIMIT 100
        """,
        (account_id, environment, thread_id),
    )
    draft_rows = rows(
        """
        SELECT category,confidence,status,reply_text,reasoning,created_at,sent_at
        FROM support_ai_drafts
        WHERE marketplace_account_id=? AND environment=? AND external_thread_id=?
        ORDER BY id DESC LIMIT 100
        """,
        (account_id, environment, thread_id),
    )
    if action_rows:
        st.markdown("**Azioni API**")
        st.dataframe(action_rows, hide_index=True, use_container_width=True)
    if draft_rows:
        st.markdown("**Bozze IA**")
        st.dataframe(draft_rows, hide_index=True, use_container_width=True)
    if not action_rows and not draft_rows:
        st.info("Nessuna azione registrata per questo ticket.")
