"""
The reference journey (usability redesign, step 2): a hand-off to a named
implementer with its specification (PDF) and recorded evidence, the business
acceptance of the outcome bound to exactly what was verified, the three
outcomes kept apart, work items with owners, and JADE's live activity line.

Nothing here contacts a real JD Edwards system: the AIS server is the test
fixture at the HTTP boundary and the Architect is scripted at the model
boundary.
"""

from __future__ import annotations

import asyncio
import io
import json
import time

import claude_agent_sdk as sdk

from .conftest import accept_outcome, headers

ROUTE = "Human Implementation"


def _human_design(client, monkeypatch, story: str, *, company: str = "vdb") -> None:
    """A scripted Architect run that routes the story to a person (protected
    GL/AAI configuration), with its result block."""
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    from ._technical import OBJECT_NAME

    real_build = architecture_driver.build_discovery_tools

    def spy(*args, **kwargs):
        tools = real_build(*args, **kwargs)
        tools.read("object_librarian", OBJECT_NAME, ["SIOBNM", "SIFUNO", "SISY", "SIMD"])
        return tools

    summary = {
        "architect_decision": {"recommended_route": ROUTE, "confidence": 0.7,
                               "existing_functionality_found": "Line type F exists; its GL class posts freight to goods "
                                                               "revenue.",
                               "alternatives_considered": [], "objects_affected": ["F40205", "F4095"],
                               "dependencies_and_conflicts": [], "rollback_strategy": "Set AAI 4230 back to 5010"},
        "implementation_spec": {
            "sequence": ["Set AAI 4230 for GL class FR01 to object account 5110",
                         "Set line type F (F40205) to GL class FR01"],
            "required_mcp_operations": [],
            "human_actions_required": ["Finance confirms the posting on a test invoice",
                                       "Promote through CNC after the as-built record is finalised"],
            "validation_approach": "Enter a DEV order with a freight line, run Sales Update, check the GL posting"},
        "evidence": {},
        "result": {"conclusion": "Freight posts to its own revenue account once AAI 4230 and the line type's GL class "
                                 "are set; both are protected Finance settings, so a named implementer makes them.",
                   "changes": [{"what": "AAI 4230, GL class FR01", "current": "", "proposed": "5110",
                                "status": "not_yet_verified"}],
                   "issues": [], "uncertainty": []},
    }

    async def fake_query(prompt, options):
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=2,
                                session_id="t", result="```json\n" + json.dumps(summary) + "\n```")

    real_query = sdk.query
    architecture_driver.build_discovery_tools, sdk.query = spy, fake_query
    try:
        service = get_architecture_review_service()
        asyncio.run(architecture_driver.run_architecture_review(story_id=story, repo_root=settings.repo_root,
                                                                run_service=service, customer_id=company,
                                                                initiated_by="u-hendro"))
    finally:
        architecture_driver.build_discovery_tools, sdk.query = real_build, real_query
    assert service.get(story).stage == "done", service.get(story).error


def _ready(client, monkeypatch, story: str) -> None:
    from ._discovery import ready_company
    from ._technical import approved_reads, technical_scope
    from .test_stage1_execution_safeguards import _approved_story, _save_scope

    ready_company(client, "vdb", approvedReads=approved_reads("vdb"))
    _save_scope(client, "vdb", technical_scope())
    _approved_story(story)
    _human_design(client, monkeypatch, story)


def _lc(client, story: str) -> dict:
    return client.get(f"/changes/{story}", headers=headers("vdb")).json()["lifecycle"]


