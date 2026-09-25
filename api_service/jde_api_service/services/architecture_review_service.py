"""
Persistence for ArchitectureReviewRun records. Same file-backed
JsonFileStore-per-directory pattern as enhancement_run_service.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import uuid

from ..models.architecture_review import ArchitectAnalysisVersion, ArchitectureReviewRun, ArchitectureRunStage
from ..models.domain_review import ConversationTurn, ConversationTurnKind
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArchitectureReviewService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get(self, story_id: str) -> Optional[ArchitectureReviewRun]:
        doc = self._store.get(story_id)
        return ArchitectureReviewRun.model_validate(doc) if doc else None

    def list_all(self) -> list[ArchitectureReviewRun]:
        return [ArchitectureReviewRun.model_validate(doc) for doc in self._store.list_all()]

    def start(self, story_id: str) -> ArchitectureReviewRun:
        """A (re)start -- the manual retrigger, or the automatic run
        Gate 1 schedules. Preserves any existing history/conversation:
        a re-analysis must never wipe the Architect's prior reasoning
        or the solution conversation just because a new run began: only
        complete() (by appending) and append_conversation_turn change
        those, never a fresh run object here."""
        now = _now()
        existing = self.get(story_id)
        run = ArchitectureReviewRun(
            story_id=story_id,
            stage="analyzing",
            started_at=now,
            updated_at=now,
            architect_decision=existing.architect_decision if existing else None,
            implementation_spec=existing.implementation_spec if existing else None,
            history=existing.history if existing else [],
            conversation=existing.conversation if existing else [],
        )
        self._save(run)
        return run

    def complete(self, story_id: str, *, architect_decision, implementation_spec, note: str = "") -> None:
        run = self.get(story_id)
        if run is None:
            return
        now = _now()
        run.stage = "done"
        run.updated_at = now
        # "Current" fields, kept for backward compatibility -- and a new
        # append-only history entry, same evidence convention as
        # domain_review.py's StoryVersion, so a re-analysis (manual
        # retrigger or a recommend_reanalysis conversation turn) never
        # silently discards the Architect's prior reasoning.
        run.architect_decision = architect_decision
        run.implementation_spec = implementation_spec
        run.history.append(
            ArchitectAnalysisVersion(
                architect_decision=architect_decision,
                implementation_spec=implementation_spec,
                note=note,
                captured_at=now,
            )
        )
        self._save(run)

    def attach_baseline(self, story_id: str, baseline_id: str, baseline_sha256: str) -> None:
        run = self.get(story_id)
        if run is None or not run.history:
            return
        run.history[-1].baseline_id = baseline_id
        run.history[-1].baseline_sha256 = baseline_sha256
        self._save(run)

    def fail(self, story_id: str, error: str) -> None:
        run = self.get(story_id)
        if run is None:
            return
        run.stage = "failed"
        run.updated_at = _now()
        run.error = error
        self._save(run)

    def set_stage(self, story_id: str, stage: ArchitectureRunStage) -> None:
        run = self.get(story_id)
        if run is None:
            return
        run.stage = stage
        run.updated_at = _now()
        self._save(run)

    def append_conversation_turn(
        self,
        story_id: str,
        *,
        asked_by: str,
        question: str,
        answer: str,
        kind: ConversationTurnKind,
        identity_id: Optional[str] = None,
    ) -> Optional[ArchitectureReviewRun]:
        """"Ask Jade about this solution" -- same append-only evidence
        convention as domain_review_service.append_conversation_turn.
        proposed_user_story is always None here: the Architect never
        drafts an inline amendment the way Improve does, so a
        recommend_reanalysis turn only ever points back at the existing
        manual retrigger, never at a payload this method could apply."""
        run = self.get(story_id)
        if run is None:
            return None
        run.conversation.append(
            ConversationTurn(
                turn_id=f"CONV-{uuid.uuid4().hex[:8]}",
                asked_by=asked_by,
                question=question,
                answer=answer,
                kind=kind,
                proposed_user_story=None,
                asked_at=_now(),
                identity_id=identity_id,
            )
        )
        run.updated_at = _now()
        self._save(run)
        return run

    def _save(self, run: ArchitectureReviewRun) -> None:
        self._store.put(run.story_id, run.model_dump(mode="json", by_alias=False))
