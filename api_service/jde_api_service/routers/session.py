from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import Identity, resolve_identity
from ..models.session import Customer, SessionOut
from ..services import auth_service, membership_service

router = APIRouter(tags=["session"])


@router.get("/session", response_model=SessionOut)
def get_session(identity: Identity = Depends(resolve_identity)) -> SessionOut:
    companies = membership_service.companies_for_user(identity.id)
    customers = [
        Customer(
            id=c["company_id"], name=c["name"], short_name=c["short_name"],
            tools_release=c["tools_release"], environment=c["environment"], roles=c["roles"],
        )
        for c in companies
    ]
    active = customers[0].id if customers else ""
    active_roles = customers[0].roles if customers else []
    user = auth_service.get_user_by_id(identity.id)
    return SessionOut(
        user_id=identity.id,
        display_name=identity.display_name,
        email=user.email if user else "",
        role=", ".join(active_roles),
        customers=customers,
        active_customer_id=active,
    )
