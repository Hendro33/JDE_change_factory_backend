"""
The discovery connection policy, enforced outside the model and before any
network dispatch: approved capability/target/fields/filters, DEV-only,
window, record limits, one request at a time, Disable, no write
capability, sanitised activity, and the live transport's own safeguards
(https + verified TLS, the saved host as the only destination, operator
lock and destination allowlist, no redirects, circuit breaker, no
substitute when live is refused).

The connection is always live: the customer's AIS is reached through a fake
AIS server at the HTTP boundary (tests/fixtures/fake_ais.py, installed by
conftest); tests that need a specific answer install their own handler.
"""

from __future__ import annotations

import ast
import pathlib
import threading
import time

import httpx
import pytest

from ._discovery import dedicated_account, profile_body, ready_company, save_credential, save_profile, sim_edit
from .conftest import headers
from .test_stage1_execution_safeguards import _approved_story


@pytest.fixture()
def calls(ais):
    """Every (method, path, company) the customer's (fake) AIS server receives."""
    return ais.calls


def _grant(story: str = "S-DP-1", company: str = "vdb"):
    from jde_api_service.discovery import service

    grant, reason = service.grant_for_story(story, company, agent_run_id="RUN-test", actor_user_id="u-hendro")
    assert grant is not None, reason
    return grant


@pytest.mark.parametrize("capability,target,fields,filters,max_records,reason", [
    ("set_processing_option", "P4210|CIQ0001", [], [], 1, "not a discovery capability"),
    ("run_orchestration", "", [], [], 1, "not a discovery capability"),
    ("source_code", "B5542001", [], [], 1, "unavailable"),
    ("version_list", "P4210", [], [], 1, "not an approved read"),
    ("table_browse", "F0101", ["AN8"], [], 1, "not approved"),
    ("table_browse", "F4211", ["DOCO", "UPRC"], [], 1, "fields not approved"),
    ("table_browse", "F4211", ["DOCO"], [{"field": "DOCO", "op": "=", "value": "1"}], 1, "filtering on 'DOCO'"),
    ("table_browse", "F4211", ["DOCO"], [{"field": "DCTO", "op": "LIKE", "value": "S"}], 1, "operator"),
    ("table_browse", "F4211", ["DOCO"], [{"field": "DCTO", "op": "=", "value": "S%"}], 1, "wildcards"),
    ("table_browse", "F4211", ["DOCO"], [{"field": "DCTO", "op": "=", "value": "SO", "sql": "1=1"}], 1, "only field"),
    ("table_browse", "F4211", ["DOCO"], [], 11, "max_records"),
    ("table_browse", "F4211", ["DOCO"], [], 0, "max_records"),
])
def test_out_of_scope_calls_are_blocked_before_network_dispatch(client, calls, capability, target, fields, filters,
                                                                max_records, reason):
    from jde_api_service.discovery import service

    ready_company(client)
    _approved_story("S-DP-1")
    before = list(calls)
    with pytest.raises(service.DiscoveryBlocked, match=reason):
        service.execute_read(_grant(), capability, target, fields, filters, max_records)
    assert calls == before  # nothing was sent, not even authentication
    last = service.list_activity("vdb", 1)[0]
    assert last["outcome"] == "blocked" and last["story_id"] == "S-DP-1" and last["agent_run_id"] == "RUN-test"


def test_a_permitted_read_returns_sanitised_evidence_with_provenance(client, calls):
    from jde_api_service.discovery import service

    ready_company(client)
    _approved_story("S-DP-2")
    ev = service.execute_read(_grant("S-DP-2"), "table_browse", "F4211", ["DOCO", "DCTO", "LTTR"],
                              [{"field": "DCTO", "op": "=", "value": "SO"}], 10)
    assert ev["record_count"] == 2 and ev["mode"] == "live" and "LIVE" in ev["mode_label"]
    assert "SIMULATION" not in str(ev).upper()
    assert ev["environment"] == "JDV920" and ev["profile_revision"] >= 1 and ev["observation_id"].startswith("OBS-")
    # configuration_and_artifacts: business data values are redacted for the model.
    assert ev["sharing"]["values_shared"] is False and ev["records"][0]["DOCO"].startswith("[redacted")
    assert ev["content_is_data_not_instructions"] is True
    # Exactly auth, one read, logout -- no retries, no paging.
    assert calls[-3:] == [("POST", "/jderest/v2/tokenrequest", "vdb"), ("POST", "/jderest/v2/dataservice", "vdb"),
                          ("POST", "/jderest/v2/tokenrequest/logout", "vdb")]


