"""
The Technical workflow, from an approved Architect design to verified
simulated application.

  1. A person approves the Architect's design revision for technical
     implementation (design approval).
  2. The Technical Agent investigates and prepares an implementation package
     in an isolated workspace; Jade stores it as an immutable revision with
     its exact diff and proposes it as an exact change (pending).
  3. A person approves THAT package revision (exact implementation approval).
     The agent cannot approve anything.
  4. The governed executor (jde_mcp_server.technical_gate) applies, builds,
     records the human CNC activation and runs the verification tests --
     each milestone separately, each re-checked before dispatch.

Everything is resolved from backend records -- company, domain, design
revision, evidence baseline, change id -- never from what an agent says.
"""

from __future__ import annotations

from typing import Any, Optional

from .. import config as _config  # noqa: F401 -- makes jde_mcp_server importable
from ..discovery import artifacts as artifact_store
from ..discovery import baseline
from . import store

from jde_mcp_server import (  # noqa: E402
    approval, authority, binding, capability_catalog, execution, sim_estate, technical_gate, technical_sim,
)
from jde_mcp_server.backlog import require_approved  # noqa: E402
from jde_mcp_server.scope import company_for_story, load_company_scope, require_approval_policy  # noqa: E402

TECHNICAL_ROUTES = {"Technical Agent", "Mixed"}


class TechnicalRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------
# The assignment -- from trusted records only
# ---------------------------------------------------------------------
def assignment(company_id: str, story_id: str) -> dict[str, Any]:
    require_approved(story_id)
    if company_for_story(story_id) != company_id:
        raise TechnicalRefused("the story does not belong to this company")
    from ..services.registry import get_architecture_review_service, get_domain_review_service

    run = get_architecture_review_service().get(story_id)
    handoff = binding.design_handoff(story_id)
    if run is None or not run.history or handoff is None:
        raise TechnicalRefused("the story has no Architect design with an evidence baseline")
    if handoff.get("company_id") != company_id:
        raise TechnicalRefused("the design hand-off belongs to another company")
    latest = run.history[-1]
    review = get_domain_review_service().get(story_id)
    scope = load_company_scope(company_id)
    env = (scope.get("environment") or {}).get("dev_environment_id") or ""
    stored = baseline.get_baseline(company_id, handoff["baseline_id"])
    if stored is None or not baseline.verify(stored) or stored["manifest_sha256"] != handoff["manifest_sha256"]:
        raise TechnicalRefused("the design's evidence baseline does not verify -- refusing")
    return {
        "company_id": company_id, "story_id": story_id,
        "domain_id": review.business_domain_id if review else None,
        "design_revision": handoff["design_revision"], "route": latest.architect_decision.recommended_route,
        "architect_decision": latest.architect_decision.model_dump(mode="json"),
        "implementation_spec": latest.implementation_spec.model_dump(mode="json"),
        "baseline_id": handoff["baseline_id"], "manifest_sha256": handoff["manifest_sha256"],
        "baseline_status": handoff.get("status"), "evidence_manifest": handoff["evidence_manifest"],
        "design_approval": handoff.get("design_approval"), "target_environment": env,
        "mode": technical_gate.mode(),
    }


def approve_design(company_id: str, story_id: str, design_revision: int, *, actor_name: str, actor_user_id: str,
                   roles: set[str], note: str) -> dict:
    a = assignment(company_id, story_id)
    if a["design_revision"] != design_revision:
        raise TechnicalRefused(f"design revision {design_revision} is not the current design (revision "
                               f"{a['design_revision']})")
    if a["baseline_status"] == "needs_reassessment":
        raise TechnicalRefused("the design's evidence changed and it is flagged for reassessment -- re-run the Architect")
    if a["route"] not in TECHNICAL_ROUTES:
        raise TechnicalRefused(f"the design routes to {a['route']!r}, not to the Technical Agent")
    if store.design_approval_for(company_id, story_id, design_revision):
        raise TechnicalRefused(f"design revision {design_revision} is already approved")
    policy = require_approval_policy(load_company_scope(company_id))
    matched = sorted(set(roles) & set(policy["exact_change_approver_roles"]))
    if not matched:
        raise approval.ApproverNotAuthorised("approving a design needs a role the company's approval policy allows")
    authority.require_current_approver(actor_user_id, company_id, policy["exact_change_approver_roles"])
    row = store.add_design_approval(
        company_id=company_id, story_id=story_id, design_revision=design_revision, baseline_id=a["baseline_id"],
        manifest_sha256=a["manifest_sha256"], approved_by=actor_name, approver_user_id=actor_user_id, roles=matched,
        note=note)
    baseline.set_design_approval(story_id, row)
    return row


