#!/usr/bin/env python3
"""
Demonstration only: a BicycleWorks story delivered through the FUNCTIONAL
route (a processing-option change) in the SIMULATED DEV estate, for the
local preview. Adds its own story only if it does not exist yet; never
touches other records.

  * Scripted stand-in (clearly not the model) for the Architect, which
    consults the story's process context and proposes the exact change
    through the same governed code the real agent uses.
  * The exact change is approved by the throwaway demo admin (E2E Admin),
    written to the simulated DEV estate through the execution gate, and its
    approved test orchestration is run (a fixed mock PASS in simulation).
  * The as-built record is left for the person trying the preview.

    python3 scripts/seed_demo_functional.py      # after seed_demo_process.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api_service"))

from jde_api_service.config import settings  # noqa: E402

for name, sub in (("JDE_COMPANY_SCOPE_DIR", "engagement_scope"), ("JDE_STORY_COMPANY_DIR", "customer_links"),
                  ("JDE_DESIGN_BASELINE_DIR", "design_baselines"), ("JDE_SIM_ESTATE_DIR", "sim_estate")):
    os.environ.setdefault(name, os.path.abspath(os.path.join(settings.data_dir, sub)))
os.environ.setdefault("JDE_AUTH_DB_PATH", os.path.abspath(os.path.join(settings.data_dir, "jde.sqlite3")))

import claude_agent_sdk as sdk  # noqa: E402

from jde_api_service.models.change import UserStory  # noqa: E402
from jde_api_service.models.engagement_scope import EngagementScopeUpdate  # noqa: E402
from jde_api_service.process import framework, maps, story as story_process  # noqa: E402
from jde_api_service.services import architecture_driver, auth_service, membership_service  # noqa: E402
from jde_api_service.services.registry import (  # noqa: E402
    get_architecture_review_service, get_customer_link_service, get_delivery_queue_service,
    get_domain_review_service, get_engagement_scope_service,
)
from jde_mcp_server import approval, backlog  # noqa: E402
from jde_mcp_server.ais_client import client as ais  # noqa: E402

COMPANY, STORY, DOMAIN = "bwm", "S-BW-RETURNTYPE", "DOM-BWM-CUST-SERVICE"
if backlog._load(STORY) is not None:  # noqa: SLF001 -- idempotence check only
    print(json.dumps({"story": STORY, "seeded": "already"}))
    sys.exit(0)
sel = framework.selected_active(COMPANY)
if sel is None:
    sys.exit("run seed_demo_process.py first (no active process framework)")
FID, FV = sel["framework"]["framework_id"], sel["version"]["version"]
admin = auth_service.get_user_by_email("admin@e2e.local")
roles = set(membership_service.roles_for(admin.id, COMPANY))
ADMIN = "E2E Admin"

# Scope: add the one approved target, test and bounded spike window (keeps everything else).
scopes = get_engagement_scope_service()
current = scopes.get_for_customer(COMPANY)
base = current.model_dump(mode="json")
fa = base.get("functional_agent") or {}
fa["approved_versions"] = [v for v in fa.get("approved_versions") or [] if v.get("version") != "CIQ0001"] + [{
    "capability_id": "processing_option_update", "option_category": "document_and_order_types",
    "application": "P4210", "version": "CIQ0001", "options": ["PDOCTYPE"], "allowed_values": ["CR"]}]
fa["spike_experiments"] = [s for s in fa.get("spike_experiments") or [] if s.get("version") != "CIQ0001"] + [{
    "capability_id": "processing_option_update", "capability_revision": "r1", "application": "P4210",
    "version": "CIQ0001", "option": "PDOCTYPE", "environment": "DEV", "expires_at": "2099-01-01T00:00:00+00:00",
    "note": "SIMULATION demo window"}]
tests = [t for t in (base.get("test_scope") or {}).get("approved_tests") or [] if t.get("orchestration") != "ORCH_RETURN"]
tests.append({"orchestration": "ORCH_RETURN", "side_effects": ["creates_dev_transaction"],
              "note": "creates one DEV return order (simulated)"})
scopes.upsert(COMPANY, EngagementScopeUpdate.model_validate({
    **{k: v for k, v in base.items() if k in EngagementScopeUpdate.model_fields},
    "tools_release": base.get("tools_release") or "9.2.8.2", "functional_agent": fa,
    "mechanisms_allowed": sorted(set((base.get("mechanisms_allowed") or []) + ["ais_form_service_request", "ais_orchestration"])),
    "test_scope": {"approved_tests": tests}, "expected_revision": current.revision,
}), actor="demo seed")

TEXT = """As a customer service agent, I want return orders entered on the dealer-returns order entry version to default to order type CR, so that returns are never booked as ordinary sales orders.