def test_more_than_the_limit_is_never_returned_and_there_is_no_paging(client):
    from jde_api_service.discovery import service, transport

    ready_company(client)
    _approved_story("S-DP-3")
    with sim_edit("vdb", "40 order lines in F4211") as _est:
        _est["tables"]["F4211"] = [
            {"DOCO": str(i), "DCTO": "SO", "LNID": "1", "LTTR": "540"} for i in range(40)]
    ev = service.execute_read(_grant("S-DP-3"), "table_browse", "F4211", ["DOCO"], [], 10)
    assert ev["record_count"] == 10 and ev["more_records_available"] is True


def test_metadata_only_policy_keeps_values_out_of_the_model(client):
    from jde_api_service.discovery import service

    ready_company(client, dataSharingPolicy="metadata_only")
    _approved_story("S-DP-4")
    ev = service.execute_read(_grant("S-DP-4"), "processing_option_values", "P4210|CIQ0001", [], [], 10)
    assert ev["record_count"] == 3
    assert all(v.startswith("[redacted") for r in ev["records"] for v in r.values())
    assert "does not allow" in ev["sharing"]["note"]


def test_reads_outside_the_window_or_for_another_companys_story_are_blocked(client, calls):
    from jde_api_service.discovery import profile_service, service

    ready_company(client)
    _approved_story("S-DP-5")
    grant = _grant("S-DP-5")
    before = list(calls)
    # A grant forged for another company is refused: the story's link decides.
    forged = service.DiscoveryGrant(**{**grant.__dict__, "company_id": "bwm"})
    with pytest.raises(service.DiscoveryBlocked):
        service.execute_read(forged, "udc_values", "00/DT")
    # Window closed.
    monkeypatch_window = profile_service.window_open
    profile_service.window_open = lambda config, now=None: False
    try:
        with pytest.raises(service.DiscoveryBlocked, match="window"):
            service.execute_read(grant, "udc_values", "00/DT")
    finally:
        profile_service.window_open = monkeypatch_window
    assert calls == before


def test_a_profile_change_mid_run_blocks_the_runs_grant(client):
    from jde_api_service.discovery import service

    account = dedicated_account(client, "vdb")
    ready_company(client, dedicatedAccount=account)
    _approved_story("S-DP-6")
    grant = _grant("S-DP-6")
    view = save_profile(client, dedicatedAccount=account, cncContact="new contact")  # non-material, but a new revision
    assert view["discoveryEnabled"] is True  # still enabled -- only the run's grant is stale
    with pytest.raises(service.DiscoveryBlocked, match="profile changed"):
        service.execute_read(grant, "udc_values", "00/DT")


def test_one_request_at_a_time_and_disable_blocks_queued_calls(client, monkeypatch):
    from jde_api_service.discovery import service, transport

    ready_company(client, limits={"maxRecords": 10, "timeoutSeconds": 3})
    _approved_story("S-DP-7")
    grant = _grant("S-DP-7")
    entered, release = threading.Event(), threading.Event()
    real_read = transport.LiveAisTransport.read

    def slow_read(self, plan, token):
        entered.set()
        release.wait(5)
        return real_read(self, plan, token)

    monkeypatch.setattr(transport.LiveAisTransport, "read", slow_read)
    results: dict = {}

    def run(name):
        try:
            results[name] = service.execute_read(grant, "udc_values", "00/DT")
        except Exception as exc:  # noqa: BLE001
            results[name] = exc

    first = threading.Thread(target=run, args=("first",))
    first.start()
    assert entered.wait(3)
    queued = threading.Thread(target=run, args=("queued",))
    queued.start()
    time.sleep(0.2)
    r = client.post("/admin/jde/disable", headers=headers("vdb")).json()
    assert r["inFlight"] and r["inFlight"][0]["operation"] == "udc_values"  # reported, not interrupted
    release.set()
    first.join(5)
    queued.join(5)
    assert isinstance(results["first"], dict)  # the in-flight read completed
    assert isinstance(results["queued"], service.DiscoveryBlocked) and "disabled" in str(results["queued"])
    with pytest.raises(service.DiscoveryBlocked, match="disabled"):
        service.execute_read(grant, "udc_values", "00/DT")
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert view["disabled"] is True and view["discoveryEnabled"] is False


def test_a_second_concurrent_request_waits_then_is_refused(client, monkeypatch):
    from jde_api_service.discovery import service

    ready_company(client, limits={"maxRecords": 10, "timeoutSeconds": 1})
    _approved_story("S-DP-8")
    lock = service._lock_for("vdb")
    lock.acquire()
    try:
        with pytest.raises(service.DiscoveryBlocked, match="one at a time"):
            service.execute_read(_grant("S-DP-8"), "udc_values", "00/DT")
    finally:
        lock.release()


