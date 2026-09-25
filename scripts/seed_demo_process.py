#!/usr/bin/env python3
"""
Demonstration only: the BicycleWorks dealer-returns journey for the local
preview -- process framework -> story finalisation -> process maps ->
design -> simulated implementation -- WITHOUT a model run.

  * The process framework is the SYNTHETIC fixture
    (fixtures/process_framework/..._v1.xlsx): SYN- ids, not APQC content.
  * SCRIPTED STAND-INS (clearly not the model) play the refinement process
    analysis, the Architect and the Technical Agent through the same
    governed code the real agents use.
  * Decisions are made by throwaway demo identities (E2E Admin, the demo
    CNC operator and a demo Domain Owner) -- never a real person.
  * Implementation and verification happen in the SIMULATED DEV estate
    against a SYNTHETIC object (P55RET01). Nothing touches any JDE.

The as-built record is left for the person trying the preview to generate
and finalise. A second story (stock write-off) is left unmapped so the
mapping and map editor can be tried from scratch.

    python3 scripts/seed_demo_process.py        # company bwm; idempotent per data directory

Passwords for the demo users come from JADE_E2E_CNC_PASSWORD and
JADE_E2E_DO_PASSWORD (throwaway, local only).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api_service"))

from jde_api_service.config import settings  # noqa: E402

for name, sub in (("JDE_COMPANY_SCOPE_DIR", "engagement_scope"), ("JDE_STORY_COMPANY_DIR", "customer_links"),
                  ("JDE_DESIGN_BASELINE_DIR", "design_baselines"), ("JDE_SIM_ESTATE_DIR", "sim_estate")):
    os.environ.setdefault(name, os.path.abspath(os.path.join(settings.data_dir, sub)))
os.environ.setdefault("JDE_AUTH_DB_PATH", os.path.abspath(os.path.join(settings.data_dir, "jde.sqlite3")))

import claude_agent_sdk as sdk  # noqa: E402

from jde_api_service.discovery import architect_tools, artifacts, profile_service  # noqa: E402
from jde_api_service.discovery.models import ArtifactUpload, JdeProfileConfig  # noqa: E402
from jde_api_service.models.change import UserStory  # noqa: E402
from jde_api_service.models.engagement_scope import EngagementScopeUpdate  # noqa: E402
from jde_api_service.persistence.db import connection  # noqa: E402
from jde_api_service.process import framework, maps, story as story_process  # noqa: E402
from jde_api_service.services import architecture_driver, auth_service, membership_service  # noqa: E402
from jde_api_service.services.registry import (  # noqa: E402
    get_architecture_review_service, get_customer_link_service, get_delivery_queue_service,
    get_domain_review_service, get_engagement_scope_service,
)
from jde_api_service.technical import service, store  # noqa: E402
from jde_api_service.technical.tools import TechnicalAgentTools  # noqa: E402
from jde_mcp_server import backlog, technical_sim  # noqa: E402

COMPANY = "bwm"
STORY, STORY2 = "S-BW-RETURNS", "S-BW-WRITEOFF"
DOMAIN = "DOM-BWM-CUST-SERVICE"
FIXTURE = os.path.join(_ROOT, "fixtures", "process_framework", "SYNTHETIC_bicycleworks_process_framework_v1.xlsx")

if backlog._load(STORY) is not None:  # noqa: SLF001 -- idempotence check only
    print(json.dumps({"story": STORY, "seeded": "already"}))
    sys.exit(0)

admin = auth_service.get_user_by_email("admin@e2e.local")
admin_id = admin.id
roles = set(membership_service.roles_for(admin_id, COMPANY))
ADMIN = "E2E Admin"

# -- demo users -----------------------------------------------------------
for email, name, uid, role, env in (("cnc@e2e.local", "E2E CNC Operator", "u-e2e-cnc", "cnc_operator", "JADE_E2E_CNC_PASSWORD"),
                                   ("do@e2e.local", "E2E Domain Owner (returns)", "u-e2e-do", "domain_owner", "JADE_E2E_DO_PASSWORD")):
    pw = os.environ.get(env)
    if pw and auth_service.get_user_by_email(email) is None:
        auth_service.create_user(email, pw, name, user_id=uid)
        membership_service.create_membership(uid, COMPANY, [role], created_by=admin_id)
if auth_service.get_user_by_email("do@e2e.local"):
    with connection() as conn:
        mid = conn.execute("SELECT id FROM company_memberships WHERE user_id = 'u-e2e-do' AND company_id = ?",
                           (COMPANY,)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO domain_assignments (membership_id, business_domain_id) VALUES (?, ?)",
                     (mid, DOMAIN))

# -- scope and a simulation discovery profile ---------------------------------
scopes = get_engagement_scope_service()
current = scopes.get_for_customer(COMPANY)
base = current.model_dump(mode="json") if current else {}
env = ((base.get("environment") or {}).get("dev_environment_id")) or "JDV920"
scopes.upsert(COMPANY, EngagementScopeUpdate.model_validate({
    **{k: v for k, v in base.items() if k in EngagementScopeUpdate.model_fields},
    "environment": {**(base.get("environment") or {}), "dev_environment_id": env,
                    "dev_path_code": (base.get("environment") or {}).get("dev_path_code") or "DV920",
                    "isolation_confirmed": True, "isolation_evidence": "SIMULATION demo"},
    "approval_policy": base.get("approval_policy") or {"policy_version": 1,
                                                       "exact_change_approver_roles": ["product_manager"],
                                                       "approval_valid_hours": 24},
    "technical_agent": {"authorized_object_types": ["ER"], "reserved_product_code": "55"},
    "expected_revision": current.revision if current else None,
}), actor="demo seed")
if profile_service.load(COMPANY) is None:
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    profile_service.save(COMPANY, JdeProfileConfig.model_validate({
        "connection_mode": "simulation", "ais_base_url": "https://ais-dev.synthetic.invalid", "environment": env,
        "role": "JADEDISC", "expected_application_release": "9.2", "expected_tools_release": "9.2.8.2",
        "path_code": "DV920", "customer_contact": "Synthetic", "cnc_contact": "Synthetic",
        "network_route": "simulation only", "isolation_evidence": "SIMULATION", "routing_isolation_confirmed": True,
        "privilege_statement": "SIMULATION", "privilege_confirmed": True, "runtime_attestation_confirmed": True,
        "runtime_attestation_evidence": "SIMULATION",
        "approved_reads": [{"capability_id": "object_librarian", "targets": ["P55RET01"],
                            "fields": ["SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"]}],
        "discovery_window": {"starts_at": (day - timedelta(days=1)).isoformat(),
                             "ends_at": (day + timedelta(days=2)).isoformat()},
        "limits": {"max_records": 10, "timeout_seconds": 10}, "data_sharing_policy": "configuration_and_artifacts",
    }), expected_revision=None, actor="demo seed")

# -- 1. the SYNTHETIC framework, imported and activated like an admin would ----
if not framework.list_frameworks(COMPANY):
    with open(FIXTURE, "rb") as f:
        data = f.read()
    sheet = framework.inspect(data)["sheets"][0]
    draft = framework.create_draft(COMPANY, name="SYNTHETIC BicycleWorks process framework", source_kind="synthetic_fixture",
                                   source_statement="Demonstration fixture generated by scripts/make_process_fixtures.py",
                                   file_name=os.path.basename(FIXTURE), data=data, sheet=sheet["sheet"],
                                   mapping=sheet["proposed_mapping"], derive_parent=False, actor=ADMIN)
    framework.activate(COMPANY, draft["framework"]["framework_id"], draft["version"]["version"], actor=ADMIN)
FID = framework.selected_active(COMPANY)["framework"]["framework_id"]

# -- 2. the approved dealer-returns story (and an unmapped second story) ---------
RETURNS = """As the BicycleWorks returns coordinator, I want every dealer return to carry a return authorisation number and a recorded inspection outcome, so that returns are tracked from request through inspection to final disposition and Finance sees their value impact.

