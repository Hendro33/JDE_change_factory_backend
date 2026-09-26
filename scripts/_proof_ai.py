"""
Shared by the real-model proof scripts: the proof customer's AI connection is
configured exactly as an Admin would (Admin > AI Connections), from a key the
operator supplies in the environment for this run only. Without one the
proof stops -- the backend machine's own key or login is never used.

    JADE_PROOF_ANTHROPIC_API_KEY=... JADE_PROOF_MODEL=claude-sonnet-5 python3 scripts/prove_...py
"""

from __future__ import annotations

import os
import sys

KEY_ENV, MODEL_ENV = "JADE_PROOF_ANTHROPIC_API_KEY", "JADE_PROOF_MODEL"


def require_key() -> str:
    key = os.environ.pop(KEY_ENV, "").strip()  # removed from this process's environment at once
    if not key:
        sys.exit(f"{KEY_ENV} is not set. Real-model proofs run with a customer AI connection only (no fallback to "
                 "this machine's key or login). They make billable requests: set the key for this run, with approval.")
    return key


def configure(company_id: str, key: str, repo_root: str, *, policy: str = "permitted_content") -> None:
    from jde_api_service.ai import connection, packs

    model = os.environ.get(MODEL_ENV, "claude-sonnet-5")
    current = connection.view(company_id)
    connection.save(company_id, model=model, enabled=True, document_policy=policy, limits=None,
                    expected_revision=current.get("revision"), actor="proof operator")
    connection.save_credential(company_id, key, actor="proof operator")
    packs.ensure_templates(repo_root)
    assigned = packs.assignments(company_id)
    for role in packs.ROLES:
        if role not in assigned:
            revs = [r["revision"] for p in packs.list_packs(company_id) if p["packId"] == packs.template_pack_id(role)
                    for r in p["revisions"] if r["status"] == "published"]
            packs.assign(company_id, role, packs.template_pack_id(role), revs[0], actor="proof operator")