def test_a_human_implementation_is_handed_off_recorded_verified_accepted_and_released(client, monkeypatch):
    from pypdf import PdfReader

    from jde_mcp_server.evidence import entries, verify_chain

    from .test_technical_controls import _user

    story = "S-HI-1"
    h = headers("vdb")
    _ready(client, monkeypatch, story)

    # The specification is authorised before anyone implements it.
    lc = _lc(client, story)
    assert (lc["phase"], lc["nextAction"]["action"], lc["nextAction"]["owner"]) == (
        "solution_review", "authorise_handoff", "product_manager")
    view = client.get(f"/changes/{story}/handoff", headers=h).json()
    texts = [i["text"] for i in view["items"]]
    # The steps and the Finance check are hand-offs; promotion after the as-built record is the release itself.
    assert texts == ["Set AAI 4230 for GL class FR01 to object account 5110", "Set line type F (F40205) to GL class FR01",
                     "Finance confirms the posting on a test invoice"]
    assert view["authorisation"] is None and view["specification"]["acceptance_criteria"] is not None
    first = view["items"][0]["id"]
    r = client.post(f"/changes/{story}/handoff/items/{first}/complete", headers=h,
                    json={"evidenceReference": "screenshot P0012", "note": ""})
    assert r.status_code == 409 and "not authorised" in r.json()["detail"]

    pdf = client.get(f"/changes/{story}/handoff/specification.pdf", headers=h)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert "Implementation specification" in text and "NOT YET AUTHORISED" in text and "AAI 4230" in text

    # Only a role the approval policy allows authorises it.
    owner = _user("u-do-only", ["domain_owner"])
    assert owner.post(f"/changes/{story}/handoff/authorise", headers=h, json={"note": ""}).status_code == 403
    r = client.post(f"/changes/{story}/handoff/authorise", headers=h, json={"note": "Finance agreed the account"})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert (lc["phase"], lc["nextAction"]["action"]) == ("delivery", "record_handoff")
    assert client.post(f"/changes/{story}/handoff/authorise", headers=h, json={"note": ""}).status_code == 409
    assert "Authorised by" in "".join(p.extract_text() for p in PdfReader(io.BytesIO(
        client.get(f"/changes/{story}/handoff/specification.pdf", headers=h).content)).pages)

    # A named implementer, and each item recorded once, with evidence.
    r = client.post(f"/changes/{story}/handoff/items/{first}/implementer", headers=h, json={"name": "Finance (Marieke)"})
    assert r.status_code == 200
    assert "Finance (Marieke)" in r.json()["lifecycle"]["nextAction"]["summary"]
    r = client.post(f"/changes/{story}/handoff/items/{first}/complete", headers=h, json={"evidenceReference": "", "note": ""})
    assert r.status_code == 409 and "evidence reference" in r.json()["detail"]
    for i, item in enumerate(view["items"]):
        r = client.post(f"/changes/{story}/handoff/items/{item['id']}/complete", headers=h,
                        json={"evidenceReference": f"P0012 screenshot {i}", "note": "done in DEV"})
        assert r.status_code == 200, r.text
    assert client.post(f"/changes/{story}/handoff/items/{first}/complete", headers=h,
                       json={"evidenceReference": "again", "note": ""}).status_code == 409
    lc = _lc(client, story)
    assert (lc["phase"], lc["nextAction"]["action"]) == ("validation", "record_handoff_verification")

    # Verification, then the business accepts: three outcomes, never implied by one another.
    r = client.post(f"/changes/{story}/handoff/verification", headers=h,
                    json={"passed": True, "evidenceReference": "invoice 4711 GL posting", "note": "Freight posted to 5110"})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert (lc["phase"], lc["nextAction"]["action"], lc["nextAction"]["owner"]) == (
        "validation", "accept_outcome", "domain_owner")
    card = lc["outcomeCard"]
    assert card["devVerified"]["done"] and not card["businessAccepted"]["done"] and not card["released"]["done"]
    assert any("AAI 4230" in w for w in card["whatChanged"]) and card["verified"] == ["Freight posted to 5110"]
    groups = [(w["group"], w["state"]) for w in lc["workItems"]]
    assert groups.count(("handoff", "done")) == 3 and ("acceptance", "todo") in groups and ("release", "todo") in groups
    assert next(w for w in lc["workItems"] if w["id"] == first)["completedBy"] == "Finance (Marieke)"

    # A Domain Owner accepts only within their own domain (this story has none); the Application
    # Manager records it on a named business person's behalf.
    r = client.post(f"/changes/{story}/acceptance", headers=h, json={"decision": "accepted", "note": ""})
    assert r.status_code == 403
    accept_outcome(client, story, by="Marieke Jansen (Finance)")
    lc = _lc(client, story)
    assert (lc["phase"], lc["nextAction"]["action"]) == ("release", "finalise_asbuilt")
    assert lc["outcomeCard"]["businessAccepted"]["by"] == "Marieke Jansen (Finance)"

    # The as-built record carries every hand-off, the verification and the acceptance; then it is released.
    rec = client.post(f"/changes/{story}/as-built", headers=h).json()
    cps = {c["id"]: c["complete"] for c in rec["content"]["checkpoints"]}
    assert cps["implementation_recorded"] and cps["verified"] and cps["business_accepted"] and cps["design_approved"], cps
    assert client.post(f"/changes/{story}/as-built/{rec['version']}/finalise", headers=h).status_code == 200
    lc = _lc(client, story)
    assert (lc["phase"], lc["outcome"]) == ("done", "delivered") and lc["outcomeCard"]["released"]["done"]
    events = [e.get("event") for e in entries(story)]
    for e in ("handoff_authorised", "handoff_completed", "handoff_verified", "business_acceptance"):
        assert e in events, events
    assert verify_chain(story)["valid"] is True


