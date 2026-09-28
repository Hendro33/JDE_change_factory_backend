"""
Derives FactoryMetrics/ActivityEntry from ChangeService's already
customer-scoped Change list -- same rule mockApi.ts documents for
itself: every number here is computed from real records, nothing is
hard-coded, so this keeps behaving correctly as real data grows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models.change import Change
from ..models.metrics import (
    ActivityEntry,
    BusinessDomainCount,
    BusinessImpactCount,
    ChangeTypeCount,
    FactoryMetrics,
    PipelineStage,
    Performance,
    Total,
)
from .business_domain_service import BusinessDomainService
from .change_service import ChangeService
from .delivery_queue_service import DeliveryQueueService

_IN_BUILD = {"APPROVED", "ARCHITECTING", "SPEC_READY", "CHANGE_APPROVED", "EXECUTING"}
_COMPLETED = {"CLOSED", "VALIDATED", "CNC_HANDOFF", "RESOLVED_WITHOUT_CHANGE"}
_REJECTED = {"REJECTED", "FAILED"}
# "Reached the backlog and is still live demand" -- excludes RECEIVED/
# REFINING (nothing to classify yet) and REJECTED/FAILED (no longer
# current demand), matching item 6's "distribution of CURRENT Change
# Requests/User Stories by Business Domain".
_DOMAIN_GOVERNED_STATES = {"BACKLOG_READY"} | _IN_BUILD | {"TESTING"} | _COMPLETED
# A story counts as "awaiting Domain Owner approval" until the Domain
# Owner has actually approved it -- None means the DomainReview sidecar
# hasn't even been opened yet (lazily created on first view), which is
# still, honestly, awaiting.
_PRE_DOMAIN_OWNER_APPROVAL_STAGES = {
    None, "ready_for_domain_owner", "domain_owner_reviewing",
    "domain_owner_requested_revision", "reviewer_agent_refining",
}


class MetricsService:
    def __init__(
        self,
        change_service: ChangeService,
        business_domain_service: Optional[BusinessDomainService] = None,
        delivery_queue_service: Optional[DeliveryQueueService] = None,
        delivered_at=None,
    ) -> None:
        self._changes = change_service
        # story id -> delivered time, per customer (the finalised as-built records).
        self._delivered_at = delivered_at or _delivered_at
        self._domains = business_domain_service
        self._delivery_queue = delivery_queue_service

    def _domain_breakdown(self, all_changes: list[Change], customer_id: str) -> list[BusinessDomainCount]:
        # Only changes that have actually reached the backlog are
        # counted at all: a request still in Receive/Improve/Check has
        # no business domain decision to report yet, honest-absence
        # rather than lumped into "Unclassified". Deliberately gated on
        # STATE, not on the DomainReview sidecar existing yet -- the
        # sidecar is created lazily on first Domain Owner view
        # (routers/domain_governance.py), and a story nobody has opened
        # yet must still show up here as unclassified, not vanish from
        # the dashboard until someone happens to look at it.
        governed = [c for c in all_changes if c.state in _DOMAIN_GOVERNED_STATES]
        if not governed:
            return []

        domains_by_id = {d.id: d for d in self._domains.list_for_customer(customer_id)} if self._domains else {}
        counts: dict[Optional[str], int] = {}
        for c in governed:
            counts[c.business_domain_id] = counts.get(c.business_domain_id, 0) + 1

        out = []
        for domain_id, count in counts.items():
            if domain_id is None:
                out.append(BusinessDomainCount(domain_id=None, domain_name="Unclassified / needs review", count=count))
            else:
                domain = domains_by_id.get(domain_id)
                out.append(
                    BusinessDomainCount(
                        domain_id=domain_id,
                        domain_name=domain.name if domain else domain_id,
                        apqc_code=domain.apqc_code if domain else "",
                        count=count,
                    )
                )
        out.sort(key=lambda x: x.count, reverse=True)
        return out

    def metrics_for_customer(self, customer_id: str, period: str = "lifetime", *, now: Optional[datetime] = None) -> FactoryMetrics:
        if period not in {"week", "month", "year", "lifetime"}:
            raise ValueError("Unknown Insights period")
        everything = self._changes.list_for_customer(customer_id)
        delivered = self._delivered_at(customer_id)
        if period == "lifetime":
            return self._compute(customer_id, everything, delivered, None, None)
        end = now or datetime.now(timezone.utc)
        length = timedelta(days={"week": 7, "month": 30, "year": 365}[period])
        start = end - length
        current = self._compute(customer_id, [c for c in everything if _created_in(c, start, end, True)], delivered,
                                start, end)
        # The same measures over the previous period of the same length: the
        # trend each figure shows (current minus previous).
        previous = self._compute(customer_id, [c for c in everything if _created_in(c, start - length, start)],
                                 delivered, start - length, start)
        prev_totals = {t.key: t.value for t in previous.totals}
        for t in current.totals:
            t.delta = t.value - prev_totals.get(t.key, 0)
        cur, prev = current.performance, previous.performance
        cur.change_volume_delta = cur.change_volume - prev.change_volume
        cur.first_time_success_delta = cur.first_time_success_rate - prev.first_time_success_rate
        cur.average_cycle_time_delta = (round(cur.average_cycle_time_days - prev.average_cycle_time_days, 1)
                                        if cur.delivered_count and prev.delivered_count else 0)
        return current

    def _compute(self, customer_id: str, all_changes: list[Change], delivered: dict[str, datetime],
                 start: Optional[datetime], end: Optional[datetime]) -> FactoryMetrics:
        def in_state(*states: str) -> int:
            return sum(1 for c in all_changes if c.state in states)

        incoming_requests = in_state("RECEIVED")
        awaiting_approval = in_state("BACKLOG_READY")
        awaiting_domain_owner = sum(
            1 for c in all_changes
            if c.state == "BACKLOG_READY" and c.domain_review_stage in _PRE_DOMAIN_OWNER_APPROVAL_STAGES
        )
        # Gate 1 -- Domain Owner already approved, Application Manager
        # hasn't authorised it for delivery yet.
        awaiting_application_manager = sum(
            1 for c in all_changes
            if c.state == "BACKLOG_READY" and c.domain_review_stage == "ready_for_application_manager"
        )
        # Gate 2 -- the Architect has proposed an exact change (or a
        # human did, via the CLI) and nobody has approved or rejected
        # it yet. change_approval only exists once a decision has been
        # made (change_service.py), so its absence alongside a real
        # exact_change is exactly "still pending".
        awaiting_exact_change_approval = sum(
            1 for c in all_changes if c.exact_change is not None and c.change_approval is None
        )
        in_delivery = sum(e.change_id in {c.id for c in all_changes} for e in self._delivery_queue.list_for_customer(customer_id)) if self._delivery_queue else 0
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

        from .lifecycle import HEALTH_LABELS, PHASE_LABELS, PHASES

        # Cycle time: from the request's arrival to its finalised as-built
        # record (delivered), for stories delivered in this period.
        cycles = []
        for c in self._changes.list_for_customer(customer_id) if start is not None else all_changes:
            done = delivered.get(c.id)
            if done is None or (start is not None and not (start <= done <= end)):
                continue
            created = _parse(c.created_at)
            if created is not None and done >= created:
                cycles.append((done - created).total_seconds() / 86400)
        cycle_avg = round(sum(cycles) / len(cycles), 1) if cycles else 0

        lifecycles = [c.lifecycle for c in all_changes if c.lifecycle is not None]
        phase_counts = [PipelineStage(stage=PHASE_LABELS[p], count=sum(1 for lc in lifecycles if lc.phase == p))
                        for p in PHASES]
        health_counts = [PipelineStage(stage=label, count=sum(1 for lc in lifecycles if lc.health == h))
                         for h, label in HEALTH_LABELS.items()]

        return FactoryMetrics(
            phases=phase_counts,
            health=health_counts,
            totals=[
                Total(key="incoming_requests", label="Incoming Requests", value=incoming_requests),
                Total(key="awaiting_domain_owner", label="User Stories awaiting Domain Owner approval", value=awaiting_domain_owner),
                Total(key="awaiting_application_manager", label="User Stories awaiting Application Manager decision", value=awaiting_application_manager),
                Total(key="awaiting_exact_change_approval", label="Changes awaiting Exact Change Approval", value=awaiting_exact_change_approval),
                Total(key="in_delivery", label="Changes in Delivery", value=in_delivery),
                Total(key="awaiting_business_validation", label="Awaiting Business Validation", value=in_testing),
                Total(key="completed", label="Completed", value=completed),
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
            business_domain_breakdown=self._domain_breakdown(all_changes, customer_id),
            performance=Performance(
                average_cycle_time_days=cycle_avg, delivered_count=len(cycles),
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


def _parse(iso: Optional[str]) -> Optional[datetime]:
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _created_in(change: Change, start: datetime, end: datetime, include_end: bool = False) -> bool:
    at = _parse(change.created_at)
    return at is not None and start <= at and (at <= end if include_end else at < end)


def _delivered_at(customer_id: str) -> dict[str, datetime]:
    """story id -> when its as-built record was first finalised."""
    from ..persistence.db import connection

    with connection() as conn:
        rows = conn.execute("SELECT story_id, MIN(finalised_at) AS at FROM as_built_records WHERE company_id = ? "
                            "AND status = 'final' AND finalised_at IS NOT NULL GROUP BY story_id",
                            (customer_id,)).fetchall()
    out = {}
    for r in rows:
        at = _parse(r["at"])
        if at is not None:
            out[r["story_id"]] = at
    return out


def _display_time(iso_timestamp: str) -> str:
    """Same intent as mockApi.ts's toLocaleString formatting: this field
    is a display string, not a raw timestamp for the frontend to parse."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    return dt.strftime("%d %b, %H:%M")
