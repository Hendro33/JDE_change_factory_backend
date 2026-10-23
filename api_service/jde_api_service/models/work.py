"""My Work: what needs the signed-in person, from the canonical lifecycle."""

from __future__ import annotations

from .base import ApiModel
from .change import Change


class MyWork(ApiModel):
    # Stories whose next action is owned by one of this person's roles.
    needs_you: list[Change] = []
    # Stories waiting on another person (not on JADE, not on this person).
    waiting_on_others: list[Change] = []
    # Stories JADE itself is working on right now.
    jade_working: list[Change] = []
    # User stories in progress (requests that are not a story yet excluded).
    in_progress_count: int = 0
    # Incoming requests JADE has not turned into a user story yet.
    requests_count: int = 0
    completed_this_month: list[Change] = []
    roles: list[str] = []
