"""
FastAPI dependencies that enforce the security requirements:

  - WHO is calling is resolved from a real, server-verified session
    (auth_service.get_user_for_session) -- not a client-supplied header.
    The old X-Demo-User-Id "identity as an assertion, entitlement always
    server-resolved" design (Section 15.10) is now a real login, but
    the shape of that guarantee is unchanged: a cookie names an
    identity, it never carries permissions.
  - WHAT COMPANY the caller can see is still an explicit assertion
    (X-Customer-Id) that is only ever used to select among that
    identity's own ACTIVE company memberships -- never to grant access
    on its own. require_customer_access is still the one place this is
    checked, so there is exactly one place it can be forgotten, not one
    per route handler.
  - WHAT THE CALLER MAY DO on that company is its roles, resolved the
    same way (membership_service.roles_for) and never trusted from the
    client. require_role/require_write_access build on
    require_customer_access for the routes that need a specific role;
    every other route stays as it always was (any active member may
    call it).
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, Request

from .services import auth_service, membership_service


@dataclass(frozen=True)
class Identity:
    id: str
    display_name: str


def resolve_identity(
    jde_session: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
) -> Identity:
    if not jde_session:
        raise HTTPException(status_code=401, detail="not signed in")
    user = auth_service.get_user_for_session(jde_session)
    if user is None:
        raise HTTPException(status_code=401, detail="session expired or invalid -- please sign in again")
    return Identity(id=user.id, display_name=user.display_name)


@dataclass(frozen=True)
class AuthContext:
    identity: Identity
    customer_id: str
    roles: frozenset[str]


def require_customer_access(
    x_customer_id: str = Header(..., alias="X-Customer-Id"),
    identity: Identity = Depends(resolve_identity),
) -> AuthContext:
    """The one place entitlement is actually checked. x_customer_id is
    an assertion from the client about which company it wants to see;
    it is verified against this identity's ACTIVE company memberships
    and rejected outright if there is none -- it is never used to grant
    access on its own."""
    roles = membership_service.roles_for(identity.id, x_customer_id)
    if not roles:
        raise HTTPException(
            status_code=403,
            detail=f"'{identity.id}' is not entitled to customer '{x_customer_id}'",
        )
    return AuthContext(identity=identity, customer_id=x_customer_id, roles=roles)


def require_role(*allowed: str):
    """A route-level dependency factory: require_role("admin") etc.
    Stacks on top of require_customer_access -- the caller must still
    be an active member of the company, AND hold at least one of the
    given roles there."""

    def _dependency(ctx: AuthContext = Depends(require_customer_access)) -> AuthContext:
        if not (ctx.roles & set(allowed)):
            raise HTTPException(status_code=403, detail=f"requires one of these roles: {', '.join(allowed)}")
        return ctx

    return _dependency


_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


def require_write_access(request: Request, ctx: AuthContext = Depends(require_customer_access)) -> AuthContext:
    """Defence in depth for every write endpoint that doesn't already
    have a more specific role requirement (require_role(...), or the
    domain-owner scoping check below): a membership whose ONLY role is
    dashboard_viewer may never write, regardless of which endpoint it
    calls. Endpoints that already require a specific role (e.g. Jira
    admin routes require "admin") don't need this too -- dashboard_viewer
    can never satisfy those role checks either way."""
    if request.method in _WRITE_METHODS and ctx.roles and ctx.roles <= {"dashboard_viewer"}:
        raise HTTPException(status_code=403, detail="Dashboard Viewer access is read-only")
    return ctx


def require_domain_owner_access(ctx: AuthContext, business_domain_id: str | None) -> None:
    """Called directly inside a domain-review handler (domain_governance.py),
    once it has loaded the record and therefore knows which business
    domain is involved -- that isn't known from the URL alone, so this
    can't be a plain Depends(). Raises 403 unless the caller holds the
    domain_owner role on this company AND is assigned to this specific
    business domain."""
    if "domain_owner" not in ctx.roles:
        raise HTTPException(status_code=403, detail="requires the Domain Owner role")
    if business_domain_id:
        assigned = membership_service.domain_ids_for_membership(ctx.identity.id, ctx.customer_id)
        if business_domain_id not in assigned:
            raise HTTPException(status_code=403, detail="not assigned to this business domain")
