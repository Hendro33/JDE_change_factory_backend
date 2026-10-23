"""
Wire models for the shared information model of the usability redesign:

  * AgentResult -- what an agent run concluded, in the same parts for every
    agent: a one-sentence conclusion, the changes it proposes (each value
    marked verified, not yet verified or proposed), the issues it leaves
    open, and the uncertainty that could change the decision. The full agent
    output is kept separately on the run record (ai_runs.output) for
    Administrators; this is the readable part.

  * Issue -- one open point with an owner: what it is, what it affects, who
    resolves it, how, and whether it blocks progress. Jade's response
    depends on the kind (services/issues.py).

  * WorkLine -- one line of "What needs to happen": who does what, with the
    control that does it where it can be done on the spot.

All of this is a read-only projection of the authoritative records. It never
changes a gate, an approval or an execution record, and it never decides
whether anything may run: the gates still do that, independently.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

# Who acts on an issue, in Jade's real roles (models/lifecycle.Owner).
IssueOwner = Literal["jade", "domain_owner", "product_manager", "cnc_operator", "admin", "none"]

# How Jade responds (review, change 4):
#   jade      -- a read or lookup problem Jade recovers itself
#   business  -- a business fact only the business can confirm
#   decision  -- a design choice for the decision-maker
#   setup     -- customer setup in Jade; one Administrator task per customer
#   delivery  -- a delivery failure, an uncertain outcome or a hand-off
IssueKind = Literal["jade", "business", "decision", "setup", "delivery"]

ValueStatus = Literal["verified", "not_yet_verified", "proposed"]


class ResultChange(ApiModel):
    """One change, as before -> after. An unknown current value is shown as
    unknown, never as "none"."""

    what: str
    current: str = ""
    proposed: str = ""
    status: ValueStatus = "proposed"
    # Evidence status per value (audit F03): "verified" speaks for the current
    # value only; the target can still be unknown, which is shown separately.
    target_known: bool = True


class ResultIssue(ApiModel):
    """An open point as the agent stated it (before Jade maps it to a role)."""

    title: str
    consequence: str = ""
    # jade | business | decision | administrator | delivery
    owner: str = "business"
    # The person or team in words, e.g. "Finance", "the requester".
    who: str = ""
    action: str = ""
    blocks: bool = False
    proposed_answer: str = ""


class AgentResult(ApiModel):
    conclusion: str = ""
    changes: list[ResultChange] = []
    issues: list[ResultIssue] = []
    uncertainty: list[str] = []
    # Story questions the Architect's discovery answered (question, answer,
    # evidence): they are retired with that answer instead of staying open.
    answered_questions: list[dict] = []
    # The business projection (audit F08): what changes in the business's work
    # and the outcome, without JD Edwards names, codes or delivery mechanics.
    business_summary: str = ""
    # True when Jade derived this from an older result that had no result
    # block (the agent did not write it in these parts).
    derived: bool = False


class IssueResolution(ApiModel):
    """The latest answer or resolution recorded on an issue."""

    issue_id: str
    issue_title: str = ""
    issue_kind: str = ""
    status: Literal["answered", "resolved"] = "answered"
    answer: str = ""
    by: str = ""
    by_user_id: Optional[str] = None
    at: str = ""


class Issue(ApiModel):
    # Stable across reads of the same records (a hash of kind and text).
    id: str
    kind: IssueKind
    title: str
    consequence: str = ""
    owner: IssueOwner = "none"
    # Who, in words: "Finance", "Domain Owner", "Administrator", "JADE".
    owner_label: str = ""
    # What resolves it, in words.
    action: str = ""
    # The control that resolves it, where Jade has one (same identifiers as
    # NextAction.action).
    action_id: Optional[str] = None
    blocks: bool = False
    proposed_answer: str = ""
    # Where it came from: the Architect's result, Jade's own checks of the
    # records, or the delivery gate.
    source: Literal["architect", "design_evidence", "design_quality", "delivery_gate", "lifecycle", "story"] = "architect"
    tab: Literal["overview", "story", "solution", "delivery", "evidence", "technical", "outcome"] = "solution"
    resolution: Optional[IssueResolution] = None


class WorkLine(ApiModel):
    """One line of "What needs to happen" (at most five per story)."""

    owner: IssueOwner = "none"
    who: str = ""
    text: str
    action: Optional[str] = None
    tab: Literal["overview", "story", "solution", "delivery", "evidence", "technical", "outcome"] = "overview"
    blocking: bool = False
    primary: bool = False
    # The open point this line restates (None for the next action itself),
    # so a screen that lists the open points does not say them twice.
    issue_id: Optional[str] = None
    issue_kind: Optional[str] = None
