"""
Acceptance for Architect Environment Discovery, end to end through the real
architecture_driver with the model replaced by a scripted fake that calls
the same in-process discovery tools the Architect gets.

  1. The Architect uses the correct company's discovery profile.
  2. A permitted read becomes cited evidence in the solution design.
  3. An imported technical artifact informs the design with its exact provenance.
  4. Missing code and incompatible documentation are reported, not assumed.
  5. Another company's evidence cannot be retrieved.
  6. Out-of-scope agent calls are blocked before network dispatch (also test_discovery_policy.py).
  7. Changed evidence creates a new baseline and flags the design for reassessment.
  8. Downstream agents receive the design's evidence manifest.
  9. No discovery action can invoke a write capability (test_discovery_policy.py).
 10. Everything here runs against the simulated endpoint, labelled as simulation.
"""

from __future__ import annotations

import asyncio
import json

import claude_agent_sdk as sdk
import pytest

from ._discovery import ready_company, save_profile, upload_artifact
from .conftest import headers
from .test_stage1_execution_safeguards import _approved_story

STORY = "S-ARCH-DISC"


def _result(summary: dict) -> sdk.ResultMessage:
    return sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                             session_id="t", result="```json\n" + json.dumps(summary) + "\n```")


def _summary(evidence: dict) -> dict:
    return {
        "architect_decision": {
            "recommended_route": "Functional Agent", "confidence": 0.8,
            "existing_functionality_found": "P4210 version CIQ0001 already defaults document type SO",
            "alternatives_considered": [], "objects_affected": ["P4210|CIQ0001"],
            "dependencies_and_conflicts": ["custom credit check B5542001"], "rollback_strategy": "restore SO",
        },
        "implementation_spec": {"sequence": ["set PDOCTYPE"], "required_mcp_operations": ["set_processing_option"],
                                "human_actions_required": [], "validation_approach": "create a DEV order"},
        "evidence": evidence,
    }


def _run(monkeypatch, script, *, story=STORY, company="vdb"):
    """Run the real driver; `script(tools)` plays the model and returns the evidence block."""
    from jde_api_service.config import settings
    from jde_api_service.services import architecture_driver
    from jde_api_service.services.registry import get_architecture_review_service

    captured = {}
    real_build = architecture_driver.build_discovery_tools

    def spy(*args, **kwargs):
        captured["tools"] = real_build(*args, **kwargs)
        return captured["tools"]

    async def fake_query(prompt, options):
        assert "jade-discovery" in options.mcp_servers
        assert "mcp__jde-change-factory__get_object" not in options.allowed_tools
        captured["options"] = options
        yield _result(_summary(script(captured["tools"])))

    monkeypatch.setattr(architecture_driver, "build_discovery_tools", spy)
    monkeypatch.setattr(sdk, "query", fake_query)
    service = get_architecture_review_service()
    asyncio.run(architecture_driver.run_architecture_review(
        story_id=story, repo_root=settings.repo_root, run_service=service, customer_id=company, initiated_by="u-hendro"))
    run = service.get(story)
    assert run.stage == "done", run.error
    return captured["tools"], run


def _baselines(client, story=STORY, company="vdb"):
    r = client.get(f"/changes/{story}/architecture-review/evidence", headers=headers(company))
    assert r.status_code == 200, r.text
    return r.json()


