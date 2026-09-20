"""
EngagementScope -- the per-customer, human-authored configuration that
answers "what is this engagement actually authorised to touch in JDE."

This is api_service's own, customer-scoped counterpart to the single
global scope.json the design document's Appendix D.2/E.2 describes and
mcp_server/jde_mcp_server/scope.py enforces. It does NOT replace
scope.json and does NOT change how mcp_server enforces engagement scope
-- that module stays exactly as it is (sibling package, unmodified).
Today there is exactly one scope.json for the whole deployment, keyed
by nothing; this model is the intended per-customer source of truth an
operator would export into that file per engagement, once the MCP/AIS
layer itself becomes multi-tenant -- a larger, explicitly out-of-scope
change this increment does not attempt (see the ERP Landscape screen's
own honesty note, surfaced via models/admin.py).

Same "no configuration means no default permission" principle scope.py
already applies: an EngagementScope with no approved_versions/
authorized_object_types authorises nothing, and is shown as such rather
than silently defaulted to something permissive.
"""

from __future__ import annotations

from typing import Optional

from .base import ApiModel


class ApprovedVersion(ApiModel):
    application: str
    version: str
    options: list[str] = []
    allowed_values: list[str] = []
    notes: str = ""


class FunctionalAgentScope(ApiModel):
    approved_versions: list[ApprovedVersion] = []
    never_touch_categories: list[str] = []
    approvers: list[str] = []


class TechnicalAgentScope(ApiModel):
    authorized_object_types: list[str] = []
    reserved_product_code: str = ""
    naming_prefix: str = ""
    approvers: list[str] = []


class EngagementScope(ApiModel):
    customer_id: str
    tools_release: str = ""
    functional_agent: FunctionalAgentScope = FunctionalAgentScope()
    technical_agent: TechnicalAgentScope = TechnicalAgentScope()
    # None means "never configured" -- distinct from an explicitly
    # empty-but-saved scope, same honesty this whole model exists for.
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class EngagementScopeUpdate(ApiModel):
    """Full-replace payload -- an admin edits the whole scope at once,
    same convention as the frontend's other edit forms. No partial-patch
    semantics, to keep this simple."""

    tools_release: str = ""
    functional_agent: FunctionalAgentScope = FunctionalAgentScope()
    technical_agent: TechnicalAgentScope = TechnicalAgentScope()
    updated_by: str
