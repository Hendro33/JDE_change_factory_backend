"""
Persistence for the per-customer EngagementScope. JsonFileStore-backed,
keyed by customer_id: one document per customer, since the customer's
whole scope IS the document.

This is the record mcp_server's execution gate reads (scope.py's
load_company_scope, pointed at this same directory via
JDE_COMPANY_SCOPE_DIR) -- what an Admin saves here is exactly what is
enforced. The document is stored snake_case (by_alias=False) and that
file format is the contract between the two packages.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.engagement_scope import EngagementScope, EngagementScopeUpdate, SpikeExperiment
from ..persistence.json_file_store import JsonFileStore
from ..persistence.revisions import next_revision


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _spike_key(s: SpikeExperiment) -> tuple:
    return (s.capability_id, s.capability_revision, s.application.upper(), s.version.upper(),
            s.option.upper(), s.environment, s.expires_at)


class EngagementScopeService:
    def __init__(self, directory: str) -> None:
        self._store = JsonFileStore(directory)

    def _load(self, customer_id: str) -> Optional[EngagementScope]:
        doc = self._store.get(customer_id)
        if not doc:
            return None
        doc.setdefault("revision", 1)  # saved before revisions existed
        return EngagementScope.model_validate(doc)

    def get_for_customer(self, customer_id: str) -> Optional[EngagementScope]:
        return self._load(customer_id)

    def upsert(self, customer_id: str, payload: EngagementScopeUpdate, actor: str) -> EngagementScope:
        with self._store.locked():
            existing = self._load(customer_id)
            revision = next_revision(existing.revision if existing else None, payload.expected_revision)
            now = _now()

            environment = payload.environment.model_copy()
            prev_env = existing.environment if existing else None
            if environment.isolation_confirmed:
                if prev_env and prev_env.isolation_confirmed:
                    environment.isolation_confirmed_by = prev_env.isolation_confirmed_by
                    environment.isolation_confirmed_at = prev_env.isolation_confirmed_at
                else:
                    environment.isolation_confirmed_by = actor
                    environment.isolation_confirmed_at = now
            else:
                environment.isolation_confirmed_by = None
                environment.isolation_confirmed_at = None

            # Approval stamps on spike experiments are server-side: an
            # unchanged experiment keeps its original approver; a new or
            # edited one is approved by whoever saves it now.
            previous = {_spike_key(s): s for s in (existing.functional_agent.spike_experiments if existing else [])}
            spikes = []
            for s in payload.functional_agent.spike_experiments:
                prior = previous.get(_spike_key(s))
                stamped = s.model_copy(update={
                    "approved_by": prior.approved_by if prior else actor,
                    "approved_at": prior.approved_at if prior else now,
                })
                spikes.append(stamped)
            functional = payload.functional_agent.model_copy(update={"spike_experiments": spikes})

            scope = EngagementScope(
                customer_id=customer_id,
                tools_release=payload.tools_release,
                environment=environment,
                functional_agent=functional,
                technical_agent=payload.technical_agent,
                approval_policy=payload.approval_policy,
                mechanisms_allowed=payload.mechanisms_allowed,
                test_scope=payload.test_scope,
                revision=revision,
                updated_at=now,
                updated_by=actor,
            )
            self._store.put(customer_id, scope.model_dump(mode="json", by_alias=False))
            return scope
