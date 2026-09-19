"""
BicycleWorks Manufacturing BV pilot dataset.

Nine mock Topdesk tickets and two non-Topdesk business inputs, used to
exercise all three intake routes (Topdesk, direct/business) against
the common ChangeRequest model. Source text is preserved verbatim --
nothing here has been tidied, reworded, or had missing details filled
in. T009 in particular is deliberately inadequate; that is the point
of including it (it should be bounced by the Receive/Improve/Check
quality gate once that pipeline exists, not "fixed" here).

`caller` on each TopdeskTicket is the role/department the ticket text
itself names as raising or affected by the issue -- never an invented
individual. Where the text does not clearly name one (T007, T009),
that is left honestly unspecified rather than guessed, matching the
same principle Section 3.6 applies to business_impact: a blank is a
legitimate answer, a confident-sounding invention is not.

No AI-generated fields (user story, architect decision, implementation
spec) are attached anywhere in this file -- these are raw source
material for the agents, exactly as the pilot requires.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..services.mock_topdesk_connector import TopdeskTicket

CUSTOMER_ID = "bwm"

UNSPECIFIED_REPORTER = "BicycleWorks (reporter not specified in ticket)"

TOPDESK_TICKETS: list[TopdeskTicket] = [
    TopdeskTicket(
        ticket_number="T001",
        brief_description="Default delivery date is wrong",
        request=(
            "When our sales team enters a new sales order, the requested delivery date "
            "is currently defaulted to today's date. For bicycles that are not "
            "immediately available, this causes confusion and the sales team has to "
            "manually change the date. We would like the requested delivery date to "
            "default to 7 working days from the order date."
        ),
        caller="BicycleWorks Sales Team",
    ),
    TopdeskTicket(
        ticket_number="T002",
        brief_description="Warehouse users cannot easily see available stock",
        request=(
            "Warehouse staff need to know how many bicycles are actually available to "
            "promise to customers. At the moment they look at item availability "
            "information and have to manually interpret several quantities. We would "
            "like a simple way of seeing On hand, Allocated, Available, On order and "
            "Backordered for a bicycle item."
        ),
        caller="BicycleWorks Warehouse",
    ),
    TopdeskTicket(
        ticket_number="T003",
        brief_description="Damaged bicycle stock write-off",
        request=(
            "We regularly receive bicycles back from dealers because they have been "
            "damaged during transport or handling. Currently warehouse staff manually "
            "adjust inventory and then email Finance explaining what happened.\n"
            "We want a controlled stock write-off process. The warehouse employee "
            "should be able to identify the bicycle/item, enter the quantity and "
            "reason for the write-off, and submit it for approval. Once approved, the "
            "stock should be removed from available inventory and the transaction "
            "should be traceable for Finance.\n"
            "Write-off reasons should include Transport damage, Warehouse damage, "
            "Quality failure, Obsolete stock and Other.\n"
            "Finance needs reporting on the value of stock written off by reason."
        ),
        caller="BicycleWorks Warehouse",
    ),
    TopdeskTicket(
        ticket_number="T004",
        brief_description="Dealers need partial shipment visibility",
        request=(
            "Dealers frequently order multiple bicycle models on one sales order. When "
            "only part of the order can be shipped, customer service cannot easily "
            "tell the dealer which bicycles have already shipped and which are still "
            "waiting for stock. We would like the sales order view to make partial "
            "shipment status easier to understand."
        ),
        caller="BicycleWorks Customer Service",
    ),
    TopdeskTicket(
        ticket_number="T005",
        brief_description="Automatic reorder point for fast-moving bicycles",
        request=(
            "Our most popular commuter bicycle models frequently run out of stock. "
            "Supply Chain wants JDE to automatically recommend replenishment when "
            "stock falls below a minimum level. The minimum should be different for "
            "each bicycle model and should be based on historical demand and supplier "
            "lead time where possible."
        ),
        caller="BicycleWorks Supply Chain",
    ),
    TopdeskTicket(
        ticket_number="T006",
        brief_description="Sales order processing option change",
        request=(
            "The sales team wants the default behaviour for a particular sales order "
            "processing option changed because users are currently having to override "
            "it on almost every order. Please investigate whether this can be changed "
            "through standard JDE configuration rather than development."
        ),
        caller="BicycleWorks Sales Team",
    ),
    TopdeskTicket(
        ticket_number="T007",
        brief_description="Sales order unexpectedly on hold",
        request=(
            "Several customer orders for model BW-ROAD-500 are being placed on credit "
            "hold even though the customers have available credit according to "
            "Finance.\n"
            "Customer: C10045\n"
            "Sales Order: 123456\n"
            "Credit limit: €50,000\n"
            "Outstanding: €18,000\n"
            "Order value: €2,400\n"
            "The issue started this morning. Please investigate why the order is being "
            "placed on hold."
        ),
        caller=UNSPECIFIED_REPORTER,
    ),
    TopdeskTicket(
        ticket_number="T008",
        brief_description="Inventory quantity does not match warehouse count",
        request=(
            "The warehouse has physically counted 24 units of bicycle model "
            "BW-CITY-300. JDE shows 31 units available. The warehouse has checked the "
            "physical location twice and the count of 24 appears correct. Please "
            "investigate the discrepancy. No inventory adjustment should be made until "
            "the cause has been identified."
        ),
        caller="BicycleWorks Warehouse",
    ),
    TopdeskTicket(
        ticket_number="T009",
        brief_description="Bike orders are wrong",
        request=(
            "JDE is causing problems with our bike orders. Please fix it urgently. "
            "Management is unhappy."
        ),
        caller=UNSPECIFIED_REPORTER,
    ),
]


@dataclass(frozen=True)
class DirectInput:
    request_id: str
    title: str
    raw_content: str
    requester: str


DIRECT_INPUTS: list[DirectInput] = [
    DirectInput(
        request_id="P001",
        title="New dealer returns process",
        raw_content=(
            "BicycleWorks wants to introduce a formal process for dealer returns. "
            "Dealers should request a return number before shipping a bicycle back. "
            "Warehouse staff inspect the returned bicycle and classify it as "
            "resaleable, repair required, damaged, warranty claim or scrap. The system "
            "should track the return from initial request through inspection and "
            "final disposition. Finance needs the value impact of the return. "
            "Management wants monthly reporting by dealer, bicycle model and return "
            "reason."
        ),
        requester="BicycleWorks Management",
    ),
    DirectInput(
        request_id="P002",
        title="Stock write-off idea",
        raw_content=(
            "We need to improve the way warehouse staff deal with damaged and obsolete "
            "bicycles. At the moment they either adjust inventory directly or email "
            "Finance. I'd like a simple function where someone can select the item, "
            "quantity and reason for the write-off. It should probably require "
            "approval for larger amounts. Finance should be able to see what has been "
            "written off and why. We also want to know how much stock we are losing "
            "through transport damage versus warehouse damage. Can you work out what "
            "we would need?"
        ),
        requester=UNSPECIFIED_REPORTER,
    ),
]
