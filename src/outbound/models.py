"""Data models for the engine.

These Pydantic types are the contracts that every other module reads and writes: the distilled
offer, raw leads, enriched/scored prospects, the generated sequences and their A/B variants, and
the send/reply events that feed the optimization loop. Enums are string-valued so they serialize
cleanly into SQLite and JSON.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, EmailStr, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


def stable_prospect_id(email: str) -> str:
    """Deterministic id from an email, so re-ingesting the same lead dedupes."""
    digest = hashlib.sha1(email.strip().lower().encode()).hexdigest()[:12]
    return f"prs_{digest}"


# --------------------------------------------------------------------------- enums


class Channel(str, Enum):
    EMAIL = "email"


class Tier(str, Enum):
    UNSCORED = "unscored"
    DISQUALIFIED = "disqualified"
    BROAD = "broad"
    PRIORITY = "priority"


class ProspectStatus(str, Enum):
    NEW = "new"
    SCORED = "scored"
    DRAFTED = "drafted"
    APPROVED = "approved"
    QUEUED = "queued"
    IN_SEQUENCE = "in_sequence"
    REPLIED = "replied"
    SUPPRESSED = "suppressed"
    DISQUALIFIED = "disqualified"


class SequenceStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    PUSHED = "pushed"


class VariantKind(str, Enum):
    SUBJECT = "subject"
    BODY = "body"
    CTA = "cta"


class EventType(str, Enum):
    SENT = "sent"
    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    BOUNCED = "bounced"
    REPLIED = "replied"
    UNSUBSCRIBED = "unsubscribed"
    FAILED = "failed"


class ReplyClass(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    OUT_OF_OFFICE = "out_of_office"
    UNSUBSCRIBE = "unsubscribe"
    REFERRAL = "referral"


# --------------------------------------------------------------------------- offer


class ObjectionResponse(BaseModel):
    objection: str
    response: str


class Offer(BaseModel):
    """The product/service brief distilled from uploaded context files."""

    name: str = ""
    summary: str = ""
    value_props: list[str] = Field(default_factory=list)
    proof_points: list[str] = Field(default_factory=list)
    buyer_motivations: list[str] = Field(default_factory=list)
    objections: list[ObjectionResponse] = Field(default_factory=list)
    price_posture: str = ""
    icp_hypotheses: list[str] = Field(default_factory=list)
    source_files: list[str] = Field(default_factory=list)
    distilled_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------- leads/prospects


class Lead(BaseModel):
    """A raw inbound contact from a CSV (later, Apollo), before enrichment or scoring."""

    email: EmailStr
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    company: str | None = None
    domain: str | None = None
    industry: str | None = None
    location: str | None = None
    source: str = "csv"
    raw: dict[str, str] = Field(default_factory=dict)


class Prospect(BaseModel):
    """An enriched, scored lead — the working unit the engine drafts and sequences for."""

    id: str = ""
    lead: Lead
    fit_score: float = Field(default=0.0, ge=0.0, le=1.0)
    tier: Tier = Tier.UNSCORED
    status: ProspectStatus = ProspectStatus.NEW
    enrichment: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _assign_id(self) -> Prospect:
        if not self.id:
            self.id = stable_prospect_id(str(self.lead.email))
        return self

    @property
    def email(self) -> str:
        return str(self.lead.email)

    @classmethod
    def from_lead(cls, lead: Lead) -> Prospect:
        return cls(lead=lead)


# ------------------------------------------------------------------ sequences/variants


class SequenceStep(BaseModel):
    """One email in a sequence: the chosen copy plus the day it goes out."""

    index: int = Field(ge=0)
    day_offset: int = Field(default=0, ge=0)
    subject: str
    body: str
    cta: str = ""


class VariantStats(BaseModel):
    """Running tallies the bandit uses to favour better-performing copy."""

    sends: int = 0
    replies: int = 0
    positive: int = 0

    @property
    def reply_rate(self) -> float:
        return self.replies / self.sends if self.sends else 0.0

    @property
    def positive_rate(self) -> float:
        return self.positive / self.sends if self.sends else 0.0


class Variant(BaseModel):
    """An alternative for one slot (subject, body, or CTA) of a given step, with its stats."""

    id: str = Field(default_factory=lambda: _uid("var"))
    step_index: int = Field(ge=0)
    element: VariantKind
    content: str
    stats: VariantStats = Field(default_factory=VariantStats)


class Sequence(BaseModel):
    """A full multi-step sequence for one prospect, with variants queued for A/B testing."""

    id: str = Field(default_factory=lambda: _uid("seq"))
    prospect_id: str
    steps: list[SequenceStep] = Field(default_factory=list)
    variants: list[Variant] = Field(default_factory=list)
    status: SequenceStatus = SequenceStatus.DRAFT
    model: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------- events/replies


class SendEvent(BaseModel):
    """A delivery-side event pulled back from the sending provider."""

    id: str = Field(default_factory=lambda: _uid("evt"))
    prospect_id: str
    sequence_id: str | None = None
    step_index: int | None = None
    type: EventType
    channel: Channel = Channel.EMAIL
    provider: str | None = None
    occurred_at: datetime = Field(default_factory=_utcnow)
    meta: dict[str, str] = Field(default_factory=dict)


class Reply(BaseModel):
    """An inbound reply, classified (later) by the LLM reply classifier."""

    id: str = Field(default_factory=lambda: _uid("rpl"))
    prospect_id: str
    sequence_id: str | None = None
    subject: str | None = None
    body: str = ""
    classification: ReplyClass | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    handled: bool = False
    received_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "Channel",
    "Tier",
    "ProspectStatus",
    "SequenceStatus",
    "VariantKind",
    "EventType",
    "ReplyClass",
    "ObjectionResponse",
    "Offer",
    "Lead",
    "Prospect",
    "SequenceStep",
    "VariantStats",
    "Variant",
    "Sequence",
    "SendEvent",
    "Reply",
    "stable_prospect_id",
]
