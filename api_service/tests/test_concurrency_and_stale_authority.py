"""
Concurrent role, domain and workflow changes cannot leave stale
authority usable.

  * Membership role/domain/status changes carry the revision the Admin
    loaded (409 stale, 428 absent) and re-check, inside the same SQLite
    write transaction (BEGIN IMMEDIATE), that the acting Admin is STILL
    an Admin. Two concurrent edits: exactly one lands.
  * Domain-review decisions are a stage compare-and-set under the review
    store's lock, with the caller's authority re-read from the database
    inside the lock. Approve racing reject: exactly one lands. A domain
    assignment removed between the request's first check and the write:
    refused.
  * Exact-change approve and reject run under the change's lock; racing
    decisions: exactly one lands. The approver's roles are re-read from
    the database at approval AND immediately before dispatch, inside the
    attempt lock. A role revoked, membership deactivated or user disabled
    after approval leaves the approval unusable -- even when it happens
    between the tool's own checks and the dispatch.
"""

from __future__ import annotations

import threading
import time

import pytest

from jde_mcp_server import backlog

from .conftest import headers, place_in_owned_domain
from .test_stage1_execution_safeguards import _approve, _approved_story, _execute, _full_scope, _propose, _save_scope


def _run_together(*fns):
    """Start every fn at the same moment; return each one's result or exception."""
    barrier = threading.Barrier(len(fns))
    results: list = [None] * len(fns)

    def runner(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 -- collected for the assertion
            results[i] = exc

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def _membership(company: str, user_id: str) -> dict:
    from jde_api_service.services import membership_service

    return next(m for m in membership_service.list_company_members(company) if m["user_id"] == user_id)


def _set_roles(company: str, user_id: str, roles: list[str]) -> None:
    from jde_api_service.persistence.db import connection

    m = _membership(company, user_id)
    with connection() as conn:
        conn.execute("DELETE FROM membership_roles WHERE membership_id = ?", (m["membership_id"],))
        conn.executemany(
            "INSERT INTO membership_roles (membership_id, role) VALUES (?, ?)", [(m["membership_id"], r) for r in roles]
        )


# ---------------------------------------------------------------------
# Membership changes
# ---------------------------------------------------------------------
def test_a_stale_or_missing_membership_revision_is_refused(client):
    ellen = next(m for m in client.get("/admin/users", headers=headers("vdb")).json()["members"] if m["email"] == "ellen@test.local")
    url = f"/admin/users/{ellen['membershipId']}/roles"
    body = {"roles": ["domain_owner"], "domainIds": []}

    assert client.put(url, headers=headers("vdb"), json=body).status_code == 428
    first = client.put(url, headers=headers("vdb"), json={**body, "expectedRevision": ellen["revision"]})
    assert first.status_code == 200 and first.json()["revision"] == ellen["revision"] + 1
    # A second Admin working from the same, now stale, copy.
    stale = client.put(url, headers=headers("vdb"), json={"roles": ["admin"], "domainIds": [], "expectedRevision": ellen["revision"]})
    assert stale.status_code == 409 and stale.json()["currentRevision"] == ellen["revision"] + 1
    assert _membership("vdb", "u-ellen")["roles"] == ["domain_owner"]


def test_two_concurrent_membership_edits_from_the_same_copy_land_exactly_once(client):
    from jde_api_service.persistence.revisions import RevisionConflict
    from jde_api_service.services import membership_service

    m = _membership("vdb", "u-ellen")

    def edit(roles):
        return lambda: membership_service.update_membership_roles(
            m["membership_id"], set(roles), set(), actor_user_id="u-hendro",
            expected_revision=m["revision"], as_admin=True,
        )

    results = _run_together(edit(["domain_owner"]), edit(["product_manager"]))
    assert sum(isinstance(r, int) for r in results) == 1
    assert sum(isinstance(r, RevisionConflict) for r in results) == 1
    assert _membership("vdb", "u-ellen")["revision"] == m["revision"] + 1


def test_an_admin_demoted_mid_request_changes_nothing(client, ellen_client, monkeypatch):
    """Ellen passes the request's Admin check, then -- before her change is
    written -- Hendro removes her Admin role. The write transaction re-checks
    and refuses."""
    from jde_api_service.routers import company_users

    target = _membership("vdb", "u-hendro")
    real = company_users._require_company_domains

    def demote_ellen_first(domain_ids, company_id):
        _set_roles("vdb", "u-ellen", ["product_manager"])  # a concurrent demotion lands here
        return real(domain_ids, company_id)

    monkeypatch.setattr(company_users, "_require_company_domains", demote_ellen_first)
    r = ellen_client.put(
        f"/admin/users/{target['membership_id']}/roles", headers=headers("vdb"),
        json={"roles": ["dashboard_viewer"], "domainIds": [], "expectedRevision": target["revision"]},
    )
    assert r.status_code == 403 and "no longer hold the Admin role" in r.json()["detail"]
    assert "admin" in _membership("vdb", "u-hendro")["roles"]


def test_deactivation_needs_the_current_revision_too(client):
    ellen = _membership("vdb", "u-ellen")
    url = f"/admin/users/{ellen['membership_id']}/deactivate"
    assert client.post(url, headers=headers("vdb")).status_code == 428
    assert client.post(url, headers=headers("vdb"), json={"expectedRevision": ellen["revision"] + 5}).status_code == 409
    assert client.post(url, headers=headers("vdb"), json={"expectedRevision": ellen["revision"]}).status_code == 200


# ---------------------------------------------------------------------
# Domain-review workflow
# ---------------------------------------------------------------------
def _story_in_review(client, story_id: str) -> str:
    from jde_api_service.services.registry import get_customer_link_service

    backlog.propose_to_backlog(story_id, "As a clerk I want X", {}, "Low", source="Business")
    get_customer_link_service().link(story_id, "bwm")
    place_in_owned_domain(client, story_id)
    assert client.post(f"/changes/{story_id}/domain-review/start", headers=headers("bwm"), json={}).status_code == 200
    return story_id


def test_a_domain_assignment_removed_mid_request_blocks_the_approval(client, monkeypatch):
    from jde_api_service.persistence.db import connection
    from jde_api_service.routers import domain_governance
    from jde_api_service.services.registry import get_domain_review_service

    sid = _story_in_review(client, "S-CC-DOMAIN")
    real = domain_governance.require_domain_owner_access
    calls = {"n": 0}

    def removed_after_first_check(ctx, domain_id):
        real(ctx, domain_id)
        calls["n"] += 1
        if calls["n"] == 1:  # the Admin removes the assignment right after the request's own check
            with connection() as conn:
                conn.execute("DELETE FROM domain_assignments WHERE business_domain_id = ?", (domain_id,))

    monkeypatch.setattr(domain_governance, "require_domain_owner_access", removed_after_first_check)
    r = client.post(f"/changes/{sid}/domain-review/approve", headers=headers("bwm"), json={"note": "ok"})
    assert r.status_code == 403 and "not assigned" in r.json()["detail"]
    assert get_domain_review_service().get(sid).stage == "domain_owner_reviewing"
    assert get_domain_review_service().get(sid).domain_owner_approval is None


def test_approve_racing_reject_lands_exactly_once(client):
    from jde_api_service.services.domain_review_service import StageConflict
    from jde_api_service.services.registry import get_domain_review_service

    sid = _story_in_review(client, "S-CC-RACE")
    service = get_domain_review_service()

    def decide(record):
        def run():
            with service.transition(sid, from_stages={"domain_owner_reviewing"}):
                time.sleep(0.05)  # widen the window a check-then-write would lose
                return record(sid, "someone", "note")
        return run

    results = _run_together(decide(service.record_domain_owner_approval), decide(service.record_domain_owner_rejection))
    assert sum(isinstance(r, StageConflict) for r in results) == 1
    final = service.get(sid)
    assert final.stage in {"ready_for_application_manager", "domain_owner_rejected"}
    assert final.domain_owner_approval.status == ("approved" if final.stage == "ready_for_application_manager" else "rejected")


def test_a_product_manager_demoted_mid_request_cannot_authorise_delivery(client, monkeypatch):
    from jde_api_service.routers import domain_governance
    from jde_api_service.services.registry import get_domain_review_service

    sid = _story_in_review(client, "S-CC-AM")
    assert client.post(f"/changes/{sid}/domain-review/approve", headers=headers("bwm"), json={"note": "ok"}).status_code == 200
    real = domain_governance._change_with_story

    def demoted_first(change_id, customer_id):
        _set_roles("bwm", "u-hendro", ["admin", "domain_owner"])
        return real(change_id, customer_id)

    monkeypatch.setattr(domain_governance, "_change_with_story", demoted_first)
    r = client.post(f"/changes/{sid}/domain-review/application-manager-approve", headers=headers("bwm"), json={"note": "go"})
    assert r.status_code == 403
    assert get_domain_review_service().get(sid).stage == "ready_for_application_manager"
    with pytest.raises(backlog.StoryNotApproved):
        backlog.require_approved(sid)  # Gate 1 never cleared


# ---------------------------------------------------------------------
# Exact-change approval and dispatch
# ---------------------------------------------------------------------
def _approved_change(client, story: str) -> dict:
    _save_scope(client, "vdb", _full_scope())
    _approved_story(story)
    change = _propose(story)
    _approve(change["change_id"])
    return change


def test_concurrent_approve_and_reject_land_exactly_once(client):
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-CC-EXACT")
    change = _propose("S-CC-EXACT")
    cid = change["change_id"]
    results = _run_together(
        lambda: _approve(cid),
        lambda: approval.reject_change(cid, "Ellen Vos", "no", company_id="vdb"),
    )
    assert sum(isinstance(r, approval.ChangeApprovalError) for r in results) == 1
    assert approval._load(cid)["status"] in {"approved", "rejected"}


def test_approval_rereads_roles_so_a_stale_session_cannot_approve(client):
    from jde_mcp_server import approval

    _save_scope(client, "vdb", _full_scope())
    _approved_story("S-CC-STALE-APPROVE")
    change = _propose("S-CC-STALE-APPROVE")
    _set_roles("vdb", "u-hendro", ["admin"])  # the session still says product_manager
    with pytest.raises(approval.ApproverNotAuthorised, match="no longer holds"):
        _approve(change["change_id"])
    assert approval._load(change["change_id"])["status"] == "pending"


@pytest.mark.parametrize("revoke", ["role", "membership", "user"])
def test_authority_lost_after_approval_blocks_execution(client, revoke):
    from jde_api_service.persistence.db import connection
    from jde_mcp_server import approval, execution

    change = _approved_change(client, f"S-CC-LOST-{revoke}")
    assert change is not None
    if revoke == "role":
        _set_roles("vdb", "u-hendro", ["admin", "domain_owner"])
    else:
        with connection() as conn:
            if revoke == "membership":
                conn.execute("UPDATE company_memberships SET status = 'inactive' WHERE user_id = 'u-hendro' AND company_id = 'vdb'")
            else:
                conn.execute("UPDATE users SET is_active = 0 WHERE id = 'u-hendro'")
    with pytest.raises(approval.ChangeApprovalError, match="no longer holds"):
        _execute(f"S-CC-LOST-{revoke}", change["change_id"])
    assert execution.effective_state(approval._load(change["change_id"])) == "ready"


def test_authority_lost_between_the_tools_checks_and_dispatch_is_caught_inside_the_lock(client, monkeypatch):
    """The role is revoked after set_processing_option's own checks passed
    but before the attempt is recorded: the revalidation inside the
    attempt lock refuses, no attempt is recorded and JDE is untouched."""
    from jde_mcp_server import ais_client, approval, execution

    change = _approved_change(client, "S-CC-WINDOW")
    real_read = ais_client._mock_read

    def revoke_then_read(*args):
        _set_roles("vdb", "u-hendro", ["admin"])
        return real_read(*args)

    monkeypatch.setattr(ais_client, "_mock_read", revoke_then_read)
    with pytest.raises(approval.ChangeApprovalError, match="no longer holds"):
        _execute("S-CC-WINDOW", change["change_id"])
    record = approval._load(change["change_id"])
    assert execution.effective_state(record) == "ready"
    assert not (record.get("execution") or {}).get("write", {}).get("attempts")
    assert real_read(ais_client.sim_target("vdb", "P4210", "CIQ0001", "PDOCTYPE")) != "SO"


def test_without_the_membership_database_nothing_runs(client, monkeypatch):
    from jde_mcp_server import approval

    change = _approved_change(client, "S-CC-NODB")
    monkeypatch.delenv("JDE_AUTH_DB_PATH")
    with pytest.raises(approval.ChangeApprovalError, match="cannot be checked"):
        _execute("S-CC-NODB", change["change_id"])


def test_an_approval_without_an_approver_identity_cannot_run(client):
    from jde_mcp_server import approval

    from .test_stage1_execution_safeguards import _edit_change_record

    change = _approved_change(client, "S-CC-NOID")
    record = approval._load(change["change_id"])
    authority = dict(record["approver_authority"])
    authority.pop("user_id")
    _edit_change_record(change["change_id"], approver_authority=authority)
    with pytest.raises(approval.ChangeApprovalError, match="no approver identity"):
        _execute("S-CC-NOID", change["change_id"])
