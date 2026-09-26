"""Demonstration only: give a DEMO customer an AI connection so the demo
seeds' scripted agent runs pass Jade's "customer has an AI connection" gate.

The key is a fake placeholder (never a real credential) and the seeds replace
the model with scripted stand-ins, so nothing is ever sent to a provider.
Refuses for any customer that is not a demo customer, and never touches an
existing connection. An Admin can replace it with a real key under
Admin > AI Connections.
"""

from __future__ import annotations

FAKE_KEY_PREFIX = "sk-ant-demo-not-a-real-key-"


def ensure_demo_ai_connection(company_id: str, *, model: str = "claude-sonnet-5") -> None:
    from jde_api_service.ai import connection, packs
    from jde_api_service.config import settings
    from jde_api_service.persistence.db import connection as db
    from jde_api_service.services.customer_service import is_demo_company

    if not is_demo_company(company_id):
        raise SystemExit(f"refusing: {company_id} is not a demo customer")
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM ai_connections WHERE company_id = ?", (company_id,)).fetchone()
    if not exists:
        connection.save(company_id, model=model, enabled=True, document_policy="permitted_content", limits=None,
                        expected_revision=None, actor="demo seed")
        connection.save_credential(company_id, FAKE_KEY_PREFIX + company_id.ljust(12, "x"), actor="demo seed")
    packs.ensure_templates(settings.repo_root)
    current = packs.assignments(company_id)
    for role in packs.ROLES:
        if role in current:
            continue
        revs = [r for p in packs.list_packs(company_id) if p["packId"] == packs.template_pack_id(role)
                for r in p["revisions"] if r["status"] == "published"]
        if revs:
            packs.assign(company_id, role, packs.template_pack_id(role), revs[0]["revision"], actor="demo seed")