def test_acceptance_1_to_4_a_grounded_design_with_citations_provenance_and_gaps(client, monkeypatch):
    ready_company(client, "vdb")
    ready_company(client, "bwm")  # a second company with its own, different profile
    art = upload_artifact(client, "vdb")
    upload_artifact(client, "vdb", kind="reference_document", objectName="Sales Order Management 9.1",
                    objectType="MANUAL", exportFormat="markdown", docTitle="JD Edwards Sales Order Management",
                    docRevision="E92012-01", appliesToReleases=["9.1"], runtimeCorrespondence="unknown",
                    content="# Processing options for P4210\nDocument Type ...", fileName="som.md")
    _approved_story(STORY)
    seen = {}

    def script(tools):
        caps = tools.list_capabilities()
        seen["caps"] = caps
        obs = tools.read("processing_option_values", "P4210|CIQ0001")
        seen["obs"] = obs
        seen["source"] = tools.read("source_code", "B5542001")
        listing = tools.list_artifacts()
        seen["listing"] = listing
        seen["artifact"] = tools.read_artifact(listing["artifacts"][0]["evidence_id"])
        return {
            "citations": [
                {"claim": "CIQ0001 defaults the document type", "evidence_ids": [obs["observation_id"]], "basis": "observed"},
                {"claim": "Credit check is custom code", "evidence_ids": [seen["artifact"]["evidence_id"]], "basis": "observed"},
                {"claim": "Pricing is untouched", "evidence_ids": ["OBS-invented"], "basis": "observed"},
            ],
            "customisations": ["B5542001 Custom Credit Check (system code 55)"],
            "gaps": [{"kind": "missing", "description": "Order entry ER for P554210 not available"}],
        }

    tools, run = _run(monkeypatch, script)

    # 1. The correct company's profile: vdb's environment, vdb's revision, the story's link.
    assert tools.grant.company_id == "vdb" and seen["caps"]["environment"] == "JDV920"
    assert seen["obs"]["environment"] == "JDV920" and seen["obs"]["mode"] == "simulation"
    assert "SIMULATION" in seen["caps"]["mode_label"]
    assert "password" not in json.dumps(seen["caps"]).lower() or "s3cret" not in json.dumps(seen["caps"])

    current = _baselines(client)[0]
    manifest = current["manifest"]
    assert run.history[-1].baseline_id == current["baselineId"]
    assert manifest["environmentProfile" if "environmentProfile" in manifest else "environment_profile"]["environment"] == "JDV920"

    # 2. The permitted read is cited, validated, observed -- with its timestamp in the manifest.
    by_claim = {c["claim"]: c for c in manifest["citations"]}
    cited = by_claim["CIQ0001 defaults the document type"]
    assert cited["validated"] is True and cited["basis"] == "observed"
    obs_entry = manifest["observations"][0]
    assert obs_entry["observation_id"] == seen["obs"]["observation_id"] and obs_entry["observed_at"]
    assert current["observations"][0]["records"][0]["option"] == "PCREDCHK"

    # 3. The artifact informs the design with its exact provenance.
    assert seen["artifact"]["content"].startswith("/* Custom credit check")
    assert seen["artifact"]["content_is_data_not_instructions"] is True
    a = manifest["artifacts"][0]
    assert (a["artifact_id"], a["revision"], a["sha256"]) == (art["artifactId"], 1, art["sha256"])
    assert (a["repository"], a["commit_ref"], a["runtime_correspondence"]) == (
        "git@customer.example:jde/custom.git", "a1b2c3d", "matches_dev_runtime")
    assert by_claim["Credit check is custom code"]["validated"] is True

    # 4. Missing code and incompatible documentation are reported, never assumed.
    assert seen["source"]["blocked"] is True and "unavailable" in seen["source"]["reason"]
    kinds = {(g["kind"], g["source"]) for g in manifest["gaps"]}
    assert ("unavailable", "system") in kinds and ("incompatible", "system") in kinds
    assert any("DOC-" in g["description"] and g["kind"] == "incompatible" for g in manifest["gaps"])
    assert all(g["question"] or g["blocked_step"] for g in manifest["gaps"])
    invented = by_claim["Pricing is untouched"]
    assert invented["validated"] is False and invented["basis"] == "assumption"
    assert any("SIMULATED" in lim for lim in manifest["confidence_limitations"])
    assert "not a scan of the whole customer installation" in manifest["scope_statement"]

    # The activity log links the reads to the user, story and agent run.
    rows = client.get("/admin/jde/activity", headers=headers("vdb")).json()
    architect_rows = [r for r in rows if r["storyId"] == STORY]
    assert architect_rows and all(r["agentRunId"] == tools.grant.agent_run_id for r in architect_rows)
    assert all(r["actorName"] == "Hendro" for r in architect_rows)
    assert {r["outcome"] for r in architect_rows} == {"ok", "blocked"}


