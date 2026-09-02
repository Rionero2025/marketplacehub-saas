from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from services.ai_providers import (
    PROVIDER_CATALOG,
    delete_profile,
    ensure_schema,
    get_profile,
    list_profiles,
    profile_config,
    profile_secrets,
    provider_defaults,
    provider_options,
    save_profile,
    test_profile,
    usage_summary,
)
from services.security import masked
from services.session import bootstrap, seller_selector


bootstrap()
ensure_schema()

st.title("Provider IA")
st.caption(
    "Configura più servizi di intelligenza artificiale, conserva le chiavi cifrate e "
    "riutilizzale nei Ticket e nel motore Catalog Intelligence per la Creazione Prodotti."
)

seller_id = seller_selector("Seller per i provider IA")
if seller_id is None:
    st.stop()
seller_id = int(seller_id)

profiles = list_profiles(seller_id)
profile_map = {int(item["id"]): item for item in profiles}

summary_cols = st.columns(4)
summary_cols[0].metric("Profili configurati", len(profiles))
summary_cols[1].metric("Profili attivi", sum(1 for item in profiles if item.get("enabled")))
summary_cols[2].metric("Provider differenti", len({item.get("provider") for item in profiles}))
summary_cols[3].metric(
    "Connessioni verificate",
    sum(1 for item in profiles if item.get("last_test_status") == "success"),
)

if profiles:
    table = []
    for item in profiles:
        catalog = provider_defaults(item.get("provider"))
        table.append({
            "ID": int(item["id"]),
            "Nome": item.get("name"),
            "Provider": catalog.get("label"),
            "Modello": item.get("model"),
            "Attivo": bool(item.get("enabled")),
            "Ultimo test": item.get("last_test_status") or "Mai",
            "Messaggio test": item.get("last_test_message") or "",
            "Aggiornato": item.get("updated_at"),
        })
    st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

st.divider()
st.subheader("Crea o modifica un profilo")
edit_options = [0] + list(profile_map)
edit_id = st.selectbox(
    "Profilo da modificare",
    edit_options,
    format_func=lambda value: "Nuovo profilo" if value == 0 else f"{profile_map[value]['name']} · ID {value}",
)
editing = profile_map.get(int(edit_id), {})
current_provider = str(editing.get("provider") or "openai")
provider_keys = [key for key, _ in provider_options()]
provider = st.selectbox(
    "Provider",
    provider_keys,
    index=provider_keys.index(current_provider) if current_provider in provider_keys else 0,
    format_func=lambda key: PROVIDER_CATALOG[key]["label"],
)
defaults = provider_defaults(provider)
config = profile_config(editing)
secrets = profile_secrets(editing)

main_cols = st.columns(2)
name = main_cols[0].text_input(
    "Nome del profilo",
    value=str(editing.get("name") or defaults["label"]),
)
model = main_cols[1].text_input(
    "Modello",
    value=str(editing.get("model") or defaults.get("default_model") or ""),
)
base_url = st.text_input(
    "Base URL / endpoint",
    value=str(editing.get("base_url") or defaults.get("base_url") or ""),
    help="Per Azure inserisci l'endpoint della risorsa. Per Ollama normalmente http://localhost:11434.",
)

params = st.columns(4)
temperature = params[0].number_input(
    "Temperatura", min_value=0.0, max_value=2.0,
    value=float(editing.get("temperature") or 0.2), step=0.1,
)
max_tokens = params[1].number_input(
    "Token massimi", min_value=64, max_value=100000,
    value=int(editing.get("max_tokens") or 1200), step=64,
)
timeout_seconds = params[2].number_input(
    "Timeout secondi", min_value=5, max_value=600,
    value=int(editing.get("timeout_seconds") or 60), step=5,
)
retries = params[3].number_input(
    "Tentativi", min_value=0, max_value=5,
    value=int(editing.get("retries") or 2), step=1,
)

limit_cols = st.columns(3)
enabled = limit_cols[0].checkbox("Profilo attivo", value=bool(editing.get("enabled", 1)))
daily_limit = limit_cols[1].number_input(
    "Limite richieste giornaliere", min_value=0, max_value=100000,
    value=int(editing.get("daily_request_limit") or 0),
    help="0 = nessun limite.",
)
monthly_limit = limit_cols[2].number_input(
    "Limite richieste mensili", min_value=0, max_value=1000000,
    value=int(editing.get("monthly_request_limit") or 0),
    help="0 = nessun limite.",
)

st.markdown("**Credenziali segrete**")
secret_values: dict[str, str] = {}
if provider == "bedrock":
    aws_cols = st.columns(2)
    secret_values["aws_access_key_id"] = aws_cols[0].text_input(
        "AWS Access Key ID", type="password", value="",
        placeholder=masked(secrets.get("aws_access_key_id", "")),
    )
    secret_values["aws_secret_access_key"] = aws_cols[1].text_input(
        "AWS Secret Access Key", type="password", value="",
        placeholder=masked(secrets.get("aws_secret_access_key", "")),
    )
    secret_values["aws_session_token"] = st.text_input(
        "AWS Session Token facoltativo", type="password", value="",
        placeholder=masked(secrets.get("aws_session_token", "")),
    )
