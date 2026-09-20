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

from ..models.business_domain import BusinessDomainCreate
from ..models.change_request import ChangeRequestCreate
from ..persistence.pilot_business_domains_bicycleworks import BICYCLEWORKS_DOMAINS
from ..persistence.pilot_business_domains_bicycleworks import CUSTOMER_ID as DOMAINS_CUSTOMER_ID
from ..persistence.pilot_data_bicycleworks import CUSTOMER_ID, DIRECT_INPUTS, TOPDESK_TICKETS
from .business_domain_service import BusinessDomainService
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


def ensure_bicycleworks_business_domains(business_domain_service: BusinessDomainService) -> list[str]:
    """Idempotent, same convention as ensure_bicycleworks_pilot_dataset:
    a small representative set of business domains (not the APQC
    catalogue -- see BICYCLEWORKS_DOMAINS's own docstring), safe to call
    on every startup."""
    created: list[str] = []
    existing_ids = {d.id for d in business_domain_service.list_for_customer(DOMAINS_CUSTOMER_ID)}
    for seed in BICYCLEWORKS_DOMAINS:
        if seed.domain_id in existing_ids:
            continue
        business_domain_service.create(
            BusinessDomainCreate(
                apqc_code=seed.apqc_code,
                name=seed.name,
                level=seed.level,
                description=seed.description,
            ),
            customer_id=DOMAINS_CUSTOMER_ID,
            domain_id=seed.domain_id,
        )
        created.append(seed.domain_id)
    return created
