"""The one authoritative revision of a user story, for every view and agent.

A story is written by the agents (the enhancement run), may be edited by its
Domain Owner and revised by the Reviewer Agent in User Story Review (the
domain review's version history), and may later be refined from process
findings (story_revisions). Every screen and every agent must see the same
revision: the most recent of these. Earlier revisions stay in their own
histories; nothing is overwritten.
"""

from __future__ import annotations

from typing import Optional

from ..models.change import UserStory


def authoritative(base: Optional[UserStory], review, company_id: str, story_id: str) -> Optional[UserStory]:
    candidates: list[tuple[str, UserStory]] = []
    if review is not None and getattr(review, "history", None):
        last = review.history[-1]
        candidates.append((last.captured_at or "", last.user_story))
    try:
        from ..process import refinement

        refined, at = refinement.latest_story_at(company_id, story_id)
        if refined is not None:
            candidates.append((at or "", refined))
    except Exception:  # noqa: BLE001 -- no refinement store: nothing refined
        pass
    if not candidates:
        return base
    return max(candidates, key=lambda c: c[0])[1]
