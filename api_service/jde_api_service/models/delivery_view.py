"""
Wire models for the reference journey (usability redesign, step 2):

  * WorkItem    -- one piece of delivery work with its owner, dependencies and
                   progress: an agent's configuration item, a person's item, a
                   technical package milestone, a hand-off to a named
                   implementer, the business acceptance, the release.
  * OutcomeCard -- at the end, what changed, what was verified and what the
                   business does now, with the three outcomes kept apart:
                   verified in DEV, accepted by the business, handed over for
                   release.

Read-only projections of the authoritative records; nothing here approves,
refuses or executes anything.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

WorkOwnerKind = Literal["agent", "person", "implementer", "cnc", "business", "release", "jade"]
WorkState = Literal["todo", "in_progress", "done", "failed", "blocked", "not_needed"]


class WorkItem(ApiModel):
    id: str
    title: str
    # Who does it, as a kind (for the icon) and in words.
    owner_kind: WorkOwnerKind
    who: str
    state: WorkState = "todo"
    detail: str = ""
    depends_on: list[str] = []
    # The control that progresses it, where Jade has one (NextAction ids).
    action: Optional[str] = None
    # Which group it belongs to, in the order the work happens.
    group: Literal["configuration", "development", "handoff", "verification", "acceptance", "release"] = "configuration"
    # For a hand-off item: the recorded completion.
    completed_by: str = ""
    completed_at: str = ""
    evidence_reference: str = ""


class OutcomeStep(ApiModel):
    done: bool = False
    by: str = ""
    at: str = ""
    detail: str = ""


class OutcomeCard(ApiModel):
    headline: str = ""
    what_changed: list[str] = []
    verified: list[str] = []
    # The three outcomes, never implied by one another.
    dev_verified: OutcomeStep = OutcomeStep()
    business_accepted: OutcomeStep = OutcomeStep()
    released: OutcomeStep = OutcomeStep()
    # A recorded "not accepted", with the reason, until it is resolved.
    not_accepted: Optional[OutcomeStep] = None
    what_to_do_now: str = ""
    # Each acceptance criterion with how far it is verified (register C12). The
    # DEV test covers the change as a whole; that is said, not claimed per criterion.
    criteria: list[dict] = []
    jira_key: str = ""
    jira_url: str = ""
