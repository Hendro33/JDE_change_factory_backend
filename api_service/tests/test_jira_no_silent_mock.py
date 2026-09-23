"""
Real mode never falls back to the simulated Jira. A missing, unreadable,
incomplete or rejected setup is reported as "unavailable" and blocks the
operation; simulated data appears only in explicit demo mode
(JDE_JIRA_MOCK_MODE=true).
"""

from __future__ import annotations

import httpx
from cryptography.fernet import Fernet

from jde_api_service import config as api_config

from .conftest import headers
from .test_jira_integration import _config_payload


def _jira_change_ids(client) -> set[str]:
    return {c["id"] for c in client.get("/changes", headers=headers("vdb")).json() if c["id"].startswith("CR-JIRA-")}


def _save_credentials(client) -> None:
    r = client.put("/admin/jira-credentials", headers=headers("vdb"), json={"email": "bot@example.com", "apiToken": "tok"})
    assert r.status_code == 200, r.text


def test_nothing_configured_blocks_sync_and_imports_nothing(client):
    client.put("/admin/jira-integration", headers=headers("vdb"), json=_config_payload())
    r = client.post("/admin/jira-integration/sync", headers=headers("vdb"))
    assert r.status_code == 409
    assert r.json()["detail"].startswith("Jira integration unavailable: No Jira credential")
    assert _jira_change_ids(client) == set()
    row = next(i for i in client.get("/admin/integrations", headers=headers("vdb")).json() if i["name"] == "Jira Service Management")
    assert row["connected"] is False and row["detail"].startswith("Unavailable:")


def test_an_unreadable_token_blocks_sync_instead_of_mocking(client, monkeypatch):
    client.put("/admin/jira-integration", headers=headers("vdb"), json=_config_payload())
    _save_credentials(client)
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())  # key no longer matches
    r = client.post("/admin/jira-integration/sync", headers=headers("vdb"))
    assert r.status_code == 409 and "cannot be decrypted" in r.json()["detail"]
    assert _jira_change_ids(client) == set()


def test_a_wrong_token_is_reported_as_rejected_not_replaced_by_simulated_data(client, monkeypatch):
    from jde_api_service.services import registry
    from jde_api_service.services.jira_gateway import JiraHttpGateway

    client.put("/admin/jira-integration", headers=headers("vdb"), json=_config_payload())
    _save_credentials(client)
    assert client.get("/admin/jira-integration/status", headers=headers("vdb")).json()["state"] == "live"

    def jira_says_unauthorized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"errorMessages": ["Unauthorized"]})

    monkeypatch.setattr(
        registry, "JiraHttpGateway",
        lambda email, api_token: JiraHttpGateway(
            email=email, api_token=api_token, transport=httpx.MockTransport(jira_says_unauthorized)
        ),
    )
    r = client.post("/admin/jira-integration/sync", headers=headers("vdb"))
    assert r.status_code == 502
    assert "Jira rejected the request (HTTP 401)" in r.json()["detail"]
    assert _jira_change_ids(client) == set()


def test_simulated_jira_only_in_explicit_demo_mode(client, monkeypatch):
    client.put("/admin/jira-integration", headers=headers("vdb"), json=_config_payload())
    assert client.post("/admin/jira-integration/sync", headers=headers("vdb")).status_code == 409

    monkeypatch.setattr(api_config.settings, "jira_mock_mode", True)
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["state"] == "demo" and status["mockMode"] is True
    r = client.post("/admin/jira-integration/sync", headers=headers("vdb"))
    assert r.status_code == 200 and r.json()["imported"]
