"""
Capability Catalogue -- Functional Agent design update, Section 2.

The agent's functional REMIT (functional-agent.md, Section 4.4 of the
design document) is broad: anything an ordinary JDE setup application
normally covers. Execution permission is NOT the same thing, and is
capability-specific -- a bounded, repeatable operation, never "maintain
all X configuration." This module is what makes that distinction an
enforced fact rather than a paragraph the agent is trusting itself to
remember (the same relationship scope.py already has to the
Configuration/Development Guidelines).

The catalogue itself -- CATALOG_FILE below -- is version-controlled
source, committed to this repo like any other code: it is NOT
engagement-specific (unlike scope.json, which is per-customer and
gitignored) and it is NOT something the agent, or this module, can
promote to Validated. Promotion happens by a human editing the JSON and
getting it reviewed like any other change -- "the agent cannot certify
its own capabilities" is enforced simply by there being no write path
here at all, on purpose, the same way approval.py's approve_change/
reject_change are human-only with no agent tool wrapping them.

Path resolution is anchored to this file's own location, not to the
process's current working directory. scope.py's SCOPE_FILE (a plain
"./scope.json" default) only resolves correctly because every
documented invocation path happens to run from the repo root; the
catalogue needs to be found identically whether it's read from
prove_the_gate.py (repo root), a Claude Code session cwd'd into
mcp_server (per .mcp.json), or a future api_service import -- so this
does not repeat that assumption.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG_FILE = os.environ.get("JDE_CAPABILITY_CATALOG_FILE", str(_REPO_ROOT / "capability_catalog.json"))

# Statuses (design Section 2). Keep in sync with capability_catalog.json
# and api_service/jde_api_service/models/capability.py's Literal.
EXECUTABLE_STATUSES = {"validated"}
# "needs_spike" can still run, but ONLY as an explicitly approved,
# narrowly-scoped experiment (scope.json's spike_experiments list) --
# never as an ordinary write. See require_executable below.
SPIKE_ELIGIBLE_STATUSES = {"needs_spike"}


class CapabilityError(RuntimeError):
    """Raised whenever a capability is unknown, not executable in the
    requested posture, or the catalogue itself can't be loaded. Distinct
    from ScopeViolation (scope.py) and ChangeApprovalError (approval.py)
    -- this is about whether the OPERATION TYPE is allowed to run at
    all, independent of whether this specific engagement has authorised
    this specific target and a human has approved this specific value."""


def _load_catalog() -> dict:
    if not os.path.exists(CATALOG_FILE):
        raise CapabilityError(
            f"No capability catalogue found at {CATALOG_FILE}. This is not "
            "optional -- every capability starts at Needs spike until a "
            "designated functional owner and technical validator promote "
            "it, and there is no 'no catalogue configured' default that "
            "would make everything executable."
        )
    with open(CATALOG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def catalog_revision() -> str:
    """The catalogue's own revision, stamped onto every exact-change
    record that binds to a capability (approval.py's propose_change) --
    the 'record the versions used for each run' requirement, enforced
    in code rather than left to the agent to remember to mention."""
    return _load_catalog().get("catalog_revision", "unknown")


def list_capabilities() -> list[dict]:
    """Read-only listing for the Jade interface (Admin > Agents,
    governance screens) and for get_capability_status below. Never
    called by anything that writes to JDE."""
    return _load_catalog().get("capabilities", [])


def get_capability(capability_id: str) -> Optional[dict]:
    for cap in list_capabilities():
        if cap.get("capability_id") == capability_id:
            return cap
    return None


def require_capability(capability_id: str) -> dict:
    cap = get_capability(capability_id)
    if cap is None:
        raise CapabilityError(
            f"'{capability_id}' is not a registered capability. A capability "
            "must exist in the catalogue, with a functional owner and a "
            "field-complete entry, before any operation can reference it -- "
            "this is not something an agent or an engagement's scope.json "
            "can invent on the fly."
        )
    return cap


def require_executable(capability_id: str, capability_revision: str, environment: str, *, spike_experiment_approved: bool = False) -> dict:
    """The real gate. Called from approval.py's require_exact_change
    (i.e. immediately before every write), never trusted to the model
    alone.

    - Validated: executes normally (still subject to every other check
      -- scope.json, exact-change hash, Oracle-owned-version rule).
    - Needs spike: executes ONLY if spike_experiment_approved is True,
      which require_exact_change sets from scope.json's own
      spike_experiments allowlist (Section 2's "a bounded DEV
      validation experiment requires its own explicit approval") --
      never from the agent's own say-so.
    - Restricted / Human Implementation / Suspended: never executes
      here, regardless of any other approval. 'Additional approval
      alone does not make it executable' (Section 2) -- changing that
      requires changing the catalogue entry itself, a separate,
      human-reviewed act.
    """
    cap = require_capability(capability_id)
    if cap.get("revision") != capability_revision:
        raise CapabilityError(
            f"capability '{capability_id}' revision mismatch: the operation "
            f"was proposed against revision '{capability_revision}', but the "
            f"catalogue's current entry is revision '{cap.get('revision')}'. "
            "The catalogue changed underneath this proposal -- re-propose "
            "against the current revision rather than executing against a "
            "specification that no longer exists."
        )
    if environment != "DEV":
        raise CapabilityError(
            f"capability execution is DEV-only; '{environment}' is not DEV. "
            "This is a universal rule (Section 1's 'Mandatory boundaries'), "
            "not something any capability entry or scope.json can override."
        )
    status = cap.get("validation", {}).get("status")
    if status in EXECUTABLE_STATUSES:
        return cap
    if status in SPIKE_ELIGIBLE_STATUSES and spike_experiment_approved:
        return cap
    if status in SPIKE_ELIGIBLE_STATUSES:
        raise CapabilityError(
            f"capability '{capability_id}' is Needs spike -- it cannot execute "
            "as an ordinary write. A bounded DEV validation experiment is "
            "possible, but only with its own explicit approval recorded in "
            "scope.json's spike_experiments (not this exact-change approval "
            "alone) -- see Section 2/3 of the design update."
        )
    raise CapabilityError(
        f"capability '{capability_id}' has status '{status}' and cannot "
        "execute through this agent. Restricted means policy blocks routine "
        "execution regardless of approval; Human Implementation means a "
        "human performs the write, not this agent; Suspended means a "
        "previously validated capability was disabled pending "
        "revalidation. Route this to Human Implementation with a precise "
        "proposal and test specification instead."
    )
