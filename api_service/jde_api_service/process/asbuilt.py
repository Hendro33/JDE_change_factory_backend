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
        "mode": view["mode"],
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


def _functional(company_id: str, story_id: str, change) -> Optional[dict]:
    """The exact change as recorded by the gate: target, before and approved
    values, the approval and its binding, every recorded step and test, and
    how the applied value was verified (live read-back or stated in JDE)."""
    from ..services.change_service import _latest_change_record_for

    if not change or not change.exact_change:
        return None
    record = _latest_change_record_for(story_id) or {}
    op = record.get("operation") or {}
    ex = record.get("execution") or {}
    attempts = {k: [{a_k: a.get(a_k) for a_k in ("attempt_id", "outcome", "detail", "before_value", "started_at",
                                                 "finished_at", "actor")} for a in (ex.get(k) or {}).get("attempts", [])]
                for k in ("write", "test")}
    applied = next((a.get("recorded") for a in reversed((ex.get("write") or {}).get("attempts", []))
                    if a.get("outcome") == "applied" and a.get("recorded")), None)
    readback = None
    if applied:
        readback = {"value": applied.get("observed_value"),
                    "matches_approved": str(applied.get("observed_value")) == str(op.get("value")),
                    "source": applied.get("source", ""), "live": str(applied.get("source", "")).startswith("live"),
                    "evidence_reference": applied.get("evidence_reference", ""), "by": applied.get("by")}
    verification = {k: v for k, v in (record.get("verification") or {}).items() if k != "answer"}
    binding = record.get("binding") or {}
    items = _items_as_built(record)
    if op.get("tool") == "configuration_change_set":
        recorded = [i for i in items if i["readback"]]
        readback = None
        if recorded:
            readback = {"value": f"{len(recorded)}/{len(items)} items recorded",
                        "matches_approved": len(recorded) == len(items) and all(
                            i["readback"]["matches_approved"] for i in items),
                        "source": "per item, below", "live": all(i["readback"]["live"] for i in recorded),
                        "evidence_reference": "; ".join(i["readback"]["evidence_reference"] for i in recorded
                                                        if i["readback"]["evidence_reference"]),
                        "by": ", ".join(sorted({str(i["readback"]["by"]) for i in recorded}))}
    return {"exact_change": change.exact_change.model_dump(mode="json"),
            "change_id": record.get("change_id"), "capability_id": record.get("capability_id"),
            "environment": record.get("environment"), "operation": op,
            "approval": change.change_approval.model_dump(mode="json") if change.change_approval else None,
            "approver_authority": record.get("approver_authority"),
            "binding": {"design": binding.get("design"), "before_state": binding.get("before_state")},
            "invalidations": record.get("invalidations") or [],
            "attempts": attempts, "readback": readback, "verification": verification, "items": items,
            "summary": op.get("summary") or "",
            "test_orchestration": op.get("test_orchestration") or "",
            "test_note": ("The approved test orchestration ran live on the customer's AIS."
                          if verification.get("source") == "live orchestration" else
                          f"The test was run in DEV and its result recorded by {verification.get('by')} "
                          f"(evidence: {verification.get('evidence_reference')})." if verification else "No test recorded.")}


def _items_as_built(record: dict) -> list[dict]:
    """Every configuration item: what it changes, its value before (read at
    approval), the approved value, and what was read back when a person
    recorded it applied -- live, or stated with evidence when AIS cannot
    read it."""
    from jde_mcp_server import config_items

    try:
        items = config_items.items_of(record)
    except Exception:  # noqa: BLE001 -- an unreadable record is shown without items, never hidden
        return []
    before = ((record.get("binding") or {}).get("before_state") or {}).get("items") or {}
    delivered = record.get("item_delivery") or {}
    out = []
    for it in items:
        approved = config_items.approved_values(it)
        d = delivered.get(it["id"])
        rb = None
        if d:
            observed = d.get("observed")
            rb = {"value": observed, "source": d.get("source", ""), "live": bool(d.get("live")),
                  "evidence_reference": d.get("evidence_reference", ""), "by": d.get("by"), "at": d.get("at"),
                  "matches_approved": _item_matches(it, approved, observed)}
        b = before.get(it["id"]) or {}
        out.append({"id": it["id"], "capability_id": it.get("capability_id"), "kind": it["kind"],
                    "label": config_items.label(it), "purpose": it.get("purpose", ""),
                    "before": b.get("value") if b.get("known", True) else None,
                    "before_note": "" if b.get("known", True) else b.get("reason", ""),
                    "approved": approved, "readback": rb})
    return out


