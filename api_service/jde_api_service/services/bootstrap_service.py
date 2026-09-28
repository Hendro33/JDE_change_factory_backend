"""
Controlled first-Admin creation.

There is no public admin-registration endpoint anywhere in this
service (see routers/auth.py's own docstring) -- registration is
invite-only, and an invitation can only be created by an existing
Admin. This module is the one deliberate way around that circularity:
an operator with access to the server's environment (never an HTTP
caller) sets JDE_BOOTSTRAP_ADMIN_EMAIL/_PASSWORD, and the very first
Admin is created on startup. Idempotent and safe on every restart --
once that email is registered, this is a no-op forever, so it never
resets a password an Admin has since changed.

Jade starts empty (no demo data). On an installation with no customer at
all, the bootstrap also creates the first customer
(JDE_BOOTSTRAP_CUSTOMER_NAME) so the setup account can start setting up;
it is an ordinary customer the Admin renames and configures.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from ..config import settings
from ..persistence.db import connection
from ..models.auth import BOOTSTRAP_ROLES
from . import auth_service, membership_service
from .customer_service import get_registry


def ensure_bootstrap_admin() -> None:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return
    if auth_service.get_user_by_email(settings.bootstrap_admin_email) is not None:
        return

    user = auth_service.create_user(
        settings.bootstrap_admin_email, settings.bootstrap_admin_password, settings.bootstrap_admin_name,
    )
    company_ids = settings.bootstrap_admin_companies or [c.id for c in get_registry().list_companies()]
    if not company_ids:
        company_ids = [_create_first_customer(settings.bootstrap_customer_name or "First customer", user.id)]
    for company_id in company_ids:
        membership_service.create_membership(user.id, company_id, BOOTSTRAP_ROLES, created_by=user.id)


def _create_first_customer(name: str, created_by: str) -> str:
    name = name.strip()[:120] or "First customer"
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "customer"
    company_id = f"{base}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        conn.execute(
            "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, updated_at, updated_by) "
            "VALUES (?, ?, ?, '', '', ?, ?, ?)", (company_id, name, name[:40], now, now, created_by))
    return company_id
