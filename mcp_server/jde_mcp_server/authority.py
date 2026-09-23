"""
The approver's CURRENT authority, read at the moment it matters.

An exact-change approval records who approved and under which roles.
Roles and memberships change afterwards: a Product Manager is demoted, a
membership is deactivated, a user is disabled. None of that may leave the
earlier approval usable. So immediately before dispatch -- inside the
per-change attempt lock (execution.begin) -- the gate re-reads the
approver's live membership from the api_service's SQLite database and
requires that they STILL hold a role the company's current approval
policy allows.

The database is opened read-only (this process never writes it). Its
path comes from JDE_AUTH_DB_PATH, which api_service's startup exports
(main._wire_execution_gate). No path, no file, or no approver identity on
the approval: the gate cannot verify authority, so nothing runs.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable

AUTH_DB_ENV = "JDE_AUTH_DB_PATH"


class AuthorityUnverifiable(Exception):
    """The approver's current authority cannot be established."""


class AuthorityRevoked(Exception):
    """The approver no longer holds a role that may approve this change."""


def _connect() -> sqlite3.Connection:
    path = os.environ.get(AUTH_DB_ENV, "")
    if not path or not os.path.exists(path):
        raise AuthorityUnverifiable(
            f"the approver's current authority cannot be checked: {AUTH_DB_ENV} is not set or points at no "
            "database -- refusing (fail-closed)"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def current_roles(user_id: str, company_id: str) -> frozenset[str]:
    """Roles held right now; empty for an inactive user or membership."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT r.role FROM company_memberships m "
            "JOIN users u ON u.id = m.user_id "
            "JOIN membership_roles r ON r.membership_id = m.id "
            "WHERE m.user_id = ? AND m.company_id = ? AND m.status = 'active' AND u.is_active = 1",
            (user_id, company_id),
        ).fetchall()
    except sqlite3.Error as exc:
        raise AuthorityUnverifiable(f"the approver's current authority cannot be read: {exc}") from exc
    finally:
        conn.close()
    return frozenset(r["role"] for r in rows)


def require_current_approver(user_id: str | None, company_id: str, allowed_roles: Iterable[str]) -> list[str]:
    """The allowed roles the approver holds now, or raise."""
    if not user_id:
        raise AuthorityUnverifiable(
            "the approval records no approver identity, so the approver's current authority cannot be checked "
            "-- re-approve it"
        )
    allowed = set(allowed_roles)
    held = current_roles(user_id, company_id)
    matched = sorted(held & allowed)
    if not matched:
        raise AuthorityRevoked(
            f"the approver no longer holds a role this company's approval policy allows "
            f"(allowed: {', '.join(sorted(allowed))}; held now: {', '.join(sorted(held)) or 'none'}) -- "
            "the approval cannot be used; re-approve it"
        )
    return matched