else:
    secret_values["api_key"] = st.text_input(
        "API Key / token segreto",
        type="password",
        value="",
        placeholder=masked(secrets.get("api_key", "")),
        help="Lascia vuoto per mantenere la chiave già salvata.",
    )

st.markdown("**Configurazione specifica**")
provider_config: dict[str, Any] = dict(config)
if provider == "azure_openai":
    azure_cols = st.columns(2)
    provider_config["deployment"] = azure_cols[0].text_input(
        "Deployment Azure",
        value=str(config.get("deployment") or editing.get("model") or ""),
    )
    provider_config["api_version"] = azure_cols[1].text_input(
        "API version",
        value=str(config.get("api_version") or "2024-10-21"),
    )
elif provider == "bedrock":
    provider_config["region"] = st.text_input(
        "Regione AWS", value=str(config.get("region") or "eu-west-1")
    )
elif provider == "anthropic":
    provider_config["anthropic_version"] = st.text_input(
        "Anthropic version", value=str(config.get("anthropic_version") or "2023-06-01")
    )
elif provider == "openrouter":
    router_cols = st.columns(2)
    provider_config["site_url"] = router_cols[0].text_input(
        "URL del sito facoltativo", value=str(config.get("site_url") or "")
    )
    provider_config["app_name"] = router_cols[1].text_input(
        "Nome applicazione", value=str(config.get("app_name") or "Marketplace Hub")
    )
elif provider == "custom_openai":
    headers_text = st.text_area(
        "Header aggiuntivi JSON facoltativi",
        value=json.dumps(config.get("headers") or {}, ensure_ascii=False, indent=2),
        height=100,
    )
    try:
        provider_config["headers"] = json.loads(headers_text or "{}")
    except json.JSONDecodeError:
        st.warning("Gli header aggiuntivi non sono JSON valido e non verranno salvati.")
        provider_config["headers"] = {}

button_cols = st.columns(4)
if button_cols[0].button("Salva profilo", type="primary", use_container_width=True):
    try:
        saved_id = save_profile(
            seller_id=seller_id,
            profile_id=int(edit_id) if edit_id else None,
            name=name,
            provider=provider,
            model=model,
            base_url=base_url,
            enabled=enabled,
            temperature=temperature,
            max_tokens=int(max_tokens),
            timeout_seconds=int(timeout_seconds),
            retries=int(retries),
            daily_request_limit=int(daily_limit),
            monthly_request_limit=int(monthly_limit),
            config=provider_config,
            secrets=secret_values,
        )
        st.success(f"Profilo IA salvato · ID {saved_id}.")
        st.rerun()
    except Exception as exc:
        st.error(f"Salvataggio non riuscito: {exc}")

if button_cols[1].button(
    "Verifica connessione", use_container_width=True,
    disabled=not bool(edit_id),
):
    try:
        with st.spinner("Verifica del provider IA in corso…"):
            result = test_profile(int(edit_id), seller_id)
        if result["status"] == "success":
            st.success(f"{result['message']} · {result['latency_ms']} ms")
        else:
            st.error(result["message"])
        st.rerun()
    except Exception as exc:
        st.error(f"Test non riuscito: {exc}")

confirm_delete = button_cols[2].checkbox(
    "Conferma eliminazione", disabled=not bool(edit_id)
)
if button_cols[3].button(
    "Elimina profilo", use_container_width=True,
    disabled=not bool(edit_id) or not confirm_delete,
):
    delete_profile(int(edit_id), seller_id)
    st.success("Profilo eliminato.")
    st.rerun()

st.divider()
st.subheader("Utilizzo IA negli ultimi 30 giorni")
usage = usage_summary(seller_id, days=30)
if usage:
    usage_table = []
    for item in usage:
        usage_table.append({
            "Provider": item.get("provider"),
            "Modello": item.get("model"),
            "Esito": item.get("status"),
            "Richieste": int(item.get("requests") or 0),
            "Token input": int(item.get("input_tokens") or 0),
            "Token output": int(item.get("output_tokens") or 0),
            "Latenza media ms": round(float(item.get("avg_latency_ms") or 0)),
        })
    st.dataframe(pd.DataFrame(usage_table), hide_index=True, use_container_width=True)
else:
    st.info("Non risultano ancora utilizzi IA registrati per questo Seller.")

st.caption(
    "Le chiavi vengono cifrate con MARKETPLACE_HUB_MASTER_KEY e non vengono incluse nei "
    "pacchetti ZIP, nei log o nelle esportazioni."
)
