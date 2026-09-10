import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    search: str = Field("", max_length=200)
    statuses: list[str] | None = Field(None, max_length=100)
    storefronts: list[str] | None = Field(None, max_length=100)
    currencies: list[str] | None = Field(None, max_length=100)
    carriers: list[str] = Field(default_factory=list, max_length=100)
    tracking: Literal["all", "present", "missing"] = "all"
    commission: Literal["all", "present", "missing"] = "all"
    payment: Literal["all", "available", "waiting", "unknown", "ticket_open"] = "all"
    amount_min: Decimal | None = Field(None, ge=0, max_digits=30, decimal_places=8)
    amount_max: Decimal | None = Field(None, ge=0, max_digits=30, decimal_places=8)
    date_from: date | None = None
    date_to: date | None = None

    @field_validator("statuses", "storefronts", "currencies", "carriers")
    @classmethod
    def normalize_values(cls, value):
        if value is None:
            return None
        if any(len(item) > 200 for item in value):
            raise ValueError("Valore filtro troppo lungo.")
        return sorted(set(item.strip() for item in value))

    @field_validator("search")
    @classmethod
    def normalize_search(cls, value):
        return value.strip().casefold()

    @model_validator(mode="after")
    def validate_ranges(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Intervallo date non valido.")
        if self.date_to == date.max:
            raise ValueError("Data finale fuori intervallo.")
        if (self.amount_min is not None and self.amount_max is not None
                and self.amount_min > self.amount_max):
            raise ValueError("Intervallo importi non valido.")
        return self

    def fingerprint(self):
        values = self.model_dump(mode="json")
        for key in ("amount_min", "amount_max"):
            if values[key] is not None:
                values[key] = str(Decimal(values[key]).normalize())
        raw = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def dates(self):
        return (
            datetime.combine(self.date_from, time.min, UTC) if self.date_from else None,
            datetime.combine(self.date_to + timedelta(days=1), time.min, UTC)
            if self.date_to else None,
        )
