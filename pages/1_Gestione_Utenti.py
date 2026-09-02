from __future__ import annotations

import streamlit as st

from services.auth import current_user, is_admin
from services.db import sellers
from services.session import bootstrap
from services.user_access import (
    MENU_AREAS,
    create_user,
    delete_user,
    get_user,
    list_users,
    update_user,
)

bootstrap()
if not is_admin():
    st.error("Questa area è riservata agli amministratori.")
    st.stop()

st.title("Gestione Utenti")
st.caption(
    "Crea gli utenti che possono accedere a Marketplace Hub e scegli sia le aree "
    "del menu sia i Seller che ciascun utente può vedere e utilizzare."
)


def permission_checkboxes(prefix: str, selected: set[str], *, disabled: bool = False) -> list[str]:
    st.markdown("#### Aree del menu abilitate")
    st.caption("Spunta soltanto le sezioni che questo utente deve poter utilizzare.")
    columns = st.columns(3)
    result: list[str] = []
    for index, (key, label) in enumerate(MENU_AREAS):
        enabled = columns[index % 3].checkbox(
            label,
            value=(key in selected),
            key=f"{prefix}_{key}",
            disabled=disabled,
        )
        if enabled:
            result.append(key)
    return result


def seller_checkboxes(
    prefix: str,
    selected: set[int],
    *,
    disabled: bool = False,
) -> list[int]:
    all_sellers = sellers(False)  # admin: services.db returns every seller
    st.markdown("#### Seller visibili")
    st.caption(
        "Selezione multipla: l'utente vedrà e potrà lavorare esclusivamente sui Seller "
        "spuntati. Il filtro viene applicato centralmente anche a Dashboard e Contabilità."
    )
    if not all_sellers:
        st.info("Non risultano Seller registrati.")
        return []
    cols = st.columns(3)
    result: list[int] = []
    for index, seller in enumerate(all_sellers):
        seller_id = int(seller["id"])
        label = f"{seller.get('name') or 'Seller'} · ID {seller_id}"
        checked = cols[index % 3].checkbox(
            label,
            value=(seller_id in selected),
            key=f"{prefix}_seller_{seller_id}",
            disabled=disabled,
        )
        if checked:
            result.append(seller_id)
    return result


all_seller_rows = sellers(False)
all_seller_ids = {int(item["id"]) for item in all_seller_rows}

