#!/usr/bin/env python3
"""
Demonstration only: prepares ONE Technical story for the Technical Work
screen's browser demonstration, WITHOUT a model run.

Scripted stand-ins (clearly not the model) play the Architect and the
Technical Agent through the same governed code the real agents use: a
design routed to the Technical Agent with its evidence baseline, a design
approval by the throwaway e2e admin, and package revision 1 prepared in an
isolated workspace from a SYNTHETIC source (jade_sim_er -- a simulation
format, not a JDE export). It also creates a throwaway CNC operator whose
password comes from JADE_E2E_CNC_PASSWORD. Run it against a throwaway data
directory with the backend's JDE_* variables.

    python3 scripts/seed_demo_technical.py [company_id]   # default: bwm

Nothing is written to any JDE; the simulated DEV estate is the only target.
"""

from __future__ import annotations

import asyncio
import base64
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

from jde_api_service.discovery import artifacts  # noqa: E402
from jde_api_service.discovery.models import ArtifactUpload  # noqa: E402
from jde_api_service.models.engagement_scope import EngagementScopeUpdate  # noqa: E402
from jde_api_service.services import architecture_driver, auth_service, membership_service  # noqa: E402
from jde_api_service.services.registry import (  # noqa: E402
    get_architecture_review_service, get_customer_link_service, get_delivery_queue_service,
    get_engagement_scope_service,
)
from jde_api_service.technical import service, store  # noqa: E402
from jde_api_service.technical.tools import TechnicalAgentTools  # noqa: E402
from jde_mcp_server import backlog, technical_sim  # noqa: E402

company = sys.argv[1] if len(sys.argv) > 1 else "bwm"
STORY = "S-DEMO-TECH-1"
SOURCE = """// SYNTHETIC SIMULATION SOURCE (jade_sim_er) -- not a JD Edwards export.
OBJECT P554210 FORM W554210A SYSTEM 55
INPUT BC OrderTotal NUMBER
INPUT BC CreditLimit NUMBER
INPUT BC OrderType STRING
INPUT BC CreditExempt STRING   // "Y" = credit-exempt customer
OUTPUT VA HoldCode STRING

EVENT OK_Button_Clicked
IF BC OrderType = "SO" AND BC OrderTotal > BC CreditLimit
    VA HoldCode = "C1"
ELSE
    VA HoldCode = ""
END IF
END EVENT
"""
OLD = 'IF BC OrderType = "SO" AND BC OrderTotal > BC CreditLimit'
admin = auth_service.get_user_by_email("admin@e2e.local")
admin_id = getattr(admin, "id", None) or os.environ.get("JADE_E2E_ADMIN_ID", "")

