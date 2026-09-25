"""
Process frameworks (Admin), a story's process mapping and maps, and its
as-built record.

Authority comes from the session. Administrators import and activate
frameworks. A story's processes, maps and as-built finalisation are
decided by a Product Manager, or by the Domain Owner assigned to the
story's business domain. Another company's story or framework is simply
not found.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response

from ..config import settings
from ..dependencies import (AuthContext, require_customer_access, require_domain_owner_access, require_role,
                            require_write_access)
from ..models.base import ApiModel
from ..process import agent, asbuilt, framework, maps, refinement, story as story_process
from ..services import membership_service
from ..services.registry import get_change_service, get_customer_link_service, get_domain_review_service

router = APIRouter(tags=["process"])
_REFUSALS = (framework.FrameworkRefused, story_process.MappingRefused, maps.MapRefused, asbuilt.AsBuiltRefused,
             refinement.RefinementRefused)


def _refusal(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409 if "meanwhile" in str(exc) else 422, detail=str(exc))


def _decode(b64: str) -> bytes:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="the file content is not valid base64") from exc


def _change(story_id: str, company_id: str):
    if get_customer_link_service().customer_for(story_id) != company_id:
        raise HTTPException(status_code=404, detail=f"no such change: {story_id}")
    return get_change_service().get_for_customer(story_id, company_id)


def _domain(story_id: str) -> Optional[str]:
    review = get_domain_review_service().get(story_id)
    return review.business_domain_id if review else None


def _require_reviewer(ctx: AuthContext, story_id: str) -> None:
    """A Product Manager, or the Domain Owner assigned to this story's domain
    -- re-read from the database, not from the request's cached roles."""
    roles = membership_service.roles_for(ctx.identity.id, ctx.customer_id)
    if "product_manager" in roles:
        return
    if "domain_owner" in roles:
        require_domain_owner_access(ctx, _domain(story_id))
        return
    raise HTTPException(status_code=403, detail="requires a Product Manager, or the Domain Owner assigned to this "
                                                "story's business domain")


def _can_review(ctx: AuthContext, story_id: str) -> bool:
    try:
        _require_reviewer(ctx, story_id)
        return True
    except HTTPException:
        return False


# ---------------------------------------------------------------------
# Frameworks (Admin > Process Framework)
# ---------------------------------------------------------------------
class FileInput(ApiModel):
    file_name: str
    content_base64: str


class DraftInput(FileInput):
    name: str
    source_kind: Literal["apqc_authorised", "customer_defined", "synthetic_fixture"]
    source_statement: str = ""
    sheet: str
    mapping: dict[str, Optional[str]]
    derive_parent: bool = False
    framework_id: Optional[str] = None


class RemapInput(ApiModel):
    sheet: str
    mapping: dict[str, Optional[str]]
    derive_parent: bool = False


class SelectInput(ApiModel):
    selected_framework_id: str
    expected_revision: int


