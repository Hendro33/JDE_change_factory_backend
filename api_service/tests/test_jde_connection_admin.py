"""Admin > Integrations > JDE: the settings needed for a first real read-only
connection, and the boundaries around it. Everything runs against the
simulated endpoint or an httpx mock -- no network."""

from __future__ import annotations

import logging

import httpx
import pytest

from ._discovery import profile_body, ready_company, save_credential, save_profile
from .conftest import TEST_PASSWORD, _apply_csrf_header, headers
from .test_discovery_policy import _ais_ok, _live

SECRET = "Sentinel-Pw-9f3b1c7e"


def _second_admin():
    """Another authorised browser: a different admin of the same company."""
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user("admin2@test.local", TEST_PASSWORD, "Second Admin", user_id="u-admin2")
    membership_service.create_membership("u-admin2", "vdb", ["admin"], created_by="u-hendro")
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": "admin2@test.local", "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(c)
    return c


def test_every_setting_persists_across_restart_and_another_browser_without_the_secret(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    save_profile(client, connectionName="BicycleWorks PS920 trial", environment="PS920", environmentPurpose="isolated_trial",
                 trialApprovalReference="Customer e-mail 2026-09-24: PS920 approved for the read-only trial",
                 aisBaseUrl="https://ais.customer.example:9302/proxy/jderest/")
    save_credential(client, password=SECRET)
    with TestClient(app):  # restart
        pass
    other = _second_admin()
    view = other.get("/admin/jde/profile", headers=headers("vdb")).json()
    cfg = view["config"]
    assert (cfg["connectionName"], cfg["environment"], cfg["environmentPurpose"]) == (
        "BicycleWorks PS920 trial", "PS920", "isolated_trial")
    assert cfg["aisBaseUrl"] == "https://ais.customer.example:9302/proxy"  # /jderest normalised, never doubled
    assert view["requestUrls"]["token_request"] == "https://ais.customer.example:9302/proxy/jderest/v2/tokenrequest"
    assert view["credentialConfigured"] and view["credentialStorage"] == "encrypted"
    assert SECRET not in other.get("/admin/jde/profile", headers=headers("vdb")).text


def test_cross_company_access_is_rejected(client, ellen_client):
    save_profile(client, "nhd")
    assert ellen_client.get("/admin/jde/profile", headers=headers("nhd")).status_code == 403
    assert ellen_client.put("/admin/jde/profile", headers=headers("nhd"), json=profile_body("nhd")).status_code == 403
    assert ellen_client.post("/admin/jde/test-connection", headers=headers("nhd")).status_code == 403


def test_environment_purpose_is_stated_not_inferred_from_the_name(client):
    body = profile_body(environment="PS920", environmentPurpose="isolated_trial")
    r = client.put("/admin/jde/profile", headers=headers("vdb"), json=body)
    assert r.status_code == 422 and "approval" in r.text  # an isolated trial needs its approval reference
    assert client.put("/admin/jde/profile", headers=headers("vdb"),
                      json={**body, "environmentType": "PROD"}).status_code == 422
    r = client.put("/admin/jde/profile", headers=headers("vdb"),
                   json={**body, "trialApprovalReference": "CAB-2026-114 approved the PS920 read-only trial"})
    assert r.status_code == 200, r.text
    assert client.put("/admin/jde/profile", headers=headers("vdb"),
                      json={**profile_body(), "aisBaseUrl": "https://ais.customer.example:99999", "expectedRevision": 1}
                      ).status_code == 422


def test_only_material_edits_invalidate_verification_and_discovery(client):
    ready_company(client)
    before = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert before["discoveryEnabled"] and before["health"]["authentication"]["state"] == "ok"
    save_profile(client, connectionName="renamed only")  # not material
    after = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert after["discoveryEnabled"] and after["health"]["authentication"]["state"] == "ok"
    save_profile(client, connectionName="renamed only", role="JADEREAD")  # material
    after = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert not after["discoveryEnabled"]
    assert {after["health"][c]["state"] for c in ("reachability", "authentication", "environment")} == {"stale"}
    blocked = [p for p in after["prerequisites"] if p["required"] and not p["satisfied"]]
    assert {p["kind"] for p in blocked} == {"machine_verified"}


def test_saving_never_connects_even_in_live_mode(client, monkeypatch):
    requests = []
    _live(client, monkeypatch, _ais_ok(requests))  # saves a live profile and a credential
    save_profile(client, connectionMode="live", cncContact="New CNC")
    assert requests == []


def test_live_and_simulation_never_switch_or_mix(client, monkeypatch):
    from jde_api_service.discovery import service

    ready_company(client)  # simulation, enabled
    grant, _ = service.admin_grant("vdb", "u-hendro", "t"), None
    monkeypatch.delenv("JDE_DISCOVERY_LIVE_ENABLED", raising=False)
    save_profile(client, connectionMode="live")
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert not view["discoveryEnabled"] and view["modeLabel"].startswith("LIVE")
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "blocked" and "switched off for this deployment" in r["detail"]  # no fallback to simulation
    with pytest.raises(service.DiscoveryBlocked):  # a grant from the simulation revision cannot read live
        service.execute_read(grant, "udc_values", "00/DT", [], [], 1)
    server = {p["id"]: p for p in view["serverPrerequisites"]}
    assert not server["live_enabled"]["satisfied"] and not server["allowlist"]["satisfied"]


def test_forbidden_reads_are_blocked_before_any_request(client, monkeypatch):
    requests = []
    _live(client, monkeypatch, _ais_ok(requests))
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "ok", r
    sent = len(requests)
    for body in ({"capabilityId": "object_librarian", "target": "P0101"},  # not an approved target
                 {"capabilityId": "object_librarian", "target": ""},  # none selected
                 {"capabilityId": "table_browse", "target": "F4211", "fields": ["DOCO", "AN8"]},  # field not approved
                 {"capabilityId": "table_browse", "target": "F4211", "filters": [{"field": "AN8", "op": "=", "value": "1"}]},
                 {"capabilityId": "set_processing_option", "target": "P4210|CIQ0001"}):
        r = client.post("/admin/jde/sample-read", headers=headers("vdb"), json=body).json()
        assert r["outcome"] == "blocked", (body, r)
    assert len(requests) == sent  # nothing left the backend


def test_tls_and_network_failures_are_explained_and_never_bypassed(client, monkeypatch):
    def tls_fail(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer")

    _live(client, monkeypatch, tls_fail)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed" and "JDE_DISCOVERY_CA_BUNDLE" in r["detail"] and "never switched off" in r["detail"]

    def unreachable(request):
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    from jde_api_service.discovery import service, transport

    transport.reset_breaker("vdb")
    monkeypatch.setattr(service, "LIVE_HTTP_TRANSPORT", httpx.MockTransport(unreachable))
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert "VPN" in r["detail"] and "backend" in r["detail"]
    monkeypatch.setenv("JDE_DISCOVERY_CA_BUNDLE", "/nonexistent/ca.pem")
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert not {p["id"]: p for p in view["serverPrerequisites"]}["tls_trust"]["satisfied"]


def test_credentials_never_appear_in_responses_logs_or_exports(client, caplog, tmp_path):
    from jde_api_service.services import backup_restore

    caplog.set_level(logging.DEBUG)
    save_profile(client)
    save_credential(client, password=SECRET)
    bodies = [client.post("/admin/jde/test-connection", headers=headers("vdb")).text,
              client.get("/admin/jde/profile", headers=headers("vdb")).text,
              client.get("/admin/jde/activity", headers=headers("vdb")).text,
              client.post("/admin/jde/sample-read", headers=headers("vdb"),
                          json={"capabilityId": "udc_values", "target": "00/DT"}).text]
    assert all(SECRET not in b for b in bodies)
    assert SECRET not in caplog.text
    archive = tmp_path / "backup.tar.gz"
    backup_restore.create_backup(str(archive), by="test", settle_seconds=0)
    import tarfile

    with tarfile.open(archive) as tar:
        for m in tar.getmembers():
            if m.isfile():
                assert SECRET.encode() not in tar.extractfile(m).read(), m.name


def test_process_citations_are_validated_against_the_context_given_to_the_run():
    from jde_api_service.discovery.baseline import RunLedger, _validate_citations

    ledger = RunLedger(None, "none")
    ledger.process_context_consulted = {
        "mapping": {"revision": 2, "processes": [{"ref": "PF-1@v1:SYN-5.1.3"}]},
        "maps": {"to_be": {"version": 3, "steps": [{"id": "T2"}]}}}
    raw = [
        {"claim": "authorisation happens at receipt", "basis": "process_reference", "evidence_ids": ["PROC:PF-1@v1:SYN-5.1.3", "MAP:to_be@v3:T2"]},
        {"claim": "the mapping was confirmed", "basis": "process_reference", "evidence_ids": ["MAPPING@r2"]},
        {"claim": "made-up process", "basis": "process_reference", "evidence_ids": ["PROC:PF-1@v1:SYN-9.9"]},
        {"claim": "an older map", "basis": "process_reference", "evidence_ids": ["MAP:to_be@v2"]},
        {"claim": "process as JDE fact", "basis": "observed", "evidence_ids": ["PROC:PF-1@v1:SYN-5.1.3"]},
    ]
    citations, gaps = _validate_citations(raw, ledger, None)
    assert [c["validated"] for c in citations] == [True, True, False, False, False]
    assert [c["basis"] for c in citations] == ["process_reference", "process_reference", "assumption", "assumption", "assumption"]
    assert "not an observation" in citations[4]["note"]
    assert len(gaps) == 3
    # No process context given -> no process reference is acceptable.
    assert not _validate_citations(raw[:1], RunLedger(None, "none"), None)[0][0]["validated"]


def test_substantively_same_findings_are_not_proposed_again(client):
    from jde_api_service.process import refinement

    assert refinement.substantively_same(
        "An approver other than the person who recorded the write-off must approve write-offs above the threshold.",
        "Write-offs above the threshold must be approved by an approver other than the person who recorded the write-off")
    assert not refinement.substantively_same("Audit trail of approvals", "Finance can see the value adjustment")