with st.expander("➕ Crea nuovo utente", expanded=True):
    create_left, create_right = st.columns(2)
    new_username = create_left.text_input("Username *", key="new_app_username")
    new_display_name = create_right.text_input("Nome visualizzato", key="new_app_display_name")
    password_left, password_right = st.columns(2)
    new_password = password_left.text_input("Password *", type="password", key="new_app_password")
    confirm_password = password_right.text_input(
        "Conferma password *", type="password", key="new_app_password_confirm"
    )
    admin_col, active_col = st.columns(2)
    new_is_admin = admin_col.checkbox(
        "Amministratore (tutte le aree + tutti i Seller + Gestione Utenti)",
        key="new_app_is_admin",
    )
    new_active = active_col.checkbox("Utente attivo", value=True, key="new_app_active")

    selected_new = permission_checkboxes(
        "new_user_perm", set(), disabled=bool(new_is_admin)
    )
    selected_new_sellers = seller_checkboxes(
        "new_user_scope",
        all_seller_ids if new_is_admin else set(),
        disabled=bool(new_is_admin),
    )
    if new_is_admin:
        selected_new_sellers = sorted(all_seller_ids)
        st.info("Un amministratore ha automaticamente accesso a tutte le aree e a tutti i Seller.")

    if st.button("Crea utente", type="primary", key="create_app_user"):
        if new_password != confirm_password:
            st.error("Le due password non coincidono.")
        else:
            try:
                user_id = create_user(
                    new_username,
                    new_password,
                    display_name=new_display_name,
                    permissions=selected_new,
                    seller_ids=selected_new_sellers,
                    is_admin=new_is_admin,
                    active=new_active,
                )
                st.success(f"Utente creato correttamente · ID {user_id}.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

st.divider()
st.subheader("Utenti registrati")
users = list_users()
if not users:
    st.info("Non sono ancora stati creati utenti aggiuntivi. L'account amministratore Render resta attivo.")
    st.stop()

labels = {
    f"{item.get('username')} · {item.get('display_name') or 'senza nome'} · ID {item['id']}": int(item["id"])
    for item in users
}
chosen_label = st.selectbox("Utente da modificare", list(labels), key="edit_app_user_select")
selected_user = get_user(labels[chosen_label])
if not selected_user:
    st.error("Utente non trovato.")
    st.stop()

edit_prefix = f"edit_user_{selected_user['id']}"
left, right = st.columns(2)
edit_username = left.text_input(
    "Username", value=str(selected_user.get("username") or ""), key=f"{edit_prefix}_username"
)
edit_display = right.text_input(
    "Nome visualizzato",
    value=str(selected_user.get("display_name") or ""),
    key=f"{edit_prefix}_display",
)
flag_left, flag_right = st.columns(2)
edit_admin = flag_left.checkbox(
    "Amministratore",
    value=bool(int(selected_user.get("is_admin") or 0)),
    key=f"{edit_prefix}_admin",
)
edit_active = flag_right.checkbox(
    "Utente attivo",
    value=bool(int(selected_user.get("active") or 0)),
    key=f"{edit_prefix}_active",
)
selected_existing = permission_checkboxes(
    edit_prefix + "_perm",
    set(selected_user.get("permissions") or []),
    disabled=bool(edit_admin),
)

stored_scope = selected_user.get("seller_ids")
# v291 legacy users have scope=None: visually show all selected. Saving converts
# that legacy unrestricted state into an explicit list, as requested in v292.
selected_scope = all_seller_ids if stored_scope is None else {int(x) for x in stored_scope}
selected_existing_sellers = seller_checkboxes(
    edit_prefix + "_scope",
    selected_scope,
    disabled=bool(edit_admin),
)
if edit_admin:
    selected_existing_sellers = sorted(all_seller_ids)
    st.info("Un amministratore ha automaticamente accesso a tutte le aree e a tutti i Seller.")
elif stored_scope is None:
    st.info(
        "Questo utente proviene dalla v291 e al momento vede tutti i Seller. "
        "Salvando le modifiche verrà memorizzata la selezione esplicita mostrata sopra."
    )

new_password_edit = st.text_input(
    "Nuova password (lascia vuoto per non cambiarla)",
    type="password",
    key=f"{edit_prefix}_password",
)

save_col, delete_col = st.columns([2, 1])
if save_col.button("Salva modifiche", type="primary", key=f"{edit_prefix}_save"):
    try:
        update_user(
            selected_user["id"],
            username=edit_username,
            display_name=edit_display,
            permissions=selected_existing,
            seller_ids=selected_existing_sellers,
            is_admin=edit_admin,
            active=edit_active,
            new_password=new_password_edit,
        )
        st.success("Utente aggiornato. I nuovi permessi Seller sono attivi dal prossimo rerun.")
        st.rerun()
    except Exception as exc:
        st.error(str(exc))

current = current_user() or {}
current_id = int(current.get("id") or 0)
can_delete = current_id != int(selected_user["id"])
confirm_delete = delete_col.checkbox(
    "Conferma eliminazione", key=f"{edit_prefix}_delete_confirm", disabled=not can_delete
)
if delete_col.button(
    "Elimina utente",
    key=f"{edit_prefix}_delete",
    disabled=(not can_delete or not confirm_delete),
):
    try:
        delete_user(selected_user["id"])
        st.success("Utente eliminato.")
        st.rerun()
    except Exception as exc:
        st.error(str(exc))

st.divider()
st.caption(
    "Sicurezza: password salvate solo come hash PBKDF2-SHA256 con salt. La sessione "
    "conserva il profilo autenticato, non la password. I Seller assegnati sono filtrati "
    "centralmente: un utente non amministratore vede solo quelli autorizzati."
)
