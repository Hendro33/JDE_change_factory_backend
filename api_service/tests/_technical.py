"""Fixtures for the Technical workflow tests: a SYNTHETIC customer-owned
event-rule object (jade_sim_er -- a simulation format, not a JDE export), a
company scope that authorises it, a scripted Architect design routed to the
Technical Agent, and a person's design approval. Approver identities here
are synthetic test users."""

from __future__ import annotations

import asyncio
import base64
import json

import claude_agent_sdk as sdk

from ._discovery import profile_body, ready_company
from .conftest import headers
from .test_stage1_execution_safeguards import _approved_story, _full_scope, _save_scope

ENV = "JDV920"
OBJECT_KEY = "P554210|ER"
SOURCE = """// SYNTHETIC SIMULATION SOURCE (jade_sim_er) -- not a JD Edwards export.
OBJECT P554210 FORM W554210A SYSTEM 55
INPUT BC OrderTotal NUMBER
INPUT BC CreditLimit NUMBER
INPUT BC OrderType STRING
INPUT BC CreditExempt STRING
OUTPUT VA HoldCode STRING

EVENT OK_Button_Clicked
IF BC OrderType = "SO" AND BC OrderTotal > BC CreditLimit
    VA HoldCode = "C1"
ELSE
    VA HoldCode = ""
END IF
END EVENT
"""
OLD_LINE = 'IF BC OrderType = "SO" AND BC OrderTotal > BC CreditLimit'
BUILD_RULES = [{"id": "SIM-BLD-1", "kind": "modification_marker", "marker": "// MOD {story_id}",
                "description": "customer standard: every added or changed line carries a modification marker"}]
TESTS = [
    {"name": "exempt customer over limit is not held", "kind": "positive", "event": "OK_Button_Clicked",
     "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "SO", "CreditExempt": "Y"},
     "expected": {"HoldCode": ""}},
    {"name": "non-exempt customer over limit is still held", "kind": "negative", "event": "OK_Button_Clicked",
     "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "SO", "CreditExempt": "N"},
     "expected": {"HoldCode": "C1"}},
    {"name": "order within limit is released", "kind": "neighbouring", "event": "OK_Button_Clicked",
     "inputs": {"OrderTotal": 500, "CreditLimit": 1000, "OrderType": "SO", "CreditExempt": "N"},
     "expected": {"HoldCode": ""}},
    {"name": "direct-ship order type is unaffected", "kind": "neighbouring", "event": "OK_Button_Clicked",
     "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "S3", "CreditExempt": "N"},
     "expected": {"HoldCode": ""}},
]


def technical_scope() -> dict:
    body = _full_scope()
    body["technicalAgent"] = {"authorizedObjectTypes": ["ER"], "reservedProductCode": "55"}
    return body


def seed_object(company: str = "vdb", source: str = SOURCE) -> str:
    from jde_mcp_server import technical_sim

    return technical_sim.seed_object(company, ENV, source=source, description="Custom Sales Order Review (synthetic)",
                                     build_rules=BUILD_RULES, actor="test", reason="TEST FIXTURE: synthetic ER object")