def _item_matches(it: dict, approved, observed) -> bool:
    if it["kind"] == "processing_option":
        return str(observed) == str(approved)
    if isinstance(approved, dict) and "values" in approved:
        vals = (observed or {}).get("values") if isinstance(observed, dict) else None
        return bool(vals is not None and all(str(vals.get(k, "")).strip() == str(v).strip()
                                             for k, v in approved["values"].items()))
    return isinstance(observed, dict) and observed.get("specification") == approved.get("specification")


def _story_revision(company_id: str, story_id: str) -> Optional[dict]:
    from . import refinement

    revs = refinement.revisions(company_id, story_id)
    if not revs:
        return None
    r = revs[0]
    return {k: r[k] for k in ("revision", "author_name", "created_at", "applied_findings", "process_refs", "story_sha256")}


def gather(company_id: str, story_id: str, change) -> dict:
    """Everything the record is generated from, read now."""
    route = (change.architect_decision.recommended_route if change and change.architect_decision else None)
    tech = _technical(company_id, story_id) if route in TECHNICAL_ROUTES else None
    func = _functional(company_id, story_id, change) if route not in TECHNICAL_ROUTES else None
    us = change.user_story.model_dump(mode="json") if change and change.user_story else None
    return {
        "story": {"story_id": story_id, "title": change.title if change else story_id,
                  "business_domain_id": change.business_domain_id if change else None,
                  "state": change.state if change else None, "user_story": us,
                  "approved": bool(change and change.state not in ("RECEIVED", "REFINING", "BACKLOG_READY", "REJECTED"))},
        "process": {"mapping": story_process.mapping_view(company_id, story_id),
                    "maps": {k: maps.latest_version(company_id, story_id, k) for k in maps.KINDS},
                    # Without an active process framework there is nothing to map
                    # against (the story lifecycle treats the decision as not applicable).
                    "framework_active": _framework_active(company_id)},
        "story_revision": _story_revision(company_id, story_id),
        "design": _design(company_id, story_id, change), "route": route,
        "implementation": {"technical": tech, "functional": func},
    }


def _design_approved(src: dict) -> tuple:
    d = src["design"]
    if src["route"] in TECHNICAL_ROUTES:
        a = d["design_approval"]
        return ("design_approved", "Architect design approved", a is not None,
                f"design revision {a['design_revision']} by {a['approved_by']}" if a else "no approval of the current design")
    # Functional route: a person approves the design together with its exact
    # change in Architecture Review, bound to the design baseline.
    f = src["implementation"]["functional"] or {}
    bound = ((f.get("binding") or {}).get("design") or {})
    b = d["baseline"]
    ok = bool(f.get("approval")) and bool(b) and bound.get("design_revision") == b["design_revision"]
    return ("design_approved", "Architect design approved (with its exact change, in Architecture Review)", ok,
            f"approved by {f['approval'].get('approved_by')} against baseline {bound.get('baseline_id')}"
            if f.get("approval") else "the exact change has not been approved")


def _framework_active(company_id: str) -> bool:
    from . import framework

    return framework.selected_active(company_id) is not None


_NO_FRAMEWORK = "not applicable: no process framework is active for this customer"


