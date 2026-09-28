"""
The governed delivery of Technical implementation packages.

A Technical Agent prepares a package (api_service stores it, immutably, per
revision): the candidate source, its exact diff, the objects, the test plan
and the recovery plan -- a developer-ready specification. A person approves
THAT exact package revision -- its content hash -- as an exact change (kind
"technical"). Delivery is the RECORDED route: people do the work in JD
Edwards and record each milestone in Jade, which re-checks everything first:

    apply   -> a developer checks the approved candidate in through OMW   [WRITE]
    build   -> the checked-in objects are built (package build)           [BUILD]
    CNC     -> a CNC deploys/activates the built package                  [recorded]
    verify  -> the approved test plan is run in DEV, results recorded     [TEST]

Each milestone is recorded separately and is never implied by another.
Before recording, Jade re-derives inside the change's lock: the approval
(approved, unexpired, approver's CURRENT authority), that the package is
byte-for-byte the approved one and not superseded, the approval's basis
(design revision and design approval, no invalidation, each object's source
still the one the package was prepared from), the company's scope (DEV
binding, authorised object types, customer system codes 55-59) and the
recorder's current role. No agent and no Jade component writes to JDE here.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from . import approval, authority, binding, capability_catalog, execution
from .approval import ChangeApprovalError
from .backlog import require_approved
from .scope import (
    ScopeViolation,
    check_custom_product_code,
    check_environment_binding,
    check_technical_scope,
    company_for_story,
    scope_revision,
)

CAPABILITY_ID = "custom_object_text_change"
TOOL = "apply_technical_package"
APPLY, BUILD, VERIFY = execution.WRITE, execution.BUILD, execution.TEST
MODE = "recorded"
# Who records the development milestones (apply, build, verify): the
# Application Manager. The CNC activation has its own roles (catalogue).
RECORDER_ROLES = ("product_manager",)


class LiveAdapterUnavailable(ChangeApprovalError):
    """A candidate's format cannot be delivered (no safe way to apply it)."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def object_key(object_name: str, object_type: str) -> str:
    return f"{object_name.strip().upper()}|{object_type.strip().upper()}"


def package_sha256(content: dict) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mode() -> str:
    return MODE


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
            f"{CAPABILITY_ID} cannot be delivered ({mode()}): {adapter.get('reason', 'not available')}")
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
        raise ChangeApprovalError(f"approved for {record.get('execution_mode')} application, which no longer exists; "
                                  "prepare and approve the package again")
    try:
        capability_catalog.require_deliverable_by_person(record["capability_id"], record["capability_revision"],
                                                         record["environment"])
    except capability_catalog.CapabilityError as exc:
        raise ChangeApprovalError(str(exc)) from exc
    adapter_for_mode()
    for cand in content.get("candidates") or []:
        if not (format_support(cand.get("format", "")).get("apply") or {}).get(mode()):
            raise LiveAdapterUnavailable(f"{cand.get('format')} cannot be delivered: "
                                         f"{format_support(cand.get('format', '')).get('note') or 'no safe way to apply it'}")
    check_package_scope(scope, content)
    if milestone == APPLY:
        binding.require_valid(record)
        before = (record.get("binding") or {}).get("before_state", {}).get("value") or {}
        for cand in content["candidates"]:
            if before.get(cand["object_key"]) != cand["before_sha256"]:
                raise binding.BindingInvalid(
                    f"stale source: {cand['object_key']}'s current source ({str(before.get(cand['object_key']))[:12]}) "
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
        "event": event, "stage": f"technical_{event}", "actor": data.get("actor", "technical delivery"),
        "detail": detail, "change_id": record["change_id"], "package": record["operation"],
        "mode": record.get("execution_mode"), **data,
    })["entry_hash"]


def _milestone(change_id: str, name: str, entry: dict) -> None:
    with execution._locked(change_id):
        record = approval._load(change_id)
        record.setdefault("milestones", []).append({"milestone": name, "at": time.time(), **entry})
        approval._save(change_id, record)


def _require_recorder(record: dict, actor_user_id: str, actor_name: str, what: str) -> None:
    try:
        authority.require_current_approver(actor_user_id, record["company_id"], RECORDER_ROLES)
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise approval.ApproverNotAuthorised(f"{actor_name} may not record {what}: {exc}") from exc


def _require_text(**fields: str) -> dict:
    missing = [k.replace("_", " ") for k, v in fields.items() if not (v or "").strip()]
    if missing:
        raise ChangeApprovalError("recording this needs " + ", ".join(missing))
    return {k: v.strip() for k, v in fields.items()}