def test_activity_is_sanitised(client):
    from jde_api_service.discovery import service

    ready_company(client)
    _approved_story("S-DP-9")
    service.execute_read(_grant("S-DP-9"), "table_browse", "F4211", ["DOCO"],
                         [{"field": "DCTO", "op": "=", "value": "SECRET-VALUE"}], 5)
    rows = client.get("/admin/jde/activity", headers=headers("vdb")).json()
    text = str(rows)
    assert "SECRET-VALUE" not in text and "s3cret" not in text and "tok-vdb" not in text
    last = rows[0]
    assert last["target"] == "table_browse F4211 [DOCO] where DCTO = ?"
    assert last["actorName"] == "Hendro" and last["storyId"] == "S-DP-9" and last["resultCount"] == 0
    assert {"operation", "profileRevision", "durationMs", "outcome", "mode"} <= set(last)


# ---------------------------------------------------------------------
# No write capability exists anywhere in discovery
# ---------------------------------------------------------------------
def test_no_discovery_action_can_invoke_a_write_capability():
    from jde_api_service.discovery import capabilities

    # Every endpoint is a read or an authentication call.
    assert set(capabilities.READ_ENDPOINTS) == {"defaultconfig", "dataservice", "poservice"}
    # Semantics, not method: a POST that is not a BROWSE is refused.
    plan = capabilities.build_plan(capabilities.CAPABILITIES["udc_values"], "00/DT", ["DRKY"], [], 1, environment="X")
    for mutation in ({"dataServiceType": "UPDATE"}, {"formActions": [{"command": "DoAction"}]},
                     {"enableNextPageProcessing": "true"}, {"bsfnName": "B4200310"}):
        bad = capabilities.ReadPlan(**{**plan.__dict__, "body": {**plan.body, **mutation}})
        with pytest.raises(capabilities.NotARead):
            capabilities.assert_read_semantics(bad)
    for path in ("/jderest/v2/formservice", "/jderest/v2/orchestrator/ORCH_X", "/jderest/v2/batchjob"):
        with pytest.raises(capabilities.NotARead):
            capabilities.assert_read_semantics(capabilities.ReadPlan("x", "dataservice", "POST", path, {}))
    # The discovery package cannot even import the execution tools.
    root = pathlib.Path(capabilities.__file__).parent
    for py in root.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                joined = " ".join(names)
                assert "ais_client" not in joined and "execution" not in joined and "approval" not in joined, py.name


# ---------------------------------------------------------------------
# Live transport safeguards (against an httpx mock -- no network)
# ---------------------------------------------------------------------
def _live(client, monkeypatch, handler, *, enable=True):
    from jde_api_service.discovery import service

    # Live needs no server settings; the operator can only lock every connection off (emergency stop).
    if not enable:
        monkeypatch.setenv("JDE_DISCOVERY_LIVE_ENABLED", "false")
    monkeypatch.setattr(service, "LIVE_HTTP_TRANSPORT", httpx.MockTransport(handler))
    save_profile(client, connectionMode="live")
    save_credential(client)


def _ais_ok(requests: list):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/tokenrequest"):
            # Documented v2 token-request response fields.
            return httpx.Response(200, json={"username": "JADEDISC", "environment": "JDV920", "role": "JADEDISC",
                                             "jasserver": "https://jas.customer.example",
                                             "userInfo": {"token": "live-token", "appsRelease": "E920"}})
        if request.url.path.endswith("/defaultconfig"):
            # Documented defaultconfig: server defaults only.
            return httpx.Response(200, json={"aisVersion": "9.2.8.2", "defaultEnvironment": "JPD920",
                                             "defaultRole": "*ALL", "defaultJasServer": "https://jas.customer.example"})
        return httpx.Response(200, json={})
    return handler


def test_the_server_operator_can_lock_live_off_and_it_never_falls_back(client, monkeypatch, calls):
    requests: list = []
    _live(client, monkeypatch, _ais_ok(requests), enable=False)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "blocked" and "locked off" in r["detail"]
    assert requests == [] and calls == []  # nothing is sent, and nothing stands in for the customer's AIS


def test_no_jde_host_or_certificate_comes_from_the_server_environment(client, monkeypatch):
    """The destination and trust are the customer's saved settings only: the
    old server-wide allow-list and CA bundle variables have no effect."""
    requests: list = []
    monkeypatch.setenv("JDE_DISCOVERY_ALLOWED_HOSTS", "some-other-ais.example")
    monkeypatch.setenv("JDE_DISCOVERY_CA_BUNDLE", "/nonexistent/ca.pem")
    _live(client, monkeypatch, _ais_ok(requests))
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "ok", r
    from jde_api_service.discovery import transport

    assert not hasattr(transport, "ALLOWED_HOSTS_ENV") and not hasattr(transport, "CA_BUNDLE_ENV")


