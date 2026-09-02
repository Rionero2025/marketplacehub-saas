from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from services.ai_providers import list_profiles, provider_defaults
from services.catalog_intelligence import CATALOG_INTELLIGENCE_VERSION
from services.catalog_intelligence.ai_enrichment import ai_runs
from services.catalog_intelligence.accounts import marketplace_accounts_for_seller
from services.catalog_intelligence.capabilities import discover_account_capabilities
from services.catalog_intelligence.normalization import persist_saved_view
from services.catalog_intelligence.publication import (
    plan_publication_job,
    publication_configuration_options,
    refresh_offers,
    refresh_products,
    retry_failed_items,
    run_publication_cycle,
    simulate_publication_job,
    submit_offers,
    submit_products,
)
from services.catalog_intelligence.repository import (
    apply_feed_preparations_to_job,
    capabilities_for_account,
    category_assignments_for_source,
    category_classification_runs,
    create_publication_job,
    feed_preparations_for_source,
    latest_taxonomy_snapshot,
    marketplace_imports_for_job,
    publication_artifact_bytes,
    publication_artifacts,
    publication_events,
    publication_items,
    publication_jobs,
    source_snapshot,
    source_snapshots,
    taxonomy_categories,
    taxonomy_sync_runs,
)
from services.catalog_intelligence.taxonomy import ensure_account_taxonomy, taxonomy_scope_key
from services.catalog_intelligence.utils import clean_text, load_json
from services.catalog_intelligence.workflow import (
    approve_category_assignment,
    classify_source_products,
    prepare_assigned_product_feeds,
    resolve_review_categories_with_ai,
)
from services.db import rows
from services.session import bootstrap, seller_selector
from services.saved_view_storage import resolve_saved_view_path


bootstrap()

st.title("Creazione Prodotti")
st.caption(
    "Catalog Intelligence costruisce da zero la scheda di un prodotto non ancora presente sul marketplace. "
    "La categoria viene scelta soltanto tra le categorie foglia della tassonomia ufficiale salvata nel database."
)
# Contratto storico v244 preservato nei test: La pubblicazione remota rimane bloccata finché non viene esplicitamente sbloccata.
st.info(
    f"Versione v{CATALOG_INTELLIGENCE_VERSION}: tassonomia persistente, classificazione per singolo prodotto, "
    "feed completo, AI vincolata alla tassonomia ufficiale, validazione deterministica e pubblicazione reale controllata. "
    "L'AI non può creare categorie o caratteristiche: ogni proposta deve citare un dato reale del feed e viene validata "
    "prima dell'uso. La modalità Simulazione non scrive mai sul marketplace; la modalità Reale richiede uno sblocco "
    "esplicito per ciascun ciclo."
)

seller_id = seller_selector("Seller per la creazione prodotti")
if seller_id is None:
    st.stop()

ai_profiles = list_profiles(int(seller_id), enabled_only=True)
ai_profile_by_id = {int(item["id"]): item for item in ai_profiles}

def _ai_profile_label(profile_id: int) -> str:
    profile = ai_profile_by_id[int(profile_id)]
    provider_label = provider_defaults(profile.get("provider")).get("label") or profile.get("provider")
    return f"{profile.get('name')} · {provider_label} · {profile.get('model')}"

accounts = marketplace_accounts_for_seller(seller_id)
if not accounts:
    st.warning(
        "Il Seller non ha account Kaufland o Worten attivi. Inserisci prima le credenziali in Gestione Seller."
    )
    st.stop()

account_labels = {
    f"{item['marketplace'].title()} · {item['account_name']} · ID {item['id']}": item
    for item in accounts
}
account_label = st.selectbox(
    "Marketplace e account",
    list(account_labels),
    key=f"catalog_account_{seller_id}",
)
account = account_labels[account_label]
account_id = int(account["id"])
marketplace = clean_text(account["marketplace"]).lower()

if marketplace == "kaufland":
    environment = st.radio(
        "Ambiente Kaufland",
        ("live", "test"),
        horizontal=True,
        key=f"catalog_environment_{account_id}",
    )
else:
    environment = "live"
    st.caption("Worten utilizza il tenant Mirakl configurato nell'account del Seller.")


def _stored_capability(key: str) -> dict[str, Any] | None:
    for item in capabilities_for_account(account_id, environment):
        if item.get("capability_key") == key:
            return item
    return None


def _capability_sample(key: str) -> list[Any]:
    item = _stored_capability(key)
    if not item:
        return []
    details = load_json(item.get("details_json"), {})
    return list(details.get("sample") or []) if isinstance(details, dict) else []


