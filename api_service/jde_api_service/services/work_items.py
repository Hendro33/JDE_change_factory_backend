"""
Work items and the outcome card (usability redesign, step 2).

Mixed delivery shows the real division of the work (review, change 7): which
items an agent applies, which a person applies or completes outside Jade,
the technical package's milestones, the verification, the business
acceptance and the release hand-over -- each with an owner, its dependencies
and its progress. At the end, the outcome card says what changed, what was
verified and what the business does now, with the three outcomes kept apart.

Derived only from the authoritative records (approval.py's change records,
the technical package, the hand-offs, the acceptance and as-built records).
Presentational: nothing here approves or executes anything.
"""

from __future__ import annotations

from typing import Any, Optional

from ..models.delivery_view import OutcomeCard, OutcomeStep, WorkItem
from ..models.lifecycle import Lifecycle

TECHNICAL_ROUTES = {"Technical Agent", "Mixed"}
HUMAN_ROUTE = "Human Implementation"


def _iso(t: Any) -> str:
    if isinstance(t, (int, float)):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()
    return str(t or "")


def _item_state(it) -> str:
    if it.applied:
        return "done"
    if it.delivery_state in ("unknown", "diverged"):
        return "failed"
    if it.delivery_state in ("agent_unavailable", "agent_could_not_apply"):
        return "blocked"
    if it.delivery_state == "in_progress":
        return "in_progress"
    return "todo"


def _value(it) -> str:
    if it.value is not None:
        return str(it.value)
    if it.values:
        return ", ".join(f"{k} = {v}" for k, v in it.values.items())
    return it.specification or ""


def _configuration(change) -> list[WorkItem]:
    ec = change.exact_change
    if ec is None:
        return []
    out: list[WorkItem] = []
    approved = bool(change.change_approval and change.change_approval.status == "approved")
    prev: Optional[str] = None
    if ec.items:
        for it in ec.items:
            agent = it.executor == "agent"
            who = ("JADE Functional Agent (" + ("web client" if it.route == "browser" else "AIS") + ")") if agent \
                else "Application Manager, in JDE DEV"
            state = _item_state(it) if approved else "todo"
            detail = it.delivery_detail or f"{it.target}: {_value(it)}".strip(": ")
            out.append(WorkItem(id=it.id, title=it.label or it.target, owner_kind="agent" if agent else "person",
                                who=who, state=state, detail=detail, depends_on=[prev] if prev else [],
                                action=None if agent else "record_applied", group="configuration"))
            prev = it.id
    else:
        ex = ec.execution
        state = "done" if ex.write_state == "applied" else "failed" if ex.write_state in ("unknown", "diverged") \
            else "todo"
        out.append(WorkItem(id="C1", title=f"{ec.application} version {ec.version}, processing option {ec.option}",
                            owner_kind="person", who="Application Manager, in JDE DEV", state=state,
                            detail=f"{ec.current_value or 'current value'} → {ec.proposed_value}",
                            action="record_applied", group="configuration"))
    ex = ec.execution
    ver = ex.verification or {}
    t_state = ("failed" if ver.get("passed") is False else "done") if ex.test_state == "completed" else \
        "failed" if ex.test_state in ("unknown", "diverged") else "todo"
    out.append(WorkItem(
        id="TEST", title="Test in DEV" + (f" ({ec.test_orchestration})" if ec.test_orchestration else ""),
        owner_kind="agent" if ec.test_orchestration else "person",
        who="JADE, approved test orchestration" if ec.test_orchestration else "Application Manager",
        state=t_state, detail=("Passed" if ver.get("passed") else "Failed" if ver.get("passed") is False else "")
        + (f" — {ver.get('source')}" if ver.get("source") else ""),
        depends_on=[w.id for w in out], action="run_or_record_test", group="verification"))
    return out


