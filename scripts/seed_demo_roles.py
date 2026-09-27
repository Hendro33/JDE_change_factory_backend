#!/usr/bin/env python3
"""
Demonstration only: the three-role operating model on the DEMO customer.

Adds, once and only if absent:
  * a throwaway Application Manager (am@e2e.local, role product_manager only),
    password from JADE_E2E_AM_PASSWORD (skipped when the variable is unset);
  * one SYNTHETIC story, S-DEMO-GATES-1, placed in the demo Domain Owner's
    business domain and waiting for the Domain Owner's review -- so the
    Domain Owner's User Story Review and then the Application Manager's
    Backlog Review (Gate 1) can be walked in the browser.

Run after seed_demo_process.py (it creates the Domain Owner and the domain),
with the same JDE_* directory variables as the backend:

    python3 scripts/seed_demo_roles.py [company_id]   # default: bwm

Nothing is written to JD Edwards.
"""

from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "api_service"))

from jde_api_service.config import settings  # noqa: E402

for name, sub in (("JDE_COMPANY_SCOPE_DIR", "engagement_scope"), ("JDE_STORY_COMPANY_DIR", "customer_links")):
    os.environ.setdefault(name, os.path.abspath(os.path.join(settings.data_dir, sub)))
os.environ.setdefault("JDE_AUTH_DB_PATH", os.path.abspath(os.path.join(settings.data_dir, "jde.sqlite3")))

from jde_api_service.models.change import AcceptanceCriterion, UserStory  # noqa: E402
from jde_api_service.services import auth_service, membership_service  # noqa: E402
from jde_api_service.services.registry import get_customer_link_service, get_domain_review_service  # noqa: E402
from jde_mcp_server import backlog  # noqa: E402

COMPANY = sys.argv[1] if len(sys.argv) > 1 else "bwm"
STORY = "S-DEMO-GATES-1"
DOMAIN = "DOM-BWM-CUST-SERVICE"

admin = auth_service.get_user_by_email("admin@e2e.local")
if admin is None:
    sys.exit("admin@e2e.local does not exist; start the backend with the e2e bootstrap admin first")

# -- the Application Manager ------------------------------------------------------
am_pw = os.environ.get("JADE_E2E_AM_PASSWORD")
if am_pw and auth_service.get_user_by_email("am@e2e.local") is None:
    auth_service.create_user("am@e2e.local", am_pw, "E2E Application Manager", user_id="u-e2e-am")
    membership_service.create_membership("u-e2e-am", COMPANY, ["product_manager"], created_by=admin.id)

# -- one story waiting for the Domain Owner ---------------------------------------
if backlog._load(STORY) is None:  # noqa: SLF001 -- idempotence check only
    text = """As a customer service agent, I want the dealer's preferred return carrier shown on each return order, so that returns are collected by the carrier the dealer agreed.

business_context: SYNTHETIC demonstration story for the Domain Owner and Application Manager gates.

acceptance_criteria:
- AC1: The preferred return carrier of the dealer is shown on the return order
- AC2: A return order for a dealer without a preferred carrier shows that none is set"""
    backlog.propose_to_backlog(STORY, text, {"operational_reach": "All dealer returns"}, "Medium", source="Business")
    get_customer_link_service().link(STORY, COMPANY)
    reviews = get_domain_review_service()
    reviews.ensure(STORY, UserStory(
        statement=text.split("\n")[0],
        business_context="SYNTHETIC demonstration story for the Domain Owner and Application Manager gates.",
        acceptance_criteria=[AcceptanceCriterion(id="AC1", text="The preferred return carrier of the dealer is shown on the return order"),
                             AcceptanceCriterion(id="AC2", text="A return order for a dealer without a preferred carrier shows that none is set")],
    ))
    reviews.assign_domain(STORY, business_domain_id=DOMAIN, uncertain=False, note="demo seed")

print(json.dumps({"story": STORY, "application_manager": "am@e2e.local" if am_pw else None}))
