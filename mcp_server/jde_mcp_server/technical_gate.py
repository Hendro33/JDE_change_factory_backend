"""
The governed executor for Technical implementation packages.

A Technical Agent prepares a package (api_service stores it, immutably, per
revision). A person approves THAT exact package revision -- its content hash
-- as an exact change (kind "technical"). Everything that then happens to
the customer's DEV system goes through this module, never through the agent:

    apply   -> the candidate source is checked in (not active)     [WRITE]
    build   -> the checked-in source is built                      [BUILD]
    CNC     -> a human CNC deploys/activates the built package     [recorded, never automated]
    verify  -> the approved test plan runs against the active DEV  [TEST]

Each milestone is recorded separately and is never implied by another.
Before every dispatch the executor re-derives, inside the change's lock:
the approval (approved, unexpired, approver's CURRENT authority), that the
package about to run is byte-for-byte the approved one and is not
superseded, the approval's basis (design revision and design approval, no
invalidation, the objects still in their approved before-state), the
company's scope (DEV binding, authorised object types, customer system
codes 55-59), and that an adapter is qualified for this mode.

Only the SIMULATION adapter exists (technical_sim.py). The live adapter is
unavailable until the customer's actual mechanism is qualified; there are
no speculative JDE import or edit commands here. Execution credentials are
never needed or read by this module.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from . import approval, authority, binding, capability_catalog, execution, technical_sim
from .approval import ChangeApprovalError
from .backlog import require_approved
from .config import settings
from .scope import (
    ScopeViolation,
    check_custom_product_code,
    check_environment_binding,
    check_technical_scope,
    company_for_story,
    load_company_scope,
    scope_revision,
)

CAPABILITY_ID = "custom_object_text_change"
TOOL = "apply_technical_package"
APPLY, BUILD, VERIFY = execution.WRITE, execution.BUILD, execution.TEST


class LiveAdapterUnavailable(ChangeApprovalError):
    """No qualified live mechanism exists for this capability."""


def package_sha256(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mode() -> str:
    return "simulation" if settings.mock_mode else "live"


def technical_enforcement() -> dict:
    cap = capability_catalog.require_capability(CAPABILITY_ID)
    enf = cap.get("technical_enforcement")
    if not isinstance(enf, dict) or enf.get("tool") != TOOL:
        raise capability_catalog.CapabilityError(f"{CAPABILITY_ID} has no technical enforcement contract")
    return enf


def adapter_for_mode() -> dict:
    enf = technical_enforcement()
    adapter = (enf.get("adapters") or {}).get(mode()) or {}
    if not adapter.get("available"):
        raise LiveAdapterUnavailable(
            f"no qualified {mode()} adapter for {CAPABILITY_ID}: {adapter.get('reason', 'not available')} -- "
            "application is blocked; investigation and preparation remain possible")
    return adapter


def format_support(fmt: str) -> dict:
    return (technical_enforcement().get("formats") or {}).get(fmt) or {"prepare": False, "apply": {}}


# ---------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------
def check_package_scope(scope: dict, content: dict) -> None:
    check_environment_binding(scope, "DEV")
    bound = ((scope.get("environment") or {}).get("dev_environment_id") or "").upper()
    if content.get("target_environment", "").upper() != bound:
        raise ScopeViolation(f"the package targets {content.get('target_environment')!r}, but the company's scope is "
                             f"bound to {bound!r}")
    tech = scope.get("technical_agent") or {}
    for obj in content.get("objects") or []:
        check_technical_scope(scope, obj["object_type"])
        check_custom_product_code(str(obj.get("system_code", "")))
        reserved = str(tech.get("reserved_product_code") or "").strip()
        if reserved and str(obj.get("system_code")) != reserved:
            raise ScopeViolation(f"{obj['object_name']} is system code {obj.get('system_code')}; this company reserves "
                                 f"{reserved} for Jade's changes")
        prefix = str(tech.get("naming_prefix") or "").strip().upper()
        if prefix and not obj["object_name"].upper().startswith(prefix):
            raise ScopeViolation(f"{obj['object_name']} does not follow the company's naming prefix {prefix!r}")


# ---------------------------------------------------------------------
# Proposal (after api_service has stored the package revision)
# ---------------------------------------------------------------------
def propose(story_id: str, package: dict) -> dict:
    """The exact change a person approves: this package revision's hash."""
    require_approved(story_id)
    company_id = company_for_story(story_id)
    content = package["content"]
    if content.get("company_id") != company_id or content.get("story_id") != story_id:
        raise ChangeApprovalError("the package does not belong to this story's company -- refusing")
    if package_sha256(content) != package["content_sha256"]:
        raise ChangeApprovalError("the package content does not match its checksum -- refusing")
    cap = capability_catalog.require_capability(CAPABILITY_ID)
    technical_enforcement()
    operation = {
        "tool": TOOL, "story_id": story_id, "package_id": package["package_id"],
        "package_revision": package["revision"], "package_sha256": package["content_sha256"],
        "objects": [o["object_key"] for o in content["objects"]],
        "design_revision": content["design"]["design_revision"], "baseline_id": content["design"]["baseline_id"],
    }
    change_id = f"{story_id}-TP{package['revision']}-{int(time.time() * 1000)}"
    record = {
        "change_id": change_id, "kind": "technical", "story_id": story_id, "company_id": company_id,
        "operation": operation, "change_hash": approval._hash(operation), "environment": "DEV",
        "capability_id": CAPABILITY_ID, "capability_revision": cap["revision"], "status": "pending",
        "created_at": time.time(), "approved_by": None, "approved_at": None, "expires_at": None,
        "approver_authority": None, "decision_note": None,
        "execution_mode": mode(),
        # What the package depends on: the objects themselves and their
        # object-librarian rows (a discovery refresh of either is material).
        "depends_on_targets": [f"technical_object:{k}" for k in operation["objects"]]
                              + [f"object_librarian:{o['object_name'].upper()}" for o in content["objects"]],
        "depends_on_artifacts": sorted({s["artifact_id"] for s in content.get("sources") or []}),
        "catalog_revision": capability_catalog.catalog_revision(), "scope_revision": scope_revision(company_id),
    }
    approval._save(change_id, record)
    return record


