"""
Read-model for the Functional Agent Capability Catalogue (design
update Section 2), sourced live from the EXISTING, version-controlled
capability_catalog.json at the repo root -- see
mcp_server/jde_mcp_server/capability_catalog.py, which this module's
service wraps read-only, the same relationship agent_registry.py's own
docstring describes for agent definitions. Nothing here is a new
source of truth, and nothing here can write to the catalogue -- "the
agent cannot certify its own capabilities" (Section 2) is enforced by
there being no write path anywhere in this API either.

Field groups below stay close to the catalogue's own free-form JSON
shape (dicts, not a rigid nested schema) -- the design update's field
list is deliberately broad ("bounded, repeatable operation," not a
fixed form), and over-modelling it here would just be a second place
that schema has to be kept in sync.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

CapabilityStatus = Literal["validated", "needs_spike", "restricted", "human_implementation", "suspended"]


class CapabilityValidation(ApiModel):
    status: CapabilityStatus
    # Kept as two SEPARATE fields, never collapsed into status alone
    # (design update Section 2: "a technically validated action can
    # still be Restricted").
    technical_validation: str = ""
    policy_restriction: str = ""
    evidence_references: list[str] = []
    validation_date: Optional[str] = None
    approver: Optional[str] = None
    revalidation_triggers: list[str] = []


class Capability(ApiModel):
    capability_id: str
    revision: str
    priority: int
    family: Optional[str] = None
    identity: dict = {}
    target: dict = {}
    compatibility: dict = {}
    execution: dict = {}
    scope: dict = {}
    risk: dict = {}
    preconditions: dict = {}
    verification: dict = {}
    recovery: dict = {}
    delivery: dict = {}
    validation: CapabilityValidation


class CapabilityCatalog(ApiModel):
    catalog_revision: str
    capabilities: list[Capability] = []
