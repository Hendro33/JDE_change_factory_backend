"""
As-is and to-be process maps beside a story: steps, actors, decisions,
connections, systems and controls, each step optionally linked to a
framework node (pinned to its version) and to affected stories.

Every save is a new immutable version. A step's basis is either an
'assumption' (proposed, not yet confirmed) or 'confirmed' customer
practice -- and a confirmed step must say where the confirmation came
from. A version whose material content differs from the previous one
flags the story's current design for reassessment; a wording-only edit
(a note or a title) does not.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from . import story as story_process

KINDS = ("as_is", "to_be")
STEP_TYPES = ("start", "task", "decision", "end")
BASES = ("assumption", "confirmed")
MAX_STEPS = 80


class MapRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v: Any, n: int = 300) -> str:
    return str(v or "").strip()[:n]


def normalise(company_id: str, raw: dict, *, story_exists) -> dict:
    """Validate and pin a map's content. Nothing is silently repaired."""
    steps_in = raw.get("steps") or []
    if not isinstance(steps_in, list) or not steps_in:
        raise MapRefused("a map needs at least one step")
    if len(steps_in) > MAX_STEPS:
        raise MapRefused(f"a map may have at most {MAX_STEPS} steps")
    steps, ids = [], set()
    for i, s in enumerate(steps_in, start=1):
        sid = _s(s.get("id"), 20) or f"S{i}"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
            raise MapRefused(f"step id '{sid}' may only contain letters, digits, - and _")
        if sid in ids:
            raise MapRefused(f"step id {sid} is used twice")
        ids.add(sid)
        label = _s(s.get("label"))
        if not label:
            raise MapRefused(f"step {sid} needs a label")
        stype = s.get("type") or "task"
        if stype not in STEP_TYPES:
            raise MapRefused(f"step {sid}: type must be one of {', '.join(STEP_TYPES)}")
        basis = s.get("basis") or "assumption"
        if basis not in BASES:
            raise MapRefused(f"step {sid}: basis must be assumption or confirmed")
        source = _s(s.get("confirmation_source"), 500)
        if basis == "confirmed" and len(source) < 5:
            raise MapRefused(f"step {sid} is marked confirmed customer practice: say who confirmed it and how "
                             "(for example 'walkthrough with the warehouse lead, 2026-09-20')")
        node_ref = None
        if s.get("node_ref") and (s["node_ref"].get("node_key") or "").strip():
            try:
                p = story_process.pin(company_id, s["node_ref"])
            except story_process.MappingRefused as exc:
                raise MapRefused(f"step {sid}: {exc}") from exc
            node_ref = {k: p[k] for k in ("framework_id", "version", "node_key", "node_sha256", "name")}
        story_ids = sorted({_s(x, 60) for x in (s.get("story_ids") or []) if _s(x, 60)})
        for x in story_ids:
            if not story_exists(x):
                raise MapRefused(f"step {sid}: story {x} is not a story of this company")
        steps.append({"id": sid, "label": label, "type": stype, "actor": _s(s.get("actor"), 120),
                      "system": _s(s.get("system"), 120),
                      "controls": [_s(c, 300) for c in (s.get("controls") or []) if _s(c, 300)][:10],
                      "node_ref": node_ref, "story_ids": story_ids, "basis": basis,
                      "confirmation_source": source if basis == "confirmed" else "",
                      "notes": _s(s.get("notes"), 1000)})
    connections, seen = [], set()
    for c in raw.get("connections") or []:
        a, b = _s(c.get("from"), 20), _s(c.get("to"), 20)
        if a not in ids or b not in ids:
            raise MapRefused(f"connection {a or '?'} -> {b or '?'} refers to a step that is not in the map")
        if a == b:
            raise MapRefused(f"step {a} cannot connect to itself")
        if (a, b) in seen:
            continue
        seen.add((a, b))
        connections.append({"from": a, "to": b, "label": _s(c.get("label"), 80)})
    for s in steps:
        if s["type"] == "decision" and sum(1 for c in connections if c["from"] == s["id"]) < 2:
            raise MapRefused(f"decision {s['id']} needs at least two outgoing connections")
    return {"title": _s(raw.get("title")) or "Process map", "steps": steps, "connections": connections}