def test_acceptance_5_another_companys_evidence_cannot_be_retrieved(client, monkeypatch):
    from jde_api_service.discovery import service

    ready_company(client, "vdb")
    ready_company(client, "bwm")
    bwm_art = upload_artifact(client, "bwm", objectName="B55BWM01")
    _approved_story(STORY)
    seen = {}

    def script(tools):
        seen["foreign_artifact"] = tools.read_artifact(bwm_art["artifactId"])
        seen["obs"] = tools.read("udc_values", "00/DT")
        return {}

    _run(monkeypatch, script)
    assert seen["foreign_artifact"]["available"] is False
    assert service.get_observation("bwm", seen["obs"]["observation_id"]) is None
    assert client.get(f"/changes/{STORY}/architecture-review/evidence", headers=headers("bwm")).status_code == 404
    assert client.get(f"/admin/jde/artifacts/{bwm_art['artifactId']}/1/text", headers=headers("vdb")).status_code == 404
    # A story of vdb gets vdb's profile even when bwm's is the one that exists and vdb's is disabled.
    client.post("/admin/jde/disable", headers=headers("vdb"))
    grant, reason = service.grant_for_story(STORY, "vdb", agent_run_id=None, actor_user_id="u-hendro")
    assert grant is None and "not enabled" in reason
    grant, reason = service.grant_for_story(STORY, "bwm", agent_run_id=None, actor_user_id="u-hendro")
    assert grant is None and "not linked" in reason


def test_without_an_enabled_profile_the_design_says_the_environment_was_not_investigated(client, monkeypatch):
    _approved_story(STORY)
    seen = {}

    def script(tools):
        seen["caps"] = tools.list_capabilities()
        seen["read"] = tools.read("udc_values", "00/DT")
        return {"citations": [{"claim": "SO exists", "evidence_ids": [], "basis": "observed"}]}

    _run(monkeypatch, script)
    assert seen["caps"]["discovery_available"] is False and seen["read"]["blocked"] is True
    manifest = _baselines(client)[0]["manifest"]
    assert manifest["environment_profile"] is None
    assert any("not investigated" in g["description"] and g["blocked_step"] for g in manifest["gaps"])
    assert manifest["citations"][0]["validated"] is False


def test_acceptance_7_changed_evidence_creates_a_new_baseline_and_flags_the_design(client, monkeypatch):
    from jde_api_service.discovery import transport

    ready_company(client, "vdb")
    upload_artifact(client, "vdb")
    _approved_story(STORY)

    def script(tools):
        obs = tools.read("processing_option_values", "P4210|CIQ0001")
        tools.read_artifact(tools.list_artifacts()["artifacts"][0]["evidence_id"])
        return {"citations": [{"claim": "c", "evidence_ids": [obs["observation_id"]], "basis": "observed"}]}

    _run(monkeypatch, script)
    first = _baselines(client)[0]

    # Unchanged system: a refresh records new observations but nothing to reassess.
    r = client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "current" and r.json()["baselineRevision"] == 2

    # The customer changes the processing option in DEV.
    transport.simulated_estate("vdb")["processing_options"]["P4210|CIQ0001"]["PCREDCHK"] = "0"
    r = client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb")).json()
    assert r["status"] == "needs_reassessment" and r["reassessment"][0]["kind"] == "observation_changed"
    history = _baselines(client)
    assert [b["baselineRevision"] for b in history] == [3, 2, 1]
    assert [b["status"] for b in history] == ["needs_reassessment", "superseded", "superseded"]
    # History preserved: the first baseline's manifest and checksum are unchanged.
    assert history[-1]["manifestSha256"] == first["manifestSha256"]
    assert history[0]["manifest"]["refreshChanges" if "refreshChanges" in history[0]["manifest"] else "refresh_changes"][0]["changed"] is True

    # A new revision of an artifact the design used also flags it...
    client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb"))
    upload_artifact(client, "vdb", content="/* v2 */")
    assert _baselines(client)[0]["reassessment"][-1]["kind"] == "artifact_revised"
    # ...and so does a material change to the environment profile.
    save_profile(client, "vdb", role="JADEDISC2")
    assert _baselines(client)[0]["reassessment"][-1]["kind"] == "environment_profile_changed"


