from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from services import postgresql_backend
from services.database_config import database_config_public, load_database_config
from services.db import DB_PATH, database_storage_status, database_write_probe, init_db

st.title("Database")
init_db()
status = database_storage_status()
config = load_database_config()
public = database_config_public(config)

engine = str(status.get("engine") or public.get("engine") or "sqlite")
if engine == "postgresql":
    st.success("Database attivo: PostgreSQL")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Host", f"{status.get('host', public['postgresql_host'])}:{status.get('port', public['postgresql_port'])}")
    c2.metric("Database", status.get("database") or public["postgresql_database"])
    c3.metric("Utente", status.get("user") or public["postgresql_user"])
    pool = status.get("pool") or {}
    c4.metric("Connessioni pool", str(pool.get("pool_size", pool.get("pool_available", "—"))))
    if status.get("server_version"):
        st.caption(f"Server PostgreSQL: {status['server_version']}")
    if not status.get("ok"):
        st.error(str(status.get("error") or "Connessione PostgreSQL non disponibile."))
else:
    st.info("Database attivo: SQLite")
    c1, c2, c3 = st.columns(3)
    c1.metric("File database", Path(str(status.get("database_path") or DB_PATH)).name)
    c2.metric("Cartella scrivibile", "Sì" if status.get("directory_writable") else "No")
    c3.metric("Database scrivibile", "Sì" if status.get("database_writable") else "No")
    st.caption(str(status.get("database_path") or DB_PATH))

st.divider()
st.subheader("Diagnostica")
if st.button("Verifica scrittura database", use_container_width=True):
    try:
        database_write_probe()
        st.success("Scrittura transazionale verificata correttamente.")
    except Exception as error:
        st.error(f"Verifica scrittura non riuscita: {error}")

st.divider()
st.subheader("PostgreSQL")
pg1, pg2, pg3 = st.columns(3)
pg1.metric("Host configurato", f"{public['postgresql_host']}:{public['postgresql_port']}")
pg2.metric("Database configurato", public["postgresql_database"])
pg3.metric("Pool", f"{public['postgresql_pool_min']}–{public['postgresql_pool_max']}")
if public["postgresql_password_configured"]:
    if st.button("Testa connessione PostgreSQL configurata", use_container_width=True):
        try:
            info = postgresql_backend.test_connection(config)
            st.success(
                f"Connessione riuscita: {info.get('database', public['postgresql_database'])} · "
                f"utente {info.get('username', public['postgresql_user'])}."
            )
        except Exception as error:
            st.error(f"Connessione PostgreSQL non riuscita: {error}")
else:
    st.caption(
        "PostgreSQL non è ancora configurato. Usa MIGRA_A_POSTGRESQL_WINDOWS.bat "
        "dalla cartella principale di Marketplace Hub."
    )

reports = sorted((Path(__file__).resolve().parents[1] / "data").glob("postgresql_migration_report_*.json"), reverse=True)
if reports:
    st.markdown("#### Ultima migrazione")
    try:
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        verification = report.get("verification") or {}
        r1, r2, r3 = st.columns(3)
        r1.metric("Tabelle verificate", len(verification.get("tables") or {}))
        r2.metric("Differenze", len(verification.get("mismatches") or []))
        r3.metric("Esito", "OK" if verification.get("ok") else "Da verificare")
        st.caption(f"Report: {reports[0]}")
        st.caption(f"Backup SQLite: {report.get('backup', '—')}")
    except Exception as error:
        st.warning(f"Impossibile leggere l'ultimo report di migrazione: {error}")

st.divider()
st.caption(
    "Per tornare temporaneamente a SQLite senza cancellare PostgreSQL, chiudi Marketplace Hub "
    "ed esegui TORNA_A_SQLITE_WINDOWS.bat."
)
