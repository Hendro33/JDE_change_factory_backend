"""
Small company-level settings stored as one JSON value per (company, key)
in the SQLite company_settings table -- revisioned (persistence/
revisions.py) and attributed to the authenticated user who saved them.

Used for settings that are too small to justify their own table:
dashboard alert thresholds, the exact-change approval policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from ..persistence.revisions import next_revision


@dataclass(frozen=True)
class StoredSetting:
    value: dict[str, Any]
    revision: int
    updated_at: str
    updated_by: str


class CompanySettingsService:
    def get(self, company_id: str, key: str) -> Optional[StoredSetting]:
        with connection() as conn:
            row = conn.execute(
                "SELECT value, revision, updated_at, updated_by FROM company_settings "
                "WHERE company_id = ? AND key = ?",
                (company_id, key),
            ).fetchone()
        if row is None:
            return None
        return StoredSetting(json.loads(row["value"]), row["revision"], row["updated_at"], row["updated_by"])

    def put(
        self, company_id: str, key: str, value: dict[str, Any], expected_revision: Optional[int], actor: str
    ) -> StoredSetting:
        """Raises RevisionConflict/RevisionRequired on a stale save."""
        now = datetime.now(timezone.utc).isoformat()
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM company_settings WHERE company_id = ? AND key = ?", (company_id, key)
            ).fetchone()
            revision = next_revision(row["revision"] if row else None, expected_revision)
            conn.execute(
                "INSERT INTO company_settings (company_id, key, value, revision, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(company_id, key) DO UPDATE SET value=excluded.value, revision=excluded.revision, "
                "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                (company_id, key, json.dumps(value), revision, now, actor),
            )
        return StoredSetting(value, revision, now, actor)