def _sample_codes(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            code = clean_text(
                value.get("code") or value.get("locale") or value.get("storefront") or value.get("id")
            )
        else:
            code = clean_text(value)
        if code and code not in result:
            result.append(code)
    return result


st.subheader("Contesto di lavoro")
context_columns = st.columns(3)
if marketplace == "kaufland":
    storefront_candidates = [value.lower() for value in _sample_codes(_capability_sample("storefronts"))]
    storefront_candidates = list(dict.fromkeys(value for value in storefront_candidates if value))
    with context_columns[0]:
        if storefront_candidates:
            storefront = st.selectbox(
                "Storefront",
                storefront_candidates,
                key=f"catalog_storefront_{account_id}_{environment}",
            )
        else:
            storefront = st.text_input(
                "Storefront",
                value="de",
                help="Esegui il controllo capacità per caricare gli storefront ufficiali.",
                key=f"catalog_storefront_text_{account_id}_{environment}",
            ).strip().lower()
    locale_candidates = _sample_codes(_capability_sample("locales"))
    with context_columns[1]:
        if locale_candidates:
            locale = st.selectbox(
                "Locale dati prodotto",
                locale_candidates,
                key=f"catalog_locale_{account_id}_{environment}",
            )
        else:
            locale = st.text_input(
                "Locale dati prodotto",
                value="de-DE" if storefront == "de" else "",
                key=f"catalog_locale_text_{account_id}_{environment}",
            ).strip()
    with context_columns[2]:
        st.text_input("Gerarchia", value="Kaufland", disabled=True)
    hierarchy = ""
else:
    storefront = "pt"
    with context_columns[0]:
        st.text_input("Canale", value="Worten Portugal", disabled=True)
    with context_columns[1]:
        locale = st.text_input(
            "Locale Worten/Mirakl",
            value="pt_PT",
            key=f"catalog_locale_{account_id}",
        ).strip()
    with context_columns[2]:
        hierarchy = st.text_input(
            "Codice gerarchia Mirakl",
            help="Vuoto = tutte le gerarchie accessibili all'account.",
            key=f"catalog_hierarchy_{account_id}",
        ).strip()

scope_key = taxonomy_scope_key(
    marketplace,
    storefront=storefront,
    locale=locale,
    hierarchy=hierarchy,
)
active_taxonomy = latest_taxonomy_snapshot(
    account_id,
    environment=environment,
    scope_key=scope_key,
)

config_tab, source_tab, classification_tab, feed_tab, publication_tab, history_tab = st.tabs(
    (
        "1. Account e tassonomia",
        "2. Catalogo sorgente",
        "3. Determina categorie",
        "4. Prepara feed",
        "5. Pubblicazione",
        "6. Storico",
    )
)

with config_tab:
    st.subheader("Capacità reali dell'account")
    st.write(
        "Il controllo usa endpoint di lettura e non crea prodotti, offerte o import."
    )
    if st.button(
        "Verifica credenziali e capacità API",
        type="primary",
        key=f"catalog_capabilities_{account_id}_{environment}",
    ):
        with st.spinner("Controllo in corso..."):
            try:
                discovered = discover_account_capabilities(
                    seller_id=seller_id,
                    account_id=account_id,
                    environment=environment,
                )
            except Exception as exc:
                st.error(f"Controllo non riuscito: {exc}")
            else:
                supported = sum(1 for item in discovered if item.supported)
                st.success(f"Controllo completato: {supported}/{len(discovered)} capacità disponibili.")
                st.rerun()

    stored_capabilities = capabilities_for_account(account_id, environment)
    if stored_capabilities:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Capacità": item.get("capability_key"),
                        "Disponibile": "Sì" if item.get("supported") else "No",
                        "HTTP": item.get("status_code") or "",
                        "Messaggio": item.get("message") or "",
                        "Controllata": item.get("checked_at") or "",
                    }
                    for item in stored_capabilities
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.warning("Nessun controllo capacità ancora memorizzato per questo account.")

    st.divider()
    st.subheader("Tassonomia ufficiale persistente")
    active_taxonomy = latest_taxonomy_snapshot(
        account_id,
        environment=environment,
        scope_key=scope_key,
    )
    if active_taxonomy:
        st.success(
            "La tassonomia è già nel database. Apertura pagina, classificazione e preparazione feed "
            "usano questa copia locale senza riscaricare categorie e attributi."
        )
        metrics = st.columns(5)
        metrics[0].metric("Snapshot", active_taxonomy["id"])
        metrics[1].metric("Categorie", active_taxonomy["category_count"])
        metrics[2].metric("Attributi", active_taxonomy["attribute_count"])
        metrics[3].metric("Valori ammessi", active_taxonomy["value_count"])
        metrics[4].metric("Scope", active_taxonomy["scope_key"])
        st.caption(
            f"Salvata: {active_taxonomy['created_at']} · Hash {str(active_taxonomy['content_hash'])[:12]}"
        )
    else:
        st.info(
            "È la prima configurazione per questo account/storefront. Il primo download salva la tassonomia "
            "nel database; i lavori successivi la riutilizzano."
        )

    # Contract preserved from v244: the official taxonomy is synchronized once
    # and then reused from the local database.
    st.caption("Sincronizza tassonomia ufficiale soltanto alla prima configurazione o quando desideri aggiornarla.")
    sync_label = "Aggiorna tassonomia via API" if active_taxonomy else "Scarica tassonomia iniziale"
    if st.button(
        sync_label,
        type="primary",
        key=f"catalog_sync_taxonomy_{account_id}_{environment}_{scope_key}",
    ):
        progress_bar = st.progress(0, text="Avvio sincronizzazione...")

        def taxonomy_progress(current: int, total: int | None) -> None:
            if total and total > 0:
                progress_bar.progress(
                    min(1.0, current / total),
                    text=f"Categorie scaricate: {current}/{total}",
                )
            else:
                progress_bar.progress(0, text=f"Categorie scaricate: {current}")

        try:
            result = ensure_account_taxonomy(
                seller_id=seller_id,
                account_id=account_id,
                environment=environment,
                storefront=storefront,
                locale=locale,
                hierarchy=hierarchy,
                force_refresh=bool(active_taxonomy),
                progress=taxonomy_progress,
            )
        except Exception as exc:
            progress_bar.empty()
            st.error(f"Sincronizzazione tassonomia fallita: {exc}")
        else:
            progress_bar.progress(1.0, text="Tassonomia memorizzata.")
            snapshot_id = result["snapshot_id"]
            st.success(f"Snapshot ufficiale {snapshot_id} disponibile nel database.")
            st.rerun()

    active_taxonomy = latest_taxonomy_snapshot(
        account_id,
        environment=environment,
        scope_key=scope_key,
    )
    if active_taxonomy:
        search_category = st.text_input(
            "Cerca nella tassonomia salvata",
            key=f"catalog_taxonomy_search_{active_taxonomy['id']}",
        )
        category_preview = taxonomy_categories(
            int(active_taxonomy["id"]),
            leaf_only=False,
            search=search_category,
            limit=1000,
        )
        if category_preview:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "ID": item["external_id"],
                            "Percorso": item["path"] or item["label"],
                            "Foglia": "Sì" if item["is_leaf"] else "No",
                            "Livello": item["level"],
                        }
                        for item in category_preview
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=330,
            )

