from __future__ import annotations

import streamlit as st

from services.auth import allowed_menu_keys, is_admin, require_auth

st.set_page_config(page_title="Marketplace Hub", page_icon="🧩", layout="wide")
require_auth()

PAGE_SPECS = [
    ("dashboard", "pages/0_Dashboard.py", "Dashboard", "🏠"),
    ("seller_management", "pages/1_Gestione_Seller.py", "Gestione Seller", "👥"),
    ("suppliers_lists", "pages/2_Fornitori_e_Listini.py", "Fornitori e Listini", "📦"),
    ("ai_provider", "pages/2_Provider_IA.py", "Provider IA", "🤖"),
    ("work_lists", "pages/3_Lavora_sui_Listini.py", "Lavora sui Listini", "🧰"),
    ("product_creation", "pages/3_Creazione_Prodotti.py", "Creazione Prodotti", "🧠"),
    ("marketplace_publication", "pages/3_Pubblicazione_Marketplace.py", "Pubblicazione sui Marketplace", "🚀"),
    ("buybox", "pages/3_Controllo_BuyBox.py", "Controllo Buy Box", "🏆"),
    ("marketplace_orders", "pages/3_Ordini_Marketplace.py", "Ordini Marketplace", "🧾"),
    ("top_products", "pages/3_Prodotti_Piu_Venduti.py", "Prodotti più venduti", "📈"),
    ("cecotec_orders", "pages/4_Ordini_Cecotec.py", "Creazione Ordini Cecotec", "📤"),
    ("innpro_orders", "pages/4_Ordini_INNPRO.py", "Creazione Ordini INNPRO", "📦"),
    ("packlink", "pages/4_Packlink_PRO.py", "Packlink PRO", "📮"),
    ("tracking", "pages/4_Tracciabilita_Ordini.py", "Tracciabilità ordini", "🚚"),
    ("accounting", "pages/4_Contabilita.py", "Contabilità", "📊"),
    ("support", "pages/3_Assistenza_Marketplace.py", "Ticket e messaggi", "💬"),
    ("marketplace_deletion", "pages/3_Cancellazione_Marketplace.py", "Cancellazione dai Marketplace", "🗑️"),
    ("history", "pages/4_Storico.py", "Storico", "🕘"),
    ("backup_transfer", "pages/5_Backup_Trasferimento.py", "Backup e trasferimento", "🔁"),
    ("database", "pages/5_Database.py", "Database", "🗄️"),
]

allowed = allowed_menu_keys()
pages = []
for key, path, title, icon in PAGE_SPECS:
    if key in allowed:
        pages.append(
            st.Page(
                path,
                title=title,
                icon=icon,
                default=(key == "dashboard" and "dashboard" in allowed),
            )
        )

# Gestione Utenti è volutamente riservata agli amministratori: un operatore non
# può autoassegnarsi permessi o creare account con privilegi superiori.
if is_admin():
    pages.append(st.Page("pages/1_Gestione_Utenti.py", title="Gestione Utenti", icon="🔐"))

if not pages:
    st.error("Il tuo account non ha alcuna area del programma abilitata. Contatta l'amministratore.")
    st.stop()

st.navigation(pages, position="sidebar").run()
