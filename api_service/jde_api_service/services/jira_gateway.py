"""
JiraGateway -- the boundary between the Jira sync service and the Jira
Cloud REST API (or, in mock mode, canned in-memory fixture data).

Mirrors mock_topdesk_connector.py's own boundary: nothing above this
module ever sees a Jira-shaped payload, only JiraIssueSummary. Unlike
the Topdesk connector (a fixture-only stand-in that never calls a real
API, because no live Topdesk instance exists), JiraHttpGateway below is
a genuine, live implementation -- JIRA_MOCK_MODE exists so the rest of
this service is buildable and testable before real credentials are
available (config.py's own docstring), the same role JDE_MCP_MOCK_MODE
already plays for the AIS connection.

No project key, status name or field id is ever hardcoded here -- every
method takes them as parameters, sourced from the caller's
JiraIntegrationConfig. That is the whole point of this module: Jade's
Jira hand-off is entirely configuration-driven.

Status/transition NAMES, not ids, are what config stores in this
increment (JiraIntegrationConfig.pickup_status / post_pickup_status).
list_statuses()/list_transitions() below exist so a later increment can
offer an admin a real dropdown sourced from Jira's own workflow instead
of free text, without changing JiraIntegrationConfig's shape or
JiraGateway's calling convention -- see that model's own docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx


class JiraGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraIssueSummary:
    key: str
    id: str
    summary: str
    description: str
    reporter: str
    created: str
    # Free-form source context (Work Type / Priority / Request Type,
    # where configured) -- see ChangeRequest.source_metadata. Carried
    # through for display only; never read here or by jira_sync_service
    # to make a routing decision -- that decision was already made in
    # Jira before the ticket ever reached the configured pickup status.
    metadata: dict[str, str] = field(default_factory=dict)
    # The current value of the configured Jade-id field on this issue,
    # if any -- lets jira_sync_service detect "write-back already
    # completed for this issue" without a second round trip.
    jade_id_field_value: Optional[str] = None


class JiraStatus(Protocol):
    name: str


class JiraGateway(Protocol):
    def search_issues_in_status(
        self, *, base_url: str, project_key: str, status_name: str,
        jade_id_field: str, request_type_field: str = "",
    ) -> list[JiraIssueSummary]: ...

    def set_field(self, *, base_url: str, issue_key: str, field_id: str, value: str) -> None: ...

    def add_comment(self, *, base_url: str, issue_key: str, body: str) -> None: ...

    def find_transition_id(self, *, base_url: str, issue_key: str, target_status_name: str) -> Optional[str]: ...

    def transition_issue(self, *, base_url: str, issue_key: str, transition_id: str) -> None: ...

    def list_project_statuses(self, *, base_url: str, project_key: str) -> list[str]: ...


def _jql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _adf_paragraph(text: str) -> dict:
    """Jira's v3 comment API requires Atlassian Document Format, not
    plain text -- this is the minimal single-paragraph shape."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


