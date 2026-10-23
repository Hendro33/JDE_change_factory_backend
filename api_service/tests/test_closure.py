"""Register H3, H4, C4, F4: closing the loop with the source."""

from __future__ import annotations

from .conftest import headers
from .fixtures.fake_ais import apply_in_dev
from .fixtures.jira_double import FakeJiraGateway, _MockIssueState


def _jira(monkeypatch, key: str) -> FakeJiraGateway:
    from jde_api_service.models.jira_integration import JiraIntegrationConfigUpdate
    from jde_api_service.services import registry
    from jde_api_service.services.registry import get_jira_integration_service

    gw = FakeJiraGateway(seed=[_MockIssueState(key=key, id="1", summary="s", description="d", reporter="r",
                                               created="2026-10-01", status="Jade - In Progress")])
    monkeypatch.setattr(registry, "get_jira_gateway", lambda customer_id: gw)
    get_jira_integration_service().upsert("vdb", JiraIntegrationConfigUpdate(
        base_url="https://example.atlassian.net", project_key="XX", pickup_status="Ready for Jade",
        post_pickup_status="Jade - In Progress"), actor="Tester")
    return gw


def _origin(story: str, key: str) -> None:
    from jde_api_service.models.change_request import ChangeRequestSourceType
    from jde_api_service.services.registry import get_change_request_service

    get_change_request_service()._create(  # noqa: SLF001 -- a Jira-sourced request with this story's id
        customer_id="vdb", source_type=ChangeRequestSourceType.DIRECT, business_source="Business",
        source_reference=key, title="Default order type", raw_content="Please default the order type to SO.",
        requester="Ellen", request_id=story)


def _comments(gw: FakeJiraGateway, key: str) -> list[str]:
    return gw._issues[key].comments  # noqa: SLF001


def test_c4_a_business_question_is_posted_once_and_its_answer_noted(client, monkeypatch):
    from jde_mcp_server import backlog

    from jde_api_service.services import closure
    from jde_api_service.services.registry import get_change_service

    from .test_stage1_execution_safeguards import _approved_story

    gw = _jira(monkeypatch, "XX-7")
    _approved_story("S-CL-Q")
    _origin("S-CL-Q", "XX-7")
    rec = backlog._load("S-CL-Q")  # noqa: SLF001
    rec["story_detail"] = {"statement": "As a clerk I want SO", "acceptance_criteria": [{"id": "AC1", "text": "SO"}],
                           "open_questions": ["Should credit orders also default to SO?"]}
    backlog._save("S-CL-Q", rec)  # noqa: SLF001
    change = get_change_service().get_for_customer("S-CL-Q", "vdb")
    assert closure.sweep("vdb", [change]) == 1
    assert closure.sweep("vdb", [get_change_service().get_for_customer("S-CL-Q", "vdb")]) == 0   # never twice
    assert "Should credit orders also default to SO?" in _comments(gw, "XX-7")[0]
    issue = next(i for i in change.lifecycle.issues if i.source == "story")
    r = client.post(f"/changes/S-CL-Q/issues/{issue.id}/resolution", headers=headers("vdb"),
                    json={"status": "answered", "answer": "No, credit orders keep CO."})
    assert r.status_code == 200, r.text
    closure.sweep("vdb", [get_change_service().get_for_customer("S-CL-Q", "vdb")])
    assert any(c.startswith("Answered in JADE") and "credit orders keep CO" in c for c in _comments(gw, "XX-7"))


def test_h3_h4_the_outcome_goes_back_and_not_solved_opens_a_linked_request(client, monkeypatch):
    from jde_api_service.services import closure

    from .test_recorded_delivery import _applied, _change as approved_change

    gw = _jira(monkeypatch, "XX-8")
    approved_change(client, "S-CL-OUT")
    _origin("S-CL-OUT", "XX-8")
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, "S-CL-OUT", note="set in DEV").status_code == 200
    r = client.post("/changes/S-CL-OUT/delivery/test-result", headers=headers("vdb"),
                    json={"passed": True, "evidenceReference": "TEST-1", "note": "order entered, SO defaulted"})
    assert r.status_code == 200, r.text
    # Before acceptance the requester cannot confirm.
    r = client.post("/changes/S-CL-OUT/requester-confirmation", headers=headers("vdb"),
                    json={"solved": True, "onBehalfOf": "Ellen"})
    assert r.status_code == 409
    r = client.post("/changes/S-CL-OUT/acceptance", headers=headers("vdb"),
                    json={"decision": "accepted", "note": "works", "onBehalfOf": "Ellen Vos"})
    assert r.status_code == 200, r.text
    assert any(c.startswith("Outcome:") for c in _comments(gw, "XX-8"))   # H3
    r = client.post("/changes/S-CL-OUT/requester-confirmation", headers=headers("vdb"),
                    json={"solved": False, "note": "Credit orders now also get SO", "onBehalfOf": "Ellen"})
    assert r.status_code == 200, r.text
    card = r.json()["lifecycle"]["outcomeCard"]
    conf = card["requesterConfirmation"]
    assert conf["solved"] is False and conf["new_request"]
    assert card["linkedRequests"][0]["kind"] == "not_solved"
    new = client.get(f"/changes/{conf['new_request']}", headers=headers("vdb")).json()
    assert new["lifecycle"]["phase"] == "understand" and "Credit orders now also get SO" in new["originalRequest"]
    assert client.get("/changes/S-CL-OUT", headers=headers("vdb")).json()["lifecycle"]["phase"] != "understand"
    assert any("not solved" in c for c in _comments(gw, "XX-8"))
    assert client.post("/changes/S-CL-OUT/requester-confirmation", headers=headers("vdb"),
                       json={"solved": True, "onBehalfOf": "Ellen"}).status_code == 409


def test_f4_an_improvement_after_authorisation_becomes_a_linked_follow_up(client):
    from .test_recorded_delivery import _change as approved_change

    approved_change(client, "S-CL-FU")
    r = client.post("/changes/S-CL-FU/follow-up", headers=headers("vdb"),
                    json={"text": "Also default the order type for web orders."})
    assert r.status_code == 200, r.text
    links = r.json()["lifecycle"].get("outcomeCard")
    change = client.get("/changes/S-CL-FU", headers=headers("vdb")).json()
    from jde_api_service.services import closure

    rows = closure.links("vdb", "S-CL-FU")
    assert rows and rows[0]["kind"] == "follow_up"
    assert client.get(f"/changes/{rows[0]['request_id']}", headers=headers("vdb")).json()["lifecycle"]["phase"] == "understand"
    assert change["exactChange"]["option"] == "PDOCTYPE"   # the authorised scope did not grow
    del links