# ---------------------------------------------------------------------
# Milestones, each recorded by a person and re-checked first
# ---------------------------------------------------------------------
def apply(change_id: str, package: dict, *, actor: str, actor_user_id: str, omw_project: str,
          evidence_reference: str, note: str = "") -> dict:
    """A developer checked the approved candidate in through OMW (not yet
    active). Recorded only if the package, approval, basis and scope hold."""
    record, _scope = authorise(change_id, package, milestone=APPLY)
    _require_recorder(record, actor_user_id, actor, "a check-in")
    fields = _require_text(omw_project=omw_project, evidence_reference=evidence_reference)
    before = json.dumps(record["binding"]["before_state"]["value"], sort_keys=True)
    after = {c["object_key"]: c["after_sha256"] for c in package["content"]["candidates"]}
    execution.record(change_id, APPLY, "applied", before_value=before,
                     revalidate=lambda: authorise(change_id, package, milestone=APPLY),
                     detail=f"checked in through OMW project {fields['omw_project']} (not active)",
                     recorded={"by": actor, "user_id": actor_user_id, **fields, "note": note, "after_sha256": after})
    h = _evidence(record, "applied", f"package {package['package_id']}@r{package['revision']} checked in through OMW "
                  f"project {fields['omw_project']} by {actor}, not active",
                  {"actor": actor, "omw_project": fields["omw_project"], "evidence_reference": fields["evidence_reference"],
                   "note": note, "candidates": [{"object_key": k, "after_sha256": v} for k, v in after.items()]})
    _milestone(change_id, "applied", {"actor": actor, "omw_project": fields["omw_project"], "evidence_entry_hash": h})
    return {"milestone": "applied", "change_id": change_id, "active": False}


def build(change_id: str, package: dict, *, actor: str, actor_user_id: str, succeeded: bool, build_reference: str,
          log: str = "") -> dict:
    """The checked-in objects were built (e.g. an OMW/package build). A failed
    build is recorded as such: a repair is a new package revision."""
    record, _scope = authorise(change_id, package, milestone=BUILD)
    if execution.effective_state(record, APPLY) != "applied":
        raise execution.ExecutionBlocked(f"change {change_id}: nothing is known to be checked in, so nothing can be "
                                         f"built (apply: {execution.effective_state(record, APPLY)})")
    _require_recorder(record, actor_user_id, actor, "a build")
    fields = _require_text(build_reference=build_reference)
    lines = [ln for ln in (log or "").splitlines() if ln.strip()][:50]
    outcome = "built" if succeeded else "failed"
    execution.record(change_id, BUILD, outcome, revalidate=lambda: authorise(change_id, package, milestone=BUILD),
                     detail=f"{outcome}: {fields['build_reference']}",
                     recorded={"by": actor, "user_id": actor_user_id, **fields, "log": lines})
    h = _evidence(record, "built" if succeeded else "build_failed",
                  f"build {'succeeded' if succeeded else 'FAILED'} ({fields['build_reference']}), recorded by {actor}",
                  {"actor": actor, "build_reference": fields["build_reference"], "log": lines})
    _milestone(change_id, "built" if succeeded else "build_failed",
               {"actor": actor, "build_reference": fields["build_reference"], "log": lines, "evidence_entry_hash": h})
    return {"milestone": "built" if succeeded else "build_failed", "change_id": change_id, "log": lines,
            "next": ("a repair is a new package revision with a fresh approval" if not succeeded
                     else "awaiting CNC activation" if technical_enforcement().get("requires_cnc_activation")
                     else "ready to verify")}


def record_cnc_activation(change_id: str, package: dict, *, actor_user_id: str, actor_name: str, package_name: str,
                          evidence_reference: str, note: str = "") -> dict:
    """A CNC deployed/activated the built package in DEV. Only a person who
    holds a CNC activation role in the company right now may record it."""
    record, _scope = authorise(change_id, package, milestone="cnc")
    roles = technical_enforcement().get("cnc_activation_roles") or []
    try:
        authority.require_current_approver(actor_user_id, record["company_id"], roles)
    except (authority.AuthorityRevoked, authority.AuthorityUnverifiable) as exc:
        raise approval.ApproverNotAuthorised(f"{actor_name} may not record a CNC activation: {exc}") from exc
    if not (evidence_reference or "").strip() or not (package_name or "").strip():
        raise ChangeApprovalError("a CNC activation needs the package name and an evidence reference")
    now = time.time()
    entry = {"by": actor_name, "user_id": actor_user_id, "package_name": package_name.strip(),
             "evidence_reference": evidence_reference.strip(), "note": note, "at": now}
    with execution._locked(change_id):
        current = approval._load(change_id)
        if execution.effective_state(current, BUILD) != "built":
            raise execution.ExecutionBlocked(f"change {change_id}: the package is not known to be built "
                                             f"(build: {execution.effective_state(current, BUILD)}) -- nothing to activate")
        if current.get("cnc_activation"):
            raise execution.ExecutionBlocked(f"change {change_id}: the CNC activation is already recorded")
        entry["evidence_entry_hash"] = _evidence(record, "cnc_activation_recorded",
                                                 f"CNC activation of package {entry['package_name']} recorded by {actor_name}",
                                                 {"actor": actor_name, "cnc": dict(entry)})
        current["cnc_activation"] = entry
        current.setdefault("milestones", []).append({"milestone": "cnc_activated", "at": now, "actor": actor_name,
                                                     "evidence_entry_hash": entry["evidence_entry_hash"]})
        approval._save(change_id, current)
    return entry