def start_run(company_id: str, story_id: str, *, purpose: str, initiated_by: str) -> dict:
    a = assignment(company_id, story_id)
    approved = a["design_approval"]
    if not approved or approved.get("design_revision") != a["design_revision"]:
        raise TechnicalRefused("the current design revision has not been approved for technical implementation")
    if a["baseline_status"] == "needs_reassessment":
        raise TechnicalRefused("the design is flagged for reassessment; the Technical Agent does not work from it")
    return store.start_run(company_id=company_id, story_id=story_id, purpose=purpose,
                           design_revision=a["design_revision"], baseline_id=approved["baseline_id"],
                           design_approval_id=approved["id"], initiated_by=initiated_by)


# ---------------------------------------------------------------------
# Source artifacts: what can be prepared, and how it relates to DEV
# ---------------------------------------------------------------------
def original_text(artifact: dict) -> Optional[str]:
    try:
        return artifact_store.default_store().get(artifact["storage_key"]).decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def classify(company_id: str, artifact: dict, environment: str) -> dict[str, Any]:
    """Whether a source artifact can be prepared/applied, and whether it is a
    development export or the verified active DEV runtime."""
    meta = artifact["meta"]
    fmt = meta.get("export_format", "other")
    support = technical_gate.format_support(fmt)
    coverage = meta.get("analysis_coverage") or {}
    reasons = []
    if artifact["kind"] != "technical_export":
        reasons.append("a reference document, not source")
    if not support.get("prepare"):
        reasons.append(support.get("note") or f"{fmt}: no safe way to edit this format as text")
    if coverage.get("truncated"):
        reasons.append(f"partial export: only {coverage.get('analysed_chars')} of {coverage.get('total_chars')} characters "
                       "were extracted -- a partial export never represents the complete object")
    if artifact["extraction_status"] != "supported":
        reasons.append(artifact.get("extraction_note") or "not extractable as text")
    key = technical_sim.object_key(meta.get("object_name", ""), meta.get("object_type", ""))
    runtime: dict[str, Any] = {"object_key": key}
    if technical_gate.mode() == "simulation" and environment:
        obj = technical_sim.get_object(company_id, environment, key)
        if obj is None:
            runtime.update({"state": "not_in_estate", "detail": "the object is not in the simulated DEV estate"})
        elif obj["active"]["sha256"] == artifact["sha256"]:
            runtime.update({"state": "verified_active_runtime", "detail": "matches the ACTIVE simulated DEV runtime "
                            "(simulation-only check: no live mechanism reads a runtime specification)"})
        else:
            runtime.update({"state": "stale", "detail": "differs from the ACTIVE simulated DEV runtime -- this export is "
                            "not what runs in DEV; preparing from it would be stale"})
    else:
        runtime.update({"state": "unverifiable", "detail": "no qualified mechanism reads the active runtime"})
    correspondence = meta.get("runtime_correspondence", "unknown")
    classification = ("verified_active_runtime" if runtime.get("state") == "verified_active_runtime"
                      else "runtime_export_attested" if correspondence == "matches_dev_runtime"
                      else "development_export")
    if runtime.get("state") == "stale":
        reasons.append("stale source: it is not the active DEV runtime")
    return {"evidence_id": artifact_store.evidence_ref(artifact), "artifact_id": artifact["artifact_id"],
            "revision": artifact["revision"], "sha256": artifact["sha256"], "format": fmt,
            "object_key": key, "object_name": meta.get("object_name"), "object_type": meta.get("object_type"),
            "classification": classification, "customer_statement": correspondence,
            "runtime_check": runtime, "can_prepare": not reasons, "reasons": reasons,
            "can_apply": {m: bool((support.get("apply") or {}).get(m)) for m in ("simulation", "live")},
            "coverage": coverage, "provenance": {k: meta.get(k) for k in (
                "repository", "commit_ref", "source_location", "exported_at", "customer_environment", "path_code",
                "runtime_statement", "runtime_stated_by")}}


