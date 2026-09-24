"""
The evidence baseline of one Architect design revision.

For every completed Architect analysis Jade stores an IMMUTABLE manifest:
the environment-profile revision, the live observations actually made
(with timestamps and payload hashes), the artifacts and documents actually
consulted (revision + checksum + release applicability), dependencies and
customisations, citations, and known gaps, contradictions and confidence
limits. It is the baseline for THIS request -- not a claim that the whole
installation was scanned -- and a snapshot: it never authorises a write,
and execution re-checks live preconditions on its own.

What the model claims is checked against what actually happened in the run
(the RunLedger): a citation to evidence that was not gathered is marked
unvalidated and downgraded to an assumption; missing evidence becomes a
targeted question or a blocked step.

Changed evidence never edits a manifest. Refresh Evidence creates a new
baseline revision (new observations, history preserved); a changed
observation, a newer artifact revision or a changed environment profile
flags the design for reassessment.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from ..config import settings
from ..persistence.db import connection
from . import artifacts, capabilities, profile_service, service

SCOPE_STATEMENT = (
    "Evidence gathered for this request only -- not a scan of the whole customer installation. "
    "A snapshot at the times shown: it does not authorise any write and does not prove nothing has changed since; "
    "execution re-validates live preconditions independently."
)
BASES = {"observed", "customer_attestation", "assumption"}
GAP_KINDS = {"missing", "stale", "conflict", "incompatible", "unavailable"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLedger:
    """What one Architect run actually did -- the authority for the
    manifest, whatever the model says afterwards."""

    def __init__(self, grant: Optional[service.DiscoveryGrant], no_grant_reason: str = "") -> None:
        self.grant = grant
        self.no_grant_reason = no_grant_reason
        self.observations: list[dict] = []
        self.blocked: list[dict] = []
        self.artifacts_listed: dict[str, dict] = {}
        self.artifacts_consulted: dict[str, dict] = {}
        self.artifacts_unavailable: list[dict] = []
        self.process_context_consulted: Optional[dict] = None
        self._lock = threading.Lock()

    def add_observation(self, evidence: dict) -> None:
        with self._lock:
            self.observations.append(evidence)

    def add_blocked(self, capability_id: str, target: str, reason: str) -> None:
        with self._lock:
            self.blocked.append({"capability_id": capability_id, "target": target, "reason": reason})

    def list_artifact(self, ref: str, summary: dict) -> None:
        with self._lock:
            self.artifacts_listed[ref] = summary

    def consult_artifact(self, ref: str, summary: dict) -> None:
        with self._lock:
            self.artifacts_consulted[ref] = summary

    def artifact_unavailable(self, ref: str, reason: str) -> None:
        with self._lock:
            self.artifacts_unavailable.append({"ref": ref, "reason": reason})


def artifact_summary(a: dict, profile: Optional[dict]) -> dict:
    meta = a["meta"]
    config = profile["config"] if profile else None
    summary = {
        "evidence_id": artifacts.evidence_ref(a),
        "artifact_id": a["artifact_id"], "revision": a["revision"], "kind": a["kind"],
        "object_name": meta.get("object_name"), "object_type": meta.get("object_type"),
        "export_format": meta.get("export_format"), "sha256": a["sha256"],
        "customer_environment": meta.get("customer_environment"), "path_code": meta.get("path_code"),
        "release": meta.get("release"), "source_location": meta.get("source_location"),
        "repository": meta.get("repository"), "commit_ref": meta.get("commit_ref"),
        "exported_at": meta.get("exported_at"), "uploaded_by": a["uploaded_by"], "uploaded_at": a["uploaded_at"],
        "runtime_correspondence": meta.get("runtime_correspondence", "unknown"),
        "runtime_statement": meta.get("runtime_statement", ""),
        "runtime_stated_by": meta.get("runtime_stated_by", ""),
        "extraction_status": a["extraction_status"], "domain_id": a["domain_id"],
        # Exactly how much of the file could be analysed (older uploads: unknown).
        "analysis_coverage": meta.get("analysis_coverage") or {"analysed": a["extraction_status"] == "supported",
                                                               "truncated": None, "note": "coverage not recorded"},
        "extraction_note": a.get("extraction_note", ""),
    }
    if a["kind"] == "reference_document":
        summary.update({
            "title": meta.get("doc_title"), "doc_revision": meta.get("doc_revision"),
            "applies_to_releases": meta.get("applies_to_releases") or [],
            "compatibility": artifacts.compatibility(
                a, config.expected_application_release if config else None,
                config.expected_tools_release if config else None),
        })
    return summary


# ---------------------------------------------------------------------
# Building a manifest
# ---------------------------------------------------------------------
def _validate_citations(raw: list[dict], ledger: RunLedger, profile_ref: Optional[str]) -> tuple[list[dict], list[dict]]:
    observed_ids = {o["observation_id"] for o in ledger.observations}
    artifact_ids = set(ledger.artifacts_consulted)
    doc_ids = {r for r, s in ledger.artifacts_listed.items() if s["kind"] == "reference_document"} | {
        r for r, s in ledger.artifacts_consulted.items() if s["kind"] == "reference_document"}
    attestable = artifact_ids | ({profile_ref} if profile_ref else set())
    citations, gaps = [], []
    for c in raw or []:
        claim = str(c.get("claim", "")).strip()[:500]
        ids = [str(i) for i in (c.get("evidence_ids") or [])][:10]
        basis = c.get("basis") if c.get("basis") in BASES else "assumption"
        problem = ""
        if basis == "observed":
            known = observed_ids | artifact_ids | doc_ids
            if not ids or any(i not in known for i in ids):
                problem = "cites evidence that was not gathered in this run"
        elif basis == "customer_attestation":
            if not ids or any(i not in attestable for i in ids):
                problem = "cites an attestation Jade does not hold"
        validated = not problem
        entry = {"claim": claim, "evidence_ids": ids, "basis": basis if validated else "assumption",
                 "claimed_basis": basis, "validated": validated}
        if not validated:
            entry["note"] = problem
            gaps.append({"kind": "missing", "description": f"Unsupported citation: {claim}",
                         "question": f"What evidence confirms: {claim}?", "blocked_step": "", "source": "system"})
        # Limitations of what was cited.
        for i in ids:
            s = ledger.artifacts_consulted.get(i) or ledger.artifacts_listed.get(i)
            if s and s["kind"] == "technical_export" and s["runtime_correspondence"] != "matches_dev_runtime":
                entry.setdefault("limitations", []).append(
                    f"{i}: export's correspondence to the active DEV runtime is {s['runtime_correspondence']}")
            if s and (s.get("analysis_coverage") or {}).get("truncated"):
                cov = s["analysis_coverage"]
                entry.setdefault("limitations", []).append(
                    f"{i}: only {cov['analysed_chars']:,} of {cov['total_chars']:,} characters were analysed (truncated)")
            if s and s["kind"] == "reference_document" and s.get("compatibility") != "compatible":
                entry.setdefault("limitations", []).append(
                    f"{i}: documentation release applicability is {s.get('compatibility')}")
        citations.append(entry)
    return citations, gaps


def _architect_gaps(raw: list[dict]) -> list[dict]:
    out = []
    for g in raw or []:
        kind = g.get("kind") if g.get("kind") in GAP_KINDS else "missing"
        desc = str(g.get("description", "")).strip()[:500]
        question = str(g.get("question", "")).strip()[:500]
        step = str(g.get("blocked_step", "")).strip()[:300]
        if not question and not step:
            question = f"What evidence resolves: {desc}?"
        out.append({"kind": kind, "description": desc, "question": question, "blocked_step": step,
                    "source": "architect"})
    return out


def _system_gaps(ledger: RunLedger, profile: Optional[dict]) -> tuple[list[dict], list[dict], list[str]]:
    gaps, contradictions, limits = [], [], []
    if ledger.grant is None:
        gaps.append({"kind": "missing", "description": f"Customer environment not investigated: {ledger.no_grant_reason}",
                     "question": "Can the company's Admin configure, verify and enable JDE discovery for this company?",
                     "blocked_step": "confirming objects, versions and configuration against the customer's DEV system",
                     "source": "system"})
    for b in ledger.blocked:
        cap = capabilities.get(b["capability_id"])
        unavailable = cap is not None and cap.base_status == "unavailable"
        gaps.append({
            "kind": "unavailable" if unavailable else "missing",
            "description": f"{b['capability_id']} on {b['target'] or '-'} was not read: {b['reason']}",
            "question": (cap.alternative if unavailable and cap else
                         f"Should an Admin approve {b['capability_id']} for {b['target'] or 'this target'}? "
                         "(any scope expansion needs explicit authorisation)"),
            "blocked_step": "", "source": "system"})
    for u in ledger.artifacts_unavailable:
        gaps.append({"kind": "unavailable", "description": f"{u['ref']} could not be analysed: {u['reason']}",
                     "question": "Can the customer provide this export in a supported text format?",
                     "blocked_step": "", "source": "system"})
    for ref, s in {**ledger.artifacts_listed, **ledger.artifacts_consulted}.items():
        if s["kind"] == "reference_document" and s.get("compatibility") == "incompatible":
            gaps.append({"kind": "incompatible", "description": f"{ref} ({s.get('title')}) applies to "
                         f"{', '.join(s.get('applies_to_releases') or [])}, not the customer's release",
                         "question": "Is documentation for the customer's application/Tools release available?",
                         "blocked_step": "", "source": "system"})
    for ref, s in ledger.artifacts_consulted.items():
        if s["kind"] == "technical_export":
            if s["runtime_correspondence"] == "known_mismatch":
                contradictions.append(f"{ref}: the customer states this export does NOT match the active DEV runtime "
                                      f"({s['runtime_statement'] or 'no detail'})")
            elif s["runtime_correspondence"] == "unknown":
                limits.append(f"{ref}: whether this export matches the active DEV runtime is unknown")
    for ref, s in ledger.artifacts_consulted.items():
        cov = s.get("analysis_coverage") or {}
        if cov.get("truncated"):
            limits.append(f"{ref}: TRUNCATED -- only {cov['analysed_chars']:,} of {cov['total_chars']:,} characters "
                          "were available for analysis; conclusions about the rest of the file are not supported")
    if any(o["mode"] == "simulation" for o in ledger.observations):
        limits.append("Live observations in this baseline are SIMULATED, not the customer's JDE")
    if any(not o["sharing"]["values_shared"] for o in ledger.observations):
        limits.append("Some values were redacted under the company's data-sharing policy; the Architect saw "
                      "structure and counts only for those reads")
    if profile and profile["config"].connection_mode == "live":
        limits.append("Live AIS response shapes are unverified until confirmed with the customer (Experiment A1)")
    return gaps, contradictions, limits


def _profile_block(profile: Optional[dict], grant: Optional[service.DiscoveryGrant]) -> Optional[dict]:
    if profile is None:
        return None
    config = profile["config"]
    return {
        "revision": grant.profile_revision if grant else profile["revision"],
        "material_hash": profile["material_hash"], "environment": config.environment,
        "environment_type": config.environment_type, "role": config.role, "path_code": config.path_code,
        "application_release": config.expected_application_release, "tools_release": config.expected_tools_release,
        "mode": config.connection_mode, "discovery_enabled": profile_service.is_active(profile),
        "routing_isolation_confirmed": config.routing_isolation_confirmed,
        "privilege_confirmed": config.privilege_confirmed, "data_sharing_policy": config.data_sharing_policy,
        "attestation_ref": f"PROFILE@r{profile['revision']}",
    }


def _observation_entry(o: dict, payload_sha: str) -> dict:
    return {"observation_id": o["observation_id"], "capability_id": o["capability_id"], "target": o["target"],
            "fields": o["fields"], "observed_at": o["observed_at"], "mode": o["mode"],
            "record_count": o["record_count"],
            # A change detector only: Jade does not keep the unredacted result,
            # so this hash cannot reproduce or prove content nobody retained.
            "payload_sha256": payload_sha, "payload_sha256_role": "change_detector",
            "retained": "the evidence as shown to the model" + ("" if o["sharing"]["values_shared"]
                                                                else " (values redacted)"),
            "values_shared": o["sharing"]["values_shared"], "provenance": o["provenance"]}


EVIDENCE_NOTES = [
    "payload_sha256 on each observation is computed over the FULL read result, including values redacted from "
    "the model. It detects whether a later read of the same target returns something different. Jade does not "
    "retain that full result, so the hash is not proof of any content that was not shown or kept.",
    "Retained evidence is the sanitised observation as shown to the model; redacted values were never seen by "
    "the model and are not stored.",
]


def _payload_sha(company_id: str, observation_id: str) -> str:
    obs = service.get_observation(company_id, observation_id)
    return obs["payload_sha256"] if obs else ""


def _store(company_id: str, story_id: str, design_revision: int, trigger: str, manifest: dict,
           status: str, reassessment: list[dict]) -> dict:
    with connection(immediate=True) as conn:
        prev = conn.execute(
            "SELECT MAX(baseline_revision) AS r FROM design_baselines WHERE company_id = ? AND story_id = ?",
            (company_id, story_id)).fetchone()["r"] or 0
        manifest["baseline_revision"] = prev + 1
        manifest["baseline_id"] = baseline_id = f"BL-{uuid.uuid4().hex[:10]}"
        blob = json.dumps(manifest, sort_keys=True)
        sha = hashlib.sha256(blob.encode()).hexdigest()
        conn.execute("UPDATE design_baselines SET status = 'superseded' WHERE company_id = ? AND story_id = ? "
                     "AND status != 'superseded'", (company_id, story_id))
        conn.execute(
            "INSERT INTO design_baselines (baseline_id, company_id, story_id, design_revision, baseline_revision, "
            "created_at, trigger, manifest, manifest_sha256, status, reassessment) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (baseline_id, company_id, story_id, design_revision, prev + 1, manifest["created_at"], trigger, blob, sha,
             status, json.dumps(reassessment)),
        )
    return get_baseline(company_id, baseline_id)  # type: ignore[return-value]


def create_for_design(*, company_id: str, story_id: str, design_revision: int, ledger: RunLedger,
                      evidence: dict, agent_run_id: Optional[str], initiated_by: Optional[str]) -> dict:
    profile = profile_service.load(company_id)
    profile_block = _profile_block(profile, ledger.grant)
    citations, citation_gaps = _validate_citations(evidence.get("citations") or [], ledger,
                                                   profile_block["attestation_ref"] if profile_block else None)
    sys_gaps, contradictions, limits = _system_gaps(ledger, profile)
    manifest = {
        "manifest_version": 1,
        "scope_statement": SCOPE_STATEMENT,
        "evidence_notes": EVIDENCE_NOTES,
        "company_id": company_id, "story_id": story_id,
        "domain_id": ledger.grant.domain_id if ledger.grant else None,
        "design_revision": design_revision, "created_at": _now(), "trigger": "architect_run",
        "agent_run_id": agent_run_id, "initiated_by": initiated_by,
        "environment_profile": profile_block,
        "observations": [_observation_entry(o, _payload_sha(company_id, o["observation_id"])) for o in ledger.observations],
        "artifacts": [s for s in ledger.artifacts_consulted.values() if s["kind"] == "technical_export"],
        "documents": [s for s in {**ledger.artifacts_listed, **ledger.artifacts_consulted}.values()
                      if s["kind"] == "reference_document"],
        "artifacts_available_not_consulted": sorted(
            r for r, s in ledger.artifacts_listed.items() if s["kind"] == "technical_export" and r not in ledger.artifacts_consulted),
        "dependencies": [str(d)[:300] for d in (evidence.get("dependencies") or [])][:30],
        "customisations": [str(d)[:300] for d in (evidence.get("customisations") or [])][:30],
        "citations": citations,
        "gaps": _architect_gaps(evidence.get("gaps") or []) + citation_gaps + sys_gaps,
        "contradictions": [str(c)[:500] for c in (evidence.get("contradictions") or [])][:20] + contradictions,
        "confidence_limitations": [str(c)[:500] for c in (evidence.get("confidence_limitations") or [])][:20] + limits,
        "blocked_requests": ledger.blocked,
        "process_context": _process_context(company_id, story_id, ledger, evidence),
    }
    return _store(company_id, story_id, design_revision, "architect_run", manifest, "current", [])


def _process_context(company_id: str, story_id: str, ledger: RunLedger, evidence: dict) -> dict:
    """The story's process context this design rests on: always the
    fingerprint at design time (so a later material change is detected),
    whether the Architect consulted it, and its process findings."""
    from ..process import story as story_process

    raw = evidence.get("process_findings") or {}
    return {"fingerprint": story_process.fingerprint(company_id, story_id),
            "consulted": ledger.process_context_consulted is not None,
            "consulted_fingerprint": (ledger.process_context_consulted or {}).get("fingerprint"),
            "findings": {k: [str(x)[:500] for x in (raw.get(k) or [])][:15]
                         for k in ("affected_processes", "missing_requirements", "missing_controls",
                                   "missing_acceptance_criteria")}}


# ---------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------
def _row(row) -> dict:
    d = dict(row)
    d["manifest"] = json.loads(d["manifest"])
    d["reassessment"] = json.loads(d["reassessment"] or "[]")
    return d


def get_baseline(company_id: str, baseline_id: str) -> Optional[dict]:
    with connection() as conn:
        row = conn.execute("SELECT * FROM design_baselines WHERE baseline_id = ? AND company_id = ?",
                           (baseline_id, company_id)).fetchone()
    return _row(row) if row else None


def list_for_story(company_id: str, story_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM design_baselines WHERE company_id = ? AND story_id = ? "
                            "ORDER BY baseline_revision DESC", (company_id, story_id)).fetchall()
    return [_row(r) for r in rows]


def current_for_story(company_id: str, story_id: str) -> Optional[dict]:
    rows = [b for b in list_for_story(company_id, story_id) if b["status"] != "superseded"]
    return rows[0] if rows else None


def verify(baseline: dict) -> bool:
    blob = json.dumps(baseline["manifest"], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest() == baseline["manifest_sha256"]


# ---------------------------------------------------------------------
# Reassessment flags
# ---------------------------------------------------------------------
def _flag(baseline: dict, reason: dict) -> None:
    with connection(immediate=True) as conn:
        row = conn.execute("SELECT reassessment, status FROM design_baselines WHERE baseline_id = ?",
                           (baseline["baseline_id"],)).fetchone()
        if row is None or row["status"] == "superseded":
            return
        reasons = json.loads(row["reassessment"] or "[]")
        reasons.append({**reason, "flagged_at": _now()})
        conn.execute("UPDATE design_baselines SET status = 'needs_reassessment', reassessment = ? WHERE baseline_id = ?",
                     (json.dumps(reasons), baseline["baseline_id"]))
    _update_handoff_status(baseline["story_id"], baseline["baseline_id"], "needs_reassessment", reasons)


def _update_handoff_status(story_id: str, baseline_id: str, status: str, reasons: list[dict]) -> None:
    """Keep the downstream agents' copy in step with the flag."""
    path = os.path.join(handoff_dir(), f"{story_id}.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        package = json.load(f)
    if package.get("baseline_id") != baseline_id:
        return
    package["status"], package["reassessment"] = status, reasons
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".handoff-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _current_baselines(company_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute("SELECT * FROM design_baselines WHERE company_id = ? AND status != 'superseded'",
                            (company_id,)).fetchall()
    return [_row(r) for r in rows]


