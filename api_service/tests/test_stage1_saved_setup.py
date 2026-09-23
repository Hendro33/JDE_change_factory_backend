"""
Stage 1 increment S1-1: reliable saved setup and user identity.

  * Who saved a setting comes from the authenticated session, never from
    an `updatedBy` the client sends.
  * A save based on a stale copy is refused (409 with the current
    revision) instead of silently overwriting someone else's change; a
    save of an existing record that states no revision at all is 428.
  * Dashboard thresholds are company data on the server (Admin-only
    writes, every member reads, never visible across companies), not
    browser localStorage.
  * JSON-file records are written atomically.
"""

from __future__ import annotations

import os

from .conftest import headers


def _scope_payload(**overrides) -> dict:
    payload = {
        "toolsRelease": "9.2.7",
        "functionalAgent": {"approvedVersions": [], "neverTouchCategories": [], "approvers": []},
        "technicalAgent": {"authorizedObjectTypes": [], "reservedProductCode": "", "namingPrefix": "", "approvers": []},
    }
    payload.update(overrides)
    return payload


def _jira_payload(**overrides) -> dict:
    payload = {
        "baseUrl": "https://bicycleworks.atlassian.net",
        "projectKey": "CON",
        "pickupStatus": "Ready for Jade",
        "postPickupStatus": "Jade - In Progress",
        "jadeIdField": "customfield_10057",
        "requestTypeField": "",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------
# Actor identity
# ---------------------------------------------------------------------
def test_engagement_scope_actor_comes_from_the_session_not_the_client(ellen_client):
    r = ellen_client.put(
        "/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload(updatedBy="Somebody Else")
    )
    assert r.status_code == 200, r.text
    assert r.json()["updatedBy"] == "Ellen Vos"


def test_jira_config_actor_comes_from_the_session_not_the_client(ellen_client):
    r = ellen_client.put("/admin/jira-integration", headers=headers("vdb"), json=_jira_payload(updatedBy="Hendro"))
    assert r.status_code == 200, r.text
    assert r.json()["updatedBy"] == "Ellen Vos"


def test_business_domain_actor_comes_from_the_session(ellen_client):
    r = ellen_client.post(
        "/admin/business-domains", headers=headers("vdb"),
        json={"apqcCode": "4.4", "name": "Deliver", "level": "4.4", "updatedBy": "Hendro"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["updatedBy"] == "Ellen Vos"
    assert r.json()["revision"] == 1


# ---------------------------------------------------------------------
# Concurrent-overwrite protection
# ---------------------------------------------------------------------
def test_engagement_scope_rejects_a_stale_save(client, ellen_client):
    first = client.put("/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload())
    assert first.status_code == 200
    assert first.json()["revision"] == 1

    # Ellen loaded revision 1 and saves: accepted, revision 2.
    r = ellen_client.put(
        "/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload(toolsRelease="9.2.8", expectedRevision=1)
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 2

    # Hendro still holds revision 1: refused, and told what is current.
    r = client.put(
        "/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload(toolsRelease="9.2.6", expectedRevision=1)
    )
    assert r.status_code == 409
    assert r.json()["currentRevision"] == 2
    assert client.get("/admin/engagement-scope", headers=headers("vdb")).json()["toolsRelease"] == "9.2.8"

    # Saving an existing record without stating a revision is refused too.
    r = client.put("/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload())
    assert r.status_code == 428
    assert r.json()["currentRevision"] == 2


def test_jira_config_rejects_a_stale_save(client):
    assert client.put("/admin/jira-integration", headers=headers("vdb"), json=_jira_payload()).json()["revision"] == 1
    r = client.put("/admin/jira-integration", headers=headers("vdb"), json=_jira_payload(projectKey="NEW", expectedRevision=1))
    assert r.status_code == 200
    assert r.json()["revision"] == 2
    r = client.put("/admin/jira-integration", headers=headers("vdb"), json=_jira_payload(projectKey="OLD", expectedRevision=1))
    assert r.status_code == 409
    assert client.get("/admin/jira-integration", headers=headers("vdb")).json()["projectKey"] == "NEW"


def test_business_domain_status_rejects_a_stale_save(client):
    domain_id = client.post(
        "/admin/business-domains", headers=headers("vdb"), json={"apqcCode": "4.4", "name": "Deliver", "level": "4.4"}
    ).json()["id"]
    url = f"/admin/business-domains/{domain_id}/status"
    assert client.put(url, headers=headers("vdb"), json={"status": "proposed"}).status_code == 428
    assert client.put(url, headers=headers("vdb"), json={"status": "proposed", "expectedRevision": 1}).json()["revision"] == 2
    r = client.put(url, headers=headers("vdb"), json={"status": "retired", "expectedRevision": 1})
    assert r.status_code == 409
    assert r.json()["currentRevision"] == 2


# ---------------------------------------------------------------------
# Dashboard thresholds
# ---------------------------------------------------------------------
def test_dashboard_thresholds_default_until_saved(client):
    body = client.get("/admin/dashboard-thresholds", headers=headers("vdb")).json()
    assert body == {
        "warnAt": 10, "criticalAt": 25, "configured": False, "revision": 0, "updatedAt": None, "updatedBy": None,
    }


def test_dashboard_thresholds_saved_by_admin_and_read_by_every_member(ellen_client, viewer_client):
    r = ellen_client.put(
        "/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 3, "criticalAt": 7, "expectedRevision": 0}
    )
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is True
    assert r.json()["updatedBy"] == "Ellen Vos"
    assert r.json()["revision"] == 1

    # A different user in a different session sees the same saved values.
    body = viewer_client.get("/admin/dashboard-thresholds", headers=headers("vdb")).json()
    assert (body["warnAt"], body["criticalAt"], body["revision"]) == (3, 7, 1)


def test_dashboard_thresholds_write_requires_admin(viewer_client):
    r = viewer_client.put("/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 1, "criticalAt": 2})
    assert r.status_code == 403


def test_dashboard_thresholds_are_company_scoped(client, ellen_client):
    client.put("/admin/dashboard-thresholds", headers=headers("nhd"), json={"warnAt": 1, "criticalAt": 2})
    assert client.get("/admin/dashboard-thresholds", headers=headers("vdb")).json()["configured"] is False
    # Ellen has no nhd membership at all.
    assert ellen_client.get("/admin/dashboard-thresholds", headers=headers("nhd")).status_code == 403


def test_dashboard_thresholds_reject_invalid_and_stale_saves(client):
    r = client.put("/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 9, "criticalAt": 4})
    assert r.status_code == 422
    assert client.put(
        "/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 4, "criticalAt": 9}
    ).status_code == 200
    r = client.put("/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 5, "criticalAt": 9, "expectedRevision": 0})
    assert r.status_code == 409


def test_dashboard_thresholds_survive_a_backend_restart(client, isolated_dirs):
    """The TestClient app and a fresh service instance share nothing but
    the database file -- the same position a restarted process is in."""
    client.put("/admin/dashboard-thresholds", headers=headers("vdb"), json={"warnAt": 2, "criticalAt": 6})
    from jde_api_service.services.company_settings_service import CompanySettingsService

    stored = CompanySettingsService().get("vdb", "dashboard_thresholds")
    assert stored is not None
    assert stored.value == {"warn_at": 2, "critical_at": 6}
    assert stored.updated_by == "Hendro"


def test_engagement_scope_survives_a_backend_restart(client, isolated_dirs):
    client.put("/admin/engagement-scope", headers=headers("vdb"), json=_scope_payload(toolsRelease="9.2.9"))
    from jde_api_service.services.engagement_scope_service import EngagementScopeService

    fresh = EngagementScopeService(os.path.join(isolated_dirs["api_data_dir"], "engagement_scope"))
    scope = fresh.get_for_customer("vdb")
    assert scope is not None
    assert scope.tools_release == "9.2.9"
    assert scope.updated_by == "Hendro"


# ---------------------------------------------------------------------
# Atomic JSON writes
# ---------------------------------------------------------------------
def test_json_file_store_writes_atomically(tmp_path):
    from jde_api_service.persistence.json_file_store import JsonFileStore

    store = JsonFileStore(str(tmp_path))
    store.put("a", {"v": 1})
    store.put("a", {"v": 2})
    assert store.get("a") == {"v": 2}
    assert sorted(os.listdir(tmp_path)) == ["a.json"]  # no temp files left behind


def test_json_file_store_keeps_the_old_record_when_a_write_fails(tmp_path):
    from jde_api_service.persistence.json_file_store import JsonFileStore

    store = JsonFileStore(str(tmp_path))
    store.put("a", {"v": 1})

    class Unserialisable:
        def __str__(self):
            raise RuntimeError("boom")

    try:
        store.put("a", {"v": Unserialisable()})
    except RuntimeError:
        pass
    assert store.get("a") == {"v": 1}
    assert sorted(os.listdir(tmp_path)) == ["a.json"]