def _technical(tech: dict) -> list[WorkItem]:
    pkg, rec, st = tech.get("package"), tech.get("record") or {}, tech.get("states") or {}
    approved = rec.get("status") == "approved"
    ver = rec.get("verification") or {}
    results = ver.get("results") or []

    def s(done: bool, failed: bool = False, current: bool = False) -> str:
        return "failed" if failed else "done" if done else "in_progress" if current else "todo"

    return [
        WorkItem(id="PKG", title="Prepare the implementation package", owner_kind="agent", who="JADE Technical Agent",
                 state=s(bool(pkg), current=bool(tech.get("running"))),
                 detail=f"package revision {pkg['revision']}" if pkg else "", group="development"),
        WorkItem(id="PKG-APPROVE", title="Approve the exact package", owner_kind="person", who="Application Manager",
                 state=s(approved, failed=rec.get("status") == "rejected"), depends_on=["PKG"],
                 action="approve_package", group="development"),
        WorkItem(id="CHECKIN", title="Check in through OMW", owner_kind="person", who="Developer",
                 state=s(st.get("apply") == "applied", failed=st.get("apply") in ("unknown", "diverged")),
                 depends_on=["PKG-APPROVE"], action="record_technical_apply", group="development"),
        WorkItem(id="BUILD", title="Build the objects", owner_kind="person", who="Developer",
                 state=s(st.get("build") == "built", failed=st.get("build") in ("failed", "unknown", "diverged")),
                 depends_on=["CHECKIN"], action="record_technical_build", group="development"),
        WorkItem(id="CNC", title="Activate in DEV", owner_kind="cnc", who="CNC",
                 state=s(bool(rec.get("cnc_activation"))), depends_on=["BUILD"], action="record_cnc",
                 group="development"),
        WorkItem(id="VERIFY", title="Run the test plan in DEV", owner_kind="person", who="Application Manager",
                 state=s(bool(ver.get("passed")) and bool(ver.get("runtime_is_approved_artifact")),
                         failed=bool(ver) and not (ver.get("passed") and ver.get("runtime_is_approved_artifact"))),
                 detail=f"{sum(1 for r in results if r.get('passed'))} of {len(results)} tests passed" if results else "",
                 depends_on=["CNC"], action="record_technical_verify", group="verification"),
    ]


def _handoffs(change, hs: dict) -> list[WorkItem]:
    authorised = hs["authorisation"] is not None
    out = []
    for i in hs["items"]:
        c = i.get("completion")
        out.append(WorkItem(
            id=i["id"], title=i["text"], owner_kind="implementer",
            who=i["implementer"] or (c or {}).get("implementer") or "Implementer (to be named)",
            state="done" if c else "todo" if authorised else "blocked",
            detail=(f"Recorded by {c['by']}" if c else "" if authorised else "Waiting for authorisation"),
            action=None if c else "record_handoff", group="handoff",
            completed_by=(c or {}).get("implementer", ""), completed_at=(c or {}).get("at", ""),
            evidence_reference=(c or {}).get("evidence_reference", "")))
    return out


def _tech_facts(change, lc: Lifecycle) -> dict:
    if lc.route not in TECHNICAL_ROUTES:
        return {}
    from .lifecycle import _technical_facts

    return _technical_facts(change.customer_id, change.id)


def _final(change) -> Optional[dict]:
    from .lifecycle import _asbuilt_final

    return _asbuilt_final(change.customer_id, change.id)


def items(change, lc: Lifecycle, tech: dict, hs: dict, accepted: Optional[dict], final: Optional[dict]) -> list[WorkItem]:
    route = lc.route
    if lc.phase in ("understand", "story_review", "solutioning") or route in (
            None, "Clarification Required", "Resolve without Change"):
        return []
    out: list[WorkItem] = []
    if route in TECHNICAL_ROUTES:
        out += _technical(tech)
    elif route != HUMAN_ROUTE:
        out += _configuration(change)
    out += _handoffs(change, hs)
    if route == HUMAN_ROUTE:
        v = hs.get("verification")
        out.append(WorkItem(id="H-VERIFY", title="Verify the result against the acceptance criteria",
                            owner_kind="person", who="Application Manager",
                            state="done" if v and v["passed"] else "failed" if v else "todo",
                            detail=(v or {}).get("note", ""), depends_on=[i["id"] for i in hs["items"]],
                            action="record_handoff_verification", group="verification"))
    if not out:
        return []
    verify_ids = [w.id for w in out if w.group == "verification"] + [w.id for w in out if w.group == "handoff"]
    out.append(WorkItem(id="ACCEPT", title="Accept the outcome", owner_kind="business", who="Domain Owner",
                        state=("done" if accepted["decision"] == "accepted" else "failed") if accepted else "todo",
                        detail=(accepted or {}).get("note", ""), depends_on=verify_ids, action="accept_outcome",
                        group="acceptance"))
    out.append(WorkItem(id="RELEASE", title="Hand over for release (as-built record, CNC)", owner_kind="release",
                        who="Application Manager", state="done" if final else "todo", depends_on=["ACCEPT"],
                        action="finalise_asbuilt", group="release",
                        detail="Promotion beyond DEV is done by CNC outside JADE"))
    return out


