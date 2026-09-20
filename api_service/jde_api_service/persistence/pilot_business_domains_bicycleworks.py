"""
A small, REPRESENTATIVE set of BicycleWorks business domains -- enough
to classify the existing pilot tickets (T001-T009, P001-P002), not the
APQC catalogue. Loading the full catalogue is explicitly out of scope
for this increment (see BusinessDomain's own docstring).

Each domain's `_covers` comment below is documentation only -- it is
NOT applied automatically to the pilot ChangeRequests. Per the task
that introduced this: T002-T009 are not processed as part of this
development task, and that includes not auto-classifying them. T001 is
classified live, through the API, as part of the T001 governance-flow
walkthrough this increment ships with -- see the domain-governance
tests and GETTING_STARTED-style walkthrough for that flow.

T009 ("Bike orders are wrong") is deliberately left uncovered by any
domain here -- it is too vague to place honestly, which is the whole
point of including it in the pilot set (see pilot_data_bicycleworks.py).
A human reviewing it is expected to mark it domain-classification-
uncertain rather than force it into one of these.

domain_owner is left blank ("not yet named") throughout: the pilot
ticket text never names who owns a given business domain end to end,
only who raised or is affected by a specific ticket (see `caller` on
each TopdeskTicket) -- and those are not the same thing. Inventing an
owner name here would be exactly the kind of confident-sounding guess
this codebase's own conventions (see UNSPECIFIED_REPORTER) exist to
avoid.
"""

from __future__ import annotations

from dataclasses import dataclass

CUSTOMER_ID = "bwm"


@dataclass(frozen=True)
class DomainSeed:
    domain_id: str
    apqc_code: str
    name: str
    level: str
    description: str


BICYCLEWORKS_DOMAINS: list[DomainSeed] = [
    DomainSeed(
        domain_id="DOM-BWM-SUPPLY-PLAN",
        apqc_code="4.1",
        name="Plan Supply Chain & Demand",
        level="4.1",
        description="Demand planning, reorder points and replenishment for finished bicycle inventory. Covers T005.",
    ),
    DomainSeed(
        domain_id="DOM-BWM-WAREHOUSE",
        apqc_code="4.4",
        name="Manage Logistics & Warehousing",
        level="4.4",
        description="Warehouse stock visibility, inventory accuracy and controlled write-offs. Covers T002, T003, T008, P001, P002.",
    ),
    DomainSeed(
        domain_id="DOM-BWM-ORDER-FULFIL",
        apqc_code="4.4.3",
        name="Order Fulfilment & Shipment Management",
        level="4.4.3",
        description="Sales order entry, delivery date defaults and dealer shipment status. Covers T001, T004, T006.",
    ),
    DomainSeed(
        domain_id="DOM-BWM-CUST-SERVICE",
        apqc_code="6.1",
        name="Manage Customer Service",
        level="6.1",
        description="Dealer- and customer-facing order visibility and enquiry handling.",
    ),
    DomainSeed(
        domain_id="DOM-BWM-CREDIT",
        apqc_code="9.3",
        name="Manage Order-to-Cash / Credit & Collections",
        level="9.3",
        description="Customer credit limits, credit holds and accounts-receivable exposure on sales orders. Covers T007.",
    ),
]
