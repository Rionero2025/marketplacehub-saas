from __future__ import annotations

import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from services.cecotec_orders import clean_text, normalize_country_code
from services.packlink import normalize_sender_address, packlink_destination_address, package_payload

PACKLINK_CSV_VERSION = 271

# Header copied byte-for-byte (text-wise) from the official Italian Packlink PRO
# csv_pro template supplied by Packlink/user. Do not rename, reorder or remove columns.
PACKLINK_CSV_HEADERS: tuple[str, ...] = (
    "Numero di ordine",
    "nome mittente",
    "Cognome mittente",
    "Azienda mittente",
    "Indirizzo Di Spedizione 1",
    "Indirizzo Di Spedizione 2",
    "CAP Spedizione",
    "citta Spedizione",
    "provincia di Spedizione",
    "Paese di spedizione",
    "Telefono spedizione",
    "Email Spedizione",
    "Nome destinatario",
    "Cognome destinatario",
    "Azienda destinatario",
    "Indirizzo di consegna 1",
    "Indirizzo di consegna 2",
    "CAP di consegna",
    "citta di consegna",
    "provincia di consegna",
    "Paese di consegna",
    "Telefono di consegna",
    "Email di consegna",
    "assicurazione",
    "Titolo dell'oggetto",
    "Valore merce",
    "Larghezza oggetto",
    "Altezza oggetto",
    "Lughezza oggetto",
    "Peso dell'oggetto",
)


# Country-aware postal-code normalisation used before writing Packlink CSV.
# The rules follow UPU/libaddressinput country metadata. We intentionally
# canonicalise separators only where the national format defines them, while
# preserving other valid alphanumeric codes. This avoids turning, for example,
# Slovak 040 11 into the compact 04011 that Packlink may reject.
_POSTAL_FIXED_DIGITS: dict[str, int] = {
    # Europe / Packlink core markets
    "AL": 4, "AT": 4, "BA": 5, "BE": 4, "BG": 4, "BY": 6, "CH": 4,
    "CY": 4, "DE": 5, "DK": 4, "EE": 5, "ES": 5, "FI": 5, "FO": 3,
    "FR": 5, "GE": 4, "HR": 5, "HU": 4, "IS": 3, "IT": 5, "LI": 4,
    "LT": 5, "LU": 4, "MC": 5, "ME": 5, "MK": 4, "NO": 4, "RO": 6,
    "RS": 5, "SI": 4, "SM": 5, "TR": 5, "UA": 5, "MD": 4, "XK": 5,
    # Americas
    "CL": 7, "CO": 6, "CR": 5, "DO": 5, "EC": 6, "GT": 5, "MX": 5,
    "PE": 5, "UY": 5, "VE": 4,
    # Asia / Oceania / Africa commonly reachable through Packlink services
    "AF": 4, "AM": 4, "AU": 4, "AZ": 4, "BD": 4, "BT": 5, "CN": 6, "DZ": 5,
    "EG": 5, "ID": 5, "IR": 10, "IL": 7, "IN": 6, "JO": 5, "KE": 5, "KR": 5,
    "KZ": 6, "LK": 5, "MA": 5, "MV": 5, "MY": 5, "NP": 5, "NZ": 4, "PK": 5,
    "PH": 4, "RU": 6, "SA": 5, "SG": 6, "TH": 5, "TN": 4, "VN": 6, "ZA": 4,
}

_POSTAL_FORMAT_HINTS: dict[str, str] = {
    "AD": "AD999",
    "BR": "99999-999",
    "CA": "A9A 9A9",
    "CZ": "999 99",
    "GB": "AA9A 9AA",
    "GR": "999 99",
    "IE": "A99 A9A9",
    "JP": "999-9999",
    "LV": "LV-9999",
    "MT": "AAA 9999",
    "NL": "9999 AA",
    "PL": "99-999",
    "PT": "9999-999",
    "SE": "999 99",
    "SK": "999 99",
    "US": "99999 or 99999-9999",
}


