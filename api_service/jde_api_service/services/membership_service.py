"""
Company memberships, roles, domain assignments, and the access audit
log -- "who belongs to which company, with what roles, and who changed
that, when." Companies themselves live in company_service.py; users'
credentials live in auth_service.py. This module is the join between
them that actually decides access.

Creating a login (auth_service.create_user) grants nothing by itself --
only a row here, with status 'active', does; see require_customer_access
(dependencies.py), which is the only place that reads this module to
decide whether a request may proceed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from ..persistence.db import connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LastAdminError(RuntimeError):
    """Raised when an action would leave a company with zero active Admins."""


class NoSuchMembership(RuntimeError):
    pass


def log_access_change(conn, *, company_id: Optional[str], actor_user_id: Optional[str], action: str,
          target_user_id: Optional[str] = None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO access_audit_log (company_id, actor_user_id, action, target_user_id, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (company_id, actor_user_id, action, target_user_id, detail, _now()),
    )


def roles_for(user_id: str, company_id: str) -> frozenset[str]:
    """Empty if there is no ACTIVE membership -- inactive and absent
    are indistinguishable to every caller that checks entitlement this
    way, which is exactly the "no access" outcome both should have."""
    with connection() as conn:
        row = conn.execute(
            "SELECT id, status FROM company_memberships WHERE user_id = ? AND company_id = ?",
            (user_id, company_id),
        ).fetchone()
        if row is None or row["status"] != "active":
            return frozenset()
        roles = conn.execute(
            "SELECT role FROM membership_roles WHERE membership_id = ?", (row["id"],)
        ).fetchall()
        return frozenset(r["role"] for r in roles)


def domain_ids_for_membership(user_id: str, company_id: str) -> frozenset[str]:
    with connection() as conn:
        row = conn.execute(
            "SELECT id FROM company_memberships WHERE user_id = ? AND company_id = ? AND status = 'active'",
            (user_id, company_id),
        ).fetchone()
        if row is None:
            return frozenset()
        rows = conn.execute(
            "SELECT business_domain_id FROM domain_assignments WHERE membership_id = ?", (row["id"],)
        ).fetchall()
        return frozenset(r["business_domain_id"] for r in rows)


def companies_for_user(user_id: str) -> list[dict]:
    """Active memberships only, each with its roles -- what the
    /session bootstrap and the company switcher need."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT m.id as membership_id, m.company_id, c.name, c.short_name, c.tools_release, c.environment "
            "FROM company_memberships m JOIN companies c ON c.id = m.company_id "
            "WHERE m.user_id = ? AND m.status = 'active' ORDER BY c.name",
            (user_id,),
        ).fetchall()
        result = []
        for row in rows:
            role_rows = conn.execute(
                "SELECT role FROM membership_roles WHERE membership_id = ?", (row["membership_id"],)
            ).fetchall()
            result.append({
                "company_id": row["company_id"], "name": row["name"], "short_name": row["short_name"],
                "tools_release": row["tools_release"], "environment": row["environment"],
                "roles": sorted(r["role"] for r in role_rows),
            })
        return result


def create_membership(
    user_id: str, company_id: str, roles: Iterable[str], domain_ids: Iterable[str] = (),
    *, created_by: Optional[str], status: str = "active",
) -> str:
    now = _now()
    membership_id = f"mem-{uuid.uuid4().hex[:12]}"
    with connection() as conn:
        conn.execute(
            "INSERT INTO company_memberships (id, user_id, company_id, status, created_at, updated_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (membership_id, user_id, company_id, status, now, now, created_by),
        )
        for role in roles:
            conn.execute("INSERT INTO membership_roles (membership_id, role) VALUES (?, ?)", (membership_id, role))
        for domain_id in domain_ids:
            conn.execute(
                "INSERT INTO domain_assignments (membership_id, business_domain_id) VALUES (?, ?)",
                (membership_id, domain_id),
            )
        log_access_change(conn, company_id=company_id, actor_user_id=created_by, action="membership_created", target_user_id=user_id)
    return membership_id


def _active_admin_membership_ids(conn, company_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT m.id FROM company_memberships m JOIN membership_roles r ON r.membership_id = m.id "
        "WHERE m.company_id = ? AND m.status = 'active' AND r.role = 'admin'",
        (company_id,),
    ).fetchall()
    return {r["id"] for r in rows}


