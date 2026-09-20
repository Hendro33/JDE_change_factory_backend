"""
Read-model for one agent's DEFINITION, as parsed live from its EXISTING
.claude/agents/*.md file, plus the runtime options the driver that
invokes it (if any) already hardcodes. Nothing here is a new source of
truth -- see services/agent_registry_service.py, which only reads.
"""

from __future__ import annotations

from typing import Optional

from .base import ApiModel


class AgentRuntimeConfig(ApiModel):
    driver: str
    # None means "not set by the driver -- falls through to the Claude
    # Agent SDK's own default," never a fabricated model name.
    model: Optional[str] = None
    permission_mode: str
    max_turns: int
    allowed_tools: list[str] = []


class AgentDefinition(ApiModel):
    name: str
    description: str = ""
    declared_tools: list[str] = []
    # Content hash of the .md file, computed at read time -- the
    # smallest honest stand-in for design doc Section 14.3's "every
    # production-used agent carries a version identifier" that doesn't
    # require building a real versioning/release workflow.
    version: str
    file_updated_at: str
    # None means no api_service driver currently invokes this agent
    # (e.g. functional-agent, still invoked directly via Claude Code
    # today, not yet orchestrated from this service).
    runtime: Optional[AgentRuntimeConfig] = None
