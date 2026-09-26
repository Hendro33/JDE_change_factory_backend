"""Process framework -> story finalisation -> process maps -> as-built record.

Uses the SYNTHETIC framework fixture (fixtures/process_framework, SYN- ids,
not APQC content) and the synthetic Technical workflow fixtures; approver
identities are synthetic test users."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import claude_agent_sdk as sdk
import pytest

from . import _technical as t
from .conftest import TEST_PASSWORD, _apply_csrf_header, headers

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "..", "fixtures", "process_framework")
STORY = "S-PROC-1"


def _file(version: int) -> bytes:
    with open(os.path.join(FIXTURES, f"SYNTHETIC_bicycleworks_process_framework_v{version}.xlsx"), "rb") as f:
        return f.read()


def _draft(client, version: int, company: str = "vdb", framework_id: str | None = None, data: bytes | None = None):
    data = data if data is not None else _file(version)
    b64 = base64.b64encode(data).decode()
    inspected = client.post("/process/frameworks/inspect", headers=headers(company),
                            json={"fileName": "fw.xlsx", "contentBase64": b64})
    assert inspected.status_code == 200, inspected.text
    sheet = inspected.json()["sheets"][0]
    return client.post("/process/frameworks/drafts", headers=headers(company), json={
        "name": "SYNTHETIC BicycleWorks framework", "sourceKind": "synthetic_fixture", "sourceStatement": "",
        "fileName": f"fw_v{version}.xlsx", "contentBase64": b64, "sheet": sheet["sheet"],
        "mapping": sheet["proposed_mapping"], "deriveParent": False, "frameworkId": framework_id})


def _import(client, version: int, company: str = "vdb", framework_id: str | None = None) -> dict:
    r = _draft(client, version, company, framework_id)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["version"]["validation"]["errors"] == []
    a = client.post(f"/process/frameworks/{d['framework']['framework_id']}/versions/{d['version']['version']}/activate",
                    headers=headers(company))
    assert a.status_code == 200, a.text
    return a.json()


def _to_be(fid: str, version: int = 1, **overrides) -> dict:
    steps = [
        {"id": "S1", "label": "Dealer requests a return", "type": "start", "actor": "Dealer", "basis": "confirmed",
         "confirmation_source": "workshop with the returns lead (synthetic)"},
        {"id": "S2", "label": "Issue return authorisation", "type": "task", "actor": "Customer service",
         "system": "JDE", "controls": ["no return without an authorisation number"],
         "node_ref": {"framework_id": fid, "version": version, "node_key": "SYN-5.1.3"}, "basis": "assumption"},
        {"id": "S3", "label": "Resaleable?", "type": "decision", "actor": "Warehouse", "basis": "assumption",
         "node_ref": {"framework_id": fid, "version": version, "node_key": "SYN-5.2.2"}},
        {"id": "S4", "label": "Return to stock", "type": "end", "basis": "assumption"},
        {"id": "S5", "label": "Repair or scrap", "type": "end", "basis": "assumption"},
    ]
    content = {"title": "Dealer returns (to-be)", "steps": steps,
               "connections": [{"from": "S1", "to": "S2"}, {"from": "S2", "to": "S3"},
                               {"from": "S3", "to": "S4", "label": "yes"}, {"from": "S3", "to": "S5", "label": "no"}]}
    content.update(overrides)
    return content


def _confirm(client, fid: str, version: int = 1, expected: int = 0, keys=("SYN-5.1.3", "SYN-5.2.2")):
    return client.post(f"/changes/{STORY}/process/mapping", headers=headers(), json={
        "status": "confirmed", "expectedRevision": expected, "note": "synthetic reviewer",
        "refs": [{"framework_id": fid, "version": version, "node_key": k, "rationale": "affected"} for k in keys],
        "findings": {"accepted_controls": ["no return without authorisation"]}})


def _baseline():
    from jde_api_service.discovery import baseline

    return baseline.current_for_story("vdb", STORY)


# ---------------------------------------------------------------------
def test_template_import_preview_activation_and_provenance(client):
    tpl = client.get("/process/template", headers=headers())
    assert tpl.status_code == 200 and tpl.content[:2] == b"PK"
    insp = client.post("/process/frameworks/inspect", headers=headers(),
                       json={"fileName": "t.xlsx", "contentBase64": base64.b64encode(tpl.content).decode()}).json()
    assert insp["sheets"][0]["proposed_mapping"]["node_key"] == "Node ID"

    r = _draft(client, 1)
    assert r.status_code == 200, r.text
    d = r.json()
    fid = d["framework"]["framework_id"]
    assert d["version"]["status"] == "draft" and d["version"]["node_count"] == 26
    assert any("SYNTHETIC" in w for w in d["version"]["validation"]["warnings"])
    assert d["version"]["file_sha256"] == hashlib.sha256(_file(1)).hexdigest()
    node = next(n for n in d["nodes"] if n["node_key"] == "SYN-5.2.2")
    assert node["parent_key"] == "SYN-5.2" and node["level"] == 3

    # Nothing can reference a draft.
    assert client.get("/process/frameworks", headers=headers()).json()["frameworks"][0]["active_version"] is None
    a = client.post(f"/process/frameworks/{fid}/versions/1/activate", headers=headers())
    assert a.status_code == 200, a.text
    listing = client.get("/process/frameworks", headers=headers()).json()
    assert listing["frameworks"][0]["active_version"] == 1
    assert listing["settings"]["selected_framework_id"] == fid
    original = client.get(f"/process/frameworks/{fid}/versions/1/file", headers=headers())
    assert hashlib.sha256(original.content).hexdigest() == hashlib.sha256(_file(1)).hexdigest()
    again = client.post(f"/process/frameworks/{fid}/versions/1/activate", headers=headers())
    assert again.status_code == 422  # an activated version is immutable


def test_validation_errors_block_activation(client):
    from openpyxl import Workbook
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Framework"
    ws.append(["Node ID", "Parent ID", "Name"])
    ws.append(["A", "", "Top"])
    ws.append(["A", "", "Duplicate"])
    ws.append(["B", "MISSING", "Orphan"])
    ws.append(["C", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    r = _draft(client, 1, data=buf.getvalue())
    assert r.status_code == 200, r.text
    errors = r.json()["version"]["validation"]["errors"]
    assert any("duplicates" in e for e in errors) and any("MISSING" in e for e in errors) and any("Name is empty" in e for e in errors)
    fid = r.json()["framework"]["framework_id"]
    refused = client.post(f"/process/frameworks/{fid}/versions/1/activate", headers=headers())
    assert refused.status_code == 422 and "validation error" in refused.json()["detail"]
    bad = client.post("/process/frameworks/inspect", headers=headers(),
                      json={"fileName": "x.xlsx", "contentBase64": base64.b64encode(b"not a workbook").decode()})
    assert bad.status_code == 422


def test_admin_only_and_company_isolation(client, ellen_client, viewer_client):
    d = _import(client, 1, company="nhd")
    fid = d["framework"]["framework_id"]
    # Ellen is not entitled to nhd; the viewer is not an admin.
    assert ellen_client.get("/process/frameworks", headers=headers("nhd")).status_code == 403
    assert ellen_client.get("/process/frameworks", headers=headers("vdb")).json()["frameworks"] == []
    assert viewer_client.post("/process/frameworks/inspect", headers=headers("vdb"),
                              json={"fileName": "x.xlsx", "contentBase64": ""}).status_code == 403
    # Another company's framework cannot be referenced from a vdb story.
    t._approved_story(STORY, "vdb")
    r = _confirm(client, fid)
    assert r.status_code == 422 and "not an activated version of this company" in r.json()["detail"]
    assert client.get(f"/process/frameworks/{fid}/versions/1", headers=headers("vdb")).status_code == 404


def test_reviewer_authority_on_mapping(client):
    from jde_api_service.services import auth_service, membership_service
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    auth_service.create_user("do@test.local", TEST_PASSWORD, "Dora Domain", user_id="u-do")
    membership_service.create_membership("u-do", "vdb", ["domain_owner"], created_by="u-hendro")
    do = TestClient(app)
    assert do.post("/auth/login", json={"email": "do@test.local", "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(do)
    refused = do.post(f"/changes/{STORY}/process/mapping", headers=headers(), json={
        "status": "no_mapping", "noMappingReason": "a purely technical change", "expectedRevision": 0})
    assert refused.status_code == 403  # a Domain Owner not assigned to the story's domain
    short = client.post(f"/changes/{STORY}/process/mapping", headers=headers(),
                        json={"status": "no_mapping", "noMappingReason": "n/a", "expectedRevision": 0})
    assert short.status_code == 422
    ok = _confirm(client, fid)
    assert ok.status_code == 200, ok.text
    ref = ok.json()["mapping"]["refs"][0]
    assert (ref["framework_id"], ref["version"], ref["node_key"]) == (fid, 1, "SYN-5.1.3") and len(ref["node_sha256"]) == 64
    assert [p["node_key"] for p in ref["path"]] == ["SYN-5", "SYN-5.1", "SYN-5.1.3"]
    assert _confirm(client, fid, expected=0).status_code == 409  # a stale review never overwrites


def _design_with_process(client, monkeypatch):
    """After mapping and maps: the (scripted) Architect, which calls
    get_process_context through its company-scoped tools, and a design
    approval by a synthetic user. The story already exists."""
    from jde_api_service.services import architecture_driver

    t.ready_company(client, "vdb", approvedReads=t.approved_reads("vdb"))
    t._save_scope(client, "vdb", t.technical_scope())
    t.seed_object("vdb")
    art = t.upload_source(client, "vdb")
    real = architecture_driver.build_discovery_tools
    seen = {}

    def spy(*a, **k):
        tools = real(*a, **k)
        seen["context"] = tools.process_context()
        return tools

    architecture_driver.build_discovery_tools = spy
    try:
        t.technical_design(client, monkeypatch, STORY, company="vdb", artifact=art)
    finally:
        architecture_driver.build_discovery_tools = real
    design = client.get(f"/changes/{STORY}/technical", headers=headers()).json()["assignment"]
    r = client.post(f"/changes/{STORY}/technical/approve-design", headers=headers(),
                    json={"designRevision": design["design_revision"], "note": "synthetic test approval"})
    assert r.status_code == 200, r.text
    return seen["context"]


def test_architect_receives_process_context_and_changes_flag_the_design(client, monkeypatch):
    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    assert _confirm(client, fid).status_code == 200
    r = client.put(f"/changes/{STORY}/process/maps/to_be", headers=headers(),
                   json={"content": _to_be(fid), "note": "first draft", "expectedVersion": 0})
    assert r.status_code == 200, r.text
    ctx = _design_with_process(client, monkeypatch)
    assert ctx["mapping"]["processes"][0]["ref"] == f"{fid}@v1:SYN-5.1.3"
    assert ctx["maps"]["to_be"]["steps"][1]["basis"] == "assumption"
    b = _baseline()
    assert b["status"] == "current" and b["manifest"]["process_context"]["consulted"] is True

    # A wording-only map edit does not flag the design.
    r = client.put(f"/changes/{STORY}/process/maps/to_be", headers=headers(),
                   json={"content": _to_be(fid, title="Dealer returns (to-be, reworded)"), "expectedVersion": 1})
    assert r.status_code == 200 and r.json()["saved"]["material_change"] is False
    assert _baseline()["status"] == "current"
    # A confirmed step must say where the confirmation came from.
    bad = _to_be(fid)
    bad["steps"][1].update(basis="confirmed", confirmation_source="")
    assert client.put(f"/changes/{STORY}/process/maps/to_be", headers=headers(),
                      json={"content": bad, "expectedVersion": 2}).status_code == 422
    # A material change flags it.
    changed = _to_be(fid)
    changed["steps"][1]["controls"].append("dealer credit only after inspection")
    r = client.put(f"/changes/{STORY}/process/maps/to_be", headers=headers(),
                   json={"content": changed, "expectedVersion": 2})
    assert r.status_code == 200 and r.json()["saved"]["design_flagged"] is True
    b = _baseline()
    assert b["status"] == "needs_reassessment" and b["reassessment"][-1]["kind"] == "process_map_changed"


def test_framework_update_preserves_references_and_flags_the_design(client, monkeypatch):
    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    assert _confirm(client, fid).status_code == 200
    _design_with_process(client, monkeypatch)
    assert _baseline()["status"] == "current"

    v2 = _import(client, 2, framework_id=fid)
    assert v2["version"]["changes"]["changed"] == ["SYN-5.2.2"] and v2["version"]["changes"]["removed"] == ["SYN-1.1"]
    assert v2["affected_stories"][0]["story_id"] == STORY
    view = client.get(f"/changes/{STORY}/process", headers=headers()).json()
    refs = {r["node_key"]: r for r in view["mapping"]["refs"]}
    assert refs["SYN-5.2.2"]["version"] == 1 and refs["SYN-5.2.2"]["status_now"]["state"] == "changed"
    assert refs["SYN-5.1.3"]["status_now"]["state"] == "unchanged"
    # Version 1 stays resolvable, exactly as referenced.
    v1 = client.get(f"/process/frameworks/{fid}/versions/1", headers=headers()).json()
    assert v1["version"]["status"] == "superseded"
    assert next(n for n in v1["nodes"] if n["node_key"] == "SYN-5.2.2")["node_sha256"] == refs["SYN-5.2.2"]["node_sha256"]
    b = _baseline()
    assert b["status"] == "needs_reassessment" and b["reassessment"][-1]["kind"] == "process_framework_revised"
    stories = client.get(f"/process/frameworks/{fid}/nodes/SYN-5.2.2/stories", headers=headers()).json()
    assert stories == [{"story_id": STORY, "revision": 1, "versions": [1]}]


def test_as_built_finalises_only_after_every_checkpoint(client, monkeypatch):
    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    assert _confirm(client, fid).status_code == 200
    assert client.put(f"/changes/{STORY}/process/maps/to_be", headers=headers(),
                      json={"content": _to_be(fid), "expectedVersion": 0}).status_code == 200
    _design_with_process(client, monkeypatch)

    early = client.post(f"/changes/{STORY}/as-built", headers=headers()).json()
    assert early["status"] == "draft" and not early["content"]["all_checkpoints_complete"]
    refused = client.post(f"/changes/{STORY}/as-built/{early['version']}/finalise", headers=headers())
    assert refused.status_code == 422 and "checkpoints are not complete" in refused.json()["detail"]

    t.prepare("vdb", STORY)
    t.approve_package(client, STORY, 1)
    assert t.milestone(client, STORY, 1, "apply").status_code == 200
    assert t.milestone(client, STORY, 1, "build").status_code == 200
    assert t.cnc(client, STORY, 1).status_code == 200
    v = t.milestone(client, STORY, 1, "verify")
    assert v.status_code == 200, v.text

    # The earlier draft is stale: its sources changed.
    stale = client.post(f"/changes/{STORY}/as-built/{early['version']}/finalise", headers=headers())
    assert stale.status_code == 422
    rec = client.post(f"/changes/{STORY}/as-built", headers=headers()).json()
    assert rec["content"]["all_checkpoints_complete"], rec["content"]["checkpoints"]
    assert rec["delivery_mode"] == "simulation" and rec["content"]["simulated_notice"].startswith("SIMULATED DELIVERY")
    assert any("assumption" in x for x in rec["content"]["limitations"])
    final = client.post(f"/changes/{STORY}/as-built/{rec['version']}/finalise", headers=headers())
    assert final.status_code == 200, final.text
    md = client.get(f"/changes/{STORY}/as-built/{rec['version']}/markdown", headers=headers()).text
    assert "status **FINAL**" in md and "SIMULATED DELIVERY" in md and "```mermaid" in md and "SYN-5.1.3" in md
    assert client.post(f"/changes/{STORY}/as-built/{rec['version']}/finalise", headers=headers()).status_code == 422


def test_everything_survives_a_restart(client):
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    assert _confirm(client, fid).status_code == 200
    assert client.put(f"/changes/{STORY}/process/maps/as_is", headers=headers(),
                      json={"content": _to_be(fid), "expectedVersion": 0}).status_code == 200
    with TestClient(app) as fresh:  # a new process start: startup, migrations, recovery
        assert fresh.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD}).status_code == 200
        view = fresh.get(f"/changes/{STORY}/process", headers=headers()).json()
    assert view["framework"]["framework_id"] == fid and view["mapping"]["revision"] == 1
    assert view["maps"]["as_is"]["versions"][0]["version"] == 1


def test_process_analysis_agent_suggestions_are_validated(client):
    from jde_api_service.process import agent, story as story_process
    from jde_api_service.services.registry import get_change_service

    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    run = story_process.start_analysis("vdb", STORY, initiated_by="u-hendro")
    captured = {}

    async def scripted(prompt, options):  # deterministic stand-in for the model, using the run's own tools
        captured["allowed"] = options.allowed_tools
        server = options.mcp_servers[agent.SERVER_NAME]["instance"]
        del server
        tools = captured["tools"]
        tools.search("return authorisation")
        tools.submit({"suggested_processes": [{"node_key": "SYN-5.1.3", "rationale": "return number", "confidence": "high"},
                                              {"node_key": "4.4.3", "rationale": "invented"}],
                      "missing_controls": ["credit only after inspection"]})
        yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                session_id="t", result="done")

    real_init = agent.ProcessAnalysisTools.__init__

    def init(self, **kw):
        real_init(self, **kw)
        captured["tools"] = self

    agent.ProcessAnalysisTools.__init__ = init
    try:
        change = get_change_service().get_for_customer(STORY, "vdb")
        asyncio.run(agent.run_process_analysis(company_id="vdb", story_id=STORY, run_id=run["run_id"], change=change,
                                               repo_root=".", observer=scripted))
    finally:
        agent.ProcessAnalysisTools.__init__ = real_init
    done = story_process.get_run(run["run_id"])
    assert done["status"] == "completed", done
    assert [s["node_key"] for s in done["result"]["suggested_processes"]] == ["SYN-5.1.3"]
    assert "4.4.3" in done["result"]["rejected_suggestions"][0]
    # No subagent in this run, so no Task tool: exactly the process tools.
    assert set(captured["allowed"]) == set(agent.ALLOWED_TOOLS)
    assert json.dumps(done["result"]).count(fid) >= 1


def test_functional_route_as_built_records_the_actual_change_and_refuses_incomplete_or_stale(client, monkeypatch):
    """A bounded SIMULATED Functional delivery: processing-option change through
    the existing gate, into a finalised as-built record."""
    from jde_mcp_server.ais_client import client as ais

    from ._discovery import ready_company
    from jde_mcp_server import approval

    from .test_architect_discovery import _run
    from .test_stage1_execution_safeguards import OPERATION, _approve, _approved_story, _execute, _full_scope, _save_scope

    story = "S-PROC-FUNC"
    ready_company(client)
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story)
    fid = _import(client, 1)["framework"]["framework_id"]
    r = client.post(f"/changes/{story}/process/mapping", headers=headers(), json={
        "status": "confirmed", "expectedRevision": 0,
        "refs": [{"framework_id": fid, "version": 1, "node_key": "SYN-3.2", "rationale": "order entry default"}]})
    assert r.status_code == 200, r.text
    assert client.put(f"/changes/{story}/process/maps/to_be", headers=headers(),
                      json={"content": _to_be(fid), "expectedVersion": 0}).status_code == 200
    change = {}

    def architect(tools):  # scripted stand-in: reads the target, consults the process context, proposes
        tools.read("processing_option_values", "P4210|CIQ0001")
        tools.process_context()
        change.update(approval.propose_change(story, {**OPERATION, "story_id": story, "test_orchestration": "ORCH_SO"},
                                              "processing_option_update"))
        return {}

    _run(monkeypatch, architect, story=story)

    def record():
        return client.post(f"/changes/{story}/as-built", headers=headers()).json()

    def cps(rec):
        return {c["id"]: c["complete"] for c in rec["content"]["checkpoints"]}

    pending = record()
    assert cps(pending)["design_approved"] is False and cps(pending)["applied"] is False
    assert client.post(f"/changes/{story}/as-built/{pending['version']}/finalise", headers=headers()).status_code == 422

    _approve(change["change_id"])
    _execute(story, change["change_id"])
    applied_only = record()
    assert cps(applied_only)["applied"] and not cps(applied_only)["tested"]
    assert client.post(f"/changes/{story}/as-built/{applied_only['version']}/finalise", headers=headers()).status_code == 422

    ais.run_orchestration(story, change["change_id"], "ORCH_SO", {})
    done = record()
    assert done["content"]["all_checkpoints_complete"], done["content"]["checkpoints"]
    f = done["content"]["implementation"]["functional"]
    assert (f["operation"]["option"], f["operation"]["value"], f["binding"]["before_state"]["value"]) == ("PDOCTYPE", "SO", "S3")
    assert f["readback"] == {"value": "SO", "matches_approved": True,
                             "source": "read-back from the simulated DEV estate (SIMULATION)"}
    assert f["attempts"]["write"][0]["outcome"] == "applied" and f["attempts"]["test"][0]["outcome"] == "completed"
    assert done["content"]["process"]["mapping"]["refs"][0]["node_key"] == "SYN-3.2"
    assert done["content"]["process"]["maps"]["to_be"]["version"] == 1
    assert done["content"]["design"]["baseline"]["process_context"]["mapping_revision"] == 1
    assert any("SIMULATION STUB" in x for x in done["content"]["limitations"])
    assert [c["label"] for c in done["content"]["checkpoints"] if c["id"] == "tested"][0].endswith("not behavioural evidence)")

    # Stale: the to-be map changes after generation -> the draft cannot be finalised.
    changed = _to_be(fid)
    changed["steps"][1]["controls"].append("second check")
    assert client.put(f"/changes/{story}/process/maps/to_be", headers=headers(),
                      json={"content": changed, "expectedVersion": 1}).status_code == 200
    stale = client.post(f"/changes/{story}/as-built/{done['version']}/finalise", headers=headers())
    assert stale.status_code == 422 and "changed since this draft" in stale.json()["detail"]
    # ...and the map change flagged the design, so a new draft is incomplete too.
    again = record()
    assert cps(again)["design_current"] is False
    assert client.post(f"/changes/{story}/as-built/{again['version']}/finalise", headers=headers()).status_code == 422
    md = client.get(f"/changes/{story}/as-built/{again['version']}/markdown", headers=headers()).text
    assert "SIMULATED DELIVERY" in md and "'S3' -> 'SO'" in md and "Read-back of the target: 'SO'" in md


def test_accepted_findings_become_a_reviewed_story_revision(client, monkeypatch, viewer_client):
    """Findings are only proposals; a reviewer applies selected ones as a new,
    attributed story revision -- once -- and dependent work is flagged."""
    from jde_mcp_server import approval, backlog

    from jde_api_service.process import story as story_process

    fid = _import(client, 1)["framework"]["framework_id"]
    t._approved_story(STORY, "vdb")
    run = story_process.start_analysis("vdb", STORY, initiated_by="u-hendro", scripted=True)
    story_process.finish_analysis(run["run_id"], status="completed", result=story_process.normalise_findings("vdb", fid, 1, {
        "missing_requirements": ["State how long a return authorisation stays valid"],
        "missing_controls": ["No dealer credit before inspection"],
        "missing_acceptance_criteria": ["An expired authorisation is refused", "Credit equals the value adjustment"]}))
    assert _confirm(client, fid).status_code == 200
    _design_with_process(client, monkeypatch)
    t.prepare("vdb", STORY)  # pending work that rests on the current story text
    base = f"/changes/{STORY}/process/refinement"

    view = client.get(base, headers=headers()).json()
    by_text = {f["text"]: f for f in view["findings"]}
    assert len(by_text) == 4 and {f["status"] for f in view["findings"]} == {"proposed"} and view["current_revision"] == 0
    chosen = [by_text["No dealer credit before inspection"]["finding_id"], by_text["An expired authorisation is refused"]["finding_id"]]

    diff = client.post(f"{base}/preview", headers=headers(), json={"findingIds": chosen}).json()["diff"]
    added = [d["line"] for d in diff if d["op"] == "+"]
    assert added == ["Control: No dealer credit before inspection", "AC1: An expired authorisation is refused"]
    assert viewer_client.post(f"{base}/apply", headers=headers(), json={"findingIds": chosen, "expectedRevision": 0}).status_code == 403

    r = client.post(f"{base}/apply", headers=headers(), json={"findingIds": chosen, "note": "reviewed", "expectedRevision": 0})
    assert r.status_code == 200, r.text
    view = r.json()
    rev = view["revisions"][0]
    assert (rev["revision"], rev["author_name"], rev["source"]) == (2, "Hendro", "process_refinement")
    assert view["revisions"][1]["source"] == "approved_story"  # the approved text is kept as revision 1
    assert {a["finding_id"] for a in rev["applied_findings"]} == set(chosen)
    assert rev["process_refs"]["mapping_revision"] == 1 and rev["process_refs"]["refs"][0]["node_key"] == "SYN-5.1.3"
    assert {f["finding_id"]: f["applied_in_revision"] for f in view["findings"] if f["status"] == "applied"} == {c: 2 for c in chosen}

    # Once only, and never over a newer revision.
    again = client.post(f"{base}/apply", headers=headers(), json={"findingIds": chosen[:1], "expectedRevision": 2})
    assert again.status_code == 422 and "applied at most once" in again.json()["detail"]
    other = by_text["Credit equals the value adjustment"]["finding_id"]
    stale = client.post(f"{base}/apply", headers=headers(), json={"findingIds": [other], "expectedRevision": 0})
    assert stale.status_code == 409
    # Rejected and deferred findings keep their status and reason.
    req = by_text["State how long a return authorisation stays valid"]["finding_id"]
    client.post(f"{base}/findings/{req}", headers=headers(), json={"status": "deferred", "reason": "ask the customer first"})
    client.post(f"{base}/findings/{other}", headers=headers(), json={"status": "rejected", "reason": "covered by finance"})
    statuses = {f["finding_id"]: (f["status"], f["reason"]) for f in client.get(base, headers=headers()).json()["findings"]}
    assert statuses[req] == ("deferred", "ask the customer first") and statuses[other] == ("rejected", "covered by finance")

    # The story everyone reads is the new revision; the Architect's copy too.
    change = client.get(f"/changes/{STORY}", headers=headers()).json()
    assert "Control: No dealer credit before inspection" in change["userStory"]["businessRules"]
    assert [a["text"] for a in change["userStory"]["acceptanceCriteria"]][-1] == "An expired authorisation is refused"
    record = backlog.get_approved_story(STORY)
    assert "AC1: An expired authorisation is refused" in record["user_story"] and record["story_revisions"][0]["revision"] == 2
    # Design flagged; pending work invalidated.
    assert _baseline()["reassessment"][-1]["kind"] == "story_revised"
    from jde_api_service.technical import store

    pkg = store.get_package("vdb", STORY)
    assert approval._load(pkg["change_id"])["invalidations"][-1]["kind"] == "story_revised"