def on_artifact_uploaded(company_id: str, artifact: dict) -> list[str]:
    flagged = []
    for b in _current_baselines(company_id):
        m = b["manifest"]
        for entry in (*m.get("artifacts", []), *m.get("documents", [])):
            if entry["artifact_id"] == artifact["artifact_id"] and entry["revision"] < artifact["revision"]:
                _flag(b, {"kind": "artifact_revised", "detail": f"{entry['evidence_id']} superseded by "
                                                                f"{artifacts.evidence_ref(artifact)}"})
                flagged.append(b["story_id"])
                break
    from ..services.work_invalidation import invalidate_affected

    invalidate_affected(company_id, source=f"new artifact revision {artifacts.evidence_ref(artifact)}",
                        revised_artifacts={artifact["artifact_id"]})
    return flagged


def on_profile_saved(company_id: str, profile: dict) -> list[str]:
    flagged = []
    for b in _current_baselines(company_id):
        p = b["manifest"].get("environment_profile")
        if p and p["material_hash"] != profile["material_hash"]:
            _flag(b, {"kind": "environment_profile_changed",
                      "detail": f"baseline used profile revision {p['revision']}; now revision {profile['revision']}"})
            flagged.append(b["story_id"])
    from ..services.work_invalidation import invalidate_affected

    invalidate_affected(company_id, source=f"discovery profile revision {profile['revision']}",
                        profile_material_hash=profile["material_hash"])
    return flagged