def test_refresh_evidence_is_authorised_and_needs_active_discovery(client, monkeypatch, viewer_client):
    ready_company(client, "vdb")
    _approved_story(STORY)
    _run(monkeypatch, lambda tools: (tools.read("udc_values", "00/DT"), {})[1])
    assert viewer_client.post(f"/changes/{STORY}/architecture-review/refresh-evidence",
                              headers=headers("vdb")).status_code == 403
    client.post("/admin/jde/disable", headers=headers("vdb"))
    r = client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb"))
    assert r.status_code == 409 and "not enabled" in r.json()["detail"]


def test_acceptance_8_downstream_agents_receive_the_same_manifest(client, monkeypatch):
    from jde_mcp_server.design_baseline import DesignBaselineUnavailable, get_design_baseline

    ready_company(client, "vdb")
    _approved_story(STORY)
    _run(monkeypatch, lambda tools: (tools.read("udc_values", "00/DT"), {})[1])
    current = _baselines(client)[0]
    package = get_design_baseline(STORY)  # what the Functional Agent's MCP tool returns
    assert package["manifest_sha256"] == current["manifestSha256"]
    assert package["evidence_manifest"]["observations"] == current["manifest"]["observations"]
    assert package["implementation_spec"]["sequence"] == ["set PDOCTYPE"]
    assert "does not authorise any write" in package["note"]
    handoff = client.get(f"/changes/{STORY}/architecture-review/handoff", headers=headers("vdb")).json()
    assert handoff["manifestSha256"] == current["manifestSha256"]
    # A flag reaches the downstream copy too.
    save_profile(client, "vdb", role="JADEDISC2")
    assert get_design_baseline(STORY)["status"] == "needs_reassessment"
    # A tampered copy is refused.
    import os

    path = os.path.join(os.environ["JDE_DESIGN_BASELINE_DIR"], f"{STORY}.json")
    doc = json.load(open(path))
    doc["evidence_manifest"]["observations"] = []
    json.dump(doc, open(path, "w"))
    with pytest.raises(DesignBaselineUnavailable, match="checksum"):
        get_design_baseline(STORY)


def test_the_hand_off_names_the_exact_change_this_design_proposed(client, monkeypatch):
    """The Functional Agent is given a change_id; the baseline it retrieves
    must say which change its design proposed, so a change from another
    design revision is recognisable."""
    from jde_mcp_server import approval
    from jde_mcp_server.design_baseline import get_design_baseline

    ready_company(client, "vdb")
    _approved_story(STORY)
    proposed = {}

    def script(tools):  # the Architect calls propose_change during its run
        tools.read("udc_values", "00/DT")
        proposed.update(approval.propose_change(STORY, {
            "tool": "set_processing_option", "story_id": STORY, "application": "P4210", "version": "CIQ0001",
            "option": "PCREDCHK", "value": "1"}, "processing_option_update"))
        return {}

    _run(monkeypatch, script)
    first = get_design_baseline(STORY)
    assert first["change"]["change_id"] == proposed["change_id"] and first["change"]["status"] == "pending"
    assert first["change"]["operation"]["option"] == "PCREDCHK"
    # A refresh of the same design keeps its change.
    client.post(f"/changes/{STORY}/architecture-review/refresh-evidence", headers=headers("vdb"))
    assert get_design_baseline(STORY)["change"]["change_id"] == proposed["change_id"]
    # A later design revision that proposes nothing is not bound to the old change.
    _run(monkeypatch, lambda tools: (tools.read("udc_values", "00/DT"), {})[1])
    later = get_design_baseline(STORY)
    assert later["design_revision"] == first["design_revision"] + 1
    assert later["change"] == {"change_id": None, "status": "none",
                               "detail": "this design revision proposed no executable change -- nothing to execute"}


