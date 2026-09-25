"""
Architect Environment Discovery -- Admin > Integrations > JDE, the
technical baseline import, and the evidence behind each design.

Profile and connection actions are Admin-only. Saving never contacts JDE;
Test Connection, Run Approved Sample Read, Enable Discovery and Disable
Connection are explicit actions. No endpoint ever returns the credential.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from ..dependencies import AuthContext, require_customer_access, require_role
from ..discovery import artifacts, baseline, profile_service, service
from ..discovery.models import (
    ActionResult,
    ActivityRow,
    ArtifactUpload,
    ArtifactView,
    CredentialUpdate,
    DesignBaselineView,
    JdeProfileConfig,
    JdeProfileUpdate,
    JdeProfileView,
    SampleReadInput,
)
from ..models.base import ApiModel
from ..services import credential_crypto
from ..services.registry import get_architecture_review_service, get_change_service

router = APIRouter(tags=["jde-discovery"])


class RevisionInput(ApiModel):
    expected_revision: Optional[int] = None


def _action(company_id: str, outcome: str, detail: str = "", evidence: Optional[dict] = None,
            in_flight: Optional[list] = None) -> ActionResult:
    return ActionResult(outcome=outcome, detail=detail, profile=profile_service.view(company_id), evidence=evidence,
                        in_flight=in_flight or [])


# ---------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------
@router.get("/admin/jde/profile", response_model=JdeProfileView)
def get_profile(ctx: AuthContext = Depends(require_customer_access)) -> JdeProfileView:
    return profile_service.view(ctx.customer_id)


@router.put("/admin/jde/profile", response_model=JdeProfileView)
def save_profile(payload: JdeProfileUpdate, ctx: AuthContext = Depends(require_role("admin"))) -> JdeProfileView:
    """Stores a new revision. Does not contact JDE. A material change
    switches discovery off until re-verified and re-enabled."""
    config = JdeProfileConfig.model_validate(payload.model_dump(exclude={"expected_revision"}))
    from ..services.customer_service import is_demo_company

    if config.connection_mode == "simulation" and not is_demo_company(ctx.customer_id):
        raise HTTPException(status_code=422, detail="Simulation is only available for demo customers. Use Live with "
                                                    "the customer's real AIS address.")
    for ref in (*config.evidence_artifact_ids, *config.dedicated_account.evidence_artifact_ids,
                *config.network_restriction.evidence_artifact_ids):  # this company's own documents only
        a = artifacts.get(ctx.customer_id, ref.partition("@r")[0])
        if a is None:
            raise HTTPException(status_code=422, detail=f"evidence document {ref} is not a document of this company")
    before = profile_service.load(ctx.customer_id)
    saved = profile_service.save(ctx.customer_id, config, expected_revision=payload.expected_revision,
                                 actor=ctx.identity.display_name)
    if before is not None and before["material_hash"] != saved["material_hash"]:
        baseline.on_profile_saved(ctx.customer_id, saved)
    return profile_service.view(ctx.customer_id)


@router.put("/admin/jde/credential", response_model=JdeProfileView)
def save_credential(payload: CredentialUpdate, ctx: AuthContext = Depends(require_role("admin"))) -> JdeProfileView:
    try:
        saved = profile_service.save_credential(ctx.customer_id, payload.username, payload.password,
                                                expected_revision=payload.expected_revision,
                                                actor=ctx.identity.display_name)
    except credential_crypto.CredentialKeyMissing as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except profile_service.ProfileNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    baseline.on_profile_saved(ctx.customer_id, saved)
    return profile_service.view(ctx.customer_id)


@router.post("/admin/jde/test-connection", response_model=ActionResult)
def test_connection(ctx: AuthContext = Depends(require_role("admin"))) -> ActionResult:
    try:
        outcome, detail = service.test_connection(ctx.customer_id, ctx.identity.id)
    except service.DiscoveryBlocked as exc:
        return _action(ctx.customer_id, "blocked", str(exc))
    return _action(ctx.customer_id, outcome, detail)


@router.post("/admin/jde/sample-read/preview")
def preview_sample_read(payload: SampleReadInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    """The exact AIS request a sample read would send, built server-side from
    the approved read. Nothing is sent to JDE."""
    try:
        return service.sample_read_preview(ctx.customer_id, payload.capability_id, target=payload.target,
                                           fields=payload.fields, filters=payload.filters,
                                           max_records=payload.max_records)
    except service.DiscoveryBlocked as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/admin/jde/sample-read", response_model=ActionResult)
def run_sample_read(payload: SampleReadInput, ctx: AuthContext = Depends(require_role("admin"))) -> ActionResult:
    try:
        evidence = service.sample_read(ctx.customer_id, ctx.identity.id, payload.capability_id, target=payload.target,
                                       fields=payload.fields, filters=payload.filters, max_records=payload.max_records)
    except service.DiscoveryBlocked as exc:
        return _action(ctx.customer_id, "blocked", str(exc))
    except service.DiscoveryFailed as exc:
        return _action(ctx.customer_id, "failed", str(exc))
    return _action(ctx.customer_id, "ok", f"{evidence['record_count']} record(s)", evidence=evidence)


@router.post("/admin/jde/enable", response_model=ActionResult)
def enable_discovery(payload: RevisionInput, ctx: AuthContext = Depends(require_role("admin"))) -> ActionResult:
    try:
        profile_service.set_enabled(ctx.customer_id, actor=ctx.identity.display_name,
                                    expected_revision=payload.expected_revision)
    except profile_service.ProfileNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return _action(ctx.customer_id, "ok", "discovery enabled for this profile revision")


@router.post("/admin/jde/disable", response_model=ActionResult)
def disable_connection(ctx: AuthContext = Depends(require_role("admin"))) -> ActionResult:
    """Blocks new and queued calls at once; reports anything already in
    flight (it is not interrupted mid-request)."""
    try:
        profile_service.set_disabled(ctx.customer_id, actor=ctx.identity.display_name)
    except profile_service.ProfileNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    flying = service.in_flight(ctx.customer_id)
    detail = "connection disabled" + (f"; {len(flying)} request already in flight will finish" if flying else "")
    return _action(ctx.customer_id, "ok", detail, in_flight=flying)


@router.get("/admin/jde/activity", response_model=list[ActivityRow])
def activity(ctx: AuthContext = Depends(require_role("admin"))) -> list[ActivityRow]:
    return [ActivityRow.model_validate(r) for r in service.list_activity(ctx.customer_id)]


# ---------------------------------------------------------------------
# Technical baseline and reference documents
# ---------------------------------------------------------------------
def _artifact_view(a: dict, profile: Optional[dict], latest: bool = True) -> ArtifactView:
    compat = None
    if a["kind"] == "reference_document":
        config = profile["config"] if profile else None
        compat = artifacts.compatibility(a, config.expected_application_release if config else None,
                                         config.expected_tools_release if config else None)
    meta = {k: v for k, v in a["meta"].items() if k != "text_storage_key"}
    return ArtifactView(artifact_id=a["artifact_id"], revision=a["revision"], company_id=a["company_id"],
                        domain_id=a["domain_id"], kind=a["kind"], meta=meta, sha256=a["sha256"],
                        size_bytes=a["size_bytes"], extraction_status=a["extraction_status"],
                        extraction_note=a["extraction_note"], uploaded_by=a["uploaded_by"],
                        uploaded_at=a["uploaded_at"], compatibility=compat, latest=latest)


@router.post("/admin/jde/artifacts", response_model=ArtifactView)
def upload_artifact(payload: ArtifactUpload, ctx: AuthContext = Depends(require_role("admin", "product_manager"))) -> ArtifactView:
    try:
        created = artifacts.upload(ctx.customer_id, payload, actor=ctx.identity.display_name)
    except artifacts.ArtifactRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if created["revision"] > 1:
        baseline.on_artifact_uploaded(ctx.customer_id, created)
    return _artifact_view(created, profile_service.load(ctx.customer_id))


@router.get("/admin/jde/artifacts", response_model=list[ArtifactView])
def list_artifacts(ctx: AuthContext = Depends(require_customer_access)) -> list[ArtifactView]:
    profile = profile_service.load(ctx.customer_id)
    rows = artifacts.list_for(ctx.customer_id, all_domains=True, latest_only=False)
    latest = {}
    for a in rows:
        latest[a["artifact_id"]] = max(latest.get(a["artifact_id"], 0), a["revision"])
    return [_artifact_view(a, profile, latest=a["revision"] == latest[a["artifact_id"]]) for a in rows]


@router.get("/admin/jde/artifacts/{artifact_id}/{revision}/text")
def artifact_text(artifact_id: str, revision: int,
                  ctx: AuthContext = Depends(require_role("admin", "product_manager"))) -> dict:
    a = artifacts.get(ctx.customer_id, artifact_id, revision)
    if a is None:
        raise HTTPException(status_code=404, detail="no such artifact")
    text = artifacts.read_text(ctx.customer_id, a)
    if text is None:
        return {"available": False, "reason": a["extraction_note"]}
    return {"available": True, "text": text[:20000], "note": a["extraction_note"]}


# ---------------------------------------------------------------------
# Evidence behind a design
# ---------------------------------------------------------------------
def _require_change(change_id: str, customer_id: str) -> None:
    if get_change_service().get_for_customer(change_id, customer_id) is None:
        raise HTTPException(status_code=404, detail=f"no such change: {change_id}")


def _baseline_view(company_id: str, b: dict, with_observations: bool) -> DesignBaselineView:
    obs = []
    if with_observations:
        for entry in b["manifest"].get("observations", []):
            o = service.get_observation(company_id, entry["observation_id"])
            if o:
                obs.append(o["evidence"])
    return DesignBaselineView(baseline_id=b["baseline_id"], story_id=b["story_id"], design_revision=b["design_revision"],
                              baseline_revision=b["baseline_revision"], created_at=b["created_at"], trigger=b["trigger"],
                              manifest=b["manifest"], manifest_sha256=b["manifest_sha256"], status=b["status"],
                              reassessment=b["reassessment"], observations=obs)


@router.get("/changes/{change_id}/architecture-review/evidence", response_model=list[DesignBaselineView])
def design_evidence(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> list[DesignBaselineView]:
    _require_change(change_id, ctx.customer_id)
    rows = baseline.list_for_story(ctx.customer_id, change_id)
    return [_baseline_view(ctx.customer_id, b, with_observations=i == 0) for i, b in enumerate(rows)]


@router.post("/changes/{change_id}/architecture-review/refresh-evidence", response_model=DesignBaselineView)
def refresh_evidence(change_id: str,
                     ctx: AuthContext = Depends(require_role("product_manager", "admin"))) -> DesignBaselineView:
    """Re-reads the same approved targets under the current profile. A new
    baseline revision is created; earlier ones are kept."""
    _require_change(change_id, ctx.customer_id)
    try:
        created = baseline.refresh(ctx.customer_id, change_id, actor_user_id=ctx.identity.id)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except service.DiscoveryBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    run = get_architecture_review_service().get(change_id)
    if run and run.history:
        latest = run.history[-1]
        baseline.write_handoff(ctx.customer_id, change_id, created, latest.architect_decision.model_dump(mode="json"),
                               latest.implementation_spec.model_dump(mode="json"))
    return _baseline_view(ctx.customer_id, created, with_observations=True)


@router.get("/changes/{change_id}/architecture-review/handoff")
def design_handoff(change_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    """What the Functional and Technical agents receive: the Architect's
    instructions with the same evidence manifest."""
    _require_change(change_id, ctx.customer_id)
    current = baseline.current_for_story(ctx.customer_id, change_id)
    run = get_architecture_review_service().get(change_id)
    if current is None or run is None or not run.history:
        raise HTTPException(status_code=404, detail="no design with an evidence baseline yet")
    latest = run.history[-1]
    return {
        "storyId": change_id, "baselineId": current["baseline_id"], "manifestSha256": current["manifest_sha256"],
        "status": current["status"], "reassessment": current["reassessment"],
        "architectDecision": latest.architect_decision.model_dump(mode="json", by_alias=True),
        "implementationSpec": latest.implementation_spec.model_dump(mode="json", by_alias=True),
        "evidenceManifest": current["manifest"], "note": baseline.HANDOFF_NOTE,
    }

