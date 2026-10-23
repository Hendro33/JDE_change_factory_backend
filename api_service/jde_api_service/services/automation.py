"""Routine work runs by itself (redesign step 4).

Routine agent work needs no "start the next stage" clicks:

* Intake -- a new request (typed in, or picked up from Jira) is turned into a
  user story without anyone pressing "Start analysis". Placement in a business
  domain already happens in that run (orchestration_driver.place_in_domain);
  when the agents cannot place it, the Application Manager gets the
  assignment task (lifecycle "assign_domain").
* Reassessment -- solutioning runs again by itself when the answers to its
  open points are in, when the story changed after the design, when the
  customer's setup changed while the design was waiting on it, or after a run
  of its own failed for a passing reason. Duplicate runs are refused (a run
  already in progress is never started twice; an input that was already
  reassessed is not reassessed again), and at most MAX_RUNS automatic runs of
  one kind per story per 24 hours start -- after that a person takes over.
* Re-check before delivery -- an approved, not yet used exact change is read
  against JD Edwards again before delivery (at most every RECHECK_HOURS). The
  Application Manager is told only when something changed.
* Notifications -- decisions, meaningful completions and exceptions, derived
  from the canonical lifecycle (services/notifications.py).

Everything here is decided from the same records every screen reads (the
lifecycle); a "plan" is shown on the story before JADE acts on it, so nobody
is surprised. One switch per customer turns it off (Administration ›
Governance › Routine automation); the bulk "Create user stories" and every
manual button stay as the fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from ..models.lifecycle import AutomationPlan, Lifecycle, NextAction

logger = logging.getLogger("jde_api_service")

SETTINGS_KIND = "automation_settings"
STATE_KIND = "automation_state"
EVENTS_KIND = "automation_events"
RECHECK_KIND = "automation_rechecks"

MAX_RUNS = 3                       # automatic runs of one kind per story per 24 hours
MAX_EVENTS = 300
RUNNING_INTAKE = ("receiving", "improving", "checking")
_SOLUTION_PHASES = ("solutioning", "solution_review")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def settle_seconds() -> int:
    """How long JADE waits after the last answer or setup change before it
    reassesses -- people often answer several points in a row."""
    return _env_int("JDE_AUTOMATION_SETTLE_SECONDS", 60)


def parallel_limit() -> int:
    return _env_int("JDE_AUTOMATION_PARALLEL", 2)


def recheck_hours() -> int:
    return _env_int("JDE_RECHECK_HOURS", 6)


def jira_pickup_minutes() -> int:
    return _env_int("JDE_JIRA_PICKUP_MINUTES", 15)


def loop_seconds() -> int:
    return _env_int("JDE_AUTOMATION_LOOP_SECONDS", 30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _parse(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------
def server_enabled() -> bool:
    """Off for the whole server with JDE_ROUTINE_AUTOMATION=false (the test
    suite sets this; tests of this module switch it on)."""
    return os.environ.get("JDE_ROUTINE_AUTOMATION", "true").strip().lower() not in ("false", "0", "no", "off")


def settings_for(company_id: str) -> dict:
    from jde_mcp_server import docstore

    try:
        doc = docstore.get(SETTINGS_KIND, company_id) or {}
    except Exception:  # noqa: BLE001 -- no store yet
        doc = {}
    return {"enabled": doc.get("enabled", True), "jira_pickup": doc.get("jira_pickup", True),
            "setup_changed_at": doc.get("setup_changed_at"), "setup_changed": doc.get("setup_changed", ""),
            "last_jira_pickup_at": doc.get("last_jira_pickup_at"), "updated_by": doc.get("updated_by"),
            "updated_at": doc.get("updated_at")}


def _save_settings(company_id: str, **changes: Any) -> dict:
    from jde_mcp_server import docstore

    with docstore.transaction():
        doc = docstore.get(SETTINGS_KIND, company_id) or {"company_id": company_id}
        doc.update(changes)
        docstore.put(SETTINGS_KIND, company_id, doc, company_id=company_id)
    return settings_for(company_id)


def update_settings(company_id: str, *, enabled: Optional[bool], jira_pickup: Optional[bool], by: str) -> dict:
    changes: dict[str, Any] = {"updated_by": by, "updated_at": _iso()}
    if enabled is not None:
        changes["enabled"] = bool(enabled)
    if jira_pickup is not None:
        changes["jira_pickup"] = bool(jira_pickup)
    out = _save_settings(company_id, **changes)
    log(company_id, None, "settings", "changed",
        f"Routine automation {'on' if out['enabled'] else 'off'}, Jira pick-up "
        f"{'on' if out['jira_pickup'] else 'off'} (by {by}).")
    return out


def enabled(company_id: Optional[str]) -> bool:
    if not company_id or not server_enabled():
        return False
    return bool(settings_for(company_id)["enabled"])


def setup_changed(company_id: Optional[str], what: str) -> None:
    """Called when a customer's setup changes (governance scope, JDE
    connection, AI connection, agent instructions): designs that were waiting
    on setup are reassessed once it has settled."""
    if not company_id or not server_enabled():
        return
    try:
        _save_settings(company_id, setup_changed_at=_iso(), setup_changed=what[:200])
    except Exception:  # noqa: BLE001 -- automation is best-effort; the change itself stands
        logger.exception("could not record a setup change for routine automation")


# ---------------------------------------------------------------------
# Per-story state and the log
# ---------------------------------------------------------------------
def _state_id(company_id: str, story_id: str) -> str:
    return f"{company_id}:{story_id}"


def state_of(company_id: str, story_id: str) -> dict:
    from jde_mcp_server import docstore

    try:
        return docstore.get(STATE_KIND, _state_id(company_id, story_id)) or {}
    except Exception:  # noqa: BLE001
        return {}


def _update_state(company_id: str, story_id: str, fn: Callable[[dict], Any]) -> Any:
    from jde_mcp_server import docstore

    with docstore.transaction():
        doc = docstore.get(STATE_KIND, _state_id(company_id, story_id)) or {
            "company_id": company_id, "story_id": story_id, "runs": []}
        result = fn(doc)
        docstore.put(STATE_KIND, _state_id(company_id, story_id), doc, company_id=company_id)
    return result


def log(company_id: str, story_id: Optional[str], kind: str, action: str, detail: str) -> None:
    from jde_mcp_server import docstore

    try:
        with docstore.transaction():
            doc = docstore.get(EVENTS_KIND, company_id) or {"company_id": company_id, "events": []}
            events = list(doc.get("events", []))
            events.append({"at": _iso(), "story_id": story_id, "kind": kind, "action": action, "detail": detail[:500]})
            doc["events"] = events[-MAX_EVENTS:]
            docstore.put(EVENTS_KIND, company_id, doc, company_id=company_id)
    except Exception:  # noqa: BLE001 -- the log never stops the work
        logger.exception("could not write the automation log")


def events(company_id: str, limit: int = 100) -> list[dict]:
    from jde_mcp_server import docstore

    try:
        doc = docstore.get(EVENTS_KIND, company_id) or {}
    except Exception:  # noqa: BLE001
        return []
    return list(reversed(doc.get("events", [])))[:limit]


def queue_intake(company_id: str, story_ids: Iterable[str], source: str) -> None:
    """A request created now is turned into a story by itself. Requests that
    existed before (or were created with "analyse" unticked) are left alone."""
    if not server_enabled():
        return
    for sid in story_ids:
        def mark(doc: dict) -> None:
            doc["intake_queued"] = True
            doc["intake_source"] = source
            doc["queued_at"] = _iso()
        try:
            _update_state(company_id, sid, mark)
        except Exception:  # noqa: BLE001
            logger.exception("could not queue %s for automatic intake", sid)


def _runs(state: dict, kind: str, within_hours: int = 24) -> list[dict]:
    since = _now() - timedelta(hours=within_hours)
    return [r for r in state.get("runs", []) if r.get("kind") == kind and (_parse(r.get("at")) or since) >= since]


# ---------------------------------------------------------------------
# Deciding: what JADE will do next by itself, from the lifecycle
# ---------------------------------------------------------------------
_NOT_RETRYABLE = ("api key", "x-api-key", "401", "failed to authenticate", "ai connection", "budget", "disabled",
                  "switched off", "not configured", "no ai", "permission")


def _retryable(error: Optional[str]) -> bool:
    low = (error or "").lower()
    return bool(low) and not any(w in low for w in _NOT_RETRYABLE)


def _sig(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p or "") for p in parts).encode()).hexdigest()[:16]


def _latest_answer_at(change, company_id: str) -> Optional[str]:
    from . import issues

    decided = change.architect_decision.decided_at if change.architect_decision else ""
    times = [r.get("at", "") for r in issues.resolutions(company_id, change.id).values()
             if r.get("answer") and r.get("at", "") > (decided or "")]
    return max(times) if times else None


def _open_blocking(lc: Lifecycle, kinds: Optional[set[str]] = None) -> list:
    return [i for i in lc.issues if i.resolution is None and i.blocks and i.owner not in ("jade", "none")
            and (kinds is None or i.kind in kinds)]


def plan_for(change, lc: Lifecycle, company_id: str, *, state: Optional[dict] = None,
             settings: Optional[dict] = None) -> Optional[AutomationPlan]:
    """What JADE will start by itself on this story, if anything. Pure
    derivation from the lifecycle and the recorded automatic runs."""
    if lc.phase == "done":
        return None
    settings = settings or settings_for(company_id)
    if not settings["enabled"]:
        return None
    state = state if state is not None else state_of(company_id, change.id)
    candidate = _intake_candidate(change, lc, state) or _solution_candidate(change, lc, company_id, state, settings)
    if candidate is None:
        return None
    kind, reason, label, due, signature = candidate
    done = {r.get("signature") for r in state.get("runs", [])}
    if signature in done:
        return None
    if len(_runs(state, kind)) >= MAX_RUNS:
        return AutomationPlan(kind=kind, state="capped", reason=reason, signature=signature,
                              label=f"JADE stopped starting this by itself after {MAX_RUNS} automatic runs in 24 "
                                    "hours. Start it by hand when the cause is fixed.")
    return AutomationPlan(kind=kind, state="scheduled", reason=reason, label=label, signature=signature,
                          due_at=_iso(due))


def _retry_due(state: dict, kind: str) -> Optional[tuple[datetime, str]]:
    """A run JADE started by itself failed: when to try again (backing off),
    and the signature that makes each retry happen once."""
    recent = _runs(state, kind)
    if not recent:
        return None
    last = recent[-1]
    at = _parse(last.get("at")) or _now()
    return at + timedelta(minutes=5 * len(recent)), f"retry:{last.get('at')}"


def _intake_candidate(change, lc: Lifecycle, state: dict):
    if lc.phase != "understand" or change.state not in ("RECEIVED", "REFINING"):
        return None
    if not state.get("intake_queued"):
        return None
    stage = change.processing_stage
    if stage is None:
        return ("intake", "new request", "JADE starts turning this request into a user story by itself.",
                _now(), _sig("intake", "new", state.get("queued_at")))
    if stage == "failed" and _retryable(change.processing_error):
        due = _retry_due(state, "intake")
        if due is None:
            return None
        return ("intake", "retry after a failed run",
                "JADE's analysis stopped before it finished; JADE tries again by itself shortly.", due[0],
                _sig("intake", due[1]))
    return None


def _solution_candidate(change, lc: Lifecycle, company_id: str, state: dict, settings: dict):
    if lc.phase not in _SOLUTION_PHASES or change.architecture_review_stage == "analyzing":
        return None
    decided = change.architect_decision.decided_at if change.architect_decision else None
    if change.architecture_review_stage == "failed":
        if not _retryable(change.architecture_review_error):
            return None
        due = _retry_due(state, "solutioning")
        if due is None:
            return None
        return ("solutioning", "retry after a failed run",
                "The solution analysis stopped before it finished; JADE runs it again by itself shortly.", due[0],
                _sig("solutioning", due[1]))
    if decided is None:
        return None
    try:
        from ..discovery import baseline as _baseline

        b = _baseline.current_for_story(company_id, change.id)
    except Exception:  # noqa: BLE001
        b = None
    if b is not None and b.get("status") not in (None, "current"):
        return ("solutioning", "the story changed after the design",
                "The story changed after the solution was designed; JADE reassesses the solution by itself.",
                _now(), _sig("solutioning", "story", b.get("baseline_id"), b.get("status")))
    approved = change.change_approval is not None and change.change_approval.status == "approved"
    if approved:
        return None    # past authorisation: the re-check before delivery takes over
    answered = _latest_answer_at(change, company_id)
    if answered and not _open_blocking(lc):
        due = (_parse(answered) or _now()) + timedelta(seconds=settle_seconds())
        return ("solutioning", "answers recorded",
                "The open points are answered; JADE reassesses the solution with the answers by itself.", due,
                _sig("solutioning", "answers", decided, answered))
    changed_at = settings.get("setup_changed_at")
    if changed_at and changed_at > decided and _open_blocking(lc, {"setup"}):
        due = (_parse(changed_at) or _now()) + timedelta(seconds=settle_seconds())
        return ("solutioning", "setup changed",
                f"The setup changed ({settings.get('setup_changed') or 'administration'}); JADE reassesses the "
                "solution by itself.", due, _sig("solutioning", "setup", decided, changed_at))
    return None


def apply(change, lc: Lifecycle, company_id: Optional[str] = None) -> Lifecycle:
    """Show routine work JADE will start by itself as JADE's, not a person's
    task; and a person's again (with the reason) once it stopped retrying."""
    company_id = company_id or change.customer_id
    if not server_enabled() or not company_id:
        return lc
    state = state_of(company_id, change.id)
    plan = plan_for(change, lc, company_id, state=state)
    lc.automation = plan
    if plan is not None and plan.state == "scheduled":
        lc.next_action = NextAction(kind="none", summary=plan.label, owner="jade",  # type: ignore[arg-type]
                                    tab="solution" if plan.kind == "solutioning" else "overview")
        lc.health = "in_progress"
        lc.health_label = "JADE will start shortly"
        if lc.what_next and lc.what_next[0].primary:
            lc.what_next[0] = lc.what_next[0].model_copy(update={
                "owner": "jade", "who": "JADE", "text": plan.label, "action": "", "blocking": False})
    elif plan is not None and plan.state == "capped":
        if plan.label not in lc.open_items:
            lc.open_items = [plan.label] + lc.open_items
    recheck = (state.get("recheck") or {})
    if recheck.get("status") == "changed" and lc.phase in ("delivery", "solution_review", "solutioning"):
        note = ("Re-checked before delivery: JD Edwards changed since this change was approved -- "
                + "; ".join(recheck.get("problems", [])[:2]))
        if note not in lc.open_items:
            lc.open_items = [note] + lc.open_items
    return lc


