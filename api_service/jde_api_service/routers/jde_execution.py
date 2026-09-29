"""
Agent execution: Administration > Systems & Connections > JDE (the
settings, the DEV write user, Test and the on/off switches) and the
agents' side of delivery (run the agents, reconcile an item).

Settings are Admin-only and never return the password. Switching agent
execution on or off is recorded with name and date.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from jde_mcp_server import approval, authority, config_items, execution
from jde_mcp_server.binding import BindingInvalid
from jde_mcp_server.capability_catalog import CapabilityError
from jde_mcp_server.scope import ScopeViolation

from ..dependencies import AuthContext, require_customer_access, require_role, require_write_access
from ..executors import checks, routes, runner, settings
from ..models.base import ApiModel
from ..services import credential_crypto

router = APIRouter(tags=["jde-execution"])


def _mask(username: Optional[str]) -> Optional[str]:
    return (username[:2] + "•" * max(3, len(username) - 2)) if username else None


def view(company_id: str) -> dict[str, Any]:
    from jde_mcp_server import capability_catalog

    from ..discovery import profile_service
    from ..executors import browser

    s = settings.load(company_id)
    profile = profile_service.load(company_id)
    pc = profile["config"] if profile else None
    caps = []
    for cid in routes.switchable_capabilities():
        cap = capability_catalog.get_capability(cid) or {}
        on, why = settings.capability_enabled(s, cid)
        caps.append({"capabilityId": cid, "title": (cap.get("identity") or {}).get("description", cid),
                     "itemKind": (cap.get("enforcement") or {}).get("item_kind"), "enabled": on, "detail": why})
    route_state = {}
    for r in (routes.AIS, routes.BROWSER):
        ready, why = routes.readiness(company_id, r)
        route_state[r] = {"ready": ready, "detail": why, "label": routes.ROUTE_LABELS[r]}
    b_ok, b_why = browser.available()
    out: dict[str, Any] = {
        "configured": s is not None, "revision": s["revision"] if s else 0,
        "config": s["config"].model_dump(mode="json", by_alias=True) if s else settings.ExecutionConfig().model_dump(
            mode="json", by_alias=True),
        "writeUserConfigured": bool(s and s.get("credential_secret")),
        "writeUserMasked": _mask(s.get("credential_username")) if s else None,
        "writeUserStorage": settings.credential_storage(s),
        "agentExecutionEnabled": bool(s["agent_execution_enabled"]) if s else True,
        "capabilities": caps,
        "checks": {c: settings.check_state(s, c) for c in settings.CHECKS} if s else {},
        "routes": route_state,
        "browserAvailable": b_ok, "browserDetail": b_why,
        "connection": {"aisBaseUrl": pc.ais_base_url if pc else "", "environment": pc.environment if pc else "",
                       "pathCode": pc.path_code if pc else "", "discoveryRole": pc.role if pc else "",
                       "discoveryUserMasked": _mask(profile.get("credential_username")) if profile else None,
                       "discoveryEnabled": profile_service.is_active(profile)},
        "updatedAt": s["updated_at"] if s else None, "updatedBy": s["updated_by"] if s else None,
        "audit": settings.audit_log(company_id, 30),
    }
    return out


@router.get("/admin/jde/execution")
def get_execution(ctx: AuthContext = Depends(require_customer_access)) -> dict:
    return view(ctx.customer_id)


@router.put("/admin/jde/execution")
def save_execution(payload: settings.ExecutionSettingsUpdate, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    config = settings.ExecutionConfig.model_validate(payload.model_dump(exclude={"expected_revision"}))
    if config.web_ca_certificate_sha256:
        from ..discovery import certificates

        if certificates.get(ctx.customer_id, config.web_ca_certificate_sha256) is None:
            raise HTTPException(status_code=422, detail="the selected web client certificate is not one uploaded for "
                                                        "this customer")
    settings.save(ctx.customer_id, config, expected_revision=payload.expected_revision,
                  actor=ctx.identity.display_name)
    return view(ctx.customer_id)


@router.put("/admin/jde/execution/write-user")
def save_write_user(payload: settings.WriteCredentialUpdate, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    from ..discovery import profile_service

    profile = profile_service.load(ctx.customer_id)
    if profile and (profile.get("credential_username") or "").strip().upper() == payload.username.strip().upper():
        raise HTTPException(status_code=422, detail="the DEV write user must be a different user than the read-only "
                                                    "discovery user")
    try:
        settings.save_credential(ctx.customer_id, payload.username, payload.password,
                                 expected_revision=payload.expected_revision, actor=ctx.identity.display_name)
    except credential_crypto.CredentialKeyMissing as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except settings.SettingsNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return view(ctx.customer_id)


@router.post("/admin/jde/execution/test")
def test_execution(ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        results = checks.test(ctx.customer_id)
    except settings.SettingsNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    settings.audit(ctx.customer_id, "test_requested", "settings tested", ctx.identity.display_name)
    return {"results": results, "execution": view(ctx.customer_id)}


@router.put("/admin/jde/execution/switch")
def set_switch(payload: settings.SwitchUpdate, ctx: AuthContext = Depends(require_role("admin"))) -> dict:
    try:
        settings.set_switch(ctx.customer_id, payload, actor=ctx.identity.display_name)
    except settings.SettingsNotFound as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return view(ctx.customer_id)


# ---------------------------------------------------------------------
# Delivery: the agents' side
# ---------------------------------------------------------------------
_REFUSALS = (approval.ChangeApprovalError, BindingInvalid, ScopeViolation, CapabilityError, runner.RunRefused,
             authority.AuthorityRevoked, authority.AuthorityUnverifiable)


def _own_change_set(change_id: str, ctx: AuthContext) -> dict:
    from .architecture_review import _own_change_record

    record = _own_change_record(change_id, ctx)
    if not config_items.is_change_set(record):
        raise HTTPException(status_code=409, detail="only a configuration change set is delivered by the agents")
    return record


@router.post("/changes/{change_id}/delivery/agents/run", status_code=202)
def run_agents(change_id: str, background: BackgroundTasks, ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Start (or resume) the agents on the approved change set. Every item
    goes through the whole delivery gate; the result shows on the change."""
    record = _own_change_set(change_id, ctx)
    try:
        runner._authorise(record["change_id"], ctx.identity.id, ctx.identity.display_name)
    except approval.ApproverNotAuthorised as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except _REFUSALS as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # A person asked: an item the agent stopped on before sending anything is tried again.
    background.add_task(runner.run_in_background, record["change_id"], ctx.identity.id, ctx.identity.display_name,
                        True)
    return {"status": "started", "changeId": record["change_id"]}


