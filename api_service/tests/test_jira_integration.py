"""
Tests for the Jira Service Management hand-off connector: per-customer
configuration (Admin > Integrations > Jira), the sync handshake
(configured pickup status -> durable ChangeRequest -> configured
post-pickup status + Jade id field + acceptance comment), and the
things the design explicitly requires:
  - no hardcoded status/project/field names (jira_sync_service.py takes
    every one of them from JiraIntegrationConfig),
  - source metadata (Work Type/Priority/Request Type) is imported but
    never used to decide whether an item is picked up,
  - re-running sync never creates a duplicate ChangeRequest, and never
    re-posts the acceptance comment once it has already landed,
  - the Jira transition never happens before intake has durably
    succeeded,
  - importing does not itself trigger Receive -> Improve -> Check.
"""

from __future__ import annotations

from jde_api_service import config as api_config
from jde_api_service.models.jira_integration import JiraIntegrationConfigUpdate
from jde_api_service.services.change_request_service import ChangeRequestService
from jde_api_service.services.jira_gateway import JiraHttpGateway, JiraMockGateway, _MockIssueState
from jde_api_service.services.jira_integration_service import JiraIntegrationService
from jde_api_service.services.jira_sync_service import JiraSyncService
from jde_api_service.services.registry import get_jira_gateway

from .conftest import headers


def _config_payload(**overrides) -> dict:
    payload = {
        "baseUrl": "https://bicycleworks.atlassian.net",
        "projectKey": "CON",
        "pickupStatus": "Ready for Jade",
        "postPickupStatus": "Jade - In Progress",
        "jadeIdField": "customfield_10057",
        "requestTypeField": "",
        "updatedBy": "Hendro",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------
# Configuration -- Admin > Integrations > Jira
# ---------------------------------------------------------------------
def test_jira_integration_config_is_customer_scoped_and_editable(client):
    r = client.get("/admin/jira-integration", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json()["baseUrl"] == ""
    assert r.json()["updatedAt"] is None

    r = client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["projectKey"] == "CON"
    assert body["pickupStatus"] == "Ready for Jade"
    assert body["updatedBy"] == "Hendro"
    assert body["updatedAt"]
    # The API token is never part of this model -- confirm it can't even
    # be asked for, let alone leaked.
    assert "token" not in str(body).lower()
    assert "apiToken" not in body

    # Scoped: nhd must not see vdb's Jira configuration.
    r = client.get("/admin/jira-integration", headers=headers(customer="nhd"))
    assert r.json()["baseUrl"] == ""


def test_jira_integration_normalizes_a_trailing_slash(client):
    r = client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload(baseUrl="https://bicycleworks.atlassian.net/"))
    assert r.status_code == 200
    assert r.json()["baseUrl"] == "https://bicycleworks.atlassian.net"


def test_jira_integration_rejects_a_project_or_queue_url(client):
    r = client.put(
        "/admin/jira-integration", headers=headers(customer="vdb"),
        json=_config_payload(baseUrl="https://bicycleworks.atlassian.net/jira/software/projects/CON/issues"),
    )
    assert r.status_code == 422
    assert "site url" in r.json()["detail"].lower()

    # Nothing was saved -- a rejected save must not leave a half-written config.
    r2 = client.get("/admin/jira-integration", headers=headers(customer="vdb"))
    assert r2.json()["baseUrl"] == ""


def test_jira_integration_rejects_a_non_http_url(client):
    r = client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload(baseUrl="ftp://bicycleworks.atlassian.net"))
    assert r.status_code == 422


def test_jira_status_never_exposes_credentials(client):
    r = client.get("/admin/jira-integration/status", headers=headers(customer="vdb"))
    assert r.status_code == 200
    body = r.json()
    assert body["mockMode"] is True  # default in this test environment: no live Jira credential set
    assert body["credentialsConfigured"] is False
    assert body["configConfigured"] is False
    dumped = str(body).lower()
    for forbidden in ("password", "token", "secret"):
        assert forbidden not in dumped


def test_sync_refuses_when_not_configured(client):
    r = client.post("/admin/jira-integration/sync", headers=headers(customer="vdb"))
    assert r.status_code == 409


def test_integrations_list_reflects_jira_configuration_state(client):
    # No credentials configured for this customer -- stays mock, honestly
    # reported as "no credential yet" rather than a deployment-wide claim.
    r = client.get("/admin/integrations", headers=headers(customer="vdb"))
    jira_row = next(i for i in r.json() if i["name"] == "Jira Service Management")
    assert jira_row["connected"] is False
    assert "no jira credential" in jira_row["detail"].lower()


