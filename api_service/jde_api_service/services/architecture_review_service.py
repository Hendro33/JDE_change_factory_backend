"""
Persistence for ArchitectureReviewRun records. Same file-backed
JsonFileStore-per-directory pattern as enhancement_run_service.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.architecture_review import ArchitectureReviewRun, ArchitectureRunStage
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArchitectureReviewService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def get(self, story_id: str) -> Optional[ArchitectureReviewRun]:
        doc = self._store.get(story_id)
        return ArchitectureReviewRun.model_validate(doc) if doc else None

    def start(self, story_id: str) -> ArchitectureReviewRun:
        now = _now()
        run = ArchitectureReviewRun(story_id=story_id, stage="analyzing", started_at=now, updated_at=now)
        self._save(run)
        return run

    def complete(self, story_id: str, *, architect_decision, implementation_spec) -> None:
        run = self.get(story_id)
        if run is None:
            return
        run.stage = "done"
        run.updated_at = _now()
        run.architect_decision = architect_decision
        run.implementation_spec = implementation_spec
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

    def _save(self, run: ArchitectureReviewRun) -> None:
        self._store.put(run.story_id, run.model_dump(mode="json", by_alias=False))