class JiraHttpGateway:
    """The real connector -- talks to a live Jira Cloud site via Basic
    Auth (API token), the auth shape Atlassian itself recommends for
    exactly this kind of single-site service-to-service integration.
    Only exercised when JDE_JIRA_MOCK_MODE is false. Credentials are
    supplied by the caller (registry.py's get_jira_gateway, reading
    this customer's own JiraCredentials via jira_credentials_service.py)
    rather than read from settings here -- see models/jira_integration.py's
    own docstring for why credentials are per-customer and Admin-entered
    in this pilot, not a deployment-level environment variable."""

    def __init__(
        self, *, email: str = "", api_token: str = "", transport: Optional[httpx.BaseTransport] = None
    ) -> None:
        self._email = email
        self._api_token = api_token
        # transport is a test-only seam (httpx.MockTransport) -- normal
        # construction (registry.py) always leaves it None, giving a
        # real network-backed client.
        self._http = httpx.Client(timeout=30.0, transport=transport)

    def _auth(self) -> tuple[str, str]:
        if not self._email or not self._api_token:
            raise JiraGatewayError(
                "This customer has no Jira credentials configured -- enter them under "
                "Admin > Integrations > Jira first."
            )
        return (self._email, self._api_token)

    def _get(self, base_url: str, path: str, **kwargs) -> httpx.Response:
        resp = self._http.get(f"{base_url}{path}", auth=self._auth(), **kwargs)
        resp.raise_for_status()
        return resp

    def _post(self, base_url: str, path: str, **kwargs) -> httpx.Response:
        resp = self._http.post(f"{base_url}{path}", auth=self._auth(), **kwargs)
        resp.raise_for_status()
        return resp

    def _put(self, base_url: str, path: str, **kwargs) -> httpx.Response:
        resp = self._http.put(f"{base_url}{path}", auth=self._auth(), **kwargs)
        resp.raise_for_status()
        return resp

    def search_issues_in_status(
        self, *, base_url: str, project_key: str, status_name: str,
        jade_id_field: str, request_type_field: str = "",
    ) -> list[JiraIssueSummary]:
        jql = f'project = "{_jql_quote(project_key)}" AND status = "{_jql_quote(status_name)}" ORDER BY created ASC'
        fields = ["summary", "description", "reporter", "created", "issuetype", "priority", jade_id_field]
        if request_type_field:
            fields.append(request_type_field)

        out: list[JiraIssueSummary] = []
        page_token: Optional[str] = None
        for _ in range(20):  # safety cap -- see module docstring on pagination
            body: dict = {"jql": jql, "fields": fields, "maxResults": 100}
            if page_token:
                body["nextPageToken"] = page_token
            resp = self._post(base_url, "/rest/api/3/search/jql", json=body)
            data = resp.json()
            for raw in data.get("issues", []):
                out.append(_issue_from_jira_json(raw, jade_id_field, request_type_field))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    def set_field(self, *, base_url: str, issue_key: str, field_id: str, value: str) -> None:
        self._put(base_url, f"/rest/api/3/issue/{issue_key}", json={"fields": {field_id: value}})

    def add_comment(self, *, base_url: str, issue_key: str, body: str) -> None:
        self._post(base_url, f"/rest/api/3/issue/{issue_key}/comment", json={"body": _adf_paragraph(body)})

    def find_transition_id(self, *, base_url: str, issue_key: str, target_status_name: str) -> Optional[str]:
        resp = self._get(base_url, f"/rest/api/3/issue/{issue_key}/transitions")
        for t in resp.json().get("transitions", []):
            if (t.get("to") or {}).get("name") == target_status_name:
                return t.get("id")
        return None

    def transition_issue(self, *, base_url: str, issue_key: str, transition_id: str) -> None:
        self._post(base_url, f"/rest/api/3/issue/{issue_key}/transitions", json={"transition": {"id": transition_id}})

    def list_project_statuses(self, *, base_url: str, project_key: str) -> list[str]:
        """Distinct status names actually reachable in this project's
        workflow(s) -- the data source a future admin-UI dropdown would
        use instead of free text (see this module's own docstring)."""
        resp = self._get(base_url, f"/rest/api/3/project/{project_key}/statuses")
        names: list[str] = []
        for issue_type in resp.json():
            for status in issue_type.get("statuses", []):
                name = status.get("name")
                if name and name not in names:
                    names.append(name)
        return names


