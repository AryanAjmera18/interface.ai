"""Define synthetic fixture state and fault types; forbid automation-package imports."""

from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.target_app.config import TenantId


class FaultKind(StrEnum):
    RECORD_NOT_FOUND = "record_not_found"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    UNEXPECTED_INTERSTITIAL = "unexpected_interstitial"
    SESSION_EXPIRED = "session_expired"
    SLOW_LOAD = "slow_load"
    SERVER_ERROR_500 = "server_error_500"


class ApiError(BaseModel):
    error: str


class FaultRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FaultKind
    remaining: int = Field(default=1, ge=1, le=100)
    delay_ms: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def delay_only_for_slow_load(self) -> Self:
        if self.kind != FaultKind.SLOW_LOAD and self.delay_ms:
            raise ValueError("delay_ms is only valid for slow_load")
        return self


class FaultConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    faults: list[FaultRule] = Field(default_factory=list, max_length=7)

    @model_validator(mode="after")
    def unique_kinds(self) -> Self:
        if len({fault.kind for fault in self.faults}) != len(self.faults):
            raise ValueError("Each fault kind may be configured only once")
        return self

    def consume(self, kind: FaultKind) -> FaultRule | None:
        for rule in self.faults:
            if rule.kind == kind:
                fired = rule.model_copy()
                if rule.remaining == 1:
                    self.faults.remove(rule)
                else:
                    rule.remaining -= 1
                return fired
        return None


class Account(BaseModel):
    account_id: str
    account_type: str
    nickname: str
    balance: Decimal


class Member(BaseModel):
    member_id: str
    name: str
    address: str
    restricted: bool = False
    accounts: list[Account]


class FormValues(BaseModel):
    member_id: str = ""
    account_type: str = "Savings"
    nickname: str = ""
    initial_deposit: str = ""
    funding_source: str = ""
    token: str = ""


class PendingAction(BaseModel):
    action: str
    method: str
    form: FormValues


class Draft(BaseModel):
    form: FormValues
    token: str
    extra_confirmed: bool = False


class Receipt(BaseModel):
    reference: str
    token: str
    member_id: str
    account: Account


class Session(BaseModel):
    """Hold synthetic state only in memory; submitted login credentials are never retained."""

    session_id: str
    tenant_id: TenantId = "alpha"
    authenticated: bool = False
    members: list[Member] = Field(default_factory=list)
    faults: FaultConfiguration = Field(default_factory=FaultConfiguration)
    pending: PendingAction | None = None
    draft: Draft | None = None
    receipts: list[Receipt] = Field(default_factory=list)


Screen = Literal[
    "login",
    "shell",
    "nav",
    "content",
    "search",
    "results",
    "not_found",
    "detail",
    "open",
    "review",
    "extra",
    "confirmation",
    "denied",
    "server_error",
    "notice",
    "invalid",
]


class View(BaseModel):
    screen: Screen
    form: FormValues = Field(default_factory=FormValues)
    member: Member | None = None
    notice: str = ""
    error: str = ""
    token: str = ""
    receipt: Receipt | None = None
    as_of: str = "2026-01-15"