# ---------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------
def _launch(fn: Callable, kwargs: dict, *, background_tasks=None, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start a run without waiting for it: as the request's background task
    when there is one, on the server's event loop when called from the
    automation loop, or on a thread of its own otherwise."""
    if background_tasks is not None:
        background_tasks.add_task(fn, **kwargs)
        return
    is_async = inspect.iscoroutinefunction(fn)
    if loop is not None and loop.is_running():
        if is_async:
            asyncio.run_coroutine_threadsafe(fn(**kwargs), loop)
        else:
            loop.call_soon_threadsafe(lambda: loop.run_in_executor(None, lambda: fn(**kwargs)))
        return

    def run() -> None:
        try:
            if is_async:
                asyncio.run(fn(**kwargs))
            else:
                fn(**kwargs)
        except Exception:  # noqa: BLE001 -- run functions record their own failures
            logger.exception("automatic run failed to start")

    threading.Thread(target=run, daemon=True, name="jade-automation-run").start()


def _record_run(company_id: str, story_id: str, plan: AutomationPlan) -> bool:
    """Claim the run: False when the same input was already run or the cap is
    reached (duplicate-run protection that also holds between workers)."""
    def claim(doc: dict) -> bool:
        if plan.signature in {r.get("signature") for r in doc.get("runs", [])}:
            return False
        if len(_runs(doc, plan.kind)) >= MAX_RUNS:
            return False
        doc["runs"] = (list(doc.get("runs", [])) + [{"kind": plan.kind, "at": _iso(), "reason": plan.reason,
                                                       "signature": plan.signature}])[-30:]
        if plan.kind == "intake":
            doc["intake_started_at"] = _iso()
        return True
    return bool(_update_state(company_id, story_id, claim))


def start_intake(company_id: str, story_id: str, plan: AutomationPlan, *, background_tasks=None, loop=None) -> bool:
    from ..config import settings as app_settings
    from . import agent_settings
    from .orchestration_driver import run_enhancement
    from .registry import get_change_request_service, get_customer_link_service, get_enhancement_run_service

    cr = get_change_request_service().get(story_id)
    if cr is None or cr.customer_id != company_id or getattr(cr, "status", "") == "withdrawn":
        return False
    run_service = get_enhancement_run_service()
    existing = run_service.get(story_id)
    if existing is not None and existing.stage in RUNNING_INTAKE:
        return False
    try:
        agent_settings.require_enabled(company_id, "receive-agent", "improve-agent", "check-agent")
    except agent_settings.AgentDisabled as exc:
        _skip_once(company_id, story_id, plan, f"not started: {exc}")
        return False
    if not _record_run(company_id, story_id, plan):
        return False
    # Marked as running before answering, like the manual button: every screen
    # shows JADE at work at once and a second start is refused.
    run_service.start(story_id)
    source = ", ".join(p for p in (cr.business_source, cr.source_reference) if p)
    _launch(run_enhancement, dict(request_id=story_id, story_id=story_id, source=source, raw_content=cr.raw_content,
                                  repo_root=app_settings.repo_root, run_service=run_service,
                                  link_service=get_customer_link_service(), customer_id=company_id),
            background_tasks=background_tasks, loop=loop)
    log(company_id, story_id, "intake", "started", f"Turning the request into a user story ({plan.reason}).")
    return True


def start_solutioning(company_id: str, story_id: str, plan: AutomationPlan, *, background_tasks=None, loop=None) -> bool:
    from ..config import settings as app_settings
    from . import agent_settings
    from .architecture_driver import run_architecture_review
    from .registry import get_architecture_review_service

    run_service = get_architecture_review_service()
    existing = run_service.get(story_id)
    if existing is not None and existing.stage == "analyzing":
        return False
    try:
        agent_settings.require_enabled(company_id, "architect")
    except agent_settings.AgentDisabled as exc:
        _skip_once(company_id, story_id, plan, f"not started: {exc}")
        return False
    if not _record_run(company_id, story_id, plan):
        return False
    run_service.start(story_id)
    _launch(run_architecture_review, dict(story_id=story_id, repo_root=app_settings.repo_root,
                                          run_service=run_service, customer_id=company_id, initiated_by=None),
            background_tasks=background_tasks, loop=loop)
    log(company_id, story_id, "solutioning", "started", f"Reassessing the solution ({plan.reason}).")
    return True


def _skip_once(company_id: str, story_id: str, plan: AutomationPlan, detail: str) -> None:
    key = f"skip:{plan.signature}"

    def mark(doc: dict) -> bool:
        if key in doc.get("skipped", []):
            return False
        doc["skipped"] = (list(doc.get("skipped", [])) + [key])[-30:]
        return True
    if _update_state(company_id, story_id, mark):
        log(company_id, story_id, plan.kind, "skipped", detail)


def nudge(company_id: str, story_id: str, *, background_tasks=None) -> bool:
    """Act on one story now (e.g. right after it was created), if something
    is due. The automation loop picks up everything else."""
    if not enabled(company_id):
        return False
    from .registry import get_change_service

    change = get_change_service().get_for_customer(story_id, company_id)
    if change is None or change.lifecycle is None:
        return False
    plan = change.lifecycle.automation
    if plan is None or plan.state != "scheduled" or (_parse(plan.due_at) or _now()) > _now():
        return False
    starter = start_intake if plan.kind == "intake" else start_solutioning
    return starter(company_id, story_id, plan, background_tasks=background_tasks)


# ---------------------------------------------------------------------
# The sweep: one pass over a customer's stories
# ---------------------------------------------------------------------
def sweep(company_id: str, *, loop: Optional[asyncio.AbstractEventLoop] = None, background_tasks=None,
          recheck: bool = True) -> dict:
    """Start what is due, notify what changed, re-check approved changes.
    Returns a short account (for tests and the log)."""
    out: dict[str, list] = {"started": [], "capped": [], "notified": [], "rechecked": []}
    if not server_enabled():
        return out
    from . import notifications
    from .registry import get_change_service

    settings = settings_for(company_id)
    changes = [c for c in get_change_service().list_for_customer(company_id) if c.lifecycle is not None]
    running = {"intake": sum(1 for c in changes if c.processing_stage in RUNNING_INTAKE),
               "solutioning": sum(1 for c in changes if c.architecture_review_stage == "analyzing")}
    now = _now()
    for change in changes:
        lc = change.lifecycle
        try:
            if notifications.observe(company_id, change, lc):
                out["notified"].append(change.id)
        except Exception:  # noqa: BLE001
            logger.exception("notification check failed for %s", change.id)
        if not settings["enabled"]:
            continue
        plan = lc.automation
        if plan is None:
            continue
        if plan.state == "capped":
            _notify_capped(company_id, change, plan)
            out["capped"].append(change.id)
            continue
        if (_parse(plan.due_at) or now) > now or running[plan.kind] >= parallel_limit():
            continue
        starter = start_intake if plan.kind == "intake" else start_solutioning
        try:
            if starter(company_id, change.id, plan, loop=loop, background_tasks=background_tasks):
                running[plan.kind] += 1
                out["started"].append(change.id)
        except Exception:  # noqa: BLE001
            logger.exception("could not start automatic %s for %s", plan.kind, change.id)
    if recheck and settings["enabled"]:
        out["rechecked"] = recheck_before_delivery(company_id, changes)
    return out


def _notify_capped(company_id: str, change, plan: AutomationPlan) -> None:
    from . import notifications

    key = f"capped:{change.id}:{plan.signature}"
    if notifications.add(company_id, kind="exception", audience=["product_manager"], story_id=change.id,
                         title=change.title, text=plan.label, tab="overview", key=key):
        log(company_id, change.id, plan.kind, "capped", plan.label)


# ---------------------------------------------------------------------
# Re-check before delivery
# ---------------------------------------------------------------------
_UNREADABLE = ("cannot be read", "was not established", "has no recorded approval basis")


def recheck_before_delivery(company_id: str, changes: Optional[list] = None, *, force: bool = False) -> list[str]:
    """Read every approved, not yet used exact change of this customer
    against JD Edwards again (at most every recheck_hours). Only a change
    that is now different is reported -- and only once per difference."""
    from jde_mcp_server import approval, binding

    from . import notifications

    if changes is None:
        from .registry import get_change_service

        changes = [c for c in get_change_service().list_for_customer(company_id) if c.lifecycle is not None]
    open_stories = {c.id: c for c in changes if c.lifecycle and c.lifecycle.phase != "done"}
    checked: list[str] = []
    for record in approval.all_records():
        if record.get("company_id") != company_id or record.get("status") != "approved":
            continue
        story = open_stories.get(record.get("story_id"))
        if story is None:
            continue
        try:
            if not approval._never_used(record):  # noqa: SLF001 -- same rule the authorisation uses
                continue
        except Exception:  # noqa: BLE001
            continue
        state = state_of(company_id, story.id)
        last = (state.get("recheck") or {})
        last_at = _parse(last.get("checked_at")) if last.get("change_id") == record["change_id"] else None
        if not force and last_at is not None and _now() - last_at < timedelta(hours=recheck_hours()):
            continue
        try:
            found = binding.problems(record, read_current=True)
        except Exception as exc:  # noqa: BLE001 -- a failed read is "could not check", never "changed"
            found = [f"the target's current state cannot be read: {exc}"]
        changed = [p for p in found if not any(u in p for u in _UNREADABLE)]
        status = "changed" if changed else ("unreadable" if found else "ok")
        result = {"change_id": record["change_id"], "checked_at": _iso(), "status": status,
                  "problems": changed or found}

        def save(doc: dict) -> Optional[dict]:
            before = doc.get("recheck") or {}
            doc["recheck"] = result
            return before
        before = _update_state(company_id, story.id, save) or {}
        checked.append(story.id)
        was = before.get("status") if before.get("change_id") == record["change_id"] else None
        if status == "changed" and (was != "changed" or before.get("problems") != result["problems"]):
            text = ("JD Edwards changed since this change was approved, so it will not be delivered as approved: "
                    + "; ".join(changed[:2]) + ". Review it and run the Architect again or approve it again.")
            notifications.add(company_id, kind="exception", audience=["product_manager"], story_id=story.id,
                              title=story.title, text=text, tab="delivery",
                              key=f"recheck:{record['change_id']}:{_sig(*changed)}")
            log(company_id, story.id, "recheck", "changed", text)
        elif status == "unreadable" and was not in ("unreadable",):
            log(company_id, story.id, "recheck", "unreadable",
                "Could not read the target in JD Edwards for the re-check: " + "; ".join(found[:1]))
        elif status == "ok" and was not in ("ok",):
            log(company_id, story.id, "recheck", "unchanged", "Re-checked before delivery: unchanged since approval.")
    return checked


# ---------------------------------------------------------------------
# Jira pick-up and the loop
# ---------------------------------------------------------------------
def pick_up_jira(company_id: str) -> list[str]:
    """New tickets in the pick-up status become requests (then stories) by
    themselves, every jira_pickup_minutes, when Jira is set up."""
    settings = settings_for(company_id)
    if not settings["enabled"] or not settings["jira_pickup"]:
        return []
    last = _parse(settings.get("last_jira_pickup_at"))
    if last is not None and _now() - last < timedelta(minutes=jira_pickup_minutes()):
        return []
    from .registry import get_jira_integration_service, get_jira_sync_service

    try:
        config = get_jira_integration_service().get_for_customer(company_id)
    except Exception:  # noqa: BLE001
        config = None
    if config is None or not config.is_configured():
        return []
    _save_settings(company_id, last_jira_pickup_at=_iso())
    try:
        result = get_jira_sync_service(company_id).sync_for_customer(company_id)
    except Exception as exc:  # noqa: BLE001 -- shown in the log; the next pick-up tries again
        log(company_id, None, "jira", "failed", f"Picking up new Jira tickets failed: {exc}")
        return []
    if result.imported:
        log(company_id, None, "jira", "imported", f"Picked up {len(result.imported)} new Jira ticket(s): "
            + ", ".join(result.imported[:10]))
    return list(result.imported)


def _companies() -> list[str]:
    from .customer_service import get_registry

    return [c.id for c in get_registry().list_companies()]


def run_once(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    from . import notifications

    for company_id in _companies():
        try:
            pick_up_jira(company_id)
        except Exception:  # noqa: BLE001
            logger.exception("automatic Jira pick-up failed for %s", company_id)
        try:
            sweep(company_id, loop=loop)
        except Exception:  # noqa: BLE001
            logger.exception("routine automation sweep failed for %s", company_id)
        try:
            notifications.send_digests(company_id)
        except Exception:  # noqa: BLE001
            logger.exception("daily digest failed for %s", company_id)


async def run_forever() -> None:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(run_once, loop)
        except Exception:  # noqa: BLE001
            logger.exception("routine automation pass failed")
        await asyncio.sleep(loop_seconds())
