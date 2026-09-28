"""
A test double for Jira at the HTTP boundary -- used by the TEST SUITE only.

It exercises the same write-back sequence (field set, comment, transition)
the real gateway performs, against whatever status names are passed in.
The product never uses it: a customer's Jira is live or unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from jde_api_service.services.jira_gateway import JiraIssueSummary


@dataclass
class _MockIssueState:
    key: str
    id: str
    summary: str
    description: str
    reporter: str
    created: str
    status: str
    metadata: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    comments: list[str] = field(default_factory=list)


def _default_mock_seed() -> list[_MockIssueState]:
    # Arbitrary fixture content, exactly like mock_topdesk_connector.py's
    # TOPDESK_TICKETS -- the status value here is a seed default only
    # (matching the pickup status name used as the example throughout
    # this integration's own design discussion); it is never read or
    # special-cased by any connector logic, which always filters by
    # whatever status name the caller passes in.
    return [
        _MockIssueState(
            key="JADE-101", id="10101",
            summary="Default delivery date is wrong on sales orders",
            description=(
                "When our sales team enters a new sales order, the requested delivery date "
                "defaults to today. We would like it to default to 7 working days out instead."
            ),
            reporter="BicycleWorks Sales Team",
            created="2026-09-01T09:00:00.000+0000",
            status="Ready for Jade",
            metadata={"workType": "Change", "priority": "Medium"},
        ),
        _MockIssueState(
            key="JADE-104", id="10104",
            summary="Warehouse cannot see available stock at a glance",
            description=(
                "Warehouse staff need On hand, Allocated, Available, On order and Backordered "
                "for an item in one place instead of interpreting several separate quantities."
            ),
            reporter="BicycleWorks Warehouse",
            created="2026-09-03T14:30:00.000+0000",
            status="Ready for Jade",
            metadata={"workType": "Improvement", "priority": "Low"},
        ),
    ]


class FakeJiraGateway:
    """Mock mode -- an in-memory stand-in exercising the exact same
    write-back sequence (field set, comment, transition) the real
    gateway performs, against whatever status names are passed in, so
    the whole handshake is demonstrably configuration-driven even
    without a live Jira site. A fresh instance is constructed per call
    (registry.py's convention), so state does not persist between
    requests -- ChangeRequestService's own get-before-create idempotency
    is what's actually load-bearing (and IS real, file-backed) for
    "re-running sync never creates a duplicate"; see
    test_jira_integration.py for the seeded, stateful test coverage of
    the write-back short-circuit and ordering."""

    def __init__(self, seed: Optional[list[_MockIssueState]] = None) -> None:
        self._issues: dict[str, _MockIssueState] = {i.key: i for i in (seed if seed is not None else _default_mock_seed())}

    def search_issues_in_status(
        self, *, base_url: str, project_key: str, status_name: str,
        jade_id_field: str, request_type_field: str = "",
    ) -> list[JiraIssueSummary]:
        return [
            JiraIssueSummary(
                key=i.key, id=i.id, summary=i.summary, description=i.description,
                reporter=i.reporter, created=i.created, metadata=dict(i.metadata),
                jade_id_field_value=i.fields.get(jade_id_field),
            )
            for i in self._issues.values()
            if i.status == status_name
        ]

    def set_field(self, *, base_url: str, issue_key: str, field_id: str, value: str) -> None:
        self._issues[issue_key].fields[field_id] = value

    def add_comment(self, *, base_url: str, issue_key: str, body: str) -> None:
        self._issues[issue_key].comments.append(body)

    def find_transition_id(self, *, base_url: str, issue_key: str, target_status_name: str) -> Optional[str]:
        # Every status is reachable in the mock -- id just encodes the
        # target name so transition_issue() can apply it statelessly.
        return f"mock-transition::{target_status_name}"

    def transition_issue(self, *, base_url: str, issue_key: str, transition_id: str) -> None:
        prefix = "mock-transition::"
        if transition_id.startswith(prefix):
            self._issues[issue_key].status = transition_id[len(prefix):]

    def list_project_statuses(self, *, base_url: str, project_key: str) -> list[str]:
        return sorted({i.status for i in self._issues.values()})