def test_live_connection(
    *,
    base_url: str,
    email: str,
    api_token: str,
    project_key: str = "",
    transport: Optional[httpx.BaseTransport] = None,
) -> tuple[bool, str]:
    """Admin > Integrations > Jira's "Test Connection" button -- a
    deliberately stateless, ad-hoc connectivity check against
    whatever's currently typed in the form. Never persists anything
    (the Save action does that separately) and ALWAYS makes a real
    call to Jira regardless of JDE_JIRA_MOCK_MODE (that flag governs
    the sync pipeline, not this check -- the whole point of this
    button is to verify the real connection). The returned message is
    always safe to render as-is: it never contains the token, only the
    account's own display name (from Jira's own /myself response) and
    plain diagnostic text.

    GET /rest/api/3/myself proves the credential authenticates; a
    second GET /rest/api/3/project/{project_key} (only when a project
    key was given) additionally proves this account can see that
    specific project -- the same permission the sync handshake itself
    needs."""
    if not base_url:
        return False, "Jira site URL is required."
    if not email or not api_token:
        return False, "Email and API token are both required."
    base_url = base_url.rstrip("/")

    with httpx.Client(timeout=15.0, transport=transport) as http:
        try:
            resp = http.get(f"{base_url}/rest/api/3/myself", auth=(email, api_token))
        except httpx.RequestError as exc:
            return False, f"Could not reach {base_url}: {exc.__class__.__name__}"

        if resp.status_code == 401:
            return False, "Authentication failed -- check the email and API token."
        if resp.status_code == 403:
            return False, "Authenticated, but this account does not have permission to use the Jira API."
        if resp.status_code >= 400:
            return False, f"Jira returned HTTP {resp.status_code} for {base_url} -- check the site URL."

        display_name = (resp.json() or {}).get("displayName") or email

        if not project_key:
            return True, f"Connected to {base_url} as {display_name}. No project key given, so project access was not checked."

        try:
            proj_resp = http.get(f"{base_url}/rest/api/3/project/{project_key}", auth=(email, api_token))
        except httpx.RequestError as exc:
            return True, f"Connected as {display_name}, but could not verify project '{project_key}': {exc.__class__.__name__}"

        if proj_resp.status_code == 404:
            return False, f"Connected as {display_name}, but project '{project_key}' was not found or is not accessible to this account."
        if proj_resp.status_code >= 400:
            return False, f"Connected as {display_name}, but checking project '{project_key}' returned HTTP {proj_resp.status_code}."

        project_name = (proj_resp.json() or {}).get("name") or project_key
        return True, f"Connected to {base_url} as {display_name}. Project '{project_key}' ({project_name}) is accessible."


def _issue_from_jira_json(raw: dict, jade_id_field: str, request_type_field: str) -> JiraIssueSummary:
    fields = raw.get("fields") or {}
    reporter = fields.get("reporter") or {}
    metadata = {}
    issuetype = (fields.get("issuetype") or {}).get("name")
    if issuetype:
        metadata["workType"] = issuetype
    priority = (fields.get("priority") or {}).get("name")
    if priority:
        metadata["priority"] = priority
    if request_type_field:
        request_type = fields.get(request_type_field)
        if isinstance(request_type, dict):
            request_type = request_type.get("value") or request_type.get("name")
        if request_type:
            metadata["requestType"] = str(request_type)

    return JiraIssueSummary(
        key=raw.get("key", ""),
        id=raw.get("id", ""),
        summary=fields.get("summary") or "",
        description=_plain_text_from_description(fields.get("description")),
        reporter=reporter.get("displayName") or reporter.get("emailAddress") or "Jira (reporter not available)",
        created=fields.get("created") or "",
        metadata=metadata,
        jade_id_field_value=fields.get(jade_id_field),
    )


def _plain_text_from_description(description) -> str:
    """Jira v3 issue descriptions are Atlassian Document Format, not
    plain text -- flatten the text nodes verbatim, no summarising or
    editing (same "preserved verbatim" rule mock_topdesk_connector.py
    already follows for Topdesk's request text)."""
    if isinstance(description, str):
        return description
    if not isinstance(description, dict):
        return ""
    parts: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "text":
            parts.append(node.get("text", ""))
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    walk(description)
    return "".join(parts)


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


class JiraMockGateway:
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
