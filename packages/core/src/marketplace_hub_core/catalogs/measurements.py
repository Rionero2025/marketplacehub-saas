"""Physical catalog measurements; missing or ambiguous values are never zero."""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

FIELDS = ("weight_kg", "length_cm", "width_cm", "height_cm")
INNPRO_BOX_UNIT: str | None = None


def positive(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        return None
    text = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"\+?\d+(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or not 0 < number < Decimal("1000000000"):
        return None
    return number.quantize(Decimal("0.000001")) or None


def _token(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def source_data(canonical: str) -> dict:
    try:
        data = json.loads(canonical)
    except (ValueError, TypeError):
        return {}
    source = data.get("source", {}) if isinstance(data, dict) else {}
    return source if isinstance(source, dict) else {}


def measurements(canonical: str, *, provider: str = "generic") -> dict:
    source = source_data(canonical)
    fields = {_token(k): v for k, v in source.items()}
    result = dict.fromkeys(FIELDS)
    for name in ("weight_kg", "peso_kg", "package_weight_kg",
                 "gross_weight_kg", "peso", "weight"):
        if name in fields:
            result["weight_kg"] = positive(fields[name])
            break
    if result["weight_kg"] is None and "weight_kg" not in fields:
        grams = positive(fields.get("weight_g", fields.get("peso_g")))
        result["weight_kg"] = grams / 1000 if grams else None
    for axis, italian in (("length", "lunghezza"), ("width", "larghezza"),
                          ("height", "altezza")):
        for unit, multiplier in (("cm", Decimal(1)), ("mm", Decimal("0.1")),
                                 ("m", Decimal(100))):
            found = next((fields[k] for k in (
                f"{axis}_{unit}", f"{italian}_{unit}", f"box_{axis}_{unit}",
                f"package_{axis}_{unit}",
            ) if k in fields), None)
            value = positive(found)
            if value:
                result[f"{axis}_cm"] = value * multiplier
                break
    if provider == "innpro" and INNPRO_BOX_UNIT == "cm":
        parameters = source.get("parameters", {})
        if isinstance(parameters, dict):
            for axis in ("length", "width", "height"):
                result[f"{axis}_cm"] = positive(parameters.get(f"Box {axis}"))
    return result


def unknown_dimensions(canonical: str, *, provider: str) -> str | None:
    if provider != "innpro" or INNPRO_BOX_UNIT is not None:
        return None
    parameters = source_data(canonical).get("parameters", {})
    if not isinstance(parameters, dict):
        return None
    values = [positive(parameters.get(f"Box {axis}")) for axis in ("length", "width", "height")]
    if not any(values):
        return None
    return " × ".join(format(v.normalize(), "f") if v else "—" for v in values)


@dataclass(frozen=True)
class MeasurementFilter:
    field: str = "weight_kg"
    mode: str = "none"
    lower: Decimal = Decimal(0)
    upper: Decimal = Decimal(0)

    def __post_init__(self):
        if self.field not in FIELDS or self.mode not in {"none", "above", "below", "between"}:
            raise ValueError("Filtro misure non valido.")
        if any(not v.is_finite() or not 0 <= v < Decimal("1000000000")
               for v in (self.lower, self.upper)):
            raise ValueError("Inserisci misure valide e non negative.")
        if self.mode == "between" and self.lower > self.upper:
            raise ValueError("Nel filtro misure il valore Da non può superare il valore A.")

    def condition(self, products):
        from sqlalchemy import or_, true
        if self.mode == "none":
            return true()
        value = products.c[self.field]
        excluded = (value > self.lower if self.mode == "above" else
                    value < self.lower if self.mode == "below" else
                    value.between(self.lower, self.upper))
        # Same exclusion boundaries as Streamlit; unknown measurements survive.
        return or_(value.is_(None), value <= 0, ~excluded)
