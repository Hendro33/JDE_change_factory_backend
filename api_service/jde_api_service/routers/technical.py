"""
The Technical work screen's API: the approved design and baseline, the
Technical Agent's runs and package revisions, exact implementation approval,
the governed milestones and the human CNC hand-off.

Response bodies keep the backend's snake_case keys (like the evidence
manifest). Authority comes from the session; nothing here trusts a client
for the company, the actor or their roles.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from jde_mcp_server import approval, authority, execution
from jde_mcp_server.binding import BindingInvalid
from jde_mcp_server.capability_catalog import CapabilityError
from jde_mcp_server.scope import ScopeViolation
from jde_mcp_server.technical_sim import AdapterOutcome, SimObjectError

from ..config import settings
from ..dependencies import AuthContext, require_customer_access, require_role, require_write_access
from ..models.base import ApiModel
from ..services.registry import get_customer_link_service
from ..technical import driver, service, store

router = APIRouter(tags=["technical"])


class DesignApprovalInput(ApiModel):
    design_revision: int
    note: str = ""


class RunInput(ApiModel):
    purpose: Literal["prepare", "execute", "verify"] = "prepare"
    note: str = ""


class NoteInput(ApiModel):
    note: str = ""


class CncInput(ApiModel):
    package_name: str
    evidence_reference: str
    note: str = ""


class ReconcileInput(ApiModel):
    milestone: Literal["apply", "build"]
    note: str = ""


def _require_story(story_id: str, company_id: str) -> None:
    """Another company's story is simply not found."""
    if get_customer_link_service().customer_for(story_id) != company_id:
        raise HTTPException(status_code=404, detail=f"no such change: {story_id}")


def _refusal(exc: Exception) -> HTTPException:
    if isinstance(exc, (approval.ApproverNotAuthorised, authority.AuthorityRevoked)):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


_REFUSALS = (service.TechnicalRefused, approval.ChangeApprovalError, BindingInvalid, ScopeViolation, CapabilityError,
             store.StaleSubmission, SimObjectError, AdapterOutcome, authority.AuthorityRevoked,
             authority.AuthorityUnverifiable, LookupError, execution.ExecutionBlocked)


@router.get("/changes/{story_id}/technical")
def technical_work(story_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    _require_story(story_id, ctx.customer_id)
    return service.work_view(ctx.customer_id, story_id)


@router.post("/changes/{story_id}/technical/approve-design")
def approve_design(story_id: str, payload: DesignApprovalInput,
                   ctx: AuthContext = Depends(require_write_access)) -> dict:
    _require_story(story_id, ctx.customer_id)
    try:
        return service.approve_design(ctx.customer_id, story_id, payload.design_revision,
                                      actor_name=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                                      roles=set(ctx.roles), note=payload.note)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/changes/{story_id}/technical/runs", status_code=202)
def start_run(story_id: str, payload: RunInput, background: BackgroundTasks,
              ctx: AuthContext = Depends(require_role("product_manager", "admin"))) -> dict:
    """Start the Technical Agent (the real runtime) for one purpose."""
    _require_story(story_id, ctx.customer_id)
    from ..services import agent_settings

    try:
        agent_settings.require_enabled(ctx.customer_id, "technical-agent")
    except agent_settings.AgentDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        run = service.start_run(ctx.customer_id, story_id, purpose=payload.purpose, initiated_by=ctx.identity.id)
    except _REFUSALS as exc:
        raise _refusal(exc)

    def _go() -> None:
        asyncio.run(driver.run_technical_agent(company_id=ctx.customer_id, story_id=story_id, run_id=run["run_id"],
                                               repo_root=settings.repo_root, purpose=payload.purpose,
                                               note=payload.note))

    background.add_task(_go)
    return run


@router.post("/changes/{story_id}/technical/packages/{revision}/approve")
def approve_package(story_id: str, revision: int, payload: NoteInput,
                    ctx: AuthContext = Depends(require_write_access)) -> dict:
    _require_story(story_id, ctx.customer_id)
    try:
        return service.approve_package(ctx.customer_id, story_id, revision, actor_name=ctx.identity.display_name,
                                       actor_user_id=ctx.identity.id, roles=set(ctx.roles), note=payload.note)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/changes/{story_id}/technical/packages/{revision}/reject")
def reject_package(story_id: str, revision: int, payload: NoteInput,
                   ctx: AuthContext = Depends(require_write_access)) -> dict:
    _require_story(story_id, ctx.customer_id)
    if not payload.note.strip():
        raise HTTPException(status_code=422, detail="a rejection must include a reason")
    try:
        package = service.package_or_404(ctx.customer_id, story_id, revision)
        return approval.reject_change(package["change_id"], ctx.identity.display_name, payload.note,
                                      company_id=ctx.customer_id)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/changes/{story_id}/technical/packages/{revision}/cnc-activation")
def record_cnc(story_id: str, revision: int, payload: CncInput,
               ctx: AuthContext = Depends(require_role("cnc_operator"))) -> dict:
    """A human CNC deployed and activated the package; Jade records it."""
    _require_story(story_id, ctx.customer_id)
    try:
        return service.record_cnc(ctx.customer_id, story_id, revision, actor_user_id=ctx.identity.id,
                                  actor_name=ctx.identity.display_name, package_name=payload.package_name,
                                  evidence_reference=payload.evidence_reference, note=payload.note)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/changes/{story_id}/technical/packages/{revision}/reconcile")
def reconcile(story_id: str, revision: int, payload: ReconcileInput,
              ctx: AuthContext = Depends(require_role("product_manager", "admin"))) -> dict:
    _require_story(story_id, ctx.customer_id)
    try:
        return service.reconcile(ctx.customer_id, story_id, revision, payload.milestone,
                                 actor_user_id=ctx.identity.id, actor_name=ctx.identity.display_name,
                                 note=payload.note)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/changes/{story_id}/technical/packages/{revision}/{milestone}")
def run_milestone(story_id: str, revision: int, milestone: Literal["apply", "build", "verify"],
                  ctx: AuthContext = Depends(require_role("product_manager", "admin"))) -> dict:
    """An operator asks the governed executor for one milestone (the agent
    can ask too, through its own tools; both pass the same checks)."""
    _require_story(story_id, ctx.customer_id)
    try:
        return service.run_milestone(ctx.customer_id, story_id, revision, milestone,
                                     actor=f"{ctx.identity.display_name} ({ctx.identity.id})")
    except _REFUSALS as exc:
        raise _refusal(exc)
