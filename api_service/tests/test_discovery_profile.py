"""
The company JDE discovery profile (Admin > Integrations > JDE): versioned
persistence, credential protection, company isolation, revisions, and the
rule that saving never contacts JDE while a material change switches
discovery off until it is re-verified.
"""

from __future__ import annotations

import sqlite3

import pytest

from ._discovery import profile_body, ready_company, save_credential, save_profile, sim_edit
from .conftest import headers

PASSWORD = "s3cret-Discovery-pw"


def _db_rows(sql: str, *args):
    from jde_api_service.persistence.db import db_path

    conn = sqlite3.connect(db_path())
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_the_profile_is_versioned_in_the_database_and_saving_never_contacts_jde(client, monkeypatch):
    from jde_api_service.discovery import transport

    def never(*a, **k):
        raise AssertionError("saving a profile must not contact JDE")

    for name in ("check_reachability", "authenticate", "read"):
        monkeypatch.setattr(transport.SimulatedAisEndpoint, name, never)
    first = save_profile(client)
    second = save_profile(client, role="JADEDISC2")
    assert (first["revision"], second["revision"]) == (1, 2)
    revisions = _db_rows("SELECT revision, saved_by FROM jde_profile_revisions WHERE company_id = 'vdb' ORDER BY revision")
    assert [r[0] for r in revisions] == [1, 2] and revisions[0][1] == "Hendro"
    assert all(v["state"] == "unknown" for v in second["health"].values())


def test_stale_or_missing_revisions_are_refused(client):
    save_profile(client)
    r = client.put("/admin/jde/profile", headers=headers("vdb"), json=profile_body())
    assert r.status_code == 428
    r = client.put("/admin/jde/profile", headers=headers("vdb"), json={**profile_body(), "expectedRevision": 7})
    assert r.status_code == 409 and r.json()["currentRevision"] == 1


def test_the_credential_is_encrypted_and_never_returned(client):
    save_profile(client)
    view = save_credential(client, password=PASSWORD)
    assert view["credentialConfigured"] is True and view["credentialStorage"] == "encrypted"
    assert view["credentialUsernameMasked"].startswith("JA") and "JADEDISC" not in view["credentialUsernameMasked"]
    stored = _db_rows("SELECT credential_secret FROM jde_profiles WHERE company_id = 'vdb'")[0][0]
    assert stored.startswith("enc:v1:") and PASSWORD not in stored
    from jde_api_service.persistence.db import db_path

    assert PASSWORD.encode() not in open(db_path(), "rb").read()
    for path in ("/admin/jde/profile",):
        assert PASSWORD not in client.get(path, headers=headers("vdb")).text


def test_no_key_means_no_credential_and_a_lost_key_blocks_discovery(client, monkeypatch):
    from cryptography.fernet import Fernet

    save_profile(client)
    monkeypatch.delenv("JDE_CREDENTIAL_KEY")
    rev = client.get("/admin/jde/profile", headers=headers("vdb")).json()["revision"]
    r = client.put("/admin/jde/credential", headers=headers("vdb"),
                   json={"username": "JADEDISC", "password": PASSWORD, "expectedRevision": rev})
    assert r.status_code == 409
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    save_credential(client)
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())  # key lost
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert view["credentialStorage"] == "unreadable"
    r = client.post("/admin/jde/test-connection", headers=headers("vdb"))
    assert r.json()["outcome"] == "failed"
    assert r.json()["profile"]["health"]["authentication"]["state"] == "failed"


def test_a_material_change_switches_discovery_off_until_rechecked(client):
    view = ready_company(client)
    assert view["discoveryEnabled"] is True
    # Contact names are not material.
    view = save_profile(client, cncContact="Another CNC person")
    assert view["discoveryEnabled"] is True
    # The role is.
    view = save_profile(client, role="JADEDISC2")
    assert view["discoveryEnabled"] is False
    assert {v["state"] for v in view["health"].values()} == {"stale"}
    assert any("stale" in b for b in view["enableBlockers"])
    assert all(c["status"] != "supported" for c in view["capabilities"])


def test_a_new_credential_is_a_material_change(client):
    ready_company(client)
    view = save_credential(client, password="another-password")
    assert view["discoveryEnabled"] is False


def test_the_profile_is_company_scoped_and_admin_only(client, viewer_client):
    save_profile(client, "vdb")
    assert client.get("/admin/jde/profile", headers=headers("bwm")).json()["configured"] is False
    assert viewer_client.put("/admin/jde/profile", headers=headers("vdb"),
                             json={**profile_body(), "expectedRevision": 1}).status_code == 403
    assert viewer_client.post("/admin/jde/test-connection", headers=headers("vdb")).status_code == 403
    assert viewer_client.get("/admin/jde/activity", headers=headers("vdb")).status_code == 403


@pytest.mark.parametrize("override,message", [
    ({"aisBaseUrl": "http://ais.customer.example"}, "https"),
    ({"aisBaseUrl": "https://user:pw@ais.customer.example"}, "credentials"),
    ({"environment": "*ALL"}, "explicitly"),
    ({"role": "*ALL"}, "explicitly"),
    ({"environmentType": "PROD"}, ""),
    ({"approvedReads": [{"capabilityId": "set_processing_option", "targets": ["P4210|CIQ0001"]}]}, "unknown discovery capability"),
    ({"approvedReads": [{"capabilityId": "source_code", "targets": ["B5542001"]}]}, "unavailable"),
    ({"approvedReads": [{"capabilityId": "table_browse", "targets": ["F4211; DROP TABLE"], "fields": ["DOCO"]}]}, "exact"),
    ({"approvedReads": [{"capabilityId": "table_browse", "targets": ["F4211"], "fields": []}]}, "approved columns"),
    ({"limits": {"maxRecords": 50}}, ""),
])
def test_the_profile_refuses_unsafe_settings(client, override, message):
    r = client.put("/admin/jde/profile", headers=headers("vdb"), json=profile_body(**override))
    assert r.status_code == 422, r.text
    assert message in r.text