def material_sha(content: dict) -> str:
    """Everything except wording-only fields (title, step notes)."""
    material = {"steps": [{k: v for k, v in s.items() if k != "notes"} for s in content["steps"]],
                "connections": content["connections"]}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _version_row(r) -> dict:
    d = dict(r)
    d["content"] = json.loads(d["content"])
    d["material_change"] = bool(d["material_change"])
    return d


def get_map(company_id: str, story_id: str, kind: str) -> Optional[dict]:
    with connection() as conn:
        r = conn.execute("SELECT * FROM process_maps WHERE company_id = ? AND story_id = ? AND kind = ?",
                         (company_id, story_id, kind)).fetchone()
    return dict(r) if r else None


def versions(company_id: str, story_id: str, kind: str) -> list[dict]:
    m = get_map(company_id, story_id, kind)
    if m is None:
        return []
    with connection() as conn:
        return [_version_row(r) for r in conn.execute(
            "SELECT * FROM process_map_versions WHERE map_id = ? ORDER BY version DESC", (m["map_id"],)).fetchall()]


def latest_version(company_id: str, story_id: str, kind: str) -> Optional[dict]:
    v = versions(company_id, story_id, kind)
    return v[0] if v else None


def save(company_id: str, story_id: str, kind: str, raw: dict, *, note: str, actor: str, actor_user_id: str,
         expected_version: int, story_exists) -> dict:
    if kind not in KINDS:
        raise MapRefused("kind must be as_is or to_be")
    content = normalise(company_id, raw, story_exists=story_exists)
    blob = json.dumps(content, sort_keys=True)
    sha, msha = hashlib.sha256(blob.encode()).hexdigest(), material_sha(content)
    with connection(immediate=True) as conn:
        m = conn.execute("SELECT * FROM process_maps WHERE company_id = ? AND story_id = ? AND kind = ?",
                         (company_id, story_id, kind)).fetchone()
        if m is None:
            map_id = f"PM-{uuid.uuid4().hex[:8]}"
            conn.execute("INSERT INTO process_maps (map_id, company_id, story_id, kind, title, created_by, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (map_id, company_id, story_id, kind, content["title"], actor, _now()))
            prev = None
        else:
            map_id = m["map_id"]
            prev = conn.execute("SELECT version, material_sha256, content_sha256 FROM process_map_versions WHERE map_id = ? "
                                "ORDER BY version DESC LIMIT 1", (map_id,)).fetchone()
        current = prev["version"] if prev else 0
        if current != expected_version:
            raise MapRefused(f"the map changed meanwhile (version {current}); reload and apply your edits again")
        if prev and prev["content_sha256"] == sha:
            raise MapRefused("nothing changed")
        material = prev is None or prev["material_sha256"] != msha
        conn.execute("INSERT INTO process_map_versions (map_id, version, company_id, content, content_sha256, "
                     "material_sha256, material_change, note, created_by, created_by_user_id, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (map_id, current + 1, company_id, blob, sha, msha, int(material), note.strip()[:1000], actor,
                      actor_user_id, _now()))
        conn.execute("UPDATE process_maps SET title = ? WHERE map_id = ?", (content["title"], map_id))
    flagged = False
    if material:
        flagged = story_process.flag_designs_if_material(
            company_id, story_id, kind="process_map_changed",
            detail=f"{kind.replace('_', '-')} map version {current + 1} by {actor}: material change")
    return {**latest_version(company_id, story_id, kind), "design_flagged": flagged}  # type: ignore[dict-item]


def mermaid(content: dict) -> str:
    """A Mermaid flowchart of the map, for the Markdown record."""
    def esc(t: str) -> str:
        return t.replace('"', "'")

    lines = ["flowchart TD"]
    for s in content["steps"]:
        label = esc(s["label"] + (f" ({s['actor']})" if s.get("actor") else "")
                    + (" [assumption]" if s["basis"] == "assumption" else ""))
        shape = {"start": f'(["{label}"])', "end": f'(["{label}"])', "decision": f'{{"{label}"}}'}.get(s["type"], f'["{label}"]')
        lines.append(f"  {s['id']}{shape}")
    for c in content["connections"]:
        lines.append(f"  {c['from']} -->{'|' + esc(c['label']) + '|' if c['label'] else ''} {c['to']}")
    return "\n".join(lines)
