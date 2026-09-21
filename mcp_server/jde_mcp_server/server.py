"""
JDE MCP server -- the six-tool MVP set from Section 7.3 of the JDE
AI-Driven Change Factory design document.

Run it directly for a quick manual check:
    python -m jde_mcp_server.server

Or register it with Claude Code via the .mcp.json at the repo root,
which is already wired to launch this module over stdio.

IMPORTANT: this server exposes tools; it does NOT enforce the human
approval gate on writes. That gate is a Claude Code / Claude Agent SDK
PreToolUse hook (Section 8.1), configured separately in
.claude/hooks/approve_writes.py and wired up in .claude/settings.json.
Keeping the two separate matches the design: the MCP server describes
*what JDE can do*, the hook decides *whether this particular call is
allowed to happen right now*.
"""

from __future__ import annotations

try:
    # mcp>=2.0 renamed FastMCP to MCPServer; the .tool()/.run() surface
    # used below is unchanged, so this file works against either.
    from mcp.server.mcpserver import MCPServer as _MCPServerClass
except ImportError:  # mcp<2
    from mcp.server.fastmcp import FastMCP as _MCPServerClass

from .ais_client import client
from .capability_catalog import get_capability as _get_capability, CapabilityError
from .evidence import capture_evidence as _capture_evidence, verify_chain as _verify_chain
from .backlog import (
    propose_to_backlog as _propose_to_backlog,
    get_approved_story as _get_approved_story,
    resolve_without_change as _resolve_without_change,
    StoryNotApproved,
)
from .approval import propose_change as _propose_change, ChangeApprovalError

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
    it was traced from. This does NOT approve the story -- nothing
    downstream can act on it until a human runs backlog_review.py."""
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
    update Section 5.2). Fails closed if capability_id is unknown, or if
    'environment' isn't a confirmed-isolated DEV (scope.json). Returns a
    pending change record with a change_id -- this does NOT approve
    anything. A human approves it separately via backlog_review.py
    before set_processing_option will accept the matching change_id."""
    return _propose_change(story_id, operation, capability_id, environment)


# ---------------------------------------------------------------------
# Capability catalogue (Functional Agent design update, Section 2/5.3).
# Read-only, like discovery below -- an agent checks this BEFORE
# proposing anything, to distinguish what it can analyse/propose from
# what it is actually authorised and technically able to execute. This
# is advisory for the agent's own reasoning; propose_change/
# require_exact_change enforce the real gate independently, so a stale
# or ignored read here can never let an unauthorised write through.
# ---------------------------------------------------------------------

@mcp.tool()
def get_capability_status(capability_id: str) -> dict:
    """Look up one capability's current status (validated / needs_spike
    / restricted / human_implementation / suspended), its separate
    technical_validation and policy_restriction notes, and its current
    revision. Call this before propose_change -- if status isn't
    'validated', the write will not execute as an ordinary operation
    (see capability_catalog.require_executable), and a Restricted or
    Human Implementation capability should be routed there instead,
    not proposed at all."""
    cap = _get_capability(capability_id)
    if cap is None:
        raise CapabilityError(f"'{capability_id}' is not a registered capability -- see capability_catalog.json")
    return cap


# ---------------------------------------------------------------------
# Discovery (Section 7.2: proven, since Tools Release 9.1.4.6). NOT
# gated on story approval -- the Improve Agent (Phase 1) needs these
# long before any backlog approval exists, and Section 7.2 treats
# discovery as universally safe. The gate lives on the write and
# test-execution tools below, where an unapproved story could actually
# cause something to happen in JDE.
# ---------------------------------------------------------------------

@mcp.tool()
def get_object(object_name: str) -> dict:
    """Look up a JDE object (application, table, business view, etc.) by
    name. Read-only discovery -- safe to call freely, in any phase."""
    return client.get_object(object_name)


@mcp.tool()
def get_version(application: str, version: str) -> dict:
    """Look up a specific version of a JDE application. Read-only
    discovery -- safe to call freely, in any phase."""
    return client.get_version(application, version)


@mcp.tool()
def get_processing_options(application: str, version: str) -> dict:
    """Read the current processing option values for a version, via the
    AIS processingOption capability. Read-only -- safe to call freely,
    in any phase."""
    return client.get_processing_options(application, version)


# ---------------------------------------------------------------------
# Functional write (Section 7.3: the one validated write for the MVP)
# ---------------------------------------------------------------------

@mcp.tool()
def set_processing_option(story_id: str, change_id: str, application: str, version: str, option: str, value: str) -> dict:
    """Change a single processing option value on a named version, via a
    validated Form Service Request against the Processing Option
    Revisions form (Section 7.3, Appendix B).

    This is a WRITE and passes through several independent checks
    before it does anything: an approved story_id (Section 3.5, Gate
    2); an approved, exactly-matching change_id from propose_change
    (Section 15.3) -- the operation you pass here must byte-for-byte
    match what was approved, or this fails closed; a universal rule
    against writing to Oracle-owned XJDE/ZJDE template versions
    (Appendix B.4); and this engagement's specific scope file
    confirming the combination is authorised (Appendix D.2). It is also
    intercepted by the PreToolUse approval hook (Section 8.1) before it
    reaches this function. Do not rely on this tool alone to enforce
    any of these -- they are the actual controls; this function is
    just where they're all applied together.
    """
    return client.set_processing_option(story_id, change_id, application, version, option, value)


# ---------------------------------------------------------------------
# Runtime / evidence
# ---------------------------------------------------------------------

@mcp.tool()
def run_orchestration(story_id: str, change_id: str, name: str, payload: dict) -> dict:
    """Execute a pre-built acceptance-test Orchestration in DEV and
    return its structured result. Requires an approved story_id
    (Section 3.5, Gate 2) and that 'name' is the exact test named in
    the approved change record (Section 17.1) -- not just any test
    against any approved story."""
    return client.run_orchestration(story_id, change_id, name, payload)


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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
