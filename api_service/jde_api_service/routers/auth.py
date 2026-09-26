"""
Login, logout, password reset, and invitation acceptance.

Deliberately NOT here: any public "create an account" / "register"
endpoint. This service is invite-only (design requirement for this
increment) -- the only ways a users row can be created are accepting a
still-valid invitation (accept_invitation below) or the one
server-environment-controlled bootstrap path (services/bootstrap_service.py,
never reachable over HTTP).
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response

from ..config import settings
from ..dependencies import Identity, resolve_identity, verify_csrf_if_unsafe
from ..models.auth import (
    SetupHandoverInput,
    AcceptInvitationInput,
    ForgotPasswordInput,
    ForgotPasswordResult,
    InvitationPreview,
    LoginInput,
    MeOut,
    ResetPasswordInput,
)
from ..services import auth_service, invitation_service, login_throttle
from ..services.email_service import OutgoingEmail, get_email_service

router = APIRouter(prefix="/auth", tags=["auth"])

# Query params on the frontend's root path, not distinct paths -- the
# frontend is a single static index.html with no client-side router
# and no server-side rewrite rule (confirmed: no 404.html SPA-fallback
# in that repo), so a link to e.g. "/reset-password" would 404 on
# GitHub Pages. "resetToken"/"acceptInvitation" (not both "token") so
# the frontend can tell which flow a link opened into.
_RESET_QUERY_KEY = "resetToken"


def _frontend_origin() -> str:
    # Best-effort only, for building a human-followable link in the
    # dev-preview response -- never used for anything security-relevant
    # (CORS/cookie decisions use settings.allowed_origins/cookie_* directly).
    return settings.allowed_origins[0] if settings.allowed_origins else ""


def _cookie_kwargs() -> dict:
    kwargs: dict = dict(secure=settings.cookie_secure, samesite=settings.cookie_samesite, path="/")
    if settings.cookie_domain:
        kwargs["domain"] = settings.cookie_domain
    return kwargs


def _set_auth_cookies(response: Response, raw_session_token: str) -> None:
    """Sets both the httponly session cookie AND the JS-readable CSRF
    cookie (see dependencies.verify_csrf_if_unsafe's own docstring) --
    every place that logs someone in sets both together, so there is no
    window where a session exists without its CSRF cookie."""
    kwargs = _cookie_kwargs()
    response.set_cookie(
        key=auth_service.SESSION_COOKIE_NAME, value=raw_session_token, httponly=True,
        max_age=auth_service.SESSION_TTL_DAYS * 24 * 3600, **kwargs,
    )
    response.set_cookie(
        key=auth_service.CSRF_COOKIE_NAME, value=auth_service.generate_token(), httponly=False,
        max_age=auth_service.SESSION_TTL_DAYS * 24 * 3600, **kwargs,
    )


def _clear_auth_cookies(response: Response) -> None:
    kwargs = _cookie_kwargs()
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, **kwargs)
    response.delete_cookie(auth_service.CSRF_COOKIE_NAME, **kwargs)


@router.post("/login", response_model=MeOut)
def login(payload: LoginInput, request: Request, response: Response) -> MeOut:
    # Rate limited per account and per client (services/login_throttle.py).
    # A refused attempt never reaches the password check.
    client = login_throttle.client_address(request)
    wait = login_throttle.check(payload.email, client)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed sign-in attempts. Try again in {max(1, round(wait / 60))} minute(s).",
            headers={"Retry-After": str(wait)},
        )
    user = auth_service.authenticate(payload.email, payload.password)
    if user is None:
        login_throttle.record_failure(payload.email, client)
        raise HTTPException(status_code=401, detail="incorrect email or password")
    login_throttle.record_success(payload.email)
    raw_token = auth_service.create_session(user.id)
    _set_auth_cookies(response, raw_token)
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    jde_session: str | None = Cookie(default=None, alias=auth_service.SESSION_COOKIE_NAME),
    jde_csrf: str | None = Cookie(default=None, alias=auth_service.CSRF_COOKIE_NAME),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict:
    # Revokes the server-side session record (not just the cookie) so
    # the same raw token can't be replayed if it leaked before logout --
    # and never 401s just because the cookie was already missing or
    # expired, since the end state (signed out) is the same either way.
    # CSRF-checked like every other state-changing call (see
    # dependencies.verify_csrf_if_unsafe) -- a forged logout is low
    # severity, but there is no reason to exempt it.
    if jde_session:
        verify_csrf_if_unsafe(request, jde_csrf, x_csrf_token)
        auth_service.revoke_session(jde_session)
    _clear_auth_cookies(response)
    return {"ok": True}


@router.get("/setup-handover")
def setup_handover_status(identity: Identity = Depends(resolve_identity)) -> dict:
    """Whether the signed-in account is the temporary setup account, which
    should create the owner's own administrator account and then retire."""
    from ..services import setup_handover

    user = auth_service.get_user_by_id(identity.id)
    return {"isSetupAccount": bool(user and setup_handover.is_setup_account(user.email)),
            "minPasswordLength": setup_handover.MIN_PASSWORD_LENGTH}


@router.post("/setup-handover")
def finish_setup(payload: SetupHandoverInput, response: Response,
                 identity: Identity = Depends(resolve_identity)) -> dict:
    """Create the owner's administrator account (Admin in every customer the
    setup account administers), then disable the setup account and sign it
    out. The password is never logged or returned."""
    from ..services import setup_handover

    try:
        result = setup_handover.hand_over(identity.id, email=payload.email, display_name=payload.display_name,
                                          password=payload.password)
    except setup_handover.HandoverRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    _clear_auth_cookies(response)
    return {"ok": True, **result}


@router.get("/me", response_model=MeOut)
def me(identity: Identity = Depends(resolve_identity)) -> MeOut:
    user = auth_service.get_user_by_id(identity.id)
    assert user is not None  # resolve_identity already confirmed this session's user exists and is active
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/forgot-password", response_model=ForgotPasswordResult)
def forgot_password(payload: ForgotPasswordInput) -> ForgotPasswordResult:
    """Always returns ok=true and never the link -- whether the email is
    registered is not revealed, and the reset link only ever goes to the
    account's own inbox (once an email provider exists). An earlier build
    returned the link here in dev-preview mode, which let any anonymous
    caller reset any account's password. Without an email provider, a
    company Admin issues the link from Admin > Users instead."""
    user = auth_service.get_user_by_email(payload.email)
    if user is None or not user.is_active:
        return ForgotPasswordResult(ok=True, preview_url=None)

    raw_token = auth_service.create_password_reset_token(user.id)
    email_service = get_email_service()
    link = f"{_frontend_origin()}?{_RESET_QUERY_KEY}={raw_token}"
    email_service.send(OutgoingEmail(
        to=user.email, subject="Reset your Jade password",
        body=f"Reset your password: {link}", action_url=link,
    ))
    return ForgotPasswordResult(ok=True, preview_url=None)


@router.post("/reset-password")
def reset_password(payload: ResetPasswordInput) -> dict:
    user_id = auth_service.consume_password_reset_token(payload.token)
    if user_id is None:
        raise HTTPException(status_code=400, detail="invalid or expired reset link")
    auth_service.set_password(user_id, payload.new_password)
    # A password reset invalidates every existing session -- a stolen
    # session token is exactly the scenario a reset is meant to recover
    # from.
    auth_service.revoke_all_sessions_for_user(user_id)
    return {"ok": True}


@router.get("/invitation/{token}/preview", response_model=InvitationPreview)
def preview_invitation(token: str) -> InvitationPreview:
    from ..services.customer_service import get_registry

    inv = invitation_service.get_by_token(token)
    if inv is None:
        return InvitationPreview(email="", company_name="", roles=[], requires_password=False, valid=False, reason="invitation not found")
    if inv["status"] != "pending":
        return InvitationPreview(
            email=inv["email"], company_name="", roles=inv["roles"], requires_password=False,
            valid=False, reason=f"invitation is {inv['status']}",
        )
    company = get_registry().get_customer(inv["company_id"])
    existing_user = auth_service.get_user_by_email(inv["email"])
    return InvitationPreview(
        email=inv["email"], company_name=company.name if company else inv["company_id"],
        roles=inv["roles"], requires_password=existing_user is None, valid=True,
    )


@router.post("/accept-invitation", response_model=MeOut)
def accept_invitation_route(payload: AcceptInvitationInput, response: Response) -> MeOut:
    """New user: password is required, and the response logs them
    straight in. Existing user: must already be signed in as the
    invited email -- this route never silently attaches an invitation
    to whichever session cookie happens to be present."""
    inv = invitation_service.get_by_token(payload.token)
    if inv is None or inv["status"] != "pending":
        raise HTTPException(status_code=400, detail="invalid, expired, or already-used invitation")

    existing_user = auth_service.get_user_by_email(inv["email"])
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail="an account with this email already exists -- sign in first, then open the invitation link again",
        )

    try:
        user, _membership_id = invitation_service.accept_invitation(
            payload.token, password=payload.password, display_name=payload.display_name,
        )
    except invitation_service.InvalidInvitation as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    raw_token = auth_service.create_session(user.id)
    _set_auth_cookies(response, raw_token)
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)


@router.post("/accept-invitation/existing-user", response_model=MeOut)
def accept_invitation_existing_user(
    payload: AcceptInvitationInput, identity: Identity = Depends(resolve_identity),
) -> MeOut:
    """The existing-user counterpart to accept_invitation_route above:
    the caller must already be signed in, as the exact email the
    invitation was sent to."""
    inv = invitation_service.get_by_token(payload.token)
    if inv is None or inv["status"] != "pending":
        raise HTTPException(status_code=400, detail="invalid, expired, or already-used invitation")

    user = auth_service.get_user_by_id(identity.id)
    assert user is not None
    if user.email.lower() != inv["email"].lower():
        raise HTTPException(status_code=403, detail="this invitation was sent to a different email address")

    try:
        invitation_service.accept_invitation(payload.token, password=None, display_name=None)
    except invitation_service.InvalidInvitation as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return MeOut(user_id=user.id, email=user.email, display_name=user.display_name)