with source_tab:
    st.subheader("Listino e vista prodotti")
    views = rows(
        """
        SELECT sv.*,pl.name AS price_list_name,pl.supplier_id,s.name AS supplier_name
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE sv.seller_id=?
        ORDER BY sv.updated_at DESC,sv.id DESC
        """,
        (seller_id,),
    )
    if not views:
        st.warning(
            "Non ci sono viste salvate per questo Seller. Creane una in 'Lavora sui Listini' e poi torna qui."
        )
    else:
        view_labels = {
            f"{item['supplier_name']} · {item['price_list_name']} · {item['name']} · {item['row_count']} righe": item
            for item in views
        }
        view_label = st.selectbox(
            "Vista sorgente",
            list(view_labels),
            key=f"catalog_source_view_{seller_id}",
        )
        selected_view = view_labels[view_label]
        try:
            path = resolve_saved_view_path(selected_view)
            view_available = path.is_file()
        except Exception as storage_error:
            path = Path(clean_text(selected_view.get("snapshot_path")))
            view_available = False
            st.error(f"La vista non è disponibile né in cache né nello storage: {storage_error}")
        force_rebuild = st.checkbox(
            "Rigenera anche se questa stessa vista è già stata normalizzata",
            value=False,
            help="Normalmente Marketplace Hub riutilizza subito lo snapshot già pronto. Attivalo soltanto se vuoi ricostruirlo.",
            key=f"catalog_normalize_force_{selected_view['id']}",
        )
        if st.button(
            "Normalizza e memorizza i prodotti",
            type="primary",
            disabled=not view_available,
            key=f"catalog_normalize_{selected_view['id']}",
        ):
            progress_bar = st.progress(0)
            progress_status = st.empty()
            progress_stats = st.empty()
            operation_started = time.perf_counter()
            progress_counters = {
                "total": max(0, int(selected_view.get("row_count") or 0)),
                "read": 0,
                "saved": 0,
            }
            phase_labels = {
                "LOAD": "Lettura della vista",
                "HASH": "Verifica del contenuto",
                "SNAPSHOT": "Preparazione snapshot",
                "NORMALIZE": "Lettura e normalizzazione prodotti",
                "PERSIST": "Salvataggio nel database",
                "CACHE": "Riutilizzo dati già memorizzati",
                "COMPLETE": "Completato",
            }

            def _format_duration(seconds: float) -> str:
                seconds = max(0, int(round(seconds)))
                if seconds < 60:
                    return f"{seconds}s"
                minutes, remaining = divmod(seconds, 60)
                if minutes < 60:
                    return f"{minutes}m {remaining:02d}s"
                hours, minutes = divmod(minutes, 60)
                return f"{hours}h {minutes:02d}m"

            def _progress_update(event: dict[str, Any]) -> None:
                percent = max(
                    0.0, min(100.0, float(event.get("overall_percent") or 0.0))
                )
                total = max(0, int(event.get("total_products") or 0))
                read_count = max(0, int(event.get("products_read") or 0))
                normalized_count = max(0, int(event.get("products_normalized") or 0))
                saved_count = max(0, int(event.get("products_saved") or 0))
                elapsed = float(
                    event.get("elapsed_seconds")
                    or (time.perf_counter() - operation_started)
                )
                rate = float(event.get("products_per_second") or 0.0)
                eta = float(event.get("eta_seconds") or 0.0)
                phase = clean_text(event.get("phase_label")) or phase_labels.get(
                    str(event.get("phase") or ""), "Elaborazione"
                )
                progress_bar.progress(
                    int(round(percent)),
                    text=f"{percent:.1f}% · {phase}",
                )
                progress_status.markdown(
                    f"**Prodotti letti:** {read_count:,} / {total:,}  ·  "
                    f"**Normalizzati:** {normalized_count:,}  ·  "
                    f"**Memorizzati:** {saved_count:,}".replace(",", ".")
                )
                details = [f"Tempo: {_format_duration(elapsed)}"]
                if rate > 0:
                    details.append(
                        f"Velocità: {rate:,.0f} prodotti/s".replace(",", ".")
                    )
                if eta > 0 and percent < 100:
                    details.append(
                        f"Tempo residuo stimato: {_format_duration(eta)}"
                    )
                message = clean_text(event.get("message"))
                if message:
                    details.append(message)
                progress_stats.caption(" · ".join(details))

            try:
                result = persist_saved_view(
                    seller_id=seller_id,
                    supplier_id=int(selected_view["supplier_id"]),
                    price_list_id=int(selected_view["price_list_id"]),
                    saved_view_id=int(selected_view["id"]),
                    snapshot_path=str(path),
                    metadata={
                        "saved_view_name": selected_view["name"],
                        "marketplace_account_id": account_id,
                    },
                    progress_callback=_progress_update,
                    force=force_rebuild,
                    batch_size=1200,
                )
            except Exception as exc:
                progress_status.empty()
                progress_stats.empty()
                st.error(f"Normalizzazione fallita: {exc}")
            else:
                progress_bar.progress(100, text="100.0% · Normalizzazione completata")
                st.session_state[f"catalog_last_source_{seller_id}"] = result["source_snapshot_id"]
                cache_note = " · dati già memorizzati riutilizzati" if result.get("cache_hit") else ""
                st.success(
                    f"{result['normalized_count']:,} prodotti pronti in {result.get('elapsed_seconds', 0):.2f}s "
                    f"({result.get('products_per_second', 0):,.0f} prodotti/s) · completezza media "
                    f"{result['average_completeness']:.2f}% · snapshot {result['source_snapshot_id']}{cache_note}."
                    .replace(",", ".")
                )

    snapshots = source_snapshots(seller_id=seller_id, limit=100)
    if snapshots:
        st.divider()
        snapshot_labels = {
            f"Snapshot {item['id']} · listino {item['price_list_id']} · {item['row_count']} righe · {item['created_at']}": item
            for item in snapshots
        }
        default_snapshot_id = st.session_state.get(f"catalog_last_source_{seller_id}")
        options = list(snapshot_labels)
        default_index = 0
        if default_snapshot_id:
            for index, label in enumerate(options):
                if int(snapshot_labels[label]["id"]) == int(default_snapshot_id):
                    default_index = index
                    break
        source_label = st.selectbox(
            "Snapshot catalogo da utilizzare",
            options,
            index=default_index,
            key=f"catalog_source_snapshot_{seller_id}",
        )
        selected_source = snapshot_labels[source_label]
        source_snapshot_id = int(selected_source["id"])
        st.session_state[f"catalog_selected_source_{seller_id}"] = source_snapshot_id
        products = rows(
            """
            SELECT id,source_row_number,ean,supplier_sku,brand,title,description,
                   normalized_json,completeness_score,status
            FROM canonical_products WHERE source_snapshot_id=?
            ORDER BY source_row_number,id LIMIT 10000
            """,
            (source_snapshot_id,),
        )
        if products:
            st.caption(
                "Il prodotto canonico conserva il feed completo del fornitore: marchio (anche da producer), "
                "descrizione lunga e breve, tutti gli URL immagini, documenti e parametri tecnici."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "ID": item["id"],
                            "Riga": item["source_row_number"],
                            "EAN": item["ean"],
                            "SKU": item["supplier_sku"],
                            "Marca": item["brand"],
                            "Titolo": item["title"],
                            "Descrizione": clean_text(item.get("description"))[:280],
                            "Immagine principale": (
                                list(load_json(item.get("normalized_json"), {}).get("images") or [""])[0]
                                if list(load_json(item.get("normalized_json"), {}).get("images") or []) else ""
                            ),
                            "URL immagini": " | ".join(
                                clean_text(url) for url in list(load_json(item.get("normalized_json"), {}).get("images") or [])[:5]
                            ),
                            "N. immagini": len(
                                list(load_json(item.get("normalized_json"), {}).get("images") or [])
                            ),
                            "Documenti": len(
                                list(load_json(item.get("normalized_json"), {}).get("documents") or [])
                            ),
                            "Completezza %": item["completeness_score"],
                            "Stato": item["status"],
                        }
                        for item in products
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=430,
            )