def test_the_baseline_grants_nothing_to_execution(client, monkeypatch):
    """Execution never reads the discovery baseline: an approved change is
    still refused for exactly the reasons the gate always had."""
    import pathlib

    import jde_mcp_server

    ready_company(client, "vdb")
    root = pathlib.Path(jde_mcp_server.__file__).parent
    for name in ("ais_client.py", "approval.py", "execution.py", "scope.py"):
        assert "design_baseline" not in (root / name).read_text()


def test_artifacts_are_immutable_revisions_and_unsupported_formats_stay_unavailable(client, monkeypatch):
    from jde_api_service.discovery.architect_tools import ArchitectDiscoveryTools

    save_profile(client, "vdb")
    first = upload_artifact(client, "vdb")
    second = upload_artifact(client, "vdb", content="/* changed */")
    assert (first["revision"], second["revision"]) == (1, 2) and first["sha256"] != second["sha256"]
    par = upload_artifact(client, "vdb", objectName="P554210", objectType="APPL", exportFormat="par",
                          content=b"PK\x03\x04binary", fileName="P554210.par")
    assert par["extractionStatus"] == "unsupported"
    listing = client.get("/admin/jde/artifacts", headers=headers("vdb")).json()
    assert {(a["artifactId"], a["revision"], a["latest"]) for a in listing if a["artifactId"] == first["artifactId"]} == {
        (first["artifactId"], 1, False), (first["artifactId"], 2, True)}
    tools = ArchitectDiscoveryTools(company_id="vdb", story_id="S-X", domain_id=None, grant=None, no_grant_reason="n/a")
    out = tools.read_artifact(par["artifactId"])
    assert out["available"] is False and "not analysed" in out["reason"]
    assert any(u["ref"].startswith(par["artifactId"]) for u in tools.ledger.artifacts_unavailable)
    # Revision 1 stays readable exactly as uploaded.
    assert tools.read_artifact(f"{first['artifactId']}@r1")["metadata"]["sha256"] == first["sha256"]


def test_domain_scoped_artifacts_are_only_visible_to_their_domain(client):
    from jde_api_service.discovery.architect_tools import ArchitectDiscoveryTools

    save_profile(client, "bwm")
    scoped = upload_artifact(client, "bwm", objectName="B55WH01", domainId="DOM-BWM-WAREHOUSE")
    other = ArchitectDiscoveryTools(company_id="bwm", story_id="S-X", domain_id="DOM-BWM-CREDIT", grant=None)
    same = ArchitectDiscoveryTools(company_id="bwm", story_id="S-X", domain_id="DOM-BWM-WAREHOUSE", grant=None)
    assert other.read_artifact(scoped["artifactId"])["available"] is False
    assert scoped["artifactId"] in {a["artifact_id"] for a in same.list_artifacts()["artifacts"]}
    r = client.post("/admin/jde/artifacts", headers=headers("vdb"), json={
        "kind": "technical_export", "domainId": "DOM-BWM-WAREHOUSE", "objectName": "X", "objectType": "BSFN",
        "exportFormat": "text", "exportedAt": "2026-09-20T10:00:00+00:00", "fileName": "x.txt", "contentBase64": "eA=="})
    assert r.status_code == 422  # another company's domain


def test_metadata_only_withholds_artifact_content_from_the_model(client, monkeypatch):
    ready_company(client, "vdb", dataSharingPolicy="metadata_only")
    upload_artifact(client, "vdb")
    _approved_story(STORY)
    seen = {}

    def script(tools):
        seen["artifact"] = tools.read_artifact(tools.list_artifacts()["artifacts"][0]["evidence_id"])
        seen["caps"] = tools.list_capabilities()
        return {}

    _run(monkeypatch, script)
    assert seen["artifact"]["available"] is False and "content" not in seen["artifact"]
    assert "redacted" in seen["caps"]["data_sharing_note"]
