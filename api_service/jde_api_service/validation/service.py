"""Validation domain. All transitions are transactional and tenant scoped.

The existing document store is backed by SQLite/PostgreSQL with a cross-process
write lock. Approved versions and attempts are append-only; current coverage is
a projection, never a mutable 'passed' flag on a story.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from ..config import settings
from ..dependencies import AuthContext, require_current_role
from ..persistence import blob_store
from ..persistence.db import connection
from ..persistence.json_file_store import JsonFileStore
from ..services import credential_crypto, membership_service
from ..services.registry import get_change_service
from . import models as m

READ_ROLES = ("test_manager", "product_manager", "admin")
ACTIVE = {"queued", "preflight", "running", "stop_requested", "awaiting_input"}


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def store(kind):
    return JsonFileStore(os.path.join(settings.data_dir, "validation_" + kind))


def rows(kind, company):
    return store(kind).list_all(company_id=company)


def get(kind, key, company):
    value = store(kind).get(key)
    if not value or value.get("company_id") != company:
        raise HTTPException(404, "Validation record not found")
    return value


def check_revision(record, revision):
    if record.get("revision", 0) != revision:
        raise HTTPException(409, "This record changed. Reload before saving")


def audit(company, actor, action, target, detail=""):
    key = uid("audit")
    store("audit").put(key, {"id": key, "company_id": company, "actor": actor, "action": action,
                              "target": target, "detail": detail[:4000], "at": now()})


def policy(company):
    return store("policy").get(company) or {"company_id": company, **m.Policy().model_dump()}


def save_policy(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "admin")
        old = policy(ctx.customer_id)
        check_revision(old, payload.revision)
        result = {**payload.model_dump(), "company_id": ctx.customer_id, "revision": payload.revision + 1}
        store("policy").put(ctx.customer_id, result)
        audit(ctx.customer_id, ctx.identity.id, "policy_saved", ctx.customer_id)
        return result


def public_environment(record):
    return {k: v for k, v in record.items() if k != "credential"}


def save_environment(ctx, payload, environment_id=None):
    with connection(immediate=True):
        require_current_role(ctx, "admin")
        old = get("environments", environment_id, ctx.customer_id) if environment_id else {"revision": 0}
        check_revision(old, payload.revision)
        key = environment_id or uid("env")
        cert = payload.certificate_sha
        if payload.ca_pem:
            from ..discovery import certificates
            try:
                cert = certificates.store(ctx.customer_id, payload.ca_pem, ctx.identity.id)["sha256"]
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        credential = old.get("credential", "")
        if payload.password:
            try:
                credential = credential_crypto.encrypt(payload.password)
            except credential_crypto.CredentialKeyMissing:
                raise HTTPException(409, "The server credential encryption key must be configured before saving accounts")
        elif old.get("username") != payload.username and credential:
            raise HTTPException(422, "Enter the password when changing the test account")
        result = {**payload.model_dump(), "id": key, "company_id": ctx.customer_id,
                  "revision": payload.revision + 1, "credential": credential,
                  "credential_set": bool(credential), "certificate_sha": cert,
                  "updated_at": now(), "updated_by": ctx.identity.id, "connection_test": None}
        store("environments").put(key, result)
        audit(ctx.customer_id, ctx.identity.id, "environment_saved", key)
        return public_environment(result)


def source(company, story_id):
    change = get_change_service().get_for_customer(story_id, company)
    if change is None or change.user_story is None:
        raise HTTPException(404, "A source user story is not available in this customer")
    raw = change.model_dump(mode="json")
    material = {k: raw.get(k) for k in ("user_story", "architect_decision", "implementation_spec", "exact_change",
                                        "business_domain_id", "change_id", "story_approval")}
    return {"id": story_id, "title": change.user_story.statement, "hash": digest(material), "material": material}


def sources(company, ids):
    return [source(company, key) for key in dict.fromkeys(ids)]


def current_source_hash(company, ids):
    return digest(sources(company, ids))


def version(record, number=None):
    number = number or len(record["versions"])
    found = next((v for v in record["versions"] if v["version"] == number), None)
    if found is None:
        raise HTTPException(404, "Version not found")
    return found


def save_scenario(ctx, payload, key=None):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        old = get("scenarios", key, ctx.customer_id) if key else {
            "id": uid("scenario"), "company_id": ctx.customer_id, "revision": 0, "versions": []}
        check_revision(old, payload.revision)
        if payload.story_id:
            source(ctx.customer_id, payload.story_id)
        body = payload.model_dump(exclude={"revision"})
        old["versions"].append({"version": len(old["versions"]) + 1, "status": "draft", "body": body,
                                "hash": digest(body), "created_by": ctx.identity.id, "created_at": now()})
        old["revision"] += 1
        old["retired"] = False
        store("scenarios").put(old["id"], old)
        audit(ctx.customer_id, ctx.identity.id, "scenario_version_created", old["id"])
        return old


def approve(ctx, kind, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        record = get(kind, key, ctx.customer_id)
        check_revision(record, payload.revision)
        v = version(record)
        if v["status"] != "draft":
            raise HTTPException(409, "Only a draft version can be approved")
        if policy(ctx.customer_id)["require_independent_review"] and v["created_by"] == ctx.identity.id:
            raise HTTPException(403, "A different Test Manager must approve this draft")
        if kind == "plans":
            validate_plan(ctx.customer_id, m.Plan(**v["body"]))
            if v["source_hash"] != current_source_hash(ctx.customer_id, v["body"]["story_ids"]):
                raise HTTPException(409, "Source changed. Save and review a new plan version before approval")
        v.update(status="approved", approved_by=ctx.identity.id, approved_at=now(), approval_note=payload.note)
        record["revision"] += 1
        store(kind).put(key, record)
        audit(ctx.customer_id, ctx.identity.id, kind + "_approved", key, payload.note)
        return record


def retire_scenario(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        record = get("scenarios", key, ctx.customer_id)
        check_revision(record, payload.revision)
        if not payload.note:
            raise HTTPException(422, "Give the reason for retirement")
        record.update(retired=True, revision=record["revision"] + 1)
        store("scenarios").put(key, record)
        audit(ctx.customer_id, ctx.identity.id, "scenario_retired", key, payload.note)
        return record


def validate_plan(company, payload):
    src = sources(company, payload.story_ids)
    if len(set(payload.environment_ids)) != len(payload.environment_ids):
        raise HTTPException(422, "Duplicate environment")
    for env_id in payload.environment_ids:
        get("environments", env_id, company)
    earlier, covered = set(), set()
    snapshots = []
    for selected in payload.selections:
        if selected.scenario_id in earlier or not set(selected.depends_on) <= earlier:
            raise HTTPException(422, "Scenarios must be unique and dependencies must appear earlier in the plan")
        rec = get("scenarios", selected.scenario_id, company)
        v = version(rec, selected.version)
        if rec.get("retired") or v["status"] != "approved":
            raise HTTPException(409, "Plans must reference approved, active scenario versions")
        earlier.add(selected.scenario_id)
        if v["body"].get("story_id") in payload.story_ids:
            covered.update((v["body"]["story_id"], c) for c in v["body"]["criteria"])
        snapshots.append({**selected.model_dump(), "body": v["body"], "hash": v["hash"]})
    missing = [{"story_id": s["id"], **c} for s in src for c in s["material"]["user_story"]["acceptance_criteria"]
               if (s["id"], c["id"]) not in covered]
    if missing and not payload.uncovered_criteria_reason:
        raise HTTPException(422, "Some acceptance criteria lack coverage. Add tests or explain the exclusion")
    return snapshots, src, missing


def save_plan(ctx, payload, key=None):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        old = get("plans", key, ctx.customer_id) if key else {
            "id": uid("plan"), "company_id": ctx.customer_id, "revision": 0, "versions": []}
        check_revision(old, payload.revision)
        snapshots, src, missing = validate_plan(ctx.customer_id, payload)
        body = payload.model_dump(exclude={"revision"})
        old["versions"].append({"version": len(old["versions"]) + 1, "status": "draft", "body": body,
                                "scenarios": snapshots, "sources": src, "missing_criteria": missing,
                                "source_hash": digest(src), "hash": digest([body, snapshots, src]),
                                "created_by": ctx.identity.id, "created_at": now()})
        old["revision"] += 1
        store("plans").put(old["id"], old)
        audit(ctx.customer_id, ctx.identity.id, "plan_version_created", old["id"])
        return old


def record_deployment(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "product_manager")
        get("environments", payload.environment_id, ctx.customer_id)
        result = {**payload.model_dump(), "id": uid("deployment"), "company_id": ctx.customer_id,
                  "source_hash": current_source_hash(ctx.customer_id, payload.story_ids), "at": now(),
                  "actor": ctx.identity.id, "verification": "application_manager_recorded"}
        store("deployments").put(result["id"], result)
        audit(ctx.customer_id, ctx.identity.id, "deployment_recorded", result["id"], payload.build)
        return result


def latest_deployment(company, environment_id, story_ids):
    candidates = [d for d in rows("deployments", company) if d["environment_id"] == environment_id
                  and set(d["story_ids"]) & set(story_ids)]
    return max(candidates, key=lambda d: d["at"], default=None)


def assert_current_plan(company, plan):
    if plan["status"] != "approved":
        raise HTTPException(409, "Approve the plan before execution")
    if plan["source_hash"] != current_source_hash(company, plan["body"]["story_ids"]):
        raise HTTPException(409, "The story or solution changed. Create and approve a new plan version")


def start(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        existing = next((r for r in rows("runs", ctx.customer_id)
                         if r["idempotency_key"] == payload.idempotency_key), None)
        if existing:
            if existing["request_hash"] != digest(payload.model_dump()):
                raise HTTPException(409, "Operation ID was already used for another request")
            return existing
        plan_record = get("plans", payload.plan_id, ctx.customer_id)
        p = version(plan_record, payload.version)
        if payload.version != len(plan_record["versions"]):
            raise HTTPException(409, "Use the current approved plan version")
        assert_current_plan(ctx.customer_id, p)
        env = get("environments", payload.environment_id, ctx.customer_id)
        if payload.environment_id not in p["body"]["environment_ids"]:
            raise HTTPException(422, "This environment is outside the approved plan")
        dep = get("deployments", payload.deployment_id, ctx.customer_id)
        current_dep = latest_deployment(ctx.customer_id, env["id"], p["body"]["story_ids"])
        if (not current_dep or dep["id"] != current_dep["id"] or dep["environment_id"] != env["id"]
                or set(dep["story_ids"]) != set(p["body"]["story_ids"])
                or dep["source_hash"] != p["source_hash"]):
            raise HTTPException(409, "Record the current implemented change set in this environment first")
        if payload.retest_of:
            previous = get("runs", payload.retest_of, ctx.customer_id)
            if previous["plan_id"] != payload.plan_id:
                raise HTTPException(422, "A retest must belong to the same plan")
        selected = set(payload.selected or [s["scenario_id"] for s in p["scenarios"]])
        if not selected <= {s["scenario_id"] for s in p["scenarios"]}:
            raise HTTPException(422, "Unknown scenario selection")
        # Include the dependency closure, including preparation, on every retest.
        for s in reversed(p["scenarios"]):
            if s["scenario_id"] in selected:
                selected.update(s["depends_on"])
        assignee = payload.assignee_id or ctx.identity.id
        if not membership_service.roles_for(assignee, ctx.customer_id):
            raise HTTPException(422, "The UAT assignee must be an active member of this customer")
        if env["stage"] == "PROD":
            if not policy(ctx.customer_id)["production_enabled"]:
                raise HTTPException(409, "Production validation is disabled")
            approval = get("production_approvals", payload.production_approval_id, ctx.customer_id)
            if any(approval.get(k) != getattr(payload, k) for k in ("plan_id", "version", "environment_id", "deployment_id")):
                raise HTTPException(409, "Production approval does not match this run")
            if approval["environment_revision"] != env["revision"] or approval["expires_at"] < now():
                raise HTTPException(409, "Production approval expired or environment settings changed")
        attempts = [{"scenario_id": s["scenario_id"], "version": s["version"], "body": s["body"],
                     "mandatory": s["mandatory"], "depends_on": s["depends_on"], "outcome": "not_run",
                     "selected": s["scenario_id"] in selected, "steps": [], "evidence": [], "review": None,
                     "assignee_id": assignee if s["body"]["route"] == "manual" else ""} for s in p["scenarios"]]
        result = {**payload.model_dump(), "id": uid("run"), "company_id": ctx.customer_id, "revision": 1,
                  "request_hash": digest(payload.model_dump()), "status": "queued", "created_at": now(),
                  "initiated_by": ctx.identity.id, "plan_hash": p["hash"], "source_hash": p["source_hash"],
                  "environment_revision": env["revision"], "environment_name": env["name"],
                  "build": dep["build"], "policy_revision": policy(ctx.customer_id)["revision"],
                  "attempts": attempts, "events": [], "reason": "", "lease_owner": "", "lease_until": 0,
                  "reconciliation_required": False, "finished_at": None}
        store("runs").put(result["id"], result)
        audit(ctx.customer_id, ctx.identity.id, "run_queued", result["id"])
        return result


def accessible_run(ctx, key):
    run = get("runs", key, ctx.customer_id)
    if not ctx.roles.intersection(READ_ROLES):
        assigned = [a for a in run["attempts"] if a["assignee_id"] == ctx.identity.id and a["selected"]]
        if not assigned:
            raise HTTPException(403, "Only assigned UAT tasks are accessible")
        return {k: v for k, v in {**run, "attempts": assigned, "events": []}.items()
                if k not in ("source_hash", "request_hash", "lease_owner")}
    return run


def evidence(company, run_id, scenario_id, name, mime, data, actor, *, classification="test_evidence"):
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Evidence exceeds 10 MB")
    key = uid("evidence")
    storage_key = f"validation/{company}/{run_id}/{key}"
    blob_store.default().put(storage_key, data)
    if hashlib.sha256(blob_store.default().get(storage_key)).hexdigest() != hashlib.sha256(data).hexdigest():
        raise RuntimeError("Evidence integrity check failed")
    result = {"id": key, "company_id": company, "run_id": run_id, "scenario_id": scenario_id,
              "name": name, "mime": mime, "storage_key": storage_key, "sha256": hashlib.sha256(data).hexdigest(),
              "size": len(data), "at": now(), "producer": actor, "classification": classification,
              "retention_until": (datetime.now(timezone.utc) + timedelta(days=policy(company)["evidence_days"])).isoformat()}
    store("evidence").put(key, result)
    return key


def upload(ctx, run_id, scenario_id, payload):
    accessible_run(ctx, run_id)
    try:
        data = base64.b64decode(payload.data, validate=True)
    except ValueError:
        raise HTTPException(422, "Evidence is not valid base64") from None
    with connection(immediate=True):
        run = get("runs", run_id, ctx.customer_id)
        attempt = next((a for a in run["attempts"] if a["scenario_id"] == scenario_id), None)
        if not attempt or attempt["body"]["route"] != "manual" or not attempt["selected"]:
            raise HTTPException(409, "Attachments belong to a selected manual task")
        if not membership_service.roles_for(ctx.identity.id, ctx.customer_id) or attempt["assignee_id"] != ctx.identity.id:
            raise HTTPException(403, "Only the assigned tester can attach evidence")
        if attempt.get("submitted_at"):
            raise HTTPException(409, "Submitted evidence is immutable; create a retest")
        eid = evidence(ctx.customer_id, run_id, scenario_id, payload.name, payload.mime, data, ctx.identity.id)
        attempt["evidence"].append(eid)
        run["revision"] += 1
        store("runs").put(run_id, run)
        return {"id": eid}


def manual_result(ctx, key, scenario_id, payload):
    with connection(immediate=True):
        run = get("runs", key, ctx.customer_id)
        check_revision(run, payload.revision)
        attempt = next((a for a in run["attempts"] if a["scenario_id"] == scenario_id), None)
        if not attempt or attempt["assignee_id"] != ctx.identity.id:
            raise HTTPException(403, "Only the assigned tester can submit this task")
        if not membership_service.roles_for(ctx.identity.id, ctx.customer_id):
            raise HTTPException(403, "Customer membership is no longer active")
        if attempt.get("submitted_at") or attempt["body"]["route"] != "manual" or not attempt["selected"]:
            raise HTTPException(409, "This manual task cannot be submitted")
        if run["status"] not in ("completed", "awaiting_input") or attempt["outcome"] not in ("not_run", "needs_review"):
            raise HTTPException(409, "Wait for preflight and prerequisite scenarios to complete")
        if any(a["outcome"] != "passed" for a in run["attempts"] if a["scenario_id"] in attempt["depends_on"]):
            raise HTTPException(409, "Prerequisite scenarios must pass first")
        expected = {s["id"] for s in attempt["body"]["steps"]}
        if set(payload.steps) != expected or any(not payload.observations.get(s, "").strip() for s in expected):
            raise HTTPException(422, "Every approved step needs an outcome and actual observation")
        body = payload.model_dump(exclude={"revision"})
        eid = evidence(ctx.customer_id, key, scenario_id, "Manual observations", "application/json",
                       json.dumps(body).encode(), ctx.identity.id)
        attempt["evidence"].append(eid)
        attempt["steps"] = [{"step_id": s, "outcome": payload.steps[s], "observed": payload.observations[s]}
                            for s in payload.steps]
        outcomes = set(payload.steps.values())
        attempt["outcome"] = "failed" if "failed" in outcomes else "passed" if outcomes == {"passed"} else "needs_review"
        attempt["submitted_at"] = now()
        if run["status"] == "awaiting_input":
            run["status"] = "queued"
        run["revision"] += 1
        store("runs").put(key, run)
        audit(ctx.customer_id, ctx.identity.id, "manual_task_submitted", key, payload.note)
        return accessible_run(ctx, key)


def review_run(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        run = get("runs", key, ctx.customer_id)
        check_revision(run, payload.revision)
        if run["status"] in ACTIVE:
            raise HTTPException(409, "Execution is still active")
        if policy(ctx.customer_id)["require_independent_review"] and (ctx.identity.id == run["initiated_by"] or any(a.get("submitted_at") and a["assignee_id"] == ctx.identity.id for a in run["attempts"])):
            raise HTTPException(403, "A Test Manager independent of execution and UAT must review this run")
        review = {"actor": ctx.identity.id, "at": now(), "note": payload.note}
        for a in run["attempts"]:
            if a["outcome"] in ("passed", "failed") and a["evidence"]:
                a["review"] = review
        run["revision"] += 1
        store("runs").put(key, run)
        audit(ctx.customer_id, ctx.identity.id, "results_reviewed", key, payload.note)
        return run


def assess_attempt(ctx, key, scenario_id, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        run = get("runs", key, ctx.customer_id)
        check_revision(run, payload.revision)
        if run["status"] not in ("awaiting_input", "completed") or run["reconciliation_required"]:
            raise HTTPException(409, "Execution must finish safely before assessment")
        a = next((a for a in run["attempts"] if a["scenario_id"] == scenario_id), None)
        if not a or a["body"]["route"] == "manual" or a["outcome"] != "needs_review" or not a["evidence"]:
            raise HTTPException(409, "Only evidenced automated observations awaiting judgment may be assessed")
        if policy(ctx.customer_id)["require_independent_review"] and run["initiated_by"] == ctx.identity.id:
            raise HTTPException(403, "An independent Test Manager must assess this result")
        pending = {step["step_id"] for step in a["steps"] if step["outcome"] == "needs_review"}
        if set(payload.steps) != pending or len(a["steps"]) != len(a["body"]["steps"]):
            raise HTTPException(422, "Assess every ambiguous step; unexecuted steps cannot be passed")
        a["assessment"] = {"actor": ctx.identity.id, "at": now(), "note": payload.note, "steps": payload.steps}
        outcomes = [payload.steps.get(step["step_id"], step["outcome"]) for step in a["steps"]]
        a["outcome"] = "passed" if all(o == "passed" for o in outcomes) else "failed"
        if run["status"] == "awaiting_input":
            run["status"] = "queued"
        run["revision"] += 1
        store("runs").put(key, run)
        audit(ctx.customer_id, ctx.identity.id, "observations_assessed", key, payload.note)
        return run


def stop(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        run = get("runs", key, ctx.customer_id)
        check_revision(run, payload.revision)
        if run["status"] not in ACTIVE:
            raise HTTPException(409, "Run is no longer active")
        run["status"] = "cancelled" if run["status"] in ("queued", "awaiting_input") else "stop_requested"
        run["revision"] += 1
        store("runs").put(key, run)
        audit(ctx.customer_id, ctx.identity.id, "stop_requested", key, payload.note)
        return run


def reconcile(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        run = get("runs", key, ctx.customer_id)
        check_revision(run, payload.revision)
        if not run["reconciliation_required"] or not payload.note:
            raise HTTPException(422, "Record the actual transaction and cleanup checks before releasing the environment lock")
        run.update(reconciliation_required=False, reconciliation={"actor": ctx.identity.id, "at": now(), "note": payload.note},
                   revision=run["revision"] + 1)
        store("runs").put(key, run)
        audit(ctx.customer_id, ctx.identity.id, "uncertain_run_reconciled", key, payload.note)
        return run


def raise_defect(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        run = get("runs", payload.run_id, ctx.customer_id)
        attempt = next((a for a in run["attempts"] if a["scenario_id"] == payload.scenario_id), None)
        if not attempt or attempt["outcome"] != "failed":
            raise HTTPException(409, "A defect requires an observed assertion failure")
        existing = next((d for d in rows("defects", ctx.customer_id) if d["plan_id"] == run["plan_id"]
                         and d["environment_id"] == run["environment_id"] and d["scenario_id"] == payload.scenario_id
                         and d["status"] != "closed"), None)
        if existing:
            return existing
        d = {**payload.model_dump(), "id": uid("defect"), "company_id": ctx.customer_id, "revision": 1,
             "title": attempt["body"]["title"], "plan_id": run["plan_id"], "environment_id": run["environment_id"],
             "build": run["build"], "steps": attempt["steps"], "evidence": attempt["evidence"],
             "status": "open", "created_by": ctx.identity.id, "created_at": now(), "jira_key": "",
             "sync_state": "pending" if policy(ctx.customer_id)["jira_enabled"] else "disabled"}
        store("defects").put(d["id"], d)
        audit(ctx.customer_id, ctx.identity.id, "defect_created", d["id"])
        return d


def close_defect(ctx, key, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        d = get("defects", key, ctx.customer_id)
        check_revision(d, payload.revision)
        coverage = summary(ctx.customer_id, d["plan_id"])
        matching = [c for c in coverage["coverage"] if c["scenario_id"] == d["scenario_id"]
                    and c["environment_id"] == d["environment_id"]]
        if not matching or any(c["outcome"] != "passed" or not c["reviewed"] or c["run_id"] == d["run_id"] for c in matching):
            raise HTTPException(409, "A current reviewed passing retest is required before closing this defect")
        d.update(status="closed", revision=d["revision"] + 1,
                 resolution={"actor": ctx.identity.id, "at": now(), "note": payload.note,
                             "retest_run_ids": [c["run_id"] for c in matching]})
        store("defects").put(key, d)
        audit(ctx.customer_id, ctx.identity.id, "defect_closed", key, payload.note)
        return d


def summary(company, plan_id, number=None):
    record = get("plans", plan_id, company)
    p = version(record, number)
    all_runs = sorted([r for r in rows("runs", company) if r["plan_id"] == plan_id and r["version"] == p["version"]],
                      key=lambda r: r["created_at"], reverse=True)
    current_hash = current_source_hash(company, p["body"]["story_ids"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=policy(company)["freshness_days"])).isoformat()
    coverage = []
    for env_id in p["body"]["environment_ids"]:
        env = get("environments", env_id, company)
        dep = latest_deployment(company, env_id, p["body"]["story_ids"])
        for s in p["scenarios"]:
            chosen, attempt = None, None
            for r in all_runs:
                a = next((a for a in r["attempts"] if a["scenario_id"] == s["scenario_id"] and a["selected"]), None)
                if a and r["environment_id"] == env_id:
                    chosen, attempt = r, a
                    break
            stale = bool(chosen and (chosen["source_hash"] != current_hash or chosen["plan_hash"] != p["hash"]
                or chosen["environment_revision"] != env["revision"] or not dep or chosen["deployment_id"] != dep["id"]
                or chosen["created_at"] < cutoff or chosen["policy_revision"] != policy(company)["revision"]
                or p["version"] != len(record["versions"])))
            outcome = "stale" if stale else attempt["outcome"] if attempt else "not_run"
            if chosen and chosen["status"] in ACTIVE:
                outcome = "running"
            complete = bool(attempt and attempt["evidence"] and attempt.get("review"))
            coverage.append({"environment_id": env_id, "environment_name": env["name"], "stage": env["stage"],
                             "scenario_id": s["scenario_id"], "title": s["body"]["title"], "mandatory": s["mandatory"],
                             "outcome": outcome, "reviewed": complete, "run_id": chosen["id"] if chosen else None,
                             "run_revision": chosen["revision"] if chosen else None,
                             "deployment_id": dep["id"] if dep else None, "environment_revision": env["revision"]})
    defects = [d for d in rows("defects", company) if d["plan_id"] == plan_id and d["status"] != "closed"]
    # PROD smoke is post-deployment: it cannot circularly block the pre-production release decision.
    required = [c for c in coverage if c["mandatory"] and c["stage"] != "PROD"]
    unresolved = [c for c in required if c["outcome"] != "passed" or not c["reviewed"]]
    blocking = [d for d in defects if d["severity"] in ("critical", "high")]
    valid = p["status"] == "approved" and p["source_hash"] == current_hash and p["version"] == len(record["versions"])
    result = "failed" if blocking or any(c["outcome"] == "failed" for c in required) else "incomplete" if unresolved or not valid or not required else "passed"
    ch = digest({"coverage": coverage, "defects": [(d["id"], d["revision"]) for d in defects],
                 "plan": p["hash"], "source": current_hash, "valid": valid, "policy": policy(company)["revision"]})
    signoffs = [s for s in rows("signoffs", company) if s["plan_id"] == plan_id and s["version"] == p["version"]]
    latest = max(signoffs, key=lambda s: s["at"], default=None)
    return {"plan_id": plan_id, "version": p["version"], "title": p["body"]["title"], "outcome": result,
            "coverage_hash": ch, "coverage": coverage, "defects": defects, "missing_criteria": p["missing_criteria"],
            "signoff": latest, "signoff_current": bool(latest and latest["coverage_hash"] == ch),
            "source_current": valid, "exclusions": p["body"]["exclusions"]}


def signoff(ctx, plan_id, payload):
    with connection(immediate=True):
        require_current_role(ctx, "test_manager")
        s = summary(ctx.customer_id, plan_id, payload.version)
        if s["coverage_hash"] != payload.coverage_hash:
            raise HTTPException(409, "Coverage changed. Review the current results before signing off")
        if payload.exceptions and not set(payload.exceptions) <= {d["id"] for d in s["defects"]}:
            raise HTTPException(422, "Exceptions must refer to current open defects")
        outcome = s["outcome"]
        if outcome == "passed" and (s["defects"] or s["missing_criteria"] or s["exclusions"]):
            outcome = "passed_with_exceptions"
        r = {**payload.model_dump(), "id": uid("signoff"), "company_id": ctx.customer_id, "plan_id": plan_id,
             "outcome": outcome, "actor": ctx.identity.id, "at": now(), "coverage": s["coverage"]}
        store("signoffs").put(r["id"], r)
        audit(ctx.customer_id, ctx.identity.id, "validation_signed_off", plan_id, payload.note)
        return r


def release(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "product_manager")
        s = summary(ctx.customer_id, payload.plan_id, payload.version)
        if s["coverage_hash"] != payload.coverage_hash:
            raise HTTPException(409, "Validation changed. Review the current recommendation")
        ready = s["outcome"] == "passed" and s["signoff_current"] and s["signoff"]["outcome"] == "passed"
        if payload.decision == "approve" and not ready:
            if not policy(ctx.customer_id)["allow_release_exceptions"] or not payload.exception_reason:
                raise HTTPException(409, "Current signed validation is required; an exception needs explicit policy and rationale")
        r = {**payload.model_dump(), "id": uid("release"), "company_id": ctx.customer_id,
             "actor": ctx.identity.id, "at": now(), "validation_outcome": s["outcome"],
             "signoff_id": s["signoff"]["id"] if s["signoff_current"] else None,
             "deployment_performed": False}
        store("releases").put(r["id"], r)
        audit(ctx.customer_id, ctx.identity.id, "release_" + payload.decision, r["id"], payload.note)
        return r


def approve_production(ctx, payload):
    with connection(immediate=True):
        require_current_role(ctx, "product_manager")
        env = get("environments", payload.environment_id, ctx.customer_id)
        dep = get("deployments", payload.deployment_id, ctx.customer_id)
        plan = version(get("plans", payload.plan_id, ctx.customer_id), payload.version)
        assert_current_plan(ctx.customer_id, plan)
        if env["stage"] != "PROD" or dep["environment_id"] != env["id"] or env["id"] not in plan["body"]["environment_ids"]:
            raise HTTPException(422, "Select the production deployment and approved smoke plan")
        r = {**payload.model_dump(), "id": uid("prodapproval"), "company_id": ctx.customer_id,
             "environment_revision": env["revision"], "actor": ctx.identity.id, "at": now(),
             "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()}
        store("production_approvals").put(r["id"], r)
        audit(ctx.customer_id, ctx.identity.id, "production_smoke_authorised", r["id"], payload.note)
        return r


def dashboard(ctx):
    plans = rows("plans", ctx.customer_id)
    runs = sorted(rows("runs", ctx.customer_id), key=lambda r: r["created_at"], reverse=True)
    return {"plans": plans, "scenarios": rows("scenarios", ctx.customer_id), "runs": runs[:200],
            "environments": [public_environment(e) for e in rows("environments", ctx.customer_id)],
            "deployments": rows("deployments", ctx.customer_id), "defects": rows("defects", ctx.customer_id),
            "releases": rows("releases", ctx.customer_id), "production_approvals": rows("production_approvals", ctx.customer_id),
            "summaries": [summary(ctx.customer_id, p["id"]) for p in plans],
            "agent_jobs": rows("agent_jobs", ctx.customer_id),
            "permissions": {"manage": "test_manager" in ctx.roles, "release": "product_manager" in ctx.roles,
                            "admin": "admin" in ctx.roles}}


def story_handoff(company, story_id):
    """Only plans explicitly associated with this story affect its existing release journey."""
    handoffs = []
    for plan in rows("plans", company):
        v = version(plan)
        if story_id not in v["body"]["story_ids"]:
            continue
        result = summary(company, plan["id"])
        decisions = [r for r in rows("releases", company) if r["plan_id"] == plan["id"]]
        decision = max(decisions, key=lambda r: r["at"], default=None)
        approved = bool(decision and decision["decision"] == "approve"
                        and decision["coverage_hash"] == result["coverage_hash"])
        handoffs.append({"plan_id": plan["id"], "title": v["body"]["title"], "version": v["version"],
                         "outcome": result["outcome"], "coverage_hash": result["coverage_hash"],
                         "coverage": result["coverage"], "signoff": result["signoff"],
                         "release": decision, "release_current": approved})
    return handoffs
