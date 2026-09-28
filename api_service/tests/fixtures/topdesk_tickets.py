"""Test-only Topdesk-shaped tickets for the BicycleWorks test customer."""

from __future__ import annotations

from dataclasses import dataclass

from jde_api_service.models.change_request import ChangeRequestSourceType


@dataclass(frozen=True)
class TopdeskTicket:
    ticket_number: str
    brief_description: str
    # The ticket body, preserved verbatim -- never edited or summarised
    # by this connector.
    request: str
    # Who raised it. Real Topdesk always has a caller; for fixture
    # tickets that don't name one, this is the role/department the
    # ticket text itself identifies (e.g. "our sales team"), not an
    # invented person.
    caller: str


def normalize(ticket: TopdeskTicket) -> dict:
    """The Topdesk -> ChangeRequest field mapping for the test tickets."""
    return {
        "title": ticket.brief_description,
        "business_source": "Support / Topdesk",
        "source_reference": f"Topdesk {ticket.ticket_number}",
        "raw_content": ticket.request,
        "requester": ticket.caller,
        "source_type": ChangeRequestSourceType.TOPDESK,
    }
