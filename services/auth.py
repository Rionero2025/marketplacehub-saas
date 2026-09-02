from __future__ import annotations

import hmac
import os
import time

import streamlit as st

from services.user_access import (
    ALL_MENU_KEYS,
    authenticate_user,
    ensure_user_schema,
    environment_admin_payload,
    get_user,
    session_user_payload,
)

_SESSION_KEY = "_marketplace_hub_user_session"
_SESSION_REFRESH_KEY = "_marketplace_hub_user_session_checked_at"


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _clear_auth_session() -> None:
    st.session_state.pop(_SESSION_KEY, None)
    st.session_state.pop("_marketplace_hub_authenticated", None)
    st.session_state.pop("active_seller_id", None)
    st.session_state.pop("global_seller_selector", None)
    st.session_state.pop(_SESSION_REFRESH_KEY, None)


def current_user() -> dict | None:
    value = st.session_state.get(_SESSION_KEY)
    return dict(value) if isinstance(value, dict) else None


def is_admin() -> bool:
    user = current_user()
    return bool(user and user.get("is_admin"))


def has_permission(permission: str) -> bool:
    user = current_user()
    if not user:
        return False
    if bool(user.get("is_admin")):
        return True
    return str(permission) in set(user.get("permissions") or [])


def allowed_menu_keys() -> set[str]:
    user = current_user()
    if not user:
        return set()
    if bool(user.get("is_admin")):
        return set(ALL_MENU_KEYS)
    return {str(value) for value in user.get("permissions") or []}


def allowed_seller_ids() -> set[int] | None:
    """None means unrestricted/all sellers; a set means explicit scope."""
    user = current_user()
    if not user:
        return set()
    if bool(user.get("is_admin")):
        return None
    values = user.get("seller_ids")
    if values is None:
        return None
    result: set[int] = set()
    for value in values or []:
        try:
            seller_id = int(value)
        except (TypeError, ValueError):
            continue
        if seller_id > 0:
            result.add(seller_id)
    return result


def has_seller_access(seller_id: int) -> bool:
    scope = allowed_seller_ids()
    return scope is None or int(seller_id) in scope


def _refresh_database_session(session: dict, *, force: bool = False) -> dict | None:
    if session.get("source") != "database":
        return session

    # Streamlit reruns the script on every widget interaction. v307 combines the
    # browser-session throttle with the shared Redis-ready user cache, so multiple
    # sessions/processes do not repeat the same PostgreSQL authorization read.
    refresh_seconds = max(1.0, float(os.getenv("MARKETPLACE_HUB_SESSION_REFRESH_SECONDS", "15")))
    now = time.monotonic()
    last_checked = float(st.session_state.get(_SESSION_REFRESH_KEY) or 0.0)
    if not force and last_checked and now - last_checked < refresh_seconds:
        return session

    user_id = int(session.get("id") or 0)
    if user_id <= 0:
        return None
    record = get_user(user_id)
    st.session_state[_SESSION_REFRESH_KEY] = now
    if not record or int(record.get("active") or 0) != 1:
        return None
    return session_user_payload(record, source="database")


def _render_sidebar_session(user: dict) -> None:
    label = str(user.get("display_name") or user.get("username") or "Utente")
    with st.sidebar:
        st.caption(f"Accesso: {label}")
        if not bool(user.get("is_admin")):
            st.caption(f"Aree abilitate: {len(user.get('permissions') or [])}")
            seller_ids = user.get("seller_ids")
            if seller_ids is None:
                st.caption("Seller visibili: tutti")
            else:
                st.caption(f"Seller visibili: {len(seller_ids or [])}")
        if st.button("Esci", key="marketplace_hub_logout"):
            _clear_auth_session()
            st.rerun()


def require_auth() -> dict:
    """Autenticazione multiutente con sessione browser e permessi dinamici."""
    if not _truthy(os.getenv("MARKETPLACE_HUB_REQUIRE_AUTH"), default=False):
        local = environment_admin_payload("locale")
        st.session_state[_SESSION_KEY] = local
        return local

    ensure_user_schema()
    expected_user = str(os.getenv("MARKETPLACE_HUB_ADMIN_USERNAME") or "").strip()
    expected_password = str(os.getenv("MARKETPLACE_HUB_ADMIN_PASSWORD") or "")

    existing = current_user()
    if existing is None and st.session_state.get("_marketplace_hub_authenticated") is True:
        if expected_user:
            existing = environment_admin_payload(expected_user)
            st.session_state[_SESSION_KEY] = existing

    if existing is not None:
        refreshed = _refresh_database_session(existing)
        if refreshed is None:
            _clear_auth_session()
        else:
            # Se l'admin ha appena tolto un Seller all'utente, invalida subito una
            # eventuale selezione precedente memorizzata nella sessione Streamlit.
            old_scope = existing.get("seller_ids")
            new_scope = refreshed.get("seller_ids")
            if old_scope != new_scope:
                st.session_state.pop("active_seller_id", None)
                st.session_state.pop("global_seller_selector", None)
            st.session_state[_SESSION_KEY] = refreshed
            _render_sidebar_session(refreshed)
            return refreshed

    if not expected_user or not expected_password:
        expected_user = ""
        expected_password = ""

    st.title("Marketplace Hub")
    st.subheader("Accesso riservato")
    st.caption(
        "Dopo l'accesso la sessione rimane attiva durante l'uso del programma fino "
        "a quando premi Esci o termina la sessione del browser. La password non viene "
        "memorizzata in chiaro."
    )
    with st.form("marketplace_hub_login", clear_on_submit=False):
        username = st.text_input("Utente", key="marketplace_hub_login_username")
        password = st.text_input("Password", type="password", key="marketplace_hub_login_password")
        submitted = st.form_submit_button("Accedi", type="primary")

    if submitted:
        valid_env_user = bool(expected_user) and hmac.compare_digest(
            str(username).strip(), expected_user
        )
        valid_env_password = bool(expected_password) and hmac.compare_digest(
            str(password), expected_password
        )
        if valid_env_user and valid_env_password:
            payload = environment_admin_payload(expected_user)
            st.session_state[_SESSION_KEY] = payload
            st.session_state.pop("marketplace_hub_login_password", None)
            st.rerun()

        record = authenticate_user(username, password)
        if record:
            payload = session_user_payload(record, source="database")
            st.session_state[_SESSION_KEY] = payload
            st.session_state.pop("marketplace_hub_login_password", None)
            st.rerun()
        st.error("Credenziali non valide o utente disattivato.")
    st.stop()
