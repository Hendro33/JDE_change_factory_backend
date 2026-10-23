"""Decision X2 (23 Oct): customers on the standard Jade instructions follow the
latest standard version; a customer's own pack stays pinned and is told when
a newer standard version exists, with what changed."""

from __future__ import annotations

import json

from ._ai import configure


def _publish_new_template(role: str, extra: str) -> int:
    from jde_api_service.ai import packs
    from jde_api_service.persistence.db import connection

    pid = packs.template_pack_id(role)
    with connection(immediate=True) as conn:
        last = conn.execute("SELECT revision, content FROM agent_pack_revisions WHERE pack_id = ? ORDER BY revision DESC "
                            "LIMIT 1", (pid,)).fetchone()
        content = json.loads(last["content"])
        content["instructions"] += f"\n\n# Writing style\n{extra}\n"
        rev = last["revision"] + 1
        conn.execute("INSERT INTO agent_pack_revisions (pack_id, revision, status, content, sha256, note, created_at, "
                     "created_by, published_at, published_by) VALUES (?, ?, 'published', ?, ?, 'test', "
                     "'2999-01-01T00:00:00', 'repository', '2999-01-01T00:00:00', 'repository')",
                     (pid, rev, json.dumps(content), packs.content_sha(content)))
    return rev


def test_the_standard_instructions_follow_the_latest_version(client):
    from jde_api_service.ai import packs

    configure("vdb")
    before = packs.snapshot("vdb", "architect").revision
    rev = _publish_new_template("architect", "Write for a busy reader.")
    snap = packs.snapshot("vdb", "architect")
    assert snap.revision == rev > before and "Write for a busy reader." in snap.content["instructions"]
    a = packs.assignments("vdb")["architect"]
    assert a["followsStandard"] and a["revision"] == rev and a["assignedRevision"] == before


def test_a_customer_pack_stays_pinned_and_is_told_what_changed(client):
    from jde_api_service.ai import packs

    configure("vdb")
    tpl = packs.template_pack_id("improve-agent")
    base = packs.assignments("vdb")["improve-agent"]["revision"]
    own = packs.create_pack("vdb", role="improve-agent", name="VDB improve", from_pack_id=tpl, from_revision=base,
                            actor="test")
    own_id = own["packId"] if "packId" in own else own["pack_id"]
    draft = packs.list_packs("vdb")
    revs = next(p for p in draft if p["packId"] == own_id)["revisions"]
    packs.publish("vdb", own_id, revs[0]["revision"], actor="test")
    packs.assign("vdb", "improve-agent", own_id, revs[0]["revision"], actor="test")
    _publish_new_template("improve-agent", "Ask only what blocks.")
    snap = packs.snapshot("vdb", "improve-agent")
    assert snap.pack_id == own_id and snap.revision == revs[0]["revision"]
    a = packs.assignments("vdb")["improve-agent"]
    assert not a["followsStandard"] and a["newerStandard"] and "added: Writing style" in a["newerStandard"]["changes"]
