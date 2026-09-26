"""Wire models for the canonical story lifecycle (services/lifecycle.py derives them)."""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

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
    "jade": "JADE", "domain_owner": "Domain Owner", "product_manager": "Product Owner",
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
    tab: Literal["overview", "story", "solution", "delivery", "evidence", "technical"] = "overview"
    # What happens if it's done -- shown on the decision card.
    effect: str = ""


class LifecycleStep(ApiModel):
    id: str
    label: str
    state: Literal["done", "current", "todo", "failed", "skipped"]
    detail: str = ""


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
    simulated: bool = False


