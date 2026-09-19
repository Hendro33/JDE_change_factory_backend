from __future__ import annotations

from fastapi import APIRouter, Depends

from ..dependencies import resolve_identity
from ..models.session import Customer, SessionOut
from ..services.customer_service import Identity, get_registry

router = APIRouter(tags=["session"])


@router.get("/session", response_model=SessionOut)
def get_session(identity: Identity = Depends(resolve_identity)) -> SessionOut:
    registry = get_registry()
    customers = [
        Customer(
            id=c.id, name=c.name, short_name=c.short_name,
            tools_release=c.tools_release, environment=c.environment,
        )
        for c in registry.customers_for(identity)
    ]
    active = customers[0].id if customers else ""
    return SessionOut(
        user_id=identity.id,
        display_name=identity.display_name,
        role=identity.role,  # type: ignore[arg-type]
        customers=customers,
        active_customer_id=active,
    )
