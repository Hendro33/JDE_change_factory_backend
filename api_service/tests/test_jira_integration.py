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

from jde_api_service.models.jira_integration import JiraIntegrationConfigUpdate
from jde_api_service.services.change_request_service import ChangeRequestService
from jde_api_service.services.jira_gateway import JiraMockGateway, _MockIssueState
from jde_api_service.services.jira_integration_service import JiraIntegrationService
from jde_api_service.services.jira_sync_service import JiraSyncService

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
    r = client.get("/admin/integrations", headers=headers(customer="vdb"))
    jira_row = next(i for i in r.json() if i["name"] == "Jira Service Management")
    assert jira_row["connected"] is False
    assert "mock mode" in jira_row["detail"].lower()


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
    integrations = JiraIntegrationService(str(isolated_dirs["api_data_dir"] / "jira_integrations"))
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
            jade_id_field="customfield_99999", updated_by="Tester",
        ),
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
            jade_id_field="customfield_1", updated_by="Tester",
        ),
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
            jade_id_field="customfield_1", updated_by="Tester",
        ),
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
            jade_id_field="customfield_1", updated_by="Tester",
        ),
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
