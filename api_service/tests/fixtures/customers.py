"""
Test customers -- created by the TEST SUITE, never by the product.

Jade starts empty: a fresh installation has no customers, no stories and
no business domains until an Administrator creates them. The tests need a
known starting point, so every test that uses the `client` fixture gets
these customers (same ids the tests were written against) plus the
BicycleWorks request set and business domains, created through the same
services the product uses.
"""

from __future__ import annotations

from datetime import datetime, timezone

from jde_api_service.models.business_domain import BusinessDomainCreate
from jde_api_service.models.change_request import ChangeRequestCreate
from jde_api_service.persistence.db import connection
from jde_api_service.services.business_domain_service import BusinessDomainService
from jde_api_service.services.change_request_service import ChangeRequestService
from jde_api_service.services.customer_link_service import CustomerLinkService

from .pilot_business_domains_bicycleworks import BICYCLEWORKS_DOMAINS
from .pilot_business_domains_bicycleworks import CUSTOMER_ID as DOMAINS_CUSTOMER_ID
from .pilot_data_bicycleworks import CUSTOMER_ID, DIRECT_INPUTS, TOPDESK_TICKETS
from .topdesk_tickets import normalize

TEST_CUSTOMERS = [
    {"id": "vdb", "name": "Van den Berg Logistiek", "short_name": "Van den Berg", "tools_release": "9.2.7", "environment": "DEV"},
    {"id": "nhd", "name": "Noord-Holland Dairy", "short_name": "NH Dairy", "tools_release": "9.2.8", "environment": "DEV"},
    {"id": "mrv", "name": "Maasrivier Industrials", "short_name": "Maasrivier", "tools_release": "9.2.5", "environment": "DEV"},
    {"id": "bwm", "name": "BicycleWorks Manufacturing BV", "short_name": "BicycleWorks", "tools_release": "9.2.7", "environment": "DEV"},
]


def create_test_customers() -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connection(immediate=True) as conn:
        for c in TEST_CUSTOMERS:
            conn.execute(
                # TRANSITIONAL: is_demo=1 until simulated delivery is replaced by
                # the recorded delivery route; then these are ordinary customers.
                "INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, is_demo) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT DO NOTHING",
                (c["id"], c["name"], c["short_name"], c["tools_release"], c["environment"], now),
            )


def create_bicycleworks_requests(change_request_service: ChangeRequestService) -> list[str]:
    created: list[str] = []
    for ticket in TOPDESK_TICKETS:
        stable_id = f"CR-BW-{ticket.ticket_number}"
        if change_request_service.get(stable_id) is not None:
            continue
        change_request_service._create(customer_id=CUSTOMER_ID, request_id=stable_id, **normalize(ticket))
        created.append(stable_id)
    for direct in DIRECT_INPUTS:
        stable_id = f"CR-BW-{direct.request_id}"
        if change_request_service.get(stable_id) is not None:
            continue
        change_request_service.create_direct(
            ChangeRequestCreate(title=direct.title, business_source="Business", source_reference=direct.request_id,
                                raw_content=direct.raw_content),
            customer_id=CUSTOMER_ID, requester=direct.requester, request_id=stable_id,
        )
        created.append(stable_id)
    return created


def create_bicycleworks_domains(business_domain_service: BusinessDomainService) -> list[str]:
    created: list[str] = []
    existing_ids = {d.id for d in business_domain_service.list_for_customer(DOMAINS_CUSTOMER_ID)}
    for seed in BICYCLEWORKS_DOMAINS:
        if seed.domain_id in existing_ids:
            continue
        business_domain_service.create(
            BusinessDomainCreate(apqc_code=seed.apqc_code, name=seed.name, level=seed.level,
                                 description=seed.description),
            customer_id=DOMAINS_CUSTOMER_ID, domain_id=seed.domain_id,
        )
        created.append(seed.domain_id)
    return created


def link_t001(customer_link_service: CustomerLinkService) -> None:
    customer_link_service.link("CR-BW-T001", CUSTOMER_ID)


def create_all() -> None:
    from jde_api_service.services.registry import (
        get_business_domain_service,
        get_change_request_service,
        get_customer_link_service,
    )

    create_test_customers()
    create_bicycleworks_requests(get_change_request_service())
    create_bicycleworks_domains(get_business_domain_service())
    link_t001(get_customer_link_service())
