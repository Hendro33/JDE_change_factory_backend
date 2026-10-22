"""
DeliveryQueueEntry -- the minimum useful representation of "the set of
approved changes Jade is authorised to work on" (Increment: Continuous
Delivery Flow).

Deliberately NOT a Sprint: no start/end dates, no capacity, no planning
ceremony. An entry is added to the queue by exactly one human decision
(the Application Manager's approval, routers/domain_governance.py) and
otherwise just records where it sits and whether anything is blocking
it -- ordering, dependencies and blocking status are informational
fields for this increment, not an enforced scheduler.
"""

from __future__ import annotations

from typing import Literal, Optional

from .base import ApiModel

DeliveryStatus = Literal["queued", "in_progress", "blocked"]


class DeliveryQueueEntry(ApiModel):
    change_id: str
    customer_id: str
    position: int
    status: DeliveryStatus = "queued"
    delivery_type: Optional[str] = None
    current_owner: Optional[str] = None
    blocked_reason: Optional[str] = None
    added_by: str
    added_at: str
    note: str = ""
    # Read-only, from the change's canonical lifecycle when the queue is
    # listed (services/lifecycle.py): the queue shows the same state as every
    # other screen. The stored status above is kept for the record only.
    title: str = ""
    phase: Optional[str] = None
    phase_label: str = ""
    health: Optional[str] = None
    health_label: str = ""
    next_action: str = ""
    next_action_owner: str = ""