def test_enabling_needs_every_check_and_the_attestations(client):
    save_profile(client, routingIsolationConfirmed=False)
    save_credential(client)
    rev = client.get("/admin/jde/profile", headers=headers("vdb")).json()["revision"]
    r = client.post("/admin/jde/enable", headers=headers("vdb"), json={"expectedRevision": rev})
    assert r.status_code == 409
    assert "route and isolation" in r.json()["detail"] and "reachability check is unknown" in r.json()["detail"]
    # Test Connection is also refused before the customer's confirmations exist.
    r = client.post("/admin/jde/test-connection", headers=headers("vdb"))
    assert r.json()["outcome"] == "blocked" and "routing and isolation" in r.json()["detail"]


def test_environment_verification_fails_on_a_mismatch(client):
    from jde_api_service.discovery import transport

    save_profile(client)
    save_credential(client)
    with sim_edit("vdb", "set session.apps_release") as _est:
        _est["session"]["apps_release"] = "E910"
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed" and "application release" in r["detail"] and "E910" in r["detail"]
    h = r["profile"]["health"]
    assert (h["reachability"]["state"], h["authentication"]["state"], h["environment"]["state"]) == ("ok", "ok", "failed")
    assert h["approved_read"]["state"] == "unknown"


def test_capability_status_is_explicit(client):
    view = ready_company(client)
    status = {c["capabilityId"]: c["status"] for c in view["capabilities"]}
    assert status["source_code"] == status["event_rules"] == status["object_specifications"] == "unavailable"
    assert status["udc_values"] == status["processing_option_values"] == "supported"
    assert status["version_list"] == "unverified"  # not approved, never sample-read


# ---------------------------------------------------------------------
# Environment verification against the documented AIS contract
# ---------------------------------------------------------------------
def _env_check(client) -> dict:
    return client.get("/admin/jde/profile", headers=headers("vdb")).json()["health"]["environment"]


def test_server_defaults_alone_never_verify_the_environment(client):
    """defaultconfig reports the server's DEFAULT environment. Even when it
    equals the expected one, it is not evidence of the session: if the
    token response does not state the session context, the check stays
    unverified and discovery cannot be enabled."""
    from jde_api_service.discovery import transport

    save_profile(client)
    save_credential(client)
    with sim_edit("vdb", "set defaultconfig.defaultEnvironment") as _est:
        _est["defaultconfig"]["defaultEnvironment"] = "JDV920"  # matches the profile
    with sim_edit("vdb", "set session.report_context") as _est:
        _est["session"]["report_context"] = False
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed"
    env = _env_check(client)
    assert env["state"] == "unknown"
    missing = " ".join(env["facets"]["missing_evidence"])
    assert "session environment" in missing and "session role" in missing and "application release" in missing
    assert env["facets"]["server_defaults"]["used_as_evidence"] is False
    rev = client.get("/admin/jde/profile", headers=headers("vdb")).json()["revision"]
    assert client.post("/admin/jde/enable", headers=headers("vdb"), json={"expectedRevision": rev}).status_code == 409


def test_the_session_response_is_the_evidence_not_the_server_default(client):
    from jde_api_service.discovery import transport

    save_profile(client)
    save_credential(client)
    with sim_edit("vdb", "set defaultconfig.defaultEnvironment") as _est:
        _est["defaultconfig"]["defaultEnvironment"] = "JPD920"  # server default: PROD
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "ok", r
    env = _env_check(client)
    items = {i["item"]: i for i in env["facets"]["items"]}
    assert items["session environment"]["status"] == "verified"
    assert items["session environment"]["source"] == "AIS token response"
    assert items["application release"]["status"] == "verified"
    assert {items["Tools / server release"]["status"], items["path code"]["status"],
            items["OCM data-source routing and isolation"]["status"]} == {"attested"}
    assert any("JPD920" in n for n in env["facets"]["notes"])


def test_a_session_granted_a_different_environment_is_a_mismatch(client):
    from jde_api_service.discovery import transport

    save_profile(client)
    save_credential(client)
    real = transport.SimulatedAisEndpoint.authenticate

    def falls_back(self, username, password, environment, role):
        s = real(self, username, password, environment, role)
        s.context["environment"] = "JPD920"  # AIS put the session somewhere else
        return s

    transport.SimulatedAisEndpoint.authenticate = falls_back
    try:
        r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    finally:
        transport.SimulatedAisEndpoint.authenticate = real
    assert r["outcome"] == "failed" and "session reports 'JPD920'" in r["detail"]


def test_without_a_cnc_attestation_tools_release_and_path_code_stay_unverified(client):
    save_profile(client, runtimeAttestationConfirmed=False, runtimeAttestationEvidence="")
    save_credential(client)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed"
    env = _env_check(client)
    assert env["state"] == "unknown"
    missing = " ".join(env["facets"]["missing_evidence"])
    assert "Tools / server release" in missing and "path code" in missing
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert any("attested the Tools release and path code" in b for b in view["enableBlockers"])
