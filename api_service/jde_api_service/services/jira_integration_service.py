"""
Persistence for the per-company JiraIntegrationConfig -- see
models/jira_integration.py for why this is company-scoped and why the
API token is never part of what this module stores. SQLite-backed (the
jira_integrations table, persistence/migrations.py) -- durable storage
that survives restarts and redeploys, replacing the earlier
JsonFileStore-per-customer-directory version. One row per company,
same convention EngagementScopeService's own per-customer document
follows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..models.jira_integration import JiraIntegrationConfig, JiraIntegrationConfigUpdate
from ..persistence.db import connection
from ..persistence.revisions import next_revision
from .jira_gateway import normalize_jira_base_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_row(row) -> JiraIntegrationConfig:
    return JiraIntegrationConfig(
        customer_id=row["company_id"], base_url=row["base_url"], project_key=row["project_key"],
        pickup_status=row["pickup_status"], post_pickup_status=row["post_pickup_status"],
        jade_id_field=row["jade_id_field"], request_type_field=row["request_type_field"],
        revision=row["revision"], updated_at=row["updated_at"], updated_by=row["updated_by"],
    )


class JiraIntegrationService:
    def get_for_customer(self, customer_id: str) -> Optional[JiraIntegrationConfig]:
        with connection() as conn:
            row = conn.execute(
                "SELECT * FROM jira_integrations WHERE company_id = ?", (customer_id,)
            ).fetchone()
        return _from_row(row) if row is not None else None

    def upsert(self, customer_id: str, payload: JiraIntegrationConfigUpdate, actor: str) -> JiraIntegrationConfig:
        """Raises jira_gateway.InvalidJiraBaseUrl (a ValueError) if
        base_url isn't a bare site URL -- e.g. a project/queue/issue
        link pasted in by mistake. The router turns that into a 422.
        Raises RevisionConflict/RevisionRequired on a stale or missing
        expected_revision (see persistence/revisions.py)."""
        base_url = normalize_jira_base_url(payload.base_url)
        with connection() as conn:
            # IMMEDIATE takes the write lock now, so the revision check and
            # the write below cannot interleave with another save.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM jira_integrations WHERE company_id = ?", (customer_id,)
            ).fetchone()
            revision = next_revision(row["revision"] if row else None, payload.expected_revision)
            config = JiraIntegrationConfig(
                customer_id=customer_id,
                base_url=base_url,
                project_key=payload.project_key,
                pickup_status=payload.pickup_status,
                post_pickup_status=payload.post_pickup_status,
                jade_id_field=payload.jade_id_field,
                request_type_field=payload.request_type_field,
                revision=revision,
                updated_at=_now(),
                updated_by=actor,
            )
            conn.execute(
                "INSERT INTO jira_integrations (company_id, base_url, project_key, pickup_status, "
                "post_pickup_status, jade_id_field, request_type_field, updated_at, updated_by, revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(company_id) DO UPDATE SET base_url=excluded.base_url, project_key=excluded.project_key, "
                "pickup_status=excluded.pickup_status, post_pickup_status=excluded.post_pickup_status, "
                "jade_id_field=excluded.jade_id_field, request_type_field=excluded.request_type_field, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by, revision=excluded.revision",
                (
                    customer_id, config.base_url, config.project_key, config.pickup_status,
                    config.post_pickup_status, config.jade_id_field, config.request_type_field,
                    config.updated_at, config.updated_by, config.revision,
                ),
            )
        return config
