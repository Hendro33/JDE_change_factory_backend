"""
Durable records for the Technical workflow: design approvals, Technical
Agent runs, and implementation package revisions.

Package revisions are immutable once stored. A new revision can only be
stored by a run that saw the previous one (compare-and-set on the latest
revision), so a delayed agent response can never overwrite -- or be stored
on top of -- a newer revision. Storing revision N+1 marks N superseded.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection


class StaleSubmission(RuntimeError):
    """The run's view of the package is out of date, or the run is no longer
    active: its result is discarded rather than stored over newer work."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Design approvals
# ---------------------------------------------------------------------
def add_design_approval(*, company_id: str, story_id: str, design_revision: int, baseline_id: str,
                        manifest_sha256: str, approved_by: str, approver_user_id: str, roles: list[str],
                        note: str) -> dict:
    row = {"id": f"DA-{uuid.uuid4().hex[:10]}", "company_id": company_id, "story_id": story_id,
           "design_revision": design_revision, "baseline_id": baseline_id, "manifest_sha256": manifest_sha256,
           "approved_by": approved_by, "approver_user_id": approver_user_id, "roles": json.dumps(sorted(roles)),
           "note": note, "approved_at": _now()}
    with connection(immediate=True) as conn:
        conn.execute("INSERT INTO design_approvals (id, company_id, story_id, design_revision, baseline_id, "
                     "manifest_sha256, approved_by, approver_user_id, roles, note, approved_at) "
                     "VALUES (:id, :company_id, :story_id, :design_revision, :baseline_id, :manifest_sha256, "
                     ":approved_by, :approver_user_id, :roles, :note, :approved_at)", row)
    return {**row, "roles": sorted(roles)}


def design_approvals(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM design_approvals WHERE company_id = ? AND story_id = ? "
                            "ORDER BY approved_at DESC", (company_id, story_id)).fetchall()
    return [{**dict(r), "roles": json.loads(r["roles"])} for r in rows]


def design_approval_for(company_id: str, story_id: str, design_revision: int) -> Optional[dict]:
    for a in design_approvals(company_id, story_id):
        if a["design_revision"] == design_revision:
            return a
    return None


# ---------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------
def _run_row(r) -> dict:
    d = dict(r)
    for key in ("outcome", "usage", "events"):
        d[key] = json.loads(d[key] or ("[]" if key == "events" else "{}"))
    return d


def start_run(*, company_id: str, story_id: str, purpose: str, design_revision: int, baseline_id: str,
              design_approval_id: str, initiated_by: str) -> dict:
    run_id = f"TR-{uuid.uuid4().hex[:10]}"
    with connection(immediate=True) as conn:
        busy = conn.execute("SELECT run_id FROM technical_runs WHERE company_id = ? AND story_id = ? AND status = 'running'",
                            (company_id, story_id)).fetchone()
        if busy:
            raise StaleSubmission(f"Technical Agent run {busy['run_id']} is still running for {story_id}")
        latest = conn.execute("SELECT MAX(revision) AS r FROM technical_packages WHERE company_id = ? AND story_id = ?",
                              (company_id, story_id)).fetchone()["r"] or 0
        conn.execute("INSERT INTO technical_runs (run_id, company_id, story_id, purpose, status, design_revision, "
                     "baseline_id, design_approval_id, expected_package_revision, initiated_by, started_at, events) "
                     "VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                     (run_id, company_id, story_id, purpose, design_revision, baseline_id, design_approval_id, latest,
                      initiated_by, _now(), json.dumps([{"at": _now(), "event": "started", "detail": purpose}])))
    return get_run(run_id)  # type: ignore[return-value]


def get_run(run_id: str) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM technical_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _run_row(row) if row else None


def runs_for(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM technical_runs WHERE company_id = ? AND story_id = ? ORDER BY started_at DESC",
                            (company_id, story_id)).fetchall()
    return [_run_row(r) for r in rows]


