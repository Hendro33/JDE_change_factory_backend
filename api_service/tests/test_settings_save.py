"""
Setting up a customer: every save either stores what was entered and says
so, or stores nothing and says why -- and what was stored is still there
after a restart.
"""

from __future__ import annotations

import pytest

from .conftest import headers

KEY = "sk-ant-api03-" + "k" * 40  # never a real credential


@pytest.mark.no_auto_ai
def test_the_ai_key_can_be_entered_first_and_the_connection_is_created_with_defaults(client):
    assert client.get("/admin/ai/connection", headers=headers("nhd")).json().get("configured") in (False, None)
    r = client.put("/admin/ai/connection/credential", headers=headers("nhd"), json={"apiKey": KEY})
    assert r.status_code == 200, r.text
    assert KEY not in r.text
    from jde_api_service.ai import connection

    resolved = connection.resolve_for_run("nhd")
    assert resolved.api_key == KEY and resolved.model == connection.DEFAULT_MODEL


def test_a_jira_credential_needs_both_fields_and_says_what_is_missing(client):
    r = client.put("/admin/jira-credentials", headers=headers("vdb"), json={"email": "bot@example.com", "apiToken": " "})
    assert r.status_code == 422 and "API token" in r.json()["detail"] and "Nothing was saved" in r.json()["detail"]
    r = client.put("/admin/jira-credentials", headers=headers("vdb"), json={"email": "", "apiToken": "tok"})
    assert r.status_code == 422 and "e-mail" in r.json()["detail"]
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialsConfigured"] is False


@pytest.mark.no_auto_ai
def test_ai_jira_and_jde_settings_survive_a_restart(client):
    from fastapi.testclient import TestClient

    from jde_api_service.main import app

    from ._discovery import profile_body

    assert client.put("/admin/ai/connection/credential", headers=headers("vdb"), json={"apiKey": KEY}).status_code == 200
    assert client.put("/admin/jira-integration", headers=headers("vdb"), json={
        "baseUrl": "https://acme.atlassian.net", "projectKey": "CON", "pickupStatus": "Ready for Jade",
        "postPickupStatus": "Jade - In Progress", "jadeIdField": "customfield_10057", "requestTypeField": "",
    }).status_code == 200
    assert client.put("/admin/jira-credentials", headers=headers("vdb"),
                      json={"email": "bot@example.com", "apiToken": "tok-123"}).status_code == 200
    r = client.put("/admin/jde/profile", headers=headers("vdb"), json={**profile_body("vdb"), "expectedRevision": None})
    assert r.status_code == 200, r.text
    rev = r.json()["revision"]
    assert client.put("/admin/jde/credential", headers=headers("vdb"), json={
        "username": "JADEDISC", "password": "a-jde-password", "expectedRevision": rev}).status_code == 200

    with TestClient(app):  # a restart: startup runs again on the same database
        pass

    ai = client.get("/admin/ai/connection", headers=headers("vdb")).json()
    assert ai["model"] and KEY not in str(ai)
    from jde_api_service.ai import connection

    assert connection.resolve_for_run("vdb").api_key == KEY
    jira = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert jira["state"] == "live" and jira["credentialsConfigured"] is True
    jde = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert jde["configured"] and jde["config"]["aisBaseUrl"] == "https://ais-vdb.customer.example"
    assert jde["credentialConfigured"] is True
