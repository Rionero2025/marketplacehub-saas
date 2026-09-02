from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=1, max_length=1024)
    remember: bool = False


class UserResponse(ApiModel):
    id: int
    username: str
    display_name: str
    is_admin: bool
    permissions: list[str]
    seller_ids: list[int] | None
    expires_at: int
    tenant_ids: list[int] = Field(default_factory=list)
    active_tenant_id: int = 0
    active_tenant_name: str = ""
    active_tenant_type: str = ""
    tenant_role: str = ""


class LoginResponse(ApiModel):
    token: str
    token_type: str = "bearer"
    expires_at: int
    user: UserResponse


class SellerResponse(ApiModel):
    id: int
    name: str
    legal_name: str = ""
    active: bool = True


class MarketplaceAccountResponse(ApiModel):
    id: int
    seller_id: int
    marketplace: str
    account_name: str
    active: bool


class JobResponse(ApiModel):
    job_id: str
    kind: str = ""
    status: str
    seller_id: int | None = None
    progress_current: int = 0
    progress_total: int = 0
    progress_pct: float = 0
    message: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""


class OrderSyncRequest(ApiModel):
    marketplace: str
    environment: str = "live"
    date_from: date | None = None
    date_to: date | None = None
    maximum: int | None = Field(default=1000, ge=1, le=100000)
    include_tracking_details: bool = True


class AccountingJobRequest(ApiModel):
    marketplace: str
    date_from: date
    date_to: date
    full: bool = False


class CatalogMaterializeRequest(ApiModel):
    price_list_id: int = Field(gt=0)


class TenantResponse(ApiModel):
    id: int
    name: str
    slug: str
    tenant_type: str
    status: str = "active"
    plan_code: str = ""
    access_mode: str = ""
    role: str = ""
    active: bool = False


class TenantCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(default="", max_length=100)
    tenant_type: str = Field(default="merchant", pattern="^(merchant|agency)$")
    plan_code: str = Field(default="", max_length=80)
    owner_user_id: int | None = Field(default=None, gt=0)


class TenantMembershipRequest(ApiModel):
    role: str = Field(default="operator", pattern="^(owner|admin|manager|operator|viewer)$")
    active: bool = True


class TenantSellerAttachRequest(ApiModel):
    transfer: bool = False


class AgencyClientLinkRequest(ApiModel):
    active: bool = True


class TenantActivateResponse(ApiModel):
    active_tenant_id: int
    name: str
    tenant_type: str
    role: str = ""
