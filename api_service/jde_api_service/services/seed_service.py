"""
Idempotent pilot-dataset seeding.

Ensures the BicycleWorks Manufacturing BV pilot dataset (9 mock
Topdesk tickets + 2 direct business inputs) exists as ChangeRequest
records. Safe to call any number of times: each record uses a stable
id (CR-BW-<ticket/input id>), and a record already present is left
untouched, never duplicated or overwritten.

Called once on FastAPI startup (see main.py) so the dataset is always
there with no manual step -- and independently callable from tests
against an isolated store.
"""

from __future__ import annotations

from ..models.change_request import ChangeRequestCreate
from ..persistence.pilot_data_bicycleworks import CUSTOMER_ID, DIRECT_INPUTS, TOPDESK_TICKETS
from .change_request_service import ChangeRequestService


def ensure_bicycleworks_pilot_dataset(change_request_service: ChangeRequestService) -> list[str]:
    """Returns the ids of any records actually created (empty if the
    dataset was already fully present)."""
    created: list[str] = []

    for ticket in TOPDESK_TICKETS:
        stable_id = f"CR-BW-{ticket.ticket_number}"
        if change_request_service.get(stable_id) is not None:
            continue
        change_request_service.create_from_topdesk(ticket, CUSTOMER_ID, request_id=stable_id)
        created.append(stable_id)

    for direct in DIRECT_INPUTS:
        stable_id = f"CR-BW-{direct.request_id}"
        if change_request_service.get(stable_id) is not None:
            continue
        change_request_service.create_direct(
            ChangeRequestCreate(
                title=direct.title,
                business_source="Business",
                source_reference=direct.request_id,
                raw_content=direct.raw_content,
            ),
            customer_id=CUSTOMER_ID,
            requester=direct.requester,
            request_id=stable_id,
        )
        created.append(stable_id)

    return created