business_context: BicycleWorks wants a formal dealer returns process. Dealers request a return number before shipping a bicycle back. Warehouse staff inspect the returned bicycle and classify it as resaleable, repair required, damaged, warranty claim or scrap. Finance needs the value impact of the return; management wants monthly reporting by dealer, bicycle model and return reason.

acceptance_criteria:
- AC1: A returned bicycle cannot be processed without a return authorisation number verified_by: T1
- AC2: Inspection records exactly one classification: resaleable, repair required, damaged, warranty claim or scrap verified_by: T2
- AC3: The disposition follows the classification (stock, repair, warranty claim, scrap) verified_by: T2
- AC4: Monthly returns reporting by dealer, bicycle model and return reason is available

open_questions:
1. Which approval is required before a dealer credit is issued?
2. Is the inventory value impact posted at inspection or at final disposition?"""
WRITEOFF = """As a warehouse supervisor, I want to record stock write-offs with item, quantity and reason, with approval above a threshold, so that Finance can see what was written off and why.

business_context: Warehouse staff currently adjust inventory directly or email Finance about damaged and obsolete bicycles.

acceptance_criteria:
- AC1: A write-off records item, quantity and reason
- AC2: Write-offs above the threshold need approval before posting"""
reviews = get_domain_review_service()
for sid, text in ((STORY, RETURNS), (STORY2, WRITEOFF)):
    backlog.propose_to_backlog(sid, text, {"financial_impact": "Medium"}, "Medium", source="Business")
    backlog.approve(sid, "Demo seed", "demo data")
    get_customer_link_service().link(sid, COMPANY)
    get_delivery_queue_service().add(sid, COMPANY, "Demo seed", "queued")
    reviews.ensure(sid, UserStory(statement=text.split("\n")[0]))
    reviews.assign_domain(sid, business_domain_id=DOMAIN, uncertain=False, note="demo seed")

# -- 3. refinement process analysis: a SCRIPTED STAND-IN using the real validation ---
run = story_process.start_analysis(COMPANY, STORY, initiated_by="demo seed", scripted=True)
findings = story_process.normalise_findings(COMPANY, FID, 1, {
    "summary": "SCRIPTED STAND-IN (not a model run): the story touches return authorisation, inspection and "
               "disposition, the value adjustment and returns reporting.",
    "suggested_processes": [
        {"node_key": "SYN-5.1.3", "rationale": "dealers must request a return number first", "confidence": "high"},
        {"node_key": "SYN-5.2.2", "rationale": "warehouse inspects and classifies", "confidence": "high"},
        {"node_key": "SYN-5.2.3", "rationale": "classification drives the disposition", "confidence": "high"},
        {"node_key": "SYN-6.1.2", "rationale": "Finance needs the value impact", "confidence": "medium"},
        {"node_key": "SYN-6.2.1", "rationale": "monthly reporting by dealer, model and reason", "confidence": "medium"}],
    "missing_requirements": ["State how long a return authorisation number stays valid",
                             "State who may override a classification"],
    "missing_controls": ["No dealer credit before the inspection outcome is recorded",
                         "Segregation of duties: the person inspecting does not issue the credit"],
    "missing_acceptance_criteria": ["A return with an expired authorisation number is refused",
                                    "The value adjustment equals the credited amount"]})
story_process.finish_analysis(run["run_id"], status="completed", result={**findings, "scripted": True})

# -- 4. the reviewer's decision (throwaway demo identity) ---------------------------
story_process.decide(COMPANY, STORY, status="confirmed",
                     refs=[{"framework_id": FID, "version": 1, "node_key": s["node_key"], "rationale": s["rationale"]}
                           for s in findings["suggested_processes"] if s["node_key"] != "SYN-6.2.1"],
                     no_mapping_reason="", analysis_run_id=run["run_id"], findings={},
                     note="demo seed (throwaway identity)", reviewer_name=ADMIN, reviewer_user_id=admin_id,
                     roles=sorted(roles), expected_revision=0)

# -- 4b. story refinement: the demo admin applies two findings as a story revision;
#        the other findings stay proposed for the person trying the preview -----------
from jde_api_service.process import refinement  # noqa: E402
from jde_api_service.services.registry import get_change_service  # noqa: E402

refinement.sync_findings(COMPANY, STORY)
pick = {"No dealer credit before the inspection outcome is recorded", "A return with an expired authorisation number is refused"}
refinement.apply(COMPANY, STORY, get_change_service().get_for_customer(STORY, COMPANY).user_story,
                 [f["finding_id"] for f in refinement.findings(COMPANY, STORY) if f["text"] in pick],
                 expected_revision=0, note="demo seed (throwaway identity)", actor=ADMIN, actor_user_id=admin_id,
                 roles=sorted(roles))


def ref(key):
    return {"framework_id": FID, "version": 1, "node_key": key}


def links(sid):
    return get_customer_link_service().customer_for(sid) == COMPANY


# -- 5. as-is and to-be maps ------------------------------------------------------
maps.save(COMPANY, STORY, "as_is", {"title": "Dealer returns today", "steps": [
    {"id": "A1", "label": "Dealer emails customer service", "type": "start", "actor": "Dealer", "system": "E-mail",
     "basis": "confirmed", "confirmation_source": "demo seed: stated in request P001"},
    {"id": "A2", "label": "Bicycle arrives without a reference", "type": "task", "actor": "Warehouse", "basis": "assumption"},
    {"id": "A3", "label": "Ad-hoc check of the bicycle", "type": "task", "actor": "Warehouse", "basis": "assumption",
     "node_ref": ref("SYN-5.2.2")},
    {"id": "A4", "label": "Finance adjusts stock value by hand", "type": "end", "actor": "Finance", "system": "JDE",
     "basis": "assumption", "node_ref": ref("SYN-6.1.2")}],
    "connections": [{"from": "A1", "to": "A2"}, {"from": "A2", "to": "A3"}, {"from": "A3", "to": "A4"}]},
    note="demo seed", actor=ADMIN, actor_user_id=admin_id, expected_version=0, story_exists=links)
maps.save(COMPANY, STORY, "to_be", {"title": "Dealer returns (to-be)", "steps": [
    {"id": "T1", "label": "Dealer requests a return", "type": "start", "actor": "Dealer", "system": "Dealer portal",
     "basis": "confirmed", "confirmation_source": "demo seed: stated in request P001", "node_ref": ref("SYN-5.1.1")},
    {"id": "T2", "label": "Issue return authorisation number", "type": "task", "actor": "Customer service",
     "system": "JDE", "controls": ["No return accepted without an authorisation number"], "node_ref": ref("SYN-5.1.3"),
     "story_ids": [STORY], "basis": "confirmed", "confirmation_source": "demo seed: stated in request P001"},
    {"id": "T3", "label": "Receive and inspect; record classification", "type": "task", "actor": "Warehouse",
     "system": "JDE (P55RET01)", "controls": ["Exactly one classification per return"], "node_ref": ref("SYN-5.2.2"),
     "story_ids": [STORY], "basis": "assumption"},
    {"id": "T4", "label": "Classification?", "type": "decision", "actor": "Warehouse", "system": "JDE (P55RET01)",
     "node_ref": ref("SYN-5.2.3"), "story_ids": [STORY], "basis": "assumption"},
    {"id": "T5", "label": "Return to stock", "type": "task", "actor": "Warehouse", "basis": "assumption"},
    {"id": "T6", "label": "Repair, warranty claim or scrap", "type": "task", "actor": "Warehouse", "basis": "assumption"},
    {"id": "T7", "label": "Post value adjustment and credit dealer", "type": "end", "actor": "Finance", "system": "JDE",
     "controls": ["No dealer credit before the inspection outcome is recorded"], "node_ref": ref("SYN-6.1.2"),
     "basis": "assumption"}],
    "connections": [{"from": "T1", "to": "T2"}, {"from": "T2", "to": "T3"}, {"from": "T3", "to": "T4"},
                    {"from": "T4", "to": "T5", "label": "resaleable"}, {"from": "T4", "to": "T6", "label": "other"},
                    {"from": "T5", "to": "T7"}, {"from": "T6", "to": "T7"}]},
    note="demo seed", actor=ADMIN, actor_user_id=admin_id, expected_version=0, story_exists=links)

# -- 6. the SYNTHETIC object and its source export -----------------------------------
SOURCE = """// SYNTHETIC SIMULATION SOURCE (jade_sim_er) -- not a JD Edwards export.
OBJECT P55RET01 FORM W55RET01A SYSTEM 55
INPUT BC ReturnAuth STRING
INPUT BC Condition STRING
OUTPUT VA Disposition STRING
OUTPUT VA ErrorCode STRING