business_context: Dealer returns are entered in Sales Order Entry (P4210) version CIQ0001. Today the version defaults to order type S3, and agents must remember to change it.

acceptance_criteria:
- AC1: A new order on version CIQ0001 defaults to order type CR verified_by: T1
- AC2: Other order entry versions are unchanged"""
backlog.propose_to_backlog(STORY, TEXT, {"operational_reach": "Customer service"}, "Low", source="Business")
backlog.approve(STORY, "Demo seed", "demo data")
get_customer_link_service().link(STORY, COMPANY)
get_delivery_queue_service().add(STORY, COMPANY, "Demo seed", "queued")
reviews = get_domain_review_service()
reviews.ensure(STORY, UserStory(statement=TEXT.split("\n")[0]))
reviews.assign_domain(STORY, business_domain_id=DOMAIN, uncertain=False, note="demo seed")

# Processes and a to-be map, decided by the throwaway demo admin.
story_process.decide(COMPANY, STORY, status="confirmed",
                     refs=[{"framework_id": FID, "version": FV, "node_key": "SYN-5.1.3",
                            "rationale": "the return order is created when the return is authorised"}],
                     no_mapping_reason="", findings={}, analysis_run_id=None, note="demo seed (throwaway identity)",
                     reviewer_name=ADMIN, reviewer_user_id=admin.id, roles=sorted(roles), expected_revision=0)
maps.save(COMPANY, STORY, "to_be", {"title": "Return order entry (to-be)", "steps": [
    {"id": "R1", "label": "Return authorised", "type": "start", "actor": "Customer service", "basis": "confirmed",
     "confirmation_source": "demo seed: stated in the story"},
    {"id": "R2", "label": "Enter return order on CIQ0001", "type": "task", "actor": "Customer service", "system": "JDE (P4210)",
     "controls": ["Order type defaults to CR"], "basis": "assumption",
     "node_ref": {"framework_id": FID, "version": FV, "node_key": "SYN-5.1.3"}, "story_ids": [STORY]},
    {"id": "R3", "label": "Return order booked as CR", "type": "end", "actor": "Customer service", "basis": "assumption"}],
    "connections": [{"from": "R1", "to": "R2"}, {"from": "R2", "to": "R3"}]},
    note="demo seed", actor=ADMIN, actor_user_id=admin.id, expected_version=0,
    story_exists=lambda s: get_customer_link_service().customer_for(s) == COMPANY)

# SCRIPTED Architect: consults the process context, proposes the exact change.
summary = {"architect_decision": {
    "recommended_route": "Functional Agent", "confidence": 0.8,
    "existing_functionality_found": "SCRIPTED STAND-IN: P4210 version CIQ0001 has a default order type processing option",
    "alternatives_considered": [], "objects_affected": ["P4210 CIQ0001 PDOCTYPE"],
    "dependencies_and_conflicts": [], "rollback_strategy": "set PDOCTYPE back to its previous value"},
    "implementation_spec": {"sequence": ["set P4210/CIQ0001 PDOCTYPE to CR"], "required_mcp_operations": ["set_processing_option"],
                            "human_actions_required": [], "validation_approach": "ORCH_RETURN creates one return order"},
    "evidence": {}}
real_build = architecture_driver.build_discovery_tools


def spy(*a, **k):
    tools = real_build(*a, **k)
    tools.process_context()
    return tools


async def scripted_architect(prompt, options):
    approval.propose_change(STORY, {"tool": "set_processing_option", "story_id": STORY, "application": "P4210",
                                    "version": "CIQ0001", "option": "PDOCTYPE", "value": "CR",
                                    "test_orchestration": "ORCH_RETURN"}, "processing_option_update")
    yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="demo", result="```json\n" + json.dumps(summary) + "\n```")


architecture_driver.build_discovery_tools, sdk.query = spy, scripted_architect
asyncio.run(architecture_driver.run_architecture_review(story_id=STORY, repo_root=settings.repo_root,
                                                        run_service=get_architecture_review_service(),
                                                        customer_id=COMPANY, initiated_by="demo seed"))
architecture_driver.build_discovery_tools = real_build

from jde_api_service.services.change_service import _latest_change_record_for  # noqa: E402

change = _latest_change_record_for(STORY)
approval.approve_change(change["change_id"], ADMIN, company_id=COMPANY, approver_roles=roles, approver_user_id=admin.id,
                        note="demo approval (throwaway identity)")
ais.set_processing_option(STORY, change["change_id"], "P4210", "CIQ0001", "PDOCTYPE", "CR")
ais.run_orchestration(STORY, change["change_id"], "ORCH_RETURN", {})
print(json.dumps({"story": STORY, "change": change["change_id"], "value_now":
                  ais.read_processing_option_value(COMPANY, "P4210", "CIQ0001", "PDOCTYPE")}))
