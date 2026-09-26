"""Configure a customer's AI connection and Start-up Packs the way an Admin
would -- used by the tests that run agent drivers with a mocked runtime."""

from __future__ import annotations

FAKE_KEY_PREFIX = "sk-ant-test-not-a-real-key-"  # never a real credential


def configure(company_id: str, *, model: str = "claude-sonnet-5", policy: str = "permitted_content",
              key: str | None = None) -> None:
    from jde_api_service.ai import connection, packs
    from jde_api_service.config import settings
    from jde_api_service.persistence.db import connection as db

    with db() as conn:
        exists = conn.execute("SELECT 1 FROM ai_connections WHERE company_id = ?", (company_id,)).fetchone()
    if not exists:
        connection.save(company_id, model=model, enabled=True, document_policy=policy, limits=None,
                        expected_revision=None, actor="test")
        connection.save_credential(company_id, key or FAKE_KEY_PREFIX + company_id.ljust(12, "x"), actor="test")
    packs.ensure_templates(settings.repo_root)
    current = packs.assignments(company_id)
    for role in packs.ROLES:
        if role in current:
            continue
        revs = [r for p in packs.list_packs(company_id) if p["packId"] == packs.template_pack_id(role)
                for r in p["revisions"] if r["status"] == "published"]
        if revs:
            packs.assign(company_id, role, packs.template_pack_id(role), revs[0]["revision"], actor="test")
