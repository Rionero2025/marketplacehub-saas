from __future__ import annotations

import hashlib
from datetime import datetime

import streamlit as st

from services.data_transfer import (
    TransferError,
    create_transfer_package,
    inspect_transfer_package,
    restore_transfer_package,
)
from services.database_config import database_engine
from services.session import bootstrap

bootstrap()


def _human_bytes(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


st.title("Backup e trasferimento")
st.caption(
    "Esporta in un unico file i dati già memorizzati e importali su un'altra installazione di Marketplace Hub."
)

st.info(
    "Il backup trasferisce database, Seller, account marketplace, ordini, storico, Contabilità, viste, listini attivi, "
    "configurazioni e credenziali. Dalla v261 non vengono più trascinati nel backup i vecchi download/listini duplicati "
    "e i file orfani non più usati dal database. Il file è cifrato con la password scelta qui."
)

export_tab, import_tab = st.tabs(["Esporta dati", "Importa su questo PC"])

with export_tab:
    st.subheader("Crea backup compatto completo")
    st.write(
        "Usa questa funzione sul PC attuale. Poi copia il file `.mhubbackup` sul nuovo PC, installa una versione "
        "uguale o più recente di Marketplace Hub e usa la scheda **Importa su questo PC**."
    )
    e1, e2 = st.columns(2)
    export_password = e1.text_input(
        "Password backup",
        type="password",
        key="transfer_export_password",
        help="Almeno 8 caratteri. Ti servirà sul nuovo PC.",
    )
    export_password_repeat = e2.text_input(
        "Ripeti password",
        type="password",
        key="transfer_export_password_repeat",
    )
    st.caption(f"Database attivo: {database_engine().upper()}")

    if st.button("Prepara backup compatto", type="primary", use_container_width=True):
        if len(export_password) < 8:
            st.error("Inserisci una password di almeno 8 caratteri.")
        elif export_password != export_password_repeat:
            st.error("Le due password non coincidono.")
        else:
            try:
                with st.spinner("Preparazione e cifratura del backup in corso..."):
                    package, manifest = create_transfer_package(export_password)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                st.session_state["transfer_export_ready"] = {
                    "bytes": package,
                    "manifest": manifest,
                    "filename": f"marketplace_hub_backup_{timestamp}.mhubbackup",
                }
            except TransferError as error:
                st.error(str(error))
            except Exception as error:
                st.error(f"Impossibile creare il backup: {error}")

    ready = st.session_state.get("transfer_export_ready")
    if ready:
        manifest = ready["manifest"]
        summary = manifest.get("database_summary") or {}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Seller", int(summary.get("sellers", 0)))
        c2.metric("Account marketplace", int(summary.get("marketplace_accounts", 0)))
        c3.metric("Listini", int(summary.get("price_lists", 0)))
        c4.metric("Dimensione backup", _human_bytes(len(ready["bytes"])))
        compaction = manifest.get("compaction") or {}
        excluded_files = int(compaction.get("excluded_orphan_files") or 0)
        excluded_bytes = int(compaction.get("excluded_orphan_bytes") or 0)
        if excluded_files or excluded_bytes:
            st.success(
                f"Compattazione automatica: esclusi {excluded_files} file vecchi/non più referenziati "
                f"({_human_bytes(excluded_bytes)}), senza eliminare nulla dal PC sorgente."
            )
        st.caption(
            f"File dati effettivamente inclusi: {int((manifest.get('includes') or {}).get('persistent_data_files', 0))}. "
            f"Cifratura: {manifest.get('encryption') or 'compatibile v260'}."
        )
        if not (manifest.get("includes") or {}).get("secrets"):
            st.warning(
                "Nel PC sorgente non è stata trovata la chiave master. Il backup contiene i dati, ma le credenziali "
                "cifrate potrebbero non essere riutilizzabili sul nuovo PC."
            )
        st.download_button(
            "Scarica backup per il nuovo PC",
            data=ready["bytes"],
            file_name=ready["filename"],
            mime="application/octet-stream",
            use_container_width=True,
        )

with import_tab:
    st.subheader("Importa backup")
    st.warning(
        "L'importazione sostituisce i dati presenti su questo PC con quelli del backup. Prima della sostituzione "
        "Marketplace Hub conserva automaticamente una copia completa dello stato attuale nella cartella `migration_backups`."
    )
    st.caption(
        "La v261 accetta anche i backup v260 già creati. Il limite di caricamento locale è stato portato a 1 GB, "
        "quindi puoi importare anche un vecchio file superiore a 200 MB."
    )
    uploaded = st.file_uploader(
        "Seleziona il file .mhubbackup",
        type=["mhubbackup"],
        key="transfer_import_file",
    )
    import_password = st.text_input(
        "Password del backup",
        type="password",
        key="transfer_import_password",
    )

    if uploaded is not None:
        file_bytes = uploaded.getvalue()
        package_hash = hashlib.sha256(file_bytes).hexdigest()
        verify_key = f"{package_hash}:{hashlib.sha256(import_password.encode('utf-8')).hexdigest()}" if import_password else ""
        if st.button("Verifica backup", use_container_width=True):
            try:
                with st.spinner("Verifica backup..."):
                    manifest = inspect_transfer_package(file_bytes, import_password)
                st.session_state["transfer_import_verified"] = {
                    "key": verify_key,
                    "manifest": manifest,
                }
            except TransferError as error:
                st.session_state.pop("transfer_import_verified", None)
                st.error(str(error))
            except Exception as error:
                st.session_state.pop("transfer_import_verified", None)
                st.error(f"Verifica non riuscita: {error}")

        verified = st.session_state.get("transfer_import_verified")
        if verified and verified.get("key") == verify_key:
            manifest = verified["manifest"]
            summary = manifest.get("database_summary") or {}
            st.success("Backup verificato. Nessuna modifica è stata ancora eseguita.")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Versione sorgente", f"v{int(manifest.get('source_release') or 0)}")
            c2.metric("Database", str(manifest.get("source_engine") or "—").upper())
            c3.metric("Seller", int(summary.get("sellers", 0)))
            c4.metric("Listini", int(summary.get("price_lists", 0)))
            created = str(manifest.get("created_at_utc") or "")
            if created:
                st.caption(f"Backup creato: {created}")
            includes = manifest.get("includes") or {}
            if not includes.get("secrets"):
                st.warning(
                    "Questo backup non contiene `.streamlit/secrets.toml`: i dati possono essere importati, "
                    "ma le credenziali cifrate potrebbero richiedere la chiave master originale."
                )
            confirm = st.checkbox(
                "Confermo di voler sostituire i dati di questo PC con quelli del backup verificato.",
                key=f"transfer_import_confirm_{package_hash}",
            )
            if st.button(
                "Importa dati su questo PC",
                type="primary",
                use_container_width=True,
                disabled=not confirm,
            ):
                try:
                    with st.spinner("Importazione e backup di sicurezza in corso..."):
                        report = restore_transfer_package(file_bytes, import_password)
                    st.session_state.pop("transfer_import_verified", None)
                    st.success("Importazione completata correttamente.")
                    st.write(
                        "I dati del vecchio PC sono stati ripristinati. La copia di sicurezza dei dati che erano "
                        "presenti prima dell'importazione è stata conservata qui:"
                    )
                    st.code(str(report.get("safety_backup") or ""))
                    st.error(
                        "Adesso chiudi completamente Marketplace Hub e riavvialo con `AVVIA_WINDOWS.bat`. "
                        "Il riavvio è necessario per caricare la chiave master, le credenziali e il database importati."
                    )
                    st.stop()
                except TransferError as error:
                    st.error(str(error))
                except Exception as error:
                    st.error(f"Importazione non riuscita: {error}")
