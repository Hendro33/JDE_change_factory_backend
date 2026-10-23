"""My Work: what needs the signed-in person, from the canonical lifecycle.

One service computes every section and every figure (register M6/N1): My
Work, the Application Manager home and Insights show the same numbers with
the same definitions."""

from __future__ import annotations

from typing import Optional

from .base import ApiModel
from .change import Change
from .issues import Issue


class QuestionItem(ApiModel):
    """One open question (or decision) owned by one of this person's roles."""
    change_id: str
    title: str
    phase_label: str = ""
    domain_id: Optional[str] = None
    since: Optional[str] = None
    issue: Issue


class StuckItem(ApiModel):
    change: Change
    reason: str


class DomainLine(ApiModel):
    domain_id: Optional[str] = None
    name: str = ""
    in_flight: int = 0
    oldest_waiting_days: Optional[int] = None
    waiting_on: dict[str, int] = {}


class Figure(ApiModel):
    key: str
    label: str
    value: Optional[float] = None
    display: str = ""
    definition: str = ""
    link: str = ""


class MyWork(ApiModel):
    # Stories whose next action is owned by one of this person's roles
    # (a Domain Owner only for stories in their own domains). Stories whose next
    # action is answering a question appear as question rows instead.
    needs_you: list[Change] = []
    # Every open question or decision owned by one of this person's roles,
    # on any story -- not only a story's single next action (register C3).
    questions: list[QuestionItem] = []
    # Domain Owner: the open stories in their domains, one line each.
    your_requests: list[Change] = []
    # Application Manager: stuck work, with the reason.
    stuck: list[StuckItem] = []
    # Application Manager: per business domain, what is in flight.
    per_domain: list[DomainLine] = []
    # Stories waiting on another person (not on JADE, not on this person).
    waiting_on_others: list[Change] = []
    # Stories JADE itself is working on right now.
    jade_working: list[Change] = []
    # User stories in progress (requests that are not a story yet excluded).
    in_progress_count: int = 0
    # Incoming requests JADE has not turned into a user story yet.
    requests_count: int = 0
    completed_this_month: list[Change] = []
    # "Things need you": needs_you rows plus question rows.
    attention_count: int = 0
    # The one figure strip (decision X1), the same everywhere.
    figures: list[Figure] = []
    roles: list[str] = []