# ---------------------------------------------------------------------
# End-to-end sync via the router (mock gateway, as wired by default)
# ---------------------------------------------------------------------
def test_sync_end_to_end_creates_change_requests_without_enhancing_them(client):
    client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload())

    r = client.post("/admin/jira-integration/sync", headers=headers(customer="vdb"))
    assert r.status_code == 200
    result = r.json()
    assert result["considered"] == 2
    assert set(result["imported"]) == {"CR-JIRA-JADE-101", "CR-JIRA-JADE-104"}
    assert set(result["updatedInJira"]) == {"JADE-101", "JADE-104"}
    assert result["errors"] == []

    changes = client.get("/changes", headers=headers(customer="vdb")).json()
    jira_changes = [c for c in changes if c["id"] in ("CR-JIRA-JADE-101", "CR-JIRA-JADE-104")]
    assert len(jira_changes) == 2
    for c in jira_changes:
        assert c["source"] == "Jira"
        assert c["sourceReference"].startswith("Jira JADE-")
        assert c["state"] == "RECEIVED"
        # Importing must never itself imply a User Story.
        assert c["userStory"] is None
        assert c["processingStage"] is None
        # Source context imported, present for display.
        assert "workType" in c["sourceMetadata"]

    # Re-running sync must never create a duplicate ChangeRequest --
    # the persisted store (not the per-call mock gateway) is what
    # actually guarantees this.
    r2 = client.post("/admin/jira-integration/sync", headers=headers(customer="vdb"))
    assert r2.json()["imported"] == []


def test_sync_is_customer_scoped(client):
    client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload())
    client.post("/admin/jira-integration/sync", headers=headers(customer="vdb"))

    # nhd has no Jira configuration of its own -- unaffected.
    r = client.get("/changes", headers=headers(customer="nhd"))
    ids = {c["id"] for c in r.json()}
    assert "CR-JIRA-JADE-101" not in ids


# ---------------------------------------------------------------------
# Handshake correctness against a directly-controlled mock gateway
# ---------------------------------------------------------------------
def _issue(key: str, status: str, **overrides) -> _MockIssueState:
    base = dict(
        key=key, id=key, summary=f"Summary for {key}", description=f"Description for {key}",
        reporter="Some Reporter", created="2026-09-01T09:00:00.000+0000", status=status,
    )
    base.update(overrides)
    return _MockIssueState(**base)


def _services(isolated_dirs, gateway):
    change_requests = ChangeRequestService(str(isolated_dirs["api_data_dir"] / "change_requests"))
    integrations = JiraIntegrationService()
    sync = JiraSyncService(integrations, change_requests, gateway)
    return change_requests, integrations, sync


def test_no_hardcoded_status_or_field_names(isolated_dirs):
    """Unusual, arbitrary status/field names -- if the connector had
    any hardcoded default, this would fail to find or move the issue."""
    gateway = JiraMockGateway(seed=[_issue("XX-1", status="Triaged -> Send to AI Team")])
    change_requests, integrations, sync = _services(isolated_dirs, gateway)
    integrations.upsert(
        "cust1",
        JiraIntegrationConfigUpdate(
            base_url="https://example.atlassian.net", project_key="XX",
            pickup_status="Triaged -> Send to AI Team", post_pickup_status="AI Team Working On It",
            jade_id_field="customfield_99999",
        ),
        actor="Tester",
    )

    result = sync.sync_for_customer("cust1")

    assert result.considered == 1
    assert result.imported == ["CR-JIRA-XX-1"]
    assert result.errors == []
    assert gateway._issues["XX-1"].status == "AI Team Working On It"
    assert gateway._issues["XX-1"].fields["customfield_99999"] == "CR-JIRA-XX-1"


