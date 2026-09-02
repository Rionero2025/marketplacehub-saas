from __future__ import annotations

ORDER_SERVICE_VERSION = 255

import hashlib
import hmac
import io
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

import pandas as pd
import requests

try:
    from services.db import DATA_DIR, execute, execute_many, json_text, now_iso, rows
except Exception:  # pragma: no cover - permette i test standalone
    execute = None  # type: ignore[assignment]
    execute_many = None  # type: ignore[assignment]
    rows = None  # type: ignore[assignment]
    DATA_DIR = Path("data")

    def json_text(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


CECOTEC_COLUMNS: tuple[str, ...] = (
    "order",
    "article",
    "quantity",
    "customer_name",
    "nif",
    "attention_of",
    "address",
    "postal_code",
    "phone",
    "city",
    "country_code",
    "customer_email",
    "cash_on_delivery",
    "comment",
    "price",
    "discount",
    "addressee_order_number",
    "delivery_type",
)

NORMALIZED_STATUSES = (
    "In attesa", "Da spedire", "Spedito", "Ricevuto",
    "Restituito/Rimborsato", "Cancellato",
)

# Stati ufficiali restituiti dalle API dei due marketplace. I codici vengono
# conservati senza tradurli nel database; la traduzione serve solo per l'interfaccia.
WORTEN_ORDER_STATUS_LABELS: dict[str, str] = {
    "STAGING": "In preparazione",
    "WAITING_ACCEPTANCE": "In attesa di accettazione",
    "WAITING_DEBIT": "In attesa di addebito",
    "WAITING_DEBIT_PAYMENT": "In attesa del pagamento",
    "SHIPPING": "In attesa di spedizione",
    "SHIPPED": "Spedito",
    "TO_COLLECT": "Disponibile per il ritiro",
    "RECEIVED": "Ricevuto",
    "CLOSED": "Chiuso",
    "REFUSED": "Rifiutato",
    "CANCELED": "Cancellato",
    "CANCELLED": "Cancellato",
    "RETURNED": "Restituito",
    "REFUNDED": "Rimborsato",
    "FULLY_REFUNDED": "Rimborsato",
    "PARTIALLY_REFUNDED": "Rimborsato parzialmente",
    # Stati Mirakl deprecati che possono essere ancora presenti nello storico.
    "WAITING_REFUND": "In attesa di rimborso",
    "WAITING_REFUND_PAYMENT": "In attesa del pagamento del rimborso",
}
WORTEN_ORDER_STATUS_ORDER: tuple[str, ...] = tuple(WORTEN_ORDER_STATUS_LABELS)

KAUFLAND_ORDER_STATUS_LABELS: dict[str, str] = {
    "open": "Aperto - verifica dati di consegna",
    "need_to_be_sent": "In attesa di spedizione",
    "sent": "Spedito",
    "sent_and_autopaid": "Spedito e pagato automaticamente",
    "received": "Ricevuto",
    "returned": "Restituito",
    "returned_paid": "Reso rimborsato",
    "cancelled": "Cancellato",
    "canceled": "Cancellato",
}
# Valori accettati dal filtro status di GET /order-units Kaufland.
KAUFLAND_ORDER_STATUS_ORDER: tuple[str, ...] = (
    "open",
    "need_to_be_sent",
    "sent",
    "sent_and_autopaid",
    "received",
    "returned",
    "returned_paid",
    "cancelled",
)

DEFAULT_ACTIONABLE_STATUS: dict[str, tuple[str, ...]] = {
    "worten": ("SHIPPING",),
    "kaufland": ("need_to_be_sent",),
}

# Fallback di sicurezza. La funzione calling_code_for_country prova sempre prima
# il lookup online e usa questa tabella solo se la rete non è disponibile.
FALLBACK_CALLING_CODES: dict[str, str] = {
    "AT": "43", "BE": "32", "BG": "359", "CH": "41", "CY": "357",
    "CZ": "420", "DE": "49", "DK": "45", "EE": "372", "ES": "34",
    "FI": "358", "FR": "33", "GB": "44", "GR": "30", "HR": "385",
    "HU": "36", "IE": "353", "IS": "354", "IT": "39", "LT": "370",
    "LU": "352", "LV": "371", "MT": "356", "NL": "31", "NO": "47",
    "PL": "48", "PT": "351", "RO": "40", "SE": "46", "SI": "386",
    "SK": "421", "US": "1", "CA": "1",
}

STATUS_GROUPS: dict[str, set[str]] = {
    "Cancellato": {
        "cancelled", "canceled", "cancel", "refused",
        "closed_cancelled", "closed_canceled", "cancellation_pending",
    },
    "Restituito/Rimborsato": {
        "refunded", "fully_refunded", "partially_refunded", "returned",
        "returned_paid", "waiting_refund", "waiting_refund_payment",
        "waiting_refund_tax_confirmation",
    },
    "Ricevuto": {
        "received", "delivered", "closed", "completed", "complete",
        "delivery_confirmed", "closed_received", "picked_up",
    },
    "Spedito": {
        "sent", "sent_and_autopaid", "shipped", "in_transit", "to_collect",
        "delivery", "waiting_receipt", "to_receive", "dispatched", "fulfilled",
    },
    "Da spedire": {
        "need_to_be_sent", "need_to_send", "to_ship", "shipping",
        "waiting_shipping", "waiting_shipment", "accepted",
        "processing", "in_fulfillment", "in_fulfilment",
    },
    "In attesa": {
        "open", "waiting_acceptance", "waiting_debit", "waiting_debit_payment",
        "staging", "pending",
    },
}


@dataclass(frozen=True)
class CompositeSku:
    raw: str
    supplier: str
    product_code: str
    purchase_cost: float | None
    minimum_price: float | None
    valid: bool
    error: str = ""


@dataclass(frozen=True)
class CatalogMatch:
    ean: str
    article_raw: str
    article: str
    product_name: str
    matched: bool
    error: str = ""


def _require_db() -> None:
    if execute is None or rows is None:
        raise RuntimeError("I servizi database del progetto non sono disponibili.")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_identifier(value: Any) -> str:
    text = clean_text(value)
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def normalize_country_code(value: Any) -> str:
    code = clean_text(value).upper()
    aliases = {
        "UK": "GB", "GERMANY": "DE", "ITALY": "IT", "PORTUGAL": "PT",
        "SPAIN": "ES", "FRANCE": "FR", "AUSTRIA": "AT", "POLAND": "PL",
        "CZECHIA": "CZ", "CZECH REPUBLIC": "CZ", "NETHERLANDS": "NL",
        "BELGIUM": "BE", "SLOVAKIA": "SK", "ROMANIA": "RO",
    }
    return aliases.get(code, code[:2])


def normalize_supplier(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def parse_decimal(value: Any) -> float | None:
    text = clean_text(value).replace("€", "").replace(" ", "")
    if not text:
        return None
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    elif text.count(",") and text.count("."):
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_composite_sku(value: Any) -> CompositeSku:
    raw = clean_text(value)
    if not raw:
        return CompositeSku(raw, "", "", None, None, False, "SKU vuoto")

    parts = raw.rsplit("_", 3)
    if len(parts) != 4:
        return CompositeSku(
            raw, "", "", None, None, False,
            "Formato SKU non valido: atteso fornitore_codice_costo_prezzominimo",
        )
    supplier, product_code, purchase_cost, minimum_price = parts
    supplier = clean_text(supplier)
    product_code = clean_identifier(product_code)
    if not supplier or not product_code:
        return CompositeSku(raw, supplier, product_code, None, None, False, "Fornitore o codice mancanti")

    cost = parse_decimal(purchase_cost)
    minimum = parse_decimal(minimum_price)
    # Costo e prezzo minimo non servono per il file Cecotec: anche un vecchio
    # SKU con valori finali non numerici resta utilizzabile se fornitore e codice
    # prodotto sono presenti.
    note = "" if cost is not None and minimum is not None else "Costo o prezzo minimo non numerico"
    return CompositeSku(raw, supplier, product_code, cost, minimum, True, note)


def normalize_cecotec_article(value: Any) -> str:
    """Normalizza lo SKU Cecotec senza alterare i codici alfanumerici.

    Cecotec richiede un solo zero iniziale esclusivamente per gli SKU composti
    da cifre. Quando lo SKU contiene almeno una lettera, il codice deve essere
    esportato esattamente come compare nel listino, senza anteporre ``0``.
    """
    raw = clean_identifier(value)
    if not raw:
        return ""
    if re.fullmatch(r"\d+", raw):
        core = raw.lstrip("0") or "0"
        return "0" + core
    return raw


def status_code_key(raw_status: Any) -> str:
    """Chiave confrontabile per codici API con maiuscole o minuscole diverse."""
    return re.sub(r"[^a-z0-9]+", "_", clean_text(raw_status).lower()).strip("_")


def marketplace_status_label(marketplace: Any, raw_status: Any) -> str:
    """Restituisce il label italiano corretto per il codice API del marketplace."""
    marketplace_key = clean_text(marketplace).lower()
    raw = clean_text(raw_status)
    if not raw:
        return "Stato non disponibile"
    if marketplace_key == "worten":
        code = raw.upper()
        return WORTEN_ORDER_STATUS_LABELS.get(
            code,
            code.replace("_", " ").strip().capitalize(),
        )
    if marketplace_key == "kaufland":
        code = raw.lower()
        return KAUFLAND_ORDER_STATUS_LABELS.get(
            code,
            code.replace("_", " ").strip().capitalize(),
        )
    return raw.replace("_", " ").strip().capitalize()


def marketplace_status_options(
    marketplace: Any,
    available_statuses: Iterable[Any] | None = None,
) -> list[str]:
    """Codici di stato in ordine operativo, più eventuali codici sconosciuti API."""
    marketplace_key = clean_text(marketplace).lower()
    if marketplace_key == "worten":
        canonical = list(WORTEN_ORDER_STATUS_ORDER)
        normalize = lambda value: clean_text(value).upper()
    elif marketplace_key == "kaufland":
        canonical = list(KAUFLAND_ORDER_STATUS_ORDER)
        normalize = lambda value: clean_text(value).lower()
    else:
        canonical = []
        normalize = lambda value: clean_text(value)

    available = [normalize(value) for value in (available_statuses or []) if clean_text(value)]
    extras = sorted({value for value in available if value and value not in canonical})
    return canonical + extras


def default_marketplace_statuses(
    marketplace: Any,
    available_statuses: Iterable[Any] | None = None,
) -> list[str]:
    """Preseleziona lo stato realmente pronto per la spedizione su ogni API."""
    marketplace_key = clean_text(marketplace).lower()
    defaults = list(DEFAULT_ACTIONABLE_STATUS.get(marketplace_key, ()))
    # I codici ufficiali restano selezionabili anche se nel periodo corrente il
    # conteggio è zero; così il filtro non cambia significato tra sincronizzazioni.
    return defaults


def normalized_status(raw_status: Any, marketplace: Any = "") -> str:
    """Raggruppa gli stati API nelle quattro macro-categorie del gestionale.

    Mirakl usa SHIPPING per un ordine ancora da spedire, mentre SHIPPED indica
    un ordine già spedito. La distinzione marketplace evita di confondere i due.
    """
    marketplace_key = clean_text(marketplace).lower()
    raw = clean_text(raw_status)
    if marketplace_key == "worten":
        code = raw.upper()
        if code in {"CANCELED", "CANCELLED", "REFUSED"}:
            return "Cancellato"
        if code in {
            "REFUNDED", "FULLY_REFUNDED", "PARTIALLY_REFUNDED", "RETURNED",
            "WAITING_REFUND", "WAITING_REFUND_PAYMENT",
            "WAITING_REFUND_TAX_CONFIRMATION",
        }:
            return "Restituito/Rimborsato"
        if code in {"RECEIVED", "CLOSED"}:
            return "Ricevuto"
        if code in {"SHIPPED", "TO_COLLECT"}:
            return "Spedito"
        if code == "SHIPPING":
            return "Da spedire"
        if code in {
            "STAGING", "WAITING_ACCEPTANCE", "WAITING_DEBIT",
            "WAITING_DEBIT_PAYMENT",
        }:
            return "In attesa"
    elif marketplace_key == "kaufland":
        code = raw.lower()
        if code in {"cancelled", "canceled"}:
            return "Cancellato"
        if code in {"returned", "returned_paid"}:
            return "Restituito/Rimborsato"
        if code == "received":
            return "Ricevuto"
        if code in {"sent", "sent_and_autopaid"}:
            return "Spedito"
        if code == "need_to_be_sent":
            return "Da spedire"
        if code == "open":
            return "In attesa"

    value = status_code_key(raw)
    if not value:
        return "In attesa"
    for label, values in STATUS_GROUPS.items():
        if value in values:
            return label
    if any(token in value for token in ("cancel", "refus")):
        return "Cancellato"
    if any(token in value for token in ("deliver", "receiv", "complete", "closed")):
        return "Ricevuto"
    if value == "shipping":
        return "Da spedire"
    if any(token in value for token in ("shipped", "sent", "transit", "dispatch")):
        return "Spedito"
    return "Da spedire"


def make_row_key(*parts: Any) -> str:
    raw = "\0".join(clean_text(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def filter_order_frame(
    frame: pd.DataFrame,
    *,
    suppliers: Sequence[Any] | None = None,
    statuses: Sequence[Any] | None = None,
    normalized_statuses: Sequence[Any] | None = None,
    countries: Sequence[Any] | None = None,
    search_text: Any = "",
    quantity_min: int | None = None,
    quantity_max: int | None = None,
    actionable_only: bool = False,
    actionable_statuses: Sequence[Any] | None = None,
    generated_mode: str = "all",
    generated_keys: set[str] | None = None,
    data_quality: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Filtra le righe ordine; una selezione vuota significa *tutti*.

    Questa semantica evita il caso in cui i multiselect Streamlit siano vuoti
    (per prima apertura, cambio account o azzeramento manuale) e la pagina
    nasconda erroneamente tutti gli ordini.
    """
    result = frame.copy()

    supplier_values = {clean_text(value) for value in (suppliers or []) if clean_text(value)}
    status_values = {clean_text(value) for value in (statuses or []) if clean_text(value)}
    status_keys = {status_code_key(value) for value in status_values if status_code_key(value)}
    country_values = {normalize_country_code(value) for value in (countries or []) if clean_text(value)}
    normalized_values = {clean_text(value) for value in (normalized_statuses or []) if clean_text(value)}
    actionable_keys = {status_code_key(value) for value in (actionable_statuses or []) if status_code_key(value)}
    generated_key_set = {clean_text(value) for value in (generated_keys or set()) if clean_text(value)}
    quality_values = {clean_text(value) for value in (data_quality or []) if clean_text(value)}

    if supplier_values and "supplier" in result.columns:
        result = result[result["supplier"].fillna("").astype(str).isin(supplier_values)]
    if status_values:
        mask = pd.Series(False, index=result.index)
        if "raw_status" in result.columns and status_keys:
            mask = mask | result["raw_status"].map(status_code_key).isin(status_keys)
        if "normalized_status" in result.columns:
            mask = mask | result["normalized_status"].fillna("").astype(str).isin(status_values)
        result = result[mask]
    if normalized_values and "normalized_status" in result.columns:
        result = result[result["normalized_status"].fillna("").astype(str).isin(normalized_values)]
    if country_values and "country_code" in result.columns:
        normalized_countries = result["country_code"].map(normalize_country_code)
        result = result[normalized_countries.isin(country_values)]
    if actionable_only and actionable_keys and "raw_status" in result.columns:
        result = result[result["raw_status"].map(status_code_key).isin(actionable_keys)]
    if "quantity" in result.columns:
        quantities = pd.to_numeric(result["quantity"], errors="coerce").fillna(1)
        if quantity_min is not None:
            result = result[quantities >= int(quantity_min)]
            quantities = pd.to_numeric(result["quantity"], errors="coerce").fillna(1)
        if quantity_max is not None:
            result = result[quantities <= int(quantity_max)]
    if generated_mode in {"generated", "not_generated"} and "row_key" in result.columns:
        generated_mask = result["row_key"].fillna("").astype(str).isin(generated_key_set)
        result = result[generated_mask if generated_mode == "generated" else ~generated_mask]
    if quality_values:
        masks: list[pd.Series] = []
        if "Dati cliente completi" in quality_values:
            required = [column for column in ("customer_name", "address", "postal_code", "city", "country_code") if column in result.columns]
            if required:
                masks.append(result[required].fillna("").astype(str).apply(lambda row: all(bool(clean_text(v)) for v in row), axis=1))
        if "Telefono presente" in quality_values and "phone" in result.columns:
            masks.append(result["phone"].fillna("").astype(str).map(lambda value: bool(clean_text(value))))
        if "Email presente" in quality_values and "customer_email" in result.columns:
            masks.append(result["customer_email"].fillna("").astype(str).map(lambda value: bool(clean_text(value))))
        if "SKU valido" in quality_values and "composite_sku" in result.columns:
            masks.append(result["composite_sku"].map(lambda value: parse_composite_sku(value).valid))
        if "Dati incompleti" in quality_values:
            required = [column for column in ("customer_name", "address", "postal_code", "city", "country_code") if column in result.columns]
            if required:
                masks.append(~result[required].fillna("").astype(str).apply(lambda row: all(bool(clean_text(v)) for v in row), axis=1))
        if masks:
            combined = masks[0]
            for mask in masks[1:]:
                combined = combined & mask
            result = result[combined]

    query = clean_text(search_text).lower()
    if query and not result.empty:
        candidate_columns = (
            "order_id", "order_line_id", "composite_sku", "product_title",
            "customer_name", "city", "postal_code", "country_code", "supplier",
        )
        search_columns = [column for column in candidate_columns if column in result.columns]
        if search_columns:
            haystack = (
                result[search_columns]
                .fillna("")
                .astype(str)
                .agg(" ".join, axis=1)
                .str.lower()
            )
            result = result[haystack.str.contains(query, regex=False)]
    return result.copy()


def ensure_schema() -> None:
    _require_db()
    statements = (
        """
        CREATE TABLE IF NOT EXISTS cecotec_order_cache(
            seller_id INTEGER NOT NULL,
            marketplace_account_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            row_key TEXT NOT NULL,
            order_id TEXT NOT NULL,
            order_line_id TEXT,
            order_created TEXT,
            raw_status TEXT,
            normalized_status TEXT,
            supplier TEXT,
            composite_sku TEXT,
            product_title TEXT,
            quantity INTEGER NOT NULL DEFAULT 1,
            customer_name TEXT,
            address TEXT,
            postal_code TEXT,
            phone TEXT,
            city TEXT,
            storefront TEXT,
            country_code TEXT,
            customer_email TEXT,
            raw_json TEXT,
            synced_at TEXT NOT NULL,
            PRIMARY KEY(seller_id, marketplace_account_id, marketplace, row_key)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_cecotec_order_cache_range
        ON cecotec_order_cache(
            seller_id, marketplace_account_id, marketplace, order_created
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_cecotec_order_cache_speed_v303
        ON cecotec_order_cache(
            seller_id, marketplace_account_id, marketplace, order_created DESC, row_key
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_cecotec_order_cache_synced
        ON cecotec_order_cache(
            seller_id, marketplace_account_id, marketplace, synced_at
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cecotec_order_exports(
            export_id TEXT PRIMARY KEY,
            seller_id INTEGER NOT NULL,
            marketplace_account_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_format TEXT NOT NULL,
            file_path TEXT NOT NULL,
            selected_rows INTEGER NOT NULL,
            exported_rows INTEGER NOT NULL,
            excluded_rows INTEGER NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cecotec_order_export_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id TEXT NOT NULL,
            seller_id INTEGER NOT NULL,
            marketplace_account_id INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            row_key TEXT NOT NULL,
            order_id TEXT NOT NULL,
            order_line_id TEXT,
            ean TEXT,
            article TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_cecotec_export_items_row
        ON cecotec_order_export_items(
            seller_id, marketplace_account_id, marketplace, row_key, created_at
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS cecotec_country_calling_codes(
            country_code TEXT PRIMARY KEY,
            calling_code TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        execute(statement)
    # Backward-compatible migration for installations created before storefront
    # was persisted for Kaufland orders.
    try:
        cache_columns = {
            clean_text(item.get("name"))
            for item in rows("PRAGMA table_info(cecotec_order_cache)")
        }
        if "storefront" not in cache_columns:
            execute("ALTER TABLE cecotec_order_cache ADD COLUMN storefront TEXT")
    except Exception:
        pass


def _cache_calling_code(country_code: str, calling_code: str, source: str) -> None:
    if execute is None:
        return
    execute(
        """INSERT INTO cecotec_country_calling_codes(
        country_code,calling_code,source,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(country_code) DO UPDATE SET
        calling_code=excluded.calling_code,source=excluded.source,updated_at=excluded.updated_at""",
        (country_code, calling_code, source, now_iso()),
    )


def _cached_calling_code(country_code: str, max_age_days: int = 90) -> str:
    if rows is None:
        return ""
    found = rows(
        "SELECT calling_code,updated_at FROM cecotec_country_calling_codes WHERE country_code=?",
        (country_code,),
    )
    if not found:
        return ""
    item = found[0]
    try:
        updated = datetime.fromisoformat(str(item.get("updated_at") or ""))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > timedelta(days=max_age_days):
            return ""
    except Exception:
        pass
    return re.sub(r"\D", "", str(item.get("calling_code") or ""))


def _calling_code_from_v31(payload: Any) -> str:
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, Mapping):
        return ""
    idd = item.get("idd")
    if not isinstance(idd, Mapping):
        return ""
    root = re.sub(r"\D", "", str(idd.get("root") or ""))
    suffixes = idd.get("suffixes") or []
    suffixes = [re.sub(r"\D", "", str(value)) for value in suffixes if str(value).strip()]
    if not root:
        return ""
    # Il piano nordamericano usa +1 con prefissi locali nella lista suffixes.
    if root == "1":
        return "1"
    if suffixes:
        return root + min(suffixes, key=len)
    return root


def calling_code_for_country(
    country_code: Any,
    *,
    force_online: bool = False,
    timeout: float = 8.0,
    restcountries_api_key: str = "",
) -> tuple[str, str]:
    code = normalize_country_code(country_code)
    if not re.fullmatch(r"[A-Z]{2}", code):
        return "", "country_code_non_valido"

    if not force_online:
        cached = _cached_calling_code(code)
        if cached:
            return cached, "cache"

    # Endpoint pubblico v3.1: lookup online diretto del campo idd.
    try:
        response = requests.get(
            f"https://restcountries.com/v3.1/alpha/{code}",
            params={"fields": "idd"},
            timeout=timeout,
            headers={"User-Agent": "MarketplaceHub-CecotecOrders/1.0"},
        )
        response.raise_for_status()
        value = _calling_code_from_v31(response.json())
        if value:
            _cache_calling_code(code, value, "restcountries_v3.1")
            return value, "restcountries_v3.1"
    except Exception:
        pass

    # Endpoint corrente autenticato, usato quando è configurata una chiave.
    if restcountries_api_key:
        try:
            response = requests.get(
                f"https://api.restcountries.com/countries/v5/codes.alpha_2/{code}",
                params={"response_fields": "calling_codes"},
                timeout=timeout,
                headers={
                    "Authorization": f"Bearer {restcountries_api_key}",
                    "User-Agent": "MarketplaceHub-CecotecOrders/1.0",
                },
            )
            response.raise_for_status()
            payload = response.json()
            item = payload[0] if isinstance(payload, list) and payload else payload
            values = item.get("calling_codes") if isinstance(item, Mapping) else None
            if values:
                value = re.sub(r"\D", "", str(values[0]))
                if value:
                    _cache_calling_code(code, value, "restcountries_v5")
                    return value, "restcountries_v5"
        except Exception:
            pass

    fallback = FALLBACK_CALLING_CODES.get(code, "")
    if fallback:
        _cache_calling_code(code, fallback, "fallback_e164")
        return fallback, "fallback_e164"
    return "", "non_trovato"


def normalize_phone(
    phone: Any,
    country_code: Any,
    *,
    restcountries_api_key: str = "",
) -> tuple[str, str]:
    raw = clean_text(phone)
    if not raw:
        return "", "Telefono mancante"

    country = normalize_country_code(country_code)
    prefix, source = calling_code_for_country(
        country, restcountries_api_key=restcountries_api_key
    )
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "", "Telefono non valido"

    if raw.lstrip().startswith("+"):
        return "00" + digits, f"prefisso_da_numero_{source}"
    if digits.startswith("00"):
        return digits, f"gia_internazionale_{source}"
    if prefix and digits.startswith(prefix) and len(digits) >= len(prefix) + 6:
        return "00" + digits, f"prefisso_presente_{source}"
    if not prefix:
        return digits, "Prefisso nazione non trovato"

    national = digits
    # In Italia lo zero dei numeri fissi fa parte del numero significativo.
    if country != "IT":
        national = national.lstrip("0")
    return "00" + prefix + national, source


def _norm_column(value: Any) -> str:
    text = clean_text(value).lower()
    text = text.replace("€", " euro ")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return re.sub(r"_+", "_", text).strip("_")


def _pick_column(frame: pd.DataFrame, candidates: Sequence[str]) -> str | None:
    normalized = {_norm_column(col): col for col in frame.columns}
    for candidate in candidates:
        key = _norm_column(candidate)
        if key in normalized:
            return str(normalized[key])
    for candidate in candidates:
        key = _norm_column(candidate)
        for norm, original in normalized.items():
            if key and (norm.startswith(key + "_") or norm.endswith("_" + key)):
                return str(original)
    return None


def find_cecotec_snapshots(seller_id: int) -> list[dict[str, Any]]:
    _require_db()
    queries = (
        """SELECT sv.id,sv.name,sv.snapshot_path,sv.updated_at,
        pl.id price_list_id,pl.name price_list_name,s.name supplier_name
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE sv.seller_id=? AND lower(s.name) LIKE '%cecotec%'
        ORDER BY sv.updated_at DESC,sv.id DESC""",
        """SELECT sv.id,sv.snapshot_path,sv.updated_at,
        pl.id price_list_id,pl.name price_list_name,s.name supplier_name
        FROM saved_views sv
        JOIN price_lists pl ON pl.id=sv.price_list_id
        JOIN suppliers s ON s.id=pl.supplier_id
        WHERE sv.seller_id=? AND lower(pl.name) LIKE '%cecotec%'
        ORDER BY sv.updated_at DESC,sv.id DESC""",
    )
    for query in queries:
        try:
            result = rows(query, (seller_id,))
            if result:
                return result
        except Exception:
            continue
    return []


def load_cecotec_catalog(snapshot_path: str | Path) -> tuple[dict[str, dict[str, str]], pd.DataFrame, dict[str, str]]:
    path = Path(snapshot_path)
    if not path.exists():
        raise FileNotFoundError(f"Snapshot Cecotec non trovato: {path}")

    suffix = path.suffix.lower()
    if suffix in {".pkl", ".pickle"}:
        frame = pd.read_pickle(path)
    elif suffix == ".csv":
        frame = pd.read_csv(path, dtype=str)
    elif suffix in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, dtype=str)
    else:
        raise ValueError(f"Formato listino non supportato: {suffix}")

    frame = frame.copy()
    frame.columns = [_norm_column(col) for col in frame.columns]
    ean_col = _pick_column(frame, ("ean", "ean13", "barcode", "gtin", "upc"))
    sku_col = _pick_column(
        frame,
        ("sku", "reference", "referencia", "ref", "product_code", "code", "codigo", "codice", "id"),
    )
    name_col = _pick_column(frame, ("name", "product_name", "title", "nome", "nombre_completo"))
    if not ean_col:
        raise ValueError("Nel listino Cecotec non è stata trovata una colonna EAN.")
    if not sku_col:
        raise ValueError("Nel listino Cecotec non è stata trovata una colonna SKU/referenza.")

    catalog: dict[str, dict[str, str]] = {}
    for record in frame.to_dict("records"):
        ean = clean_identifier(record.get(ean_col))
        article_raw = clean_identifier(record.get(sku_col))
        if not ean or not article_raw:
            continue
        catalog.setdefault(
            ean,
            {
                "ean": ean,
                "article_raw": article_raw,
                "article": normalize_cecotec_article(article_raw),
                "product_name": clean_text(record.get(name_col)) if name_col else "",
            },
        )
    return catalog, frame, {"ean_col": ean_col, "sku_col": sku_col, "name_col": name_col or ""}


def load_cecotec_catalogs(
    snapshots: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Carica e unisce più listini/snapshot Cecotec.

    L'ordine di ``snapshots`` stabilisce la priorità: il primo listino selezionato
    prevale quando lo stesso EAN compare in più snapshot. I conflitti non vengono
    nascosti: sono restituiti separatamente per poterli mostrare nell'interfaccia.
    """
    merged: dict[str, dict[str, str]] = {}
    loaded: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []

    for priority, snapshot in enumerate(snapshots):
        catalog, frame, columns = load_cecotec_catalog(snapshot["snapshot_path"])
        source_label = clean_text(snapshot.get("price_list_name")) or "Listino Cecotec"
        source_id = clean_text(snapshot.get("id"))
        loaded.append({
            "snapshot_id": source_id,
            "price_list_id": snapshot.get("price_list_id"),
            "price_list_name": source_label,
            "updated_at": clean_text(snapshot.get("updated_at")),
            "rows": len(frame),
            "indexed": len(catalog),
            "columns": columns,
            "priority": priority + 1,
        })
        for ean, item in catalog.items():
            enriched = dict(item)
            enriched["source_snapshot_id"] = source_id
            enriched["source_price_list_name"] = source_label
            enriched["source_priority"] = str(priority + 1)
            existing = merged.get(ean)
            if existing is None:
                merged[ean] = enriched
                continue
            existing_article = normalize_cecotec_article(existing.get("article"))
            incoming_article = normalize_cecotec_article(enriched.get("article"))
            if existing_article != incoming_article:
                conflicts.append({
                    "ean": ean,
                    "sku_usato": existing_article,
                    "listino_usato": clean_text(existing.get("source_price_list_name")),
                    "sku_alternativo": incoming_article,
                    "listino_alternativo": source_label,
                })

    return merged, loaded, conflicts


def match_catalog(ean: Any, catalog: Mapping[str, Mapping[str, Any]]) -> CatalogMatch:
    normalized_ean = clean_identifier(ean)
    item = catalog.get(normalized_ean)
    if not item:
        return CatalogMatch(normalized_ean, "", "", "", False, "EAN non trovato nel listino Cecotec")
    article_raw = clean_identifier(item.get("article_raw"))
    article = normalize_cecotec_article(item.get("article") or article_raw)
    if not article:
        return CatalogMatch(normalized_ean, article_raw, "", clean_text(item.get("product_name")), False, "SKU Cecotec mancante")
    return CatalogMatch(
        normalized_ean,
        article_raw,
        article,
        clean_text(item.get("product_name")),
        True,
    )


def _kaufland_signed_get(
    credentials: Mapping[str, Any],
    path: str,
    params: Mapping[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    client_key = clean_text(credentials.get("client_key") or credentials.get("shop_client_key"))
    secret_key = clean_text(credentials.get("secret_key") or credentials.get("shop_secret_key"))
    if not client_key or not secret_key:
        raise RuntimeError("Credenziali Kaufland incomplete: client_key/secret_key mancanti.")

    base_url = clean_text(credentials.get("base_url")) or "https://sellerapi.kaufland.com/v2"
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if params:
        query = urlencode([(key, value) for key, value in params.items() if value not in (None, "")], doseq=True)
        if query:
            url += "?" + query
    timestamp = str(int(time.time()))
    signature_text = "\n".join(("GET", url, "", timestamp))
    signature = hmac.new(
        secret_key.encode("utf-8"), signature_text.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers = {
        "Accept": "application/json",
        "Shop-Client-Key": client_key,
        "Shop-Timestamp": timestamp,
        "Shop-Signature": signature,
        "User-Agent": clean_text(credentials.get("user_agent")) or "Inhouse_development",
    }
    partner_key = clean_text(credentials.get("partner_client_key"))
    partner_secret = clean_text(credentials.get("partner_secret_key"))
    if partner_key and partner_secret:
        partner_signature = hmac.new(
            partner_secret.encode("utf-8"), signature_text.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers["Shop-Partner-Client-Key"] = partner_key
        headers["Shop-Partner-Signature"] = partner_signature

    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise RuntimeError("Risposta Kaufland non valida.")
    return dict(payload)


def _parse_address(address: Any) -> dict[str, str]:
    source = address if isinstance(address, Mapping) else {}
    first_name = clean_text(source.get("first_name") or source.get("firstname"))
    last_name = clean_text(source.get("last_name") or source.get("lastname"))
    full_name = clean_text(source.get("full_name") or source.get("name"))
    if not full_name:
        full_name = clean_text(" ".join(part for part in (first_name, last_name) if part))
    company = clean_text(source.get("company_name") or source.get("company"))
    if company and not full_name:
        full_name = company

    street = clean_text(source.get("street") or source.get("street_1") or source.get("address_line_1") or source.get("address1"))
    house_number = clean_text(source.get("house_number") or source.get("street_number"))
    extra = clean_text(source.get("additional_field") or source.get("street_2") or source.get("address_line_2") or source.get("address2"))
    address_text = clean_text(" ".join(part for part in (street, house_number, extra) if part))
    return {
        "customer_name": full_name,
        "address": address_text,
        "postal_code": clean_text(source.get("postcode") or source.get("postal_code") or source.get("zip_code") or source.get("zip")),
        "phone": clean_text(source.get("phone") or source.get("phone_number") or source.get("mobile")),
        "city": clean_text(source.get("city") or source.get("town")),
        "country_code": normalize_country_code(source.get("country") or source.get("country_code") or source.get("country_iso_code")),
        "customer_email": clean_text(source.get("email")),
    }


def _in_date_range(value: Any, date_from: date, date_to: date) -> bool:
    text = clean_text(value)
    if not text:
        return True
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return True
    return date_from <= parsed <= date_to


def _aggregate_order_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in lines:
        key = (
            clean_text(item.get("order_id")),
            clean_text(item.get("composite_sku")),
            clean_text(item.get("raw_status")),
            clean_text(item.get("customer_name")),
            clean_text(item.get("address")),
            clean_text(item.get("postal_code")),
        )
        if key not in grouped:
            grouped[key] = dict(item)
            grouped[key]["quantity"] = int(item.get("quantity") or 1)
            grouped[key]["source_line_ids"] = [clean_text(item.get("order_line_id"))]
        else:
            grouped[key]["quantity"] += int(item.get("quantity") or 1)
            grouped[key]["source_line_ids"].append(clean_text(item.get("order_line_id")))
    output: list[dict[str, Any]] = []
    for item in grouped.values():
        item["order_line_id"] = ",".join(value for value in item.pop("source_line_ids") if value)
        item["row_key"] = make_row_key(
            item.get("marketplace"), item.get("account_id"), item.get("order_id"),
            item.get("order_line_id"), item.get("composite_sku"),
        )
        output.append(item)
    return output



def _kaufland_order_manifest(
    credentials: Mapping[str, Any],
    *,
    date_from: date,
    date_to: date,
    max_rows: int = 10000,
) -> dict[str, dict[str, Any]]:
    """Return current order/storefront metadata from the official /orders API."""
    result: dict[str, dict[str, Any]] = {}
    offset = 0
    while len(result) < max_rows:
        payload = _kaufland_signed_get(
            credentials, "/orders", {"limit": 100, "offset": offset}
        )
        data = payload.get("data") or []
        if not isinstance(data, list) or not data:
            break
        for item in data:
            if not isinstance(item, Mapping):
                continue
            order_id = clean_identifier(item.get("id_order"))
            if not order_id:
                continue
            created = clean_text(item.get("ts_created_iso"))
            if not _in_date_range(created, date_from, date_to):
                continue
            result[order_id] = {
                "storefront": clean_text(item.get("storefront")).lower(),
                "order_created": created,
                "ts_units_updated_iso": clean_text(item.get("ts_units_updated_iso")),
                "raw": dict(item),
            }
        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
        total = int(pagination.get("total") or 0)
        offset += len(data)
        if len(data) < 100 or (total and offset >= total):
            break
    return result


def _kaufland_shipping_addresses(
    credentials: Mapping[str, Any], unit_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    values = list(dict.fromkeys(clean_identifier(value) for value in unit_ids if clean_identifier(value)))
    for index in range(0, len(values), 50):
        chunk = values[index:index + 50]
        payload = _kaufland_signed_get(
            credentials,
            "/shipping-addresses",
            {"order_unit_ids": ",".join(chunk)},
        )
        for entry in payload.get("data") or []:
            if not isinstance(entry, Mapping):
                continue
            unit_id = clean_identifier(entry.get("id_order_unit"))
            address_data = entry.get("address_data") if isinstance(entry.get("address_data"), Mapping) else {}
            address = address_data.get("data") if isinstance(address_data.get("data"), Mapping) else {}
            if unit_id and isinstance(address, Mapping):
                result[unit_id] = {
                    "address": _parse_address(address),
                    "address_type": clean_text(address_data.get("type")),
                    "raw": dict(entry),
                }
    return result

def fetch_kaufland_orders(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    date_from: date,
    date_to: date,
    max_rows: int | None = None,
    statuses: Sequence[str] | None = None,
    include_manifest: bool = True,
) -> list[dict[str, Any]]:
    """Download Kaufland order units.

    ``statuses`` and ``include_manifest`` allow callers such as Packlink to run a
    lightweight synchronization of only the currently actionable states.  The
    historical/full behavior remains the default for the other pages.
    """
    statuses = tuple(statuses or KAUFLAND_ORDER_STATUS_ORDER)
    order_manifest: dict[str, dict[str, Any]] = {}
    if include_manifest:
        try:
            order_manifest = _kaufland_order_manifest(
                credentials, date_from=date_from, date_to=date_to
            )
        except Exception:
            order_manifest = {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    status_errors: list[str] = []
    for status in statuses:
        offset = 0
        while max_rows is None or len(items) < max_rows:
            try:
                payload = _kaufland_signed_get(
                    credentials,
                    "/order-units",
                    {"status": status, "limit": 100, "offset": offset},
                )
            except Exception as exc:
                status_errors.append(f"{status}: {exc}")
                break
            data = payload.get("data") or []
            if not isinstance(data, list) or not data:
                break
            for unit in data:
                if not isinstance(unit, Mapping):
                    continue
                line_id = clean_identifier(unit.get("id_order_unit"))
                if line_id in seen:
                    continue
                seen.add(line_id)
                created = clean_text(unit.get("ts_created_iso") or unit.get("created_at"))
                if not _in_date_range(created, date_from, date_to):
                    continue
                shipping = _parse_address(unit.get("shipping_address") or unit.get("billing_address"))
                buyer = unit.get("buyer") if isinstance(unit.get("buyer"), Mapping) else {}
                if not shipping["customer_email"]:
                    shipping["customer_email"] = clean_text(buyer.get("email"))
                product = unit.get("product") if isinstance(unit.get("product"), Mapping) else {}
                raw_status = clean_text(unit.get("status") or status)
                composite_sku = clean_text(unit.get("id_offer") or unit.get("seller_sku") or unit.get("sku"))
                parsed = parse_composite_sku(composite_sku)
                order_id = clean_identifier(unit.get("id_order"))
                manifest_item = order_manifest.get(order_id, {})
                items.append({
                    "marketplace": "kaufland",
                    "account_id": account_id,
                    "order_id": order_id,
                    "order_line_id": line_id,
                    "order_created": created,
                    "raw_status": raw_status,
                    "normalized_status": normalized_status(raw_status, "kaufland"),
                    "supplier": normalize_supplier(parsed.supplier) if parsed.supplier else "",
                    "composite_sku": composite_sku,
                    "product_title": clean_text(product.get("title") or unit.get("product_title")),
                    "quantity": 1,
                    "storefront": clean_text(
                        unit.get("storefront") or manifest_item.get("storefront")
                    ).lower(),
                    **shipping,
                    "raw_json": dict(unit),
                })
                if max_rows is not None and len(items) >= max_rows:
                    break
            pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
            total = int(pagination.get("total") or 0)
            offset += len(data)
            if len(data) < 100 or (total and offset >= total):
                break
    if not items and status_errors:
        raise RuntimeError("; ".join(status_errors))
    try:
        address_map = _kaufland_shipping_addresses(
            credentials, [clean_identifier(item.get("order_line_id")) for item in items]
        )
    except Exception:
        address_map = {}
    for item in items:
        unit_id = clean_identifier(item.get("order_line_id"))
        address_entry = address_map.get(unit_id)
        if not address_entry:
            continue
        address = address_entry.get("address") or {}
        for field in (
            "customer_name", "address", "postal_code", "phone", "city",
            "country_code", "customer_email",
        ):
            value = clean_text(address.get(field))
            if value:
                item[field] = value
        raw_json = dict(item.get("raw_json") or {})
        raw_json["shipping_address_lookup"] = address_entry.get("raw") or {}
        item["raw_json"] = raw_json

    # Completeness pass: compare the orders manifest with the units downloaded
    # through status pages.  Any order missing from the cache is recovered by
    # exact order number.  This is intentionally independent from accounting,
    # margin or profit/loss information.
    downloaded_order_ids = {
        clean_identifier(item.get("order_id"))
        for item in items
        if clean_identifier(item.get("order_id"))
    }
    missing_order_ids = [
        order_id
        for order_id in order_manifest
        if clean_identifier(order_id) not in downloaded_order_ids
    ] if include_manifest else []
    if missing_order_ids and (max_rows is None or len(items) < max_rows):
        try:
            recovered = fetch_kaufland_orders_by_ids(
                credentials,
                account_id=account_id,
                order_ids=missing_order_ids,
            )
            for recovered_item in recovered:
                if not _in_date_range(
                    recovered_item.get("order_created"), date_from, date_to
                ):
                    continue
                items.append(recovered_item)
                if max_rows is not None and len(items) >= max_rows:
                    break
        except Exception as exc:
            status_errors.append(f"recupero ordini manifest mancanti: {exc}")

    return _aggregate_order_lines(items)



def _payload_data(payload: Any) -> Any:
    if isinstance(payload, Mapping) and "data" in payload:
        return payload.get("data")
    return payload


def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            if isinstance(nested, (Mapping, list, tuple)):
                yield from _walk_mappings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            if isinstance(nested, (Mapping, list, tuple)):
                yield from _walk_mappings(nested)


def _kaufland_shipping_addresses_by_orders(
    credentials: Mapping[str, Any], order_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Lookup delivery addresses by exact Kaufland order number."""
    result: dict[str, list[dict[str, Any]]] = {}
    values = list(dict.fromkeys(
        clean_identifier(value) for value in order_ids if clean_identifier(value)
    ))
    for index in range(0, len(values), 50):
        payload = _kaufland_signed_get(
            credentials,
            "/shipping-addresses",
            {"order_ids": ",".join(values[index:index + 50])},
        )
        for entry in payload.get("data") or []:
            if not isinstance(entry, Mapping):
                continue
            order_id = clean_identifier(entry.get("id_order"))
            if not order_id:
                continue
            address_data = (
                entry.get("address_data")
                if isinstance(entry.get("address_data"), Mapping)
                else {}
            )
            address = (
                address_data.get("data")
                if isinstance(address_data.get("data"), Mapping)
                else {}
            )
            result.setdefault(order_id, []).append({
                "id_order_unit": clean_identifier(entry.get("id_order_unit")),
                "address": _parse_address(address),
                "raw": dict(entry),
            })
    return result


def _normalize_exact_kaufland_order(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    requested_order_id: str,
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize GET /orders/{id_order} without any margin/profit filtering."""
    order_id = clean_identifier(requested_order_id)
    data = _payload_data(payload)
    order_record: dict[str, Any] = {}
    if isinstance(data, Mapping):
        order_record = dict(data)
    elif isinstance(data, list):
        order_record = next(
            (
                dict(item)
                for item in data
                if isinstance(item, Mapping)
                and clean_identifier(item.get("id_order")) == order_id
            ),
            {},
        )

    units: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    for candidate in _walk_mappings(data):
        unit_id = clean_identifier(candidate.get("id_order_unit"))
        if not unit_id or unit_id in seen_units:
            continue
        candidate_order = clean_identifier(
            candidate.get("id_order") or order_record.get("id_order")
        )
        if candidate_order and candidate_order != order_id:
            continue
        seen_units.add(unit_id)
        units.append(dict(candidate))

    try:
        address_entries = _kaufland_shipping_addresses_by_orders(
            credentials, [order_id]
        ).get(order_id, [])
    except Exception:
        address_entries = []
    address_by_unit = {
        clean_identifier(item.get("id_order_unit")): item.get("address") or {}
        for item in address_entries
        if clean_identifier(item.get("id_order_unit"))
    }

    created_default = clean_text(order_record.get("ts_created_iso"))
    storefront_default = clean_text(order_record.get("storefront")).lower()
    output: list[dict[str, Any]] = []

    for unit in units:
        line_id = clean_identifier(unit.get("id_order_unit"))
        shipping = _parse_address(
            unit.get("shipping_address") or unit.get("billing_address")
        )
        for field, value in address_by_unit.get(line_id, {}).items():
            if clean_text(value):
                shipping[field] = clean_text(value)

        buyer = unit.get("buyer") if isinstance(unit.get("buyer"), Mapping) else {}
        if not shipping["customer_email"]:
            shipping["customer_email"] = clean_text(buyer.get("email"))

        product = unit.get("product") if isinstance(unit.get("product"), Mapping) else {}
        raw_status = clean_text(unit.get("status") or order_record.get("status"))
        composite_sku = clean_text(
            unit.get("id_offer")
            or unit.get("seller_sku")
            or unit.get("sku")
            or unit.get("offer_id")
        )
        parsed = parse_composite_sku(composite_sku)
        output.append({
            "marketplace": "kaufland",
            "account_id": int(account_id),
            "order_id": order_id,
            "order_line_id": line_id or f"{order_id}-1",
            "order_created": clean_text(unit.get("ts_created_iso") or created_default),
            "raw_status": raw_status,
            "normalized_status": normalized_status(raw_status, "kaufland"),
            "supplier": normalize_supplier(parsed.supplier) if parsed.supplier else "",
            "composite_sku": composite_sku,
            "product_title": clean_text(
                product.get("title")
                or unit.get("product_title")
                or unit.get("title")
            ),
            "quantity": 1,
            "storefront": clean_text(
                unit.get("storefront") or storefront_default
            ).lower(),
            **shipping,
            "raw_json": {
                "order": order_record,
                "unit": dict(unit),
                "exact_order_lookup": True,
            },
        })

    # Never discard a confirmed exact order solely because its detail payload
    # currently lacks embedded order-unit data.
    if not output and order_record:
        shipping = _parse_address(
            order_record.get("shipping_address")
            or order_record.get("billing_address")
        )
        if address_entries:
            for field, value in (address_entries[0].get("address") or {}).items():
                if clean_text(value):
                    shipping[field] = clean_text(value)
        raw_status = clean_text(order_record.get("status"))
        output.append({
            "marketplace": "kaufland",
            "account_id": int(account_id),
            "order_id": order_id,
            "order_line_id": f"{order_id}-order",
            "order_created": created_default,
            "raw_status": raw_status,
            "normalized_status": normalized_status(raw_status, "kaufland"),
            "supplier": "",
            "composite_sku": "",
            "product_title": clean_text(
                order_record.get("product_title") or "Ordine Kaufland"
            ),
            "quantity": max(
                1, int(parse_decimal(order_record.get("order_units_count")) or 1)
            ),
            "storefront": storefront_default,
            **shipping,
            "raw_json": {
                "order": order_record,
                "exact_order_lookup": True,
                "synthetic_order_line": True,
            },
        })
    return _aggregate_order_lines(output)


def fetch_kaufland_orders_by_ids(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    order_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Fetch exact Kaufland order numbers for Packlink/manual recovery.

    Economic result, margin, purchase cost and accounting state are deliberately
    ignored.  If the order exists for the seller account, it is returned.
    """
    output: list[dict[str, Any]] = []
    for order_id in dict.fromkeys(
        clean_identifier(value) for value in order_ids if clean_identifier(value)
    ):
        try:
            payload = _kaufland_signed_get(
                credentials, f"/orders/{order_id}"
            )
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and int(
                getattr(response, "status_code", 0) or 0
            ) == 404:
                continue
            raise
        output.extend(_normalize_exact_kaufland_order(
            credentials,
            account_id=account_id,
            requested_order_id=order_id,
            payload=payload,
        ))
    return output


def _find_first_mapping(source: Mapping[str, Any], paths: Sequence[Sequence[str]]) -> Mapping[str, Any]:
    for path in paths:
        current: Any = source
        ok = True
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                ok = False
                break
            current = current[key]
        if ok and isinstance(current, Mapping):
            return current
    return {}


def _mirakl_credentials(credentials: Mapping[str, Any]) -> tuple[str, str, str]:
    base_url = clean_text(
        credentials.get("base_url") or credentials.get("api_url") or
        credentials.get("endpoint") or credentials.get("mirakl_url") or
        credentials.get("shop_url")
    ).rstrip("/")
    api_key = clean_text(
        credentials.get("api_key") or credentials.get("token") or
        credentials.get("authorization") or credentials.get("shop_api_key")
    )
    shop_id = clean_identifier(credentials.get("shop_id"))
    if not base_url or not api_key:
        raise RuntimeError("Credenziali Worten incomplete: base_url/api_key mancanti.")
    return base_url, api_key, shop_id


def fetch_worten_orders(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    date_from: date,
    date_to: date,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    base_url, api_key, shop_id = _mirakl_credentials(credentials)
    offset = 0
    items: list[dict[str, Any]] = []
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "MarketplaceHub-CecotecOrders/1.0",
    }
    while max_rows is None or len(items) < max_rows:
        params: dict[str, Any] = {
            "offset": offset,
            "max": 100,
            "start_date": datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
            "end_date": datetime.combine(date_to, datetime.max.time(), tzinfo=timezone.utc).isoformat(),
        }
        if shop_id:
            params["shop_id"] = shop_id
        orders_url = base_url + "/orders" if base_url.lower().endswith("/api") else base_url + "/api/orders"
        response = requests.get(
            orders_url, params=params, headers=headers, timeout=45
        )
        response.raise_for_status()
        payload = response.json()
        orders_payload = payload.get("orders") if isinstance(payload, Mapping) else None
        if not isinstance(orders_payload, list) or not orders_payload:
            break

        for order in orders_payload:
            if not isinstance(order, Mapping):
                continue
            order_id = clean_text(order.get("order_id") or order.get("commercial_id"))
            raw_status = clean_text(order.get("order_state") or order.get("status"))
            created = clean_text(order.get("created_date") or order.get("date_created") or order.get("creation_date"))
            address_source = _find_first_mapping(
                order,
                (
                    ("shipping_address",),
                    ("customer", "shipping_address"),
                    ("order_customer", "shipping_address"),
                    ("customer", "billing_address"),
                    ("billing_address",),
                ),
            )
            address = _parse_address(address_source)
            customer = order.get("customer") if isinstance(order.get("customer"), Mapping) else {}
            if not address["customer_name"]:
                address["customer_name"] = clean_text(
                    customer.get("firstname") or customer.get("first_name") or customer.get("name")
                )
            if not address["customer_email"]:
                address["customer_email"] = clean_text(
                    order.get("customer_notification_email") or customer.get("email")
                )
            order_lines = order.get("order_lines") or order.get("lines") or []
            if not isinstance(order_lines, list):
                continue
            for index, line in enumerate(order_lines):
                if not isinstance(line, Mapping):
                    continue
                line_id = clean_text(
                    line.get("order_line_id") or line.get("id") or f"{order_id}-{index + 1}"
                )
                composite_sku = clean_text(
                    line.get("offer_sku") or line.get("seller_sku") or line.get("shop_sku") or
                    line.get("offer_id") or line.get("sku") or line.get("product_sku")
                )
                parsed = parse_composite_sku(composite_sku)
                quantity = int(parse_decimal(line.get("quantity")) or 1)
                items.append({
                    "marketplace": "worten",
                    "account_id": account_id,
                    "order_id": order_id,
                    "order_line_id": line_id,
                    "order_created": created,
                    "raw_status": raw_status,
                    "normalized_status": normalized_status(raw_status, "worten"),
                    "supplier": normalize_supplier(parsed.supplier) if parsed.supplier else "",
                    "composite_sku": composite_sku,
                    "product_title": clean_text(
                        line.get("product_title") or line.get("product_name") or line.get("title")
                    ),
                    "quantity": max(1, quantity),
                    **address,
                    "raw_json": {"order": dict(order), "line": dict(line)},
                })
                if max_rows is not None and len(items) >= max_rows:
                    break
            if max_rows is not None and len(items) >= max_rows:
                break
        total_count = int(payload.get("total_count") or 0) if isinstance(payload, Mapping) else 0
        offset += len(orders_payload)
        if len(orders_payload) < 100 or (total_count and offset >= total_count):
            break
    return _aggregate_order_lines(items)



def _normalize_worten_order_for_cache(
    order: Mapping[str, Any],
    *,
    account_id: int,
) -> list[dict[str, Any]]:
    """Normalize one Mirakl/Worten order without economic eligibility checks."""
    order_id = clean_text(order.get("order_id") or order.get("commercial_id"))
    if not order_id:
        return []
    raw_status = clean_text(order.get("order_state") or order.get("status"))
    created = clean_text(
        order.get("created_date")
        or order.get("date_created")
        or order.get("creation_date")
    )
    address_source = _find_first_mapping(
        order,
        (
            ("shipping_address",),
            ("customer", "shipping_address"),
            ("order_customer", "shipping_address"),
            ("customer", "billing_address"),
            ("billing_address",),
        ),
    )
    address = _parse_address(address_source)
    customer = order.get("customer") if isinstance(order.get("customer"), Mapping) else {}
    if not address["customer_name"]:
        first = clean_text(
            customer.get("firstname") or customer.get("first_name")
        )
        last = clean_text(
            customer.get("lastname") or customer.get("last_name")
        )
        address["customer_name"] = clean_text(
            " ".join(part for part in (first, last) if part)
            or customer.get("name")
        )
    if not address["customer_email"]:
        address["customer_email"] = clean_text(
            order.get("customer_notification_email")
            or customer.get("email")
        )

    order_lines = order.get("order_lines") or order.get("lines") or []
    if not isinstance(order_lines, list):
        order_lines = []
    output: list[dict[str, Any]] = []
    for index, line in enumerate(order_lines):
        if not isinstance(line, Mapping):
            continue
        line_id = clean_text(
            line.get("order_line_id")
            or line.get("id")
            or f"{order_id}-{index + 1}"
        )
        composite_sku = clean_text(
            line.get("offer_sku")
            or line.get("seller_sku")
            or line.get("shop_sku")
            or line.get("offer_id")
            or line.get("sku")
            or line.get("product_sku")
        )
        parsed = parse_composite_sku(composite_sku)
        quantity = int(parse_decimal(line.get("quantity")) or 1)
        output.append({
            "marketplace": "worten",
            "account_id": int(account_id),
            "order_id": order_id,
            "order_line_id": line_id,
            "order_created": created,
            "raw_status": raw_status,
            "normalized_status": normalized_status(raw_status, "worten"),
            "supplier": normalize_supplier(parsed.supplier) if parsed.supplier else "",
            "composite_sku": composite_sku,
            "product_title": clean_text(
                line.get("product_title")
                or line.get("product_name")
                or line.get("title")
            ),
            "quantity": max(1, quantity),
            **address,
            "raw_json": {
                "order": dict(order),
                "line": dict(line),
                "exact_order_lookup": True,
            },
        })

    # Keep an exact existing order selectable even if Mirakl temporarily returns
    # no lines in the detail/list response.
    if not output:
        output.append({
            "marketplace": "worten",
            "account_id": int(account_id),
            "order_id": order_id,
            "order_line_id": f"{order_id}-order",
            "order_created": created,
            "raw_status": raw_status,
            "normalized_status": normalized_status(raw_status, "worten"),
            "supplier": "",
            "composite_sku": "",
            "product_title": "Ordine Worten",
            "quantity": 1,
            **address,
            "raw_json": {
                "order": dict(order),
                "exact_order_lookup": True,
                "synthetic_order_line": True,
            },
        })
    return _aggregate_order_lines(output)


def fetch_worten_orders_by_ids(
    credentials: Mapping[str, Any],
    *,
    account_id: int,
    order_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Fetch exact Worten/Mirakl orders using OR11 order_ids filters.

    The lookup is by marketplace order identifier only; margin, cost and
    profitability are not consulted.
    """
    base_url, api_key, shop_id = _mirakl_credentials(credentials)
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "MarketplaceHub-PacklinkExactOrders/1.0",
    }
    orders_url = (
        base_url + "/orders"
        if base_url.lower().endswith("/api")
        else base_url + "/api/orders"
    )
    requested = list(dict.fromkeys(
        clean_text(value) for value in order_ids if clean_text(value)
    ))
    found_orders: dict[str, Mapping[str, Any]] = {}

    for index in range(0, len(requested), 100):
        chunk = requested[index:index + 100]
        params: dict[str, Any] = {
            "order_ids": ",".join(chunk),
            "max": 100,
            "offset": 0,
        }
        if shop_id:
            params["shop_id"] = shop_id
        response = requests.get(
            orders_url, params=params, headers=headers, timeout=45
        )
        response.raise_for_status()
        payload = response.json()
        orders_payload = payload.get("orders") if isinstance(payload, Mapping) else []
        for order in orders_payload or []:
            if not isinstance(order, Mapping):
                continue
            key = clean_text(order.get("order_id") or order.get("commercial_id"))
            if key:
                found_orders[key] = order

        unresolved = [
            value for value in chunk
            if value not in found_orders
        ]
        # Some Mirakl configurations expose the seller-facing reference rather
        # than the internal order_id in uploaded operational files.
        if unresolved:
            params = {
                "order_references_for_seller": ",".join(unresolved),
                "max": 100,
                "offset": 0,
            }
            if shop_id:
                params["shop_id"] = shop_id
            response = requests.get(
                orders_url, params=params, headers=headers, timeout=45
            )
            response.raise_for_status()
            payload = response.json()
            orders_payload = payload.get("orders") if isinstance(payload, Mapping) else []
            for order in orders_payload or []:
                if not isinstance(order, Mapping):
                    continue
                key = clean_text(order.get("order_id") or order.get("commercial_id"))
                if key:
                    found_orders[key] = order

    output: list[dict[str, Any]] = []
    for order in found_orders.values():
        output.extend(_normalize_worten_order_for_cache(
            order, account_id=account_id
        ))
    return output


def upsert_order_cache(
    seller_id: int,
    account_id: int,
    marketplace: str,
    order_lines: Iterable[Mapping[str, Any]],
) -> int:
    """Persist normalized orders in one transaction whenever possible.

    Previous releases opened one database transaction for every order row.  On
    SQLite that was needlessly expensive and made the Packlink synchronization
    appear frozen with larger batches.
    """
    _require_db()
    synced_at = now_iso()
    values: list[tuple[Any, ...]] = []
    for item in order_lines:
        row_key = clean_text(item.get("row_key")) or make_row_key(
            marketplace, account_id, item.get("order_id"), item.get("order_line_id"), item.get("composite_sku")
        )
        values.append((
            seller_id, account_id, marketplace, row_key,
            clean_text(item.get("order_id")), clean_text(item.get("order_line_id")),
            clean_text(item.get("order_created")), clean_text(item.get("raw_status")),
            clean_text(item.get("normalized_status")), clean_text(item.get("supplier")),
            clean_text(item.get("composite_sku")), clean_text(item.get("product_title")),
            int(item.get("quantity") or 1), clean_text(item.get("customer_name")),
            clean_text(item.get("address")), clean_text(item.get("postal_code")),
            clean_text(item.get("phone")), clean_text(item.get("city")),
            clean_text(item.get("storefront") or item.get("marketplace_storefront")).lower(),
            normalize_country_code(item.get("country_code")), clean_text(item.get("customer_email")),
            json_text(item.get("raw_json") or {}), synced_at,
        ))
    if not values:
        return 0
    sql = """INSERT INTO cecotec_order_cache(
        seller_id,marketplace_account_id,marketplace,row_key,order_id,order_line_id,
        order_created,raw_status,normalized_status,supplier,composite_sku,product_title,
        quantity,customer_name,address,postal_code,phone,city,storefront,country_code,customer_email,
        raw_json,synced_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(seller_id,marketplace_account_id,marketplace,row_key) DO UPDATE SET
        order_id=excluded.order_id,order_line_id=excluded.order_line_id,
        order_created=excluded.order_created,raw_status=excluded.raw_status,
        normalized_status=excluded.normalized_status,supplier=excluded.supplier,
        composite_sku=excluded.composite_sku,product_title=excluded.product_title,
        quantity=excluded.quantity,customer_name=excluded.customer_name,address=excluded.address,
        postal_code=excluded.postal_code,phone=excluded.phone,city=excluded.city,
        storefront=excluded.storefront,country_code=excluded.country_code,
        customer_email=excluded.customer_email,raw_json=excluded.raw_json,synced_at=excluded.synced_at"""
    if execute_many is not None:
        execute_many(sql, values)
    else:  # pragma: no cover - standalone compatibility
        for params in values:
            execute(sql, params)
    return len(values)

def update_cached_order_statuses(
    seller_id: int,
    account_id: int,
    marketplace: str,
    verified_rows: Iterable[Mapping[str, Any]],
) -> int:
    """Persist live marketplace states without replacing order/customer data."""
    _require_db()
    count = 0
    checked_at = now_iso()
    for item in verified_rows:
        row_key = clean_text(item.get("row_key"))
        if not row_key or not bool(item.get("live_verified")):
            continue
        execute(
            """UPDATE cecotec_order_cache
            SET raw_status=?,normalized_status=?,
                customer_name=CASE WHEN ?<>'' THEN ? ELSE customer_name END,
                address=CASE WHEN ?<>'' THEN ? ELSE address END,
                postal_code=CASE WHEN ?<>'' THEN ? ELSE postal_code END,
                phone=CASE WHEN ?<>'' THEN ? ELSE phone END,
                city=CASE WHEN ?<>'' THEN ? ELSE city END,
                storefront=CASE WHEN ?<>'' THEN ? ELSE storefront END,
                country_code=CASE WHEN ?<>'' THEN ? ELSE country_code END,
                synced_at=?
            WHERE seller_id=? AND marketplace_account_id=? AND marketplace=? AND row_key=?""",
            (
                clean_text(item.get("live_raw_status") or item.get("raw_status")),
                clean_text(item.get("live_macro_status") or item.get("normalized_status")),
                clean_text(item.get("customer_name")), clean_text(item.get("customer_name")),
                clean_text(item.get("address")), clean_text(item.get("address")),
                clean_text(item.get("postal_code")), clean_text(item.get("postal_code")),
                clean_text(item.get("phone")), clean_text(item.get("phone")),
                clean_text(item.get("city")), clean_text(item.get("city")),
                clean_text(item.get("marketplace_storefront") or item.get("storefront")).lower(),
                clean_text(item.get("marketplace_storefront") or item.get("storefront")).lower(),
                normalize_country_code(item.get("country_code")),
                normalize_country_code(item.get("country_code")),
                checked_at,
                seller_id,
                account_id,
                marketplace,
                row_key,
            ),
        )
        count += 1
    return count


def cached_order_cache_info(
    seller_id: int, account_id: int, marketplace: str,
) -> dict[str, Any]:
    """Cheap metadata about the persistent order cache for one account."""
    _require_db()
    found = rows(
        """SELECT COUNT(*) AS row_count,
        MIN(substr(order_created,1,10)) AS first_order_date,
        MAX(substr(order_created,1,10)) AS last_order_date,
        MAX(synced_at) AS last_synced_at
        FROM cecotec_order_cache
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?""",
        (seller_id, account_id, marketplace),
    )
    return dict(found[0]) if found else {
        "row_count": 0, "first_order_date": "", "last_order_date": "", "last_synced_at": ""
    }


def cached_orders(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict[str, Any]]:
    """Read orders from the persistent local/PostgreSQL cache.

    A date range is applied in SQL instead of loading the complete order history
    on every Streamlit rerun.  Omitting the dates preserves the legacy behavior.
    """
    _require_db()
    clauses = [
        "seller_id=?", "marketplace_account_id=?", "marketplace=?",
    ]
    params: list[Any] = [seller_id, account_id, marketplace]
    if date_from is not None:
        clauses.append("substr(order_created,1,10)>=?")
        params.append(date_from.isoformat())
    if date_to is not None:
        clauses.append("substr(order_created,1,10)<=?")
        params.append(date_to.isoformat())
    return rows(
        "SELECT * FROM cecotec_order_cache WHERE "
        + " AND ".join(clauses)
        + " ORDER BY order_created DESC,order_id,row_key",
        tuple(params),
    )

def cached_orders_page(
    seller_id: int,
    account_id: int,
    marketplace: str,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    statuses: Sequence[str] | None = None,
    suppliers: Sequence[str] | None = None,
    search: str = "",
    limit: int = 250,
    offset: int = 0,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Bounded server-side page for normalized Kaufland/Worten order caches."""
    _require_db()
    clauses = ["seller_id=?", "marketplace_account_id=?", "marketplace=?"]
    params: list[Any] = [seller_id, account_id, marketplace]
    if date_from is not None:
        clauses.append("order_created>=?")
        params.append(date_from.isoformat())
    if date_to is not None:
        clauses.append("order_created<?")
        params.append((date_to + timedelta(days=1)).isoformat())

    def add_in(column: str, values: Sequence[str] | None) -> None:
        clean = [clean_text(value) for value in (values or []) if clean_text(value)]
        if clean:
            clauses.append(f"{column} IN ({','.join('?' for _ in clean)})")
            params.extend(clean)

    add_in("normalized_status", statuses)
    add_in("supplier", suppliers)
    term = clean_text(search).lower()
    if term:
        pattern = f"%{term}%"
        clauses.append(
            "(LOWER(COALESCE(order_id,'')) LIKE ? OR "
            "LOWER(COALESCE(order_line_id,'')) LIKE ? OR "
            "LOWER(COALESCE(composite_sku,'')) LIKE ? OR "
            "LOWER(COALESCE(product_title,'')) LIKE ? OR "
            "LOWER(COALESCE(customer_name,'')) LIKE ?)"
        )
        params.extend([pattern] * 5)

    where_sql = " AND ".join(clauses)
    total_rows = rows(
        f"SELECT COUNT(*) AS row_count FROM cecotec_order_cache WHERE {where_sql}",
        tuple(params),
    )
    total = int(total_rows[0].get("row_count") or 0) if total_rows else 0
    safe_limit = max(1, min(int(limit or 250), 2000))
    safe_offset = max(0, int(offset or 0))
    projection = "*" if include_raw else """
        seller_id,marketplace_account_id,marketplace,row_key,order_id,order_line_id,
        order_created,raw_status,normalized_status,supplier,composite_sku,
        product_title,quantity,customer_name,address,postal_code,phone,city,
        storefront,country_code,customer_email,synced_at
    """
    items = rows(
        f"SELECT {projection} FROM cecotec_order_cache WHERE {where_sql} "
        "ORDER BY order_created DESC,order_id,row_key LIMIT ? OFFSET ?",
        tuple(params) + (safe_limit, safe_offset),
    )
    return {
        "items": items,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "has_more": safe_offset + len(items) < total,
    }


def cached_order_facets(
    seller_id: int, account_id: int, marketplace: str
) -> dict[str, Any]:
    """Small filter metadata query for API/UI controls."""
    _require_db()
    scope = "seller_id=? AND marketplace_account_id=? AND marketplace=?"
    base = (seller_id, account_id, marketplace)

    def facet(column: str) -> list[str]:
        return [
            clean_text(item.get("value"))
            for item in rows(
                f"SELECT DISTINCT {column} AS value FROM cecotec_order_cache "
                f"WHERE {scope} AND COALESCE({column},'')<>'' ORDER BY {column}",
                base,
            )
            if clean_text(item.get("value"))
        ]

    info = cached_order_cache_info(seller_id, account_id, marketplace)
    info["statuses"] = facet("normalized_status")
    info["suppliers"] = facet("supplier")
    info["countries"] = facet("country_code")
    return info


def delete_cached_range(
    seller_id: int,
    account_id: int,
    marketplace: str,
    date_from: date,
    date_to: date,
) -> None:
    _require_db()
    execute(
        """DELETE FROM cecotec_order_cache
        WHERE seller_id=? AND marketplace_account_id=? AND marketplace=?
        AND substr(order_created,1,10)>=? AND substr(order_created,1,10)<=?""",
        (seller_id, account_id, marketplace, date_from.isoformat(), date_to.isoformat()),
    )


def build_cecotec_export(
    selected_lines: Sequence[Mapping[str, Any]],
    catalog: Mapping[str, Mapping[str, Any]],
    *,
    marketplace_label: str,
    restcountries_api_key: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid_rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for item in selected_lines:
        parsed = parse_composite_sku(item.get("composite_sku"))
        reasons: list[str] = []
        warnings: list[str] = []

        # Il file Cecotec deve essere generato anche quando l'ordine non contiene
        # un EAN utilizzabile oppure l'EAN non è presente nel listino. In questi
        # casi la colonna ``article`` resta vuota e verrà compilata manualmente.
        # Un fornitore esplicitamente diverso da Cecotec resta invece escluso.
        supplier = normalize_supplier(parsed.supplier or item.get("supplier"))
        if supplier and "cecotec" not in supplier:
            reasons.append("Fornitore diverso da Cecotec")
        elif not supplier:
            warnings.append("Fornitore non riconosciuto: verificare che la riga appartenga a Cecotec")

        product_code = clean_identifier(parsed.product_code)
        if not parsed.valid:
            warnings.append(parsed.error or "SKU composito non valido")

        if product_code:
            match = match_catalog(product_code, catalog)
        else:
            match = CatalogMatch("", "", "", "", False, "EAN mancante")

        if not match.matched:
            warnings.append(
                f"{match.error}; SKU Cecotec lasciato vuoto per compilazione manuale"
            )

        phone, phone_note = normalize_phone(
            item.get("phone"), item.get("country_code"),
            restcountries_api_key=restcountries_api_key,
        )
        customer_name = clean_text(item.get("customer_name"))
        address = clean_text(item.get("address"))
        postal_code = clean_text(item.get("postal_code"))
        city = clean_text(item.get("city"))
        country_code = normalize_country_code(item.get("country_code"))
        email = clean_text(item.get("customer_email"))
        quantity = max(1, int(item.get("quantity") or 1))

        if not phone:
            warnings.append(phone_note)
        if not customer_name:
            warnings.append("Nome cliente mancante")
        if not address:
            warnings.append("Indirizzo mancante")
        if not postal_code:
            warnings.append("CAP mancante")
        if not city:
            warnings.append("Città mancante")
        if not country_code:
            warnings.append("Nazione mancante")
        if not email:
            warnings.append("Email mancante")

        issue = {
            "row_key": clean_text(item.get("row_key")),
            "order_id": clean_text(item.get("order_id")),
            "order_line_id": clean_text(item.get("order_line_id")),
            "supplier": supplier,
            "composite_sku": parsed.raw,
            "ean": product_code,
            "article": match.article if match.matched else "",
            "errors": reasons,
            "warnings": warnings,
            "phone_note": phone_note,
            "exportable": not reasons,
        }
        issues.append(issue)
        if reasons:
            continue

        valid_rows.append({
            "order": "",
            "article": match.article if match.matched else "",
            "quantity": quantity,
            "customer_name": customer_name,
            "nif": "",
            "attention_of": customer_name,
            "address": address,
            "postal_code": postal_code,
            "phone": phone,
            "city": city,
            "country_code": country_code,
            "customer_email": email,
            "cash_on_delivery": 0,
            "comment": f"{marketplace_label} {clean_text(item.get('order_id'))}".strip(),
            "price": 0,
            "discount": 0,
            "addressee_order_number": "",
            "delivery_type": 0,
            "_row_key": clean_text(item.get("row_key")),
            "_order_id": clean_text(item.get("order_id")),
            "_order_line_id": clean_text(item.get("order_line_id")),
            "_ean": product_code,
        })
    return valid_rows, issues


def export_excel_bytes(export_rows: Sequence[Mapping[str, Any]], file_format: str = "xlsx") -> bytes:
    fmt = clean_text(file_format).lower().lstrip(".") or "xlsx"
    if fmt == "xlsx":
        try:
            import xlsxwriter
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Manca la dipendenza xlsxwriter.") from exc

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(
            output,
            {"in_memory": True, "strings_to_urls": False, "constant_memory": False},
        )
        worksheet = workbook.add_worksheet("Sheet1")
        header_format = workbook.add_format({"bold": True, "border": 1, "align": "center"})
        text_format = workbook.add_format({"num_format": "@"})
        int_format = workbook.add_format({"num_format": "0"})
        for col_index, header in enumerate(CECOTEC_COLUMNS):
            worksheet.write(0, col_index, header, header_format)
        for row_index, item in enumerate(export_rows, start=1):
            for col_index, column in enumerate(CECOTEC_COLUMNS):
                value = item.get(column, "")
                if column in {"quantity", "cash_on_delivery", "price", "discount", "delivery_type"}:
                    worksheet.write_number(row_index, col_index, float(value or 0), int_format)
                else:
                    worksheet.write_string(row_index, col_index, clean_text(value), text_format)
        widths = {
            "A": 11, "B": 12, "C": 10, "D": 28, "E": 14, "F": 28,
            "G": 42, "H": 14, "I": 20, "J": 22, "K": 13, "L": 34,
            "M": 17, "N": 28, "O": 10, "P": 11, "Q": 25, "R": 15,
        }
        for column_letter, width in widths.items():
            worksheet.set_column(f"{column_letter}:{column_letter}", width)
        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, max(0, len(export_rows)), len(CECOTEC_COLUMNS) - 1)
        workbook.close()
        return output.getvalue()

    if fmt == "xls":
        try:
            import xlwt
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Per il formato .xls installare la dipendenza xlwt.") from exc
        workbook = xlwt.Workbook(encoding="utf-8")
        worksheet = workbook.add_sheet("Sheet1")
        header_style = xlwt.easyxf("font: bold on; borders: bottom thin, top thin, left thin, right thin; align: horiz center")
        text_style = xlwt.easyxf("num_format_str: @")
        for col_index, header in enumerate(CECOTEC_COLUMNS):
            worksheet.write(0, col_index, header, header_style)
        for row_index, item in enumerate(export_rows, start=1):
            for col_index, column in enumerate(CECOTEC_COLUMNS):
                value = item.get(column, "")
                if column in {"quantity", "cash_on_delivery", "price", "discount", "delivery_type"}:
                    worksheet.write(row_index, col_index, int(float(value or 0)))
                else:
                    worksheet.write(row_index, col_index, clean_text(value), text_style)
        output = io.BytesIO()
        workbook.save(output)
        return output.getvalue()

    raise ValueError("Formato non supportato: scegliere xlsx oppure xls.")


def previous_exports_for_rows(
    seller_id: int,
    account_id: int,
    marketplace: str,
    row_keys: Sequence[str],
) -> list[dict[str, Any]]:
    _require_db()
    keys = list(dict.fromkeys(clean_text(key) for key in row_keys if clean_text(key)))
    if not keys:
        return []
    found: list[dict[str, Any]] = []
    # SQLite può avere un limite di circa 999 parametri per query.
    for start in range(0, len(keys), 800):
        chunk = keys[start:start + 800]
        placeholders = ",".join("?" for _ in chunk)
        found.extend(rows(
            f"""SELECT i.row_key,i.order_id,i.order_line_id,i.created_at,
            e.export_id,e.file_name,e.file_path
            FROM cecotec_order_export_items i
            JOIN cecotec_order_exports e ON e.export_id=i.export_id
            WHERE i.seller_id=? AND i.marketplace_account_id=? AND i.marketplace=?
            AND i.row_key IN ({placeholders})
            ORDER BY i.created_at DESC""",
            (seller_id, account_id, marketplace, *chunk),
        ))
    found.sort(key=lambda item: clean_text(item.get("created_at")), reverse=True)
    return found


def apply_duplicate_generation_choice(
    selected_lines: Sequence[Mapping[str, Any]],
    valid_rows: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    previously_generated_keys: Sequence[str] | set[str],
    choice: str,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Return the rows covered by the duplicate-handling choice.

    ``new_only`` excludes rows already present in a previous Cecotec export,
    while ``all`` keeps the complete current selection.  The same scope is
    applied to selected order lines, exportable rows and validation issues so
    the archive counters remain coherent with the generated file.
    """
    normalized_choice = clean_text(choice).lower()
    if normalized_choice not in {"new_only", "all"}:
        raise ValueError("Scelta di generazione non valida.")

    selected_list = list(selected_lines)
    valid_list = list(valid_rows)
    issues_list = list(issues)
    if normalized_choice == "all":
        return selected_list, valid_list, issues_list

    generated = {clean_text(value) for value in previously_generated_keys if clean_text(value)}
    allowed_keys = {
        clean_text(item.get("row_key"))
        for item in selected_list
        if clean_text(item.get("row_key")) not in generated
    }
    return (
        [item for item in selected_list if clean_text(item.get("row_key")) in allowed_keys],
        [item for item in valid_list if clean_text(item.get("_row_key")) in allowed_keys],
        [item for item in issues_list if clean_text(item.get("row_key")) in allowed_keys],
    )


def save_export(
    *,
    seller_id: int,
    account_id: int,
    marketplace: str,
    file_name: str,
    file_format: str,
    file_bytes: bytes,
    selected_lines: Sequence[Mapping[str, Any]],
    valid_rows: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    _require_db()
    export_id = uuid.uuid4().hex
    archive_root = Path(base_dir) if base_dir is not None else DATA_DIR / "cecotec_orders"
    root = archive_root / str(seller_id) / marketplace
    root.mkdir(parents=True, exist_ok=True)
    path = root / file_name
    path.write_bytes(file_bytes)
    created_at = now_iso()
    excluded_rows = sum(1 for issue in issues if not bool(issue.get("exportable")))
    execute(
        """INSERT INTO cecotec_order_exports(
        export_id,seller_id,marketplace_account_id,marketplace,file_name,file_format,
        file_path,selected_rows,exported_rows,excluded_rows,details_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            export_id, seller_id, account_id, marketplace, file_name, file_format,
            str(path), len(selected_lines), len(valid_rows), excluded_rows,
            json_text({"issues": list(issues)}), created_at,
        ),
    )
    selected_by_key = {clean_text(item.get("row_key")): item for item in selected_lines}
    # Sono marcate come “già generate” soltanto le righe realmente incluse nel file.
    # Le righe escluse restano nello storico errori ma possono essere corrette e generate dopo.
    for valid in valid_rows:
        row_key = clean_text(valid.get("_row_key"))
        selected = selected_by_key.get(row_key, {})
        execute(
            """INSERT INTO cecotec_order_export_items(
            export_id,seller_id,marketplace_account_id,marketplace,row_key,order_id,
            order_line_id,ean,article,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                export_id, seller_id, account_id, marketplace, row_key,
                clean_text(selected.get("order_id") or valid.get("_order_id")),
                clean_text(selected.get("order_line_id") or valid.get("_order_line_id")),
                clean_text(valid.get("_ean")), clean_text(valid.get("article")), created_at,
            ),
        )
    return {
        "export_id": export_id,
        "file_name": file_name,
        "file_path": str(path),
        "file_format": file_format,
        "selected_rows": len(selected_lines),
        "exported_rows": len(valid_rows),
        "excluded_rows": excluded_rows,
        "created_at": created_at,
    }


def export_history(
    seller_id: int,
    account_id: int | None = None,
    marketplace: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    _require_db()
    clauses = ["seller_id=?"]
    params: list[Any] = [seller_id]
    if account_id is not None:
        clauses.append("marketplace_account_id=?")
        params.append(account_id)
    if marketplace:
        clauses.append("marketplace=?")
        params.append(marketplace)
    params.append(int(limit))
    return rows(
        f"""SELECT * FROM cecotec_order_exports
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC LIMIT ?""",
        tuple(params),
    )


def default_file_name(marketplace: str, file_format: str) -> str:
    label = "Kaufland" if marketplace.lower() == "kaufland" else "Worten"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"Ordini_Cecotec_{label}_{timestamp}.{file_format.lower().lstrip('.')}"
