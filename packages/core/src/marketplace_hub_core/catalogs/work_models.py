from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Amount = Annotated[Decimal, Field(ge=0, le=999999, allow_inf_nan=False)]
CellAmount = Annotated[Decimal, Field(ge=0, le=999999999999, allow_inf_nan=False)]


class WorkRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    price_list_id: UUID
    version: int = Field(ge=1)
    content_list_id: UUID | None = None
    content_version: int | None = Field(None, ge=1)
    search: str = Field("", max_length=200)
    min_qty: Amount = Decimal(0)
    min_cost: Amount = Decimal(0)
    max_cost: Amount = Decimal(0)
    measure: Literal["weight_kg", "length_cm", "width_cm", "height_cm"] = "weight_kg"
    exclude: Literal["none", "above", "below", "between"] = "none"
    lower: Amount = Decimal(0)
    upper: Amount = Decimal(0)
    shipping: Annotated[Decimal, Field(ge=0, le=9999)] = Decimal(0)
    margin: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(35)
    minimum_margin: Annotated[Decimal, Field(ge=0, le=500)] = Decimal(10)

    @model_validator(mode="after")
    def ordered(self):
        if self.max_cost and self.max_cost < self.min_cost:
            raise ValueError("Costo massimo inferiore al minimo.")
        if self.exclude == "between" and self.lower > self.upper:
            raise ValueError("Soglie peso non ordinate.")
        if bool(self.content_list_id) != bool(self.content_version):
            raise ValueError("Versione contenuti mancante.")
        return self


class WorkPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    recipe: WorkRecipe
    page: int = Field(1, ge=1, le=100000)


class RowEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    cost: CellAmount | None = None
    shipping_cost: CellAmount | None = None
    total_cost: CellAmount | None = None
    quantity: CellAmount | None = None
    price: CellAmount | None = None
    minimum_price: CellAmount | None = None


class ViewSave(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    recipe: WorkRecipe
    name: str = Field(min_length=1, max_length=200)
    account_ids: list[UUID] = Field(min_length=1, max_length=100)
    select_all: bool = True
    # All-mode excludes these IDs; individual-mode includes only these IDs.
    selection: list[UUID] = Field(default_factory=list, max_length=20000)
    edits: dict[UUID, RowEdit] = Field(default_factory=dict, max_length=1000)
    overwrite_id: UUID | None = None
    expected_revision: int | None = Field(None, ge=1)


class SavedRowEdit(RowEdit):
    ean: str | None = Field(None, max_length=128)
    sku: str | None = Field(None, max_length=500)
    name: str | None = Field(None, max_length=10000)
    weight_kg: Amount | None = None


class AddedRow(RowEdit):
    ean: str = Field("", max_length=128)
    sku: str = Field("", max_length=500)
    name: str = Field("", max_length=10000)
    weight_kg: Amount | None = None

    @model_validator(mode="after")
    def identifier(self):
        if not self.ean.strip() and not self.sku.strip():
            raise ValueError("EAN o SKU obbligatorio per una nuova riga.")
        return self


class ViewUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=200)
    account_ids: list[UUID] = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)
    edits: dict[UUID, SavedRowEdit] = Field(default_factory=dict, max_length=1000)
    removed: list[UUID] = Field(default_factory=list, max_length=20000)
    added: list[AddedRow] = Field(default_factory=list, max_length=100)


class ViewDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    confirmation: Literal["ELIMINA"]