# ---------------------------------------------------------------------
# Refresh Evidence
# ---------------------------------------------------------------------
def refresh(company_id: str, story_id: str, *, actor_user_id: str) -> dict:
    current = current_for_story(company_id, story_id)
    if current is None:
        raise LookupError("no evidence baseline for this design yet")
    grant, reason = service.refresh_grant(story_id, company_id, actor_user_id)
    if grant is None:
        raise service.DiscoveryBlocked(f"cannot refresh evidence: {reason}")
    old = current["manifest"]
    new_obs, changes, blocked = [], [], []
    for entry in old.get("observations", []):
        prior = service.get_observation(company_id, entry["observation_id"])
        req = prior["request"] if prior else {}
        try:
            ev = service.execute_read(grant, entry["capability_id"], req.get("target", ""), req.get("fields"),
                                      req.get("filters"), req.get("max_records", 10), refresh_of=entry["observation_id"])
        except (service.DiscoveryBlocked, service.DiscoveryFailed) as exc:
            blocked.append({"capability_id": entry["capability_id"], "target": entry["target"], "reason": str(exc)})
            continue
        sha = _payload_sha(company_id, ev["observation_id"])
        new_obs.append(_observation_entry(ev, sha))
        changed = sha != entry["payload_sha256"]
        changes.append({"previous": entry["observation_id"], "current": ev["observation_id"], "changed": changed})
    reassessment = []
    if any(c["changed"] for c in changes):
        reassessment.append({"kind": "observation_changed", "detail": ", ".join(
            f"{c['previous']} -> {c['current']}" for c in changes if c["changed"]), "flagged_at": _now()})
    for entry in (*old.get("artifacts", []), *old.get("documents", [])):
        latest = artifacts.latest_revision(company_id, entry["artifact_id"])
        if latest > entry["revision"]:
            reassessment.append({"kind": "artifact_revised", "detail": f"{entry['evidence_id']} now at r{latest}",
                                 "flagged_at": _now()})
    profile = profile_service.load(company_id)
    if old.get("environment_profile") and profile and old["environment_profile"]["material_hash"] != profile["material_hash"]:
        reassessment.append({"kind": "environment_profile_changed", "detail": "profile changed since the design",
                             "flagged_at": _now()})
    from ..process import story as story_process

    recorded = ((old.get("process_context") or {}).get("fingerprint") or {}).get("sha256")
    if recorded and recorded != story_process.fingerprint(company_id, story_id)["sha256"]:
        reassessment.append({"kind": "process_context_changed", "detail": "the story's confirmed processes or maps "
                             "changed since the design", "flagged_at": _now()})
    if blocked:
        reassessment.append({"kind": "refresh_incomplete", "detail": f"{len(blocked)} observation(s) could not be refreshed",
                             "flagged_at": _now()})
    # Which approved or pending work this materially affects (its own
    # dependencies), each invalidated for execution under its approval.
    from ..services.work_invalidation import invalidate_affected

    by_id = {e["observation_id"]: e for e in old.get("observations", [])}
    affected = invalidate_affected(
        company_id, story_id=story_id, source=f"Refresh Evidence by {actor_user_id}",
        changed_targets={f"{by_id[c['previous']]['capability_id']}:{by_id[c['previous']]['target'].upper()}"
                         for c in changes if c["changed"]},
        unverified_targets={f"{b['capability_id']}:{str(b['target']).upper()}" for b in blocked},
        revised_artifacts={e["artifact_id"] for e in (*old.get("artifacts", []), *old.get("documents", []))
                           if artifacts.latest_revision(company_id, e["artifact_id"]) > e["revision"]},
        profile_material_hash=profile["material_hash"] if profile else None)
    manifest = {**old, "created_at": _now(), "trigger": "refresh", "refreshed_by": actor_user_id,
                "evidence_notes": EVIDENCE_NOTES,
                "refresh_note": ("Evidence re-read only: new observations were recorded for the same approved "
                                 "targets. The design, its Implementation Specification and any exact-change "
                                 "approval are unchanged -- nothing was regenerated or re-approved. If the evidence "
                                 "changed, the design is flagged for reassessment; a person decides whether to "
                                 "re-run the Architect."),
                "refreshed_from": current["baseline_id"], "observations": new_obs,
                "environment_profile": _profile_block(profile, grant),
                "refresh_changes": changes, "refresh_blocked": blocked, "affected_work": affected}
    manifest.pop("baseline_id", None)
    status = "needs_reassessment" if reassessment else "current"
    return _store(company_id, story_id, current["design_revision"], "refresh", manifest, status, reassessment)