# ---------------------------------------------------------------------
# Authorisation, re-run inside the attempt lock before every dispatch
# ---------------------------------------------------------------------
def authorise(change_id: str, package: dict, *, milestone: str) -> tuple[dict, dict]:
    record, scope = approval._require_live_approval(change_id)
    if record.get("kind") != "technical":
        raise ChangeApprovalError(f"{change_id} is not a technical implementation change")
    op, content = record["operation"], package["content"]
    if (package_sha256(content) != package["content_sha256"] or op.get("package_sha256") != package["content_sha256"]
            or op.get("package_id") != package["package_id"] or op.get("package_revision") != package["revision"]):
        raise ChangeApprovalError(
            f"the package about to run (revision {package['revision']}, sha256 {package['content_sha256'][:12]}) is not "
            f"the exact package approved under {change_id} (revision {op.get('package_revision')}, sha256 "
            f"{str(op.get('package_sha256'))[:12]}) -- a changed package needs its own approval")
    if package.get("superseded_by"):
        raise ChangeApprovalError(f"package revision {package['revision']} is superseded by revision "
                                  f"{package['superseded_by']}; only the latest revision may run")
    if record.get("execution_mode") != mode():
        raise ChangeApprovalError(f"approved for {record.get('execution_mode')} application only; this is {mode()}")
    adapter_for_mode()
    for cand in content.get("candidates") or []:
        if not (format_support(cand.get("format", "")).get("apply") or {}).get(mode()):
            raise LiveAdapterUnavailable(f"{cand.get('format')} cannot be applied in {mode()} mode: no qualified mechanism")
    check_package_scope(scope, content)
    if milestone == APPLY:
        binding.require_valid(record)
        before = (record.get("binding") or {}).get("before_state", {}).get("value") or {}
        for cand in content["candidates"]:
            if before.get(cand["object_key"]) != cand["before_sha256"]:
                raise binding.BindingInvalid(
                    f"stale source: {cand['object_key']}'s active DEV runtime ({str(before.get(cand['object_key']))[:12]}) "
                    f"is not the source the package was prepared from ({cand['before_sha256'][:12]})")
    else:
        found = binding.problems(record, read_current=False)
        if found:
            raise binding.BindingInvalid(f"change {change_id} is not eligible: " + "; ".join(found))
    return record, scope


