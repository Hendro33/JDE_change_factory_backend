"""The role homes and the one figure strip (register C1, C3, K7, K15, M6, N1).

Every section and every figure comes from the canonical lifecycle of the
customer's stories, computed once here. My Work, the Application Manager home
and Insights read the same figures, so the same label never shows two numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Iterable, Optional

from ..models.work import DomainLine, Figure, MyWork, QuestionItem, StuckItem
from .lifecycle import is_mine

STUCK_DAYS = 5
_QUESTION_ACTIONS = {"answer_issue", "clarify", "decide_issue"}
_QUESTION_KINDS = {"business", "decision"}


def _days(iso: Optional[str], now: datetime) -> Optional[int]:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, (now - t).days)


def _blocked(c) -> bool:
    lc = c.lifecycle
    return lc.phase != "done" and (lc.health in ("blocked", "failed") or bool(lc.authorisation and lc.authorisation.renewable))


def figures(changes: list, *, attention: Optional[int] = None, oldest_mine: Optional[int] = None) -> list[Figure]:
    """The figure strip. Portfolio figures are the same for everyone; the two
    personal ones (things need you, oldest waiting) are added when given."""
    now = datetime.now(timezone.utc)
    open_ = [c for c in changes if c.lifecycle.phase != "done"]
    stories = [c for c in open_ if c.lifecycle.phase != "understand"]
    delivered = [c for c in changes if c.lifecycle.outcome == "delivered" and (_days(c.lifecycle.since, now) or 0) <= 31
                 and c.lifecycle.phase == "done"]
    leads = []
    for c in changes:
        if c.lifecycle.outcome == "delivered" and c.lifecycle.phase == "done" and (_days(c.lifecycle.since, now) or 0) <= 90:
            a, b = _days(c.created_at, now), _days(c.lifecycle.since, now)
            if a is not None and b is not None and a >= b:
                leads.append(a - b)
    out: list[Figure] = []
    if attention is not None:
        out.append(Figure(key="needs_you", label="Need you", value=attention, display=str(attention),
                          definition="Decisions, tasks and questions whose next step belongs to one of your roles."))
        out.append(Figure(key="oldest_mine", label="Oldest waiting on you",
                          value=oldest_mine, display="—" if oldest_mine is None else f"{oldest_mine} d",
                          definition="Days since your longest-waiting item's current step began."))
    blocked = sum(1 for c in open_ if _blocked(c))
    out += [
        Figure(key="blocked", label="Blocked stories", value=blocked, display=str(blocked), link="/stories?health=attention",
               definition="Open stories (all owners) that are blocked, need attention, or whose authorisation expired."),
        Figure(key="in_progress", label="Stories in progress", value=len(stories), display=str(len(stories)),
               link="/stories", definition="Open user stories. Requests that are not a story yet are counted apart."),
        Figure(key="requests", label="Incoming requests", value=len(open_) - len(stories),
               display=str(len(open_) - len(stories)), link="/stories?phase=understand",
               definition="Requests JADE has not turned into a user story yet."),
        Figure(key="delivered", label="Delivered, 31 days", value=len(delivered), display=str(len(delivered)),
               link="/stories?phase=done", definition="Stories delivered with a recorded finish in the last 31 days."),
        Figure(key="lead_time", label="Median lead time", value=median(leads) if leads else None,
               display=f"{median(leads):.0f} d" if leads else "—",
               definition=f"From story proposed to delivered, for stories delivered in the last 90 days ({len(leads)})."),
    ]
    return out


def _domain_ok(c, roles: set[str], my_domains: frozenset[str]) -> bool:
    """A Domain Owner acts only in their own domains."""
    return c.business_domain_id in my_domains


def _mine(c, roles: set[str], my_domains: frozenset[str]) -> bool:
    lc = c.lifecycle
    if not is_mine(lc, roles):
        return False
    if lc.next_action.owner == "domain_owner":
        return _domain_ok(c, roles, my_domains)
    return True


def _owns_issue(i, c, roles: set[str], my_domains: frozenset[str]) -> bool:
    if i.owner == "domain_owner":
        return "domain_owner" in roles and _domain_ok(c, roles, my_domains)
    if i.owner == "admin":
        return "admin" in roles
    return i.owner in roles


def build(changes: list, roles: Iterable[str], my_domains: frozenset[str], domain_names: dict[str, str]) -> MyWork:
    roles = set(roles)
    now = datetime.now(timezone.utc)
    open_ = [c for c in changes if c.lifecycle.phase != "done"]

    questions: list[QuestionItem] = []
    for c in open_:
        for i in c.lifecycle.issues or []:
            if i.resolution is None and i.kind in _QUESTION_KINDS and _owns_issue(i, c, roles, my_domains):
                questions.append(QuestionItem(change_id=c.id, title=c.title, phase_label=c.lifecycle.phase_label,
                                              domain_id=c.business_domain_id, since=c.lifecycle.since, issue=i))
    with_questions = {q.change_id for q in questions}
    mine = [c for c in open_ if _mine(c, roles, my_domains)
            and not (c.lifecycle.next_action.action in _QUESTION_ACTIONS and c.id in with_questions)]
    mine.sort(key=lambda c: (c.lifecycle.next_action.kind != "decision", c.lifecycle.since or c.created_at))
    mine_ids = {c.id for c in mine} | with_questions

    your_requests: list = []
    if "domain_owner" in roles:
        your_requests = [c for c in open_ if c.business_domain_id in my_domains and c.id not in mine_ids]
        your_requests.sort(key=lambda c: c.lifecycle.since or c.created_at)

    stuck: list[StuckItem] = []
    per_domain: list[DomainLine] = []
    if roles & {"product_manager", "admin"}:
        for c in open_:
            lc = c.lifecycle
            if getattr(c, "backlog_hold", None):
                continue
            waited = _days(lc.since, now)
            setup = next((i for i in lc.issues or [] if i.resolution is None and i.kind == "setup" and i.blocks), None)
            if lc.health == "failed":
                reason = f"Needs attention: {lc.next_action.summary}"
            elif setup is not None:
                reason = f"Setup blocks it: {setup.title}"
            elif lc.health == "blocked":
                reason = f"Blocked: {lc.next_action.summary}"
            elif lc.next_action.owner not in ("jade", "none") and waited is not None and waited >= STUCK_DAYS:
                reason = f"Waiting {waited} days on {lc.next_action.owner_label or lc.next_action.owner}"
            else:
                continue
            stuck.append(StuckItem(change=c, reason=reason))
        groups: dict[Optional[str], list] = {}
        for c in open_:
            if c.lifecycle.phase == "understand":
                continue
            groups.setdefault(c.business_domain_id, []).append(c)
        for dom, rows in sorted(groups.items(), key=lambda kv: domain_names.get(kv[0] or "", "~")):
            waits = [d for d in (_days(c.lifecycle.since, now) for c in rows) if d is not None]
            on: dict[str, int] = {}
            for c in rows:
                na = c.lifecycle.next_action
                who = "JADE" if na.owner == "jade" else (na.owner_label or na.owner or "nobody")
                on[who] = on.get(who, 0) + 1
            per_domain.append(DomainLine(domain_id=dom, name=domain_names.get(dom or "", "No domain yet"),
                                         in_flight=len(rows), oldest_waiting_days=max(waits) if waits else None,
                                         waiting_on=on))

    # New requests waiting for "Start analysis" count as one item, as every view groups them.
    new_requests = [c for c in mine if c.lifecycle.next_action.action == "start_analysis"]
    attention = len(mine) - len(new_requests) + (1 if new_requests else 0) + len(questions)
    waits_mine = [d for d in (_days(c.lifecycle.since, now) for c in mine) if d is not None]
    waits_mine += [d for d in (_days(q.since, now) for q in questions) if d is not None]
    cutoff = (now - timedelta(days=31)).isoformat()
    return MyWork(
        needs_you=mine, questions=questions, your_requests=your_requests, stuck=stuck, per_domain=per_domain,
        waiting_on_others=[c for c in open_ if c.id not in mine_ids and not getattr(c, "backlog_hold", None)
                           and c.lifecycle.next_action.owner not in ("jade", "none")],
        jade_working=[c for c in open_ if c.lifecycle.next_action.owner == "jade"],
        in_progress_count=len([c for c in open_ if c.lifecycle.phase != "understand"]),
        requests_count=len([c for c in open_ if c.lifecycle.phase == "understand"]),
        completed_this_month=[c for c in changes if c.lifecycle.phase == "done" and c.lifecycle.outcome != "rejected"
                              and (c.lifecycle.since or "") >= cutoff],
        attention_count=attention,
        figures=figures(changes, attention=attention, oldest_mine=max(waits_mine) if waits_mine else None),
        roles=sorted(roles),
    )