def _checkpoints(src: dict) -> list[dict]:
    m = src["process"]["mapping"]
    to_be = src["process"]["maps"]["to_be"]
    # Records made before this field existed were made with a framework.
    no_framework = m is None and src["process"].get("framework_active", True) is False
    d = src["design"]
    b = d["baseline"]
    cps = [
        ("story_approved", "Story approved", src["story"]["approved"], src["story"]["state"] or ""),
        ("process_decided", "Processes confirmed, or no-mapping recorded, by a reviewer", m is not None or no_framework,
         f"mapping revision {m['revision']} ({m['status']}) by {m['reviewer_name']}" if m
         else _NO_FRAMEWORK if no_framework else "no reviewer decision"),
        ("to_be_map", "To-be process map recorded",
         to_be is not None or (m is not None and m["status"] == "no_mapping") or (no_framework and to_be is None),
         f"version {to_be['version']}" if to_be else "none (no mapping applies)" if m and m["status"] == "no_mapping"
         else _NO_FRAMEWORK if no_framework else "none"),
        _design_approved(src),
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
             tech["cnc_activation"]["package_name"] if tech and tech["cnc_activation"] else "not recorded"),
            ("verified", "Verification passed against the active runtime",
             bool(v) and v.get("passed") and v.get("runtime_is_approved_artifact"),
             f"{sum(1 for r in v.get('results', []) if r['passed'])}/{len(v.get('results', []))} tests" if v else "not run"),
        ]
    else:
        ex = ((func or {}).get("exact_change") or {}).get("execution") or {}
        rb = (func or {}).get("readback")
        cps += [
            ("implementation_approved", "Exact change approved", bool(func and func["approval"]),
             "approved" if func and func["approval"] else "no approved exact change"),
            ("applied", "Change applied", ex.get("write_state") == "applied", ex.get("write_state") or "not started"),
            ("tested", "Test passed in DEV", ex.get("test_state") == "completed"
             and ((func or {}).get("verification") or {}).get("passed") is True,
             (func or {}).get("test_note") or ex.get("test_state") or "not run"),
            ("verified", "Applied value is the approved value", bool(rb) and rb["matches_approved"],
             (f"{rb['value']} ({rb['source']})" if rb and (func or {}).get("items") and
              func["operation"].get("tool") == "configuration_change_set" else
              f"{rb['value']!r} ({rb['source']})" if rb else "not recorded")),
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
        lim.append("Check-in, build, CNC activation and test results were recorded by people; Jade cannot read "
                   "an object's active runtime, so that the active DEV runtime is the approved package is as stated "
                   "by the person who verified it.")
    f = src["implementation"]["functional"]
    if f:
        for inv in f["invalidations"]:
            lim.append(f"Approval invalidation recorded: {inv.get('kind')} -- {inv.get('detail')}")
        if f.get("items") and f["operation"].get("tool") == "configuration_change_set":
            for i in f["items"]:
                rb = i["readback"]
                if rb and not rb["live"]:
                    lim.append(f"{i['id']} ({i['label']}) could not be read back live; it is as stated by "
                               f"{rb['by']} (evidence: {rb['evidence_reference']}).")
                elif rb and not rb["matches_approved"]:
                    dev.append(f"{i['id']} ({i['label']}) was read back as {rb['value']!r}, not the approved value.")
        elif f["readback"] and not f["readback"].get("live"):
            lim.append("The applied value could not be read back live; it is as stated by "
                       f"{f['readback'].get('by')} (evidence: {f['readback'].get('evidence_reference')}).")
    b = d["baseline"]
    if b and b["reassessment"]:
        for r in b["reassessment"]:
            lim.append(f"Design flagged for reassessment ({r.get('kind')}): {r.get('detail')}")
    if b and not b.get("process_context"):
        lim.append("The design's evidence baseline records no process context (designed before processes were confirmed).")
    m = src["process"]["mapping"]
    if m is None and src["process"].get("framework_active", True) is False:
        lim.append("No process framework was active for this customer, so the story's business processes were not "
                   "mapped.")
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
    content = {"story": src["story"], "story_revision": src["story_revision"], "process": src["process"], "design": src["design"], "route": src["route"],
               "implementation": src["implementation"], "checkpoints": checkpoints,
               "all_checkpoints_complete": all(c["complete"] for c in checkpoints),
               "deviations": dev, "limitations": lim,
               "delivery_mode": "recorded"}
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
    L += ["## Delivery checkpoints", ""]
    for cp in c["checkpoints"]:
        L.append(f"- [{'x' if cp['complete'] else ' '}] {cp['label']} -- {cp['detail']}")
    L += ["", "## Story", "", us.get("statement") or "(no refined statement)", ""]
    sr = c.get("story_revision")
    if sr:
        L.append(f"Story revision {sr['revision']} by {sr['author_name']} ({sr['created_at']}), applying "
                 f"{len(sr['applied_findings'])} finding(s); process mapping revision {sr['process_refs'].get('mapping_revision')}.")
        L.append("")
    if us.get("business_rules"):
        L += ["Requirements and controls:", ""] + [f"- {r}" for r in us["business_rules"]] + [""]
    if us.get("acceptance_criteria"):
        L += ["Acceptance criteria:", ""] + [f"- {a['id']}: {a['text']}" for a in us["acceptance_criteria"]] + [""]
    m = c["process"]["mapping"]
    L += ["## Processes", ""]
    if not m:
        L.append("No process framework was active; processes were not mapped." if c["process"].get("framework_active") is False
                 else "No reviewer decision recorded.")
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
                 f"{t['approval']['approved_by']}; delivery **{t['mode']}** (each step recorded by a person).")
        L.append("")
        L.append("Objects: " + ", ".join(f"{o['object_name']} ({o['object_type']})" for o in t["objects"]))
        L += ["", "```diff", t["diff"].rstrip(), "```", ""]
        for ms in t["milestones"]:
            L.append(f"- {ms.get('milestone')} at {ms.get('at')}" + (f" by {ms['actor']}" if ms.get("actor") else ""))
        if t["cnc_activation"]:
            ca = t["cnc_activation"]
            L.append(f"- CNC activation of {ca['package_name']} by {ca['by']} ({ca['evidence_reference']})")
    elif f:
        op, bs = f["operation"], (f["binding"] or {}).get("before_state") or {}
        if op.get("tool") == "configuration_change_set":
            L.append(f"Configuration change set {f['change_id']}, environment {f['environment']}: "
                     f"{f.get('summary') or ''}".rstrip(": "))
            L += ["", "| Item | Change | Before | Read back after |", "|---|---|---|---|"]
            for i in f["items"]:
                rb = i["readback"]
                after = "not recorded" if not rb else (
                    f"{_short(rb['value'])} ({'live' if rb['live'] else 'stated, ' + str(rb['evidence_reference'])})"
                    + ("" if rb["matches_approved"] else " -- DOES NOT match"))
                before = _short(i["before"]) if i["before"] is not None else (i["before_note"] or "unknown")
                L.append(f"| {i['id']} | {i['label']} | {before} | {after} |")
            L.append("")
        else:
            L.append(f"Exact change {f['change_id']} ({f['capability_id']}), environment {f['environment']}: "
                     f"`{op.get('tool')}` {op.get('application')}/{op.get('version')} option {op.get('option')}: "
                     f"{bs.get('value')!r} -> {op.get('value')!r}")
        ap = f["approval"] or {}
        L.append(f"Approved by {ap.get('approved_by')} at {ap.get('approved_at')} "
                 f"(roles: {', '.join((f.get('approver_authority') or {}).get('roles') or [])})")
        L.append("")
        for kind in ("write", "test"):
            for a in f["attempts"][kind]:
                L.append(f"- {kind} attempt {a.get('attempt_id')}: {a.get('outcome')} -- {a.get('detail')}")
    else:
        L.append("No implementation recorded.")
    L += ["", "## Verification", ""]
    v = (t or {}).get("verification")
    if f and not t:
        rb = f.get("readback") or {}
        fv = f.get("verification") or {}
        L.append(f"Test {f['test_orchestration'] or '(recorded manually)'}: "
                 f"{'passed' if fv.get('passed') else 'FAILED' if fv else f['exact_change']['execution']['test_state']}. "
                 f"{f['test_note']}")
        L.append("")
        if f.get("items") and f["operation"].get("tool") == "configuration_change_set":
            L.append(f"Applied configuration: {rb.get('value') or 'nothing recorded'} -- "
                     f"{'every item matches' if rb.get('matches_approved') else 'NOT every item matches'} the approved "
                     "values (see the items above)")
        else:
            L.append(f"Applied value: {rb.get('value')!r} -- {'matches' if rb.get('matches_approved') else 'DOES NOT match'} "
                     f"the approved value ({rb.get('source')})")
    elif v:
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


def _short(value) -> str:
    if isinstance(value, dict):
        if "values" in value:
            return ("new row" if not value.get("exists") and not value["values"] else
                    ", ".join(f"{k}={v!r}" for k, v in value["values"].items()) or "exists")
        if "specification" in value:
            return str(value["specification"])
    return repr(value)
