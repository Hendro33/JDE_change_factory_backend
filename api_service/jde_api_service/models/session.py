"""
Session/customer wire models -- mirrors src/types/domain.ts's
Customer and Session interfaces field-for-field.

SECURITY NOTE: a Session is always built server-side by
customer_service, from a resolved identity's entitlement list. Nothing
in this module is ever constructed from client-supplied data.
"""

from __future__ import annotations

from .base import ApiModel


class Customer(ApiModel):
    id: str
    name: str
    short_name: str
    tools_release: str
    environment: str
    # This user's roles on THIS company specifically -- a user can hold
    # different roles on different companies. See models/auth.py's own
    # docstring for what each role means.
    roles: list[str] = []


class SessionOut(ApiModel):
    user_id: str
    display_name: str
    email: str
    # Deprecated display-only field: the active company's roles,
    # comma-joined, kept only so App.tsx's header chip still has
    # something to show. Real permission checks use Customer.roles
    # (per company) or the roles on the specific company in question --
    # never this field.
    role: str
    customers: list[Customer]
    active_customer_id: str