EVENT OK_Button_Clicked
VA ErrorCode = ""
IF BC Condition = "RESALEABLE"
    VA Disposition = "STOCK"
ELSE
    VA Disposition = "HOLD"
END IF
END EVENT
"""
OLD = """IF BC Condition = "RESALEABLE"
    VA Disposition = "STOCK"
ELSE
    VA Disposition = "HOLD"
END IF"""
M = f"// MOD {STORY}"
NEW = f"""IF BC ReturnAuth = ""  {M}
    VA ErrorCode = "RA01"  {M}
    VA Disposition = "HOLD"  {M}
ELSE  {M}
    IF BC Condition = "RESALEABLE"  {M}
        VA Disposition = "STOCK"  {M}
    ELSE  {M}
        IF BC Condition = "REPAIR"  {M}
            VA Disposition = "REPAIR"  {M}
        ELSE  {M}
            IF BC Condition = "WARRANTY"  {M}
                VA Disposition = "WARRANTY"  {M}
            ELSE  {M}
                IF BC Condition = "DAMAGED" OR BC Condition = "SCRAP"  {M}
                    VA Disposition = "SCRAP"  {M}
                ELSE  {M}
                    VA Disposition = "HOLD"  {M}
                END IF  {M}
            END IF  {M}
        END IF  {M}
    END IF  {M}
