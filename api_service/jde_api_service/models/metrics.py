from __future__ import annotations

from .base import ApiModel
from .change import ChangeType, LifecycleState


class Total(ApiModel):
    label: str
    value: int
    delta: int = 0


class PipelineStage(ApiModel):
    stage: str
    count: int


class ChangeTypeCount(ApiModel):
    type: ChangeType
    count: int


class BusinessImpactCount(ApiModel):
    category: str
    count: int


class Performance(ApiModel):
    average_cycle_time_days: float = 0
    average_cycle_time_delta: float = 0
    first_time_success_rate: int = 0
    first_time_success_delta: int = 0
    human_approvals: int = 0
    human_rejections: int = 0
    change_volume: int = 0
    change_volume_delta: int = 0


class FactoryMetrics(ApiModel):
    totals: list[Total]
    pipeline: list[PipelineStage]
    change_types: list[ChangeTypeCount]
    business_impact_breakdown: list[BusinessImpactCount]
    performance: Performance


class ActivityEntry(ApiModel):
    time: str
    change_id: str
    description: str
    state: LifecycleState
    updated_by: str
