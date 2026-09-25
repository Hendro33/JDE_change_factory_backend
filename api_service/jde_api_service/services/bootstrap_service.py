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
"""

from __future__ import annotations

from ..config import settings
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
    for company_id in company_ids:
        membership_service.create_membership(user.id, company_id, BOOTSTRAP_ROLES, created_by=user.id)