@router.get("/process/frameworks")
def list_frameworks(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return {"frameworks": framework.list_frameworks(ctx.customer_id), "settings": framework.settings(ctx.customer_id),
            "source_kinds": framework.SOURCE_KINDS, "fields": list(framework.FIELDS),
            "field_labels": framework.FIELD_LABELS, "required_fields": list(framework.REQUIRED_FIELDS)}


@router.get("/process/template")
def template(ctx: AuthContext = Depends(require_customer_access)) -> Response:
    return Response(framework.template_bytes(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="jade_process_framework_template.xlsx"'})


@router.post("/process/frameworks/inspect")
def inspect(payload: FileInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return framework.inspect(_decode(payload.content_base64))
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.post("/process/frameworks/drafts")
def create_draft(payload: DraftInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return framework.create_draft(
            ctx.customer_id, name=payload.name, source_kind=payload.source_kind,
            source_statement=payload.source_statement, file_name=payload.file_name,
            data=_decode(payload.content_base64), sheet=payload.sheet, mapping=payload.mapping,
            derive_parent=payload.derive_parent, actor=ctx.identity.display_name, framework_id=payload.framework_id)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)


@router.post("/process/frameworks/{framework_id}/draft/remap")
def remap(framework_id: str, payload: RemapInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return framework.remap(ctx.customer_id, framework_id, sheet=payload.sheet, mapping=payload.mapping,
                               derive_parent=payload.derive_parent, actor=ctx.identity.display_name)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)


@router.get("/process/frameworks/{framework_id}/versions/{version}")
def version_view(framework_id: str, version: int, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    try:
        return framework.preview(ctx.customer_id, framework_id, version)
    except LookupError as exc:
        raise _refusal(exc)


@router.get("/process/frameworks/{framework_id}/versions/{version}/file")
def original_file(framework_id: str, version: int, ctx: AuthContext = Depends(require_customer_access)) -> Response:
    try:
        name, data = framework.original_file(ctx.customer_id, framework_id, version)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/process/frameworks/{framework_id}/versions/{version}/activate")
def activate(framework_id: str, version: int, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return framework.activate(ctx.customer_id, framework_id, version, actor=ctx.identity.display_name)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)


@router.put("/process/settings")
def select(payload: SelectInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return framework.select(ctx.customer_id, payload.selected_framework_id,
                                expected_revision=payload.expected_revision, actor=ctx.identity.display_name)
    except _REFUSALS as exc:
        raise _refusal(exc)


@router.get("/process/frameworks/{framework_id}/nodes/{node_key}/stories")
def node_stories(framework_id: str, node_key: str, ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    if framework.get_framework(ctx.customer_id, framework_id) is None:
        raise HTTPException(status_code=404, detail="no such framework")
    links = get_customer_link_service()
    return [s for s in story_process.stories_for_node(ctx.customer_id, framework_id, node_key)
            if links.customer_for(s["story_id"]) == ctx.customer_id]


# ---------------------------------------------------------------------
# A story's processes and maps
# ---------------------------------------------------------------------
class MappingInput(ApiModel):
    status: Literal["confirmed", "no_mapping"]
    refs: list[dict] = []
    no_mapping_reason: str = ""
    findings: dict = {}
    analysis_run_id: Optional[str] = None
    note: str = ""
    expected_revision: int


class MapInput(ApiModel):
    content: dict
    note: str = ""
    expected_version: int


def _design_summary(company_id: str, story_id: str) -> Optional[dict]:
    from ..discovery import baseline

    b = baseline.current_for_story(company_id, story_id)
    if b is None:
        return None
    pc = b["manifest"].get("process_context") or {}
    return {"baseline_id": b["baseline_id"], "design_revision": b["design_revision"], "status": b["status"],
            "reassessment": b["reassessment"], "process_context_recorded": pc.get("fingerprint"),
            "process_context_consulted": pc.get("consulted", False), "architect_process_findings": pc.get("findings")}


@router.get("/changes/{story_id}/process")
def story_process_view(story_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    sel = framework.selected_active(ctx.customer_id)
    return {
        "story_id": story_id, "title": change.title if change else story_id,
        "business_domain_id": _domain(story_id),
        "route": change.architect_decision.recommended_route if change and change.architect_decision else None,
        "framework": None if sel is None else {
            "framework_id": sel["framework"]["framework_id"], "name": sel["framework"]["name"],
            "source_kind": sel["framework"]["source_kind"], "source_label": sel["framework"]["source_label"],
            "version": sel["version"]["version"]},
        "runs": story_process.runs(ctx.customer_id, story_id),
        "mapping": story_process.mapping_view(ctx.customer_id, story_id),
        "mapping_history": story_process.mappings(ctx.customer_id, story_id),
        "maps": {k: {"versions": maps.versions(ctx.customer_id, story_id, k)} for k in maps.KINDS},
        "fingerprint": story_process.fingerprint(ctx.customer_id, story_id),
        "design": _design_summary(ctx.customer_id, story_id),
        "can_review": _can_review(ctx, story_id),
    }


@router.post("/changes/{story_id}/process/analysis", status_code=202)
def start_analysis(story_id: str, background: BackgroundTasks,
                   ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Start the refinement process analysis (the real agent runtime)."""
    change = _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    try:
        run = story_process.start_analysis(ctx.customer_id, story_id, initiated_by=ctx.identity.id)
    except _REFUSALS as exc:
        raise _refusal(exc)

    def _go() -> None:
        asyncio.run(agent.run_process_analysis(company_id=ctx.customer_id, story_id=story_id, run_id=run["run_id"],
                                               change=change, repo_root=settings.repo_root))

    background.add_task(_go)
    return run


@router.post("/changes/{story_id}/process/mapping")
def decide_mapping(story_id: str, payload: MappingInput, ctx: AuthContext = Depends(require_write_access)) -> dict:
    _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    try:
        story_process.decide(ctx.customer_id, story_id, status=payload.status, refs=payload.refs,
                             no_mapping_reason=payload.no_mapping_reason, findings=payload.findings,
                             analysis_run_id=payload.analysis_run_id, note=payload.note,
                             reviewer_name=ctx.identity.display_name, reviewer_user_id=ctx.identity.id,
                             roles=sorted(membership_service.roles_for(ctx.identity.id, ctx.customer_id)),
                             expected_revision=payload.expected_revision)
    except _REFUSALS as exc:
        raise _refusal(exc)
    return story_process_view(story_id, ctx)


@router.put("/changes/{story_id}/process/maps/{kind}")
def save_map(story_id: str, kind: Literal["as_is", "to_be"], payload: MapInput,
             ctx: AuthContext = Depends(require_write_access)) -> dict:
    _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    links = get_customer_link_service()
    try:
        saved = maps.save(ctx.customer_id, story_id, kind, payload.content, note=payload.note,
                          actor=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                          expected_version=payload.expected_version,
                          story_exists=lambda sid: links.customer_for(sid) == ctx.customer_id)
    except _REFUSALS as exc:
        raise _refusal(exc)
    return {"saved": saved, "view": story_process_view(story_id, ctx)}


# ---------------------------------------------------------------------
# Story refinement from findings
# ---------------------------------------------------------------------
class FindingSelection(ApiModel):
    finding_ids: list[str]


class ApplyInput(FindingSelection):
    note: str = ""
    expected_revision: int


class FindingStatusInput(ApiModel):
    status: Literal["rejected", "deferred", "proposed"]
    reason: str = ""


def _current_story(change):
    from ..models.change import UserStory

    return change.user_story if change and change.user_story else UserStory(statement=change.title if change else "")


@router.get("/changes/{story_id}/process/refinement")
def refinement_view(story_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    return {**refinement.view(ctx.customer_id, story_id), "story": _current_story(change).model_dump(mode="json"),
            "can_review": _can_review(ctx, story_id)}


@router.post("/changes/{story_id}/process/refinement/preview")
def refinement_preview(story_id: str, payload: FindingSelection, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    try:
        p = refinement.preview(ctx.customer_id, story_id, _current_story(change), payload.finding_ids)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)
    return {"diff": p["diff"], "story": p["story"].model_dump(mode="json")}


@router.post("/changes/{story_id}/process/refinement/apply")
def refinement_apply(story_id: str, payload: ApplyInput, ctx: AuthContext = Depends(require_write_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    try:
        refinement.apply(ctx.customer_id, story_id, _current_story(change), payload.finding_ids,
                         expected_revision=payload.expected_revision, note=payload.note,
                         actor=ctx.identity.display_name, actor_user_id=ctx.identity.id,
                         roles=sorted(membership_service.roles_for(ctx.identity.id, ctx.customer_id)))
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)
    return refinement_view(story_id, ctx)


@router.post("/changes/{story_id}/process/refinement/findings/{finding_id}")
def refinement_finding_status(story_id: str, finding_id: str, payload: FindingStatusInput,
                              ctx: AuthContext = Depends(require_write_access)) -> dict:
    _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    try:
        refinement.set_status(ctx.customer_id, story_id, finding_id, status=payload.status, reason=payload.reason,
                              actor=ctx.identity.display_name, actor_user_id=ctx.identity.id)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)
    return refinement_view(story_id, ctx)


# ---------------------------------------------------------------------
# As-built record
# ---------------------------------------------------------------------
@router.get("/changes/{story_id}/as-built")
def as_built(story_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    current = asbuilt.build(ctx.customer_id, story_id, change)["content"]
    return {"story_id": story_id, "records": asbuilt.records(ctx.customer_id, story_id),
            "checkpoints_now": current["checkpoints"], "delivery_mode_now": current["delivery_mode"],
            "can_finalise": _can_review(ctx, story_id)}


@router.post("/changes/{story_id}/as-built")
def generate_as_built(story_id: str, ctx: AuthContext = Depends(require_write_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    return asbuilt.generate(ctx.customer_id, story_id, change, actor=ctx.identity.display_name)


@router.post("/changes/{story_id}/as-built/{version}/finalise")
def finalise_as_built(story_id: str, version: int, ctx: AuthContext = Depends(require_write_access)) -> dict:
    change = _change(story_id, ctx.customer_id)
    _require_reviewer(ctx, story_id)
    try:
        return asbuilt.finalise(ctx.customer_id, story_id, version, change, actor=ctx.identity.display_name)
    except (*_REFUSALS, LookupError) as exc:
        raise _refusal(exc)


@router.get("/changes/{story_id}/as-built/{version}/markdown")
def as_built_markdown(story_id: str, version: int, ctx: AuthContext = Depends(require_customer_access)) -> Response:
    _change(story_id, ctx.customer_id)
    rec = asbuilt.get(ctx.customer_id, story_id, version)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"no as-built version {version}")
    return Response(rec["markdown"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{story_id}_as_built_v{version}.md"'})
