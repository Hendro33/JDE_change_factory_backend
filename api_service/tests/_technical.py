"""Fixtures for the Technical workflow tests: a customer-owned custom
business function (C source, B5542001), a company scope that authorises
it, a scripted Architect design routed to the Technical Agent, and a
person's design approval. Approver identities here are synthetic test
users. Delivery is the recorded route: the tests record check-in, build,
CNC activation and verification the way the Application Manager and the
CNC do in Jade."""

from __future__ import annotations

import asyncio
import base64
import json

import claude_agent_sdk as sdk

from ._discovery import profile_body, ready_company
from .conftest import headers
from .test_stage1_execution_safeguards import _approved_story, _full_scope, _save_scope

ENV = "JDV920"
OBJECT_NAME = "B5542001"
OBJECT_KEY = "B5542001|BSFN"
SOURCE = """/* B5542001 -- Custom credit check (customer-owned, system code 55) */
#include <jde.h>

JDEBFRTN (ID) JDEBFWINAPI CustomCreditCheck (LPBHVRCOM lpBhvrCom, LPVOID lpVoid, LPDSD5542001 lpDS)
{
   if (lpDS->cOrderType == 'S' && lpDS->mnOrderTotal > lpDS->mnCreditLimit)
   {
      jdeStrcpy(lpDS->szHoldCode, _J("C1"));
   }
   else
   {
      jdeStrcpy(lpDS->szHoldCode, _J(""));
   }
   return ER_SUCCESS;
}
"""
OLD_LINE = "if (lpDS->cOrderType == 'S' && lpDS->mnOrderTotal > lpDS->mnCreditLimit)"
TESTS = [
    {"name": "exempt customer over limit is not held", "kind": "positive", "event": "CustomCreditCheck",
     "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "S", "CreditExempt": "Y"},
     "expected": {"HoldCode": ""}},
    {"name": "non-exempt customer over limit is still held", "kind": "negative", "event": "CustomCreditCheck",
     "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "S", "CreditExempt": "N"},
     "expected": {"HoldCode": "C1"}},
    {"name": "order within limit is released", "kind": "neighbouring", "event": "CustomCreditCheck",
     "inputs": {"OrderTotal": 500, "CreditLimit": 1000, "OrderType": "S", "CreditExempt": "N"},
     "expected": {"HoldCode": ""}},
]
ALL_PASSED = [{"name": t["name"], "passed": True, "note": "ran in DEV"} for t in TESTS]


def technical_scope() -> dict:
    body = _full_scope()
    body["technicalAgent"] = {"authorizedObjectTypes": ["BSFN"], "reservedProductCode": "55"}
    return body


def upload_source(client, company: str = "vdb", content: str = SOURCE, fmt: str = "c_source",
                  correspondence: str = "matches_dev_runtime", object_name: str = OBJECT_NAME,
                  object_type: str = "BSFN") -> dict:
    body = {"kind": "technical_export", "objectName": object_name, "objectType": object_type, "exportFormat": fmt,
            "customerEnvironment": ENV, "pathCode": "DV920", "release": "9.2",
            "sourceLocation": f"source/{object_name}.c",
            "repository": "git@customer.example:jde/custom.git", "commitRef": "5e7a1c0",
            "exportedAt": "2026-09-23T08:00:00+00:00", "runtimeCorrespondence": correspondence,
            "runtimeStatement": "CNC: built into the active DV920 package", "runtimeStatedBy": "Chris CNC",
            "fileName": f"{object_name}.c", "contentBase64": base64.b64encode(content.encode()).decode()}
    r = client.post("/admin/jde/artifacts", headers=headers(company), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def technical_design(client, monkeypatch, story: str, *, company: str = "vdb", route: str = "Technical Agent",
                     artifact: dict | None = None) -> None:
    """A scripted Architect run (stand-in for the model at the model boundary)
    that reads the object librarian row, consults the source artifact and
    routes to the Technical Agent."""
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    real_build = architecture_driver.build_discovery_tools

    def spy(*args, **kwargs):
        tools = real_build(*args, **kwargs)
        tools.read("object_librarian", OBJECT_NAME, ["SIOBNM", "SIFUNO", "SISY", "SIMD"])
        if artifact:
            tools.read_artifact(artifact["artifactId"], artifact["revision"])
        return tools

    summary = {
        "architect_decision": {"recommended_route": route, "confidence": 0.8,
                               "existing_functionality_found": "B5542001 holds over-limit orders with C1",
                               "alternatives_considered": [], "objects_affected": [OBJECT_NAME],
                               "dependencies_and_conflicts": [], "rollback_strategy": "restore the previous source"},
        "implementation_spec": {"sequence": ["exclude credit-exempt customers from the C1 hold in B5542001"],
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
    """A scripted Technical Agent (deterministic stand-in for the model at the
    model boundary) using the SAME run-bound tools the real agent gets."""
    from jde_api_service.technical import service, store
    from jde_api_service.technical.tools import TechnicalAgentTools

    if run_id is None:
        run_id = service.start_run(company, story, purpose="prepare", initiated_by="u-hendro")["run_id"]
    tools = TechnicalAgentTools(company_id=company, story_id=story, run_id=run_id)
    listing = tools.list_source_artifacts()["artifacts"]
    source = next(a for a in listing if a["format"] == "c_source")
    opened = tools.open_in_workspace(source["evidence_id"])
    assert opened["opened"], opened
    new_line = OLD_LINE[:-1] + " && lpDS->cCreditExempt != 'Y')" + extra + (f" /* MOD {story} */" if marker else "")
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


def record_apply(client, story: str, revision: int, company: str = "vdb", omw_project: str = "PRJ-JADE-1"):
    return client.post(f"/changes/{story}/technical/packages/{revision}/apply", headers=headers(company),
                       json={"omwProject": omw_project, "evidenceReference": "OMW project screenshot OMW-1"})


def record_build(client, story: str, revision: int, company: str = "vdb", succeeded: bool = True, log: str = ""):
    return client.post(f"/changes/{story}/technical/packages/{revision}/build", headers=headers(company),
                       json={"succeeded": succeeded, "buildReference": "DV920 package build DV920TECH01",
                             "log": log})


def record_verify(client, story: str, revision: int, company: str = "vdb", results=None, runtime: bool = True):
    return client.post(f"/changes/{story}/technical/packages/{revision}/verify", headers=headers(company),
                       json={"results": results if results is not None else ALL_PASSED,
                             "runtimeIsApprovedArtifact": runtime, "evidenceReference": "test run TR-7 in DV920"})


def cnc(client, story: str, revision: int, company: str = "vdb", package_name: str = "DV920TECH01"):
    return client.post(f"/changes/{story}/technical/packages/{revision}/cnc-activation", headers=headers(company),
                       json={"packageName": package_name, "evidenceReference": "CNC ticket CNC-99",
                             "note": "deployed to DV920"})
