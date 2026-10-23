"""Which actions may be taken on a story right now, and why not (audit 23 Oct,
F02).

The next action, the buttons, the queues and the notifications must follow
the same prerequisites. The next action already comes from the lifecycle;
this adds, for the controls that act on JD Edwards or start an agent, whether
the backend would accept them now -- asked from the same gates the endpoints
use (read-only), never re-derived in the browser.
"""

from __future__ import annotations

from typing import Optional

from ..models.lifecycle import Lifecycle


def _delivery_refusal(change) -> Optional[str]:
    """The functional delivery gate's refusal for this change, or None."""
    ap = change.change_approval
    if ap is None or ap.status != "approved" or not ap.approval_id:
        return "The exact change is not approved."
    try:
        from jde_mcp_server import approval

        approval.authorise_functional_delivery(ap.approval_id)
    except Exception as exc:  # noqa: BLE001 -- every refusal is a reason
        return str(exc)
    return None


def _test_refusal(change) -> Optional[str]:
    """The test gate's refusal for an applied change, or None."""
    ap = change.change_approval
    if ap is None or ap.status != "approved" or not ap.approval_id:
        return "The exact change is not approved."
    try:
        from jde_mcp_server import approval

        approval.require_change_covers_test(ap.approval_id, None)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


def apply(change, lc: Lifecycle) -> Lifecycle:
    allowed: list[str] = []
    blocked: dict[str, str] = {}
    ec = change.exact_change
    ex = ec.execution if ec else None
    if ec is not None and lc.phase in ("delivery", "validation") and ex is not None and ex.write_state != "applied":
        refusal = _delivery_refusal(change)
        gate = next((i for i in lc.issues if i.source == "delivery_gate" and i.resolution is None), None)
        reason = (gate.title + " " + gate.action).strip() if gate else refusal
        for action in ("record_applied", "record_item", "run_agents"):
            if refusal is None:
                allowed.append(action)
            else:
                blocked[action] = reason or "The delivery check refuses it now."
    if ec is not None and lc.phase in ("delivery", "validation") and ex is not None and ex.write_state == "applied" \
            and ex.test_state not in ("completed",):
        refusal = _test_refusal(change)
        gate = next((i for i in lc.issues if i.source == "delivery_gate" and i.resolution is None), None)
        reason = (gate.title + " " + gate.action).strip() if gate else refusal
        for action in ("record_test", "run_test"):
            if refusal is None:
                allowed.append(action)
            else:
                blocked[action] = reason or "The test check refuses it now."
    if lc.route in ("Technical Agent", "Mixed") and lc.phase == "delivery":
        if lc.next_action.action == "start_technical_prepare":
            allowed.append("start_technical_prepare")
        elif lc.next_action.action in ("provide_technical_evidence", "answer_issue", "rerun_solutioning") \
                and lc.health == "blocked":
            blocked["start_technical_prepare"] = lc.next_action.summary
    lc.allowed_actions = allowed
    lc.action_blockers = blocked
    return lc
