"""
Session/customer wire models -- mirrors src/types/domain.ts's
Customer and Session interfaces field-for-field.

SECURITY NOTE: a Session is always built server-side by
customer_service, from a resolved identity's entitlement list. Nothing
in this module is ever constructed from client-supplied data.
"""

from __future__ import annotations

from typing import Literal

from .base import ApiModel

UserRole = Literal[
    "Application Manager", "Product Owner", "ConsultIQ Consultant", "JDE CNC"
]


class Customer(ApiModel):
    id: str
    name: str
    short_name: str
    tools_release: str
    environment: str


class SessionOut(ApiModel):
    user_id: str
    display_name: str
    role: UserRole
    customers: list[Customer]
    active_customer_id: str
