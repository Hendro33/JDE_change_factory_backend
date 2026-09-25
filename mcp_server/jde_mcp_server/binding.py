"""
What an approval was given AGAINST, and whether that still holds.

An exact-change approval is a decision about a specific operation, made on
the basis of a specific Architect design revision, its evidence baseline,
the artifact revisions it relied on, and the target's state at that moment.
This module records that basis on the approval (the binding) and re-checks
it before every dispatch:

  * the design revision is still the story's latest design, and the change
    is still the one that design proposed -- a later baseline or design is
    never silently substituted into previously approved work;
  * no invalidation has been recorded against the change: Refresh Evidence,
    a new artifact revision or a material profile change that touches what
    the change depends on records one (api_service decides materiality and
    calls record_invalidation), and it is permanent for that approval --
    running the change again needs a fresh proposal and approval;
  * the target's current state is still the before-state the approval saw.

The historical approval record is never edited or removed: approved_at and
approved_by stay as history. Eligibility is computed, not stored, and an
unchanged approval timestamp says nothing about it.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .approval import ChangeApprovalError


class BindingInvalid(ChangeApprovalError):
    """The approval's basis no longer holds -- execution is not eligible."""


def _handoff_dir() -> str:
    return os.environ.get("JDE_DESIGN_BASELINE_DIR", "./design_baselines")


