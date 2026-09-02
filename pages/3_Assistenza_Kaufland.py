from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from services.db import execute, now_iso, rows
from services.kaufland import KauflandClient
from services.kaufland_support import (
    close_support_ticket,
    encode_ticket_attachment,
    open_support_ticket,
    send_support_message,
    sync_recent_order_units,
    sync_support,
    ticket_sla,
)
from services.security import decrypt_dict
from services.session import bootstrap, seller_selector


def mark_ticket_read(
    account_id: int, environment: str, id_ticket: str, is_read: bool = True
) -> None:
    """Mantiene lo stato letto locale senza dipendere dal connettore API."""
    execute(
        """
        UPDATE kaufland_support_tickets
        SET is_read_local=?,read_at=?
        WHERE marketplace_account_id=? AND environment=? AND id_ticket=?
        """,
        (
            int(bool(is_read)), now_iso() if is_read else "",
            account_id, environment, str(id_ticket).strip(),
        ),
    )


bootstrap()
st.title("Assistenza Kaufland")
st.caption(
    "Consulta ticket, conversazioni, ordini e prodotti collegati. Puoi anche "
    "rispondere, allegare documenti, inviare avvisi provvisori, aprire e chiudere "
    "ticket con anteprima e conferma obbligatoria."
)

seller_id = seller_selector()
if seller_id is None:
    st.stop()

accounts = rows(
    """
    SELECT * FROM marketplace_accounts
    WHERE seller_id=? AND marketplace='kaufland' AND active=1
    ORDER BY account_name
    """,
    (seller_id,),
)
if not accounts:
    st.error("Configura prima un account Kaufland per questo Seller.")
    st.stop()

account_map = {
    f"{item['account_name']} · ID {item['id']}": item for item in accounts
}
account = account_map[
    st.selectbox("Account Kaufland", list(account_map), key="support_account")
]
playground = st.checkbox(
    "Playground (test)",
    value=False,
    key=f"support_playground_{account['id']}",
    help="Produzione e Playground mantengono cache e storico separati.",
)
environment = "test" if playground else "live"
st.info(
    f"Ambiente: {'PLAYGROUND (test)' if playground else 'PRODUZIONE'}. "
    "La sincronizzazione è in sola lettura; le azioni operative vengono inviate "
    "soltanto dopo una conferma esplicita."
)
if not playground:
    st.warning(
        "Ambiente di PRODUZIONE: messaggi, nuovi ticket e chiusure confermate "
        "avranno effetto reale sull’account Kaufland."
    )

credentials = decrypt_dict(account["credentials_encrypted"])
client = KauflandClient(
    credentials.get("client_key", ""),
    credentials.get("secret_key", ""),
    playground,
)

last_sync = rows(
    """
    SELECT * FROM kaufland_support_syncs
    WHERE seller_id=? AND marketplace_account_id=? AND environment=?
    ORDER BY id DESC LIMIT 1
    """,
    (seller_id, account["id"], environment),
)
sync_col, order_sync_col, limit_col = st.columns([1, 1, 1])
sync_scope = limit_col.selectbox(
    "Ticket da sincronizzare",
    ["Tutti i ticket", "Ultimi 100", "Ultimi 250", "Ultimi 500", "Ultimi 1000"],
    index=0,
    key=f"support_sync_limit_{account['id']}_{environment}",
    help=(
        "Aperti e chiusi vengono letti separatamente in pagine da 30, "
        "il massimo consentito dall’API Kaufland."
    ),
)
maximum = {
    "Tutti i ticket": None,
    "Ultimi 100": 100,
    "Ultimi 250": 250,
    "Ultimi 500": 500,
    "Ultimi 1000": 1000,
}[sync_scope]
if sync_col.button(
    "Sincronizza da Kaufland",
    type="primary",
    use_container_width=True,
    key=f"support_sync_{account['id']}_{environment}",
):
    progress_bar = st.progress(0.0, text="Avvio sincronizzazione…")

    def update_progress(done: int, total: int, label: str) -> None:
        progress_bar.progress(
            min(1.0, done / max(1, total)),
            text=f"{done}/{total} · {label}",
        )

    try:
        result = sync_support(
            client,
            seller_id,
            account["id"],
            environment,
            maximum_tickets=maximum,
            progress=update_progress,
        )
        progress_bar.progress(1.0, text="Sincronizzazione completata")
        if result["errors"]:
            st.warning(
                f"Salvati {result['tickets_saved']} ticket, "
                f"{result['messages_saved']} messaggi e "
                f"{result['order_units_saved']} unità ordine. "
                f"Dettagli non disponibili per {len(result['errors'])} richieste."
            )
            with st.expander("Dettagli sincronizzazione"):
                st.dataframe(result["errors"], use_container_width=True, hide_index=True)
        else:
            st.success(
                f"Sincronizzati {result['tickets_saved']} ticket, "
                f"{result['messages_saved']} messaggi e "
                f"{result['order_units_saved']} unità ordine."
            )
        st.rerun()
    except Exception as error:
        progress_bar.empty()
        st.error(f"Sincronizzazione Kaufland non riuscita: {error}")

