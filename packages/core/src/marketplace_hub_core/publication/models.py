from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Number = Annotated[float, Field(ge=0, le=999999, allow_inf_nan=False)]


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    account_id: UUID
    view_id: UUID
    revision: int = Field(ge=1)
    storefront: Literal["de", "cz", "sk", "at", "pl", "fr", "it", "pt"] = "de"
    playground: bool = True
    margin: float = Field(35, ge=0, le=500, allow_inf_nan=False)
    minimum_margin: float = Field(10, ge=0, le=500, allow_inf_nan=False)
    commission: float = Field(15, ge=0, le=100, allow_inf_nan=False)
    min_qty: int = Field(1, ge=0, le=99999)
    min_cost: Number = 0
    min_profit: Number = 0
    weight_mode: Literal["none", "above", "below", "between"] = "none"
    weight_from: Number = 0
    weight_to: Number = 0
    composite_sku: bool = False
    shipping_group: str = Field("", max_length=80, pattern=r"^[0-9]*$")
    warehouse: str = Field("", max_length=80, pattern=r"^[0-9]*$")
    handling: int = Field(1, ge=0, le=44)
    vat: Literal[
        "standard_rate", "reduced_rate_1", "reduced_rate_2", "super_reduced_rate", "zero_rate"
    ] = "standard_rate"
    multiplier: float = Field(1, gt=0, le=1000, allow_inf_nan=False)
    fx_date: str = Field("", max_length=10, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    logistic_class: str = Field("", max_length=100)
    state_code: str = Field("11", max_length=20, pattern=r"^[0-9]+$")
    ship_from: str = Field("IT|Italy", max_length=100)
    start: int = Field(1, ge=1, le=1000000)
    selection_mode: Literal["all", "range"] = "range"
    limit: int = Field(100, ge=1, le=20000)

    @model_validator(mode="after")
    def interval(self):
        if self.weight_mode == "between" and self.weight_from > self.weight_to:
            raise ValueError("Intervallo peso non valido.")
        return self


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    confirmation: Literal["PUBBLICA"]
    selected: list[UUID] = Field(min_length=1, max_length=20000)
    version: str = Field(pattern=r"^[a-f0-9]{64}$")


class RowEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: UUID
    name: str = Field(default="", max_length=1000)
    ean: str = Field(default="", max_length=30)
    sku: str = Field(default="", max_length=100)
    quantity: int = Field(default=0, ge=0, le=99999)
    cost: Number = 0
    price: Number = 0
    minimum_price: Number = 0
    commission: Number = 0
    weight_kg: Number | None = None


class EditDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    rows: list[RowEdit] = Field(min_length=1, max_length=20000)