def design_handoff(story_id: str) -> Optional[dict]:
    """The story's current design hand-off package (written by api_service),
    or None when the story has no Architect design."""
    path = os.path.join(_handoff_dir(), f"{story_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Targets and before-state readers, per kind of change
# ---------------------------------------------------------------------
def functional_target(record: dict) -> str:
    op = record.get("operation") or {}
    return f"processing_option_values:{str(op.get('application', '')).upper()}|{str(op.get('version', '')).upper()}"


def read_functional_before(record: dict) -> dict:
    """The target's current value through the execution path's own reader
    (the shared simulated estate in mock mode). Live reading is not
    implemented, so the before-state is then unknown -- and fails closed."""
    from .ais_client import AISClientError, LiveReadUnavailable, client

    op = record.get("operation") or {}
    try:
        value = client.read_processing_option_value(record["company_id"], op.get("application", ""),
                                                    op.get("version", ""), op.get("option", ""))
    except LiveReadUnavailable as exc:
        return {"known": False, "reason": str(exc)}
    except AISClientError as exc:
        return {"known": False, "reason": str(exc)}
    return {"known": True, "value": value,
            "target": f"{op.get('application')}/{op.get('version')}/{op.get('option')}",
            "source": "simulated DEV estate read (SIMULATION)"}


def technical_targets(record: dict) -> list[str]:
    return [f"technical_object:{k}" for k in (record.get("operation") or {}).get("objects", [])]


def read_technical_before(record: dict) -> dict:
    """The ACTIVE runtime checksum of every object the package changes, in
    the company's bound DEV environment. Only the simulation can read it;
    there is no qualified live mechanism, so live is unknown (fail closed)."""
    from . import technical_sim
    from .config import settings
    from .scope import load_company_scope

    if not settings.mock_mode:
        return {"known": False, "reason": "no qualified live mechanism reads an object's active runtime specification"}
    try:
        env = ((load_company_scope(record["company_id"]).get("environment") or {}).get("dev_environment_id") or "")
    except Exception as exc:  # noqa: BLE001 -- recorded as unknown, never guessed
        return {"known": False, "reason": str(exc)}
    keys = (record.get("operation") or {}).get("objects", [])
    state = technical_sim.runtime_state(record["company_id"], env, keys)
    if any(v is None for v in state.values()):
        return {"known": False, "reason": f"object(s) missing from the simulated DEV estate: "
                                          f"{', '.join(k for k, v in state.items() if v is None)}"}
    return {"known": True, "value": state, "target": ", ".join(keys),
            "source": f"simulated DEV estate {env} active runtime checksums (SIMULATION)"}


BEFORE_READERS: dict[str, Callable[[dict], dict]] = {"functional": read_functional_before,
                                                     "technical": read_technical_before}


def _kind(record: dict) -> str:
    return record.get("kind") or "functional"


# ---------------------------------------------------------------------
# At approval
# ---------------------------------------------------------------------
def snapshot(record: dict, *, extra_dependencies: Optional[dict] = None) -> dict:
    """The binding stamped on the approval. Raises BindingInvalid when the
    change cannot be approved against the story's current design."""
    if record.get("invalidations"):
        raise BindingInvalid("this change was invalidated before approval ("
                             + "; ".join(i["detail"] for i in record["invalidations"]) + ") -- propose it again")
    handoff = design_handoff(record["story_id"])
    design = None
    if handoff is not None:
        if handoff.get("company_id") != record.get("company_id"):
            raise BindingInvalid("the story's design belongs to another company -- refusing")
        if _kind(record) == "technical":
            op = record.get("operation") or {}
            approved_design = handoff.get("design_approval") or {}
            if op.get("design_revision") != handoff.get("design_revision"):
                raise BindingInvalid(
                    f"package was prepared for design revision {op.get('design_revision')}, but the story's design is "
                    f"revision {handoff.get('design_revision')} -- prepare it again against the current design")
            if approved_design.get("design_revision") != handoff.get("design_revision"):
                raise BindingInvalid("the current design revision has no design approval -- a person must approve "
                                     "the design before its implementation can be approved")
        elif handoff.get("change_id") != record["change_id"]:
            raise BindingInvalid(
                f"change {record['change_id']} is not the change the story's current design revision "
                f"{handoff.get('design_revision')} proposed ({handoff.get('change_id') or 'none'}) -- it cannot be "
                "approved against a design it did not come from")
        if handoff.get("status") == "needs_reassessment":
            raise BindingInvalid("the design's evidence has changed and it is flagged for reassessment -- "
                                 "re-run the Architect before approving")
        manifest = handoff.get("evidence_manifest") or {}
        design = {
            "design_revision": handoff.get("design_revision"), "baseline_id": handoff.get("baseline_id"),
            "baseline_revision": handoff.get("baseline_revision"), "manifest_sha256": handoff.get("manifest_sha256"),
            "profile_material_hash": (manifest.get("environment_profile") or {}).get("material_hash"),
            "artifacts": [{"artifact_id": a.get("artifact_id"), "revision": a.get("revision"), "sha256": a.get("sha256")}
                          for a in (*manifest.get("artifacts", []), *manifest.get("documents", []))],
            "design_approval": handoff.get("design_approval"),
        }
    depends_on = {"targets": [functional_target(record)] if _kind(record) == "functional"
                  else sorted(set(technical_targets(record)) | set(record.get("depends_on_targets") or [])),
                  "artifacts": sorted({a["artifact_id"] for a in (design or {}).get("artifacts", [])}
                                      | set(record.get("depends_on_artifacts") or []))}
    for key, values in (extra_dependencies or {}).items():
        depends_on[key] = sorted(set(depends_on.get(key, [])) | set(values))
    reader = BEFORE_READERS.get(_kind(record))
    before = reader(record) if reader else {"known": False, "reason": "no before-state reader for this kind"}
    now = time.time()
    return {"design": design, "depends_on": depends_on, "before_state": before, "bound_at": now,
            "bound_at_iso": _iso(now),
            "note": ("bound to the Architect design and evidence baseline shown" if design else
                     "no Architect design for this story: bound to the before-state only")}


# ---------------------------------------------------------------------
# Invalidations (recorded by api_service when evidence changes)
# ---------------------------------------------------------------------
def record_invalidation(change_id: str, *, kind: str, detail: str, source: str) -> Optional[dict]:
    """Append a permanent invalidation to a change's approval basis. The
    approval record itself (who, when) is untouched."""
    from . import approval, execution  # noqa: F811

    with execution._locked(change_id):
        record = approval._load(change_id)
        if record is None or record.get("status") not in ("pending", "approved"):
            return None
        entry = {"kind": kind, "detail": detail[:500], "source": source, "at": time.time()}
        entry["at_iso"] = _iso(entry["at"])
        record.setdefault("invalidations", []).append(entry)
        approval._save(change_id, record)
        return entry


# ---------------------------------------------------------------------
# At dispatch
# ---------------------------------------------------------------------
def problems(record: dict, *, read_current: bool = True) -> list[str]:
    """Every reason the approval's basis no longer holds (empty = holds)."""
    out: list[str] = []
    binding = record.get("binding")
    if not binding:
        return [f"change {record['change_id']} has no recorded approval basis (approved before bindings existed, "
                "or not approved) -- propose and approve it again"]
    for inv in record.get("invalidations") or []:
        out.append(f"invalidated {inv.get('at_iso', '')}: {inv['kind']} -- {inv['detail']} (source: {inv['source']})")
    design = binding.get("design")
    handoff = design_handoff(record["story_id"])
    if design:
        if handoff is None:
            out.append("the design this approval was bound to is no longer available")
        else:
            if handoff.get("design_revision") != design["design_revision"]:
                out.append(f"approved against design revision {design['design_revision']}, but the story's design "
                           f"is now revision {handoff.get('design_revision')} -- the newer design is not substituted")
            elif _kind(record) == "technical":
                approved_design = handoff.get("design_approval") or {}
                if approved_design.get("design_revision") != design["design_revision"]:
                    out.append("the design approval this implementation relied on is no longer in force")
            elif handoff.get("change_id") != record["change_id"]:
                out.append("the story's design no longer proposes this change")
    elif handoff is not None:
        out.append("the story now has an Architect design this change was not approved against")
    before = binding.get("before_state") or {}
    if not before.get("known"):
        out.append(f"the target's before-state was not established at approval: {before.get('reason', 'unknown')}")
    elif read_current:
        reader = BEFORE_READERS.get(_kind(record))
        now = reader(record) if reader else {"known": False}
        if not now.get("known"):
            out.append(f"the target's current state cannot be read: {now.get('reason', 'unknown')}")
        elif now.get("value") != before.get("value"):
            out.append(f"the target changed since approval: it was {before.get('value')!r} when approved and is now "
                       f"{now.get('value')!r} -- reassess and approve again")
    return out


def require_valid(record: dict) -> None:
    found = problems(record)
    if found:
        raise BindingInvalid(f"change {record['change_id']} is not eligible to execute: " + "; ".join(found))
