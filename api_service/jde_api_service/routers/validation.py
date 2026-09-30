"""Customer-scoped validation API. Human authority is checked again on transitions."""
import hashlib
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ..dependencies import AuthContext, require_customer_access, require_role
from ..persistence import blob_store
from ..persistence.db import connection
from ..services import agent_settings, membership_service
from ..services.registry import get_change_service
from ..validation import agents, models as m, runners, service as s

router = APIRouter(prefix="/validation", tags=["validation"])
read = require_role(*s.READ_ROLES)
manage = require_role("test_manager")
admin = require_role("admin")
release_manager = require_role("product_manager")


@router.get("")
def dashboard(ctx: AuthContext = Depends(read)):
    return s.dashboard(ctx)


@router.get("/sources")
def sources(ctx: AuthContext = Depends(read)):
    return [s.source(ctx.customer_id, c.id) for c in get_change_service().list_for_customer(ctx.customer_id)
            if c.user_story is not None]


@router.get("/members")
def members(ctx: AuthContext = Depends(read)):
    return [{"id": r["user_id"], "name": r["display_name"]} for r in membership_service.list_company_members(ctx.customer_id)
            if r["status"] == "active"]


@router.get("/settings")
def settings(ctx: AuthContext = Depends(admin)):
    from ..executors.browser import available
    from ..services import credential_crypto
    return {"policy": s.policy(ctx.customer_id), "environments": [s.public_environment(e) for e in s.rows("environments", ctx.customer_id)],
            "health": {"worker_available": any(w["heartbeat"] > time.time() - 45 for w in s.store("workers").list_all()),
                       "browser_available": available()[0], "credential_key_configured": credential_crypto.is_configured()},
            "agents": agents.LABELS, "storage": type(blob_store.default()).__name__}


@router.post("/storage/test")
def test_storage(ctx: AuthContext = Depends(admin)):
    key = f"validation/{ctx.customer_id}/health/{s.uid('probe')}"
    backend = blob_store.default()
    try:
        backend.put(key, b"JADE evidence storage integrity probe")
        if backend.get(key) != b"JADE evidence storage integrity probe":
            raise ValueError("Integrity mismatch")
    except Exception:
        raise HTTPException(409, "Evidence storage is not writable and readable. Contact the platform operator") from None
    finally:
        backend.delete_prefix(key)
    s.audit(ctx.customer_id, ctx.identity.id, "evidence_storage_tested", key)
    return {"status": "verified", "backend": type(backend).__name__}


@router.put("/policy")
def policy(payload: m.Policy, ctx: AuthContext = Depends(admin)):
    return s.save_policy(ctx, payload)


@router.post("/environments")
def create_environment(payload: m.Environment, ctx: AuthContext = Depends(admin)):
    return s.save_environment(ctx, payload)


@router.put("/environments/{key}")
def update_environment(key: str, payload: m.Environment, ctx: AuthContext = Depends(admin)):
    return s.save_environment(ctx, payload, key)


@router.post("/environments/{key}/test")
def test_environment(key: str, payload: m.Revision, ctx: AuthContext = Depends(admin)):
    env = s.get("environments", key, ctx.customer_id)
    s.check_revision(env, payload.revision)
    try:
        result = runners.test_connection(env)
    except Exception:
        raise HTTPException(409, "Connection failed. Check the saved endpoint, trusted certificate, account, environment and role") from None
    with connection(immediate=True):
        current = s.get("environments", key, ctx.customer_id)
        s.check_revision(current, payload.revision)
        current["connection_test"] = {**result, "at": s.now(), "actor": ctx.identity.id}
        s.store("environments").put(key, current)
        s.audit(ctx.customer_id, ctx.identity.id, "connection_tested", key)
    return result


@router.post("/scenarios")
def create_scenario(payload: m.Scenario, ctx: AuthContext = Depends(manage)):
    return s.save_scenario(ctx, payload)


@router.put("/scenarios/{key}")
def update_scenario(key: str, payload: m.Scenario, ctx: AuthContext = Depends(manage)):
    return s.save_scenario(ctx, payload, key)


