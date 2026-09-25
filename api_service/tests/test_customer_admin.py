"""
Customer administration from the frontend: edit the customer, create a new
(real, non-demo) customer, switch agents on or off per customer -- and the
rule that simulated JDE only exists inside demo customers.
"""

from __future__ import annotations

from ._discovery import profile_body
from .conftest import headers


def test_admin_edits_the_customer_and_it_persists(client, viewer_client):
    r = client.put("/admin/customer-profile", headers=headers("vdb"),
                   json={"name": "Van den Berg Logistiek BV", "shortName": "VdB", "toolsRelease": "9.2.8.2",
                         "environment": "JPS920"})
    assert r.status_code == 200, r.text
    got = client.get("/admin/customer-profile", headers=headers("vdb")).json()
    assert got["customer"]["name"] == "Van den Berg Logistiek BV" and got["customer"]["environment"] == "JPS920"
    assert got["customer"]["isDemo"] is True and got["updatedBy"]
    assert client.put("/admin/customer-profile", headers=headers("vdb"), json={"name": " "}).status_code == 422
    assert viewer_client.put("/admin/customer-profile", headers=headers("vdb"), json={"name": "X"}).status_code == 403


def test_a_new_customer_is_real_and_its_creator_is_its_admin(client):
    r = client.post("/admin/customers", headers=headers("vdb"),
                    json={"name": "Acme Foods", "toolsRelease": "9.2.26.2", "environment": "JPS920"})
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["isDemo"] is False and new["id"].startswith("acme-foods-")
    session = client.get("/session").json()
    mine = next(c for c in session["customers"] if c["id"] == new["id"])
    assert "admin" in mine["roles"] and mine["isDemo"] is False
    # Simulation is refused for a real customer: live connections only.
    r = client.put("/admin/jde/profile", headers=headers(new["id"]), json={**profile_body(new["id"]), "expectedRevision": None})
    assert r.status_code == 422 and "demo customers" in r.json()["detail"]
    live = {**profile_body(new["id"]), "connectionMode": "live", "expectedRevision": None}
    assert client.put("/admin/jde/profile", headers=headers(new["id"]), json=live).status_code == 200


def test_switched_off_agents_never_start(client):
    s = client.get("/admin/agent-settings", headers=headers("vdb")).json()
    assert all(a["enabled"] for a in s["agents"]) and s["revision"] == 0
    r = client.put("/admin/agent-settings", headers=headers("vdb"),
                   json={"disabled": ["architect", "receive-agent"], "expectedRevision": 0})
    assert r.status_code == 200, r.text
    got = {a["name"]: a["enabled"] for a in r.json()["agents"]}
    assert got["architect"] is False and got["receive-agent"] is False and got["check-agent"] is True
    assert client.put("/admin/agent-settings", headers=headers("vdb"),
                      json={"disabled": ["nobody"], "expectedRevision": 1}).status_code == 422
    from jde_api_service.services import agent_settings

    try:
        agent_settings.require_enabled("vdb", "architect")
        raise AssertionError("a switched-off agent must be refused")
    except agent_settings.AgentDisabled as exc:
        assert "Architect Agent is switched off" in str(exc)
    agent_settings.require_enabled("nhd", "architect")  # other customers unaffected


def test_simulated_execution_is_refused_for_a_real_customer(client, monkeypatch):
    import os

    from jde_api_service.persistence.db import db_path
    from jde_mcp_server import authority

    new = client.post("/admin/customers", headers=headers("vdb"), json={"name": "Real Co"}).json()
    monkeypatch.setenv(authority.AUTH_DB_ENV, str(db_path()))
    assert os.path.exists(db_path())
    authority.require_simulation_allowed("vdb")  # demo customer: allowed
    try:
        authority.require_simulation_allowed(new["id"])
        raise AssertionError("simulation must be refused for a real customer")
    except authority.SimulationNotAllowed as exc:
        assert "nothing was executed" in str(exc)
