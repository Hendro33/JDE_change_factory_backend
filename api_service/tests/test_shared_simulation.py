"""
One authoritative simulated DEV estate (jde_mcp_server/sim_estate.py).

Discovery and simulated execution read and change the SAME state, scoped by
company, environment and target. Drift, failures and timeouts exist only as
explicit, recorded test conditions. Fixtures are isolated per test.
"""

from __future__ import annotations

import httpx
import pytest

from ._discovery import ready_company, sim_edit
from .conftest import headers
from .test_stage1_execution_safeguards import _approve, _approved_story, _execute, _full_scope, _propose, _save_scope


def _discovered_value(story: str, option: str = "PDOCTYPE") -> str:
    from jde_api_service.discovery import service

    grant, reason = service.grant_for_story(story, "vdb", agent_run_id="RUN-sim", actor_user_id="u-hendro")
    assert grant is not None, reason
    ev = service.execute_read(grant, "processing_option_values", "P4210|CIQ0001", [], [], 10)
    assert ev["mode"] == "simulation" and ev["evidence_type"] == "simulated_observation"
    return {r["option"]: r["value"] for r in ev["records"]}[option]


def _ready(client, story: str) -> dict:
    ready_company(client)
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story)
    change = _propose(story)
    _approve(change["change_id"])
    return change


def test_discovery_and_execution_read_the_same_value_and_an_applied_change_is_discovered(client):
    from jde_mcp_server.ais_client import client as ais

    change = _ready(client, "S-SIM-1")
    # The execution side's before-state is the value discovery reports -- no MOCK-INITIAL.
    assert ais.read_processing_option_value("vdb", "P4210", "CIQ0001", "PDOCTYPE") == "S3"
    assert _discovered_value("S-SIM-1") == "S3"
    result = _execute("S-SIM-1", change["change_id"])
    assert result["request"]["previous_value"] == "S3"
    assert "SIMULATION" in result["label"]
    # A later discovery read observes the applied simulated change.
    assert _discovered_value("S-SIM-1") == "SO"


def test_every_estate_change_is_recorded_with_who_and_why(client):
    from jde_mcp_server import sim_estate

    change = _ready(client, "S-SIM-HIST")
    _execute("S-SIM-HIST", change["change_id"])
    with sim_edit("vdb", "drift: someone changes PLNTY in DEV") as est:
        est["processing_options"]["P4210|CIQ0001"]["PLNTY"] = "X"
    history = sim_estate.load("vdb", "JDV920")["history"]
    assert any(h["actor"] == "simulated execution" and change["change_id"] in h["reason"] for h in history)
    assert history[-1]["actor"] == "test" and history[-1]["reason"].startswith("TEST CONDITION: drift")
    assert sim_estate.load("vdb", "JDV920")["label"].startswith("SIMULATION")


def test_estates_are_isolated_by_company_and_environment(client):
    from jde_mcp_server import sim_estate

    with sim_estate.edit("vdb", "JDV920", actor="test", reason="TEST CONDITION: vdb drift") as est:
        est["processing_options"]["P4210|CIQ0001"]["PDOCTYPE"] = "ZZ"
    assert sim_estate.read_processing_option("vdb", "JDV920", "P4210", "CIQ0001", "PDOCTYPE") == "ZZ"
    assert sim_estate.read_processing_option("bwm", "JDV920", "P4210", "CIQ0001", "PDOCTYPE") == "S3"
    assert sim_estate.read_processing_option("vdb", "JPY920", "P4210", "CIQ0001", "PDOCTYPE") == "S3"
    with pytest.raises(sim_estate.SimEstateError):
        sim_estate.load("../etc", "JDV920")


def test_a_fresh_test_starts_from_the_template(client):
    """The previous test's drift is not visible here: each test has its own estate directory."""
    from jde_mcp_server import sim_estate

    assert sim_estate.read_processing_option("vdb", "JDV920", "P4210", "CIQ0001", "PDOCTYPE") == "S3"


def test_an_unknown_target_is_refused_rather_than_invented(client):
    from jde_mcp_server.ais_client import SimTargetMissing, client as ais

    _save_scope(client, "vdb", _full_scope())
    with pytest.raises(SimTargetMissing, match="does not exist in the simulated DEV estate"):
        ais.read_processing_option_value("vdb", "P4210", "NOSUCH01", "PDOCTYPE")


# ---------------------------------------------------------------------
# Failures and timeouts: explicit test conditions only
# ---------------------------------------------------------------------
def test_a_timeout_after_apply_is_unknown_and_the_value_is_really_there(client):
    from jde_mcp_server import sim_estate

    change = _ready(client, "S-SIM-TA")
    sim_estate.add_fault("vdb", "JDV920", operation="po_write", target="P4210|CIQ0001", mode="timeout_after_apply")
    with pytest.raises(httpx.ReadTimeout, match="TEST CONDITION"):
        _execute("S-SIM-TA", change["change_id"])
    pre = client.get("/changes/S-SIM-TA/execution/preflight", headers=headers("vdb")).json()
    assert pre["writeState"] == "unknown"
    # Reconciliation reads the shared estate, where the value was applied.
    body = client.post("/changes/S-SIM-TA/execution/reconcile", headers=headers("vdb"), json={}).json()
    assert body["outcome"] == "applied" and body["observedValue"] == "SO"
    assert "SIMULATION" in body["evidenceReference"]


def test_a_request_that_never_left_is_not_sent_and_may_run_again(client):
    from jde_mcp_server import approval, execution, sim_estate

    change = _ready(client, "S-SIM-NS")
    sim_estate.add_fault("vdb", "JDV920", operation="po_write", target="P4210|CIQ0001", mode="fail_before_send")
    with pytest.raises(httpx.ConnectError):
        _execute("S-SIM-NS", change["change_id"])
    assert execution.effective_state(approval._load(change["change_id"])) == "ready"
    assert sim_estate.read_processing_option("vdb", "JDV920", "P4210", "CIQ0001", "PDOCTYPE") == "S3"
    _execute("S-SIM-NS", change["change_id"])  # the fault was consumed; every check runs again
    assert sim_estate.read_processing_option("vdb", "JDV920", "P4210", "CIQ0001", "PDOCTYPE") == "SO"


def test_a_discovery_timeout_is_a_failed_read_not_a_fabricated_result(client):
    from jde_api_service.discovery import service
    from jde_mcp_server import sim_estate

    ready_company(client)
    _approved_story("S-SIM-DT")
    sim_estate.add_fault("vdb", "JDV920", operation="discovery_read", target="P4210|CIQ0001", mode="timeout_before_apply",
                         message="no answer within the profile timeout")
    grant, _ = service.grant_for_story("S-SIM-DT", "vdb", agent_run_id="RUN-dt", actor_user_id="u-hendro")
    with pytest.raises(service.DiscoveryFailed, match="TEST CONDITION"):
        service.execute_read(grant, "processing_option_values", "P4210|CIQ0001", [], [], 10)
    # One read, one failure: the next read is answered normally.
    ev = service.execute_read(grant, "processing_option_values", "P4210|CIQ0001", [], [], 10)
    assert ev["record_count"] >= 1
