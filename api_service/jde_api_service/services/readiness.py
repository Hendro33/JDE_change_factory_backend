"""One readiness rule for every approval surface (repair brief 24 Oct, priority 1).

A design can be authorised -- the exact change approved, a technical design
approved, a hand-off authorised, a no-change finding confirmed -- only when no
open point blocks it, whoever owns that point (a person, the Administrator or
JADE's own investigation). The next action, the buttons (lifecycle
action_blockers) and the endpoints all ask this one function, so no screen can
offer an approval the backend would refuse, and no endpoint accepts one the
screen says is blocked.
"""

from __future__ import annotations

from typing import Optional

APPROVAL_ACTIONS = ("approve_exact_change", "approve_design", "authorise_handoff", "confirm_no_change")


class NotReady(Exception):
    pass


def blockers(change, lc=None) -> list:
    lc = lc if lc is not None else change.lifecycle
    if lc is None:
        return []
    return [i for i in (lc.issues or []) if i.resolution is None and i.blocks and i.source != "delivery_gate"]


def refusal(change, lc=None) -> Optional[str]:
    open_ = blockers(change, lc)
    if not open_:
        return None
    first = open_[0]
    who = "JADE" if first.owner == "jade" else (first.owner_label or "someone")
    more = f" and {len(open_) - 1} more" if len(open_) > 1 else ""
    return f"Not ready to authorise: {len(open_)} open point{'s' if len(open_) > 1 else ''} block{'' if len(open_) > 1 else 's'} it -- {first.title} ({who}){more}."


def require_ready(change) -> None:
    reason = refusal(change)
    if reason:
        raise NotReady(reason)