def upload_source(client, company: str = "vdb", content: str = SOURCE, fmt: str = "jade_sim_er",
                  correspondence: str = "matches_dev_runtime", object_name: str = "P554210") -> dict:
    body = {"kind": "technical_export", "objectName": object_name, "objectType": "ER", "exportFormat": fmt,
            "customerEnvironment": ENV, "pathCode": "DV920", "release": "9.2", "sourceLocation": "er/P554210.jser",
            "repository": "git@customer.example:jde/er-exports.git", "commitRef": "5e7a1c0",
            "exportedAt": "2026-09-23T08:00:00+00:00", "runtimeCorrespondence": correspondence,
            "runtimeStatement": "CNC: exported from the active DV920 runtime", "runtimeStatedBy": "Chris CNC",
            "fileName": "P554210.jser", "contentBase64": base64.b64encode(content.encode()).decode()}
    r = client.post("/admin/jde/artifacts", headers=headers(company), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def technical_design(client, monkeypatch, story: str, *, company: str = "vdb", route: str = "Technical Agent",
                     artifact: dict | None = None) -> None:
    """A scripted Architect run (stand-in for the model) that reads the object
    librarian row, consults the source artifact and routes to the Technical Agent."""
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    real_build = architecture_driver.build_discovery_tools

    def spy(*args, **kwargs):
        tools = real_build(*args, **kwargs)
        tools.read("object_librarian", "P554210", ["SIOBNM", "SIFUNO", "SISY", "SIMD"])
        if artifact:
            tools.read_artifact(artifact["artifactId"], artifact["revision"])
        return tools

    summary = {
        "architect_decision": {"recommended_route": route, "confidence": 0.8,
                               "existing_functionality_found": "P554210 holds over-limit SO orders with C1",
                               "alternatives_considered": [], "objects_affected": ["P554210"],
                               "dependencies_and_conflicts": [], "rollback_strategy": "restore the previous source"},
        "implementation_spec": {"sequence": ["exclude credit-exempt customers from the C1 hold in P554210"],
                                "required_mcp_operations": [], "human_actions_required": ["CNC deploys the package"],
                                "validation_approach": "positive, negative and neighbouring tests"},
        "evidence": {},
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


def approved_reads(company: str = "vdb") -> list[dict]:
    reads = profile_body(company)["approvedReads"]
    for r in reads:
        if r["capabilityId"] == "object_librarian":
            r["fields"] = ["SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"]
    return reads


def ready_story(client, monkeypatch, story: str, *, company: str = "vdb", approve: bool = True) -> dict:
    ready_company(client, company, approvedReads=approved_reads(company))
    _save_scope(client, company, technical_scope())
    seed_object(company)
    art = upload_source(client, company)
    _approved_story(story, company)
    technical_design(client, monkeypatch, story, company=company, artifact=art)
    if approve:
        design = client.get(f"/changes/{story}/technical", headers=headers(company)).json()["assignment"]
        r = client.post(f"/changes/{story}/technical/approve-design", headers=headers(company),
                        json={"designRevision": design["design_revision"], "note": "synthetic test approval"})
        assert r.status_code == 200, r.text
    return art


def prepare(company: str, story: str, *, marker: bool = True, run_id: str | None = None, extra: str = "") -> dict:
    """A scripted Technical Agent (deterministic stand-in for the model) using
    the SAME run-bound tools the real agent gets."""
    from jde_api_service.technical import service, store
    from jde_api_service.technical.tools import TechnicalAgentTools

    if run_id is None:
        run_id = service.start_run(company, story, purpose="prepare", initiated_by="u-hendro")["run_id"]
    tools = TechnicalAgentTools(company_id=company, story_id=story, run_id=run_id)
    listing = tools.list_source_artifacts()["artifacts"]
    source = next(a for a in listing if a["format"] == "jade_sim_er")
    opened = tools.open_in_workspace(source["evidence_id"])
    assert opened["opened"], opened
    new_line = OLD_LINE + ' AND BC CreditExempt != "Y"' + extra + (f" // MOD {story}" if marker else "")
    assert tools.replace(opened["file_id"], OLD_LINE, new_line)["changed"]
    out = tools.submit({"explanation": "credit-exempt customers are excluded from the C1 hold; nothing else changes",
                        "requirement_trace": [{"requirement": "exempt customers not held", "how": "extra condition"}],
                        "dependencies": ["F0301 credit limit (read by the application, unchanged)"],
                        "test_plan": TESTS, "missing_evidence": [], "unsupported": [],
                        "recovery": "restore the previous source as a new approved revision",
                        "repair_reason": "build log" if store.get_package(company, story) else ""})
    store.finish_run(run_id, status="completed", outcome=tools.outcome)
    return out


def approve_package(client, story: str, revision: int, company: str = "vdb") -> dict:
    r = client.post(f"/changes/{story}/technical/packages/{revision}/approve", headers=headers(company),
                    json={"note": "synthetic test approval"})
    assert r.status_code == 200, r.text
    return r.json()


def milestone(client, story: str, revision: int, name: str, company: str = "vdb"):
    return client.post(f"/changes/{story}/technical/packages/{revision}/{name}", headers=headers(company))


def cnc(client, story: str, revision: int, company: str = "vdb", package_name: str = "DV920TECH01"):
    return client.post(f"/changes/{story}/technical/packages/{revision}/cnc-activation", headers=headers(company),
                       json={"packageName": package_name, "evidenceReference": "synthetic CNC ticket CNC-99",
                             "note": "simulated hand-off"})
