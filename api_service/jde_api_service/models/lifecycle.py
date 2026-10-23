"""Wire models for the canonical story lifecycle (services/lifecycle.py derives them)."""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel
from .delivery_view import OutcomeCard, WorkItem
from .issues import Issue, WorkLine

Phase = Literal["understand", "story_review", "solutioning", "solution_review", "delivery", "validation", "release",
                "done"]
PHASES: tuple[Phase, ...] = ("understand", "story_review", "solutioning", "solution_review", "delivery",
                             "validation", "release", "done")
PHASE_LABELS: dict[str, str] = {
    "understand": "Understand", "story_review": "Story Review", "solutioning": "Solutioning",
    "solution_review": "Solution Review", "delivery": "Delivery", "validation": "Validation",
    "release": "Release", "done": "Done",
}

Health = Literal["in_progress", "waiting_decision", "waiting", "blocked", "failed", "done", "closed"]
HEALTH_LABELS: dict[str, str] = {
    "in_progress": "In progress", "waiting_decision": "Waiting for decision", "waiting": "Waiting",
    "blocked": "Blocked", "failed": "Needs attention", "done": "Delivered", "closed": "Closed",
}

# Who owns a next action. Roles are Jade's real roles (models/auth.py);
# "jade" means Jade itself is doing the work -- nobody needs to act.
Owner = Literal["jade", "domain_owner", "product_manager", "cnc_operator", "admin", "none"]
OWNER_LABELS: dict[str, str] = {
    "jade": "JADE", "domain_owner": "Domain Owner", "product_manager": "Application Manager",
    "cnc_operator": "CNC", "admin": "Administrator", "none": "",
}

Outcome = Literal["delivered", "rejected", "resolved_without_change"]
TECHNICAL_ROUTES = {"Technical Agent", "Mixed"}


class NextAction(ApiModel):
    kind: Literal["decision", "task", "none"]
    # One sentence in business language -- what has to happen next.
    summary: str
    owner: Owner = "none"
    owner_label: str = ""
    # A stable identifier the UI maps to the control that performs it.
    action: Optional[str] = None
    # Which Story Workspace tab holds the control.
    # "outcome" is the business view's Progress and outcome.
    tab: Literal["overview", "story", "solution", "delivery", "evidence", "technical", "outcome"] = "overview"
    # What happens if it's done -- shown on the decision card.
    effect: str = ""


class LifecycleStep(ApiModel):
    id: str
    label: str
    state: Literal["done", "current", "todo", "failed", "skipped"]
    detail: str = ""


class PastApproval(ApiModel):
    by: Optional[str] = None
    at: Optional[str] = None
    expires_at: Optional[str] = None
    superseded_at: Optional[str] = None
    reason: str = ""


class Authorisation(ApiModel):
    """The CURRENT state of the exact change's authorisation, from the same
    checks the delivery gate runs (services/authorisation.py). Every screen
    shows this, never the bare approval record; earlier approvals are history."""
    state: Literal["none", "pending", "valid", "used", "expired", "invalidated", "rejected"]
    label: str
    detail: str = ""
    by: Optional[str] = None
    at: Optional[str] = None
    expires_at: Optional[str] = None
    renewable: bool = False
    history: list[PastApproval] = []


class AutomationPlan(ApiModel):
    """What JADE will do by itself next on this story (services/automation.py):
    scheduled (it will start at due_at), capped (it stopped retrying and a
    person takes over), or off (routine automation is switched off here)."""
    kind: Literal["intake", "solutioning"]
    state: Literal["scheduled", "capped", "off"]
    reason: str
    label: str
    due_at: Optional[str] = None
    signature: str = ""


class Lifecycle(ApiModel):
    phase: Phase
    phase_label: str
    phase_index: int
    health: Health
    health_label: str
    next_action: NextAction
    outcome: Optional[Outcome] = None
    # The delivery progression (build / validation / release / production),
    # derived from the same records -- empty before delivery is authorised.
    delivery_steps: list[LifecycleStep] = []
    # Short, meaningful open items (never logs): shown under "Risks / open
    # questions" on the story.
    open_items: list[str] = []
    route: Optional[str] = None
    # Every open point with its owner (services/issues.py), and the at most
    # five lines of "What needs to happen" -- the next action first. The
    # next action itself already reflects the current blockers.
    issues: list[Issue] = []
    what_next: list[WorkLine] = []
    # The real division of the delivery work (services/work_items.py), and at
    # the end the outcome with its three outcomes kept apart.
    work_items: list[WorkItem] = []
    outcome_card: Optional[OutcomeCard] = None
    handoff_authorised: bool = False
    authorisation: Optional[Authorisation] = None
    # When the current step began: the latest recorded lifecycle event
    # (services/timing.py). For a finished story, when it finished.
    since: Optional[str] = None
    # While JADE works: what it is doing right now, from the run's own reads.
    activity: str = ""
    # Routine work JADE starts by itself (redesign step 4); None = nothing due.
    automation: Optional[AutomationPlan] = None
    # A finished story whose record lacks a checkpoint the current workflow
    # requires (audit 23 Oct, F00/F07): stated, never invented or silently
    # reopened. The Application Manager may reopen it deliberately.
    completion_gaps: list[str] = []
    # Controls that act on JD Edwards or start an agent: whether the backend
    # accepts them now, and why not (services/eligibility.py).
    allowed_actions: list[str] = []
    action_blockers: dict[str, str] = {}