# ---------------------------------------------------------------------
# Hand-off to the Functional and Technical agents
# ---------------------------------------------------------------------
HANDOFF_NOTE = ("This evidence baseline is the Architect's snapshot for this request. It does not authorise any "
                "write. Before executing, re-validate every live precondition you rely on; the execution gate "
                "re-checks approval, scope and environment on its own.")


def handoff_dir() -> str:
    return os.environ.get("JDE_DESIGN_BASELINE_DIR") or os.path.join(settings.data_dir, "design_baselines")


def write_handoff(company_id: str, story_id: str, baseline: dict, architect_decision: Optional[dict],
                  implementation_spec: Optional[dict], change_id: Optional[str] = None) -> dict:
    """change_id: the exact change this design revision proposed (None for a
    design that proposed none). A refresh of the same design revision keeps
    the change it already had."""
    directory = handoff_dir()
    path = os.path.join(directory, f"{story_id}.json")
    design_approval = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            previous = json.load(f)
        if previous.get("design_revision") == baseline["design_revision"]:
            change_id = change_id if change_id is not None else previous.get("change_id")
            # A person's approval of THIS design revision stays with it; a
            # new design revision never inherits it.
            design_approval = previous.get("design_approval")
    package = {
        "company_id": company_id, "story_id": story_id, "baseline_id": baseline["baseline_id"],
        "design_revision": baseline["design_revision"], "baseline_revision": baseline["baseline_revision"],
        "manifest_sha256": baseline["manifest_sha256"], "status": baseline["status"],
        "architect_decision": architect_decision, "implementation_spec": implementation_spec,
        "evidence_manifest": baseline["manifest"], "reassessment": baseline["reassessment"], "note": HANDOFF_NOTE,
        "change_id": change_id, "design_approval": design_approval,
    }
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".handoff-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return package


def set_design_approval(story_id: str, approval: dict) -> None:
    """Record a person's design approval on the downstream hand-off copy,
    for the design revision it names only."""
    path = os.path.join(handoff_dir(), f"{story_id}.json")
    with open(path, encoding="utf-8") as f:
        package = json.load(f)
    if package.get("design_revision") != approval["design_revision"]:
        raise LookupError("the hand-off is for a different design revision")
    package["design_approval"] = {k: approval[k] for k in (
        "id", "design_revision", "baseline_id", "manifest_sha256", "approved_by", "approver_user_id", "approved_at")}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".handoff-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2, sort_keys=True)
    os.replace(tmp, path)
