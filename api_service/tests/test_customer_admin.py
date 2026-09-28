"""
Customer administration from the frontend: edit the customer, create a new
customer (an ordinary customer -- there is no demo or simulated JDE any
more), switch agents on or off per customer -- and, for a new customer
with no JD Edwards connection yet, nothing is recorded as delivered on a
person's word without the value they read and its evidence.
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
    assert got["updatedBy"] and "isDemo" not in got["customer"]
    assert client.put("/admin/customer-profile", headers=headers("vdb"), json={"name": " "}).status_code == 422
    assert viewer_client.put("/admin/customer-profile", headers=headers("vdb"), json={"name": "X"}).status_code == 403


def test_a_new_customer_is_ordinary_and_its_creator_is_its_admin(client):
    r = client.post("/admin/customers", headers=headers("vdb"),
                    json={"name": "Acme Foods", "toolsRelease": "9.2.26.2", "environment": "JPS920"})
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"].startswith("acme-foods-") and "isDemo" not in new
    session = client.get("/session").json()
    mine = next(c for c in session["customers"] if c["id"] == new["id"])
    assert "admin" in mine["roles"] and "isDemo" not in mine
    # Its JDE connection is a live one, like every customer's.
    live = {**profile_body(new["id"]), "expectedRevision": None}
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


def test_without_a_jde_connection_nothing_is_recorded_on_a_bare_claim(client):
    """A new customer has no JD Edwards connection, so Jade cannot read the
    value back: recording "applied" without the value the person read and an
    evidence reference records nothing."""
    import pytest

    from jde_api_service.delivery import functional
    from jde_mcp_server import approval, execution

    from .test_stage1_execution_safeguards import _approve, _approved_story, _full_scope, _propose, _save_scope

    new = client.post("/admin/customers", headers=headers("vdb"), json={"name": "Real Co"}).json()
    _save_scope(client, new["id"], _full_scope())
    _approved_story("S-CA-NOCONN", company=new["id"])
    change = _propose("S-CA-NOCONN")
    _approve(change["change_id"], company=new["id"])
    for stated, evidence in ((None, "screenshot 1"), ("SO", ""), (None, "")):
        with pytest.raises(functional.DeliveryRefused, match="cannot read the value back live"):
            functional.record_applied(change["change_id"], actor_user_id="u-hendro", actor_name="Hendro",
                                      evidence_reference=evidence, stated_value=stated)
    assert execution.effective_state(approval._load(change["change_id"]), execution.WRITE) == "ready"
