"""
Story refinement from process-analysis findings.

Agents (the refinement process analysis and the Architect) only propose
findings: missing requirements, controls and acceptance criteria. Nothing
they produce changes a story. A reviewer sees the proposed change as a diff
and applies selected findings; that creates a new story revision, attributed
to the authenticated reviewer, linked to the findings it came from and to
the exact process references in force at the time. Rejected and deferred
findings keep their status and reason.

A revision is compare-and-set on the current revision, and a finding can be
applied only once. Applying one is a material change to an approved story:
the story's current design is flagged for reassessment and pending or
approved work for the story can no longer execute under its approval.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..models.change import AcceptanceCriterion, UserStory
from ..persistence.db import connection

KINDS = {"missing_requirements": "requirement", "missing_controls": "control",
         "missing_acceptance_criteria": "acceptance_criterion"}
OPEN = ("proposed", "deferred")


class RefinementRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fid(company_id: str, story_id: str, source_ref: str, kind: str, text: str) -> str:
    return "F-" + hashlib.sha256(f"{company_id}|{story_id}|{source_ref}|{kind}|{text}".encode()).hexdigest()[:12]


# ---------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------
_STOP = {"the", "and", "for", "with", "that", "this", "must", "should", "are", "is", "be", "a", "an", "of", "to",
         "in", "on", "by", "or", "its", "it", "as", "at", "from", "each", "any", "all", "not", "no", "can", "cannot"}


def _tokens(text: str) -> set[str]:
    import re

    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


def substantively_same(a: str, b: str) -> bool:
    """Conservative: the same meaningful words, give or take a few."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.6 or (len(ta) >= 4 and ta <= tb) or (len(tb) >= 4 and tb <= ta)


def sync_findings(company_id: str, story_id: str) -> None:
    """Register every finding the agents produced for this story (idempotent)."""
    from ..discovery import baseline
    from . import story as story_process

    items: list[tuple[str, str, str, str]] = []
    for run in story_process.runs(company_id, story_id):
        if run["status"] != "completed":
            continue
        source = "scripted_refinement" if run["scripted"] or run["result"].get("scripted") else "refinement_agent"
        for key, kind in KINDS.items():
            items += [(source, run["run_id"], kind, t) for t in run["result"].get(key) or []]
    for b in baseline.list_for_story(company_id, story_id):
        findings = ((b["manifest"].get("process_context") or {}).get("findings") or {})
        for key, kind in KINDS.items():
            items += [("architect", f"design r{b['design_revision']}", kind, t) for t in findings.get(key) or []]
    now = _now()
    with connection(immediate=True) as conn:
        applied = [dict(r) for r in conn.execute(
            "SELECT finding_id, text, applied_in_revision FROM story_findings WHERE company_id = ? AND story_id = ? "
            "AND status = 'applied'", (company_id, story_id)).fetchall()]
        for source, ref, kind, text in items:
            text = str(text).strip()[:600]
            if not text:
                continue
            fid = _fid(company_id, story_id, ref, kind, text)
            if conn.execute("SELECT 1 FROM story_findings WHERE finding_id = ?", (fid,)).fetchone():
                continue
            same = next((a for a in applied if substantively_same(text, a["text"])), None)
            status, reason = ("duplicate", f"substantively the same as {same['finding_id']}, already applied in story "
                                           f"revision {same['applied_in_revision']}") if same else ("proposed", "")
            conn.execute("INSERT INTO story_findings (finding_id, company_id, story_id, source, source_ref, kind, text, "
                         "status, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (fid, company_id, story_id, source, ref, kind, text, status, reason, now))


def findings(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM story_findings WHERE company_id = ? AND story_id = ? ORDER BY created_at, kind, text",
            (company_id, story_id)).fetchall()]


def set_status(company_id: str, story_id: str, finding_id: str, *, status: str, reason: str, actor: str,
               actor_user_id: str) -> dict:
    if status not in ("rejected", "deferred", "proposed"):
        raise RefinementRefused("status must be rejected, deferred or proposed")
    if status in ("rejected", "deferred") and len(reason.strip()) < 5:
        raise RefinementRefused("give a short reason")
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT * FROM story_findings WHERE finding_id = ? AND company_id = ? AND story_id = ?",
                           (finding_id, company_id, story_id)).fetchone()
        if row is None:
            raise LookupError(f"no such finding: {finding_id}")
        if row["status"] == "applied":
            raise RefinementRefused("this finding is already applied to the story; a later revision can change the story")
        conn.execute("UPDATE story_findings SET status = ?, reason = ?, decided_by = ?, decided_by_user_id = ?, "
                     "decided_at = ? WHERE finding_id = ?", (status, reason.strip()[:1000], actor, actor_user_id, _now(), finding_id))
    return next(f for f in findings(company_id, story_id) if f["finding_id"] == finding_id)


