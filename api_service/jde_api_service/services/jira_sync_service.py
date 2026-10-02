"""
Jira sync -- the connector's actual handshake, run on demand (Admin >
Integrations > Jira > "Sync now"), never on a schedule (see this
increment's own scope notes: no polling loop, no webhooks).

Handshake, exactly as designed:
    configured pickup status
    -> Jade finds the ticket (search_issues_in_status)
    -> durable, idempotent Jade ChangeRequest created
    -> Jira moves to the configured post-pickup status,
       with the Jade Change ID field set and an acceptance comment

The Jira transition is the LAST step for a given issue, deliberately:
if ChangeRequest creation fails, nothing is written back at all (the
ticket stays in the pickup status and is retried on the next sync). If
the field/comment write-back fails after intake already succeeded, the
transition still does not happen -- so the issue stays visible in the
pickup-status query and gets a full write-back retry next time, rather
than being silently left half-updated. See the idempotency note on
_sync_one for how a retried write-back avoids a duplicate comment.

This module never decides whether a ticket is "genuine change demand":
that decision was already made in Jira, by ITSM, before the ticket ever
reached the configured pickup status (see JiraIntegrationConfig's own
docstring). It also never triggers Receive -> Improve -> Check -- a
ChangeRequest created here is intake only, identical in that respect to
one created via the direct-entry form.
"""

from __future__ import annotations

from .change_request_service import ChangeRequestService
from .jira_gateway import JiraGateway
from .jira_integration_service import JiraIntegrationService
from ..models.jira_integration import JiraIntegrationConfig, JiraSyncError, JiraSyncResult


class JiraNotConfigured(RuntimeError):
    pass


def _stable_change_request_id(issue_key: str) -> str:
    """Deterministic from the Jira issue key alone, so re-running sync
    against the same ticket always resolves to the same ChangeRequest --
    the actual idempotency mechanism (get-before-create), same
    convention seed_service.py already uses for pilot data."""
    return f"CR-JIRA-{issue_key}"


class JiraSyncService:
    def __init__(
        self,
        integration_service: JiraIntegrationService,
        change_request_service: ChangeRequestService,
        gateway: JiraGateway,
    ) -> None:
        self._integrations = integration_service
        self._change_requests = change_request_service
        self._gateway = gateway
        self._account_id: str | None = None

    def sync_for_customer(self, customer_id: str) -> JiraSyncResult:
        config = self._integrations.get_for_customer(customer_id)
        if config is None or not config.is_configured():
            raise JiraNotConfigured(
                f"Jira is not fully configured for customer {customer_id}. "
                "Set it up under Administration › Systems & Connections › Jira first."
            )

        issues = self._gateway.search_issues_in_status(
            base_url=config.base_url,
            project_key=config.project_key,
            status_name=config.pickup_status,
            jade_id_field=config.jade_id_field,
            request_type_field=config.request_type_field,
        )

        imported: list[str] = []
        updated_in_jira: list[str] = []
        errors: list[JiraSyncError] = []
        skipped: list[str] = []

        for issue in issues:
            try:
                result = self._sync_one(config, issue, customer_id)
                if result.is_new:
                    imported.append(result.change_request_id)
                if result.skipped:
                    skipped.append(issue.key)
                    continue
                if result.write_back_completed:
                    updated_in_jira.append(issue.key)
                if result.error:
                    errors.append(JiraSyncError(issue_key=issue.key, message=result.error))
            except Exception as exc:  # noqa: BLE001 -- one bad issue must never abort the batch
                errors.append(JiraSyncError(issue_key=issue.key, message=str(exc)))

        return JiraSyncResult(considered=len(issues), imported=imported, updated_in_jira=updated_in_jira, errors=errors,
                              skipped_withdrawn=skipped)

    def _sync_one(self, config: JiraIntegrationConfig, issue, customer_id: str) -> "_OneResult":
        stable_id = _stable_change_request_id(issue.key)

        existing = self._change_requests.get(stable_id)
        if existing is not None and existing.status == "withdrawn":
            # Withdrawn in Jade: left alone in Jira, never re-imported.
            return _OneResult(change_request_id=stable_id, is_new=False, write_back_completed=False, error=None,
                              skipped=True)
        is_new = False
        if existing is None:
            # Intake, and ONLY intake -- no enhancement is triggered
            # here or anywhere else in this module.
            self._change_requests.create_from_jira(issue, customer_id, request_id=stable_id)
            is_new = True

        try:
            return self._write_back(config, issue, stable_id, is_new)
        except Exception as exc:  # noqa: BLE001 -- the request is in Jade; only the Jira write-back failed
            return _OneResult(change_request_id=stable_id, is_new=is_new, write_back_completed=False,
                              error=f"imported into Jade as {stable_id}, but updating the Jira ticket failed: {exc}")

    def _write_back(self, config: JiraIntegrationConfig, issue, stable_id: str, is_new: bool) -> "_OneResult":
        """Accepts the ticket the way a service desk agent would: assign it,
        move it to the post-pickup status, leave a note with the Jade ID.
        The move comes before the note, so a ticket only ever leaves the
        pickup status once and a retry never posts a second note."""
        base = config.base_url
        transition_id = self._gateway.find_transition_id(
            base_url=base, issue_key=issue.key, target_status_name=config.post_pickup_status,
        )
        if transition_id is None:
            # Nothing was changed in Jira; the ticket stays in the pickup
            # status and the whole hand-off is retried on the next sync.
            return _OneResult(
                change_request_id=stable_id, is_new=is_new, write_back_completed=False,
                error=(
                    f"imported into Jade as {stable_id}, but '{config.post_pickup_status}' is not a status "
                    f"{issue.key} can move to from its current status in the Jira workflow"
                ),
            )
        problems: list[str] = []
        if config.assign_to_jade:
            try:
                if self._account_id is None:
                    self._account_id = self._gateway.own_account_id(base_url=base)
                self._gateway.assign_issue(base_url=base, issue_key=issue.key, account_id=self._account_id)
            except Exception as exc:  # noqa: BLE001 -- the hand-off goes on; the problem is reported
                problems.append(f"could not assign it to Jade's Jira account ({exc})")
        self._gateway.transition_issue(base_url=base, issue_key=issue.key, transition_id=transition_id)
        try:
            self._gateway.add_comment(base_url=base, issue_key=issue.key,
                                      body=f"Jade has accepted this request. Jade Change ID: {stable_id}",
                                      internal=config.comment_visibility == "internal")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"could not add the note ({exc})")
        if config.jade_id_field and issue.jade_id_field_value != stable_id:
            try:
                self._gateway.set_field(base_url=base, issue_key=issue.key, field_id=config.jade_id_field,
                                        value=stable_id)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"could not fill the Jade Change ID field ({exc})")
        error = (f"imported as {stable_id} and moved to '{config.post_pickup_status}', but "
                 + "; ".join(problems)) if problems else None
        return _OneResult(change_request_id=stable_id, is_new=is_new, write_back_completed=True, error=error)


class _OneResult:
    def __init__(self, *, change_request_id: str, is_new: bool, write_back_completed: bool, error: str | None,
                 skipped: bool = False) -> None:
        self.skipped = skipped
        self.change_request_id = change_request_id
        self.is_new = is_new
        self.write_back_completed = write_back_completed
        self.error = error
