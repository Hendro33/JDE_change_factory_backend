"""
As-built records: what was actually delivered for a story, generated from
Jade's own records -- the approved story, its confirmed processes and
maps, the approved design and evidence baseline, the implementation that
was applied and the verification evidence -- with deviations and
unresolved limitations stated, never smoothed over.

Each generation is a new version (a draft). A draft can be finalised only
when every required delivery checkpoint is complete and nothing it was
generated from has changed since. Simulated delivery is labelled as such
in the record and in its Markdown; it is never presented as JDE evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from ..persistence.db import connection
from . import maps, story as story_process

SIMULATED_NOTICE = ("SIMULATED DELIVERY -- implemented and verified in Jade's simulated DEV estate. No customer JD "
                    "Edwards system was changed and none of the evidence below is JDE evidence.")
TECHNICAL_ROUTES = {"Technical Agent", "Mixed"}


class AsBuiltRefused(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _design(company_id: str, story_id: str, change) -> dict:
    from ..discovery import baseline
    from ..technical import store as tstore

    b = baseline.current_for_story(company_id, story_id)
    ad = change.architect_decision.model_dump(mode="json") if change and change.architect_decision else None
    spec = change.implementation_spec.model_dump(mode="json") if change and change.implementation_spec else None
    approvals = tstore.design_approvals(company_id, story_id)
    rev = b["design_revision"] if b else None
    approval = next((d for d in approvals if d["design_revision"] == rev), None)
    return {"architect_decision": ad, "implementation_spec": spec,
            "baseline": None if b is None else {
                "baseline_id": b["baseline_id"], "design_revision": b["design_revision"], "status": b["status"],
                "manifest_sha256": b["manifest_sha256"], "reassessment": b["reassessment"],
                "process_context": (b["manifest"].get("process_context") or {}).get("fingerprint")},
            "design_approval": None if approval is None else {k: approval[k] for k in
                                                              ("id", "design_revision", "approved_by", "approved_at")}}


def _technical(company_id: str, story_id: str) -> Optional[dict]:
    from ..technical import service as tservice

    view = tservice.work_view(company_id, story_id)
    if not view["packages"]:
        return None
    # The delivered revision: the newest one with an approval that got furthest.
    def progress(p):
        ap = p["approval"] or {}
        st = ap.get("milestone_states") or {}
        return (bool(ap.get("verification")), bool(ap.get("cnc_activation")), st.get("build") == "built",
                st.get("apply") == "applied", ap.get("status") == "approved", p["revision"])
    p = max(view["packages"], key=progress)
    c, ap = p["content"], p["approval"] or {}
    return {
        "mode": view["mode"], "simulation_label": view["simulation_label"], "format_label": view["format_label"],
        "revision": p["revision"], "content_sha256": p["content_sha256"],
        "revisions_total": len(view["packages"]),
        "repairs": [{"revision": q["revision"], "repair_of": q["content"].get("repair_of")}
                    for q in view["packages"] if q["content"].get("repair_of")],
        "objects": c.get("objects", []), "diff": c.get("diff", ""), "explanation": c.get("explanation", ""),
        "requirement_trace": c.get("requirement_trace", []), "test_plan": c.get("test_plan", []),
        "missing_evidence": c.get("missing_evidence", []), "unsupported": c.get("unsupported", []),
        "sources": [{k: s.get(k) for k in ("evidence_id", "sha256", "classification")} for s in c.get("sources", [])],
        "approval": {k: ap.get(k) for k in ("change_id", "status", "approved_by", "approved_at", "execution_mode")},
        "milestone_states": ap.get("milestone_states"), "milestones": ap.get("milestones") or [],
        "cnc_activation": ap.get("cnc_activation"), "verification": ap.get("verification"),
        "invalidations": ap.get("invalidations") or [],
        "human_actions": view["human_actions"],
    }


def _functional(change) -> Optional[dict]:
    if not change or not change.exact_change:
        return None
    ec = change.exact_change
    return {"exact_change": ec.model_dump(mode="json"),
            "approval": change.change_approval.model_dump(mode="json") if change.change_approval else None}


def gather(company_id: str, story_id: str, change) -> dict:
    """Everything the record is generated from, read now."""
    route = (change.architect_decision.recommended_route if change and change.architect_decision else None)
    tech = _technical(company_id, story_id) if route in TECHNICAL_ROUTES else None
    func = _functional(change) if route not in TECHNICAL_ROUTES else None
    us = change.user_story.model_dump(mode="json") if change and change.user_story else None
    return {
        "story": {"story_id": story_id, "title": change.title if change else story_id,
                  "business_domain_id": change.business_domain_id if change else None,
                  "state": change.state if change else None, "user_story": us,
                  "approved": bool(change and change.state not in ("RECEIVED", "REFINING", "BACKLOG_READY", "REJECTED"))},
        "process": {"mapping": story_process.mapping_view(company_id, story_id),
                    "maps": {k: maps.latest_version(company_id, story_id, k) for k in maps.KINDS}},
        "design": _design(company_id, story_id, change), "route": route,
        "implementation": {"technical": tech, "functional": func},
    }


def _checkpoints(src: dict) -> list[dict]:
    m = src["process"]["mapping"]
    to_be = src["process"]["maps"]["to_be"]
    d = src["design"]
    b = d["baseline"]
    cps = [
        ("story_approved", "Story approved", src["story"]["approved"], src["story"]["state"] or ""),
        ("process_decided", "Processes confirmed, or no-mapping recorded, by a reviewer", m is not None,
         f"mapping revision {m['revision']} ({m['status']}) by {m['reviewer_name']}" if m else "no reviewer decision"),
        ("to_be_map", "To-be process map recorded", to_be is not None or (m is not None and m["status"] == "no_mapping"),
         f"version {to_be['version']}" if to_be else "none (no mapping applies)" if m and m["status"] == "no_mapping" else "none"),
        ("design_approved", "Architect design approved", d["design_approval"] is not None,
         f"design revision {d['design_approval']['design_revision']} by {d['design_approval']['approved_by']}"
         if d["design_approval"] else "no approval of the current design"),
        ("design_current", "Design not awaiting reassessment", bool(b) and b["status"] == "current",
         (b["status"].replace("_", " ") + (f": {b['reassessment'][-1]['detail']}" if b["reassessment"] else "")) if b else "no design baseline"),
    ]
    tech, func = src["implementation"]["technical"], src["implementation"]["functional"]
    if src["route"] in TECHNICAL_ROUTES:
        st = (tech or {}).get("milestone_states") or {}
        v = (tech or {}).get("verification") or {}
        cps += [
            ("implementation_approved", "Exact implementation package approved",
             bool(tech) and tech["approval"]["status"] == "approved",
             f"package revision {tech['revision']}: {tech['approval']['status']}" if tech else "no package"),
            ("applied", "Applied (checked in)", st.get("apply") == "applied", st.get("apply") or "not started"),
            ("built", "Built", st.get("build") == "built", st.get("build") or "not started"),
            ("cnc_activation", "Human CNC activation recorded", bool(tech and tech["cnc_activation"]),
             (tech["cnc_activation"]["package_name"] + (" (simulated)" if tech["cnc_activation"].get("simulated") else ""))
             if tech and tech["cnc_activation"] else "not recorded"),
            ("verified", "Verification passed against the active runtime",
             bool(v) and v.get("passed") and v.get("runtime_is_approved_artifact"),
             f"{sum(1 for r in v.get('results', []) if r['passed'])}/{len(v.get('results', []))} tests" if v else "not run"),
        ]
    else:
        ex = ((func or {}).get("exact_change") or {}).get("execution") or {}
        cps += [
            ("implementation_approved", "Exact change approved", bool(func and func["approval"]),
             "approved" if func and func["approval"] else "no approved exact change"),
            ("applied", "Change applied", ex.get("write_state") == "applied", ex.get("write_state") or "not started"),
            ("verified", "Test verified", ex.get("test_state") in ("passed", "verified"), ex.get("test_state") or "not run"),
        ]
    return [{"id": i, "label": label, "complete": bool(ok), "detail": detail} for i, label, ok, detail in cps]


def _deviations_and_limits(src: dict) -> tuple[list[str], list[str]]:
    dev, lim = [], []
    d = src["design"]
    tech = src["implementation"]["technical"]
    planned = set((d["architect_decision"] or {}).get("objects_affected") or [])
    if tech:
        built = {o["object_name"] for o in tech["objects"]}
        for o in sorted(built - planned):
            dev.append(f"Object {o} was changed but is not among the design's affected objects.")
        for o in sorted(planned - built):
            dev.append(f"The design names {o}, but the delivered package does not change it.")
        for r in tech["repairs"]:
            ro = r["repair_of"] or {}
            dev.append(f"Package revision {r['revision']} replaced revision {ro.get('revision')}: {ro.get('reason', '')}".strip())
        for x in tech["missing_evidence"]:
            lim.append(f"Missing evidence (Technical Agent): {x}")
        for x in tech["unsupported"]:
            lim.append(f"Not supported: {x}")
        for inv in tech["invalidations"]:
            lim.append(f"Approval invalidation recorded: {inv.get('kind')} -- {inv.get('detail')}")
        if tech["mode"] == "simulation":
            lim.append("Delivery and verification ran in the simulated DEV estate with a synthetic source format; "
                       "nothing was built or tested in a JD Edwards system.")
    b = d["baseline"]
    if b and b["reassessment"]:
        for r in b["reassessment"]:
            lim.append(f"Design flagged for reassessment ({r.get('kind')}): {r.get('detail')}")
    if b and not b.get("process_context"):
        lim.append("The design's evidence baseline records no process context (designed before processes were confirmed).")
    m = src["process"]["mapping"]
    if m:
        for r in m["refs"]:
            if r["status_now"]["state"] in ("changed", "removed", "framework_inactive"):
                lim.append(f"Mapped process {r['node_key']} ({r['framework_id']} v{r['version']}): {r['status_now']['detail']}")
    for kind, v in src["process"]["maps"].items():
        if v:
            assumed = [s["label"] for s in v["content"]["steps"] if s["basis"] == "assumption"]
            if assumed:
                lim.append(f"{kind.replace('_', '-')} map v{v['version']}: {len(assumed)} step(s) are assumptions, not "
                           f"confirmed customer practice: {', '.join(assumed[:6])}{'...' if len(assumed) > 6 else ''}")
    us = src["story"]["user_story"] or {}
    for q in us.get("open_questions") or []:
        lim.append(f"Open question from refinement: {q}")
    return dev, lim


def _inputs_sha(src: dict) -> str:
    return hashlib.sha256(json.dumps(src, sort_keys=True, default=str).encode()).hexdigest()


def build(company_id: str, story_id: str, change) -> dict:
    src = gather(company_id, story_id, change)
    checkpoints = _checkpoints(src)
    dev, lim = _deviations_and_limits(src)
    tech = src["implementation"]["technical"]
    if tech:
        simulated = tech["mode"] == "simulation"
    else:
        from jde_mcp_server.config import settings as mcp_settings

        simulated = bool(mcp_settings.mock_mode)
    content = {"story": src["story"], "process": src["process"], "design": src["design"], "route": src["route"],
               "implementation": src["implementation"], "checkpoints": checkpoints,
               "all_checkpoints_complete": all(c["complete"] for c in checkpoints),
               "deviations": dev, "limitations": lim,
               "delivery_mode": "simulation" if simulated else "live",
               "simulated_notice": SIMULATED_NOTICE if simulated else None}
    return {"content": content, "inputs_sha256": _inputs_sha(src)}


# ---------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------
def markdown(record: dict) -> str:
    c = record["content"]
    s = c["story"]
    us = s.get("user_story") or {}
    L: list[str] = [f"# As-built record: {s['story_id']} -- {s['title']}", ""]
    L.append(f"Version {record['version']} · status **{record['status'].upper()}** · generated {record['generated_at']} "
             f"by {record['generated_by']}" + (f" · finalised {record['finalised_at']} by {record['finalised_by']}"
                                               if record.get("finalised_at") else ""))
    L.append("")
    if c.get("simulated_notice"):
        L += [f"> **{c['simulated_notice']}**", ""]
    L += ["## Delivery checkpoints", ""]
    for cp in c["checkpoints"]:
        L.append(f"- [{'x' if cp['complete'] else ' '}] {cp['label']} -- {cp['detail']}")
    L += ["", "## Story", "", us.get("statement") or "(no refined statement)", ""]
    if us.get("acceptance_criteria"):
        L += ["Acceptance criteria:", ""] + [f"- {a['id']}: {a['text']}" for a in us["acceptance_criteria"]] + [""]
    m = c["process"]["mapping"]
    L += ["## Processes", ""]
    if not m:
        L.append("No reviewer decision recorded.")
    elif m["status"] == "no_mapping":
        L.append(f"No process mapping applies (revision {m['revision']}, {m['reviewer_name']}): {m['no_mapping_reason']}")
    else:
        L.append(f"Confirmed by {m['reviewer_name']} (mapping revision {m['revision']}, {m['created_at']}):")
        L.append("")
        for r in m["refs"]:
            path = " > ".join(p["name"] for p in r["path"])
            L.append(f"- `{r['framework_id']}@v{r['version']}:{r['node_key']}` {path} -- now: {r['status_now']['state']}")
        acc = m.get("findings") or {}
        for key, title in (("accepted_requirements", "Requirements added"), ("accepted_controls", "Controls added"),
                           ("accepted_acceptance_criteria", "Acceptance criteria added")):
            if acc.get(key):
                L += ["", f"{title} at finalisation:", ""] + [f"- {x}" for x in acc[key]]
    for kind, v in c["process"]["maps"].items():
        L += ["", f"## {kind.replace('_', '-').capitalize()} process map" + (f" (version {v['version']})" if v else ""), ""]
        if not v:
            L.append("None recorded.")
            continue
        L += ["```mermaid", maps.mermaid(v["content"]), "```", "",
              "| Step | Actor | System | Controls | Process | Basis |", "|---|---|---|---|---|---|"]
        for st in v["content"]["steps"]:
            basis = "confirmed: " + st["confirmation_source"] if st["basis"] == "confirmed" else "ASSUMPTION"
            L.append(f"| {st['id']} {st['label']} | {st['actor']} | {st['system']} | {'; '.join(st['controls'])} | "
                     f"{(st.get('node_ref') or {}).get('node_key', '')} | {basis} |")
    d = c["design"]
    L += ["", "## Design", ""]
    if d["architect_decision"]:
        ad = d["architect_decision"]
        L.append(f"Route: **{ad.get('recommended_route')}**. Existing functionality: {ad.get('existing_functionality_found', '')}")
        L.append(f"Objects affected: {', '.join(ad.get('objects_affected') or []) or 'none'}")
    if d["baseline"]:
        L.append(f"Evidence baseline {d['baseline']['baseline_id']} (sha256 {d['baseline']['manifest_sha256'][:16]}...), "
                 f"status {d['baseline']['status']}")
    if d["design_approval"]:
        L.append(f"Design approved by {d['design_approval']['approved_by']} at {d['design_approval']['approved_at']}")
    t = c["implementation"]["technical"]
    f = c["implementation"]["functional"]
    L += ["", "## Implementation", ""]
    if t:
        L.append(f"Package revision {t['revision']} (sha256 {t['content_sha256'][:16]}...), approved by "
                 f"{t['approval']['approved_by']}; mode **{t['mode']}**. {t['format_label']}")
        L.append("")
        L.append("Objects: " + ", ".join(f"{o['object_name']} ({o['object_type']})" for o in t["objects"]))
        L += ["", "```diff", t["diff"].rstrip(), "```", ""]
        for ms in t["milestones"]:
            L.append(f"- {ms.get('milestone')} at {ms.get('at')}" + (f" by {ms['actor']}" if ms.get("actor") else ""))
        if t["cnc_activation"]:
            ca = t["cnc_activation"]
            L.append(f"- CNC activation of {ca['package_name']} by {ca['by']} ({ca['evidence_reference']})"
                     + (" -- SIMULATED" if ca.get("simulated") else ""))
    elif f:
        ec = f["exact_change"]
        L.append(f"Exact change: {ec.get('tool')} {ec.get('application')}/{ec.get('version')} option {ec.get('option')}: "
                 f"{ec.get('current_value')} -> {ec.get('proposed_value')}")
    else:
        L.append("No implementation recorded.")
    L += ["", "## Verification", ""]
    v = (t or {}).get("verification")
    if v:
        L += ["| Test | Kind | Result |", "|---|---|---|"] + [
            f"| {r['name']} | {r['kind']} | {'passed' if r['passed'] else 'FAILED'} |" for r in v["results"]]
        L.append("")
        L.append("Active runtime is the approved artifact: " + ("yes" if v.get("runtime_is_approved_artifact") else "NO"))
    else:
        L.append("No verification evidence recorded.")
    L += ["", "## Deviations from the design", ""] + ([f"- {x}" for x in c["deviations"]] or ["None found."])
    L += ["", "## Unresolved limitations", ""] + ([f"- {x}" for x in c["limitations"]] or ["None recorded."])
    L += ["", f"_Record sha256 {record['content_sha256']}_", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------
def _row(r) -> dict:
    d = dict(r)
    d["content"] = json.loads(d["content"])
    return d


def records(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        return [_row(r) for r in conn.execute(
            "SELECT * FROM as_built_records WHERE company_id = ? AND story_id = ? ORDER BY version DESC",
            (company_id, story_id)).fetchall()]


def get(company_id: str, story_id: str, version: int) -> Optional[dict]:
    with connection() as conn:
        r = conn.execute("SELECT * FROM as_built_records WHERE company_id = ? AND story_id = ? AND version = ?",
                         (company_id, story_id, version)).fetchone()
    return _row(r) if r else None


def generate(company_id: str, story_id: str, change, *, actor: str) -> dict:
    built = build(company_id, story_id, change)
    content = built["content"]
    blob = json.dumps(content, sort_keys=True, default=str)
    sha = hashlib.sha256(blob.encode()).hexdigest()
    now = _now()
    with connection(immediate=True) as conn:
        version = (conn.execute("SELECT MAX(version) AS v FROM as_built_records WHERE company_id = ? AND story_id = ?",
                                (company_id, story_id)).fetchone()["v"] or 0) + 1
        conn.execute("UPDATE as_built_records SET status = 'superseded' WHERE company_id = ? AND story_id = ? "
                     "AND status = 'draft'", (company_id, story_id))
        record = {"company_id": company_id, "story_id": story_id, "version": version, "status": "draft",
                  "delivery_mode": content["delivery_mode"], "inputs_sha256": built["inputs_sha256"],
                  "content": content, "content_sha256": sha, "generated_by": actor, "generated_at": now,
                  "finalised_by": None, "finalised_at": None}
        md = markdown(record)
        conn.execute("INSERT INTO as_built_records (company_id, story_id, version, status, delivery_mode, inputs_sha256, "
                     "content, markdown, content_sha256, generated_by, generated_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
                     (company_id, story_id, version, content["delivery_mode"], built["inputs_sha256"], blob, md, sha,
                      actor, now))
    return get(company_id, story_id, version)  # type: ignore[return-value]


def finalise(company_id: str, story_id: str, version: int, change, *, actor: str) -> dict:
    rec = get(company_id, story_id, version)
    if rec is None:
        raise LookupError(f"no as-built version {version}")
    if rec["status"] != "draft":
        raise AsBuiltRefused(f"version {version} is {rec['status']}; only the current draft can be finalised")
    now_sha = build(company_id, story_id, change)["inputs_sha256"]
    if now_sha != rec["inputs_sha256"]:
        raise AsBuiltRefused("the story, processes, design, implementation or evidence changed since this draft was "
                             "generated; generate a new version and review it")
    missing = [c["label"] for c in rec["content"]["checkpoints"] if not c["complete"]]
    if missing:
        raise AsBuiltRefused("required delivery checkpoints are not complete: " + "; ".join(missing))
    now = _now()
    with connection(immediate=True) as conn:
        cur = conn.execute("UPDATE as_built_records SET status = 'final', finalised_by = ?, finalised_at = ? "
                           "WHERE company_id = ? AND story_id = ? AND version = ? AND status = 'draft'",
                           (actor, now, company_id, story_id, version))
        if cur.rowcount != 1:
            raise AsBuiltRefused("the draft changed meanwhile; reload")
        rec = {**rec, "status": "final", "finalised_by": actor, "finalised_at": now}
        conn.execute("UPDATE as_built_records SET markdown = ? WHERE company_id = ? AND story_id = ? AND version = ?",
                     (markdown(rec), company_id, story_id, version))
    return get(company_id, story_id, version)  # type: ignore[return-value]
