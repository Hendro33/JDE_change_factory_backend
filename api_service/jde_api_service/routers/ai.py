"""
Admin > AI Connections and Admin > Agent Configuration.

  * The customer's AI connection (Anthropic API, company-owned key): Admins
    only. The key is write-only; saving never contacts the provider; the
    connection test is a separate action that must be confirmed as billable.
  * Start-up Packs: list, read, copy, edit drafts, publish, disable, assign
    a published revision per role (assigning an earlier one = rollback).
  * Runs and health: what every agent run actually used, and per role
    whether it is configured, tested, working, disabled or failing.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from ..ai import connection as ai_connection
from ..ai import packs as ai_packs
from ..ai import runtime as ai_runtime
from ..dependencies import AuthContext, require_customer_access, require_role
from ..models.base import ApiModel
from ..services import credential_crypto

router = APIRouter(tags=["ai"])


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


class ConnectionInput(ApiModel):
    model: str
    enabled: bool = True
    document_policy: str = "metadata_only"
    limits: dict[str, Any] = {}
    expected_revision: Optional[int] = None


class CredentialInput(ApiModel):
    api_key: str


class TestInput(ApiModel):
    confirm_billable: bool = False


@router.get("/admin/ai/connection")
def get_connection(ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    return {**ai_connection.view(ctx.customer_id), "testExplanation": ai_connection.TEST_EXPLANATION,
            "serverKeyConfigured": credential_crypto.is_configured()}


@router.put("/admin/ai/connection")
def put_connection(payload: ConnectionInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        ai_connection.save(ctx.customer_id, model=payload.model, enabled=payload.enabled,
                           document_policy=payload.document_policy, limits=payload.limits,
                           expected_revision=payload.expected_revision, actor=ctx.identity.display_name)
    except ai_connection.InvalidConfig as exc:
        raise _bad(exc)
    return get_connection(ctx)


@router.put("/admin/ai/connection/credential")
def put_credential(payload: CredentialInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        ai_connection.save_credential(ctx.customer_id, payload.api_key, actor=ctx.identity.display_name)
    except ai_connection.InvalidConfig as exc:
        raise _bad(exc)
    except credential_crypto.CredentialKeyMissing:
        raise HTTPException(status_code=409, detail="the server has no credential encryption key "
                                                    "(JDE_CREDENTIAL_KEY), so the API key cannot be stored")
    return get_connection(ctx)


@router.delete("/admin/ai/connection/credential")
def revoke_credential(ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        ai_connection.revoke_credential(ctx.customer_id, actor=ctx.identity.display_name)
    except ai_connection.InvalidConfig as exc:
        raise _bad(exc)
    return get_connection(ctx)


@router.post("/admin/ai/connection/test")
def test_connection(payload: TestInput, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    if not payload.confirm_billable:
        raise HTTPException(status_code=428, detail=f"confirm the billable test first: {ai_connection.TEST_EXPLANATION}")
    try:
        return ai_connection.test_connection(ctx.customer_id, actor=ctx.identity.display_name)
    except ai_connection.InvalidConfig as exc:
        raise _bad(exc)
    except credential_crypto.CredentialUnreadable:
        raise HTTPException(status_code=409, detail="the stored API key cannot be decrypted; enter it again")


# -- Start-up Packs ---------------------------------------------------------------------
class PackCreate(ApiModel):
    role: str
    name: str = ""
    from_pack_id: str
    from_revision: int


class PackDraft(ApiModel):
    content: dict[str, Any]
    note: str = ""


class PackAssign(ApiModel):
    pack_id: str
    revision: int
    expected_version: Optional[int] = None


class PackDisable(ApiModel):
    disabled: bool


@router.get("/admin/ai/roles")
def roles(ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    return [{"role": r, "label": info["label"], "ceiling": list(ai_packs.ceiling(r))}
            for r, info in ai_packs.ROLES.items()]


@router.get("/admin/ai/packs")
def list_packs(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return {"packs": ai_packs.list_packs(ctx.customer_id), "assignments": ai_packs.assignments(ctx.customer_id)}


@router.get("/admin/ai/packs/{pack_id}/revisions/{revision}")
def get_pack_revision(pack_id: str, revision: int, ctx: AuthContext = Depends(require_customer_access)) -> dict:
    try:
        return ai_packs.get_revision(ctx.customer_id, pack_id, revision)
    except ai_packs.InvalidPack as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/admin/ai/packs", status_code=201)
def create_pack(payload: PackCreate, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.create_pack(ctx.customer_id, role=payload.role, name=payload.name,
                                    from_pack_id=payload.from_pack_id, from_revision=payload.from_revision,
                                    actor=ctx.identity.display_name)
    except ai_packs.InvalidPack as exc:
        raise _bad(exc)


@router.put("/admin/ai/packs/{pack_id}/draft")
def save_draft(pack_id: str, payload: PackDraft, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.save_draft(ctx.customer_id, pack_id, payload.content, actor=ctx.identity.display_name,
                                   note=payload.note)
    except ai_packs.InvalidPack as exc:
        raise _bad(exc)


@router.post("/admin/ai/packs/{pack_id}/revisions/{revision}/publish")
def publish(pack_id: str, revision: int, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.publish(ctx.customer_id, pack_id, revision, actor=ctx.identity.display_name)
    except ai_packs.InvalidPack as exc:
        raise _bad(exc)


@router.put("/admin/ai/packs/{pack_id}/disabled")
def set_disabled(pack_id: str, payload: PackDisable, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.set_disabled(ctx.customer_id, pack_id, payload.disabled, actor=ctx.identity.display_name)
    except ai_packs.InvalidPack as exc:
        raise _bad(exc)


@router.put("/admin/ai/assignments/{role}")
def assign(role: str, payload: PackAssign, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.assign(ctx.customer_id, role, payload.pack_id, payload.revision,
                               actor=ctx.identity.display_name, expected_version=payload.expected_version)
    except ai_packs.InvalidPack as exc:
        raise HTTPException(status_code=409 if "someone else" in str(exc) else 422, detail=str(exc))


@router.delete("/admin/ai/assignments/{role}")
def unassign(role: str, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        return ai_packs.unassign(ctx.customer_id, role, actor=ctx.identity.display_name)
    except ai_packs.InvalidPack as exc:
        raise _bad(exc)


@router.get("/admin/ai/audit")
def audit(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return {"packs": ai_packs.audit(ctx.customer_id)}


# -- Runs and health ------------------------------------------------------------------------
@router.get("/admin/ai/runs")
def runs(ctx: AuthContext = Depends(require_customer_access)) -> list[dict]:
    return ai_runtime.list_runs(ctx.customer_id)


@router.get("/admin/ai/health")
def health(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return {"roles": ai_runtime.health(ctx.customer_id), "runtime": ai_runtime.ADAPTER.name,
            "provider": ai_connection.PROVIDER_LABEL}
