"""
Hand-offs to a named implementer, and the business acceptance of the outcome
(usability redesign, step 2: delivery/handoff.py, services/acceptance.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from jde_mcp_server import approval, authority

from ..dependencies import AuthContext, require_customer_access, require_domain_owner_access, require_write_access
from ..delivery import handoff
from ..models.base import ApiModel
from ..models.change import Change
from ..services import acceptance
from ..services.registry import get_change_service

router = APIRouter(tags=["delivery-work"])


def _change(change_id: str, ctx: AuthContext) -> Change:
    change = get_change_service().get_for_customer(change_id, ctx.customer_id)
    if change is None:
        raise HTTPException(status_code=404, detail="This story could not be found.")
    return change


def _refused(exc: Exception) -> HTTPException:
    if isinstance(exc, (approval.ApproverNotAuthorised, authority.AuthorityRevoked)):
        return HTTPException(status_code=403, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


_REFUSALS = (handoff.HandoffRefused, acceptance.AcceptanceRefused, approval.ChangeApprovalError,
             authority.AuthorityRevoked, authority.AuthorityUnverifiable)


# -- Hand-offs ---------------------------------------------------------------------------
@router.get("/changes/{change_id}/handoff")
def handoff_view(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(change_id, ctx)
    if change.architect_decision is None:
        return {"route": None, "items": [], "authorisation": None, "verification": None, "specification": None}
    st = handoff.state(change)
    try:
        spec = handoff.specification(change)
    except handoff.HandoffRefused:
        spec = None
    return {**st, "specification": spec}


@router.get("/changes/{change_id}/handoff/specification.pdf")
def specification_pdf(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> Response:
    change = _change(change_id, ctx)
    try:
        data = handoff.specification_pdf(change)
    except handoff.HandoffRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    name = f"JADE-specification-{change.id}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class AuthoriseInput(ApiModel):
    note: str = ""


@router.post("/changes/{change_id}/handoff/authorise", response_model=Change)
def authorise(change_id: str, payload: AuthoriseInput, ctx: AuthContext = Depends(require_write_access)) -> Change:
    change = _change(change_id, ctx)
    try:
        handoff.authorise(change, actor_name=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                          roles=set(ctx.roles), note=payload.note)
    except _REFUSALS as exc:
        raise _refused(exc) from None
    except Exception as exc:  # the technical assignment refuses an unverifiable design
        if exc.__class__.__name__ == "TechnicalRefused":
            raise HTTPException(status_code=409, detail=str(exc)) from None
        raise
    return _change(change_id, ctx)


class ImplementerInput(ApiModel):
    name: str


@router.post("/changes/{change_id}/handoff/items/{item_id}/implementer", response_model=Change)
def name_implementer(change_id: str, item_id: str, payload: ImplementerInput,
                     ctx: AuthContext = Depends(require_write_access)) -> Change:
    change = _change(change_id, ctx)
    try:
        handoff.assign(change, item_id, name=payload.name, actor_name=ctx.identity.display_name, roles=set(ctx.roles))
    except _REFUSALS as exc:
        raise _refused(exc) from None
    return _change(change_id, ctx)


class CompleteInput(ApiModel):
    implementer: str = ""
    evidence_reference: str = ""
    note: str = ""


@router.post("/changes/{change_id}/handoff/items/{item_id}/complete", response_model=Change)
def complete_item(change_id: str, item_id: str, payload: CompleteInput,
                  ctx: AuthContext = Depends(require_write_access)) -> Change:
    change = _change(change_id, ctx)
    try:
        handoff.complete(change, item_id, implementer=payload.implementer, evidence_reference=payload.evidence_reference,
                         note=payload.note, actor_name=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                         roles=set(ctx.roles))
    except _REFUSALS as exc:
        raise _refused(exc) from None
    return _change(change_id, ctx)


class VerifyInput(ApiModel):
    passed: bool
    evidence_reference: str = ""
    note: str = ""


@router.post("/changes/{change_id}/handoff/verification", response_model=Change)
def verify(change_id: str, payload: VerifyInput, ctx: AuthContext = Depends(require_write_access)) -> Change:
    change = _change(change_id, ctx)
    try:
        handoff.verify(change, passed=payload.passed, evidence_reference=payload.evidence_reference, note=payload.note,
                       actor_name=ctx.identity.display_name, actor_user_id=ctx.identity.id, roles=set(ctx.roles))
    except _REFUSALS as exc:
        raise _refused(exc) from None
    return _change(change_id, ctx)


# -- Business acceptance ----------------------------------------------------------------
class AcceptanceInput(ApiModel):
    decision: str = "accepted"
    note: str = ""
    # When the Application Manager records it for a business person.
    on_behalf_of: str = ""


@router.get("/changes/{change_id}/acceptance")
def acceptance_history(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(change_id, ctx)
    tech = _tech(change)
    return {"current": acceptance.current(change, tech), "history": acceptance.history(change),
            "basis": acceptance.basis(change, tech)}


def _tech(change: Change) -> dict:
    route = change.architect_decision.recommended_route if change.architect_decision else None
    if route not in ("Technical Agent", "Mixed"):
        return {}
    from ..services.lifecycle import _technical_facts

    return _technical_facts(change.customer_id, change.id)


@router.post("/changes/{change_id}/acceptance", response_model=Change)
def record_acceptance(change_id: str, payload: AcceptanceInput, ctx: AuthContext = Depends(require_write_access)) -> Change:
    change = _change(change_id, ctx)
    roles = set(ctx.roles)
    acting_as_owner = "domain_owner" in roles and not payload.on_behalf_of.strip()
    if acting_as_owner:
        require_domain_owner_access(ctx, change.business_domain_id)
    effective = roles if acting_as_owner else roles - {"domain_owner"}
    try:
        entry = acceptance.record(change, decision=payload.decision, note=payload.note,
                                  on_behalf_of=payload.on_behalf_of, actor_name=ctx.identity.display_name,
                                  actor_user_id=ctx.identity.id, roles=effective, tech=_tech(change))
    except _REFUSALS as exc:
        raise _refused(exc) from None
    route = change.architect_decision.recommended_route if change.architect_decision else None
    if route == "Resolve without Change" and entry["decision"] == "accepted" and change.state == "APPROVED":
        # The business accepted: only now does a no-change story close (audit 23 Oct, F00).
        from jde_mcp_server import backlog

        d = change.architect_decision
        finding = (d.result.conclusion if d.result and d.result.conclusion else d.existing_functionality_found) or ""
        who = entry.get("on_behalf_of") or ctx.identity.display_name
        backlog.close_resolved(change_id, (finding + f" -- accepted by the business ({who})").strip(" -"),
                               resolved_by=ctx.identity.display_name)
    return _change(change_id, ctx)
