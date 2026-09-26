"""
Finish first-time setup: the temporary setup account (the bootstrap Admin,
JDE_BOOTSTRAP_ADMIN_EMAIL -- admin@e2e.local in the local launcher) creates
the owner's own administrator account and is then switched off, in one step.

  * Only the setup account can do this, and only while it is active.
  * The owner's email must not already have an account; the password is
    typed by the owner in the browser on this machine (never in a file, a
    log or chat) and must be at least MIN_PASSWORD_LENGTH characters.
  * The owner receives the same roles in every customer where the setup
    account is an active Admin, so no customer is left without an Admin.
  * The setup account is then disabled: it can no longer sign in, its
    memberships are inactive and its sessions are revoked. The launcher
    never re-creates or re-enables it (bootstrap is a no-op once the email
    exists). Audited per customer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..config import settings
from ..models.auth import BOOTSTRAP_ROLES
from ..persistence.db import connection
from . import auth_service
from .membership_service import log_access_change

MIN_PASSWORD_LENGTH = 12


class HandoverRefused(RuntimeError):
    pass


def is_setup_account(email: str) -> bool:
    return bool(settings.bootstrap_admin_email) and email.strip().lower() == settings.bootstrap_admin_email.strip().lower()


def hand_over(setup_user_id: str, *, email: str, display_name: str, password: str) -> dict:
    setup = auth_service.get_user_by_id(setup_user_id)
    if setup is None or not setup.is_active or not is_setup_account(setup.email):
        raise HandoverRefused("only the active temporary setup account can finish setup")
    email = (email or "").strip()
    display_name = (display_name or "").strip() or email
    if "@" not in email or len(email) > 254:
        raise HandoverRefused("enter a valid email address")
    if is_setup_account(email):
        raise HandoverRefused("choose your own email address, not the setup account's")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise HandoverRefused(f"the password must be at least {MIN_PASSWORD_LENGTH} characters")
    if auth_service.get_user_by_email(email) is not None:
        raise HandoverRefused("an account with this email already exists; sign in with it instead")

    now = datetime.now(timezone.utc).isoformat()
    new_id = f"u-{uuid.uuid4().hex[:12]}"
    with connection(immediate=True) as conn:
        companies = [r["company_id"] for r in conn.execute(
            "SELECT DISTINCT m.company_id FROM company_memberships m JOIN membership_roles r ON r.membership_id = m.id "
            "WHERE m.user_id = ? AND m.status = 'active' AND r.role = 'admin'", (setup.id,)).fetchall()]
        if not companies:
            raise HandoverRefused("the setup account is not an active Admin of any customer")
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (new_id, email, auth_service.hash_password(password), display_name, now, now))
        for company_id in companies:
            membership_id = f"mem-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO company_memberships (id, user_id, company_id, status, created_at, updated_at, created_by) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)", (membership_id, new_id, company_id, now, now, setup.id))
            for role in BOOTSTRAP_ROLES:
                conn.execute("INSERT INTO membership_roles (membership_id, role) VALUES (?, ?)", (membership_id, role))
            log_access_change(conn, company_id=company_id, actor_user_id=setup.id, action="setup_handover",
                              target_user_id=new_id, detail=f"owner account {email} created; setup account disabled")
        conn.execute("UPDATE company_memberships SET status = 'inactive', updated_at = ?, revision = revision + 1 "
                     "WHERE user_id = ? AND status = 'active'", (now, setup.id))
        conn.execute("UPDATE users SET is_active = 0, updated_at = ? WHERE id = ?", (now, setup.id))
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (now, setup.id))
    return {"email": email, "customers": len(companies)}
