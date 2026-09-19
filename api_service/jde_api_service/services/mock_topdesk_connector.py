"""
Mock Topdesk connector.

Per the approved architecture: a source connector's only job is
obtaining and normalising source material into the common ChangeRequest
model -- the downstream Change Factory workflow never sees a
Topdesk-shaped object. TopdeskTicket is what a real Topdesk API would
hand us; normalize() is the ONLY place that knows Topdesk's field
names. Nothing outside this module reads ticket_number/brief_description/
request/caller directly.

No live Topdesk instance is connected, and this module never attempts
one -- list_tickets() returns fixture data only (see
persistence/pilot_data_bicycleworks.py for the actual tickets). A real
connector would replace list_tickets()'s body with an HTTP call and
keep normalize() unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models.change_request import ChangeRequestSourceType


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


class MockTopdeskConnector:
    def __init__(self, tickets: list[TopdeskTicket]) -> None:
        self._tickets = tickets

    def list_tickets(self) -> list[TopdeskTicket]:
        return list(self._tickets)

    @staticmethod
    def normalize(ticket: TopdeskTicket) -> dict:
        """The Topdesk -> ChangeRequest field mapping. Everything
        downstream of this function operates on the common model only."""
        return {
            "title": ticket.brief_description,
            "business_source": "Support / Topdesk",
            "source_reference": f"Topdesk {ticket.ticket_number}",
            "raw_content": ticket.request,
            "requester": ticket.caller,
            "source_type": ChangeRequestSourceType.TOPDESK,
        }
