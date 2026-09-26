from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response

from ..dependencies import AuthContext, require_customer_access, require_write_access
from ..knowledge import attachments
from ..models.base import ApiModel
from ..models.change_request import ChangeRequest, ChangeRequestCreate
from ..services.registry import get_change_request_service

router = APIRouter(tags=["change-requests"])


@router.post("/change-requests", response_model=ChangeRequest, status_code=201)
def create_change_request(
    payload: ChangeRequestCreate,
    ctx: AuthContext = Depends(require_write_access),
) -> ChangeRequest:
    """Direct text entry, optionally with documents uploaded beforehand.
    customer_id comes from the already-validated X-Customer-Id, and
    requester comes from the resolved identity -- neither is accepted from
    the request body, so a caller cannot create a request on behalf of
    another customer or person just by putting different values in the
    JSON. Attachments are checked BEFORE the request is created, so a bad
    attachment refuses the whole submission rather than half-saving it."""
    try:
        attachments.validate_for_request(ctx.customer_id, payload.attachment_ids, user_id=ctx.identity.id)
    except attachments.AttachmentRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    created = get_change_request_service().create_direct(
        payload, customer_id=ctx.customer_id, requester=ctx.identity.display_name
    )
    if payload.attachment_ids:
        attachments.link(ctx.customer_id, created.id, payload.attachment_ids, user_id=ctx.identity.id)
    return created


# -- Documents on a request -----------------------------------------------------------
class AttachmentUpload(ApiModel):
    filename: str
    content_base64: str


def _request(request_id: str, ctx: AuthContext) -> None:
    cr = get_change_request_service().get(request_id)
    if cr is None or cr.customer_id != ctx.customer_id:
        raise HTTPException(status_code=404, detail=f"no such request: {request_id}")


@router.get("/change-requests/attachments/limits")
def attachment_limits(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return attachments.LIMITS


@router.post("/change-requests/attachments", status_code=201)
def upload_attachment(payload: AttachmentUpload, background: BackgroundTasks,
                      ctx: AuthContext = Depends(require_write_access)) -> dict:
    """A pending upload: stored privately and read in the background. It
    becomes part of a request only when the request is submitted with it.
    Uploading never sends anything to an AI model."""
    try:
        data = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=422, detail="the file content is not valid base64")
    try:
        created = attachments.upload(ctx.customer_id, payload.filename, data, user_id=ctx.identity.id,
                                     user_name=ctx.identity.display_name)
    except attachments.AttachmentRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    background.add_task(attachments.run_extraction, ctx.customer_id, created["id"])
    return created


def _own_pending(attachment_id: str, ctx: AuthContext) -> dict:
    try:
        a = attachments.get(ctx.customer_id, attachment_id)
    except attachments.NotFound:
        raise HTTPException(status_code=404, detail="no such upload")
    return a


@router.get("/change-requests/attachments/{attachment_id}")
def get_upload(attachment_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    a = _own_pending(attachment_id, ctx)
    if a["requestId"] is None and a["status"] != "pending":
        raise HTTPException(status_code=404, detail="no such upload")
    return a


@router.delete("/change-requests/attachments/{attachment_id}", status_code=204)
def remove_upload(attachment_id: str, ctx: AuthContext = Depends(require_write_access)) -> Response:
    try:
        attachments.remove_pending(ctx.customer_id, attachment_id, user_id=ctx.identity.id)
    except (attachments.NotFound, attachments.AttachmentRejected):
        raise HTTPException(status_code=404, detail="no such pending upload")
    return Response(status_code=204)


@router.get("/change-requests/{request_id}/attachments")
def list_attachments(request_id: str, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    _request(request_id, ctx)
    return {"attachments": attachments.list_for_request(ctx.customer_id, request_id), "limits": attachments.LIMITS}


@router.get("/change-requests/{request_id}/attachments/{attachment_id}/download")
def download_attachment(request_id: str, attachment_id: str,
                        ctx: AuthContext = Depends(require_customer_access)) -> Response:
    _request(request_id, ctx)
    try:
        meta, data = attachments.download(ctx.customer_id, attachment_id, request_id=request_id)
    except (attachments.NotFound, attachments.AttachmentRejected, OSError):
        raise HTTPException(status_code=404, detail="no such attachment")
    media = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}[meta["fileType"]]
    safe = meta["filename"].replace('"', "")
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})


@router.delete("/change-requests/{request_id}/attachments/{attachment_id}")
def delete_attachment(request_id: str, attachment_id: str, ctx: AuthContext = Depends(require_write_access)) -> dict:
    _request(request_id, ctx)
    try:
        items = attachments.remove_from_request(ctx.customer_id, request_id, attachment_id,
                                                actor=ctx.identity.display_name)
    except attachments.NotFound:
        raise HTTPException(status_code=404, detail="no such attachment")
    return {"attachments": items, "limits": attachments.LIMITS}


@router.post("/change-requests/{request_id}/attachments/{attachment_id}/retry")
def retry_extraction(request_id: str, attachment_id: str, background: BackgroundTasks,
                     ctx: AuthContext = Depends(require_write_access)) -> dict:
    _request(request_id, ctx)
    try:
        a = attachments.get(ctx.customer_id, attachment_id)
    except attachments.NotFound:
        raise HTTPException(status_code=404, detail="no such attachment")
    if a["requestId"] != request_id or a["deletedAt"]:
        raise HTTPException(status_code=404, detail="no such attachment")
    if a["extractionStatus"] in ("pending", "extracting"):
        raise HTTPException(status_code=409, detail="the document is already being read")
    background.add_task(attachments.run_extraction, ctx.customer_id, attachment_id)
    return {**a, "extractionStatus": "pending", "extractionDetail": "waiting to be read again"}