def add_event(run_id: str, event: str, detail: str = "", **data: Any) -> None:
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT events FROM technical_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return
        events = json.loads(row["events"] or "[]")
        events.append({"at": _now(), "event": event, "detail": str(detail)[:500], **data})
        conn.execute("UPDATE technical_runs SET events = ? WHERE run_id = ?", (json.dumps(events[-200:]), run_id))


def finish_run(run_id: str, *, status: str, error: Optional[str] = None, outcome: Optional[dict] = None,
               model: Optional[str] = None, usage: Optional[dict] = None) -> None:
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT status FROM technical_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None or row["status"] != "running":
            return  # already settled (e.g. marked interrupted): never overwritten
        conn.execute("UPDATE technical_runs SET status = ?, finished_at = ?, error = ?, outcome = ?, model = ?, usage = ? "
                     "WHERE run_id = ?", (status, _now(), error, json.dumps(outcome or {}), model,
                                          json.dumps(usage or {}), run_id))
    add_event(run_id, status, error or "")


def mark_interrupted() -> int:
    """At startup no run can still be executing in this process."""
    with connection(immediate=True) as conn:
        rows = conn.execute("SELECT run_id FROM technical_runs WHERE status = 'running'").fetchall()
        conn.execute("UPDATE technical_runs SET status = 'failed', finished_at = ?, "
                     "error = 'interrupted by a restart before it finished' WHERE status = 'running'", (_now(),))
    return len(rows)


# ---------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------
def _pkg_row(r) -> dict:
    d = dict(r)
    d["content"] = json.loads(d["content"])
    return d


def store_package(*, run_id: str, company_id: str, story_id: str, content: dict, content_sha256: str) -> dict:
    """Store the next revision -- only if the run is active and saw the
    latest revision (compare-and-set)."""
    with connection(immediate=True) as conn:
        run = conn.execute("SELECT * FROM technical_runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None or run["status"] != "running" or run["company_id"] != company_id or run["story_id"] != story_id:
            raise StaleSubmission("this run is no longer active for this story; its result is not stored")
        latest = conn.execute("SELECT MAX(revision) AS r FROM technical_packages WHERE company_id = ? AND story_id = ?",
                              (company_id, story_id)).fetchone()["r"] or 0
        if latest != run["expected_package_revision"]:
            raise StaleSubmission(f"package revision {latest} was stored after this run started (it expected "
                                  f"{run['expected_package_revision']}); this delayed result is discarded")
        if content.get("revision") != latest + 1:
            raise StaleSubmission("the package's revision number is not the next revision")
        package_id = f"PKG-{story_id}"
        conn.execute("INSERT INTO technical_packages (package_id, revision, company_id, story_id, created_at, "
                     "created_by_run, content, content_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (package_id, latest + 1, company_id, story_id, _now(), run_id,
                      json.dumps(content, sort_keys=True), content_sha256))
        if latest:
            conn.execute("UPDATE technical_packages SET superseded_by = ? WHERE package_id = ? AND revision = ?",
                         (latest + 1, package_id, latest))
        conn.execute("UPDATE technical_runs SET expected_package_revision = ? WHERE run_id = ?", (latest + 1, run_id))
    return get_package(company_id, story_id, latest + 1)  # type: ignore[return-value]


def set_package_change(company_id: str, story_id: str, revision: int, change_id: str) -> None:
    with connection(immediate=True) as conn:
        conn.execute("UPDATE technical_packages SET change_id = ? WHERE company_id = ? AND story_id = ? AND revision = ?",
                     (change_id, company_id, story_id, revision))


def get_package(company_id: str, story_id: str, revision: Optional[int] = None) -> Optional[dict]:
    with connection() as conn:
        if revision is None:
            row = conn.execute("SELECT * FROM technical_packages WHERE company_id = ? AND story_id = ? "
                               "ORDER BY revision DESC LIMIT 1", (company_id, story_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM technical_packages WHERE company_id = ? AND story_id = ? AND revision = ?",
                               (company_id, story_id, revision)).fetchone()
    return _pkg_row(row) if row else None


def packages_for(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM technical_packages WHERE company_id = ? AND story_id = ? ORDER BY revision DESC",
                            (company_id, story_id)).fetchall()
    return [_pkg_row(r) for r in rows]