def update_membership_roles(membership_id: str, roles: set[str], domain_ids: set[str], *, actor_user_id: str) -> None:
    with connection() as conn:
        row = conn.execute(
            "SELECT company_id, user_id FROM company_memberships WHERE id = ?", (membership_id,)
        ).fetchone()
        if row is None:
            raise NoSuchMembership(membership_id)
        company_id, target_user_id = row["company_id"], row["user_id"]

        current_roles = {r["role"] for r in conn.execute(
            "SELECT role FROM membership_roles WHERE membership_id = ?", (membership_id,)
        ).fetchall()}
        if "admin" in current_roles and "admin" not in roles:
            remaining_admins = _active_admin_membership_ids(conn, company_id) - {membership_id}
            if not remaining_admins:
                raise LastAdminError("cannot remove the last active Admin from a company")

        conn.execute("DELETE FROM membership_roles WHERE membership_id = ?", (membership_id,))
        for role in roles:
            conn.execute("INSERT INTO membership_roles (membership_id, role) VALUES (?, ?)", (membership_id, role))
        conn.execute("DELETE FROM domain_assignments WHERE membership_id = ?", (membership_id,))
        for domain_id in domain_ids:
            conn.execute(
                "INSERT INTO domain_assignments (membership_id, business_domain_id) VALUES (?, ?)",
                (membership_id, domain_id),
            )
        conn.execute("UPDATE company_memberships SET updated_at = ? WHERE id = ?", (_now(), membership_id))
        log_access_change(
            conn, company_id=company_id, actor_user_id=actor_user_id, action="roles_updated",
            target_user_id=target_user_id, detail=",".join(sorted(roles)),
        )


def set_membership_status(membership_id: str, status: str, *, actor_user_id: str) -> None:
    with connection() as conn:
        row = conn.execute(
            "SELECT company_id, user_id, status FROM company_memberships WHERE id = ?", (membership_id,)
        ).fetchone()
        if row is None:
            raise NoSuchMembership(membership_id)
        company_id, target_user_id = row["company_id"], row["user_id"]

        if status == "inactive" and row["status"] == "active":
            is_admin = conn.execute(
                "SELECT 1 FROM membership_roles WHERE membership_id = ? AND role = 'admin'", (membership_id,)
            ).fetchone() is not None
            if is_admin:
                remaining_admins = _active_admin_membership_ids(conn, company_id) - {membership_id}
                if not remaining_admins:
                    raise LastAdminError("cannot deactivate the last active Admin from a company")

        conn.execute(
            "UPDATE company_memberships SET status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), membership_id),
        )
        log_access_change(
            conn, company_id=company_id, actor_user_id=actor_user_id,
            action=f"membership_{status}", target_user_id=target_user_id,
        )


def list_company_members(company_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT m.id as membership_id, m.user_id, m.status, u.email, u.display_name "
            "FROM company_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.company_id = ? ORDER BY u.display_name",
            (company_id,),
        ).fetchall()
        result = []
        for row in rows:
            role_rows = conn.execute(
                "SELECT role FROM membership_roles WHERE membership_id = ?", (row["membership_id"],)
            ).fetchall()
            domain_rows = conn.execute(
                "SELECT business_domain_id FROM domain_assignments WHERE membership_id = ?", (row["membership_id"],)
            ).fetchall()
            result.append({
                "membership_id": row["membership_id"], "user_id": row["user_id"], "email": row["email"],
                "display_name": row["display_name"], "status": row["status"],
                "roles": sorted(r["role"] for r in role_rows),
                "domain_ids": sorted(r["business_domain_id"] for r in domain_rows),
            })
        return result


def assigned_domain_owners(company_id: str) -> dict[str, list[str]]:
    """business_domain_id -> display names of the ACTIVE members who hold
    the domain_owner role on this company and are assigned to that
    domain: exactly the people require_domain_owner_access accepts."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT a.business_domain_id, u.display_name FROM domain_assignments a "
            "JOIN company_memberships m ON m.id = a.membership_id "
            "JOIN users u ON u.id = m.user_id "
            "JOIN membership_roles r ON r.membership_id = m.id AND r.role = 'domain_owner' "
            "WHERE m.company_id = ? AND m.status = 'active' AND u.is_active = 1 "
            "ORDER BY u.display_name",
            (company_id,),
        ).fetchall()
    owners: dict[str, list[str]] = {}
    for row in rows:
        owners.setdefault(row["business_domain_id"], []).append(row["display_name"])
    return owners


def get_membership_by_id(membership_id: str) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, user_id, company_id, status FROM company_memberships WHERE id = ?", (membership_id,)
        ).fetchone()
        return dict(row) if row else None