END IF  {M}"""
technical_sim.seed_object(COMPANY, env, source=SOURCE, description="Dealer Return Inspection (SYNTHETIC)",
                          build_rules=[{"id": "SIM-BLD-1", "kind": "modification_marker", "marker": "// MOD {story_id}",
                                        "description": "customer build standard"}],
                          actor="demo seed", reason="demo fixture: synthetic ER object")
art = artifacts.upload(COMPANY, ArtifactUpload.model_validate({
    "kind": "technical_export", "object_name": "P55RET01", "object_type": "ER", "export_format": "jade_sim_er",
    "customer_environment": env, "path_code": "DV920", "release": "9.2", "source_location": "er/P55RET01.jser",
    "repository": "git@synthetic.invalid:jde/er-exports.git", "commit_ref": "9b41d2e",
    "exported_at": "2026-09-23T08:00:00+00:00", "runtime_correspondence": "matches_dev_runtime",
    "runtime_statement": "SYNTHETIC export", "runtime_stated_by": "demo seed", "file_name": "P55RET01.jser",
    "content_base64": base64.b64encode(SOURCE.encode()).decode()}), actor="demo seed")

# -- 7. a SCRIPTED Architect that consults the process context ------------------------
summary = {"architect_decision": {
    "recommended_route": "Technical Agent", "confidence": 0.7,
    "existing_functionality_found": "SCRIPTED STAND-IN: P55RET01 records a condition but only distinguishes resaleable "
                                    "returns and does not require a return authorisation",
    "alternatives_considered": [], "objects_affected": ["P55RET01", "R55RET01"],
    "dependencies_and_conflicts": ["R55RET01 (monthly returns report) is out of this package's scope"],
    "rollback_strategy": "restore the previous source as a new approved revision"},
    "implementation_spec": {"sequence": ["P55RET01: refuse a return without authorisation (RA01)",
                                         "P55RET01: derive the disposition from the classification"],
                            "required_mcp_operations": [], "human_actions_required": ["CNC deploys the package"],
                            "validation_approach": "positive, negative and neighbouring tests"},
    "evidence": {"process_findings": {
        "affected_processes": ["SYN-5.1.3: authorisation enforced at receipt", "SYN-5.2.3: disposition rule"],
        "missing_requirements": ["Reporting (SYN-6.2.1) needs its own story"],
        "missing_controls": [], "missing_acceptance_criteria": []}}}
real_build = architecture_driver.build_discovery_tools


def spy(*a, **k):
    tools = real_build(*a, **k)
    tools.process_context()
    tools.read_artifact(art["artifact_id"], art["revision"])
    return tools


async def scripted_architect(prompt, options):
    yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="demo", result="```json\n" + json.dumps(summary) + "\n```")


architecture_driver.build_discovery_tools, sdk.query = spy, scripted_architect
asyncio.run(architecture_driver.run_architecture_review(story_id=STORY, repo_root=settings.repo_root,
                                                        run_service=get_architecture_review_service(),
                                                        customer_id=COMPANY, initiated_by="demo seed"))
architecture_driver.build_discovery_tools = real_build
del architect_tools

# -- 8. design approval, scripted Technical Agent, simulated delivery -------------------
a = service.assignment(COMPANY, STORY)
service.approve_design(COMPANY, STORY, a["design_revision"], actor_name=ADMIN, actor_user_id=admin_id, roles=roles,
                       note="demo design approval (throwaway identity)")
trun = service.start_run(COMPANY, STORY, purpose="prepare", initiated_by="demo seed")
tools = TechnicalAgentTools(company_id=COMPANY, story_id=STORY, run_id=trun["run_id"])
opened = tools.open_in_workspace(f"{art['artifact_id']}@r{art['revision']}")
assert tools.replace(opened["file_id"], OLD, NEW)["changed"]
ev = "OK_Button_Clicked"
out = tools.submit({
    "explanation": "SCRIPTED STAND-IN (not a model run): a return without an authorisation number is held with RA01; "
                   "the disposition follows the inspection classification.",
    "requirement_trace": [{"requirement": "AC1 authorisation required", "how": "RA01 when ReturnAuth is empty"},
                          {"requirement": "AC3 disposition follows classification", "how": "nested condition"}],
    "dependencies": [], "missing_evidence": ["AC4 reporting is not part of this package"], "unsupported": [],
    "recovery": "restore the previous source",
    "test_plan": [
        {"name": "repair classification goes to repair", "kind": "positive", "event": ev,
         "inputs": {"ReturnAuth": "RA-1001", "Condition": "REPAIR"}, "expected": {"Disposition": "REPAIR", "ErrorCode": ""}},
        {"name": "damaged goes to scrap", "kind": "positive", "event": ev,
         "inputs": {"ReturnAuth": "RA-1002", "Condition": "DAMAGED"}, "expected": {"Disposition": "SCRAP"}},
        {"name": "no authorisation is refused", "kind": "negative", "event": ev,
         "inputs": {"ReturnAuth": "", "Condition": "RESALEABLE"}, "expected": {"Disposition": "HOLD", "ErrorCode": "RA01"}},
        {"name": "resaleable still goes to stock", "kind": "neighbouring", "event": ev,
         "inputs": {"ReturnAuth": "RA-1003", "Condition": "RESALEABLE"}, "expected": {"Disposition": "STOCK", "ErrorCode": ""}},
    ]})
store.finish_run(trun["run_id"], status="completed", outcome={**tools.outcome, "summary": "scripted stand-in"})
rev = out["revision"]
service.approve_package(COMPANY, STORY, rev, actor_name=ADMIN, actor_user_id=admin_id, roles=roles,
                        note="demo approval (throwaway identity)")
for step in ("apply", "build"):
    service.run_milestone(COMPANY, STORY, rev, step, actor=f"{ADMIN} ({admin_id})")
if auth_service.get_user_by_email("cnc@e2e.local"):
    service.record_cnc(COMPANY, STORY, rev, actor_user_id="u-e2e-cnc", actor_name="E2E CNC Operator",
                       package_name="DV920RET01", evidence_reference="synthetic CNC ticket CNC-RET-1",
                       note="simulated hand-off (demo seed)")
    result = service.run_milestone(COMPANY, STORY, rev, "verify", actor=f"{ADMIN} ({admin_id})")
else:
    result = {"verify": "skipped: no demo CNC operator"}
print(json.dumps({"story": STORY, "framework": FID, "package": rev, "submitted": out.get("submitted"),
                  "verify": result if isinstance(result, dict) and "verify" in result else "done"}, default=str))