# ---------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------
def _rev_row(r) -> dict:
    d = dict(r)
    for k in ("user_story", "applied_findings", "process_refs", "roles"):
        d[k] = json.loads(d[k])
    return d


def revisions(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        return [_rev_row(r) for r in conn.execute(
            "SELECT * FROM story_revisions WHERE company_id = ? AND story_id = ? ORDER BY revision DESC",
            (company_id, story_id)).fetchall()]


def latest_story(company_id: str, story_id: str) -> Optional[UserStory]:
    with connection() as conn:
        r = conn.execute("SELECT user_story FROM story_revisions WHERE company_id = ? AND story_id = ? "
                         "ORDER BY revision DESC LIMIT 1", (company_id, story_id)).fetchone()
    return UserStory.model_validate(json.loads(r["user_story"])) if r else None


def _norm(t: str) -> str:
    return " ".join(t.lower().split())


def propose(current: UserStory, selected: list[dict]) -> tuple[UserStory, list[dict]]:
    """The story with the selected findings applied, and a per-section diff."""
    rules = list(current.business_rules)
    acs = [a.model_copy() for a in current.acceptance_criteria]
    existing = {_norm(x) for x in rules} | {_norm(a.text) for a in acs}
    added: list[dict] = []
    for f in selected:
        if f["kind"] == "acceptance_criterion":
            if _norm(f["text"]) in existing:
                raise RefinementRefused(f"the story already contains: {f['text']}")
            n = 1 + max([int(a.id[2:]) for a in acs if a.id.startswith("AC") and a.id[2:].isdigit()] or [0])
            acs.append(AcceptanceCriterion(id=f"AC{n}", text=f["text"]))
            added.append({"section": "acceptance_criteria", "line": f"AC{n}: {f['text']}", "finding_id": f["finding_id"]})
        else:
            line = ("Requirement: " if f["kind"] == "requirement" else "Control: ") + f["text"]
            if _norm(line) in existing or _norm(f["text"]) in existing:
                raise RefinementRefused(f"the story already contains: {f['text']}")
            rules.append(line)
            added.append({"section": "business_rules", "line": line, "finding_id": f["finding_id"]})
        existing.add(_norm(f["text"]))
    new = current.model_copy(update={"business_rules": rules, "acceptance_criteria": acs})
    diff = []
    for section, before in (("business_rules", list(current.business_rules)),
                            ("acceptance_criteria", [f"{a.id}: {a.text}" for a in current.acceptance_criteria])):
        diff += [{"section": section, "op": " ", "line": x} for x in before]
        diff += [{"section": section, "op": "+", "line": a["line"], "finding_id": a["finding_id"]}
                 for a in added if a["section"] == section]
    return new, diff


def render_backlog_text(us: UserStory) -> str:
    """The legacy backlog text format change_service parses (and the
    Architect's get_approved_story returns)."""
    parts = [us.statement]
    if us.business_context:
        parts.append(f"business_context: {us.business_context}")
    if us.acceptance_criteria:
        parts.append("acceptance_criteria:\n" + "\n".join(
            f"- {a.id}: {a.text}" + (f" verified_by: {a.verified_by}" if a.verified_by else "") for a in us.acceptance_criteria))
    if us.business_rules:
        parts.append("business_rules:\n" + "\n".join(f"- {r}" for r in us.business_rules))
    if us.open_questions:
        parts.append("open_questions:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(us.open_questions, 1)))
    return "\n\n".join(parts)


def _sha(us: UserStory) -> str:
    return hashlib.sha256(json.dumps(us.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def preview(company_id: str, story_id: str, current: UserStory, finding_ids: list[str]) -> dict:
    by_id = {f["finding_id"]: f for f in findings(company_id, story_id)}
    selected = []
    for fid in finding_ids:
        f = by_id.get(fid)
        if f is None:
            raise LookupError(f"no such finding: {fid}")
        if f["status"] not in OPEN:
            raise RefinementRefused(f"finding {fid} is {f['status']}; only proposed or deferred findings can be applied")
        selected.append(f)
    if not selected:
        raise RefinementRefused("select at least one finding")
    new, diff = propose(current, selected)
    return {"story": new, "diff": diff, "selected": selected}


def apply(company_id: str, story_id: str, current: UserStory, finding_ids: list[str], *, expected_revision: int,
          note: str, actor: str, actor_user_id: str, roles: list[str]) -> dict:
    """Apply selected findings as a new story revision (compare-and-set)."""
    from . import story as story_process

    mapping = story_process.current_mapping(company_id, story_id)
    process_refs = {"mapping_revision": mapping["revision"] if mapping else None,
                    "status": mapping["status"] if mapping else None,
                    "refs": [{k: r[k] for k in ("framework_id", "version", "node_key", "node_sha256", "name")}
                             for r in (mapping or {}).get("refs", [])]}
    now = _now()
    with connection(immediate=True) as conn:
        cur = conn.execute("SELECT MAX(revision) AS r FROM story_revisions WHERE company_id = ? AND story_id = ?",
                           (company_id, story_id)).fetchone()["r"] or 0
        if cur != expected_revision:
            raise RefinementRefused(f"the story changed meanwhile (revision {cur}); reload and review the diff again")
        rows = {r["finding_id"]: dict(r) for r in conn.execute(
            f"SELECT * FROM story_findings WHERE company_id = ? AND story_id = ? AND finding_id IN "
            f"({','.join('?' * len(finding_ids))})", (company_id, story_id, *finding_ids)).fetchall()} if finding_ids else {}
        missing = [f for f in finding_ids if f not in rows]
        if missing:
            raise LookupError(f"no such finding: {', '.join(missing)}")
        done = [f for f, r in rows.items() if r["status"] not in OPEN]
        if done:
            raise RefinementRefused(f"already {rows[done[0]]['status']}: {', '.join(done)} -- a finding is applied at most once")
        if not rows:
            raise RefinementRefused("select at least one finding")
        new, diff = propose(current, [rows[f] for f in finding_ids])
        if cur == 0:  # keep the story as it was before the first applied change
            conn.execute("INSERT INTO story_revisions (company_id, story_id, revision, source, user_story, story_sha256, "
                         "process_refs, author_name, author_user_id, note, created_at) VALUES (?, ?, 1, 'approved_story', ?, ?, ?, "
                         "'(as approved)', '', 'the approved story before process refinement', ?)",
                         (company_id, story_id, json.dumps(current.model_dump(mode="json")), _sha(current),
                          json.dumps(process_refs), now))
            cur = 1
        rev = cur + 1
        applied = [{"finding_id": f, "kind": rows[f]["kind"], "text": rows[f]["text"], "source": rows[f]["source"],
                    "source_ref": rows[f]["source_ref"]} for f in finding_ids]
        conn.execute("INSERT INTO story_revisions (company_id, story_id, revision, source, user_story, story_sha256, "
                     "applied_findings, process_refs, author_name, author_user_id, roles, note, created_at) "
                     "VALUES (?, ?, ?, 'process_refinement', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (company_id, story_id, rev, json.dumps(new.model_dump(mode="json")), _sha(new), json.dumps(applied),
                      json.dumps(process_refs), actor, actor_user_id, json.dumps(sorted(roles)), note.strip()[:1000], now))
        conn.executemany("UPDATE story_findings SET status = 'applied', applied_in_revision = ?, decided_by = ?, "
                         "decided_by_user_id = ?, decided_at = ? WHERE finding_id = ?",
                         [(rev, actor, actor_user_id, now, f) for f in finding_ids])
    _after_revision(company_id, story_id, new, rev, actor, len(finding_ids))
    return {"revision": rev, "diff": diff, "story": new.model_dump(mode="json")}


def _after_revision(company_id: str, story_id: str, story: UserStory, rev: int, actor: str, count: int) -> None:
    """Keep the Architect's copy in step, and flag everything that rested on
    the previous story text."""
    from jde_mcp_server import backlog

    from ..discovery import baseline
    from ..services.work_invalidation import invalidate_affected

    backlog.record_story_revision(story_id, render_backlog_text(story), revision=rev, revised_by=actor,
                                  reason=f"{count} process-analysis finding(s) applied")
    b = baseline.current_for_story(company_id, story_id)
    if b and b["status"] != "superseded":
        baseline._flag(b, {"kind": "story_revised",
                           "detail": f"story revision {rev} by {actor}: {count} finding(s) applied"})
    invalidate_affected(company_id, source=f"story revision {rev} by {actor}", story_id=story_id, story_revised=rev)


def view(company_id: str, story_id: str) -> dict[str, Any]:
    sync_findings(company_id, story_id)
    revs = revisions(company_id, story_id)
    return {"findings": findings(company_id, story_id), "revisions": revs,
            "current_revision": revs[0]["revision"] if revs else 0}
