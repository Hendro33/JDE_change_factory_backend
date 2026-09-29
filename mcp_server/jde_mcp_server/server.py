"""
JDE MCP server -- the agents' governed tool set: backlog hand-off, exact-
change proposals, capability status, design baselines and evidence.

Jade's agents do NOT use this stdio server: they get the same tools in-process,
bound to their run's story (api_service/jde_api_service/ai/project_tools.py).
This module remains for developers who want to call the tools from a Claude
Code session in this repository (.mcp.json):
    python -m jde_mcp_server.server

None of these tools writes to JD Edwards; approvals are given by people in
Jade and enforced by approval.py and the delivery gate.
"""

from __future__ import annotations

try:
    # mcp>=2.0 renamed FastMCP to MCPServer; the .tool()/.run() surface
    # used below is unchanged, so this file works against either.
    from mcp.server.mcpserver import MCPServer as _MCPServerClass
except ImportError:  # mcp<2
    from mcp.server.fastmcp import FastMCP as _MCPServerClass

from .capability_catalog import get_capability as _get_capability, CapabilityError
from .evidence import capture_evidence as _capture_evidence, verify_chain as _verify_chain
from .backlog import (
    propose_to_backlog as _propose_to_backlog,
    get_approved_story as _get_approved_story,
    resolve_without_change as _resolve_without_change,
    StoryNotApproved,
)
from .approval import propose_change as _propose_change, ChangeApprovalError
from .design_baseline import get_design_baseline as _get_design_baseline

mcp = _MCPServerClass("jde-change-factory")


# ---------------------------------------------------------------------
# Phase 1 -> Phase 2 handoff (Section 3.5). Callable by the Check Agent
# only, and only on a story that already passed the full Section 5.2
# checklist -- this tool does not re-check quality itself.
# ---------------------------------------------------------------------

@mcp.tool()
def propose_to_backlog(story_id: str, user_story: str, business_impact: dict, rough_complexity_signal: str, source: str = "") -> dict:
    """Record a quality-gated story in the backlog for human review
    (Section 3.5, Phase 2). business_impact should carry the five
    criteria from Section 3.6 (financial, operational reach, risk &
    compliance, strategic alignment, urgency), each with the evidence
    it was traced from. This does NOT approve the story -- an Application
    Manager and the domain's Domain Owner decide in Story Review."""
    return _propose_to_backlog(story_id, user_story, business_impact, rough_complexity_signal, source)


# ---------------------------------------------------------------------
# Phase 3 gate check (Section 3.5, Gate 2). The Architect calls this
# first; every other Phase 3 tool below also checks it independently.
# ---------------------------------------------------------------------

@mcp.tool()
def get_approved_story(story_id: str) -> dict:
    """Return the backlog record for story_id -- but only if a human
    has approved it (Section 3.5, Gate 2). Raises if the story doesn't
    exist, is still pending, or was rejected. The Architect calls this
    to fetch its input; the write and test-execution tools below also
    check independently, since discovery is unrestricted but writes and
    tests are not (Section 3.5)."""
    return _get_approved_story(story_id)


@mcp.tool()
def resolve_without_change(story_id: str, resolution_note: str, resolved_by: str) -> dict:
    """Terminal route (Section 15.6): the Architect determined existing
    JDE functionality or configuration already satisfies the
    requirement, so no Functional or Technical Agent action is needed.
    JDE is never touched. Requires an approved story_id -- this ends a
    story that went through Phase 2, it doesn't bypass Phase 2."""
    return _resolve_without_change(story_id, resolution_note, resolved_by)


@mcp.tool()
def propose_change(story_id: str, operation: dict, capability_id: str, environment: str = "DEV") -> dict:
    """Register the exact operation (e.g. {"tool": "set_processing_option",
    "application": ..., "version": ..., "option": ..., "value": ...})
    the Functional Agent intends to execute against an already-approved
    story (Section 15.3), and the catalogue capability it is exercising
    (capability_catalog.json's capability_id -- Functional Agent design
    update Section 5.2). Fails closed if capability_id is unknown, if
    'environment' isn't DEV, or if the story isn't linked to a company
    (the company is taken from the story's intake record, never from
    this call). Returns a
    pending change record with a change_id -- this does NOT approve
    anything. People approve it separately in Architecture
    Review; a person then applies it in DEV and Jade verifies it."""
    return _propose_change(story_id, operation, capability_id, environment)


# ---------------------------------------------------------------------
# Capability catalogue (Functional Agent design update, Section 2/5.3).
# Read-only, like discovery below -- an agent checks this BEFORE
# proposing anything, to distinguish what it can analyse/propose from
# what it is actually authorised and technically able to execute. This
# is advisory for the agent's own reasoning; propose_change and the
# delivery gate (approval.authorise_functional_delivery) enforce the real
# rules independently, so a stale or ignored read here changes nothing.
# ---------------------------------------------------------------------

@mcp.tool()
def get_capability_status(capability_id: str) -> dict:
    """Look up one capability's current status (validated / needs_spike
    / restricted / human_implementation / suspended), its separate
    technical_validation and policy_restriction notes, and its current
    revision. Call this before propose_change. An approved exact change is
    applied in DEV by an authorised person and verified by Jade (the
    recorded delivery route); a Restricted or Suspended capability is not
    delivered at all, and a Human Implementation capability is routed to
    Human Implementation instead of being proposed as an exact change."""
    cap = _get_capability(capability_id)
    if cap is None:
        raise CapabilityError(f"'{capability_id}' is not a registered capability -- see capability_catalog.json")
    return cap


# ---------------------------------------------------------------------
# No JDE access from the tool server. JDE research goes through the
# Architect's governed discovery tools (api_service discovery/, company-
# bound, validated before dispatch). An approved change reaches JDE only
# through the recorded delivery route: a person applies it, and Jade
# verifies it live through the customer's own connection (api_service
# delivery/). No agent tool writes to JDE or runs a test there.
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------

@mcp.tool()
def get_engagement_scope(story_id: str) -> dict:
    """What the story's company allows to be proposed (read-only): approved
    versions and configuration, approved tests, never-touch and protected
    categories. The gate re-checks every item itself."""
    from .scope import company_for_story, describe_scope

    return describe_scope(company_for_story(story_id))


@mcp.tool()
def capture_evidence(story_id: str, payload: dict) -> dict:
    """Write an immutable audit record for one stage of a story --
    what changed, what was tested, and (per Section 8.4) whatever is
    needed to roll the change back. Each entry is chained by hash to
    the one before it (Section 15.5) so tampering or reordering is
    detectable, not just theoretically excluded."""
    return _capture_evidence(story_id, payload)


@mcp.tool()
def verify_evidence_chain(story_id: str) -> dict:
    """Recomputes every evidence entry's hash from scratch and confirms
    the chain is intact (Section 15.5). Use this to actually check
    tamper-evidence rather than assume it -- e.g. before an Application
    Manager or CNC relies on a story's evidence package for sign-off."""
    return _verify_chain(story_id)


@mcp.tool()
def get_design_baseline(story_id: str) -> dict:
    """The Architect's instructions for this story together with the exact
    evidence manifest the design was based on (environment profile
    revision, observations, artifact revisions/checksums, documents,
    gaps). Call it before anything else. It is a snapshot: it does not
    authorise any write, and a status of needs_reassessment means the
    evidence changed after the design -- stop and send it back to the
    Architect. 'change' is the exact change this design revision proposed
    and its current approval state: execute only that change_id, and only
    when its status is approved. Re-validate every live precondition you
    rely on."""
    return _get_design_baseline(story_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
