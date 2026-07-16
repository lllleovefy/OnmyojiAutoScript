from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


DuelSide = Literal["self", "opponent"]
DuelResult = Literal["win", "loss", "unknown"]


def normalize_utc_iso(value: str) -> str:
    """Return a consistently sortable UTC ISO-8601 timestamp."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


class DuelPick(BaseModel):
    side: DuelSide
    round: int = Field(ge=1, le=6)
    shishen_id: int = Field(ge=0)
    count: int = Field(default=1, ge=1)


class DuelMatch(BaseModel):
    id: int
    account_id: int
    started_at: str | None = None
    score: int | None = None
    star: int | None = None
    self_ban: int | None = Field(default=None, ge=0)
    opponent_ban: int | None = Field(default=None, ge=0)
    picks: list[DuelPick] = Field(default_factory=list)
    result: DuelResult = "unknown"
    duration: float | None = Field(default=None, ge=0)
    valid: bool = True
    practice_mode: bool = False
    source: str = "oas"
    source_record_id: str | None = None


class DuelStrategy(BaseModel):
    id: int
    name: str
    content: dict[str, Any] | list[Any] | str | None = None
    enabled: bool = True
    source: str = "oas"
    source_strategy_id: str | None = None


class DuelRecommendationItem(BaseModel):
    shishen_id: int = Field(ge=0)
    shikigami_id: int = Field(ge=0)
    score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    sample_size: int = Field(default=0, ge=0)
    source: Literal["rule", "personal", "external"]
    reason: str = ""
    evidence_sources: list[Literal["rule", "personal", "external"]] = Field(
        default_factory=list
    )


class DuelRecommendation(BaseModel):
    """Public wire shape emitted by the ``recommendation`` SSE event."""

    state: str
    phase: str
    config_name: str | None = None
    mode: Literal["off", "observe", "recommend", "auto"]
    shishen_id: int = Field(ge=0)
    shikigami_id: int = Field(ge=0)
    score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    recognition_confidence: float = Field(ge=0, le=1)
    recommendation_confidence: float = Field(ge=0, le=1)
    sample_size: int = Field(default=0, ge=0)
    explanation: str = ""
    reason: str = ""
    source: Literal["rule", "personal", "external"]
    recommendations: list[DuelRecommendationItem] = Field(default_factory=list)
    evidence_sources: list[Literal["rule", "personal", "external"]] = Field(
        default_factory=list
    )


class DuelMatchList(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DuelMatch] = Field(default_factory=list)


class DuelTopPick(BaseModel):
    shishen_id: int
    count: int
    wins: int
    win_rate: float


class DuelSummary(BaseModel):
    total: int
    valid: int
    wins: int
    losses: int
    unknown: int
    practice: int
    win_rate: float
    latest_at: str | None = None
    top_picks: list[DuelTopPick] = Field(default_factory=list)


class DuelMatchPatch(BaseModel):
    started_at: str | None = None
    score: int | None = None
    star: int | None = None
    self_ban: int | None = Field(default=None, ge=0)
    opponent_ban: int | None = Field(default=None, ge=0)
    picks: list[DuelPick] | None = None
    result: DuelResult | None = None
    duration: float | None = Field(default=None, ge=0)
    valid: bool | None = None
    practice_mode: bool | None = None

    @field_validator("started_at")
    @classmethod
    def reject_blank_started_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("started_at must not be blank")
        return normalize_utc_iso(value)

    @field_validator("picks")
    @classmethod
    def reject_duplicate_pick_rounds(cls, value: list[DuelPick] | None) -> list[DuelPick] | None:
        if value is None:
            return value
        slots = [(pick.side, pick.round) for pick in value]
        if len(slots) != len(set(slots)):
            raise ValueError("picks must contain at most one entry for each side and round")
        return value