def verify(change_id: str, package: dict, *, actor: str, actor_user_id: str, results: list[dict],
           runtime_is_approved_artifact: bool, evidence_reference: str, note: str = "") -> dict:
    """The approved test plan was run in DEV against the ACTIVE runtime and
    each result recorded. Every test in the plan must have a result; the
    recorder states whether what runs in DEV is the approved package."""
    record, _scope = authorise(change_id, package, milestone=VERIFY)
    if execution.effective_state(record, BUILD) != "built":
        raise execution.ExecutionBlocked(f"change {change_id}: the package is not known to be built -- nothing to verify")
    if technical_enforcement().get("requires_cnc_activation") and not record.get("cnc_activation"):
        raise execution.ExecutionBlocked(
            f"change {change_id}: awaiting CNC activation -- the built package is not active in DEV until a CNC "
            "deploys it and that is recorded")
    _require_recorder(record, actor_user_id, actor, "a verification")
    fields = _require_text(evidence_reference=evidence_reference)
    plan = [t.get("name") for t in package["content"].get("test_plan") or []]
    kinds = {t.get("name"): t.get("kind", "") for t in package["content"].get("test_plan") or []}
    by_name = {}
    for r in results or []:
        name = str(r.get("name") or "").strip()
        if name not in plan:
            raise ChangeApprovalError(f"{name!r} is not a test in the approved test plan")
        if not isinstance(r.get("passed"), bool):
            raise ChangeApprovalError(f"test {name!r} needs a passed or failed result")
        by_name[name] = {"name": name, "kind": kinds.get(name, ""), "passed": r["passed"],
                         "note": str(r.get("note") or "")[:500]}
    missing = [n for n in plan if n not in by_name]
    if missing:
        raise ChangeApprovalError("every test in the approved plan needs a result; missing: " + ", ".join(missing))
    rows = [by_name[n] for n in plan]
    passed = bool(rows) and all(r["passed"] for r in rows)
    approved_after = {c["object_key"]: c["after_sha256"] for c in package["content"]["candidates"]}
    now = time.time()
    outcome = {"passed": passed, "results": rows, "runtime_is_approved_artifact": bool(runtime_is_approved_artifact),
               "runtime_statement": "recorded by the person who verified", "approved_after_sha256": approved_after,
               "evidence_reference": fields["evidence_reference"], "note": note, "by": actor, "at": now}
    execution.record(change_id, VERIFY, "completed", revalidate=lambda: authorise(change_id, package, milestone=VERIFY),
                     detail=f"{sum(r['passed'] for r in rows)}/{len(rows)} passed",
                     recorded={"by": actor, "user_id": actor_user_id, "evidence_reference": fields["evidence_reference"]})
    verified = passed and outcome["runtime_is_approved_artifact"]
    outcome["evidence_entry_hash"] = _evidence(
        record, "verified" if verified else "verification_failed",
        f"{sum(r['passed'] for r in rows)}/{len(rows)} tests passed; the active DEV runtime "
        f"{'IS' if outcome['runtime_is_approved_artifact'] else 'is NOT'} the approved package (stated by {actor})",
        {"actor": actor, "results": rows, "approved_after_sha256": approved_after,
         "package_sha256": package["content_sha256"], "evidence_reference": fields["evidence_reference"]})
    with execution._locked(change_id):
        current = approval._load(change_id)
        current["verification"] = outcome
        current.setdefault("milestones", []).append({
            "milestone": "verified" if verified else "verification_failed", "at": now, "actor": actor,
            "evidence_entry_hash": outcome["evidence_entry_hash"]})
        approval._save(change_id, current)
    return outcome


def status(record: Optional[dict]) -> dict[str, Any]:
    """Milestone states for display: never collapsed into one success."""
    if record is None:
        return {"apply": "not approved", "build": "not approved", "verify": "not approved", "cnc": "not recorded"}
    return {"apply": execution.effective_state(record, APPLY), "build": execution.effective_state(record, BUILD),
            "verify": execution.effective_state(record, VERIFY),
            "cnc": "recorded" if record.get("cnc_activation") else "not recorded"}