with classification_tab:
    st.subheader("Determina la categoria più attinente per ciascun prodotto")
    st.write(
        "Il motore legge nome, descrizione, marca, modello, dati tecnici, campi grezzi del fornitore e nomi file "
        "delle immagini. Le categorie candidate vengono lette dal database: nessun nuovo download della tassonomia."
    )
    source_snapshot_id = st.session_state.get(f"catalog_selected_source_{seller_id}")
    available_sources = source_snapshots(seller_id=seller_id, limit=100)
    if available_sources and not source_snapshot_id:
        source_snapshot_id = int(available_sources[0]["id"])
    active_taxonomy = latest_taxonomy_snapshot(
        account_id,
        environment=environment,
        scope_key=scope_key,
    )
    if not source_snapshot_id:
        st.warning("Normalizza prima almeno una vista del listino.")
    elif not active_taxonomy:
        st.warning("Scarica prima la tassonomia ufficiale per questo account/storefront.")
    else:
        product_count_row = rows(
            "SELECT COUNT(*) AS total FROM canonical_products WHERE source_snapshot_id=?",
            (int(source_snapshot_id),),
        )
        total_products = int(product_count_row[0]["total"] if product_count_row else 0)
        options_columns = st.columns(2)
        with options_columns[0]:
            max_products = st.number_input(
                "Numero massimo di prodotti da classificare",
                min_value=1,
                max_value=max(1, total_products),
                value=min(500, max(1, total_products)),
                step=50,
                key=f"catalog_classification_limit_{source_snapshot_id}_{account_id}",
            )
        with options_columns[1]:
            use_official = marketplace == "kaufland"
            if use_official:
                st.info(
                    "Kaufland: la categoria viene determinata automaticamente con "
                    "POST /categories/decide usando titolo, descrizione, marca, categoria "
                    "fornitore e specifiche del feed completo. La tassonomia locale resta "
                    "la verifica ufficiale della categoria restituita."
                )
            else:
                st.caption(
                    "Worten/Mirakl usa la tassonomia del tenant memorizzata nel database. Dopo la classificazione "
                    "deterministica puoi usare l'AI vincolata per scegliere esclusivamente tra le candidate ufficiali."
                )
        if st.button(
            "Determina categorie dal feed completo",
            type="primary",
            key=f"catalog_classify_{account_id}_{source_snapshot_id}_{active_taxonomy['id']}",
        ):
            bar = st.progress(0, text="Avvio classificazione...")

            def classification_progress(current: int, total: int, title: str) -> None:
                bar.progress(
                    min(1.0, current / max(1, total)),
                    text=f"{current}/{total} · {title[:80]}",
                )

            try:
                result = classify_source_products(
                    seller_id=seller_id,
                    account_id=account_id,
                    taxonomy_snapshot=active_taxonomy,
                    source_snapshot_id=int(source_snapshot_id),
                    environment=environment,
                    use_official_kaufland_suggestions=bool(use_official),
                    limit=int(max_products),
                    progress=classification_progress,
                )
            except Exception as exc:
                bar.empty()
                st.error(f"Classificazione fallita: {exc}")
            else:
                bar.progress(1.0, text="Categorie determinate e memorizzate.")
                st.session_state[f"catalog_classification_run_{account_id}"] = result["run_id"]
                st.success(
                    f"{result['product_count']} prodotti · {result['auto_approved']} automatici · "
                    f"{result['review']} da verificare · {result['blocked']} senza categoria tecnica."
                )
                st.rerun()

        assignments = category_assignments_for_source(
            seller_id=seller_id,
            account_id=account_id,
            source_snapshot_id=int(source_snapshot_id),
            limit=10000,
        )
        if assignments:
            metrics = st.columns(4)
            metrics[0].metric("Classificati", len(assignments))
            metrics[1].metric(
                "Approvati",
                sum(1 for item in assignments if item["status"] in {"AUTO_APPROVED", "APPROVED"}),
            )
            metrics[2].metric("Revisione", sum(1 for item in assignments if item["status"] == "REVIEW"))
            metrics[3].metric("Senza categoria", sum(1 for item in assignments if not item["category_external_id"]))
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Prodotto ID": item["canonical_product_id"],
                            "EAN": item["ean"],
                            "SKU": item["supplier_sku"],
                            "Titolo": item["title"],
                            "Categoria": item["category_path"] or item["category_label"],
                            "ID categoria": item["category_external_id"],
                            "Fonte": item["decision_source"],
                            "Confidence %": item["confidence"],
                            "Stato": item["status"],
                        }
                        for item in assignments
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=460,
            )

            review_count_now = sum(1 for item in assignments if item["status"] == "REVIEW")
            with st.expander("AI Catalog Intelligence · risolvi categorie in revisione", expanded=False):
                st.write(
                    "L'AI riceve il prodotto e soltanto le categorie candidate già presenti nella tassonomia ufficiale. "
                    "Una categoria inventata o fuori elenco viene scartata automaticamente. Ogni scelta deve citare "
                    "almeno un campo reale del feed fornitore."
                )
                if not ai_profiles:
                    st.info(
                        "Non ci sono profili IA attivi. Apri **Provider IA** nel menu, salva la tua API key e verifica la connessione."
                    )
                else:
                    ai_category_cols = st.columns(3)
                    with ai_category_cols[0]:
                        ai_category_profile_id = st.selectbox(
                            "Provider IA",
                            list(ai_profile_by_id),
                            format_func=_ai_profile_label,
                            key=f"catalog_ai_category_profile_{account_id}_{source_snapshot_id}",
                        )
                    with ai_category_cols[1]:
                        ai_category_confidence = st.number_input(
                            "Soglia auto-approvazione", min_value=0.50, max_value=0.99, value=0.86, step=0.01,
                            key=f"catalog_ai_category_confidence_{account_id}_{source_snapshot_id}",
                        )
                    with ai_category_cols[2]:
                        ai_category_limit = st.number_input(
                            "Max prodotti IA", min_value=1, max_value=max(1, review_count_now),
                            value=min(100, max(1, review_count_now)), step=10,
                            key=f"catalog_ai_category_limit_{account_id}_{source_snapshot_id}",
                        )
                    if st.button(
                        "Risolvi categorie in revisione con AI vincolata",
                        type="primary",
                        disabled=review_count_now == 0,
                        key=f"catalog_ai_category_run_{account_id}_{source_snapshot_id}_{active_taxonomy['id']}",
                    ):
                        ai_bar = st.progress(0, text="Avvio AI Catalog Intelligence...")

                        def ai_category_progress(current: int, total: int, title: str) -> None:
                            ai_bar.progress(
                                min(1.0, current / max(1, total)),
                                text=f"AI categorie {current}/{total} · {title[:80]}",
                            )

                        try:
                            ai_result = resolve_review_categories_with_ai(
                                seller_id=seller_id,
                                account_id=account_id,
                                taxonomy_snapshot=active_taxonomy,
                                source_snapshot_id=int(source_snapshot_id),
                                ai_profile_id=int(ai_category_profile_id),
                                minimum_confidence=float(ai_category_confidence),
                                limit=int(ai_category_limit),
                                progress=ai_category_progress,
                            )
                        except Exception as exc:
                            ai_bar.empty()
                            st.error(f"Classificazione AI non completata: {exc}")
                        else:
                            ai_bar.progress(1.0, text="Classificazione AI completata e memorizzata.")
                            st.success(
                                f"{ai_result['product_count']} analizzati · {ai_result['auto_approved']} auto-approvati · "
                                f"{ai_result['review']} ancora da verificare · {ai_result['failed']} errori provider."
                            )
                            st.rerun()

            review_items = [item for item in assignments if item["status"] in {"REVIEW", "BLOCKED"}]  # BLOCKED: compatibilità storico v245-v247
            if review_items:
                st.divider()
                st.subheader("Revisione manuale della categoria")
                review_labels = {
                    f"ID {item['canonical_product_id']} · {item['ean'] or item['supplier_sku']} · {item['title'][:90]}": item
                    for item in review_items
                }
                review_label = st.selectbox(
                    "Prodotto da correggere",
                    list(review_labels),
                    key=f"catalog_review_product_{account_id}_{source_snapshot_id}",
                )
                review_item = review_labels[review_label]
                category_search = st.text_input(
                    "Cerca categoria foglia ufficiale",
                    key=f"catalog_review_category_search_{account_id}_{review_item['canonical_product_id']}",
                )
                manual_categories = taxonomy_categories(
                    int(active_taxonomy["id"]),
                    leaf_only=True,
                    search=category_search,
                    limit=1000,
                )
                if manual_categories:
                    category_labels = {
                        f"{item['path'] or item['label']} · ID {item['external_id']}": item
                        for item in manual_categories
                    }
                    selected_category_label = st.selectbox(
                        "Categoria corretta",
                        list(category_labels),
                        key=f"catalog_manual_category_{account_id}_{review_item['canonical_product_id']}",
                    )
                    remember_mapping = st.checkbox(
                        "Memorizza il mapping per la stessa famiglia/categoria del fornitore",
                        value=True,
                        key=f"catalog_remember_mapping_{review_item['canonical_product_id']}",
                    )
                    if st.button(
                        "Approva categoria per questo prodotto",
                        type="primary",
                        key=f"catalog_approve_category_{review_item['canonical_product_id']}",
                    ):
                        try:
                            approve_category_assignment(
                                seller_id=seller_id,
                                account_id=account_id,
                                taxonomy_snapshot=active_taxonomy,
                                source_snapshot_id=int(source_snapshot_id),
                                product_id=int(review_item["canonical_product_id"]),
                                category_external_id=category_labels[selected_category_label]["external_id"],
                                approved_by="seller",
                                create_mapping_rule=bool(remember_mapping),
                            )
                        except Exception as exc:
                            st.error(f"Categoria non salvata: {exc}")
                        else:
                            st.success("Categoria approvata e memorizzata.")
                            st.rerun()

