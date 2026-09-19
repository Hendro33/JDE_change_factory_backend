"""
EnhancementRun -- this API's own bookkeeping for an in-flight or
completed Receive -> Improve -> Check pipeline run against one
ChangeRequest.

This is NOT a parallel implementation of the agents: it never decides
anything the agents decide (quality, business impact, complexity). It
only records what actually happened -- which stage is running, and the
JSON summary the orchestration itself produced -- the same relationship
change_service.py already has to backlog.py's records (read and
present, never re-derive).
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel
from .change import BusinessImpact, UserStory

ProcessingStage = Literal["receiving", "improving", "checking", "done", "failed"]
CheckOutcome = Literal["proposed_to_backlog", "needs_revision", "needs_human_input"]


class EnhancementRun(ApiModel):
    request_id: str
    stage: ProcessingStage
    started_at: str
    updated_at: str
    user_story: Optional[UserStory] = None
    business_impact: Optional[BusinessImpact] = None
    rough_complexity_signal: Optional[str] = None
    check_outcome: Optional[CheckOutcome] = None
    failed_criteria: list[str] = []
    error: Optional[str] = None
    backlog_story_id: Optional[str] = None
