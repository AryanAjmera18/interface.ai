"""Define tenant presentation settings; forbid imports from automation packages."""

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

TenantId = Literal["alpha", "beta"]
FormField = Literal["account_type", "nickname", "initial_deposit", "funding_source"]


class TenantConfig(BaseModel):
    """Keep all tenant variation in data; accessible names follow visible tenant labels."""

    model_config = ConfigDict(frozen=True)

    tenant_id: TenantId
    branding: str
    page_title: str
    member_label: str
    open_account_label: str
    css_class: str
    reverse_account_rows: bool = False
    extra_confirmation: bool = False
    field_order: tuple[FormField, FormField, FormField, FormField] = (
        "account_type",
        "nickname",
        "initial_deposit",
        "funding_source",
    )

    @property
    def config_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


ALPHA = TenantConfig(
    tenant_id="alpha",
    branding="Alpha Mutual",
    page_title="Member Services",
    member_label="Member ID",
    open_account_label="Open Sub-Account",
    css_class="x7f2a",
)
BETA = TenantConfig(
    tenant_id="beta",
    branding="Beta Community",
    page_title="Retail Operations",
    member_label="Account Holder Number",
    open_account_label="Add Deposit Product",
    css_class="q9b4e",
    extra_confirmation=True,
    reverse_account_rows=True,
    field_order=("account_type", "initial_deposit", "nickname", "funding_source"),
)


class TenantCatalog(BaseModel):
    tenants: tuple[TenantConfig, ...] = (ALPHA, BETA)

    def get(self, tenant_id: str) -> TenantConfig | None:
        return next((item for item in self.tenants if item.tenant_id == tenant_id), None)


class SurfaceVersion(BaseModel):
    app_id: str = "cua-synthetic-bank"
    app_version: str = "0.1.0"
    tenant_id: TenantId
    config_hash: str
    ui_revision: str = "legacy-frames-2"
