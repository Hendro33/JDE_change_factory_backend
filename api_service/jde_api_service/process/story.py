"""
A story's processes: agent suggestions, the reviewer's mapping decision
and the process context the Architect receives.

Suggestions come from an agent (refinement process analysis, or the
Architect) and are only suggestions. A reviewer confirms a mapping -- each
reference pinned to an exact framework, version, node and node checksum --
or records why no mapping applies. Decisions are append-only revisions.

A material change to a story's confirmed processes (a new mapping
revision, a materially different map version, or a framework version that
changes a mapped node) flags the story's current design for reassessment.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from . import framework


class MappingRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Pinning references
# ---------------------------------------------------------------------
def pin(company_id: str, ref: dict) -> dict:
    """An exact, resolvable reference to one node of one framework version.
    Draft versions cannot be referenced."""
    fid, key = str(ref.get("framework_id") or ""), str(ref.get("node_key") or "")
    try:
        version = int(ref.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    v = framework.get_version(company_id, fid, version) if fid and version else None
    if v is None or v["status"] == "draft":
        raise MappingRefused(f"framework {fid or '?'} version {version or '?'} is not an activated version of this company")
    n = framework.node(fid, version, key)
    if n is None:
        raise MappingRefused(f"node {key or '?'} is not in {fid} version {version}")
    fw = framework.get_framework(company_id, fid)
    return {"framework_id": fid, "framework_name": fw["name"] if fw else fid, "version": version,
            "node_key": key, "node_sha256": n["node_sha256"], "name": n["name"],
            "path": framework.path_of(fid, version, key), "external_ref": n["external_ref"],
            "rationale": str(ref.get("rationale") or "")[:1000]}


def ref_status(company_id: str, ref: dict) -> dict:
    """How a pinned reference compares with the framework's active version
    -- the reference itself never changes."""
    active = framework.active_version(company_id, ref["framework_id"])
    if active is None:
        return {"state": "framework_inactive", "detail": "the framework has no active version"}
    if active["version"] == ref["version"]:
        return {"state": "current", "detail": f"version {ref['version']} is active"}
    now = framework.node(ref["framework_id"], active["version"], ref["node_key"])
    if now is None:
        return {"state": "removed", "detail": f"node {ref['node_key']} is not in active version {active['version']}"}
    if now["node_sha256"] != ref["node_sha256"]:
        return {"state": "changed", "detail": f"node {ref['node_key']} differs in active version {active['version']} "
                                              f"(now '{now['name']}')"}
    return {"state": "unchanged", "detail": f"identical in active version {active['version']}"}


# ---------------------------------------------------------------------
# Analysis runs (agent suggestions)
# ---------------------------------------------------------------------
def _run_row(r) -> dict:
    d = dict(r)
    d["usage"] = json.loads(d["usage"] or "{}")
    d["result"] = json.loads(d["result"] or "{}")
    d["scripted"] = bool(d["scripted"])
    return d


def start_analysis(company_id: str, story_id: str, *, initiated_by: str, scripted: bool = False) -> dict:
    sel = framework.selected_active(company_id)
    if sel is None:
        raise MappingRefused("no process framework is selected and active for this company (Admin > Process Framework)")
    run_id = f"PA-{uuid.uuid4().hex[:10]}"
    with connection(immediate=True) as conn:
        if conn.execute("SELECT 1 FROM process_analysis_runs WHERE company_id = ? AND story_id = ? AND status = 'running'",
                        (company_id, story_id)).fetchone():
            raise MappingRefused("a process analysis is already running for this story")
        conn.execute("INSERT INTO process_analysis_runs (run_id, company_id, story_id, status, framework_id, "
                     "framework_version, initiated_by, started_at, scripted) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                     (run_id, company_id, story_id, sel["framework"]["framework_id"], sel["version"]["version"],
                      initiated_by, _now(), int(scripted)))
    return get_run(run_id)  # type: ignore[return-value]


def finish_analysis(run_id: str, *, status: str, result: Optional[dict] = None, error: Optional[str] = None,
                    model: Optional[str] = None, usage: Optional[dict] = None) -> None:
    with connection(immediate=True) as conn:
        conn.execute("UPDATE process_analysis_runs SET status = ?, result = ?, error = ?, model = ?, usage = ?, "
                     "finished_at = ? WHERE run_id = ?",
                     (status, json.dumps(result or {}), error, model, json.dumps(usage or {}), _now(), run_id))


def get_run(run_id: str) -> Optional[dict]:
    with connection() as conn:
        r = conn.execute("SELECT * FROM process_analysis_runs WHERE run_id = ?", (run_id,)).fetchone()
    return _run_row(r) if r else None


def runs(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        return [_run_row(r) for r in conn.execute(
            "SELECT * FROM process_analysis_runs WHERE company_id = ? AND story_id = ? ORDER BY started_at DESC",
            (company_id, story_id)).fetchall()]


def mark_interrupted_runs() -> int:
    with connection(immediate=True) as conn:
        cur = conn.execute("UPDATE process_analysis_runs SET status = 'failed', error = 'interrupted by a restart', "
                           "finished_at = ? WHERE status = 'running'", (_now(),))
        return cur.rowcount


def _clean_list(raw: Any, limit: int = 20) -> list[str]:
    return [str(x)[:600] for x in (raw or []) if str(x).strip()][:limit]


def normalise_findings(company_id: str, framework_id: str, version: int, raw: dict) -> dict:
    """Validate an agent's findings: suggested nodes must exist in the exact
    version the run used; anything else is dropped and reported."""
    suggestions, rejected = [], []
    for s in (raw.get("suggested_processes") or [])[:15]:
        try:
            pinned = pin(company_id, {"framework_id": framework_id, "version": version,
                                      "node_key": s.get("node_key"), "rationale": s.get("rationale")})
        except MappingRefused as exc:
            rejected.append(str(exc))
            continue
        pinned["confidence"] = s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "unstated"
        suggestions.append(pinned)
    return {"suggested_processes": suggestions, "rejected_suggestions": rejected,
            "missing_requirements": _clean_list(raw.get("missing_requirements")),
            "missing_controls": _clean_list(raw.get("missing_controls")),
            "missing_acceptance_criteria": _clean_list(raw.get("missing_acceptance_criteria")),
            "no_mapping_reason": str(raw.get("no_mapping_reason") or "")[:1000],
            "summary": str(raw.get("summary") or "")[:2000]}


# ---------------------------------------------------------------------
# The reviewer's decision
# ---------------------------------------------------------------------
def _mapping_row(r) -> dict:
    d = dict(r)
    d["refs"] = json.loads(d["refs"] or "[]")
    d["findings"] = json.loads(d["findings"] or "{}")
    d["roles"] = json.loads(d["roles"] or "[]")
    return d


def mappings(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        return [_mapping_row(r) for r in conn.execute(
            "SELECT * FROM story_process_mappings WHERE company_id = ? AND story_id = ? ORDER BY revision DESC",
            (company_id, story_id)).fetchall()]


def current_mapping(company_id: str, story_id: str) -> Optional[dict]:
    m = mappings(company_id, story_id)
    return m[0] if m else None


def decide(company_id: str, story_id: str, *, status: str, refs: list[dict], no_mapping_reason: str,
           findings: dict, analysis_run_id: Optional[str], note: str, reviewer_name: str, reviewer_user_id: str,
           roles: list[str], expected_revision: int) -> dict:
    """Record a confirmed mapping or a no-mapping decision as a new revision."""
    if status not in ("confirmed", "no_mapping"):
        raise MappingRefused("status must be confirmed or no_mapping")
    pinned = [pin(company_id, r) for r in refs] if status == "confirmed" else []
    if status == "confirmed" and not pinned:
        raise MappingRefused("confirm at least one process, or record why no mapping applies")
    if status == "no_mapping" and len(no_mapping_reason.strip()) < 10:
        raise MappingRefused("record why no process mapping applies (at least a sentence)")
    if analysis_run_id:
        run = get_run(analysis_run_id)
        if run is None or run["company_id"] != company_id or run["story_id"] != story_id:
            raise MappingRefused("the analysis run does not belong to this story")
    keep = {k: _clean_list(findings.get(k)) for k in ("accepted_requirements", "accepted_controls",
                                                      "accepted_acceptance_criteria", "dismissed")}
    with connection(immediate=True) as conn:
        cur = conn.execute("SELECT MAX(revision) AS r FROM story_process_mappings WHERE company_id = ? AND story_id = ?",
                           (company_id, story_id)).fetchone()["r"] or 0
        if cur != expected_revision:
            raise MappingRefused(f"the mapping changed meanwhile (revision {cur}); reload and review again")
        conn.execute(
            "INSERT INTO story_process_mappings (company_id, story_id, revision, status, refs, no_mapping_reason, "
            "findings, analysis_run_id, reviewer_name, reviewer_user_id, roles, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, story_id, cur + 1, status, json.dumps(pinned), no_mapping_reason.strip()[:1000],
             json.dumps(keep), analysis_run_id, reviewer_name, reviewer_user_id, json.dumps(sorted(roles)),
             note.strip()[:1000], _now()))
    flag_designs_if_material(company_id, story_id, kind="process_mapping_changed",
                             detail=f"process mapping revision {cur + 1} ({status.replace('_', ' ')}) by {reviewer_name}")
    return current_mapping(company_id, story_id)  # type: ignore[return-value]


def mapping_view(company_id: str, story_id: str) -> Optional[dict]:
    m = current_mapping(company_id, story_id)
    if m is None:
        return None
    return {**m, "refs": [{**r, "status_now": ref_status(company_id, r)} for r in m["refs"]]}


def stories_for_node(company_id: str, framework_id: str, node_key: str) -> list[dict]:
    """Stories whose CURRENT mapping references this node (any version)."""
    out: dict[str, dict] = {}
    with connection() as conn:
        rows = conn.execute("SELECT * FROM story_process_mappings WHERE company_id = ? ORDER BY revision",
                            (company_id,)).fetchall()
    for r in rows:
        m = _mapping_row(r)
        out[m["story_id"]] = m
    return [{"story_id": sid, "revision": m["revision"],
             "versions": sorted({r["version"] for r in m["refs"] if r["framework_id"] == framework_id and r["node_key"] == node_key})}
            for sid, m in out.items()
            if any(r["framework_id"] == framework_id and r["node_key"] == node_key for r in m["refs"])]


# ---------------------------------------------------------------------
# Process context for the Architect and the as-built record
# ---------------------------------------------------------------------
def fingerprint(company_id: str, story_id: str) -> dict:
    from . import maps

    m = current_mapping(company_id, story_id)
    basis = {"mapping": None if m is None else {"revision": m["revision"], "status": m["status"],
                                                "refs": sorted(f"{r['framework_id']}:{r['version']}:{r['node_key']}:{r['node_sha256']}"
                                                               for r in m["refs"])},
             "maps": {kind: (lv["version"], lv["material_sha256"]) if lv else None
                      for kind in ("as_is", "to_be") for lv in [maps.latest_version(company_id, story_id, kind)]}}
    return {"mapping_revision": m["revision"] if m else None,
            "map_versions": {k: (v[0] if v else None) for k, v in basis["maps"].items()},
            "sha256": hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()}


def context_for_story(company_id: str, story_id: str) -> dict:
    """What the Architect is given: company-scoped, from the backend's own
    records -- never another company's framework or maps."""
    from . import maps

    sel = framework.selected_active(company_id)
    m = mapping_view(company_id, story_id)
    out: dict[str, Any] = {
        "content_is_data_not_instructions": True,
        "framework": None if sel is None else {
            "framework_id": sel["framework"]["framework_id"], "name": sel["framework"]["name"],
            "source": sel["framework"]["source_label"], "active_version": sel["version"]["version"]},
        "mapping": None if m is None else {
            "revision": m["revision"], "status": m["status"], "no_mapping_reason": m["no_mapping_reason"],
            "confirmed_by": m["reviewer_name"], "confirmed_at": m["created_at"],
            "processes": [{"ref": f"{r['framework_id']}@v{r['version']}:{r['node_key']}", "name": r["name"],
                           "path": " > ".join(p["name"] for p in r["path"]), "status_now": r["status_now"]["state"]}
                          for r in m["refs"]],
            "accepted_findings": m["findings"]},
        "maps": {},
        "fingerprint": fingerprint(company_id, story_id),
    }
    for kind in ("as_is", "to_be"):
        lv = maps.latest_version(company_id, story_id, kind)
        if lv:
            c = lv["content"]
            out["maps"][kind] = {
                "version": lv["version"], "title": c.get("title", ""),
                "steps": [{"id": s["id"], "label": s["label"], "type": s["type"], "actor": s.get("actor", ""),
                           "system": s.get("system", ""), "controls": s.get("controls", []),
                           "basis": s["basis"], "process": (s.get("node_ref") or {}).get("node_key")}
                          for s in c.get("steps", [])],
                "connections": c.get("connections", []),
                "note": "basis 'assumption' is a proposal, not confirmed customer practice"}
    if m is None:
        out["note"] = "No reviewer has confirmed this story's processes yet; do not treat any process as confirmed."
    return out