def _environment(scope: dict) -> str:
    return (scope.get("environment") or {}).get("dev_environment_id") or ""


def _evidence(record: dict, event: str, detail: str, data: dict) -> str:
    from .evidence import capture_evidence

    return capture_evidence(record["story_id"], {
        "event": event, "stage": f"technical_{event}", "actor": data.get("actor", "technical executor"),
        "detail": detail, "change_id": record["change_id"], "package": record["operation"],
        "mode": record.get("execution_mode"), "label": technical_sim.sim_estate.SIMULATION_LABEL
        if record.get("execution_mode") == "simulation" else "live", **data,
    })["entry_hash"]


def _milestone(change_id: str, name: str, entry: dict) -> None:
    with execution._locked(change_id):
        record = approval._load(change_id)
        record.setdefault("milestones", []).append({"milestone": name, "at": time.time(), **entry})
        approval._save(change_id, record)


# ---------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------
def apply(change_id: str, package: dict, *, actor: str) -> dict:
    record, scope = authorise(change_id, package, milestone=APPLY)
    env = _environment(scope)
    before = json.dumps(record["binding"]["before_state"]["value"], sort_keys=True)
    attempt = execution.begin(change_id, APPLY, before_value=before,
                              revalidate=lambda: authorise(change_id, package, milestone=APPLY))
    try:
        for cand in package["content"]["candidates"]:
            technical_sim.adapter_apply(record["company_id"], env, cand["object_key"], cand["text"], change_id=change_id,
                                        package_ref=f"{package['package_id']}@r{package['revision']}")
    except technical_sim.AdapterOutcome as exc:
        execution.finish(change_id, APPLY, attempt, "unknown" if exc.sent else "not_sent", str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 -- after sending, anything unclean is unknown
        execution.finish(change_id, APPLY, attempt, "unknown", f"{type(exc).__name__}: {exc}")
        raise
    execution.finish(change_id, APPLY, attempt, "applied", "checked in to the simulated DEV estate (not active)")
    h = _evidence(record, "applied", f"package {package['package_id']}@r{package['revision']} checked in, not active",
                  {"actor": actor, "candidates": [{"object_key": c["object_key"], "after_sha256": c["after_sha256"]}
                                                  for c in package["content"]["candidates"]]})
    _milestone(change_id, "applied", {"actor": actor, "evidence_entry_hash": h})
    return {"milestone": "applied", "change_id": change_id, "active": False}


def build(change_id: str, package: dict, *, actor: str) -> dict:
    record, scope = authorise(change_id, package, milestone=BUILD)
    if execution.effective_state(record, APPLY) != "applied":
        raise execution.ExecutionBlocked(f"change {change_id}: nothing is known to be applied, so nothing can be built "
                                         f"(apply: {execution.effective_state(record, APPLY)})")
    env = _environment(scope)
    attempt = execution.begin(change_id, BUILD, revalidate=lambda: authorise(change_id, package, milestone=BUILD))
    results = []
    try:
        for cand in package["content"]["candidates"]:
            results.append(technical_sim.adapter_build(record["company_id"], env, cand["object_key"], change_id=change_id,
                                                       context={"story_id": record["story_id"]}))
    except technical_sim.AdapterOutcome as exc:
        execution.finish(change_id, BUILD, attempt, "unknown" if exc.sent else "not_sent", str(exc))
        raise
    except Exception as exc:  # noqa: BLE001
        execution.finish(change_id, BUILD, attempt, "unknown", f"{type(exc).__name__}: {exc}")
        raise
    failed = [r for r in results if r["status"] != "built"]
    log = [line for r in results for line in r["log"]]
    execution.finish(change_id, BUILD, attempt, "failed" if failed else "built", "; ".join(log)[:500] or "built")
    h = _evidence(record, "build_failed" if failed else "built", "simulated build " + ("FAILED" if failed else "succeeded"),
                  {"actor": actor, "log": log})
    _milestone(change_id, "build_failed" if failed else "built", {"actor": actor, "log": log, "evidence_entry_hash": h})
    return {"milestone": "build_failed" if failed else "built", "change_id": change_id, "log": log,
            "next": ("a repair is a new package revision with a fresh approval" if failed
                     else "awaiting human CNC activation" if technical_enforcement().get("requires_cnc_activation")
                     else "ready to verify")}


def record_cnc_activation(change_id: str, package: dict, *, actor_user_id: str, actor_name: str, package_name: str,
                          evidence_reference: str, note: str = "") -> dict:
    """A human CNC deployed the built package. Jade records it; in the
    simulation the recorded hand-off is what makes it active. Only a person
    who holds a CNC activation role in the company right now may record it."""
    record, scope = authorise(change_id, package, milestone="cnc")
    roles = technical_enforcement().get("cnc_activation_roles") or []
    try:
        authority.require_current_approver(actor_user_id, record["company_id"], roles)
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise approval.ApproverNotAuthorised(f"{actor_name} may not record a CNC activation: {exc}") from exc
    if not (evidence_reference or "").strip() or not (package_name or "").strip():
        raise ChangeApprovalError("a CNC activation needs the package name and an evidence reference")
    with execution._locked(change_id):
        current = approval._load(change_id)
        if execution.effective_state(current, BUILD) != "built":
            raise execution.ExecutionBlocked(f"change {change_id}: the package is not known to be built "
                                             f"(build: {execution.effective_state(current, BUILD)}) -- nothing to activate")
        if current.get("cnc_activation"):
            raise execution.ExecutionBlocked(f"change {change_id}: the CNC activation is already recorded")
    env = _environment(scope)
    activated = [technical_sim.adapter_activate(record["company_id"], env, c["object_key"], change_id=change_id,
                                                package_name=package_name, actor=actor_name)
                 for c in package["content"]["candidates"]]
    entry = {"by": actor_name, "user_id": actor_user_id, "package_name": package_name,
             "evidence_reference": evidence_reference.strip(), "note": note, "at": time.time(),
             "simulated": record.get("execution_mode") == "simulation", "activated": activated}
    entry["evidence_entry_hash"] = _evidence(record, "cnc_activation_recorded",
                                             f"CNC activation of package {package_name} recorded by {actor_name}",
                                             {"actor": actor_name, "cnc": {k: v for k, v in entry.items() if k != "activated"}})
    with execution._locked(change_id):
        current = approval._load(change_id)
        current["cnc_activation"] = entry
        current.setdefault("milestones", []).append({"milestone": "cnc_activated", "at": entry["at"], "actor": actor_name,
                                                     "evidence_entry_hash": entry["evidence_entry_hash"]})
        approval._save(change_id, current)
    return entry


def verify(change_id: str, package: dict, *, actor: str) -> dict:
    record, scope = authorise(change_id, package, milestone=VERIFY)
    if execution.effective_state(record, BUILD) != "built":
        raise execution.ExecutionBlocked(f"change {change_id}: the package is not known to be built -- nothing to verify")
    if technical_enforcement().get("requires_cnc_activation") and not record.get("cnc_activation"):
        raise execution.ExecutionBlocked(
            f"change {change_id}: awaiting human CNC activation -- the built package is not active in DEV until a CNC "
            "deploys it and that is recorded; Jade cannot do this step")
    env = _environment(scope)
    attempt = execution.begin(change_id, VERIFY, revalidate=lambda: authorise(change_id, package, milestone=VERIFY))
    results = []
    try:
        for cand in package["content"]["candidates"]:
            tests = [t for t in package["content"].get("test_plan") or [] if t.get("object_key", cand["object_key"]) == cand["object_key"]]
            results += technical_sim.adapter_run_tests(record["company_id"], env, cand["object_key"], tests,
                                                       change_id=change_id)
    except technical_sim.AdapterOutcome as exc:
        execution.finish(change_id, VERIFY, attempt, "unknown" if exc.sent else "not_sent", str(exc))
        raise
    passed = bool(results) and all(r["passed"] for r in results)
    runtime = technical_sim.runtime_state(record["company_id"], env, record["operation"]["objects"])
    approved_after = {c["object_key"]: c["after_sha256"] for c in package["content"]["candidates"]}
    outcome = {"passed": passed, "results": results, "runtime_sha256": runtime,
               "runtime_is_approved_artifact": runtime == approved_after, "at": time.time()}
    execution.finish(change_id, VERIFY, attempt, "completed", f"{sum(r['passed'] for r in results)}/{len(results)} passed")
    outcome["evidence_entry_hash"] = _evidence(
        record, "verified" if passed and outcome["runtime_is_approved_artifact"] else "verification_failed",
        f"{sum(r['passed'] for r in results)}/{len(results)} tests passed; active runtime "
        f"{'IS' if outcome['runtime_is_approved_artifact'] else 'is NOT'} the approved artifact",
        {"actor": actor, "results": results, "runtime_sha256": runtime, "approved_after_sha256": approved_after,
         "package_sha256": package["content_sha256"]})
    with execution._locked(change_id):
        current = approval._load(change_id)
        current["verification"] = outcome
        current.setdefault("milestones", []).append({
            "milestone": "verified" if passed and outcome["runtime_is_approved_artifact"] else "verification_failed",
            "at": outcome["at"], "actor": actor, "evidence_entry_hash": outcome["evidence_entry_hash"]})
        approval._save(change_id, current)
    return outcome


# ---------------------------------------------------------------------
# Reconciliation of an unknown outcome -- by reading the simulated state
# ---------------------------------------------------------------------
def reconcile(change_id: str, package: dict, *, milestone: str, actor_user_id: str, actor_name: str,
              note: str = "") -> dict:
    """Settle an UNKNOWN apply or build by reading the object's actual state.
    Evidence is preserved; nothing is retried here, and the next attempt
    re-runs every check (approval, expiry, scope, before-state)."""
    if milestone not in (APPLY, BUILD):
        raise ChangeApprovalError("only an apply or a build of unknown outcome is reconciled here")
    record = approval._load(change_id)
    scope = load_company_scope(record["company_id"])
    env = _environment(scope)
    with execution._locked(change_id):
        record = approval._load(change_id)
        if execution.effective_state(record, milestone) != "unknown":
            raise execution.ExecutionBlocked(f"change {change_id}: the {milestone} is "
                                             f"{execution.effective_state(record, milestone)}; only an unknown outcome is reconciled")
        block = execution._block(record, milestone)
        observed, outcome, state = {}, "", ""
        for cand in package["content"]["candidates"]:
            obj = technical_sim.get_object(record["company_id"], env, cand["object_key"]) or {}
            checked = (obj.get("checked_in") or {}).get("sha256")
            active = (obj.get("active") or {}).get("sha256")
            observed[cand["object_key"]] = {"checked_in_sha256": checked, "active_sha256": active,
                                            "build": obj.get("build")}
            if milestone == APPLY:
                if checked == cand["after_sha256"]:
                    outcome, state = "applied", "applied"
                elif checked is None and active == cand["before_sha256"]:
                    outcome, state = "not_applied", "ready"
                else:
                    outcome, state = "diverged", "diverged"
            else:
                b = obj.get("build") or {}
                if b.get("sha256") == cand["after_sha256"] and b.get("status") in ("built", "failed"):
                    outcome, state = b["status"], b["status"]
                else:
                    outcome, state = "not_built", "ready"
        entry = execution._audit(record, milestone, block, observed=observed, outcome=outcome,
                                 actor_user_id=actor_user_id, actor_name=actor_name,
                                 source="automated read of the simulated DEV estate (SIMULATION)",
                                 evidence_reference=f"simulated DEV estate {env} read at reconciliation", note=note)
        block["state"] = state
        approval._save(change_id, record)
        return entry


def status(record: Optional[dict]) -> dict[str, Any]:
    """Milestone states for display: never collapsed into one success."""
    if record is None:
        return {"apply": "not approved", "build": "not approved", "verify": "not approved", "cnc": "not recorded"}
    return {"apply": execution.effective_state(record, APPLY), "build": execution.effective_state(record, BUILD),
            "verify": execution.effective_state(record, VERIFY),
            "cnc": "recorded" if record.get("cnc_activation") else "not recorded"}
