"""
Persistence for EnhancementRun records. Same file-backed JsonFileStore
pattern as everything else in this service.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.enhancement_run import EnhancementRun, ProcessingStage
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnhancementRunService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get(self, request_id: str) -> Optional[EnhancementRun]:
        doc = self._store.get(request_id)
        return EnhancementRun.model_validate(doc) if doc else None

    def list_all(self) -> list[EnhancementRun]:
        return [EnhancementRun.model_validate(doc) for doc in self._store.list_all()]

    def start(self, request_id: str) -> EnhancementRun:
        now = _now()
        run = EnhancementRun(request_id=request_id, stage="receiving", started_at=now, updated_at=now)
        self._save(run)
        return run

    def set_stage(self, request_id: str, stage: ProcessingStage) -> None:
        run = self.get(request_id)
        if run is None:
            return
        run.stage = stage
        run.updated_at = _now()
        self._save(run)

    def complete(
        self,
        request_id: str,
        *,
        user_story,
        business_impact,
        rough_complexity_signal: Optional[str],
        check_outcome,
        failed_criteria: list[str],
        backlog_story_id: Optional[str],
    ) -> None:
        run = self.get(request_id)
        if run is None:
            return
        run.stage = "done"
        run.updated_at = _now()
        run.user_story = user_story
        run.business_impact = business_impact
        run.rough_complexity_signal = rough_complexity_signal
        run.check_outcome = check_outcome
        run.failed_criteria = failed_criteria
        run.backlog_story_id = backlog_story_id
        self._save(run)

    def fail(self, request_id: str, error: str) -> None:
        run = self.get(request_id)
        if run is None:
            return
        run.stage = "failed"
        run.updated_at = _now()
        run.error = error
        self._save(run)

    def _save(self, run: EnhancementRun) -> None:
        self._store.put(run.request_id, run.model_dump(mode="json", by_alias=False))