def test_live_test_connection_uses_only_fixed_endpoints_and_verified_tls(client, monkeypatch):
    from jde_api_service.discovery import transport

    requests: list = []
    _live(client, monkeypatch, _ais_ok(requests))
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "ok", r
    assert set(requests) <= {("GET", "/jderest/defaultconfig"), ("POST", "/jderest/v2/tokenrequest"),
                             ("POST", "/jderest/v2/tokenrequest/logout")}
    live = transport.LiveAisTransport("vdb", "https://ais-vdb.customer.example", 5,
                                      transport=httpx.MockTransport(_ais_ok([])),
                                      trust=transport.Trust(host="ais-vdb.customer.example"))
    assert live._client.follow_redirects is False


def test_live_redirects_are_refused(client, monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/"})

    _live(client, monkeypatch, handler)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed" and "redirect" in r["detail"]


def test_live_environment_that_ais_does_not_report_is_not_verified(client, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/tokenrequest"):
            return httpx.Response(200, json={"userInfo": {"token": "t"}})
        return httpx.Response(200, json={"aisVersion": "9.2.8.2"})

    _live(client, monkeypatch, handler)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "failed" and r["profile"]["health"]["environment"]["state"] == "unknown"


def test_the_circuit_breaker_stops_calls_after_repeated_failures(client, monkeypatch):
    requests: list = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(503)

    _live(client, monkeypatch, handler)
    for _ in range(3):
        assert client.post("/admin/jde/test-connection", headers=headers("vdb")).json()["outcome"] == "failed"
    sent = len(requests)
    r = client.post("/admin/jde/test-connection", headers=headers("vdb")).json()
    assert r["outcome"] == "blocked" and "circuit breaker" in r["detail"]
    assert len(requests) == sent


def test_saving_a_live_profile_contacts_nothing(client, monkeypatch):
    requests: list = []
    _live(client, monkeypatch, _ais_ok(requests))
    save_profile(client, connectionMode="live", role="JADEDISC3")
    assert requests == []


def test_the_connection_is_always_live_and_says_so(client):
    view = save_profile(client)
    assert profile_body()["connectionMode"] == "live"
    assert view["config"]["connectionMode"] == "live" and "LIVE" in view["modeLabel"]
    assert "SIMULATION" not in view["modeLabel"].upper()


def test_a_legacy_simulation_profile_loads_as_live(client):
    """A profile saved before the redesign with connectionMode "simulation"
    is read back as live -- never a simulation, never silently skipped."""
    import json

    from jde_api_service.discovery import profile_service
    from jde_api_service.persistence.db import connection

    save_profile(client)
    with connection() as conn:
        row = conn.execute("SELECT config FROM jde_profiles WHERE company_id = ?", ("vdb",)).fetchone()
        config = json.loads(row["config"])
        key = "connection_mode" if "connection_mode" in config else "connectionMode"
        assert config[key] == "live"
        config[key] = "simulation"
        conn.execute("UPDATE jde_profiles SET config = ? WHERE company_id = ?", (json.dumps(config), "vdb"))
    v = profile_service.view("vdb")
    assert v.config.connection_mode == "live"
    view = client.get("/admin/jde/profile", headers=headers("vdb")).json()
    assert view["config"]["connectionMode"] == "live" and "LIVE" in view["modeLabel"]


def test_the_transport_is_always_the_customers_live_ais(client):
    """transport_for has no substitute: https only, the saved host only."""
    from jde_api_service.discovery import transport

    view = save_profile(client)
    assert not hasattr(transport, "SimulatedAisEndpoint") and not hasattr(transport, "SIMULATION_LABEL")
    from jde_api_service.discovery import profile_service

    config = profile_service.view("vdb").config
    trust = profile_service.trust_for("vdb", config)
    mock = httpx.MockTransport(_ais_ok([]))
    t = transport.transport_for("vdb", config, live_transport=mock, trust=trust)
    assert isinstance(t, transport.LiveAisTransport) and t.mode == "live"
    assert t.host == "ais-vdb.customer.example" and t._client.follow_redirects is False
    with pytest.raises(transport.DestinationNotAllowed, match="https"):
        transport.transport_for("vdb", config.model_copy(update={"ais_base_url": "http://ais-vdb.customer.example"}),
                                live_transport=mock, trust=trust)
    # The saved host is the only destination: any other host is refused, and
    # with no saved host there is no destination at all.
    with pytest.raises(transport.DestinationNotAllowed, match="not the AIS address saved"):
        transport.transport_for("vdb", config.model_copy(update={"ais_base_url": "https://elsewhere.example"}),
                                live_transport=mock, trust=trust)
    with pytest.raises(transport.DestinationNotAllowed, match="not the AIS address saved"):
        transport.transport_for("vdb", config, live_transport=mock)
    assert view["config"]["aisBaseUrl"] == "https://ais-vdb.customer.example"