@router.get("/changes/{change_id}/delivery/screenshots")
def screenshot(change_id: str, key: str, ctx: AuthContext = Depends(require_customer_access)) -> Response:
    """One screenshot the browser executor stored as evidence for this
    change -- this customer's and this change's only."""
    from .architecture_review import _own_change_record
    from ..persistence import blob_store

    record = _own_change_record(change_id, ctx)
    prefix = f"executions/{ctx.customer_id}/{record['change_id']}/"
    if not key.startswith(prefix) or not key.endswith(".png") or ".." in key:
        raise HTTPException(status_code=404, detail="no such screenshot for this change")
    try:
        data = blob_store.default().get(key)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="no such screenshot for this change")
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


class ReconcileItemInput(ApiModel):
    note: str = ""
    evidence_reference: str = ""
    # When Jade cannot read the item live: what a person saw in JDE.
    stated_value: Optional[str] = None
    stated_values: Optional[dict] = None
    stated_specification: Optional[str] = None


@router.post("/changes/{change_id}/delivery/items/{item_id}/reconcile")
def reconcile_item(change_id: str, item_id: str, payload: ReconcileItemInput,
                   ctx: AuthContext = Depends(require_write_access)) -> dict:
    """Settle an item of unknown outcome by its ACTUAL state in DEV: read
    live where the connection can, otherwise what a person saw in JDE with a
    note and an evidence reference."""
    from ..delivery.functional import matches_approved
    from ..delivery.readers import read_item
    from .architecture_review import _require_policy_approver

    _require_policy_approver(ctx)
    record = _own_change_set(change_id, ctx)
    try:
        item = config_items.item(record, item_id)
    except config_items.ItemInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    now = read_item(record, item, ctx.identity.id)
    if now.get("known"):
        observed, source = now["value"], now.get("source", "live AIS read")
        evidence = f"{source} of {item_id}"
    else:
        if not payload.note.strip() or not payload.evidence_reference.strip():
            raise HTTPException(status_code=422, detail=f"Jade cannot read {item_id} live ({now.get('reason')}). State "
                                                        "what you see in JDE, with a note and an evidence reference.")
        if item["kind"] == "processing_option":
            observed = payload.stated_value
        elif item["kind"] in config_items.ROW_KINDS:
            stated = {str(k).upper(): v for k, v in (payload.stated_values or {}).items()}
            observed = {"exists": bool(stated), "values": stated}
        else:
            observed = {"specification": payload.stated_specification or ""}
        source, evidence = "human-verified in JDE", payload.evidence_reference
    before = (((record.get("binding") or {}).get("before_state") or {}).get("items") or {}).get(item_id) or {}
    try:
        entry = execution.reconcile_item(
            record["change_id"], item_id, observed=observed, matches_approved=matches_approved(item, observed),
            matches_before=bool(before.get("known")) and observed == before.get("value"), source=source,
            actor_user_id=ctx.identity.id, actor_name=ctx.identity.display_name, evidence_reference=evidence,
            note=payload.note)
    except execution.ExecutionBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except approval.ChangeApprovalError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"outcome": entry["outcome"], "observed": observed, "source": source,
            "evidenceEntryHash": entry["evidence_entry_hash"]}
