"""
Assembles the frontend's Change shape from three real, independent
sources:

  1. mcp_server's backlog.py / approval.py / evidence.py file stores --
     read-only, via directory listing (the same non-invasive pattern
     review_ui.py already uses). Nothing here calls a mutating
     function in that package, and nothing in that package is modified.
  2. This service's own change_request_service, for intake items that
     have not been promoted to a story yet.
  3. This service's own enhancement_run_service, which records what an
     in-flight or completed Receive/Improve/Check run (orchestration_driver.py)
     actually produced -- overlaid onto (1) or (2) for presentation,
     never a source of truth of its own.

Every backlog-derived story is customer-scoped through
customer_link_service's sidecar. A story with no link entry is
excluded from every result here -- fail-safe, not fail-open: an
unattributed story is never guessed into a customer's view.

A story_id that exists BOTH as a ChangeRequest and as a promoted
backlog record (the normal case once orchestration has run against it)
is presented once, as the backlog-derived Change -- the more complete,
more authoritative record.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from .. import config as _config  # noqa: F401  (forces the mcp_server sys.path bootstrap)
from jde_mcp_server import backlog, approval, capability_catalog, execution
from jde_mcp_server import config as mcp_config
from jde_mcp_server import scope as mcp_scope

from ..models.change import (
    AcceptanceCriterion,
    ApprovalRecord,
    BusinessImpact,
    Change,
    EvidenceRecord,
    ExactChange,
    ExecutionStatus,
    Reconciliation,
    TestStep,
    UserStory,
)
from ..models.change_request import ChangeRequest
from ..models.enhancement_run import EnhancementRun
from .architecture_review_service import ArchitectureReviewService
from .change_request_service import ChangeRequestService
from .customer_link_service import CustomerLinkService
from .domain_review_service import DomainReviewService
from .enhancement_run_service import EnhancementRunService

_VALID_SOURCES = {"Business", "Support / Topdesk", "Optimisation", "DevOps", "Jira"}


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _coerce_source(raw: Optional[str]) -> str:
    return raw if raw in _VALID_SOURCES else "Business"


_VALID_COMPLEXITY = {"Low", "Medium", "High", "Unknown"}


def _coerce_complexity(raw: Optional[str]) -> str:
    # backlog.py stores whatever string a tool caller (Check Agent's
    # own propose_to_backlog call, or a human running agents manually)
    # actually passed, with no validation of its own -- it is untrusted
    # free text as far as this assembler is concerned, same as `source`
    # above. "Unknown" is the honest fallback, not a guess.
    return raw if raw in _VALID_COMPLEXITY else "Unknown"


def _business_impact_from(raw: Optional[dict]) -> BusinessImpact:
    raw = raw or {}
    return BusinessImpact(
        financial_impact=raw.get("financial_impact") or "",
        operational_reach=raw.get("operational_reach") or "",
        risk_compliance=raw.get("risk_compliance") or "",
        strategic_alignment=raw.get("strategic_alignment") or "",
        urgency=raw.get("urgency") or "",
    )


def _evidence_records(raw_entries: list[dict]) -> list[EvidenceRecord]:
    out = []
    for i, e in enumerate(raw_entries, start=1):
        out.append(
            EvidenceRecord(
                entry_id=f"E{i}",
                stage=e.get("stage", ""),
                detail=e.get("detail", ""),
                actor=e.get("actor", ""),
                agent_versions=e.get("agent_versions"),
                captured_at=_iso(e.get("captured_at")) or "",
                prev_hash=e.get("prev_hash", ""),
                entry_hash=e.get("entry_hash", ""),
            )
        )
    return out


def _all_backlog_records() -> list[dict[str, Any]]:
    if not os.path.isdir(backlog.BACKLOG_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(backlog.BACKLOG_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(backlog.BACKLOG_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _all_change_records() -> list[dict[str, Any]]:
    if not os.path.isdir(approval.CHANGE_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(approval.CHANGE_DIR)):
        if fn.endswith(".json") and not fn.startswith("."):  # skip in-flight temp files
            with open(os.path.join(approval.CHANGE_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
    return out


def _evidence_for(story_id: str) -> list[dict[str, Any]]:
    path = os.path.join(mcp_config.settings.evidence_dir, f"{story_id}.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _latest_change_record_for(story_id: str) -> Optional[dict[str, Any]]:
    candidates = [c for c in _all_change_records() if c.get("story_id") == story_id]
    if not candidates:
        return None
    approved = [c for c in candidates if c.get("status") == "approved"]
    pool = approved or candidates
    return max(pool, key=lambda c: c.get("created_at", 0))


_LEGACY_SECTION_RE = re.compile(
    r"\n\n(business_context|acceptance_criteria|business_rules|test_script|open_questions):\s*", re.MULTILINE
)
_LEGACY_AC_LINE_RE = re.compile(r"^-\s*(AC\d+):\s*(.+?)(?:\s+verified_by:\s*(\S.*))?$")
_LEGACY_OPEN_Q_LINE_RE = re.compile(r"^\d+\.\s*(.+)$")


def _parse_legacy_backlog_story(raw: str) -> Optional[UserStory]:
    """Best-effort parser for stories entered directly as a backlog.py
    record (propose_to_backlog's user_story parameter is a plain string,
    Section: "not ours to change") using the same
    "statement / business_context: / acceptance_criteria: / test_script: /
    open_questions:" convention this pilot's human-authored backlog
    entries already use (see e.g. CR-BW-T001's real record). This is
    read-only presentation, exactly like the rest of this assembler --
    it never rewrites the stored record, and it degrades gracefully
    (returns None) for any text that doesn't follow the convention, in
    which case the caller falls back to showing the whole text as the
    statement, same as before this parser existed."""
    parts = _LEGACY_SECTION_RE.split(raw)
    if len(parts) < 3:
        return None  # doesn't follow the convention -- nothing to gain by guessing

    statement = parts[0].strip()
    sections: dict[str, str] = {}
    for label, body in zip(parts[1::2], parts[2::2]):
        sections[label] = body.strip()
    if "acceptance_criteria" not in sections:
        return None

    acceptance_criteria = []
    for line in sections.get("acceptance_criteria", "").splitlines():
        m = _LEGACY_AC_LINE_RE.match(line.strip())
        if m:
            acceptance_criteria.append(AcceptanceCriterion(id=m.group(1), text=m.group(2).strip(), verified_by=m.group(3)))

    test_script: list[TestStep] = []
    raw_test_script = sections.get("test_script", "")
    try:
        parsed = json.loads(raw_test_script)
        for step in parsed.get("steps", []):
            expected = step.get("expected_output", {})
            expected_text = ", ".join(f"{k}: {v}" for k, v in expected.items()) if isinstance(expected, dict) else str(expected)
            test_script.append(
                TestStep(id=f"T{step.get('step', len(test_script) + 1)}", action=step.get("action", ""), expected=expected_text)
            )
    except (json.JSONDecodeError, AttributeError):
        pass  # not JSON, or not the expected shape -- honestly show no test script rather than a mangled one

    open_questions = []
    for line in sections.get("open_questions", "").splitlines():
        m = _LEGACY_OPEN_Q_LINE_RE.match(line.strip())
        if m:
            open_questions.append(m.group(1).strip())

    business_rules = [ln.strip()[2:].strip() for ln in sections.get("business_rules", "").splitlines()
                      if ln.strip().startswith("- ")]

    return UserStory(
        statement=statement,
        business_context=sections.get("business_context", ""),
        business_rules=business_rules,
        acceptance_criteria=acceptance_criteria,
        test_script=test_script,
        open_questions=open_questions,
        quality_status="passed",
    )


def _execution_status(change_record: dict) -> ExecutionStatus:
    write = (change_record.get("execution") or {}).get(execution.WRITE) or {}
    attempts = write.get("attempts") or []
    last = attempts[-1] if attempts else {}
    return ExecutionStatus(
        write_state=execution.effective_state(change_record, execution.WRITE),
        test_state=execution.effective_state(change_record, execution.TEST),
        attempts=len(attempts),
        last_attempt_at=_iso(last.get("started_at")),
        last_detail=last.get("detail", "") or "",
        before_value=next((a.get("before_value") for a in reversed(attempts) if a.get("before_value") is not None), None),
        write_reconciliations=_reconciliations(change_record, execution.WRITE),
        test_reconciliations=_reconciliations(change_record, execution.TEST),
    )


def _reconciliations(change_record: dict, kind: str) -> list[Reconciliation]:
    out = []
    for r in ((change_record.get("execution") or {}).get(kind) or {}).get("reconciliations", []):
        out.append(Reconciliation(
            kind=r.get("kind", f"{kind}_reconciliation"), at=_iso(r["at"]) or "",
            actor=r.get("actor") or {"display_name": r.get("verified_by", "")},
            verified_by=r["verified_by"], source=r["source"], outcome=r["outcome"],
            target=r.get("target") or {}, observed=r.get("observed") or {},
            observed_value=r.get("observed_value"), evidence_reference=r.get("evidence_reference", ""),
            evidence_entry_hash=r.get("evidence_entry_hash"), settles_attempt_id=r.get("settles_attempt_id"),
            note=r.get("note", ""),
        ))
    return out


def _change_from_story(
    record: dict[str, Any],
    customer_id: str,
    run: Optional[EnhancementRun],
    origin: Optional[ChangeRequest],
    domain_review_service: Optional[DomainReviewService] = None,
    architecture_review_service: Optional[ArchitectureReviewService] = None,
) -> Change:
    story_id = record["story_id"]
    change_record = _latest_change_record_for(story_id)

    exact_change = None
    change_approval = None
    if change_record:
        op = change_record.get("operation", {})
        capability_id = change_record.get("capability_id")
        capability_status = None
        capability_executable = None
        if capability_id:
            cap = capability_catalog.get_capability(capability_id)
            if cap is not None:
                capability_status = cap.get("validation", {}).get("status")
                try:
                    spike = mcp_scope.find_spike_experiment(
                        mcp_scope.load_company_scope(customer_id),
                        capability_id,
                        change_record.get("capability_revision", ""),
                        op.get("application", ""),
                        op.get("version", ""),
                        op.get("option", ""),
                        change_record.get("environment", "DEV"),
                    )
                except mcp_scope.ScopeViolation:
                    spike = None  # no saved scope for this company yet -- can't be spike-approved
                capability_executable = capability_status == "validated" or (
                    capability_status == "needs_spike" and spike is not None
                )
        exact_change = ExactChange(
            tool=op.get("tool", ""),
            application=op.get("application", ""),
            version=op.get("version", ""),
            option=op.get("option", ""),
            proposed_value=str(op.get("value", "")),
            environment=change_record.get("environment", "DEV"),
            test_orchestration=op.get("test_orchestration") or "",
            capability_id=capability_id,
            capability_status=capability_status,
            capability_executable=capability_executable,
            execution=_execution_status(change_record),
        )
        if change_record.get("status") in ("approved", "rejected"):
            change_approval = ApprovalRecord(
                approval_id=change_record["change_id"],
                kind="change",
                status=change_record["status"],
                change_hash=change_record.get("change_hash"),
                approved_by=change_record.get("approved_by"),
                approved_at=_iso(change_record.get("approved_at")),
                expires_at=_iso(change_record.get("expires_at")),
                note=change_record.get("decision_note"),
            )

    story_approval = None
    if record.get("decision") in ("approved", "rejected"):
        story_approval = ApprovalRecord(
            approval_id=f"AP-{story_id}-S",
            kind="story",
            status=record["decision"],
            approved_by=record.get("decided_by"),
            approved_at=_iso(record.get("decided_at")),
            note=record.get("decision_note"),
        )

    created_at = _iso(record.get("proposed_at")) or datetime.now(timezone.utc).isoformat()
    updated_at = (
        _iso(record.get("resolved_at"))
        or _iso(record.get("decided_at"))
        or created_at
    )
    statement = record.get("user_story") or story_id

    # propose_to_backlog's user_story parameter is a plain string (the
    # existing MCP tool's actual signature, not ours to change) -- so
    # backlog.py's own record can't carry acceptance criteria, test
    # script or open questions. When this story came through
    # orchestration_driver.py, the run record has the full enriched
    # story it actually produced; use that for display. Otherwise fall
    # back to the minimal reconstruction, as before.
    if run and run.user_story and run.check_outcome == "proposed_to_backlog":
        user_story = run.user_story
    else:
        user_story = _parse_legacy_backlog_story(statement) or UserStory(statement=statement, quality_status="passed")
    # A person-applied story revision (process refinement) supersedes both.
    from ..process import refinement

    revised = refinement.latest_story(customer_id, story_id)
    if revised is not None:
        user_story = revised

    # backlog.py's own record only ever carries the AI-generated
    # statement (propose_to_backlog's user_story parameter is a plain
    # string) -- it was never the original customer input. Where this
    # story has a matching ChangeRequest (the normal case once it went
    # through orchestration_driver.py), that record's raw_content is
    # the actual original text, and must not be conflated with what the
    # agents produced from it.
    if origin is not None:
        title = origin.title
        source = origin.business_source
        source_reference = origin.source_reference
        original_request = origin.raw_content
    else:
        title = statement[:120]
        source = _coerce_source(record.get("source"))
        source_reference = ""
        original_request = statement

    domain_review = domain_review_service.get(story_id) if domain_review_service else None
    architecture_run = architecture_review_service.get(story_id) if architecture_review_service else None

    return Change(
        id=story_id,
        customer_id=customer_id,
        title=title,
        source=source,
        source_reference=source_reference,
        original_request=original_request,
        state=record.get("state", "BACKLOG_READY"),
        business_impact=_business_impact_from(record.get("business_impact")),
        complexity_signal=_coerce_complexity(record.get("rough_complexity_signal")),
        created_at=created_at,
        updated_at=updated_at,
        updated_by=record.get("decided_by") or record.get("resolved_by") or "",
        processing_stage=run.stage if run else None,
        processing_error=run.error if run else None,
        business_domain_id=domain_review.business_domain_id if domain_review else None,
        domain_review_stage=domain_review.stage if domain_review else None,
        architecture_review_stage=architecture_run.stage if architecture_run else None,
        architecture_review_error=architecture_run.error if architecture_run else None,
        user_story=user_story,
        story_approval=story_approval,
        # architect_decision/implementation_spec are this sidecar's own
        # reasoning capture (architecture_review.py's own docstring on
        # why) -- exact_change/change_approval below stay sourced ONLY
        # from approval.py's authoritative record, never duplicated here.
        architect_decision=architecture_run.architect_decision if architecture_run else None,
        implementation_spec=architecture_run.implementation_spec if architecture_run else None,
        exact_change=exact_change,
        change_approval=change_approval,
        evidence=_evidence_records(_evidence_for(story_id)),
    )


def _change_from_request(cr: ChangeRequest, run: Optional[EnhancementRun]) -> Change:
    received_iso = cr.received_at.isoformat()

    state = "RECEIVED"
    user_story = None
    business_impact = None
    complexity_signal = "Unknown"
    processing_stage = None
    processing_error = None
    updated_at = received_iso

    if run is not None:
        processing_stage = run.stage
        updated_at = run.updated_at
        if run.stage == "failed":
            processing_error = run.error
            # Stays RECEIVED -- a failed run hasn't actually refined
            # anything; the story is exactly where it was before the
            # attempt, just with a visible error rather than a silent one.
        else:
            state = "REFINING"
            if run.user_story:
                user_story = run.user_story
            if run.business_impact:
                business_impact = run.business_impact
            if run.rough_complexity_signal:
                complexity_signal = _coerce_complexity(run.rough_complexity_signal)

    return Change(
        id=cr.id,
        customer_id=cr.customer_id,
        title=cr.title,
        source=cr.business_source,
        source_reference=cr.source_reference,
        original_request=cr.raw_content,
        state=state,
        business_impact=business_impact or BusinessImpact(),
        complexity_signal=complexity_signal,
        created_at=received_iso,
        updated_at=updated_at,
        updated_by=cr.requester,
        processing_stage=processing_stage,
        processing_error=processing_error,
        source_metadata=cr.source_metadata,
        user_story=user_story,
        evidence=[],
    )


class ChangeService:
    def __init__(
        self,
        change_request_service: ChangeRequestService,
        customer_link_service: CustomerLinkService,
        enhancement_run_service: Optional[EnhancementRunService] = None,
        domain_review_service: Optional[DomainReviewService] = None,
        architecture_review_service: Optional[ArchitectureReviewService] = None,
    ) -> None:
        self._change_requests = change_request_service
        self._links = customer_link_service
        self._runs = enhancement_run_service
        self._domain_reviews = domain_review_service
        self._architecture_reviews = architecture_review_service

    def _run_for(self, request_id: str) -> Optional[EnhancementRun]:
        return self._runs.get(request_id) if self._runs else None

    def _stories_for_customer(self, customer_id: str) -> list[Change]:
        out = []
        for record in _all_backlog_records():
            story_id = record["story_id"]
            if self._links.customer_for(story_id) != customer_id:
                continue
            origin = self._change_requests.get(story_id)
            out.append(
                _change_from_story(
                    record, customer_id, self._run_for(story_id), origin,
                    self._domain_reviews, self._architecture_reviews,
                )
            )
        return out

    def list_for_customer(self, customer_id: str) -> list[Change]:
        stories = self._stories_for_customer(customer_id)
        promoted_ids = {c.id for c in stories}
        requests = [
            _change_from_request(cr, self._run_for(cr.id))
            for cr in self._change_requests.list_for_customer(customer_id)
            if cr.id not in promoted_ids
        ]
        return sorted(stories + requests, key=lambda c: c.updated_at, reverse=True)

    def get_for_customer(self, change_id: str, customer_id: str) -> Optional[Change]:
        # A promoted backlog record takes precedence over a same-id
        # ChangeRequest (the normal post-orchestration state).
        for record in _all_backlog_records():
            if record["story_id"] == change_id:
                if self._links.customer_for(change_id) != customer_id:
                    return None
                origin = self._change_requests.get(change_id)
                return _change_from_story(
                    record, customer_id, self._run_for(change_id), origin,
                    self._domain_reviews, self._architecture_reviews,
                )

        if change_id.startswith("CR-"):
            cr = self._change_requests.get(change_id)
            if cr is None or cr.customer_id != customer_id:
                return None
            return _change_from_request(cr, self._run_for(change_id))
        return None

    def backlog_for_customer(self, customer_id: str) -> list[Change]:
        return [c for c in self._stories_for_customer(customer_id) if c.state == "BACKLOG_READY"]
