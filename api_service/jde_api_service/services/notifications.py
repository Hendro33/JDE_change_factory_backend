"""Notifications: decisions, meaningful completions and exceptions.

Derived from the canonical lifecycle, never typed in by hand: on each
automation pass the story's lifecycle is compared with the one seen before
(services/automation.py). A change of who has to act, a finished story, or a
story that became blocked or failed is one notification -- for the people
whose role has to act (a Domain Owner only for stories in their own domain).
JADE starting work is not a notification; neither is every small step.

People read them under the bell ("What happened"). A daily e-mail digest is
an optional per-person preference, sent only when e-mail is set up on the
server and there is something new; it is never a second compulsory stream.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("jde_api_service")

KIND = "notifications"
READS_KIND = "notification_reads"
SNAPSHOT_KIND = "lifecycle_snapshots"
MAX_ITEMS = 300
HUMAN_OWNERS = ("product_manager", "domain_owner", "admin", "cnc_operator")
DIGEST_HOUR_LOCAL = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _parse(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------
def add(company_id: str, *, kind: str, audience: Iterable[str], title: str, text: str,
        story_id: Optional[str] = None, tab: Optional[str] = None, domain_id: Optional[str] = None,
        key: Optional[str] = None) -> bool:
    """Record one notification. A key makes it happen once (False if it
    already exists)."""
    from jde_mcp_server import docstore

    audience = sorted({a for a in audience if a})
    if not audience:
        return False
    at = _iso()
    key = key or f"{story_id}:{at}"
    item = {"id": hashlib.sha256(f"{company_id}|{key}".encode()).hexdigest()[:16], "key": key, "at": at,
            "kind": kind, "audience": audience, "title": title[:200], "text": text[:600], "story_id": story_id,
            "tab": tab, "domain_id": domain_id}
    with docstore.transaction():
        doc = docstore.get(KIND, company_id) or {"company_id": company_id, "items": []}
        items = list(doc.get("items", []))
        if any(i.get("key") == key for i in items):
            return False
        items.append(item)
        doc["items"] = items[-MAX_ITEMS:]
        docstore.put(KIND, company_id, doc, company_id=company_id)
    return True


def _visible(item: dict, roles: set[str], domain_ids: set[str]) -> bool:
    audience = set(item.get("audience", []))
    if audience & (roles - {"domain_owner"}):
        return True
    if "domain_owner" in audience and "domain_owner" in roles:
        return not item.get("domain_id") or item["domain_id"] in domain_ids
    return False


def prefs(company_id: str, user_id: str) -> dict:
    from jde_mcp_server import docstore

    try:
        doc = docstore.get(READS_KIND, f"{company_id}:{user_id}") or {}
    except Exception:  # noqa: BLE001
        doc = {}
    return {"seen_at": doc.get("seen_at"), "digest": doc.get("digest", "off"),
            "last_digest_at": doc.get("last_digest_at")}


def _save_prefs(company_id: str, user_id: str, **changes: Any) -> dict:
    from jde_mcp_server import docstore

    with docstore.transaction():
        doc = docstore.get(READS_KIND, f"{company_id}:{user_id}") or {"company_id": company_id, "user_id": user_id}
        doc.update(changes)
        docstore.put(READS_KIND, f"{company_id}:{user_id}", doc, company_id=company_id)
    return prefs(company_id, user_id)


def for_user(company_id: str, user_id: str, roles: Iterable[str], domain_ids: Iterable[str],
             limit: int = 50) -> dict:
    from jde_mcp_server import docstore

    try:
        doc = docstore.get(KIND, company_id) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    roles_, domains = set(roles), set(domain_ids)
    p = prefs(company_id, user_id)
    seen = p["seen_at"] or ""
    items = [i for i in reversed(doc.get("items", [])) if _visible(i, roles_, domains)]
    out = [{"id": i["id"], "at": i["at"], "kind": i["kind"], "title": i["title"], "text": i["text"],
            "story_id": i.get("story_id"), "tab": i.get("tab"), "unread": i["at"] > seen} for i in items[:limit]]
    return {"items": out, "unread": sum(1 for i in items if i["at"] > seen), "digest": p["digest"]}


def mark_read(company_id: str, user_id: str) -> dict:
    return _save_prefs(company_id, user_id, seen_at=_iso())


def set_digest(company_id: str, user_id: str, digest: str) -> dict:
    return _save_prefs(company_id, user_id, digest="daily" if digest == "daily" else "off",
                       last_digest_at=_iso() if digest == "daily" else None)


# ---------------------------------------------------------------------
# Deriving notifications from lifecycle changes
# ---------------------------------------------------------------------
def _snapshot(lc) -> dict:
    na = lc.next_action
    return {"phase": lc.phase, "health": lc.health, "owner": na.owner, "action": na.action or "", "kind": na.kind,
            "outcome": lc.outcome or "", "summary": na.summary}


_OUTCOME_TITLES = {"delivered": "Delivered", "resolved_without_change": "Resolved without a change to JD Edwards",
                   "rejected": "Closed: not going ahead"}


def observe(company_id: str, change, lc) -> bool:
    """Compare this story's lifecycle with the one seen last time and record
    at most one notification for the change. The first sighting of a story
    only records what it looks like (no flood after an upgrade)."""
    from jde_mcp_server import docstore

    now = _snapshot(lc)
    doc_id = f"{company_id}:{change.id}"
    with docstore.transaction():
        before = docstore.get(SNAPSHOT_KIND, doc_id)
        if before is not None and all(before.get(k) == now[k] for k in ("phase", "health", "owner", "action",
                                                                         "outcome")):
            return False
        docstore.put(SNAPSHOT_KIND, doc_id, {"company_id": company_id, "story_id": change.id, **now,
                                             "at": _iso()}, company_id=company_id)
    if before is None:
        return False
    domain = getattr(change, "business_domain_id", None)
    title = change.title
    owner = now["owner"]
    key = f"lc:{change.id}:{now['phase']}:{now['health']}:{owner}:{now['action']}:{now['outcome']}:{_iso()[:16]}"
    finished = before.get("owner") == "jade" and owner != "jade"
    if now["phase"] == "done" and before.get("phase") != "done":
        audience = ["product_manager"] + (["domain_owner"] if domain else [])
        text = _OUTCOME_TITLES.get(now["outcome"], "Finished") + f". {now['summary']}"
        return add(company_id, kind="completion", audience=audience, title=title, text=text, story_id=change.id,
                   tab="overview", domain_id=domain, key=key)
    if now["health"] in ("blocked", "failed") and (before.get("health") not in ("blocked", "failed")
                                                    or before.get("action") != now["action"]):
        audience = {"product_manager"} | ({owner} if owner in HUMAN_OWNERS else set())
        return add(company_id, kind="exception", audience=audience, title=title,
                   text=("Needs attention: " if now["health"] == "failed" else "Blocked: ") + now["summary"],
                   story_id=change.id, tab=lc.next_action.tab, domain_id=domain, key=key)
    if owner in HUMAN_OWNERS and now["kind"] in ("decision", "task") and (
            before.get("owner") != owner or before.get("action") != now["action"]):
        lead = "JADE finished its part. " if finished else ""
        return add(company_id, kind="decision" if now["kind"] == "decision" else "task", audience=[owner],
                   title=title, text=lead + now["summary"], story_id=change.id, tab=lc.next_action.tab,
                   domain_id=domain, key=key)
    return False


# ---------------------------------------------------------------------
# The optional daily digest
# ---------------------------------------------------------------------
def _local_hour() -> int:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Europe/Amsterdam")).hour
    except Exception:  # noqa: BLE001
        return _now().hour


def send_digests(company_id: str, *, force: bool = False) -> list[str]:
    """Once a day (from 07:00 Amsterdam time), one e-mail per person who chose
    the digest and has something new. Nothing is sent without e-mail set up."""
    from . import membership_service
    from .email_service import OutgoingEmail, get_email_service, public_url

    mail = get_email_service()
    if not getattr(mail, "configured", False) or (not force and _local_hour() < DIGEST_HOUR_LOCAL):
        return []
    sent: list[str] = []
    for m in membership_service.list_company_members(company_id):
        if m.get("status") != "active" or not m.get("email"):
            continue
        p = prefs(company_id, m["user_id"])
        if p["digest"] != "daily":
            continue
        last = _parse(p["last_digest_at"])
        if not force and last is not None and _now() - last < timedelta(hours=20):
            continue
        since = p["last_digest_at"] or _iso(_now() - timedelta(days=1))
        feed = for_user(company_id, m["user_id"], m.get("roles", []), m.get("domain_ids", []), limit=100)
        new = [i for i in feed["items"] if i["at"] > since]
        _save_prefs(company_id, m["user_id"], last_digest_at=_iso())
        if not new:
            continue
        lines = [f"- {i['title']}: {i['text']}" for i in new[:40]]
        body = (f"Hello {m.get('display_name') or ''},\n\nWhat happened in JADE since your last digest:\n\n"
                + "\n".join(lines) + f"\n\nOpen JADE: {public_url()}\n\nYou chose this daily digest; turn it off "
                "under the bell in JADE.")
        result = mail.send(OutgoingEmail(to=m["email"], subject=f"JADE daily digest: {len(new)} update(s)", body=body))
        if result.sent:
            sent.append(m["user_id"])
    return sent
