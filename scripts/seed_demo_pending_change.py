#!/usr/bin/env python3
"""
Seeds ONE approved story with ONE pending exact change for a company,
so the Stage 1 approval-authority demonstration has something to
approve. Development/demo data only: run it against a throwaway data
directory, with the same JDE_* directory variables the backend uses.

    python3 scripts/seed_demo_pending_change.py [company_id]   # default: bwm

Nothing is written to JD Edwards; the change stays pending until a
person approves it in Jade, and executing it would still need the
company's full scope.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api_service"))

from jde_api_service.config import settings  # noqa: E402  (also puts mcp_server on the path)

# Same wiring the backend does at startup (main._wire_execution_gate).
os.environ.setdefault("JDE_COMPANY_SCOPE_DIR", os.path.abspath(os.path.join(settings.data_dir, "engagement_scope")))
os.environ.setdefault("JDE_STORY_COMPANY_DIR", os.path.abspath(os.path.join(settings.data_dir, "customer_links")))

from jde_api_service.services.registry import get_customer_link_service, get_delivery_queue_service  # noqa: E402
from jde_mcp_server import approval, backlog  # noqa: E402

company = sys.argv[1] if len(sys.argv) > 1 else "bwm"
story_id = "S12-DEMO-1"
backlog.propose_to_backlog(
    story_id, "As an order clerk I want new orders to default to type SO", {"financial_impact": "Low"}, "Low",
    source="Business",
)
backlog.approve(story_id, "Demo seed", "demo data")
get_customer_link_service().link(story_id, company)
get_delivery_queue_service().add(story_id, company, "Demo seed", "queued")
record = approval.propose_change(
    story_id,
    {"tool": "set_processing_option", "story_id": story_id, "application": "P4210", "version": "BWM0001",
     "option": "PDOCTYPE", "value": "SO", "test_orchestration": "ORCH_SO_DEFAULT"},
    "processing_option_update",
)
print(f"{record['change_id']} pending for company {record['company_id']}")
