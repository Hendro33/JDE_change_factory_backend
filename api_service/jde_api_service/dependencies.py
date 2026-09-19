"""
FastAPI dependencies that enforce the security requirements:

  - Customer context is always validated server-side.
  - X-Customer-Id is only an assertion -- it selects which of the
    caller's OWN entitled customers to scope to, and can never grant
    access to one outside that list.
  - Every customer-scoped read/write goes through require_customer_access,
    so there is exactly one place this check can be forgotten, not one
    per route handler.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException

from .config import settings
from .services.customer_service import CustomerRegistry, Identity, UnknownIdentity, get_registry


def get_registry_dep() -> CustomerRegistry:
    return get_registry()


def resolve_identity(
    x_demo_user_id: str | None = Header(default=None, alias="X-Demo-User-Id"),
) -> Identity:
    """Resolves 'who is calling' from a demo-identity header -- an
    explicit, documented stand-in for real authentication (design doc
    Section 15.10). The header selects an identity; it never carries
    permissions itself. What matters for every downstream check is that
    the ENTITLEMENT LIST always comes from customer_service's table,
    keyed by this resolved identity -- never from anything else the
    client sends."""
    identity_id = x_demo_user_id or settings.default_identity_id
    try:
        return get_registry().resolve_identity(identity_id)
    except UnknownIdentity:
        raise HTTPException(status_code=401, detail=f"unknown identity: {identity_id}")


@dataclass(frozen=True)
class AuthContext:
    identity: Identity
    customer_id: str


def require_customer_access(
    x_customer_id: str = Header(..., alias="X-Customer-Id"),
    identity: Identity = Depends(resolve_identity),
) -> AuthContext:
    """The one place entitlement is actually checked. x_customer_id is
    an assertion from the client about which customer it wants to see;
    it is verified against `identity`'s server-resolved entitlement
    list and rejected outright if it isn't there -- it is never used to
    grant access on its own."""
    registry = get_registry()
    if not registry.is_entitled(identity, x_customer_id):
        raise HTTPException(
            status_code=403,
            detail=f"'{identity.id}' is not entitled to customer '{x_customer_id}'",
        )
    return AuthContext(identity=identity, customer_id=x_customer_id)
