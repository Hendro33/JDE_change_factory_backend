"""
Which approved or pending work an evidence change materially affects.

Refresh Evidence, a new artifact revision or a material discovery-profile
change can change the basis an exact change (Functional) or implementation
package (Technical) was approved against. Each change records what it
depends on when it is approved or bound (jde_mcp_server/binding.py):
targets it reads or changes, artifacts it was derived from, and the
profile its design used. An evidence change that touches one of those is
material for that work: an invalidation is recorded on it, permanently,
and it can no longer execute under its existing approval. Evidence changes
that touch nothing it depends on leave it eligible -- the baseline is still
flagged for the design as a whole, and a person decides about the design.

The approval record itself is never edited: its history stays as it was.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .. import config as _config  # noqa: F401 -- makes jde_mcp_server importable


def _dependencies(record: dict) -> dict:
    from jde_mcp_server import binding

    b = record.get("binding") or {}
    deps = dict(b.get("depends_on") or {})
    if not deps:  # pending work, not yet bound: what it would depend on
        deps = {"targets": [binding.functional_target(record)] if (record.get("kind") or "functional") == "functional"
                else list(record.get("depends_on_targets") or []),
                "artifacts": list(record.get("depends_on_artifacts") or [])}
    return deps


def invalidate_affected(company_id: str, *, source: str, story_id: Optional[str] = None,
                        changed_targets: Iterable[str] = (), unverified_targets: Iterable[str] = (),
                        revised_artifacts: Iterable[str] = (), profile_material_hash: Optional[str] = None,
                        story_revised: Optional[int] = None) -> list[dict]:
    """Record an invalidation on every pending or approved change of the
    company (optionally one story) that the evidence change touches.
    Returns what was affected and why."""
    from jde_mcp_server import binding

    from .change_service import _all_change_records

    changed, unverified, revised = set(changed_targets), set(unverified_targets), set(revised_artifacts)
    affected = []
    for record in _all_change_records():
        if record.get("company_id") != company_id or record.get("status") not in ("pending", "approved"):
            continue
        if story_id and record.get("story_id") != story_id:
            continue
        deps = _dependencies(record)
        reasons = []
        hit = sorted(set(deps.get("targets") or []) & changed)
        if hit:
            reasons.append(("evidence_changed", f"re-read evidence differs for {', '.join(hit)}"))
        hit = sorted(set(deps.get("targets") or []) & unverified)
        if hit:
            reasons.append(("evidence_unverifiable", f"could not re-read {', '.join(hit)} during refresh"))
        hit = sorted(set(deps.get("artifacts") or []) & revised)
        if hit:
            reasons.append(("artifact_revised", f"a newer revision exists of {', '.join(hit)}"))
        bound_profile = (((record.get("binding") or {}).get("design") or {}).get("profile_material_hash"))
        if profile_material_hash and bound_profile and bound_profile != profile_material_hash:
            reasons.append(("environment_profile_changed", "the discovery profile the design used has materially changed"))
        if story_revised and story_id:
            reasons.append(("story_revised", f"the approved story was revised (revision {story_revised}) after this "
                                             "work was proposed"))
        for kind, detail in reasons:
            binding.record_invalidation(record["change_id"], kind=kind, detail=detail, source=source)
        if reasons:
            affected.append({"change_id": record["change_id"], "story_id": record["story_id"],
                             "kind": record.get("kind") or "functional", "status": record.get("status"),
                             "reasons": [{"kind": k, "detail": d} for k, d in reasons],
                             "effect": "no longer eligible to execute under its existing approval; "
                                       "a fresh proposal and approval are required"})
    return affected