if order_sync_col.button(
    "Aggiorna ordini recenti",
    use_container_width=True,
    key=f"support_order_sync_{account['id']}_{environment}",
    help="Carica le unità ordine utilizzabili per aprire un nuovo ticket.",
):
    try:
        with st.spinner("Sincronizzazione unità ordine…"):
            order_result = sync_recent_order_units(
                client, seller_id, account["id"], environment,
                maximum=maximum or 1000,
            )
        if order_result["errors"]:
            st.warning(
                f"Salvate {order_result['saved']} unità ordine; "
                f"{len(order_result['errors'])} non sono state salvate."
            )
        else:
            st.success(f"Salvate {order_result['saved']} unità ordine.")
        st.rerun()
    except Exception as error:
        st.error(f"Sincronizzazione ordini Kaufland non riuscita: {error}")

if last_sync:
    item = last_sync[0]
    st.caption(
        f"Ultima sincronizzazione: {item['completed_at'] or item['started_at']} · "
        f"ticket {item['tickets_saved']} · messaggi {item['messages_saved']} · "
        f"unità ordine {item['order_units_saved']} · stato {item['status']}."
    )

unit_rows = rows(
    """
    SELECT * FROM kaufland_support_order_units
    WHERE seller_id=? AND marketplace_account_id=? AND environment=?
    ORDER BY ts_created_iso DESC,id DESC
    """,
    (seller_id, account["id"], environment),
)

