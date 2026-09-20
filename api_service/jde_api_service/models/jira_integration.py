"""
Jira Service Management hand-off -- per-customer connector configuration
and sync results.

Design summary (see the architecture assessment this implements):
  - Jira/ITSM remains responsible for intake and triage. Jade only ever
    picks up tickets a human has already moved to the configured pickup
    status; nothing here decides that on Jira's behalf.
  - This configuration is customer-scoped, mirroring EngagementScope --
    different customers can point at different Jira sites, projects and
    status names without any code change.
  - The API token itself is NOT part of this model and is never
    persisted in this JSON store -- see config.py's jira_email/
    jira_api_token (deployment-level environment variables, the same
    limitation EngagementScope's own docstring already documents for
    the AIS connection: one credential for the whole deployment today,
    not yet truly per-customer). JiraConnectionStatus below reports
    whether that deployment-level credential is present, never its
    value.
  - Status names are configured as plain strings for this increment
    (pickup_status / post_pickup_status), not looked up from a fixed
    enum -- see jira_gateway.py's JiraGateway.list_project_statuses,
    which exists so an admin UI can later offer a dropdown sourced from
    the real Jira workflow instead of free text, without changing this
    model's shape.
"""

from __future__ import annotations

from typing import Optional

from .base import ApiModel


class JiraIntegrationConfig(ApiModel):
    customer_id: str
    base_url: str = ""
    project_key: str = ""
    # The Jira workflow status a human moves a ticket to once ITSM has
    # decided it is genuine change/improvement demand for Jade to pick
    # up (e.g. "Ready for Jade"). Jade only ever reads tickets sitting
    # in this exact status -- it is never inferred or defaulted.
    pickup_status: str = ""
    # The status Jade transitions the ticket to once intake has
    # durably succeeded (e.g. "Jade - In Progress").
    post_pickup_status: str = ""
    # The Jira custom field id (e.g. "customfield_10057") Jade writes
    # its own Change Request id into.
    jade_id_field: str = ""
    # Optional: a Jira custom field id carrying JSM's own Request Type
    # (e.g. "customfield_10010"), imported into source_metadata for
    # display only -- see jira_gateway.py. Left blank, Request Type is
    # simply not captured; Work Type and Priority (both standard Jira
    # fields) are always captured regardless of this setting.
    request_type_field: str = ""
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def is_configured(self) -> bool:
        return bool(self.base_url and self.project_key and self.pickup_status and self.post_pickup_status and self.jade_id_field)


class JiraIntegrationConfigUpdate(ApiModel):
    base_url: str
    project_key: str
    pickup_status: str
    post_pickup_status: str
    jade_id_field: str
    request_type_field: str = ""
    updated_by: str


class JiraConnectionStatus(ApiModel):
    """Status only -- NEVER a credential, same rule AisConnectionStatus
    already follows. The credential itself is deployment-level (see
    this module's own docstring); this reports only whether one is
    present, not per-customer."""

    mock_mode: bool
    credentials_configured: bool
    config_configured: bool


class JiraSyncError(ApiModel):
    issue_key: str
    message: str


class JiraSyncResult(ApiModel):
    considered: int
    imported: list[str] = []
    updated_in_jira: list[str] = []
    errors: list[JiraSyncError] = []
