"""
Runs an approved configuration change set in the customer's DEV system,
item by item, in the approved order.

For each item marked "agent", in order:

  1. The whole delivery gate runs again (approval unexpired, the approver's
     and the initiating Application Manager's CURRENT authority, the item
     inside the customer's scope, DEV only) and the attempt is recorded
     BEFORE anything is sent -- refused while writes are paused.
  2. A live read before the change: it must still show the state the
     approval was bound to. Anything else stops the item.
  3. The change, limited to exactly the approved item (AIS form requests,
     or the web client in an agent-driven browser).
  4. A live read-back that must show exactly the approved values. Only then
     is the item recorded as applied. A mismatch, or an outcome that cannot
     be established, stops the change set: the item is flagged for
     reconciliation and never retried blindly.

The run stops at the first item marked "person" that is not yet recorded;
it resumes once that person has recorded it. When every item is recorded,
the change counts as applied.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from jde_mcp_server import approval, authority, config_items, execution
from jde_mcp_server.evidence import capture_evidence

from . import routes

logger = logging.getLogger(__name__)

AGENT_NAMES = {routes.AIS: "Functional Agent (AIS)", routes.BROWSER: "Functional Agent (web client)"}
_running: dict[str, threading.Lock] = {}
_running_guard = threading.Lock()


class RunRefused(RuntimeError):
    pass


def _lock_for(change_id: str) -> threading.Lock:
    with _running_guard:
        return _running.setdefault(change_id, threading.Lock())


def _authorise(change_id: str, initiator_user_id: str, initiator_name: str) -> tuple[dict, dict]:
    from ..delivery.functional import RECORDER_ROLES

    record, scope = approval.authorise_functional_delivery(change_id)
    try:
        authority.require_current_approver(initiator_user_id, record["company_id"], RECORDER_ROLES)
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise approval.ApproverNotAuthorised(
            f"{initiator_name} may not have the agents deliver this change: {exc}") from exc
    return record, scope


def item_status(record: dict, item: dict) -> dict[str, Any]:
    """What an item is waiting for, for people and for the lifecycle."""
    delivered = (record.get("item_delivery") or {}).get(item["id"])
    if delivered:
        return {"state": "applied", "by": delivered.get("executor") or "person", "detail": delivered.get("source", "")}
    state = execution.item_state(record, item["id"])
    block = execution.item_block(record, item["id"]) or {}
    last = (block.get("attempts") or [{}])[-1] if block.get("attempts") else None
    if state in ("unknown", "diverged", "in_progress"):
        return {"state": state, "by": "agent", "detail": (last or {}).get("detail", "")}
    if config_items.executor_of(item) == "person":
        return {"state": "waiting_for_person", "by": "person", "detail": item.get("route_reason", "")}
    ready, why = routes.readiness(record["company_id"], item.get("route", ""), item.get("capability_id"))
    if last and last.get("outcome") == "not_sent":
        return {"state": "agent_could_not_apply", "by": "agent", "detail": last.get("detail", ""),
                "handover_allowed": True}
    if not ready:
        return {"state": "agent_unavailable", "by": "agent", "detail": why, "handover_allowed": True}
    return {"state": "waiting_for_agent", "by": "agent", "detail": ""}


def handover_reason(record: dict, item: dict) -> Optional[str]:
    """Why a person may record an item marked "agent" (None: they may not).
    Only when no agent can apply it now: agent execution switched off or not
    set up, or the agent stopped before anything was saved."""
    status = item_status(record, item)
    if status.get("handover_allowed"):
        return f"handed to a person: {status['detail']}"
    return None


def run(change_id: str, *, initiator_user_id: str, initiator_name: str, retry_not_sent: bool = False) -> dict[str, Any]:
    """Run the agents over the change set until it is complete or an item
    stops it. Returns what happened, item by item.

    retry_not_sent: a person asked the agents to run again (Run the agents),
    so an item whose last attempt stopped before anything was sent is tried
    again. Automatic resumes never retry it."""
    lock = _lock_for(change_id)
    if not lock.acquire(blocking=False):
        return {"outcome": "already_running", "items": []}
    try:
        return _run(change_id, initiator_user_id, initiator_name, retry_not_sent)
    finally:
        lock.release()


def run_in_background(change_id: str, initiator_user_id: str, initiator_name: str, retry_not_sent: bool = False) -> None:
    try:
        result = run(change_id, initiator_user_id=initiator_user_id, initiator_name=initiator_name,
                     retry_not_sent=retry_not_sent)
        logger.info("agent delivery of %s: %s", change_id, result.get("outcome"))
    except Exception:  # noqa: BLE001 -- a background run never takes the server down; its state is recorded
        logger.exception("agent delivery of %s stopped", change_id)


def _run(change_id: str, initiator_user_id: str, initiator_name: str, retry_not_sent: bool) -> dict[str, Any]:
    from ..delivery.functional import complete_if_all_recorded

    record, _scope = _authorise(change_id, initiator_user_id, initiator_name)
    if not config_items.is_change_set(record):
        raise RunRefused("only a configuration change set is delivered by the agents")
    results = []
    for item in config_items.items_of(record):
        record = approval._load(change_id)
        if item["id"] in (record.get("item_delivery") or {}):
            continue
        status = item_status(record, item)
        if status["state"] == "agent_could_not_apply" and retry_not_sent:
            ready, why = routes.readiness(record["company_id"], item.get("route", ""), item.get("capability_id"))
            status = {"state": "waiting_for_agent", "detail": ""} if ready else {
                "state": "agent_unavailable", "detail": why, "handover_allowed": True}
            retry_not_sent = False  # one deliberate retry per request, for the first item that needs it
        if status["state"] != "waiting_for_agent":
            results.append({"itemId": item["id"], "outcome": "stopped", **status})
            return {"outcome": "stopped", "stoppedAt": item["id"], "reason": status["detail"] or status["state"],
                    "items": results}
        result = run_item(change_id, item, initiator_user_id=initiator_user_id, initiator_name=initiator_name)
        results.append(result)
        if result["outcome"] != "applied":
            return {"outcome": "stopped", "stoppedAt": item["id"], "reason": result["detail"], "items": results}
    complete_if_all_recorded(change_id, actor_user_id=initiator_user_id, actor_name=initiator_name)
    return {"outcome": "applied", "items": results}


def _before_of(record: dict, item_id: str) -> dict:
    return (((record.get("binding") or {}).get("before_state") or {}).get("items") or {}).get(item_id) or {}


def run_item(change_id: str, item: dict, *, initiator_user_id: str, initiator_name: str) -> dict[str, Any]:
    from ..delivery.functional import matches_approved
    from ..delivery.readers import read_item

    route = item.get("route")
    agent = AGENT_NAMES.get(route, "Functional Agent")
    what = f"{item['id']} {config_items.label(item)}"
    attempt_id = execution.begin_item(change_id, item["id"], route=route, agent=agent,
                                      revalidate=lambda: _authorise(change_id, initiator_user_id, initiator_name))
    record = approval._load(change_id)

    def stop(outcome: str, detail: str, **extra) -> dict[str, Any]:
        execution.finish_item(change_id, item["id"], attempt_id, outcome, detail, extra=extra)
        capture_evidence(record["story_id"], {
            "event": "item_agent_stopped" if outcome == "not_sent" else "item_agent_outcome_unknown",
            "stage": "delivery", "actor": agent, "change_id": change_id, "item_id": item["id"],
            "detail": f"{what}: {detail}", "attempt_id": attempt_id, "initiated_by": initiator_name, **extra})
        return {"itemId": item["id"], "outcome": "not_applied" if outcome == "not_sent" else "unknown",
                "detail": detail, "attemptId": attempt_id}

    # 1. The live read before the change.
    try:
        now = read_item(record, item, initiator_user_id)
    except Exception as exc:  # noqa: BLE001 -- nothing was sent
        return stop("not_sent", f"the live read before the change failed: {exc}")
    bound = _before_of(record, item["id"])
    if not now.get("known") and item["kind"] not in config_items.VERSION_KINDS:
        return stop("not_sent", f"Jade cannot read the item before the change ({now.get('reason')}); nothing was sent")
    if now.get("known") and bound.get("known") and now["value"] != bound.get("value"):
        return stop("not_sent", f"DEV no longer shows the state the approval was bound to (approved against "
                                f"{bound.get('value')!r}, now {now['value']!r}); nothing was sent -- reassess",
                    before=now.get("value"))
    before_value = now.get("value") if now.get("known") else None

    # 2. The change, exactly the approved item.
    log: list[dict] = []
    screenshots: list[dict] = []
    try:
        if route == routes.AIS:
            from . import ais

            outcome = ais.apply_item(record["company_id"], item, log)
        elif route == routes.BROWSER:
            from . import browser

            outcome = browser.apply_item(record, item, log, screenshots, initiator_user_id=initiator_user_id)
        else:
            return stop("not_sent", f"no agent route {route!r}")
    except _not_sent_errors() as exc:
        return stop("not_sent", str(exc), requests=_summary(log), screenshots=screenshots)
    except Exception as exc:  # noqa: BLE001 -- after starting, anything unclean is unknown
        return stop("unknown", f"{type(exc).__name__}: {exc}", requests=_summary(log), screenshots=screenshots)

    # 3. The live read-back.
    after = read_item(record, item, initiator_user_id)
    observed = after.get("value") if after.get("known") else getattr(outcome, "observed", None)
    live = bool(after.get("known"))
    if not live and observed is None:
        return stop("unknown", f"{outcome.detail}; Jade cannot read the item back ({after.get('reason')}) -- "
                               "reconcile it", requests=_summary(log), screenshots=screenshots)
    if not matches_approved(item, observed):
        return stop("unknown", f"{outcome.detail}; the read-back does not show exactly the approved values "
                               f"({observed!r}) -- the item is stopped for reconciliation",
                    requests=_summary(log), screenshots=screenshots, observed=observed)
    source = after.get("source") if live else getattr(outcome, "observed_source", "observed in the web client")
    delivery = {"by": agent, "user_id": initiator_user_id, "initiated_by": initiator_name, "observed": observed,
                "before": before_value, "source": source, "live": live, "executor": "agent", "route": route,
                "attempt_id": attempt_id, "evidence_reference": f"agent attempt {attempt_id}; {source}",
                "screenshots": [s["storage_key"] for s in screenshots], "note": "", "at": time.time()}
    execution.finish_item(change_id, item["id"], attempt_id, "applied", outcome.detail, delivery=delivery,
                          extra={"requests": _summary(log), "screenshots": screenshots})
    evidence = capture_evidence(record["story_id"], {
        "event": "item_applied_by_agent", "stage": "delivery", "actor": agent, "change_id": change_id,
        "item_id": item["id"], "detail": f"{what} applied in DEV by {agent} (started by {initiator_name}); before: "
                                         f"{before_value!r}; read back: {observed!r} ({source})",
        "item": item, "before": before_value, "observed": observed, "source": source, "attempt_id": attempt_id,
        "requests": _summary(log), "screenshots": screenshots})
    return {"itemId": item["id"], "outcome": "applied", "detail": outcome.detail, "observed": observed,
            "before": before_value, "source": source, "attemptId": attempt_id,
            "evidenceEntryHash": evidence["entry_hash"]}


def _not_sent_errors() -> tuple:
    from . import ais, browser

    return (ais.NotSent, ais.NotAWriteOfThisItem, browser.NotSent)


def _summary(log: list[dict]) -> list[dict]:
    """What each request did, for the record: no token, no password."""
    out = []
    for e in log:
        out.append({k: e.get(k) for k in ("action", "form", "errors", "warnings", "answer_sha256", "at", "step",
                                          "url", "screenshot") if e.get(k) is not None}
                   | ({"formActions": (e.get("request") or {}).get("formRequest", (e.get("request") or {}).get(
                       "actionRequest", {})).get("formActions")} if e.get("request") else {}))
    return out[:50]