# ---------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------
def build_content(*, a: dict, run: dict, revision: int, workspace, objects: list[dict], sources: list[dict],
                  explanation: str, requirement_trace: list[dict], dependencies: list[str], test_plan: list[dict],
                  missing_evidence: list[str], unsupported: list[str], recovery: str, repair_of: Optional[dict]) -> dict:
    enf = technical_gate.technical_enforcement()
    adapter = (enf["adapters"].get(technical_gate.mode()) or {})
    candidates = []
    for file_id in workspace.changed_files():
        entry = workspace.files()[file_id]
        text = workspace.text(file_id)
        candidates.append({"object_key": entry["object_key"], "file_name": entry["file_name"],
                           "source_ref": entry["evidence_id"], "format": entry["format"],
                           "before_sha256": entry["original_sha256"], "after_sha256": technical_sim.sha256_text(text),
                           "text": text, "diff": workspace.diff(file_id)})
    return {
        "schema": "jade.technical_package/1", "revision": revision,
        "company_id": a["company_id"], "story_id": a["story_id"], "domain_id": a["domain_id"],
        "target_environment": a["target_environment"], "mode": technical_gate.mode(),
        "design": {"design_revision": run["design_revision"], "baseline_id": run["baseline_id"],
                   "design_approval_id": run["design_approval_id"],
                   "manifest_sha256": (a["design_approval"] or {}).get("manifest_sha256")},
        "objects": objects, "sources": sources, "candidates": candidates,
        "diff": "".join(c["diff"] for c in candidates),
        "dependencies": dependencies,
        "toolchain": {"adapter": adapter.get("adapter"), "adapter_version": adapter.get("version"),
                      "formats": sorted({c["format"] for c in candidates}), "requires_build": enf.get("requires_build"),
                      "requires_cnc_activation": enf.get("requires_cnc_activation"),
                      "live_adapter": enf["adapters"]["live"]},
        "explanation": explanation, "requirement_trace": requirement_trace, "test_plan": test_plan,
        "missing_evidence": missing_evidence, "unsupported": unsupported,
        "lifecycle": ["prepared", "exact implementation approval (a person)", "apply: checked in, not active",
                      "build", "CNC activation (a human CNC; recorded, never automated)", "verify"],
        "recovery": {"before": {c["object_key"]: c["before_sha256"] for c in candidates}, "plan": recovery,
                     "constraints": ["before activation: abandon the checked-in candidate",
                                     "after activation: restore the previous source as a new, approved package revision",
                                     "in real JDE a checked-in or promoted object is a Human Implementation / CNC decision"]},
        "prepared_by": {"run_id": run["run_id"], "agent": "technical-agent"},
        "repair_of": repair_of,
    }


def submit(company_id: str, story_id: str, run_id: str, content: dict) -> dict:
    """Store the revision (compare-and-set) and propose it for approval."""
    if not content["candidates"]:
        raise TechnicalRefused("the workspace has no changes: nothing to package")
    content_sha = technical_gate.package_sha256(content)
    package = store.store_package(run_id=run_id, company_id=company_id, story_id=story_id, content=content,
                                  content_sha256=content_sha)
    record = technical_gate.propose(story_id, package)
    store.set_package_change(company_id, story_id, package["revision"], record["change_id"])
    store.add_event(run_id, "package_stored", f"revision {package['revision']} sha256 {content_sha[:12]}",
                    change_id=record["change_id"])
    return {**store.get_package(company_id, story_id, package["revision"]), "change": record}


def package_or_404(company_id: str, story_id: str, revision: int) -> dict:
    package = store.get_package(company_id, story_id, revision)
    if package is None:
        raise LookupError(f"no package revision {revision} for {story_id}")
    return package