def _postal_clean(value: Any) -> str:
    text = clean_text(value).upper()
    text = (
        text.replace("\u00a0", " ")
        .replace("\u202f", " ")
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", text).strip()


def _postal_compact(value: str) -> str:
    return re.sub(r"[\s-]+", "", value.upper())


def normalize_packlink_postal_code(country: Any, value: Any) -> str:
    """Return a Packlink/UPU-compatible display form for a postcode.

    The function is deliberately deterministic and offline: no package install,
    no network request and no Packlink API call is needed when exporting CSV.
    Leading zeros are restored for countries with a fixed numeric postcode
    length when the source has accidentally lost them. Unknown/variable formats
    are preserved instead of being destructively rewritten.
    """
    country_code = normalize_country_code(country)
    cleaned = _postal_clean(value)
    if not cleaned:
        return ""
    compact = _postal_compact(cleaned)

    # Common international prefixes that are part of the canonical postcode.
    if country_code == "AD":
        digits = compact[2:] if compact.startswith("AD") else compact
        if digits.isdigit() and len(digits) <= 3:
            return "AD" + digits.zfill(3)
        return cleaned
    if country_code == "LV":
        digits = compact[2:] if compact.startswith("LV") else compact
        if digits.isdigit() and len(digits) <= 4:
            return "LV-" + digits.zfill(4)
        return cleaned

    # National formats where a separator is significant/canonical.
    if country_code in {"SK", "CZ", "GR"}:
        digits = compact
        if digits.isdigit() and len(digits) <= 5:
            digits = digits.zfill(5)
            return f"{digits[:3]} {digits[3:]}"
        return cleaned
    if country_code == "SE":
        digits = compact
        if digits.isdigit() and len(digits) <= 5:
            digits = digits.zfill(5)
            return f"{digits[:3]} {digits[3:]}"
        return cleaned
    if country_code == "PL":
        digits = compact
        if digits.isdigit() and len(digits) <= 5:
            digits = digits.zfill(5)
            return f"{digits[:2]}-{digits[2:]}"
        return cleaned
    if country_code == "PT":
        digits = compact
        if digits.isdigit() and len(digits) <= 7:
            digits = digits.zfill(7)
            return f"{digits[:4]}-{digits[4:]}"
        return cleaned
    if country_code == "BR":
        digits = compact
        if digits.isdigit() and len(digits) <= 8:
            digits = digits.zfill(8)
            return f"{digits[:5]}-{digits[5:]}"
        return cleaned
    if country_code == "JP":
        digits = compact
        if digits.isdigit() and len(digits) <= 7:
            digits = digits.zfill(7)
            return f"{digits[:3]}-{digits[3:]}"
        return cleaned

    # Alphanumeric countries.
    if country_code == "NL" and re.fullmatch(r"\d{4}[A-Z]{2}", compact):
        return f"{compact[:4]} {compact[4:]}"
    if country_code == "CA" and re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", compact):
        return f"{compact[:3]} {compact[3:]}"
    if country_code == "IE" and re.fullmatch(r"[A-Z0-9]{7}", compact):
        return f"{compact[:3]} {compact[3:]}"
    if country_code == "MT" and re.fullmatch(r"[A-Z]{3}\d{2,4}", compact):
        return f"{compact[:3]} {compact[3:]}"
    if country_code in {"GB", "GG", "JE", "IM"}:
        # UK-family postcodes conventionally have one space before the inward
        # three-character code. BFPO codes are left readable as supplied.
        if compact.startswith("BFPO"):
            return cleaned
        if len(compact) >= 5 and re.fullmatch(r"[A-Z0-9]+", compact):
            return f"{compact[:-3]} {compact[-3:]}"
        return cleaned

    if country_code in {"US", "PR", "VI", "GU", "AS", "MP", "FM", "MH", "PW"}:
        digits = compact
        if digits.isdigit():
            if len(digits) <= 5:
                return digits.zfill(5)
            if len(digits) == 9:
                return f"{digits[:5]}-{digits[5:]}"
        return cleaned

    # For fixed-length numeric systems, remove accidental separators and
    # restore leading zeroes when the source arrived as a number.
    length = _POSTAL_FIXED_DIGITS.get(country_code)
    if length and compact.isdigit() and len(compact) <= length:
        return compact.zfill(length)

    # Argentina/other variable alphanumeric systems: normalise case/spacing but
    # do not invent punctuation that may change a valid postcode.
    return cleaned


def packlink_postal_format_hint(country: Any) -> str:
    code = normalize_country_code(country)
    if code in _POSTAL_FORMAT_HINTS:
        return _POSTAL_FORMAT_HINTS[code]
    length = _POSTAL_FIXED_DIGITS.get(code)
    return ("9" * length) if length else "formato nazionale preservato"


def _decimal_text(value: Any, *, minimum: float | None = None) -> str:
    """Packlink CSV uses a dot as decimal separator; avoid locale commas."""
    try:
        number = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        number = Decimal("0")
    if minimum is not None and number < Decimal(str(minimum)):
        number = Decimal(str(minimum))
    # Avoid scientific notation and needless trailing zeroes while retaining dots.
    text = format(number.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _order_reference(order: Mapping[str, Any]) -> str:
    order_id = clean_text(order.get("order_id"))
    marketplace = clean_text(order.get("marketplace")).upper()
    if not order_id:
        return ""
    if marketplace and not order_id.upper().startswith(marketplace + "-"):
        return f"{marketplace}-{order_id}"
    return order_id


def _raw_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _region_from_mapping(value: Mapping[str, Any]) -> str:
    for key in (
        "province", "province_code", "provinceCode",
        "state", "state_code", "stateCode",
        "region", "region_code", "regionCode", "county",
    ):
        text = clean_text(value.get(key))
        if text:
            return text
    return ""


def _recipient_region(order: Mapping[str, Any], fallback_country: str) -> str:
    region = _region_from_mapping(order)
    if region:
        return region
    lines = order.get("lines")
    if isinstance(lines, Sequence) and not isinstance(lines, (str, bytes, bytearray)):
        for line in lines:
            if not isinstance(line, Mapping):
                continue
            region = _region_from_mapping(line)
            if region:
                return region
            raw = _raw_mapping(line.get("raw_json"))
            stack: list[Mapping[str, Any]] = [raw] if raw else []
            seen = 0
            while stack and seen < 80:
                current = stack.pop()
                seen += 1
                region = _region_from_mapping(current)
                if region:
                    return region
                for child in current.values():
                    if isinstance(child, Mapping):
                        stack.append(child)
    # The official Italian example uses IT in the province field when a more
    # specific province is not supplied. Matching that sample is safer than
    # inventing a locality code.
    return fallback_country


def _sender_region(sender_raw: Mapping[str, Any], fallback_country: str) -> str:
    return _region_from_mapping(sender_raw) or fallback_country


def build_packlink_csv_row(
    *,
    order: Mapping[str, Any],
    sender: Mapping[str, Any],
    package: Mapping[str, Any],
    declared_value: Any,
    insurance: bool = False,
) -> tuple[list[str], list[str]]:
    """Build one row in the official 30-column Packlink PRO CSV format.

    Returns (row, validation_errors). The function never drops or reorders fields.
    Postal codes are always serialized as strings, preserving leading zeros such
    as Slovak 04011.
    """
    sender_normalized = normalize_sender_address(sender)
    recipient = packlink_destination_address(
        order, fallback_phone=sender_normalized.get("phone")
    )
    parcel = package_payload(package)

    sender_country = normalize_country_code(sender_normalized.get("country"))
    recipient_country = normalize_country_code(recipient.get("country"))

    row = [
        _order_reference(order),
        clean_text(sender_normalized.get("name")),
        clean_text(sender_normalized.get("surname")),
        clean_text(sender_normalized.get("company")),
        clean_text(sender_normalized.get("street1")),
        clean_text(sender_normalized.get("street2")),
        normalize_packlink_postal_code(sender_country, sender_normalized.get("zip_code")),
        clean_text(sender_normalized.get("city")),
        _sender_region(sender, sender_country),
        sender_country,
        clean_text(sender_normalized.get("phone")),
        clean_text(sender_normalized.get("email")),
        clean_text(recipient.get("name")),
        clean_text(recipient.get("surname")),
        clean_text(recipient.get("company")),
        clean_text(recipient.get("street1")),
        clean_text(recipient.get("street2")),
        normalize_packlink_postal_code(recipient_country, recipient.get("zip_code")),
        clean_text(recipient.get("city")),
        _recipient_region(order, recipient_country),
        recipient_country,
        clean_text(recipient.get("phone")),
        clean_text(recipient.get("email")),
        "yes" if insurance else "no",
        clean_text(order.get("product_title")) or "Merce ordine",
        _decimal_text(declared_value, minimum=0.01),
        _decimal_text(parcel.get("width"), minimum=1),
        _decimal_text(parcel.get("height"), minimum=1),
        _decimal_text(parcel.get("length"), minimum=1),
        _decimal_text(parcel.get("weight"), minimum=0.01),
    ]

    required = {
        "Numero di ordine": row[0],
        "nome mittente": row[1],
        "Indirizzo Di Spedizione 1": row[4],
        "CAP Spedizione": row[6],
        "citta Spedizione": row[7],
        "Paese di spedizione": row[9],
        "Telefono spedizione": row[10],
        "Email Spedizione": row[11],
        "Nome destinatario": row[12],
        "Indirizzo di consegna 1": row[15],
        "CAP di consegna": row[17],
        "citta di consegna": row[18],
        "Paese di consegna": row[20],
        "Telefono di consegna": row[21],
        "Email di consegna": row[22],
        "Titolo dell'oggetto": row[24],
    }
    errors = [f"{field} mancante" for field, value in required.items() if not clean_text(value)]
    if len(row) != len(PACKLINK_CSV_HEADERS):
        errors.append(f"numero colonne non valido: {len(row)}")
    return row, errors


def choose_best_packlink_service(services: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Return the cheapest usable Packlink service from a quote response.

    Services without a positive numeric total price are ignored. Ties are
    deterministic (carrier, service, id) so bulk automatic generation is
    stable across Streamlit reruns.
    """
    ranked: list[tuple[Decimal, str, str, str, dict[str, Any]]] = []
    for raw in services:
        if not isinstance(raw, Mapping):
            continue
        try:
            price = Decimal(str(raw.get("price")).replace(",", "."))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if price <= 0:
            continue
        item = dict(raw)
        ranked.append((
            price,
            clean_text(item.get("carrier")).casefold(),
            clean_text(item.get("service")).casefold(),
            clean_text(item.get("id")).casefold(),
            item,
        ))
    if not ranked:
        return None
    ranked.sort(key=lambda value: value[:4])
    return dict(ranked[0][4])


def build_packlink_csv(
    shipments: Sequence[Mapping[str, Any]],
    *,
    sender: Mapping[str, Any],
    insurance: bool = False,
) -> tuple[bytes, list[dict[str, Any]]]:
    """Return UTF-8-BOM semicolon CSV bytes + validation report.

    Each shipment item must contain: order, package, declared_value. Invalid
    orders are excluded from the CSV and returned in the report so the UI can
    make omissions explicit.
    """
    valid_rows: list[list[str]] = []
    report: list[dict[str, Any]] = []
    for item in shipments:
        order = item.get("order") if isinstance(item.get("order"), Mapping) else {}
        package = item.get("package") if isinstance(item.get("package"), Mapping) else {}
        row, errors = build_packlink_csv_row(
            order=order,
            sender=sender,
            package=package,
            declared_value=item.get("declared_value"),
            insurance=insurance,
        )
        sender_raw = normalize_sender_address(sender)
        recipient_raw = packlink_destination_address(
            order, fallback_phone=sender_raw.get("phone")
        )
        recipient_country = normalize_country_code(recipient_raw.get("country"))
        sender_country = normalize_country_code(sender_raw.get("country"))
        report.append({
            "order_id": clean_text(order.get("order_id")),
            "reference": row[0] if row else "",
            "valid": not bool(errors),
            "errors": errors,
            "recipient_country": recipient_country,
            "recipient_city": clean_text(recipient_raw.get("city")),
            "recipient_phone_fallback": (
                not clean_text(packlink_destination_address(order).get("phone"))
                and bool(clean_text(sender_raw.get("phone")))
            ),
            "recipient_postal_original": clean_text(recipient_raw.get("zip_code")),
            "recipient_postal_csv": row[17] if row else "",
            "recipient_postal_format": packlink_postal_format_hint(recipient_country),
            "sender_country": sender_country,
            "sender_postal_original": clean_text(sender_raw.get("zip_code")),
            "sender_postal_csv": row[6] if row else "",
        })
        if not errors:
            valid_rows.append(row)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(PACKLINK_CSV_HEADERS)
    writer.writerows(valid_rows)
    payload = buffer.getvalue().encode("utf-8-sig")
    return payload, report
