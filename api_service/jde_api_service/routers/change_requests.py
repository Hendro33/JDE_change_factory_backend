from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import AuthContext, require_write_access
from ..models.change_request import ChangeRequest, ChangeRequestCreate
from ..services.registry import get_change_request_service

router = APIRouter(tags=["change-requests"])


@router.post("/change-requests", response_model=ChangeRequest, status_code=201)
def create_change_request(
    payload: ChangeRequestCreate,
    ctx: AuthContext = Depends(require_write_access),
) -> ChangeRequest:
    """Direct text entry only (Phase 1). customer_id comes from the
    already-validated X-Customer-Id, and requester comes from the
    resolved identity -- neither is accepted from the request body, so
    a caller cannot create a request on behalf of another customer or
    person just by putting different values in the JSON."""
    return get_change_request_service().create_direct(
        payload, customer_id=ctx.customer_id, requester=ctx.identity.display_name
    )