def _no_change_items(change, hs: dict, accepted: Optional[dict]) -> list[WorkItem]:
    out = _handoffs(change, hs)
    if hs["items"]:
        v = hs.get("verification")
        out.append(WorkItem(id="H-VERIFY", title="Check the result against the acceptance criteria",
                            owner_kind="person", who="Application Manager",
                            state="done" if v and v["passed"] else "failed" if v else "todo",
                            detail=(v or {}).get("note", ""), depends_on=[i["id"] for i in hs["items"]],
                            action="record_handoff_verification", group="verification"))
    out.append(WorkItem(id="ACCEPT", title="Accept the outcome", owner_kind="business", who="Domain Owner",
                        state=("done" if accepted["decision"] == "accepted" else "failed") if accepted else "todo",
                        detail=(accepted or {}).get("note", ""), depends_on=[w.id for w in out],
                        action="accept_outcome", group="acceptance"))
    return out


def _jira(change) -> tuple[str, str]:
    import re

    key = (change.source_reference or "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", key):
        return "", ""
    try:
        from .registry import get_jira_integration_service

        cfg = get_jira_integration_service().get_for_customer(change.customer_id)
        base = (cfg.base_url if cfg else "").rstrip("/")
    except Exception:  # noqa: BLE001
        base = ""
    return key, f"{base}/browse/{key}" if base and key else ""