def change_for(package: dict) -> Optional[dict]:
    return approval._load(package["change_id"]) if package.get("change_id") else None


def next_milestone(record: dict) -> Optional[str]:
    apply_state = execution.effective_state(record, technical_gate.APPLY)
    if apply_state != "applied":
        return "apply"
    build_state = execution.effective_state(record, technical_gate.BUILD)
    if build_state != "built":
        return "build"
    if not record.get("cnc_activation"):
        return "cnc_activation"
    if execution.effective_state(record, technical_gate.VERIFY) != "completed":
        return "verify"
    return None


def eligibility(package: dict) -> dict[str, Any]:
    """Whether the package's NEXT milestone may run now, and every reason not.
    Only application compares the target with its approved before-state: after
    it, the package's own apply/activation changes the target by design, which
    is not drift."""
    record = change_for(package)
    if record is None:
        return {"eligible": False, "next_milestone": None, "reasons": ["no exact change proposed for this revision"]}
    nxt = next_milestone(record)
    if nxt is None:
        return {"eligible": False, "next_milestone": None, "reasons": ["every milestone is complete for this revision"]}
    reasons: list[str] = []
    gate_milestone = {"apply": technical_gate.APPLY, "build": technical_gate.BUILD, "cnc_activation": "cnc",
                      "verify": technical_gate.VERIFY}[nxt]
    try:
        technical_gate.authorise(record["change_id"], package, milestone=gate_milestone)
    except Exception as exc:  # noqa: BLE001 -- reported, never raised
        reasons.append(str(exc))
    if record.get("binding"):
        for p in binding.problems(record, read_current=nxt == "apply"):
            if p not in " ".join(reasons):
                reasons.append(p)
    state = execution.effective_state(record, {"apply": technical_gate.APPLY, "build": technical_gate.BUILD,
                                               "verify": technical_gate.VERIFY}.get(nxt, technical_gate.BUILD))
    if nxt in ("apply", "build", "verify") and state not in ("ready",):
        reasons.append(f"{nxt} is {state}")
    if nxt == "cnc_activation":
        reasons.append("awaiting a human CNC activation -- only a CNC operator can record it")
    return {"eligible": not reasons, "next_milestone": nxt, "reasons": reasons}


def approve_package(company_id: str, story_id: str, revision: int, *, actor_name: str, actor_user_id: str,
                    roles: set[str], note: str) -> dict:
    package = package_or_404(company_id, story_id, revision)
    if package.get("superseded_by"):
        raise TechnicalRefused(f"revision {revision} is superseded by revision {package['superseded_by']}")
    if not package.get("change_id"):
        raise TechnicalRefused("no exact change proposed for this revision")
    return approval.approve_change(package["change_id"], actor_name, company_id=company_id, approver_roles=roles,
                                   approver_user_id=actor_user_id, note=note)


def run_milestone(company_id: str, story_id: str, revision: int, milestone: str, *, actor: str) -> dict:
    package = package_or_404(company_id, story_id, revision)
    record = change_for(package)
    if record is None:
        raise TechnicalRefused("no exact change proposed for this revision")
    fn = {"apply": technical_gate.apply, "build": technical_gate.build, "verify": technical_gate.verify}[milestone]
    return fn(record["change_id"], package, actor=actor)


def record_cnc(company_id: str, story_id: str, revision: int, *, actor_user_id: str, actor_name: str,
               package_name: str, evidence_reference: str, note: str) -> dict:
    package = package_or_404(company_id, story_id, revision)
    record = change_for(package)
    if record is None:
        raise TechnicalRefused("no exact change proposed for this revision")
    return technical_gate.record_cnc_activation(record["change_id"], package, actor_user_id=actor_user_id,
                                                actor_name=actor_name, package_name=package_name,
                                                evidence_reference=evidence_reference, note=note)


