"""
Invitation lifecycle: create (Admin), preview + accept (invited
person), resend, revoke, and the lazy expiry check every read applies.

Invite-only registration: there is no public "create an account"
endpoint anywhere in this service (see routers/auth.py's own
docstring) -- the only way a new user row can be created is by
accepting a still-valid invitation, or the one controlled bootstrap
path in services/bootstrap_service.py.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..persistence.db import connection
from . import auth_service, membership_service

INVITATION_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class InvalidInvitation(RuntimeError):
    pass


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"], "company_id": row["company_id"], "email": row["email"],
        "roles": json.loads(row["roles"]), "domain_ids": json.loads(row["domain_ids"]),
        "status": row["status"], "invited_by": row["invited_by"],
        "created_at": row["created_at"], "expires_at": row["expires_at"], "accepted_at": row["accepted_at"],
    }


def _effective_status(row_dict: dict) -> str:
    if row_dict["status"] == "pending" and datetime.fromisoformat(row_dict["expires_at"]) < _now():
        return "expired"
    return row_dict["status"]


def create_invitation(company_id: str, email: str, roles: list[str], domain_ids: list[str], *, invited_by: str) -> tuple[dict, str]:
    raw_token = auth_service.generate_token()
    now = _now()
    invitation_id = f"inv-{uuid.uuid4().hex[:12]}"
    with connection() as conn:
        conn.execute(
            "INSERT INTO invitations (id, company_id, email, roles, domain_ids, token_hash, status, "
            "invited_by, created_at, expires_at, accepted_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)",
            (
                invitation_id, company_id, email, json.dumps(roles), json.dumps(domain_ids),
                auth_service.hash_token(raw_token), invited_by, _iso(now), _iso(now + timedelta(days=INVITATION_TTL_DAYS)),
            ),
        )
        membership_service.log_access_change(
            conn, company_id=company_id, actor_user_id=invited_by, action="invitation_created",
            target_user_id=None, detail=email,
        )
    return get_invitation(invitation_id), raw_token  # type: ignore[return-value]


def get_invitation(invitation_id: str) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM invitations WHERE id = ?", (invitation_id,)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        d["status"] = _effective_status(d)
        return d


def list_for_company(company_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM invitations WHERE company_id = ? ORDER BY created_at DESC", (company_id,)
        ).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        d["status"] = _effective_status(d)
        result.append(d)
    return result


def get_by_token(raw_token: str) -> Optional[dict]:
    token_hash = auth_service.hash_token(raw_token)
    with connection() as conn:
        row = conn.execute("SELECT * FROM invitations WHERE token_hash = ?", (token_hash,)).fetchone()
        if row is None:
            return None
        d = _row_to_dict(row)
        d["status"] = _effective_status(d)
        return d


def revoke_invitation(invitation_id: str, *, actor_user_id: str) -> dict:
    with connection() as conn:
        row = conn.execute("SELECT company_id, email, status FROM invitations WHERE id = ?", (invitation_id,)).fetchone()
        if row is None:
            raise InvalidInvitation(invitation_id)
        conn.execute("UPDATE invitations SET status = 'revoked' WHERE id = ?", (invitation_id,))
        membership_service.log_access_change(
            conn, company_id=row["company_id"], actor_user_id=actor_user_id, action="invitation_revoked",
            target_user_id=None, detail=row["email"],
        )
    return get_invitation(invitation_id)  # type: ignore[return-value]


def resend_invitation(invitation_id: str, *, actor_user_id: str) -> tuple[dict, str]:
    """Rotates the token and expiry on the same row -- the old link
    stops working the moment a new one is issued."""
    raw_token = auth_service.generate_token()
    now = _now()
    with connection() as conn:
        row = conn.execute("SELECT company_id, email FROM invitations WHERE id = ?", (invitation_id,)).fetchone()
        if row is None:
            raise InvalidInvitation(invitation_id)
        conn.execute(
            "UPDATE invitations SET token_hash = ?, status = 'pending', created_at = ?, expires_at = ?, accepted_at = NULL "
            "WHERE id = ?",
            (auth_service.hash_token(raw_token), _iso(now), _iso(now + timedelta(days=INVITATION_TTL_DAYS)), invitation_id),
        )
        membership_service.log_access_change(
            conn, company_id=row["company_id"], actor_user_id=actor_user_id, action="invitation_resent",
            target_user_id=None, detail=row["email"],
        )
    return get_invitation(invitation_id), raw_token  # type: ignore[return-value]


def accept_invitation(raw_token: str, *, password: Optional[str], display_name: Optional[str]) -> tuple[auth_service.User, str]:
    """Returns (user, membership_id). Creates a new user only if no
    account with this email exists yet -- an existing user accepts by
    already being logged in (routers/auth.py enforces that distinction,
    this function just does the membership/account work either way)."""
    inv = get_by_token(raw_token)
    if inv is None or inv["status"] != "pending":
        raise InvalidInvitation("invalid, expired, or already-used invitation")

    existing = auth_service.get_user_by_email(inv["email"])
    if existing is None:
        if not password:
            raise InvalidInvitation("a password is required to accept this invitation")
        user = auth_service.create_user(inv["email"], password, display_name or inv["email"])
    else:
        user = existing

    with connection() as conn:
        row = conn.execute(
            "SELECT id, status FROM company_memberships WHERE user_id = ? AND company_id = ?",
            (user.id, inv["company_id"]),
        ).fetchone()
    if row is not None:
        # Re-invited while already a member (e.g. after being
        # deactivated) -- apply the new roles/domains and reactivate,
        # rather than fail on the UNIQUE(user_id, company_id) constraint.
        membership_service.update_membership_roles(
            row["id"], set(inv["roles"]), set(inv["domain_ids"]), actor_user_id=inv["invited_by"]
        )
        if row["status"] != "active":
            membership_service.set_membership_status(row["id"], "active", actor_user_id=inv["invited_by"])
        membership_id = row["id"]
    else:
        membership_id = membership_service.create_membership(
            user.id, inv["company_id"], inv["roles"], inv["domain_ids"], created_by=inv["invited_by"]
        )

    with connection() as conn:
        conn.execute(
            "UPDATE invitations SET status = 'accepted', accepted_at = ? WHERE id = ?", (_iso(_now()), inv["id"])
        )
        membership_service.log_access_change(
            conn, company_id=inv["company_id"], actor_user_id=user.id, action="invitation_accepted",
            target_user_id=user.id, detail=inv["email"],
        )
    return user, membership_id