def test_acceptance_is_bound_to_exactly_what_was_verified(client):
    from jde_mcp_server import approval

    from .fixtures.fake_ais import apply_in_dev
    from .test_recorded_delivery import _applied, _change

    story = "S-ACC-1"
    h = headers("vdb")
    rec = _change(client, story)
    apply_in_dev("vdb", "P4210", "CIQ0001", "PDOCTYPE", "SO")
    assert _applied(client, story).status_code == 200
    # Nothing to accept before DEV verification.
    r = client.post(f"/changes/{story}/acceptance", headers=h, json={"decision": "accepted", "onBehalfOf": "Ellen"})
    assert r.status_code == 409 and "verified in DEV first" in r.json()["detail"]
    assert client.post(f"/changes/{story}/delivery/run-test", headers=h).status_code == 200

    # "Not accepted" needs a reason, stops progression, and the release cannot happen.
    r = client.post(f"/changes/{story}/acceptance", headers=h, json={"decision": "not_accepted", "note": "no",
                                                                      "onBehalfOf": "Ellen"})
    assert r.status_code == 409
    r = client.post(f"/changes/{story}/acceptance", headers=h,
                    json={"decision": "not_accepted", "note": "Webshop orders still arrive as S3", "onBehalfOf": "Ellen"})
    lc = r.json()["lifecycle"]
    assert (lc["health"], lc["nextAction"]["owner"]) == ("blocked", "product_manager")
    assert "still arrive as S3" in lc["nextAction"]["summary"] and lc["outcomeCard"]["notAccepted"]["detail"]
    draft = client.post(f"/changes/{story}/as-built", headers=h).json()
    assert client.post(f"/changes/{story}/as-built/{draft['version']}/finalise", headers=h).status_code == 422

    accept_outcome(client, story)
    assert _lc(client, story)["nextAction"]["action"] == "finalise_asbuilt"
    # A different verification (here: its record replaced) makes the acceptance stale; it is asked again.
    record = approval._load(rec["change_id"])  # noqa: SLF001
    record["verification"] = {**record["verification"], "evidence_entry_hash": "another-run", "at": time.time()}
    approval._save(rec["change_id"], record)  # noqa: SLF001
    lc = _lc(client, story)
    assert lc["nextAction"]["action"] == "accept_outcome" and not lc["outcomeCard"]["businessAccepted"]["done"]
    history = client.get(f"/changes/{story}/acceptance", headers=h).json()["history"]
    assert [x["decision"] for x in history] == ["not_accepted", "accepted"]


def test_work_items_show_who_does_what_in_order(client):
    from .test_recorded_delivery import _change

    story = "S-WI-1"
    _change(client, story)
    items = _lc(client, story)["workItems"]
    assert [w["id"] for w in items] == ["I1", "TEST", "ACCEPT", "RELEASE"]
    test = next(w for w in items if w["id"] == "TEST")
    assert test["ownerKind"] == "agent" and "ORCH_SO" in test["title"] and test["dependsOn"] == ["I1"]
    assert next(w for w in items if w["id"] == "ACCEPT")["who"] == "Domain Owner"


def test_jade_at_work_says_what_it_is_reading(isolated_dirs):
    from datetime import datetime, timezone

    from jde_api_service.persistence.db import connection
    from jde_api_service.services import lifecycle

    now = datetime.now(timezone.utc).isoformat()
    with connection() as conn:
        conn.execute("INSERT INTO discovery_observations (id, company_id, story_id, agent_run_id, actor_user_id, "
                     "profile_revision, capability_id, request, observed_at, mode, sharing_policy, evidence, "
                     "payload_sha256, result_count, refresh_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     ("OBS-1", "vdb", "S-LIVE", "run-1", None, 1, "udc_values", json.dumps({"target": "00/LT"}), now,
                      "live", "values", "{}", "x", 12, None))
    assert lifecycle._activity("vdb", "S-LIVE") == "Reading the codes in 00/LT … 12 found"
    assert lifecycle._activity("vdb", "S-OTHER") == ""


def test_the_outcome_links_to_the_jira_ticket(monkeypatch):
    from types import SimpleNamespace

    from jde_api_service.models.change import Change
    from jde_api_service.services import registry, work_items

    monkeypatch.setattr(registry, "get_jira_integration_service", lambda: SimpleNamespace(
        get_for_customer=lambda c: SimpleNamespace(base_url="https://consultiq.atlassian.net/")))
    c = Change(id="S-1", customer_id="c1", title="t", source="Business", state="APPROVED", source_reference="CON-40",
               created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00")
    assert work_items._jira(c) == ("CON-40", "https://consultiq.atlassian.net/browse/CON-40")
    assert work_items._jira(c.model_copy(update={"source_reference": "email from Ellen"})) == ("", "")