def reconcile(company_id: str, story_id: str, revision: int, milestone: str, *, actor_user_id: str,
              actor_name: str, note: str) -> dict:
    package = package_or_404(company_id, story_id, revision)
    record = change_for(package)
    if record is None:
        raise TechnicalRefused("no exact change proposed for this revision")
    kind = {"apply": technical_gate.APPLY, "build": technical_gate.BUILD}.get(milestone)
    if kind is None:
        raise TechnicalRefused("only apply or build is reconciled")
    return technical_gate.reconcile(record["change_id"], package, milestone=kind, actor_user_id=actor_user_id,
                                    actor_name=actor_name, note=note)


# ---------------------------------------------------------------------
# The work view (Technical work screen)
# ---------------------------------------------------------------------
def _approval_view(record: Optional[dict]) -> Optional[dict]:
    if record is None:
        return None
    keys = ("change_id", "status", "approved_by", "approved_at", "expires_at", "decision_note", "binding",
            "invalidations", "cnc_activation", "verification", "milestones", "execution_mode")
    view = {k: record.get(k) for k in keys}
    view["milestone_states"] = technical_gate.status(record)
    view["attempts"] = {k: (record.get("execution") or {}).get(k, {}).get("attempts", [])
                        for k in ("write", "build", "test")}
    view["reconciliations"] = {k: (record.get("execution") or {}).get(k, {}).get("reconciliations", [])
                               for k in ("write", "build", "test")}
    return view


def work_view(company_id: str, story_id: str) -> dict[str, Any]:
    try:
        a = assignment(company_id, story_id)
        problem = None
    except Exception as exc:  # noqa: BLE001 -- shown on the screen
        a, problem = None, str(exc)
    cap = capability_catalog.get_capability(technical_gate.CAPABILITY_ID) or {}
    packages = []
    for p in store.packages_for(company_id, story_id):
        record = change_for(p)
        packages.append({
            "revision": p["revision"], "package_id": p["package_id"], "content_sha256": p["content_sha256"],
            "created_at": p["created_at"], "created_by_run": p["created_by_run"], "superseded_by": p["superseded_by"],
            "content": p["content"], "approval": _approval_view(record),
            "eligibility": eligibility(p) if record and record.get("status") == "approved" else
            {"eligible": False, "next_milestone": "apply",
             "reasons": [f"exact implementation approval: {record.get('status') if record else 'none'}"]},
        })
    env = (a or {}).get("target_environment") or ""
    estate = sim_estate.load(company_id, env) if env and technical_gate.mode() == "simulation" else None
    human = [{"action": "design_approval", "by": d["approved_by"], "user_id": d["approver_user_id"], "at": d["approved_at"],
              "detail": f"design revision {d['design_revision']} (baseline {d['baseline_id']})"}
             for d in store.design_approvals(company_id, story_id)]
    for p in packages:
        ap = p["approval"] or {}
        if ap.get("status") in ("approved", "rejected"):
            human.append({"action": f"implementation_{ap['status']}", "by": ap.get("approved_by"), "at": ap.get("approved_at"),
                          "detail": f"package revision {p['revision']}"})
        if ap.get("cnc_activation"):
            c = ap["cnc_activation"]
            human.append({"action": "cnc_activation", "by": c["by"], "user_id": c["user_id"], "at": c["at"],
                          "detail": f"package {c['package_name']} ({c['evidence_reference']})", "simulated": c["simulated"]})
    return {
        "story_id": story_id, "mode": technical_gate.mode(),
        "simulation_label": sim_estate.SIMULATION_LABEL if technical_gate.mode() == "simulation" else None,
        "format_label": technical_sim.FORMAT_LABEL,
        "assignment": a, "assignment_problem": problem,
        "capability": {"capability_id": technical_gate.CAPABILITY_ID, "status": (cap.get("validation") or {}).get("status"),
                       "technical_validation": (cap.get("validation") or {}).get("technical_validation"),
                       "enforcement": cap.get("technical_enforcement")},
        "runs": store.runs_for(company_id, story_id), "packages": packages, "human_actions": human,
        "estate": {"environment": env, "revision": estate.get("revision"), "objects": {
            k: {"active_sha256": o["active"]["sha256"], "active_package": o["active"].get("package"),
                "checked_in_sha256": (o.get("checked_in") or {}).get("sha256"), "build": o.get("build")}
            for k, o in (estate.get("objects") or {}).items()}} if estate else None,
    }