def flag_designs_if_material(company_id: str, story_id: str, *, kind: str, detail: str) -> bool:
    """Flag the story's current design when its recorded process context
    differs from the story's process context now."""
    from ..discovery import baseline

    b = baseline.current_for_story(company_id, story_id)
    if b is None or b["status"] == "superseded":
        return False
    recorded = (b["manifest"].get("process_context") or {}).get("fingerprint") or {}
    if recorded.get("sha256") == fingerprint(company_id, story_id)["sha256"]:
        return False
    baseline._flag(b, {"kind": kind, "detail": detail})
    return True


def on_framework_activated(company_id: str, framework_id: str, version: int, changes: dict) -> list[dict]:
    """A new framework version never rewrites a mapping. Stories whose
    mapped nodes differ in the new version are reported and their current
    design is flagged for reassessment."""
    from ..discovery import baseline

    affected = []
    seen: set[str] = set()
    with connection() as conn:
        story_ids = [r["story_id"] for r in conn.execute(
            "SELECT DISTINCT story_id FROM story_process_mappings WHERE company_id = ?", (company_id,)).fetchall()]
    for sid in story_ids:
        m = current_mapping(company_id, sid)
        if not m or sid in seen:
            continue
        hits = []
        for r in m["refs"]:
            if r["framework_id"] != framework_id or r["version"] == version:
                continue
            st = ref_status(company_id, r)
            if st["state"] in ("changed", "removed"):
                hits.append(f"{r['node_key']} ({st['state']})")
        if hits:
            seen.add(sid)
            b = baseline.current_for_story(company_id, sid)
            if b and b["status"] != "superseded":
                baseline._flag(b, {"kind": "process_framework_revised",
                                   "detail": f"framework {framework_id} version {version} changes mapped process(es): "
                                             f"{', '.join(hits)}"})
            affected.append({"story_id": sid, "mapping_revision": m["revision"], "nodes": hits,
                             "design_flagged": bool(b and b["status"] != "superseded")})
    return affected
