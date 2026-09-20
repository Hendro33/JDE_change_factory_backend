"""
AgentRun -- a small, append-only execution-history entry for one agent
invocation (started/done/failed), across all customers. Exists only to
answer "what has this agent actually been doing lately" on Admin >
Agents (design doc Section 14.3's spirit: "capture outputs... as they
happen, not reconstructed from memory later"). It is NOT a replacement
for EnhancementRun/ArchitectureReviewRun (the per-story, current-state
sidecars those already are), and it never decides anything an agent or
a driver decides.

Deliberately cross-customer, not customer-scoped: which agent ran, how
often, and whether it succeeded is a fact about the AGENT, not about a
customer's data -- consistent with agent definitions themselves being
shared/global configuration (see agent_registry_service.py). Where a
customer_id is cheaply available at the call site it is still recorded,
for optional filtering later, but it is never required.

Each entry is written once at start and updated in place at its own
run_id as it finishes -- unlike EnhancementRun/ArchitectureReviewRun,
a NEW run_id is used every time, so history isn't overwritten.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

AgentRunStage = Literal["started", "done", "failed"]


class AgentRun(ApiModel):
    run_id: str
    agent_name: str  # matches a .claude/agents/*.md `name:` field
    driver: str  # orchestration_driver / review_driver / architecture_driver
    story_id: str
    customer_id: Optional[str] = None
    stage: AgentRunStage
    started_at: str
    updated_at: str
    error: Optional[str] = None
    # Content-hash of the .md file at invocation time, where known --
    # see agent_registry_service.compute_agent_version.
    agent_version: Optional[str] = None