def outcome(change, lc: Lifecycle, tech: dict, hs: dict, accepted: Optional[dict], final: Optional[dict],
            work: list[WorkItem]) -> Optional[OutcomeCard]:
    if lc.phase not in ("validation", "release", "done") or lc.outcome == "rejected":
        return None
    route = lc.route
    changed: list[str] = []
    verified: list[str] = []
    dev = OutcomeStep()
    if route in TECHNICAL_ROUTES:
        rec = tech.get("record") or {}
        ver = rec.get("verification") or {}
        pkg = tech.get("package") or {}
        expl = ((pkg.get("content") or {}).get("explanation") or "").strip()
        if expl:
            changed.append(expl.split(". ")[0].rstrip(".") + ".")
        res = ver.get("results") or []
        if res:
            verified.append(f"{sum(1 for r in res if r.get('passed'))} of {len(res)} tests passed in DEV")
        if ver.get("passed") and ver.get("runtime_is_approved_artifact"):
            dev = OutcomeStep(done=True, by=str(ver.get("by") or ""), at=_iso(ver.get("at")))
    elif route == HUMAN_ROUTE:
        changed += [f"{w.title} (by {w.completed_by or w.who})" for w in work if w.group == "handoff" and w.state == "done"]
        v = hs.get("verification")
        if v:
            verified.append(v["note"])
            if v["passed"]:
                dev = OutcomeStep(done=True, by=v["by"], at=v["at"])
    else:
        result = change.architect_decision.result if change.architect_decision else None
        done_config = [w for w in work if w.group == "configuration" and w.state == "done"]
        if done_config and result and result.changes:
            # JADE's own wording of the change, before -> after.
            changed += [f"{c.what}: {c.current or 'previous value'} → {c.proposed}" for c in result.changes if c.proposed]
        else:
            changed += [w.title for w in done_config]
        ec = change.exact_change
        ver = (ec.execution.verification if ec else None) or {}
        if ver:
            verified.append(("Test passed" if ver.get("passed") else "Test failed")
                            + (f" ({ver.get('source')})" if ver.get("source") else ""))
            if ver.get("passed") is True and ec.execution.test_state == "completed":
                dev = OutcomeStep(done=True, by=str(ver.get("by") or ""), at=_iso(ver.get("at")))
    changed += [f"{w.title} (by {w.completed_by or w.who})" for w in work
                if w.group == "handoff" and w.state == "done" and route != HUMAN_ROUTE]
    acc = OutcomeStep()
    not_acc = None
    if accepted:
        who = accepted.get("on_behalf_of") or accepted["by"]
        step = OutcomeStep(done=accepted["decision"] == "accepted", by=who, at=accepted["at"], detail=accepted["note"])
        if accepted["decision"] == "accepted":
            acc = step
        else:
            not_acc = step
    rel = OutcomeStep(done=True, by=final.get("generated_by", ""), at=final.get("generated_at", ""),
                      detail="As-built record final; promotion beyond DEV is done by CNC outside JADE") if final \
        else OutcomeStep()
    finished = lc.phase == "done"
    missing: list[str] = []
    if finished:
        # A story finished under an earlier workflow (audit F07): the facts it
        # has stay as recorded; a checkpoint it never recorded is stated, not
        # asked for and never manufactured.
        earlier = "not recorded: the story was finished under an earlier workflow"
        for label, step in (("Verified in DEV", dev), ("Accepted by the business", acc), ("Handed over for release", rel)):
            if not step.done:
                step.detail = earlier
                missing.append(f"{label}: {earlier}.")
    if finished and missing:
        todo = ("Finished. Nothing is asked of you. Some checkpoints were not recorded when it was finished; the "
                "Application Manager can reconcile it deliberately.")
    elif not dev.done:
        todo = "The change is being verified in DEV. Nothing is needed from the business yet."
    elif not_acc is not None:
        todo = "The business did not accept the outcome. The Application Manager is correcting it."
    elif not acc.done:
        todo = "Check that it works for you, then accept the outcome (or say what is wrong)."
    elif not rel.done:
        todo = "Accepted. Application Management hands it over for release; nothing more is needed from you."
    else:
        todo = "Done. The change is handed over for release to production by CNC."
    result = change.architect_decision.result if change.architect_decision else None
    # The business result, not the proposed technical work (audit HW-005/F08).
    headline = (result.business_summary if result and result.business_summary else
                change.user_story.statement if change.user_story else change.title)
    key, url = _jira(change)
    crit_state = ("verified in DEV with the change as a whole" + (f" ({dev.by})" if dev.by else "")) if dev.done else "not verified yet"
    criteria = [{"text": a.text, "state": crit_state, "verified": dev.done}
                for a in (change.user_story.acceptance_criteria if change.user_story else [])]
    from . import closure

    try:
        wb = closure.writebacks(change.customer_id, change.id)
        conf = closure.confirmation(change.customer_id, change.id)
        linked = closure.links(change.customer_id, change.id)
    except Exception:  # noqa: BLE001
        wb, conf, linked = {"posted": {}, "failed": {}}, None, []
    return OutcomeCard(headline=headline, what_changed=changed, verified=verified, dev_verified=dev, criteria=criteria,
                       requester_confirmation=conf, linked_requests=linked, jira_posted=len(wb["posted"]),
                       jira_failed=[f"{k}: {v['error']}" for k, v in wb["failed"].items()],
                       business_accepted=acc, released=rel, not_accepted=not_acc, what_to_do_now=todo,
                       jira_key=key, jira_url=url)


def apply(change, lc: Lifecycle) -> Lifecycle:
    """Attach work items and the outcome card; add open people's work to
    "What needs to happen"."""
    from ..delivery import handoff
    from . import acceptance

    if lc.phase in ("understand", "story_review") or (change.architect_decision is None and change.exact_change is None):
        return lc
    # No JD Edwards change: nothing is built or released, but the work people
    # still do for the requirement (if any) and the business acceptance are
    # work items (audit 23 Oct, F00).
    if lc.outcome == "resolved_without_change" or lc.route == "Resolve without Change":
        hs = handoff.state(change)
        accepted = acceptance.current(change, {})
        lc.work_items = _no_change_items(change, hs, accepted) if hs["authorisation"] or hs["items"] else []
        lc.handoff_authorised = hs["authorisation"] is not None
        return lc
    tech = _tech_facts(change, lc)
    hs = handoff.state(change)
    accepted = acceptance.current(change, tech)
    final = _final(change)
    lc.work_items = items(change, lc, tech, hs, accepted, final)
    lc.outcome_card = outcome(change, lc, tech, hs, accepted, final, lc.work_items)
    if lc.phase == "done" and lc.outcome == "delivered" and lc.outcome_card is not None:
        card = lc.outcome_card
        lc.completion_gaps = [f"{label}: {step.detail}." for label, step in
                              (("Verified in DEV", card.dev_verified), ("Accepted by the business", card.business_accepted),
                               ("Handed over for release", card.released)) if not step.done and step.detail]
    lc.handoff_authorised = hs["authorisation"] is not None
    return lc