# 1. Scope: DEV binding, approval policy, the technical section.
scopes = get_engagement_scope_service()
current = scopes.get_for_customer(company)
base = current.model_dump(mode="json") if current else {}
env = ((base.get("environment") or {}).get("dev_environment_id")) or "JDV920"
scopes.upsert(company, EngagementScopeUpdate.model_validate({
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

# 2. A simulation discovery profile whose data-sharing policy lets artifact
#    content reach the model (without one, content is withheld by default).
from datetime import datetime, timedelta, timezone  # noqa: E402

from jde_api_service.discovery import profile_service  # noqa: E402
from jde_api_service.discovery.models import JdeProfileConfig  # noqa: E402

if profile_service.load(company) is None:
    day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    profile_service.save(company, JdeProfileConfig.model_validate({
        "connection_mode": "simulation", "ais_base_url": "https://ais-dev.synthetic.invalid", "environment": env,
        "role": "JADEDISC", "expected_application_release": "9.2", "expected_tools_release": "9.2.8.2",
        "path_code": "DV920", "customer_contact": "Synthetic", "cnc_contact": "Synthetic",
        "network_route": "simulation only", "isolation_evidence": "SIMULATION", "routing_isolation_confirmed": True,
        "privilege_statement": "SIMULATION", "privilege_confirmed": True, "runtime_attestation_confirmed": True,
        "runtime_attestation_evidence": "SIMULATION",
        "approved_reads": [{"capability_id": "object_librarian", "targets": ["P554210"],
                            "fields": ["SIOBNM", "SIFUNO", "SISY", "SIMD", "SIPKGNAME"]}],
        "discovery_window": {"starts_at": (day - timedelta(days=1)).isoformat(), "ends_at": (day + timedelta(days=2)).isoformat()},
        "limits": {"max_records": 10, "timeout_seconds": 10}, "data_sharing_policy": "configuration_and_artifacts",
    }), expected_revision=None, actor="demo seed")

# 3. The synthetic object in the simulated estate and its source export.
technical_sim.seed_object(company, env, source=SOURCE, description="Custom Sales Order Review (SYNTHETIC)",
                          build_rules=[{"id": "SIM-BLD-1", "kind": "modification_marker", "marker": "// MOD {story_id}",
                                        "description": "customer build standard"}],
                          actor="demo seed", reason="demo fixture: synthetic ER object")
art = artifacts.upload(company, ArtifactUpload.model_validate({
    "kind": "technical_export", "object_name": "P554210", "object_type": "ER", "export_format": "jade_sim_er",
    "customer_environment": env, "path_code": "DV920", "release": "9.2", "source_location": "er/P554210.jser",
    "repository": "git@synthetic.invalid:jde/er-exports.git", "commit_ref": "5e7a1c0",
    "exported_at": "2026-09-23T08:00:00+00:00", "runtime_correspondence": "matches_dev_runtime",
    "runtime_statement": "SYNTHETIC export", "runtime_stated_by": "demo seed", "file_name": "P554210.jser",
    "content_base64": base64.b64encode(SOURCE.encode()).decode()}), actor="demo seed")

# 4. The approved story and a scripted Architect design routed to the Technical Agent.
backlog.propose_to_backlog(STORY, "SYNTHETIC: credit-exempt webshop customers (CreditExempt = Y) are not put on "
                                  "credit hold; everything else unchanged.", {"financial_impact": "Low"}, "Medium",
                           source="Business")
backlog.approve(STORY, "Demo seed", "demo data")
get_customer_link_service().link(STORY, company)
get_delivery_queue_service().add(STORY, company, "Demo seed", "queued")
summary = {"architect_decision": {"recommended_route": "Technical Agent", "confidence": 0.8,
                                  "existing_functionality_found": "P554210 holds over-limit SO orders with C1",
                                  "alternatives_considered": [], "objects_affected": ["P554210"],
                                  "dependencies_and_conflicts": [], "rollback_strategy": "restore the previous source"},
           "implementation_spec": {"sequence": ["exclude CreditExempt = Y from the C1 hold in P554210"],
                                   "required_mcp_operations": [], "human_actions_required": ["CNC deploys the package"],
                                   "validation_approach": "positive, negative and neighbouring tests"},
           "evidence": {}}


async def scripted_architect(prompt, options):
    yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="demo", result="```json\n" + json.dumps(summary) + "\n```")


sdk.query = scripted_architect
asyncio.run(architecture_driver.run_architecture_review(story_id=STORY, repo_root=settings.repo_root,
                                                        run_service=get_architecture_review_service(),
                                                        customer_id=company, initiated_by="demo seed"))

# 5. Design approval by the throwaway e2e admin, then a scripted Technical Agent prepares revision 1.
a = service.assignment(company, STORY)
roles = membership_service.roles_for(admin_id, company) if admin_id else frozenset()
service.approve_design(company, STORY, a["design_revision"], actor_name="E2E Admin", actor_user_id=admin_id,
                       roles=set(roles), note="demo design approval (throwaway identity)")
run = service.start_run(company, STORY, purpose="prepare", initiated_by="demo seed")
tools = TechnicalAgentTools(company_id=company, story_id=STORY, run_id=run["run_id"])
evidence_id = f"{art['artifact_id']}@r{art['revision']}"
opened = tools.open_in_workspace(evidence_id)
tools.replace(opened["file_id"], OLD, OLD + f' AND BC CreditExempt != "Y" // MOD {STORY}')
out = tools.submit({
    "explanation": "SCRIPTED STAND-IN (not a model run): credit-exempt customers are excluded from the C1 hold.",
    "requirement_trace": [{"requirement": "exempt customers not held", "how": "one extra condition"}],
    "dependencies": [], "missing_evidence": [], "unsupported": [], "recovery": "restore the previous source",
    "test_plan": [
        {"name": "exempt customer over limit is not held", "kind": "positive", "event": "OK_Button_Clicked",
         "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "SO", "CreditExempt": "Y"}, "expected": {"HoldCode": ""}},
        {"name": "non-exempt customer over limit is held", "kind": "negative", "event": "OK_Button_Clicked",
         "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "SO", "CreditExempt": "N"}, "expected": {"HoldCode": "C1"}},
        {"name": "direct-ship orders are unaffected", "kind": "neighbouring", "event": "OK_Button_Clicked",
         "inputs": {"OrderTotal": 1500, "CreditLimit": 1000, "OrderType": "S3", "CreditExempt": "N"}, "expected": {"HoldCode": ""}},
    ]})
store.finish_run(run["run_id"], status="completed", outcome={**tools.outcome, "summary": "scripted stand-in"})

# 6. A throwaway CNC operator for the demonstration.
cnc_pw = os.environ.get("JADE_E2E_CNC_PASSWORD")
if cnc_pw:
    auth_service.create_user("cnc@e2e.local", cnc_pw, "E2E CNC Operator", user_id="u-e2e-cnc")
    membership_service.create_membership("u-e2e-cnc", company, ["cnc_operator"], created_by=admin_id or "demo seed")
print(json.dumps({"story": STORY, "package": out.get("revision"), "submitted": out.get("submitted")}))