def test_write_back_never_happens_before_intake_persists(isolated_dirs, monkeypatch):
    gateway = JiraMockGateway(seed=[_issue("XX-2", status="Ready for Jade")])
    change_requests, integrations, sync = _services(isolated_dirs, gateway)
    integrations.upsert(
        "cust1",
        JiraIntegrationConfigUpdate(
            base_url="https://example.atlassian.net", project_key="XX",
            pickup_status="Ready for Jade", post_pickup_status="Jade - In Progress",
            jade_id_field="customfield_1",
        ),
        actor="Tester",
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("disk is full")

    monkeypatch.setattr(change_requests, "create_from_jira", _boom)

    result = sync.sync_for_customer("cust1")

    assert result.imported == []
    assert result.updated_in_jira == []
    assert len(result.errors) == 1 and result.errors[0].issue_key == "XX-2"
    # No field/comment/transition ever attempted for an issue whose
    # intake failed.
    assert gateway._issues["XX-2"].status == "Ready for Jade"
    assert gateway._issues["XX-2"].fields == {}
    assert gateway._issues["XX-2"].comments == []


def test_retry_after_transition_failure_does_not_repost_comment(isolated_dirs):
    class _FlakyGateway(JiraMockGateway):
        def __init__(self, seed):
            super().__init__(seed)
            self.transition_lookups = 0

        def find_transition_id(self, **kwargs):
            self.transition_lookups += 1
            if self.transition_lookups == 1:
                return None  # simulates "post-pickup status not yet reachable"
            return super().find_transition_id(**kwargs)

    gateway = _FlakyGateway(seed=[_issue("XX-3", status="Ready for Jade")])
    change_requests, integrations, sync = _services(isolated_dirs, gateway)
    integrations.upsert(
        "cust1",
        JiraIntegrationConfigUpdate(
            base_url="https://example.atlassian.net", project_key="XX",
            pickup_status="Ready for Jade", post_pickup_status="Jade - In Progress",
            jade_id_field="customfield_1",
        ),
        actor="Tester",
    )

    first = sync.sync_for_customer("cust1")
    assert first.imported == ["CR-JIRA-XX-3"]
    assert first.updated_in_jira == []
    assert len(first.errors) == 1
    assert gateway._issues["XX-3"].fields["customfield_1"] == "CR-JIRA-XX-3"
    assert len(gateway._issues["XX-3"].comments) == 1
    assert gateway._issues["XX-3"].status == "Ready for Jade"  # never transitioned

    second = sync.sync_for_customer("cust1")
    assert second.imported == []  # not re-created
    assert second.updated_in_jira == ["XX-3"]
    assert second.errors == []
    # The field short-circuit must have skipped a second comment.
    assert len(gateway._issues["XX-3"].comments) == 1
    assert gateway._issues["XX-3"].status == "Jade - In Progress"


def test_source_metadata_is_imported_but_never_used_for_routing(isolated_dirs):
    gateway = JiraMockGateway(seed=[
        _issue("XX-4", status="Ready for Jade", metadata={"workType": "Incident", "priority": "High"}),
        _issue("XX-5", status="Ready for Jade", metadata={"workType": "Change", "priority": "Low"}),
    ])
    change_requests, integrations, sync = _services(isolated_dirs, gateway)
    integrations.upsert(
        "cust1",
        JiraIntegrationConfigUpdate(
            base_url="https://example.atlassian.net", project_key="XX",
            pickup_status="Ready for Jade", post_pickup_status="Jade - In Progress",
            jade_id_field="customfield_1",
        ),
        actor="Tester",
    )

    result = sync.sync_for_customer("cust1")

    # Both items picked up and written back identically -- an "Incident"
    # work type is not filtered or treated differently at this stage;
    # that decision already happened in Jira before either ticket
    # reached the configured pickup status.
    assert set(result.imported) == {"CR-JIRA-XX-4", "CR-JIRA-XX-5"}
    assert set(result.updated_in_jira) == {"XX-4", "XX-5"}
    incident_cr = change_requests.get("CR-JIRA-XX-4")
    assert incident_cr.source_metadata["workType"] == "Incident"
    assert incident_cr.status == "received"  # not classified/rejected here


# ---------------------------------------------------------------------
# Credentials -- Admin > Integrations > Jira (email + API token), the
# one deliberate pilot-scoped exception to "no credential through the
# Admin API" (see routers/admin.py's own docstring). Every assertion
# here is about what must NEVER be exposed, exactly like the existing
# "token not in body" checks above for JiraIntegrationConfig.
# ---------------------------------------------------------------------
def _credentials_payload(**overrides) -> dict:
    payload = {"email": "bot@example.com", "apiToken": "super-secret-token", "updatedBy": "Hendro"}
    payload.update(overrides)
    return payload


def test_update_jira_credentials_never_echoes_the_token(client):
    r = client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())
    assert r.status_code == 200
    body = r.json()
    # Saving a valid credential is, by itself, enough to go live -- no
    # JDE_JIRA_MOCK_MODE or other backend file edit involved.
    assert body == {
        "mockMode": False, "credentialsConfigured": True, "configConfigured": False,
        "credentialStorage": "encrypted", "credentialEncryptionAvailable": True,
    }
    dumped = str(body).lower()
    for forbidden in ("super-secret-token", "apitoken", "email"):
        assert forbidden not in dumped


