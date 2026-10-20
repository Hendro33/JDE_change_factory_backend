"""
Administrator tools for setting up test customers: clear a customer's work
(keeping its settings), copy a working set-up from another customer, and
read the JD Edwards estate into a reference document.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import AuthContext, require_role
from ..models.base import ApiModel
from ..services import customer_admin

router = APIRouter(tags=["customer-admin"])


class ClearWorkInput(ApiModel):
    confirm_name: str
    remove_sample_data: bool = False


class CopySetupInput(ApiModel):
    source_customer_id: str
    move_jira: bool = True


def _customer_name(customer_id: str) -> str:
    from ..services.customer_service import get_registry

    c = get_registry().get_customer(customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"no such customer: {customer_id}")
    return c.name


@router.post("/admin/customer/clear-work")
def clear_work(payload: ClearWorkInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict[str, Any]:
    """Removes every request, story, design, change and their records; keeps the settings."""
    name = _customer_name(ctx.customer_id)
    if payload.confirm_name.strip() != name:
        raise HTTPException(status_code=422, detail=f"type the customer's name exactly ({name}) to confirm")
    try:
        return customer_admin.clear_work(ctx.customer_id, actor_user_id=ctx.identity.id,
                                         remove_sample_data=payload.remove_sample_data)
    except customer_admin.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc))


@router.post("/admin/customer/copy-setup")
def copy_setup(payload: CopySetupInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict[str, Any]:
    """Copies the JD Edwards connection, engagement scope, company settings and AI connection from another
    customer into this one; moves the Jira connection."""
    _customer_name(payload.source_customer_id)
    try:
        return customer_admin.copy_setup(payload.source_customer_id, ctx.customer_id, actor_user_id=ctx.identity.id,
                                         actor_name=ctx.identity.display_name, move_jira=payload.move_jira)
    except customer_admin.NotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/admin/jde/estate")
def read_estate(ctx: AuthContext = Depends(require_role("admin"))) -> dict[str, Any]:
    """Reads companies, branch plants, items, customers, suppliers and code lists (at most 10 rows each)
    and saves them as a reference document."""
    from ..discovery import estate, service

    try:
        return estate.read_estate(ctx.customer_id, actor_user_id=ctx.identity.id, actor_name=ctx.identity.display_name)
    except service.DiscoveryBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
