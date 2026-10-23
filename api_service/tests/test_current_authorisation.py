"""Finding 5 (post-step-3 review): every view shows the CURRENT authorisation.

An approval that expired or was invalidated before it was used is not shown
as "Authorised" anywhere; it can be authorised again under the current
policy, and the earlier approval stays in the history.
"""

from __future__ import annotations

import time

from .conftest import headers


def _expire(change_id: str) -> None:
    from jde_mcp_server import approval

    record = approval._load(change_id)  # noqa: SLF001
    record["expires_at"] = time.time() - 1
    approval._save(change_id, record)  # noqa: SLF001


def test_a_valid_approval_is_authorised(client):
    from .test_recorded_delivery import _change as approved_change

    approved_change(client, "S-AUTH-OK")
    a = client.get("/changes/S-AUTH-OK", headers=headers("vdb")).json()["lifecycle"]["authorisation"]
    assert a["state"] == "valid" and a["label"] == "Authorised" and not a["renewable"]


def test_an_expired_approval_is_not_shown_as_authorised_in_any_view(client):
    from .test_recorded_delivery import _change as approved_change

    rec = approved_change(client, "S-AUTH-EXP")
    _expire(rec["change_id"])
    h = headers("vdb")
    story = client.get("/changes/S-AUTH-EXP", headers=h).json()["lifecycle"]
    listed = next(c for c in client.get("/changes", headers=h).json() if c["id"] == "S-AUTH-EXP")["lifecycle"]
    work = client.get("/work", headers=h).json()
    in_work = next(c["lifecycle"] for bucket in ("needsYou", "waitingOnOthers", "jadeWorking")
                   for c in work.get(bucket, []) if c["id"] == "S-AUTH-EXP")
    for lc in (story, listed, in_work):
        assert lc["authorisation"]["state"] == "expired"
        assert lc["authorisation"]["label"] == "Authorisation expired" and lc["authorisation"]["renewable"]
        assert lc["nextAction"]["action"] == "approve_exact_change"


def test_an_expired_approval_can_be_authorised_again_and_the_old_one_is_kept(client):
    from jde_mcp_server import approval

    from .test_recorded_delivery import _change as approved_change

    rec = approved_change(client, "S-AUTH-RENEW")
    _expire(rec["change_id"])
    r = client.post("/changes/S-AUTH-RENEW/approve-change", headers=headers("vdb"), json={"note": "Still right."})
    assert r.status_code == 200, r.text
    a = r.json()["lifecycle"]["authorisation"]
    assert a["state"] == "valid" and len(a["history"]) == 1
    assert "expired" in a["history"][0]["reason"]
    assert approval._load(rec["change_id"])["approval_history"][0]["status"] == "approved"  # noqa: SLF001


def test_a_valid_or_used_approval_cannot_be_approved_twice(client):
    from .test_recorded_delivery import _change as approved_change

    approved_change(client, "S-AUTH-TWICE")
    r = client.post("/changes/S-AUTH-TWICE/approve-change", headers=headers("vdb"), json={"note": "again"})
    assert r.status_code == 409


def test_an_invalidated_approval_is_not_authorised(client):
    from jde_mcp_server import approval

    from .test_recorded_delivery import _change as approved_change

    rec = approved_change(client, "S-AUTH-INV")
    record = approval._load(rec["change_id"])  # noqa: SLF001
    record["approver_authority"] = {**record["approver_authority"], "roles": ["nobody"]}
    approval._save(rec["change_id"], record)  # noqa: SLF001
    a = client.get("/changes/S-AUTH-INV", headers=headers("vdb")).json()["lifecycle"]["authorisation"]
    assert a["state"] == "invalidated" and a["renewable"]
