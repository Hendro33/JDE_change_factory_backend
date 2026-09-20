"""
ArchitectureReviewRun -- this API's own bookkeeping for an in-flight or
completed Architecture Review run against one approved backlog story.

Same relationship to the Architect's real actions as EnhancementRun has
to Receive/Improve/Check (enhancement_run.py's own docstring): this
NEVER decides anything the Architect decides, and it is NOT the
authoritative exact-change/approval record -- that stays exactly where
it already is, in mcp_server's approval.py (propose_change/
approve_change/reject_change, unmodified). This sidecar exists only
because propose_change's own storage has no fields for the Architect's
REASONING (recommended route, alternatives considered, objects
affected, rollback strategy, the Implementation Specification) --
propose_change only ever stored the exact operation itself. This holds
that reasoning, keyed to the same story_id, so it can be presented
alongside the authoritative exact-change record without becoming a
second copy of it.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel
from .change import ArchitectDecision, ImplementationSpecification

ArchitectureRunStage = Literal["analyzing", "done", "failed"]


class ArchitectureReviewRun(ApiModel):
    story_id: str
    stage: ArchitectureRunStage
    started_at: str
    updated_at: str
    architect_decision: Optional[ArchitectDecision] = None
    implementation_spec: Optional[ImplementationSpecification] = None
    error: Optional[str] = None