reason_labels = {
    "Articolo diverso dalla descrizione": "product_not_as_described",
    "Articolo difettoso": "product_defect",
    "Articolo non consegnato": "product_not_delivered",
    "Reso del prodotto": "product_return",
    "Altra richiesta": "contact_other",
}
with st.expander("Apri un nuovo ticket Kaufland"):
    st.caption(
        "Il ticket può comprendere più unità, purché appartengano allo stesso ordine."
    )
    orders = sorted({
        item["id_order"] for item in unit_rows if item.get("id_order")
    })
    if not orders:
        st.info(
            "Premi «Aggiorna ordini recenti» per caricare gli ordini utilizzabili."
        )
    else:
        selected_order = st.selectbox(
            "Ordine",
            orders,
            key=f"support_new_order_{account['id']}_{environment}",
        )
        order_units = [
            item for item in unit_rows if item["id_order"] == selected_order
        ]
        unit_map = {
            (
                f"{item['product_title'] or 'Prodotto'} · "
                f"EAN {item['ean'] or '—'} · SKU {item['id_offer'] or '—'} · "
                f"unità {item['id_order_unit']}"
            ): item
            for item in order_units
        }
        chosen_units = st.multiselect(
            "Unità ordine interessate",
            list(unit_map),
            key=f"support_new_units_{account['id']}_{environment}_{selected_order}",
        )
        reason_label = st.selectbox(
            "Motivo",
            list(reason_labels),
            key=f"support_new_reason_{account['id']}_{environment}",
        )
        opening_message = st.text_area(
            "Messaggio iniziale",
            height=140,
            key=f"support_new_message_{account['id']}_{environment}",
        )
        st.markdown("**Anteprima del nuovo ticket**")
        st.info(
            f"Ordine: {selected_order} · unità: {len(chosen_units)} · "
            f"motivo: {reason_label}\n\n"
            f"{opening_message.strip() or 'Nessun messaggio inserito.'}"
        )
        confirm_open = st.checkbox(
            (
                "Confermo l’apertura del ticket nel Playground."
                if playground
                else "Confermo l’apertura REALE del ticket su Kaufland."
            ),
            key=f"support_confirm_open_{account['id']}_{environment}",
        )
        if st.button(
            "Apri ticket Kaufland",
            type="primary",
            disabled=(
                not confirm_open
                or not chosen_units
                or not opening_message.strip()
            ),
            key=f"support_open_ticket_{account['id']}_{environment}",
        ):
            try:
                result = open_support_ticket(
                    client,
                    seller_id,
                    account["id"],
                    environment,
                    [
                        int(unit_map[label]["id_order_unit"])
                        for label in chosen_units
                    ],
                    reason_labels[reason_label],
                    opening_message,
                )
                ticket_label = result["id_ticket"] or "creato"
                if result["sync_error"]:
                    st.warning(
                        f"Ticket {ticket_label} aperto, ma la rilettura immediata "
                        f"non è riuscita: {result['sync_error']}"
                    )
                else:
                    st.success(f"Ticket {ticket_label} aperto correttamente.")
            except Exception as error:
                st.error(f"Apertura ticket non riuscita: {error}")

tickets = rows(
    """
    SELECT * FROM kaufland_support_tickets
    WHERE seller_id=? AND marketplace_account_id=? AND environment=?
    ORDER BY is_seller_responsible DESC,ts_updated_iso DESC,id DESC
    """,
    (seller_id, account["id"], environment),
)
if not tickets:
    st.info(
        "Non ci sono ticket salvati per questo account e ambiente. "
        "Premi «Sincronizza da Kaufland» oppure apri un nuovo ticket."
    )
    st.stop()