with feed_tab:
    st.subheader("Prepara schede prodotto complete")
    st.write(
        "Per ogni categoria assegnata vengono letti gli attributi ufficiali. Su Kaufland il dettaglio categoria "
        "viene scaricato soltanto al primo utilizzo e poi memorizzato; su Worten gli attributi PM11 sono già nello snapshot."
    )
    st.caption(
        "La precedente azione 'Valida prodotti selezionati' è ora integrata nella preparazione del feed: "
        "ogni payload viene validato prima di essere memorizzato."
    )
    source_snapshot_id = st.session_state.get(f"catalog_selected_source_{seller_id}")
    active_taxonomy = latest_taxonomy_snapshot(
        account_id,
        environment=environment,
        scope_key=scope_key,
    )
    if not source_snapshot_id:
        st.warning("Seleziona prima uno snapshot del catalogo sorgente.")
    elif not active_taxonomy:
        st.warning("Scarica prima la tassonomia ufficiale.")
    else:
        assignments = category_assignments_for_source(
            seller_id=seller_id,
            account_id=account_id,
            source_snapshot_id=int(source_snapshot_id),
            limit=10000,
        )
        approved_count = sum(1 for item in assignments if item["status"] in {"AUTO_APPROVED", "APPROVED"})
        review_count = sum(1 for item in assignments if item["status"] == "REVIEW")
        st.caption(f"Categorie approvate: {approved_count} · Da revisionare: {review_count}")
        feed_columns = st.columns(2)
        with feed_columns[0]:
            max_feed_products = st.number_input(
                "Numero massimo di schede da preparare",
                min_value=1,
                max_value=max(1, len(assignments)),
                value=min(500, max(1, approved_count or len(assignments))),
                step=50,
                key=f"catalog_feed_limit_{account_id}_{source_snapshot_id}",
            )
        with feed_columns[1]:
            include_review = st.checkbox(
                "Includi anche categorie in revisione",
                value=False,
                key=f"catalog_feed_include_review_{account_id}_{source_snapshot_id}",
            )

        selected_ai_feed_profile_id: int | None = None
        ai_feed_confidence = 0.72
        with st.expander("AI Catalog Intelligence · completa attributi obbligatori mancanti", expanded=False):
            st.write(
                "L'AI viene chiamata solo per attributi ufficiali obbligatori che il mapping deterministico non ha trovato. "
                "Ogni valore deve indicare il campo sorgente da cui deriva; senza prova reale il mapping viene scartato. "
                "I mapping accettati restano nel database e non consumano nuovi token alle aperture successive."
            )
            if not ai_profiles:
                st.info("Configura prima almeno un profilo attivo nella pagina **Provider IA**.")
            else:
                ai_feed_options = [0] + list(ai_profile_by_id)
                selected_feed_value = st.selectbox(
                    "Provider per gli attributi",
                    ai_feed_options,
                    format_func=lambda value: "Disattivato · solo mapping deterministico" if value == 0 else _ai_profile_label(value),
                    key=f"catalog_ai_feed_profile_{account_id}_{source_snapshot_id}",
                )
                selected_ai_feed_profile_id = int(selected_feed_value) if int(selected_feed_value) else None
                ai_feed_confidence = st.number_input(
                    "Soglia minima mapping attributo", min_value=0.50, max_value=0.99, value=0.72, step=0.01,
                    key=f"catalog_ai_feed_confidence_{account_id}_{source_snapshot_id}",
                )
                st.caption(
                    "Il Validator del marketplace resta l'ultima autorità: tipo dato, unità, regex, limiti e allowed values "
                    "vengono controllati dopo la proposta AI."
                )

        if st.button(
            "Prepara feed prodotti completi",
            type="primary",
            disabled=not assignments,
            key=f"catalog_prepare_feed_{account_id}_{source_snapshot_id}_{active_taxonomy['id']}",
        ):
            bar = st.progress(0, text="Avvio preparazione feed...")

            def feed_progress(current: int, total: int, title: str) -> None:
                bar.progress(
                    min(1.0, current / max(1, total)),
                    text=f"{current}/{total} · {title[:80]}",
                )

            try:
                result = prepare_assigned_product_feeds(
                    seller_id=seller_id,
                    account_id=account_id,
                    taxonomy_snapshot=active_taxonomy,
                    source_snapshot_id=int(source_snapshot_id),
                    environment=environment,
                    include_review=bool(include_review),
                    limit=int(max_feed_products),
                    progress=feed_progress,
                    ai_profile_id=selected_ai_feed_profile_id,
                    ai_minimum_confidence=float(ai_feed_confidence),
                )
            except Exception as exc:
                bar.empty()
                st.error(f"Preparazione feed fallita: {exc}")
            else:
                bar.progress(1.0, text="Feed preparati e memorizzati.")
                st.session_state[f"catalog_feed_validation_run_{account_id}"] = result["validation_run_id"]
                ai_note = (
                    f" · AI: {result.get('ai_mapped_products', 0)} prodotti arricchiti, "
                    f"{result.get('ai_failed_products', 0)} errori"
                    if selected_ai_feed_profile_id else ""
                )
                st.success(
                    f"{result['product_count']} schede · {result['ready']} pronte · "
                    f"{result['warnings']} con avvisi · {result['blocked']} bloccate{ai_note}."
                )
                st.rerun()

        preparations = feed_preparations_for_source(
            seller_id=seller_id,
            account_id=account_id,
            source_snapshot_id=int(source_snapshot_id),
            limit=10000,
        )
        if preparations:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Preparazione ID": item["id"],
                            "Prodotto ID": item["canonical_product_id"],
                            "EAN": item["ean"],
                            "SKU": item["supplier_sku"],
                            "Titolo": item["title"],
                            "Categoria": item["category_external_id"],
                            "Stato": item["validation_status"],
                            "Readiness %": item["readiness_score"],
                            "Aggiornata": item["updated_at"],
                        }
                        for item in preparations
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=430,
            )
            preparation_labels = {
                f"ID {item['id']} · {item['ean'] or item['supplier_sku']} · {item['title'][:80]}": item
                for item in preparations
            }
            selected_preparation_label = st.selectbox(
                "Anteprima tecnica del feed",
                list(preparation_labels),
                key=f"catalog_feed_preview_{account_id}_{source_snapshot_id}",
            )
            selected_preparation = preparation_labels[selected_preparation_label]
            preview_columns = st.columns(2)
            with preview_columns[0]:
                st.markdown("**Payload prodotto**")
                st.json(load_json(selected_preparation.get("product_payload_json"), {}))
            with preview_columns[1]:
                st.markdown("**Payload offerta preparatorio**")
                st.json(load_json(selected_preparation.get("offer_payload_json"), {}))
            issues = load_json(selected_preparation.get("issues_json"), [])
            if issues:
                st.markdown("**Controlli e problemi**")
                st.dataframe(pd.DataFrame(issues), use_container_width=True, hide_index=True)

            job_candidates = [
                item
                for item in preparations
                if item["validation_status"] in {"READY", "VALID_WITH_WARNINGS"}
            ]
            if job_candidates:
                st.info(
                    "Il job salva payload e stato nel database. La scheda Pubblicazione esegue prima il controllo "
                    "anti-duplicato EAN e consente simulazione o invio reale esplicito."
                )
                if st.button(
                    "Crea job persistente dalle schede valide",
                    type="primary",
                    key=f"catalog_create_prepared_job_{account_id}_{source_snapshot_id}",
                ):
                    source_record = source_snapshot(int(source_snapshot_id))
                    if not source_record:
                        st.error("Snapshot sorgente non più disponibile.")
                    else:
                        product_ids = [int(item["canonical_product_id"]) for item in job_candidates]
                        job_id = create_publication_job(
                            seller_id=seller_id,
                            account_id=account_id,
                            marketplace=marketplace,
                            environment=environment,
                            storefront=storefront,
                            locale=locale,
                            price_list_id=int(source_record["price_list_id"]),
                            source_snapshot_id=int(source_snapshot_id),
                            taxonomy_snapshot_id=int(active_taxonomy["id"]),
                            settings={
                                "validation_run_id": st.session_state.get(
                                    f"catalog_feed_validation_run_{account_id}"
                                ),
                                "classification_mode": "NEW_PRODUCT_FROM_CACHED_TAXONOMY",
                                "publication_mode": "SIMULATION",
                                "remote_write_enabled": False,
                                "update_existing_product_data": False,
                                "update_existing_offers": False,
                            },
                            product_ids=product_ids,
                        )
                        apply_feed_preparations_to_job(job_id, job_candidates)
                        st.success(f"Job {job_id} creato. Apri la scheda Pubblicazione per pianificarlo.")
                        st.rerun()


