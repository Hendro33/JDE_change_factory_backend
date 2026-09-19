"""
Derives FactoryMetrics/ActivityEntry from ChangeService's already
customer-scoped Change list -- same rule mockApi.ts documents for
itself: every number here is computed from real records, nothing is
hard-coded, so this keeps behaving correctly as real data grows.
"""

from __future__ import annotations

from datetime import datetime

from ..models.change import Change
from ..models.metrics import (
    ActivityEntry,
    BusinessImpactCount,
    ChangeTypeCount,
    FactoryMetrics,
    PipelineStage,
    Performance,
    Total,
)
from .change_service import ChangeService

_IN_BUILD = {"APPROVED", "ARCHITECTING", "SPEC_READY", "CHANGE_APPROVED", "EXECUTING"}
_COMPLETED = {"CLOSED", "VALIDATED", "CNC_HANDOFF", "RESOLVED_WITHOUT_CHANGE"}
_REJECTED = {"REJECTED", "FAILED"}


class MetricsService:
    def __init__(self, change_service: ChangeService) -> None:
        self._changes = change_service

    def metrics_for_customer(self, customer_id: str) -> FactoryMetrics:
        all_changes = self._changes.list_for_customer(customer_id)

        def in_state(*states: str) -> int:
            return sum(1 for c in all_changes if c.state in states)

        awaiting_approval = in_state("BACKLOG_READY")
        in_build = sum(1 for c in all_changes if c.state in _IN_BUILD)
        in_testing = in_state("TESTING")
        completed = sum(1 for c in all_changes if c.state in _COMPLETED)
        rejected = sum(1 for c in all_changes if c.state in _REJECTED)

        type_counts: dict[str, int] = {}
        for c in all_changes:
            type_counts[c.change_type] = type_counts.get(c.change_type, 0) + 1

        approved = [c for c in all_changes if c.story_approval and c.story_approval.status == "approved"]

        def stated(v: str) -> bool:
            return bool(v and v.strip())

        impact = [
            BusinessImpactCount(category="Operational", count=sum(1 for c in approved if stated(c.business_impact.operational_reach))),
            BusinessImpactCount(category="Financial", count=sum(1 for c in approved if stated(c.business_impact.financial_impact))),
            BusinessImpactCount(category="Compliance", count=sum(1 for c in approved if stated(c.business_impact.risk_compliance))),
            BusinessImpactCount(category="Strategic", count=sum(1 for c in approved if stated(c.business_impact.strategic_alignment))),
            BusinessImpactCount(category="Time-critical", count=sum(1 for c in approved if stated(c.business_impact.urgency))),
        ]
        impact.sort(key=lambda x: x.count, reverse=True)

        approvals = sum(1 for c in all_changes if c.story_approval)
        rejections = sum(1 for c in all_changes if c.story_approval and c.story_approval.status == "rejected")
        with_story = sum(1 for c in all_changes if c.user_story)
        first_time_pass = sum(1 for c in all_changes if c.user_story and c.user_story.revision_count == 0)

        return FactoryMetrics(
            totals=[
                Total(label="Total requests", value=len(all_changes)),
                Total(label="Awaiting approval", value=awaiting_approval),
                Total(label="In build", value=in_build),
                Total(label="In testing", value=in_testing),
                Total(label="Completed", value=completed),
                Total(label="Rejected / on hold", value=rejected),
            ],
            pipeline=[
                PipelineStage(stage="New", count=in_state("RECEIVED")),
                PipelineStage(stage="Story enhancement", count=in_state("REFINING")),
                PipelineStage(stage="Awaiting approval", count=awaiting_approval),
                PipelineStage(stage="In build", count=in_build),
                PipelineStage(stage="Testing", count=in_testing),
                PipelineStage(stage="Completed", count=completed),
            ],
            change_types=sorted(
                (ChangeTypeCount(type=t, count=n) for t, n in type_counts.items()),
                key=lambda x: x.count, reverse=True,
            ),
            business_impact_breakdown=impact,
            performance=Performance(
                first_time_success_rate=round((first_time_pass / with_story) * 100) if with_story else 0,
                human_approvals=approvals,
                human_rejections=rejections,
                change_volume=len(all_changes),
            ),
        )

    def activity_for_customer(self, customer_id: str, limit: int = 6) -> list[ActivityEntry]:
        all_changes = sorted(
            self._changes.list_for_customer(customer_id), key=lambda c: c.updated_at, reverse=True
        )
        return [
            ActivityEntry(
                time=_display_time(c.updated_at),
                change_id=c.id,
                description=c.title,
                state=c.state,
                updated_by=c.updated_by,
            )
            for c in all_changes[:limit]
        ]


def _display_time(iso_timestamp: str) -> str:
    """Same intent as mockApi.ts's toLocaleString formatting: this field
    is a display string, not a raw timestamp for the frontend to parse."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    return dt.strftime("%d %b, %H:%M")