def json_list(value) -> list[str]:
    try:
        result = json.loads(value or "[]")
        return [str(item) for item in result] if isinstance(result, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []


def local_time(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def status_label(ticket: dict) -> str:
    if ticket["status"] != "opened":
        return "Chiuso"
    return "Da rispondere" if ticket["is_seller_responsible"] else "In attesa"


def order_text(ticket: dict) -> str:
    return ", ".join(json_list(ticket.get("order_ids_json"))) or "—"


def country_text(ticket: dict) -> str:
    return ", ".join(code.upper() for code in json_list(ticket.get("storefronts_json"))) or "—"


sla_map = {ticket["id_ticket"]: ticket_sla(ticket) for ticket in tickets}
opened = [item for item in tickets if item["status"] == "opened"]
responsible = [item for item in opened if item["is_seller_responsible"]]
overdue = [
    item for item in responsible if sla_map[item["id_ticket"]]["overdue"]
]
closed = [item for item in tickets if item["status"] != "opened"]
unread = [item for item in tickets if not item.get("is_read_local")]
attachment_rows = rows(
    """
    SELECT DISTINCT id_ticket FROM kaufland_support_messages
    WHERE marketplace_account_id=? AND environment=?
      AND attachments_json NOT IN ('','[]','null')
    """,
    (account["id"], environment),
)
tickets_with_attachments = {
    str(item["id_ticket"]) for item in attachment_rows
}

st.subheader("Situazione ticket")
k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Totale salvati", len(tickets))
k2.metric("Da leggere", len(unread))
k3.metric("Da rispondere", len(responsible))
k4.metric("Scaduti", len(overdue))
k5.metric("In attesa", len(opened) - len(responsible))
k6.metric("Chiusi", len(closed))
st.caption(
    "“Letto/da leggere” è una memoria del programma, separata da "
    "“da rispondere”, che proviene direttamente da Kaufland."
)

filter_status, filter_read, filter_country = st.columns(3)
chosen_status = filter_status.selectbox(
    "Stato operativo",
    ["Tutti", "Aperti", "Da rispondere", "Scaduti", "In attesa", "Chiusi"],
)
chosen_read = filter_read.selectbox(
    "Lettura",
    ["Tutti", "Da leggere", "Letti"],
)
countries = sorted({
    country_text(item) for item in tickets if country_text(item) != "—"
})
chosen_country = filter_country.selectbox("Paese", ["Tutti", *countries])

filter_topic, filter_reason, filter_attachments = st.columns(3)
topics = sorted({item["topic"] for item in tickets if item["topic"]})
reasons = sorted({
    item["open_reason"] for item in tickets if item["open_reason"]
})
chosen_topic = filter_topic.selectbox("Argomento", ["Tutti", *topics])
chosen_reason = filter_reason.selectbox(
    "Motivo apertura", ["Tutti", *reasons]
)
chosen_attachments = filter_attachments.selectbox(
    "Allegati", ["Tutti", "Con allegati", "Senza allegati"]
)

use_date_filter = st.checkbox("Filtra per data di ultimo aggiornamento")
from_date = to_date = None
if use_date_filter:
    date_from_col, date_to_col = st.columns(2)
    from_date = date_from_col.date_input(
        "Aggiornati dal",
        key=f"support_date_from_{account['id']}_{environment}",
    )
    to_date = date_to_col.date_input(
        "Aggiornati fino al",
        key=f"support_date_to_{account['id']}_{environment}",
    )
search = st.text_input(
    "Cerca ticket, ordine, EAN, SKU, cliente o testo dell’ultimo messaggio",
    placeholder="Numero ticket, ordine, EAN, SKU, cliente…",
)

units_by_id = {str(item["id_order_unit"]): item for item in unit_rows}


def ticket_search_text(ticket: dict) -> str:
    units = [
        units_by_id[unit_id]
        for unit_id in json_list(ticket["ids_order_units_json"])
        if unit_id in units_by_id
    ]
    values = [
        ticket["id_ticket"], order_text(ticket), ticket["id_buyer"],
        ticket["buyer_label"], ticket["topic"], ticket["open_reason"],
        ticket["last_message_preview"],
    ]
    for unit in units:
        values.extend([
            unit["ean"], unit["id_offer"], unit["product_title"],
            unit["buyer_email"], unit["buyer_pseudonym"],
        ])
    return " ".join(str(value or "") for value in values).casefold()


def ticket_updated_date(ticket: dict):
    value = ticket.get("ts_updated_iso") or ticket.get("ts_created_iso") or ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (AttributeError, ValueError):
        return None


filtered = []
for ticket in tickets:
    label = status_label(ticket)
    sla = sla_map[ticket["id_ticket"]]
    if chosen_status == "Aperti" and ticket["status"] != "opened":
        continue
    if chosen_status == "Da rispondere" and label != "Da rispondere":
        continue
    if chosen_status == "Scaduti" and not sla["overdue"]:
        continue
    if chosen_status == "In attesa" and label != "In attesa":
        continue
    if chosen_status == "Chiusi" and label != "Chiuso":
        continue
    if chosen_read == "Da leggere" and ticket.get("is_read_local"):
        continue
    if chosen_read == "Letti" and not ticket.get("is_read_local"):
        continue
    if chosen_country != "Tutti" and country_text(ticket) != chosen_country:
        continue
    if chosen_topic != "Tutti" and chosen_topic != ticket["topic"]:
        continue
    if chosen_reason != "Tutti" and chosen_reason != ticket["open_reason"]:
        continue
    has_attachments = ticket["id_ticket"] in tickets_with_attachments
    if chosen_attachments == "Con allegati" and not has_attachments:
        continue
    if chosen_attachments == "Senza allegati" and has_attachments:
        continue
    updated_date = ticket_updated_date(ticket)
    if use_date_filter and (
        updated_date is None
        or (from_date and updated_date < from_date)
        or (to_date and updated_date > to_date)
    ):
        continue
    if search.strip() and search.strip().casefold() not in ticket_search_text(ticket):
        continue
    filtered.append(ticket)

st.caption(f"{len(filtered)} ticket visualizzati su {len(tickets)} salvati.")
if not filtered:
    st.warning("Nessun ticket corrisponde ai filtri impostati.")
    st.stop()

display_rows = []
for ticket in filtered:
    sla = sla_map[ticket["id_ticket"]]
    display_rows.append({
        "Ticket": ticket["id_ticket"],
        "Stato": status_label(ticket),
        "Lettura": "Letto" if ticket.get("is_read_local") else "Da leggere",
        "SLA": sla["label"],
        "Scadenza indicativa": local_time(sla["deadline"]),
        "Ordine": order_text(ticket),
        "Paese": country_text(ticket),
        "Cliente": ticket["buyer_label"] or ticket["id_buyer"] or "—",
        "Argomento": ticket["topic"] or ticket["open_reason"] or "—",
        "Allegati": (
            "Sì" if ticket["id_ticket"] in tickets_with_attachments else "No"
        ),
        "Messaggi": ticket["message_count"],
        "Ultimo aggiornamento": local_time(ticket["ts_updated_iso"]),
        "Ultimo messaggio": ticket["last_message_preview"],
    })

st.subheader("Elenco ticket")
selection = st.dataframe(
    pd.DataFrame(display_rows),
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    height=min(650, 70 + 35 * len(display_rows)),
)
selected_indices = getattr(getattr(selection, "selection", None), "rows", [])
selected_index = selected_indices[0] if selected_indices else 0
selected = filtered[selected_index]
if selected_indices and not selected.get("is_read_local"):
    mark_ticket_read(
        account["id"], environment, selected["id_ticket"], is_read=True
    )
    selected["is_read_local"] = 1
    selected["read_at"] = datetime.now(timezone.utc).isoformat()

st.divider()
st.subheader(f"Ticket {selected['id_ticket']}")
header1, header2, header3, header4 = st.columns(4)
header1.metric("Stato", status_label(selected))
header2.metric("Ordine", order_text(selected))
header3.metric("Paese", country_text(selected))
header4.metric("Messaggi", selected["message_count"])
st.caption(
    f"Motivo apertura: {selected['open_reason'] or '—'} · "
    f"Argomento: {selected['topic'] or '—'} · "
    f"Creato: {local_time(selected['ts_created_iso'])} · "
    f"Aggiornato: {local_time(selected['ts_updated_iso'])}"
)
read_button_label = (
    "Segna come da leggere"
    if selected.get("is_read_local")
    else "Segna come letto"
)
if st.button(
    read_button_label,
    key=f"support_toggle_read_{account['id']}_{environment}_{selected['id_ticket']}",
):
    mark_ticket_read(
        account["id"], environment, selected["id_ticket"],
        is_read=not bool(selected.get("is_read_local")),
    )
    st.rerun()

selected_units = [
    units_by_id[unit_id]
    for unit_id in json_list(selected["ids_order_units_json"])
    if unit_id in units_by_id
]
order_tab, conversation_tab = st.tabs(["Ordine e prodotti", "Conversazione"])

with order_tab:
    if not selected_units:
        st.warning(
            "I dettagli delle unità ordine non sono ancora disponibili. "
            "Ripeti la sincronizzazione."
        )
    for unit in selected_units:
        with st.container(border=True):
            product_col, value_col = st.columns([3, 1])
            product_col.markdown(
                f"**{unit['product_title'] or 'Prodotto senza titolo'}**  \n"
                f"EAN: `{unit['ean'] or '—'}` · SKU: `{unit['id_offer'] or '—'}`  \n"
                f"Unità ordine: `{unit['id_order_unit']}` · "
                f"Ordine: `{unit['id_order'] or '—'}` · "
                f"Stato: `{unit['status'] or '—'}`"
            )
            total = (
                (unit["price"] or 0) + (unit["shipping_rate"] or 0)
                if unit["price"] is not None else None
            )
            value_col.metric(
                "Totale ordine",
                f"{total:.2f} {unit['currency']}" if total is not None else "—",
                help=(
                    f"Prodotto {unit['price'] or 0:.2f} + "
                    f"spedizione {unit['shipping_rate'] or 0:.2f}"
                ),
            )
            if unit["buyer_pseudonym"] or unit["buyer_email"]:
                st.caption(
                    f"Cliente: {unit['buyer_pseudonym'] or '—'} · "
                    f"{unit['buyer_email'] or '—'}"
                )
            if unit["product_url"]:
                st.link_button(
                    "Apri prodotto Kaufland",
                    unit["product_url"],
                )

with conversation_tab:
    messages = rows(
        """
        SELECT * FROM kaufland_support_messages
        WHERE marketplace_account_id=? AND environment=? AND id_ticket=?
        ORDER BY ts_created_iso,id
        """,
        (account["id"], environment, selected["id_ticket"]),
    )
    if not messages:
        st.warning(
            "La conversazione non è ancora disponibile. Ripeti la sincronizzazione."
        )
    for message in messages:
        author = message["author_name"] or message["author_type"] or "Autore"
        with st.chat_message("assistant"):
            st.markdown(f"**{author}** · {local_time(message['ts_created_iso'])}")
            st.write(message["text"] or "Messaggio senza testo")
            try:
                attachments = json.loads(message["attachments_json"] or "[]")
                if not isinstance(attachments, list):
                    attachments = []
            except (TypeError, ValueError, json.JSONDecodeError):
                attachments = []
            if attachments:
                st.caption(f"Allegati: {len(attachments)}")
                for attachment in attachments:
                    if isinstance(attachment, str):
                        st.write(f"📎 {attachment}")
                    elif isinstance(attachment, dict):
                        name = (
                            attachment.get("filename")
                            or attachment.get("name")
                            or "Allegato"
                        )
                        url = attachment.get("url") or attachment.get("download_url")
                        if url:
                            st.markdown(f"📎 [{name}]({url})")
                        else:
                            st.write(f"📎 {name}")

    if selected["status"] == "closed":
        st.info("Il ticket è chiuso; per questo motivo la risposta è disabilitata.")
    else:
        st.divider()
        st.subheader("Rispondi al ticket")
        if "return" in (
            f"{selected['open_reason']} {selected['topic']}".lower()
        ):
            st.warning(
                "Ticket relativo a un reso: verifica che la prima risposta "
                "contenga un’opzione di reso valida secondo le regole Kaufland."
            )
        reply_text = st.text_area(
            "Messaggio al cliente",
            height=160,
            key=f"support_reply_{account['id']}_{environment}_{selected['id_ticket']}",
        )
        uploaded_files = st.file_uploader(
            "Allegati facoltativi",
            type=[
                "txt", "png", "jpg", "jpeg", "gif", "tif", "tiff",
                "pdf", "xlsx", "docx", "doc",
            ],
            accept_multiple_files=True,
            key=f"support_files_{account['id']}_{environment}_{selected['id_ticket']}",
            help=(
                "Formati Kaufland supportati. Il programma applica un limite "
                "prudenziale di 20 MB per allegato."
            ),
        )
        interim_notice = st.checkbox(
            "Avviso provvisorio: mantieni il ticket a nostro carico",
            key=f"support_interim_{account['id']}_{environment}_{selected['id_ticket']}",
            help=(
                "Usalo per comunicare che stai lavorando alla richiesta ma devi "
                "ancora inviare una risposta definitiva."
            ),
        )
        st.markdown("**Anteprima prima dell’invio**")
        st.info(
            f"Ticket: {selected['id_ticket']} · "
            f"tipo: {'avviso provvisorio' if interim_notice else 'risposta'} · "
            f"allegati: {len(uploaded_files or [])}\n\n"
            f"{reply_text.strip() or 'Nessun messaggio inserito.'}"
        )
        confirm_reply = st.checkbox(
            (
                "Confermo l’invio nel Playground."
                if playground
                else "Confermo l’invio REALE di questo messaggio al cliente."
            ),
            key=f"support_confirm_reply_{account['id']}_{environment}_{selected['id_ticket']}",
        )
        if st.button(
            "Invia messaggio",
            type="primary",
            disabled=not confirm_reply or not reply_text.strip(),
            key=f"support_send_{account['id']}_{environment}_{selected['id_ticket']}",
        ):
            try:
                attachments = [
                    encode_ticket_attachment(
                        item.name, item.type, item.getvalue()
                    )
                    for item in (uploaded_files or [])
                ]
                result = send_support_message(
                    client,
                    seller_id,
                    account["id"],
                    environment,
                    selected["id_ticket"],
                    reply_text,
                    interim_notice,
                    attachments,
                )
                if result["sync_error"]:
                    st.warning(
                        "Messaggio inviato, ma la rilettura immediata del ticket "
                        f"non è riuscita: {result['sync_error']}"
                    )
                else:
                    st.success("Messaggio inviato e conversazione aggiornata.")
                    st.rerun()
            except Exception as error:
                st.error(f"Invio del messaggio non riuscito: {error}")

        with st.expander("Chiudi questo ticket"):
            st.warning(
                "La chiusura modifica lo stato del ticket su Kaufland. "
                "Usala soltanto quando la richiesta è realmente conclusa."
            )
            close_phrase = f"CHIUDI {selected['id_ticket']}"
            close_confirm = st.text_input(
                f"Digita esattamente: {close_phrase}",
                key=f"support_close_confirm_{account['id']}_{environment}_{selected['id_ticket']}",
            )
            if st.button(
                "Chiudi ticket",
                disabled=close_confirm.strip() != close_phrase,
                key=f"support_close_{account['id']}_{environment}_{selected['id_ticket']}",
            ):
                try:
                    result = close_support_ticket(
                        client,
                        seller_id,
                        account["id"],
                        environment,
                        selected["id_ticket"],
                    )
                    if result["sync_error"]:
                        st.warning(
                            "Ticket chiuso, ma la rilettura immediata non è "
                            f"riuscita: {result['sync_error']}"
                        )
                    else:
                        st.success("Ticket chiuso correttamente.")
                        st.rerun()
                except Exception as error:
                    st.error(f"Chiusura ticket non riuscita: {error}")

action_rows = rows(
    """
    SELECT created_at,id_ticket,action_type,status,request_summary_json,error
    FROM kaufland_support_actions
    WHERE seller_id=? AND marketplace_account_id=? AND environment=?
    ORDER BY id DESC LIMIT 100
    """,
    (seller_id, account["id"], environment),
)
if action_rows:
    with st.expander("Storico azioni Assistenza Kaufland"):
        action_labels = {
            "send_message": "Messaggio",
            "interim_notice": "Avviso provvisorio",
            "open_ticket": "Apertura ticket",
            "close_ticket": "Chiusura ticket",
        }
        st.dataframe(
            [
                {
                    "Data": local_time(item["created_at"]),
                    "Ticket": item["id_ticket"] or "—",
                    "Azione": action_labels.get(
                        item["action_type"], item["action_type"]
                    ),
                    "Esito": "Riuscita" if item["status"] == "success" else "Errore",
                    "Errore": item["error"],
                }
                for item in action_rows
            ],
            use_container_width=True,
            hide_index=True,
        )

st.info(
    "Scadenza SLA indicativa: 48 ore lavorative, con esclusione del fine settimana. "
    "La scadenza ufficiale rimane quella applicata da Kaufland."
)