def test_jira_status_reflects_saved_credentials(client):
    before = client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()
    assert before["credentialsConfigured"] is False

    client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())

    after = client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()
    assert after["credentialsConfigured"] is True
    assert "super-secret-token" not in str(after)


def test_jira_credentials_are_customer_scoped(client):
    client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())

    nhd_status = client.get("/admin/jira-integration/status", headers=headers(customer="nhd")).json()
    assert nhd_status["credentialsConfigured"] is False


def test_deployment_force_mock_overrides_a_configured_credential(client, monkeypatch):
    """JDE_JIRA_MOCK_MODE=true is the one remaining deployment-level
    switch -- it still wins even once a customer has saved real
    credentials, e.g. for a shared demo/staging environment."""
    monkeypatch.setattr(api_config.settings, "jira_mock_mode", True)
    client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())

    status = client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()
    assert status["credentialsConfigured"] is True
    assert status["mockMode"] is True


# ---------------------------------------------------------------------
# Test Connection -- stateless, checks whatever is currently typed,
# always a real call regardless of mock mode. The router wiring is
# tested here with test_live_connection monkeypatched (its own HTTP
# behaviour is covered directly in test_jira_test_connection.py).
# ---------------------------------------------------------------------
def test_test_connection_endpoint_wires_the_form_values_through(client, monkeypatch):
    captured = {}

    def fake_test_live_connection(*, base_url, email, api_token, project_key=""):
        captured.update(base_url=base_url, email=email, api_token=api_token, project_key=project_key)
        return True, f"Connected to {base_url} as Jade Bot."

    monkeypatch.setattr("jde_api_service.routers.admin.test_live_connection", fake_test_live_connection)

    r = client.post(
        "/admin/jira-integration/test-connection",
        headers=headers(customer="vdb"),
        json={"baseUrl": "https://bicycleworks.atlassian.net", "projectKey": "CON", "email": "bot@example.com", "apiToken": "secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Connected to https://bicycleworks.atlassian.net as Jade Bot."}
    assert captured == {
        "base_url": "https://bicycleworks.atlassian.net", "email": "bot@example.com",
        "api_token": "secret", "project_key": "CON",
    }
    # Purely stateless -- nothing was saved by testing.
    status = client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()
    assert status["credentialsConfigured"] is False


def test_test_connection_reports_failure_without_a_500(client, monkeypatch):
    monkeypatch.setattr(
        "jde_api_service.routers.admin.test_live_connection",
        lambda **kwargs: (False, "Authentication failed -- check the email and API token."),
    )
    r = client.post(
        "/admin/jira-integration/test-connection",
        headers=headers(customer="vdb"),
        json={"baseUrl": "https://x.atlassian.net", "email": "bot@example.com", "apiToken": "wrong"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "wrong" not in str(r.json())


# ---------------------------------------------------------------------
# get_jira_gateway wiring -- a customer with no saved credentials stays
# mock (no error: the connector must stay exercisable before real
# credentials exist); a customer with saved credentials goes live from
# that alone, with no JDE_JIRA_MOCK_MODE or other env var involved;
# JDE_JIRA_MOCK_MODE=true still force-overrides everyone to mock.
# ---------------------------------------------------------------------
def test_get_jira_gateway_is_mock_by_default(isolated_dirs):
    assert isinstance(get_jira_gateway("cust1"), JiraMockGateway)


def test_get_jira_gateway_stays_mock_without_credentials_even_when_not_force_mocked(isolated_dirs, monkeypatch):
    monkeypatch.setattr(api_config.settings, "jira_mock_mode", False)
    assert isinstance(get_jira_gateway("cust1"), JiraMockGateway)


def test_get_jira_gateway_goes_live_purely_from_saved_credentials(isolated_dirs):
    from jde_api_service.models.jira_integration import JiraCredentialsUpdate
    from jde_api_service.services.registry import get_jira_credentials_service

    # No monkeypatching of settings at all -- this is the out-of-the-box
    # default (jira_mock_mode defaults false) plus an Admin-saved
    # credential, exactly the "no .env or backend file edit" flow.
    get_jira_credentials_service().upsert(
        "cust1", JiraCredentialsUpdate(email="bot@example.com", api_token="secret-token"), actor="Hendro"
    )

    gateway = get_jira_gateway("cust1")
    assert isinstance(gateway, JiraHttpGateway)
    assert gateway._auth() == ("bot@example.com", "secret-token")

    # A different, unconfigured customer is unaffected -- credentials
    # are never shared across customers, and there's no error, just mock.
    assert isinstance(get_jira_gateway("cust2"), JiraMockGateway)


def test_get_jira_gateway_force_mock_overrides_a_configured_credential(isolated_dirs, monkeypatch):
    from jde_api_service.models.jira_integration import JiraCredentialsUpdate
    from jde_api_service.services.registry import get_jira_credentials_service

    get_jira_credentials_service().upsert(
        "cust1", JiraCredentialsUpdate(email="bot@example.com", api_token="secret-token"), actor="Hendro"
    )
    monkeypatch.setattr(api_config.settings, "jira_mock_mode", True)
    assert isinstance(get_jira_gateway("cust1"), JiraMockGateway)


# ---------------------------------------------------------------------
# Disconnect -- removes a saved credential (never just blanks it), and
# the connector falls straight back to mock, same as before one was
# ever entered.
# ---------------------------------------------------------------------
def test_disconnect_removes_the_credential(client):
    client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())
    assert client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()["credentialsConfigured"] is True

    r = client.request("DELETE", "/admin/jira-credentials", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json() == {
        "mockMode": True, "credentialsConfigured": False, "configConfigured": False,
        "credentialStorage": "none", "credentialEncryptionAvailable": True,
    }

    status = client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).json()
    assert status["credentialsConfigured"] is False


def test_disconnect_leaves_site_config_untouched(client):
    client.put("/admin/jira-integration", headers=headers(customer="vdb"), json=_config_payload())
    client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())

    client.request("DELETE", "/admin/jira-credentials", headers=headers(customer="vdb"))

    config = client.get("/admin/jira-integration", headers=headers(customer="vdb")).json()
    assert config["projectKey"] == "CON"  # untouched -- only the credential was removed


def test_disconnect_is_idempotent(client):
    r = client.request("DELETE", "/admin/jira-credentials", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json()["credentialsConfigured"] is False


def test_get_jira_gateway_goes_mock_again_after_disconnect(isolated_dirs):
    from jde_api_service.models.jira_integration import JiraCredentialsUpdate
    from jde_api_service.services.registry import get_jira_credentials_service

    service = get_jira_credentials_service()
    service.upsert("cust1", JiraCredentialsUpdate(email="bot@example.com", api_token="secret-token"), actor="Hendro")
    assert isinstance(get_jira_gateway("cust1"), JiraHttpGateway)

    service.delete("cust1")
    assert isinstance(get_jira_gateway("cust1"), JiraMockGateway)


# ---------------------------------------------------------------------
# Admin role -- Jira configuration/credential routes require the Admin
# role (require_role("admin"), dependencies.py) on top of company
# entitlement. Replaces the earlier JDE_ADMIN_API_KEY shared-secret
# mechanism entirely -- real per-user roles now, not a deployment-wide
# key. Never applied to status or sync (Demand > Requests' "Retrieve
# new requests"), which stay gated by company access alone.
# ---------------------------------------------------------------------
def test_admin_role_allows_jira_config_access(client):
    # `client` (Hendro) holds every role, including admin, on vdb.
    r = client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())
    assert r.status_code == 200


def test_non_admin_role_is_refused_on_jira_config_routes(viewer_client):
    # `viewer_client` holds ONLY dashboard_viewer on vdb -- no admin role.
    r = viewer_client.put("/admin/jira-credentials", headers=headers(customer="vdb"), json=_credentials_payload())
    assert r.status_code == 403


def test_admin_role_guards_the_config_routes_but_not_status_or_sync(viewer_client):
    bare = headers(customer="vdb")

    assert viewer_client.get("/admin/jira-integration", headers=bare).status_code == 403
    assert viewer_client.put("/admin/jira-integration", headers=bare, json=_config_payload()).status_code == 403
    assert viewer_client.put("/admin/jira-credentials", headers=bare, json=_credentials_payload()).status_code == 403
    assert viewer_client.request("DELETE", "/admin/jira-credentials", headers=bare).status_code == 403
    assert viewer_client.post(
        "/admin/jira-integration/test-connection", headers=bare,
        json={"baseUrl": "https://x.atlassian.net", "email": "a@b.com", "apiToken": "x"},
    ).status_code == 403

    # status is open to any active member, including Dashboard Viewer.
    assert viewer_client.get("/admin/jira-integration/status", headers=bare).status_code == 200
    # sync is a write, so dashboard_viewer is refused by require_write_access
    # specifically (403), not the admin-role check (which would also be 403,
    # but for a different reason) -- see require_write_access's own docstring.
    assert viewer_client.post("/admin/jira-integration/sync", headers=bare).status_code == 403
