"""养护资金决策与执行服务使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RoadSegment:
    segment_id: str
    organization_id: str
    route_code: str
    name: str
    length_km: float
    chainage_start: float
    chainage_end: float


@dataclass(frozen=True)
class Milestone:
    milestone_id: str
    application_id: str
    seq: int
    name: str
    amount: float
    due_date: str
    status: str
    reserved_amount: float
    paid_amount: float


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    period_id: str
    envelope_id: str | None
    round_id: str
    application_id: str
    milestone_id: str | None
    entry_type: str
    amount: float
    detail: dict[str, Any] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = ""
