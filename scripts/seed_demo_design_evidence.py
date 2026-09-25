#!/usr/bin/env python3
"""
Demonstration only: records ONE Architect design with an evidence baseline
for the story seeded by seed_demo_pending_change.py, WITHOUT a model run.

A scripted stand-in plays the Architect: it calls the same governed
discovery tools the real Architect gets (so every read is validated,
logged and sanitised exactly as in a real run), then records the design
and its immutable evidence manifest. It needs the company's discovery
profile to be enabled -- in simulation mode for a demo -- and uses the
same JDE_* directory variables as the backend.

    python3 scripts/seed_demo_design_evidence.py [company_id]   # default: bwm

Nothing is written to JD Edwards. In simulation mode nothing contacts JD
Edwards at all; every observation is labelled SIMULATION.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api_service"))

from jde_api_service.config import settings  # noqa: E402

os.environ.setdefault("JDE_COMPANY_SCOPE_DIR", os.path.abspath(os.path.join(settings.data_dir, "engagement_scope")))
os.environ.setdefault("JDE_STORY_COMPANY_DIR", os.path.abspath(os.path.join(settings.data_dir, "customer_links")))

from jde_api_service.models.change import ArchitectDecision, ImplementationSpecification  # noqa: E402
from jde_api_service.services import architecture_driver  # noqa: E402
from jde_api_service.services.registry import get_architecture_review_service  # noqa: E402

company = sys.argv[1] if len(sys.argv) > 1 else "bwm"
story_id = "S12-DEMO-1"
tools = architecture_driver.build_discovery_tools(story_id, company, agent_run_id="DEMO-SCRIPTED-RUN",
                                                  initiated_by=None)
if tools.grant is None:
    sys.exit(f"discovery is not available for {company}: {tools.ledger.no_grant_reason}")

caps = tools.list_capabilities()
po = tools.read("processing_option_values", next(
    t for c in caps["capabilities"] if c["capability_id"] == "processing_option_values" for t in c["approved_targets"]))
udc = tools.read("udc_values", "00/DT")
tools.read("source_code", "B5542001")  # unavailable through AIS: becomes a gap, never an assumption
tools.list_artifacts()

evidence = {
    "citations": [
        {"claim": "The webshop version's processing options were read", "evidence_ids": [po["observation_id"]],
         "basis": "observed"},
        {"claim": "Document type SO exists in UDC 00/DT", "evidence_ids": [udc["observation_id"]], "basis": "observed"},
        {"claim": "No custom code changes the default document type", "evidence_ids": [], "basis": "assumption"},
    ],
    "customisations": [],
    "gaps": [{"kind": "missing", "description": "Custom credit check B5542001 source not imported",
              "question": "Can the CNC export B5542001 from the DV920 path code with its commit reference?"}],
}
service = get_architecture_review_service()
service.start(story_id)
service.complete(
    story_id,
    architect_decision=ArchitectDecision(
        recommended_route="Functional Agent", confidence=0.7,
        existing_functionality_found="Scripted demonstration design -- not a model run",
        alternatives_considered=[], objects_affected=[], dependencies_and_conflicts=[],
        rollback_strategy="restore the previous processing option value", decided_at="2026-09-23T00:00:00+00:00"),
    implementation_spec=ImplementationSpecification(sequence=["demonstration only"], required_mcp_operations=[],
                                                    human_actions_required=[], validation_approach="n/a"),
    note="scripted demonstration (seed_demo_design_evidence.py)",
)
created = architecture_driver.record_design_baseline(
    story_id=story_id, customer_id=company, run_service=service, tools=tools, summary={"evidence": evidence},
    agent_run_id="DEMO-SCRIPTED-RUN", initiated_by=None)
print(f"{created['baseline_id']} recorded for {story_id} ({company}); "
      f"{len(created['manifest']['observations'])} observation(s), {len(created['manifest']['gaps'])} gap(s)")
