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
from .domain_review import ConversationTurn

ArchitectureRunStage = Literal["analyzing", "done", "failed"]


class ArchitectAnalysisVersion(ApiModel):
    """One completed Architecture Review run's reasoning, kept forever.

    Same "evidence, not a status flag" convention as domain_review.py's
    StoryVersion: complete() below appends here on every run instead of
    overwriting architect_decision/implementation_spec in place, so a
    re-analysis (whether from the manual retrigger or from a
    recommend_reanalysis conversation turn) never silently discards the
    Architect's prior reasoning.
    """

    architect_decision: ArchitectDecision
    implementation_spec: ImplementationSpecification
    note: str = ""
    captured_at: str


class ArchitectureReviewRun(ApiModel):
    story_id: str
    stage: ArchitectureRunStage
    started_at: str
    updated_at: str
    # "Current" reasoning -- kept for backward compatibility with every
    # existing caller that reads these two fields directly. Always equal
    # to history[-1]'s architect_decision/implementation_spec once
    # history is non-empty.
    architect_decision: Optional[ArchitectDecision] = None
    implementation_spec: Optional[ImplementationSpecification] = None
    error: Optional[str] = None
    history: list[ArchitectAnalysisVersion] = []
    # "Ask Jade about this solution" -- same ConversationTurn model
    # domain_review.py uses for "Ask Jade about this requirement", reused
    # rather than duplicated. See conversation_driver.ask_about_solution
    # for how the Architect-backed answers populate this.
    conversation: list[ConversationTurn] = []


class AskAboutSolutionInput(ApiModel):
    asked_by: str
    question: str