with publication_tab:
    st.subheader("Pubblicazione reale controllata")
    st.write(
        "Il motore esegue prima il controllo anti-duplicato tramite EAN, separa dati prodotto e offerta, "
        "memorizza ogni passaggio e riprende il job senza reinviare operazioni già concluse."
    )
    jobs = publication_jobs(seller_id, account_id=account_id, limit=200)
    if not jobs:
        st.warning("Crea prima un job dalle schede valide nella sezione Prepara feed.")
    else:
        job_labels = {
            f"Job {item['id']} · {item['status']} · {item['total_items']} prodotti · {item['created_at']}": item
            for item in jobs
        }
        selected_job_label = st.selectbox(
            "Job da lavorare",
            list(job_labels),
            key=f"catalog_publication_job_{account_id}",
        )
        selected_job = job_labels[selected_job_label]
        selected_job_id = int(selected_job["id"])
        job_items = publication_items(selected_job_id, limit=100000)

        metric_columns = st.columns(6)
        metric_columns[0].metric("Totale", len(job_items))
        metric_columns[1].metric(
            "Da pianificare",
            sum(1 for item in job_items if str(item.get("duplicate_check_status") or "").upper() != "COMPLETED"),
        )
        metric_columns[2].metric(
            "Prodotti in lavorazione",
            sum(1 for item in job_items if str(item.get("product_status") or "").upper() in {"SUBMITTED", "PENDING", "IN_PROGRESS", "PROCESSING"}),
        )
        metric_columns[3].metric(
            "Offerte da creare",
            sum(1 for item in job_items if str(item.get("next_action") or "").upper() in {"CREATE_OFFER", "UPDATE_OFFER"}),
        )
        metric_columns[4].metric(
            "Completati",
            sum(1 for item in job_items if str(item.get("status") or "").upper() in {"COMPLETED", "EXISTING_OFFER"}),
        )
        metric_columns[5].metric(
            "Errori/revisione",
            sum(1 for item in job_items if str(item.get("status") or "").upper() in {"FAILED", "BLOCKED", "PRODUCT_REJECTED", "OFFER_REJECTED", "REVIEW_REQUIRED"}),
        )

        if job_items:
            item_frame = pd.DataFrame(
                [
                    {
                        "Seleziona": False,
                        "Item ID": item["id"],
                        "EAN": item["ean"],
                        "SKU": item.get("seller_sku") or item["supplier_sku"],
                        "Titolo": item["title"],
                        "Stato": item["status"],
                        "Prodotto": item["product_status"],
                        "Offerta": item["offer_status"],
                        "Azione prevista": item.get("planned_action") or "",
                        "Prossimo passo": item.get("next_action") or "",
                        "Tentativi": item["attempt_count"],
                        "Errore": item["last_error"],
                    }
                    for item in job_items
                ]
            )
            edited_items = st.data_editor(
                item_frame,
                use_container_width=True,
                hide_index=True,
                height=450,
                disabled=[
                    "Item ID", "EAN", "SKU", "Titolo", "Stato", "Prodotto", "Offerta",
                    "Azione prevista", "Prossimo passo", "Tentativi", "Errore",
                ],
                column_config={
                    "Seleziona": st.column_config.CheckboxColumn("Seleziona"),
                },
                key=f"catalog_publication_items_{selected_job_id}",
            )
            selected_item_ids = [
                int(value)
                for value in edited_items.loc[edited_items["Seleziona"] == True, "Item ID"].tolist()  # noqa: E712
            ]
        else:
            selected_item_ids = []
            st.caption("Il job non contiene prodotti.")

        scope_caption = (
            f"{len(selected_item_ids)} prodotti selezionati"
            if selected_item_ids
            else "Nessuna selezione: le azioni lavorano tutti i prodotti compatibili del job"
        )
        st.caption(scope_caption)

        st.divider()
        mode = st.radio(
            "Modalità",
            ("SIMULATION", "REAL"),
            format_func=lambda value: "Simulazione — nessuna scrittura remota" if value == "SIMULATION" else "Reale — crea/aggiorna sul marketplace",
            horizontal=True,
            key=f"catalog_publication_mode_{selected_job_id}",
        )
        real_write_enabled = False
        if mode == "REAL":
            st.warning(
                "Modalità reale attiva. Il motore non duplica le operazioni concluse e crea l'offerta soltanto "
                "dopo che il prodotto risulta esistente o accettato."
            )
            real_write_enabled = st.checkbox(
                "Abilita la scrittura reale per questo ciclo",
                value=False,
                key=f"catalog_enable_remote_write_{selected_job_id}",
                help="Sblocca esclusivamente le chiamate di creazione/aggiornamento. I controlli e la simulazione restano sempre disponibili.",
            )

        settings_key = f"catalog_publication_options_{selected_job_id}"
        if st.button(
            "Carica configurazioni offerte dal marketplace",
            key=f"catalog_load_publication_options_{selected_job_id}",
        ):
            with st.spinner("Lettura configurazioni in corso..."):
                try:
                    st.session_state[settings_key] = publication_configuration_options(selected_job_id)
                except Exception as exc:
                    st.error(f"Configurazioni non caricate: {exc}")
                else:
                    st.success("Configurazioni caricate.")
                    st.rerun()
        publication_options = st.session_state.get(settings_key) or {}

        publication_settings: dict[str, Any] = {
            "remote_write_enabled": bool(real_write_enabled),
            "publication_mode": mode,
            "update_existing_product_data": st.checkbox(
                "Aggiorna anche i dati prodotto quando l'EAN esiste già",
                value=False,
                key=f"catalog_update_existing_product_{selected_job_id}",
            ),
            "update_existing_offers": st.checkbox(
                "Aggiorna anche le offerte già esistenti",
                value=False,
                key=f"catalog_update_existing_offer_{selected_job_id}",
            ),
        }
        allow_review_items = st.checkbox(
            "Includi prodotti validi con avvisi/revisione già accettata",
            value=False,
            key=f"catalog_allow_review_{selected_job_id}",
        )
        max_items_cycle = st.number_input(
            "Massimo prodotti per ciclo",
            min_value=1,
            max_value=max(1, len(job_items)),
            value=min(100, max(1, len(job_items))),
            step=10,
            key=f"catalog_publication_batch_{selected_job_id}",
        )

        if marketplace == "kaufland":
            warehouses = list(publication_options.get("warehouses") or [])
            groups = list(publication_options.get("shipping_groups") or [])
            vat_values = list(publication_options.get("vat_indicators") or [])
            option_columns = st.columns(4)
            with option_columns[0]:
                if warehouses:
                    warehouse_map = {
                        f"{item.get('name') or item.get('label') or 'Magazzino'} · {item.get('id_warehouse') or item.get('id')}": str(item.get("id_warehouse") or item.get("id"))
                        for item in warehouses if item.get("id_warehouse") or item.get("id")
                    }
                    publication_settings["id_warehouse"] = warehouse_map[st.selectbox(
                        "Magazzino", list(warehouse_map), key=f"catalog_pub_warehouse_{selected_job_id}"
                    )] if warehouse_map else ""
                else:
                    publication_settings["id_warehouse"] = st.text_input(
                        "ID magazzino", key=f"catalog_pub_warehouse_manual_{selected_job_id}"
                    ).strip()
            with option_columns[1]:
                if groups:
                    group_map = {
                        f"{item.get('name') or item.get('label') or 'Gruppo'} · {item.get('id_shipping_group') or item.get('id')}": str(item.get("id_shipping_group") or item.get("id"))
                        for item in groups if item.get("id_shipping_group") or item.get("id")
                    }
                    publication_settings["id_shipping_group"] = group_map[st.selectbox(
                        "Gruppo spedizione", list(group_map), key=f"catalog_pub_group_{selected_job_id}"
                    )] if group_map else ""
                else:
                    publication_settings["id_shipping_group"] = st.text_input(
                        "ID gruppo spedizione", key=f"catalog_pub_group_manual_{selected_job_id}"
                    ).strip()
            with option_columns[2]:
                publication_settings["handling_time"] = st.number_input(
                    "Handling time", min_value=0, max_value=30, value=2,
                    key=f"catalog_pub_handling_{selected_job_id}",
                )
            with option_columns[3]:
                vat_codes = []
                for item in vat_values:
                    if isinstance(item, dict):
                        code = str(item.get("vat_indicator") or item.get("code") or item.get("id") or "").strip()
                    else:
                        code = str(item or "").strip()
                    if code and code not in vat_codes:
                        vat_codes.append(code)
                publication_settings["vat_indicator"] = st.selectbox(
                    "Indicatore IVA",
                    vat_codes or ["standard_rate"],
                    key=f"catalog_pub_vat_{selected_job_id}",
                )
            publication_settings["manufacturer_guarantee_years"] = st.number_input(
                "Garanzia produttore in anni (0 = non inviare)",
                min_value=0, max_value=99, value=0,
                key=f"catalog_pub_guarantee_{selected_job_id}",
            )
        else:
            logistic_raw = publication_options.get("logistic_classes") or {}
            state_raw = publication_options.get("offer_states") or {}

            def _records(payload, *keys):
                if isinstance(payload, list):
                    return payload
                if isinstance(payload, dict):
                    for key in keys:
                        if isinstance(payload.get(key), list):
                            return payload[key]
                return []

            logistic_rows = _records(logistic_raw, "logistic_classes", "classes", "data")
            state_rows = _records(state_raw, "offer_states", "states", "data")
            logistic_codes = [
                str(item.get("code") or item.get("id") or "").strip()
                for item in logistic_rows if isinstance(item, dict) and (item.get("code") or item.get("id"))
            ]
            state_codes = [
                str(item.get("code") or item.get("state_code") or item.get("id") or "").strip()
                for item in state_rows if isinstance(item, dict) and (item.get("code") or item.get("state_code") or item.get("id"))
            ]
            mirakl_columns = st.columns(4)
            with mirakl_columns[0]:
                publication_settings["logistic_class"] = st.selectbox(
                    "Classe logistica", logistic_codes or [""],
                    key=f"catalog_pub_logistic_{selected_job_id}",
                )
            with mirakl_columns[1]:
                publication_settings["offer_state"] = st.selectbox(
                    "Stato offerta", state_codes or ["11"],
                    key=f"catalog_pub_state_{selected_job_id}",
                )
            with mirakl_columns[2]:
                publication_settings["leadtime_to_ship"] = st.number_input(
                    "Lead time", min_value=0, max_value=60, value=2,
                    key=f"catalog_pub_leadtime_{selected_job_id}",
                )
            with mirakl_columns[3]:
                publication_settings["ship_from_country"] = st.text_input(
                    "Paese spedizione ISO-2", value="IT",
                    key=f"catalog_pub_ship_country_{selected_job_id}",
                ).strip().upper()
            publication_settings["product_import_mode"] = "NORMAL"
            publication_settings["offer_import_mode"] = "NORMAL"

        st.divider()
        action_columns = st.columns(4)
        with action_columns[0]:
            if st.button(
                "1. Controlla duplicati e pianifica",
                type="primary",
                key=f"catalog_plan_job_{selected_job_id}",
            ):
                with st.spinner("Controllo EAN e offerte esistenti..."):
                    try:
                        result = plan_publication_job(
                            selected_job_id,
                            item_ids=selected_item_ids or None,
                            settings=publication_settings,
                            allow_review_items=allow_review_items,
                        )
                    except Exception as exc:
                        st.error(f"Pianificazione fallita: {exc}")
                    else:
                        st.success(
                            f"Pianificazione completata: {result.succeeded} riusciti, "
                            f"{result.failed} errori, {result.skipped} saltati."
                        )
                        st.rerun()
        with action_columns[1]:
            if st.button(
                "2. Simula il job",
                key=f"catalog_simulate_job_{selected_job_id}",
            ):
                try:
                    result = simulate_publication_job(
                        selected_job_id, item_ids=selected_item_ids or None
                    )
                except Exception as exc:
                    st.error(f"Simulazione fallita: {exc}")
                else:
                    st.success(f"Simulazione completata per {result.succeeded} prodotti.")
                    st.rerun()
        with action_columns[2]:
            if st.button(
                "3. Esegui un ciclo",
                type="primary" if mode == "REAL" else "secondary",
                key=f"catalog_run_cycle_{selected_job_id}",
                disabled=mode == "REAL" and not real_write_enabled,
            ):
                with st.spinner("Esecuzione ciclo persistente..."):
                    try:
                        output = run_publication_cycle(
                            selected_job_id,
                            mode=mode,
                            item_ids=selected_item_ids or None,
                            settings=publication_settings,
                            max_items=int(max_items_cycle),
                            allow_review_items=allow_review_items,
                        )
                    except Exception as exc:
                        st.error(f"Ciclo non completato: {exc}")
                    else:
                        st.success(f"Ciclo {mode} completato.")
                        st.json(output)
                        st.rerun()
        with action_columns[3]:
            if st.button(
                "Riprova errori temporanei",
                key=f"catalog_retry_job_{selected_job_id}",
            ):
                count = retry_failed_items(
                    selected_job_id, item_ids=selected_item_ids or None
                )
                st.success(f"{count} prodotti rimessi in coda.")
                st.rerun()

        advanced = st.expander("Azioni avanzate per fase")
        with advanced:
            advanced_columns = st.columns(4)
            if advanced_columns[0].button(
                "Invia product data",
                key=f"catalog_submit_products_{selected_job_id}",
                disabled=mode != "REAL" or not real_write_enabled,
            ):
                try:
                    result = submit_products(
                        selected_job_id, item_ids=selected_item_ids or None,
                        settings=publication_settings, max_items=int(max_items_cycle),
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.success(result.as_dict())
                    st.rerun()
            if advanced_columns[1].button("Aggiorna stato prodotti", key=f"catalog_refresh_products_{selected_job_id}"):
                try:
                    result = refresh_products(selected_job_id, item_ids=selected_item_ids or None)
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.success(result.as_dict())
                    st.rerun()
            if advanced_columns[2].button(
                "Invia offerte",
                key=f"catalog_submit_offers_{selected_job_id}",
                disabled=mode != "REAL" or not real_write_enabled,
            ):
                try:
                    result = submit_offers(
                        selected_job_id, item_ids=selected_item_ids or None,
                        settings=publication_settings, max_items=int(max_items_cycle),
                    )
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.success(result.as_dict())
                    st.rerun()
            if advanced_columns[3].button("Aggiorna stato offerte", key=f"catalog_refresh_offers_{selected_job_id}"):
                try:
                    result = refresh_offers(selected_job_id, item_ids=selected_item_ids or None)
                except Exception as exc:
                    st.error(str(exc))
                else:
                    st.success(result.as_dict())
                    st.rerun()

        artifacts = publication_artifacts(selected_job_id, limit=100)
        if artifacts:
            st.divider()
            st.subheader("File e payload generati")
            for artifact in artifacts:
                path = Path(str(artifact.get("local_path") or ""))
                columns = st.columns([4, 1])
                columns[0].write(
                    f"{artifact['artifact_type']} · {artifact['filename']} · {artifact['row_count']} righe"
                )
                if path.is_file() or clean_text(artifact.get("storage_key")):
                    try:
                        artifact_payload=publication_artifact_bytes(artifact)
                    except Exception as exc:
                        columns[1].caption(f"Storage: {exc}")
                    else:
                        columns[1].download_button(
                            "Scarica",
                            data=artifact_payload,
                            file_name=artifact["filename"],
                            mime="text/csv" if str(artifact.get("filename") or "").lower().endswith(".csv") else "application/octet-stream",
                            key=f"catalog_download_artifact_{artifact['id']}",
                        )

        imports = marketplace_imports_for_job(selected_job_id, limit=100)
        if imports:
            st.divider()
            st.subheader("Import marketplace")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "ID locale": item["id"],
                            "Tipo": item["import_type"],
                            "Import ID": item["external_import_id"],
                            "Stato": item["status"],
                            "Report errori": "Sì" if item["has_error_report"] else "No",
                            "Report successi": "Sì" if item["has_success_report"] else "No",
                            "Errore": item["error"],
                            "Aggiornato": item["updated_at"],
                        }
                        for item in imports
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

        events = publication_events(selected_job_id, limit=200)
        if events:
            st.divider()
            st.subheader("Registro operativo")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Data": item["created_at"],
                            "Item": item["publication_item_id"],
                            "Evento": item["event_type"],
                            "Stato": item["status"],
                            "Messaggio": item["message"],
                        }
                        for item in events
                    ]
                ),
                use_container_width=True,
                hide_index=True,
                height=350,
            )