@router.post("/scenarios/{key}/approve")
def approve_scenario(key: str, payload: m.Revision, ctx: AuthContext = Depends(manage)):
    return s.approve(ctx, "scenarios", key, payload)


@router.post("/scenarios/{key}/retire")
def retire_scenario(key: str, payload: m.Revision, ctx: AuthContext = Depends(manage)):
    return s.retire_scenario(ctx, key, payload)


@router.post("/plans")
def create_plan(payload: m.Plan, ctx: AuthContext = Depends(manage)):
    return s.save_plan(ctx, payload)


@router.put("/plans/{key}")
def update_plan(key: str, payload: m.Plan, ctx: AuthContext = Depends(manage)):
    return s.save_plan(ctx, payload, key)


@router.post("/plans/{key}/approve")
def approve_plan(key: str, payload: m.Revision, ctx: AuthContext = Depends(manage)):
    return s.approve(ctx, "plans", key, payload)


@router.get("/plans/{key}/summary")
def summary(key: str, version: int | None = None, ctx: AuthContext = Depends(read)):
    return s.summary(ctx.customer_id, key, version)


@router.get("/plans/{key}/report")
def report(key: str, ctx: AuthContext = Depends(read)):
    import io
    import json
    import zipfile
    summary = s.summary(ctx.customer_id, key)
    plan = s.get("plans", key, ctx.customer_id)
    chosen = {c["run_id"] for c in summary["coverage"] if c["run_id"]}
    runs = [s.get("runs", run_id, ctx.customer_id) for run_id in sorted(chosen)]
    evidence_ids = sorted({eid for run in runs for a in run["attempts"] for eid in a["evidence"]})
    metadata = [s.get("evidence", eid, ctx.customer_id) for eid in evidence_ids]
    if sum(e["size"] for e in metadata) > 100 * 1024 * 1024:
        raise HTTPException(413, "Report evidence exceeds 100 MB. Download individual run evidence")
    stream = io.BytesIO()
    lines = ["# " + summary["title"], "", "Testing conclusion: " + summary["outcome"],
             "Plan version: " + str(summary["version"]), "", "This is a validation report, not a deployment instruction.", ""]
    for c in summary["coverage"]:
        lines.append(f"- {c['title']} / {c['environment_name']}: {c['outcome']} ({'reviewed' if c['reviewed'] else 'unreviewed'})")
    lines.extend(["", "## Evidence", ""])
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps({"exported_at": s.now(), "summary": summary, "plan": plan,
                                                   "runs": runs, "evidence": metadata}, indent=2))
        for e in metadata:
            data = blob_store.default().get(e["storage_key"])
            if hashlib.sha256(data).hexdigest() != e["sha256"]:
                raise HTTPException(409, "Evidence integrity failed; report was not exported")
            ext = {"image/png":"png", "image/jpeg":"jpg", "application/pdf":"pdf", "application/json":"json", "text/plain":"txt"}.get(e["mime"], "bin")
            filename = f"evidence/{e['id']}.{ext}"
            archive.writestr(filename, data)
            lines.append(f"- [{e['id']}]({filename}) — SHA-256 {e['sha256']}")
        archive.writestr("README.md", "\n".join(lines))
    return Response(stream.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="jade-validation-report.zip"', "Cache-Control":"no-store"})


@router.post("/plans/{key}/signoff")
def signoff(key: str, payload: m.Signoff, ctx: AuthContext = Depends(manage)):
    return s.signoff(ctx, key, payload)


@router.post("/deployments")
def deployment(payload: m.Deployment, ctx: AuthContext = Depends(release_manager)):
    return s.record_deployment(ctx, payload)


@router.post("/releases")
def release(payload: m.Release, ctx: AuthContext = Depends(release_manager)):
    return s.release(ctx, payload)


@router.post("/production-approvals")
def production_approval(payload: m.ProductionApproval, ctx: AuthContext = Depends(release_manager)):
    return s.approve_production(ctx, payload)


@router.post("/runs")
def start(payload: m.Start, ctx: AuthContext = Depends(manage)):
    return s.start(ctx, payload)


@router.get("/tasks")
def tasks(ctx: AuthContext = Depends(require_customer_access)):
    return [s.accessible_run(ctx, r["id"]) for r in s.rows("runs", ctx.customer_id)
            if any(a["selected"] and a["assignee_id"] == ctx.identity.id and a["body"]["route"] == "manual" for a in r["attempts"])]


@router.get("/runs/{key}")
def run(key: str, ctx: AuthContext = Depends(require_customer_access)):
    return s.accessible_run(ctx, key)


@router.post("/runs/{key}/stop")
def stop(key: str, payload: m.Revision, ctx: AuthContext = Depends(manage)):
    return s.stop(ctx, key, payload)


@router.post("/runs/{key}/review")
def review(key: str, payload: m.Review, ctx: AuthContext = Depends(manage)):
    return s.review_run(ctx, key, payload)


@router.post("/runs/{key}/reconcile")
def reconcile(key: str, payload: m.Review, ctx: AuthContext = Depends(manage)):
    return s.reconcile(ctx, key, payload)


@router.post("/runs/{key}/attempts/{scenario_id}/result")
def manual_result(key: str, scenario_id: str, payload: m.ManualResult, ctx: AuthContext = Depends(require_customer_access)):
    return s.manual_result(ctx, key, scenario_id, payload)


@router.post("/runs/{key}/attempts/{scenario_id}/assessment")
def assessment(key: str, scenario_id: str, payload: m.Assessment, ctx: AuthContext = Depends(manage)):
    return s.assess_attempt(ctx, key, scenario_id, payload)


@router.post("/runs/{key}/attempts/{scenario_id}/evidence")
def upload(key: str, scenario_id: str, payload: m.EvidenceUpload, ctx: AuthContext = Depends(require_customer_access)):
    return s.upload(ctx, key, scenario_id, payload)


@router.get("/evidence/{key}")
def download(key: str, ctx: AuthContext = Depends(require_customer_access)):
    item = s.get("evidence", key, ctx.customer_id)
    run = s.accessible_run(ctx, item["run_id"])
    if not any(a["scenario_id"] == item["scenario_id"] for a in run["attempts"]):
        raise HTTPException(403, "Evidence is outside your assigned tasks")
    data = blob_store.default().get(item["storage_key"])
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise HTTPException(409, "Stored evidence failed its integrity check")
    extensions = {"application/json": "json", "application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg", "text/plain": "txt"}
    return Response(data, media_type=item["mime"], headers={"Content-Disposition": f'attachment; filename="{item["id"]}.{extensions.get(item["mime"], "bin")}"',
                                                         "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/evidence/{key}/info")
def evidence_info(key: str, ctx: AuthContext = Depends(require_customer_access)):
    item = s.get("evidence", key, ctx.customer_id)
    run = s.accessible_run(ctx, item["run_id"])
    if not any(a["scenario_id"] == item["scenario_id"] for a in run["attempts"]):
        raise HTTPException(403, "Evidence is outside your assigned tasks")
    return {k: item[k] for k in ("id", "name", "mime", "sha256", "size", "at")}


@router.post("/defects")
def defect(payload: m.Defect, ctx: AuthContext = Depends(manage)):
    return s.raise_defect(ctx, payload)


@router.post("/defects/{key}/close")
def close_defect(key: str, payload: m.Review, ctx: AuthContext = Depends(manage)):
    return s.close_defect(ctx, key, payload)


@router.post("/agents")
def agent(payload: m.AgentRequest, ctx: AuthContext = Depends(manage)):
    try:
        return agents.queue(ctx, payload)
    except (agent_settings.AgentDisabled, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/audit")
def audit(ctx: AuthContext = Depends(read)):
    return sorted(s.rows("audit", ctx.customer_id), key=lambda a: a["at"], reverse=True)


@router.post('/defects/{key}/sync')
def sync_defect(key: str, payload: m.Revision, ctx: AuthContext = Depends(manage)):
    from ..validation import jira
    return jira.sync(ctx, key, payload)
