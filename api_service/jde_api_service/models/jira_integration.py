"""
Jira Service Management hand-off -- per-customer connector configuration,
credentials and sync results.

Design summary (see the architecture assessment this implements):
  - Jira/ITSM remains responsible for intake and triage. Jade only ever
    picks up tickets a human has already moved to the configured pickup
    status; nothing here decides that on Jira's behalf.
  - This configuration is customer-scoped, mirroring EngagementScope --
    different customers can point at different Jira sites, projects and
    status names without any code change.
  - Credentials (JiraCredentials below) are ALSO customer-scoped and
    entered through Admin > Integrations > Jira, not environment
    variables -- a deliberate, explicitly pilot-scoped exception to
    this codebase's usual "no secrets through the Admin API" rule (see
    routers/admin.py's own docstring). Persistence is plain
    JsonFileStore, same as every other api_service-owned collection --
    NOT a secrets manager, NOT encrypted at rest, on purpose: this is
    the simplest reasonable mechanism for a short-lived pilot with a
    single operator, not a production posture. The docs (Section 19.7)
    now say so explicitly, flagging a real secrets provider and
    Admin-restricted write access as future hardening before this
    reaches production. JiraCredentials is NEVER used as a router
    response_model anywhere -- JiraConnectionStatus reports only
    whether a credential is present, never its value, and
    JiraTestConnectionResult reports only a safe, token-free message.
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
    # 0 = never saved; see persistence/revisions.py.
    revision: int = 0
    updated_at: Optional[str] = None
    # Always the authenticated user who saved -- never client-supplied.
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
    # The revision the client loaded; required once a config exists.
    # Any client-sent updatedBy is ignored -- the actor is the session.
    expected_revision: Optional[int] = None


class JiraCredentials(ApiModel):
    """Persisted, per-customer Jira email + API token. See this
    module's own docstring for the pilot-scoped storage decision.
    NEVER declare this as a FastAPI response_model -- every endpoint
    that touches it returns JiraConnectionStatus instead."""

    customer_id: str
    email: str = ""
    api_token: str = ""
    revision: int = 0
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class JiraCredentialsUpdate(ApiModel):
    """Write-only replacement: the stored token is never shown back, so
    a client has nothing to compare against -- replacing it is always a
    deliberate overwrite, attributed to the authenticated user. (No
    expected_revision, unlike the config.)"""

    email: str
    api_token: str


class JiraTestConnectionInput(ApiModel):
    """Deliberately stateless and separate from JiraCredentialsUpdate:
    "Test Connection" checks whatever is currently typed in the form,
    whether or not it has been saved yet, and never persists it."""

    base_url: str
    project_key: str = ""
    email: str
    api_token: str


class JiraTestConnectionResult(ApiModel):
    ok: bool
    # Always safe to render as-is -- never contains the token (see
    # jira_gateway.test_live_connection, which builds this message).
    message: str


class JiraConnectionStatus(ApiModel):
    """Status only -- NEVER a credential, same rule AisConnectionStatus
    already follows. credentials_configured now reflects THIS customer's
    own JiraCredentials record (see this module's own docstring) --
    still never the value itself."""

    # True only in explicit demo mode (JDE_JIRA_MOCK_MODE=true).
    mock_mode: bool
    credentials_configured: bool
    config_configured: bool
    # demo / live / unavailable (see registry.jira_mode); never a silent mock.
    state: str = "unavailable"
    unavailable_reason: str = ""
    # How the stored token is held: none / encrypted / plaintext (legacy) /
    # unreadable (encrypted under a key this server does not have).
    credential_storage: str = "none"
    # Whether this server can save a credential at all (encryption key set).
    credential_encryption_available: bool = False


class JiraSyncError(ApiModel):
    issue_key: str
    message: str


class JiraSyncResult(ApiModel):
    considered: int
    imported: list[str] = []
    updated_in_jira: list[str] = []
    errors: list[JiraSyncError] = []