with history_tab:
    st.subheader("Sincronizzazioni tassonomia")
    sync_rows = taxonomy_sync_runs(account_id, environment=environment, limit=100)
    if sync_rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": item["id"],
                        "Stato": item["status"],
                        "Scope": item["scope_key"],
                        "Categorie": item["category_count"],
                        "Attributi": item["attribute_count"],
                        "Valori": item["value_count"],
                        "Avvio": item["started_at"],
                        "Fine": item["completed_at"],
                        "Errore": item["error"],
                    }
                    for item in sync_rows
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nessuna sincronizzazione eseguita.")

    st.subheader("Classificazioni")
    classification_rows = category_classification_runs(
        seller_id=seller_id,
        account_id=account_id,
        limit=100,
    )
    if classification_rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": item["id"],
                        "Stato": item["status"],
                        "Prodotti": item["product_count"],
                        "Automatici": item["classified_count"],
                        "Revisione": item["review_count"],
                        "Bloccati": item["blocked_count"],
                        "Avvio": item["started_at"],
                        "Fine": item["completed_at"],
                    }
                    for item in classification_rows
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nessuna classificazione eseguita.")

    st.subheader("Esecuzioni AI Catalog Intelligence")
    catalog_ai_rows = ai_runs(seller_id=seller_id, account_id=account_id, limit=100)
    if catalog_ai_rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": item["id"],
                        "Scopo": item["purpose"],
                        "Stato": item["status"],
                        "Prodotti": item["product_count"],
                        "Accettati": item["success_count"],
                        "Revisione": item["review_count"],
                        "Errori": item["failed_count"],
                        "Profilo IA": item["ai_profile_id"],
                        "Avvio": item["started_at"],
                        "Fine": item["completed_at"],
                        "Errore": item["error"],
                    }
                    for item in catalog_ai_rows
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nessuna esecuzione AI Catalog Intelligence ancora memorizzata.")

    st.subheader("Job Creazione Prodotti")
    jobs = publication_jobs(seller_id, account_id=account_id, limit=100)
    if jobs:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": item["id"],
                        "Stato": item["status"],
                        "Marketplace": item["marketplace"],
                        "Storefront": item["storefront"],
                        "Totale": item["total_items"],
                        "Pronti": item["ready_items"],
                        "Riusciti": item["success_items"],
                        "Falliti": item["failed_items"],
                        "Revisione": item["review_items"],
                        "Creato": item["created_at"],
                    }
                    for item in jobs
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("Nessun job ancora creato.")
