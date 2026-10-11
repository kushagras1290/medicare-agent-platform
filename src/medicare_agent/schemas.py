from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SafeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
CustomerId = Annotated[str, StringConstraints(pattern=r"^C\d{5}$")]


class RiskTier(StrEnum):
    ROUTINE = "routine"
    REVIEW = "review"
    URGENT = "urgent"


class ContactChannel(StrEnum):
    PHONE = "phone"
    SMS = "sms"
    PORTAL = "portal"
    NONE = "none"


class AgentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=800)]
    recommended_action: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)
    ]
    contact_channel: ContactChannel
    reasoning_bullets: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=240)]
    ] = Field(min_length=1, max_length=6)
    requires_human_review: Literal[True]


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    idempotency_key: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=8, max_length=128)
    ]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "hold"]
    rationale: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=8, max_length=1000)
    ]
    expected_version: int = Field(ge=1)


class RuleAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: RiskTier
    reasons: list[str] = Field(min_length=1, max_length=12)
    policy_holds: list[str] = Field(default_factory=list, max_length=8)


class CustomerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: CustomerId
    age: int = Field(ge=18, le=100)
    sex_at_birth: Literal["female", "male", "intersex"]
    conditions: list[str] = Field(min_length=1, max_length=5)
    missed_appointment: bool
    consent_status: Literal["synthetic_opted_in", "synthetic_withheld"]
    last_contact_at: datetime
    measured_at: datetime
    bp_systolic_mm_hg: float = Field(ge=50, le=260)
    bp_diastolic_mm_hg: float = Field(ge=30, le=180)
    heart_rate_bpm: float = Field(ge=25, le=220)
    spo2_pct: float = Field(ge=50, le=100)
    temperature_c: float = Field(ge=30, le=45)
    respiratory_rate_bpm: float = Field(ge=5, le=60)
    weight_kg: float = Field(ge=25, le=300)
    bmi: float = Field(ge=10, le=80)
    creatinine_mg_dl: float = Field(ge=0.1, le=20)
    egfr_ml_min_1_73m2: float = Field(ge=1, le=160)
    glucose_mg_dl: float = Field(ge=30, le=600)
    hemoglobin_g_dl: float = Field(ge=3, le=25)
    sodium_mmol_l: float = Field(ge=90, le=180)
    potassium_mmol_l: float = Field(ge=1.5, le=9)
    phq9_score: int = Field(ge=0, le=27)


class GatewayProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    snapshot: CustomerSnapshot
    rule_assessment: RuleAssessment


class GatewayProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: AgentProposal
    provider: str
    model: str
    request_id: str | None = None


class CallerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    customer_id: CustomerId
    channel: Literal["phone", "sms", "portal"]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)]
    synthetic: Literal[True]
    approved_by_human: Literal[True]


class CallerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=8, max_length=128)
    ]
    status: Literal["accepted"]
    provider: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]


class CaseResponse(BaseModel):
    id: str
    customer_id: str
    status: str
    rule_tier: RiskTier
    rule_reasons: list[str]
    policy_holds: list[str]
    proposal: AgentProposal | None
    provider_error: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ListCasesResponse(BaseModel):
    items: list[CaseResponse]
    total: int
    limit: int
    offset: int


class ReviewResponse(BaseModel):
    case: CaseResponse
    decision: str
    dispatch_queued: bool


class DispatchResponse(BaseModel):
    processed: bool
    outbox_event_id: str | None = None
    case_id: str | None = None
    status: str


class ApiEnvelope(BaseModel):
    success: bool
    data: object | None = None
    error: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> ApiEnvelope:
        if self.success == (self.error is not None):
            raise ValueError(
                "Successful responses cannot have errors and failed responses must have one"
            )
        return self
