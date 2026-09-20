"""
Persistence for AgentRun history. Append-only, unlike every other
*_service.py's "one document per id, overwritten on the next call"
pattern (EnhancementRunService, ArchitectureReviewService): each start()
call creates a NEW document under its own run_id, so a run's history
stays visible even after a later run for the same story begins.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models.agent_run import AgentRun, AgentRunStage
from ..persistence.json_file_store import JsonFileStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRunService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def start(
        self,
        *,
        agent_name: str,
        driver: str,
        story_id: str,
        customer_id: Optional[str] = None,
        agent_version: Optional[str] = None,
    ) -> AgentRun:
        now = _now()
        run = AgentRun(
            run_id=f"{story_id}-{agent_name}-{uuid.uuid4().hex[:8]}",
            agent_name=agent_name,
            driver=driver,
            story_id=story_id,
            customer_id=customer_id,
            stage="started",
            started_at=now,
            updated_at=now,
            agent_version=agent_version,
        )
        self._save(run)
        return run

    def complete(self, run_id: str) -> None:
        self._set_stage(run_id, "done")

    def fail(self, run_id: str, error: str) -> None:
        run = self._get(run_id)
        if run is None:
            return
        run.stage = "failed"
        run.error = error
        run.updated_at = _now()
        self._save(run)

    def _set_stage(self, run_id: str, stage: AgentRunStage) -> None:
        run = self._get(run_id)
        if run is None:
            return
        run.stage = stage
        run.updated_at = _now()
        self._save(run)

    def _get(self, run_id: str) -> Optional[AgentRun]:
        doc = self._store.get(run_id)
        return AgentRun.model_validate(doc) if doc else None

    def list_all(self) -> list[AgentRun]:
        return [AgentRun.model_validate(doc) for doc in self._store.list_all()]

    def list_for_agent(self, agent_name: str, limit: int = 20) -> list[AgentRun]:
        runs = [r for r in self.list_all() if r.agent_name == agent_name]
        runs.sort(key=lambda r: r.started_at, reverse=True)
        return runs[:limit]

    def _save(self, run: AgentRun) -> None:
        self._store.put(run.run_id, run.model_dump(mode="json", by_alias=False))
