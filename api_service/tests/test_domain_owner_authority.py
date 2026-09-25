"""
Domain Owner authority comes only from domain_assignments (Admin > Users),
never from the free-text BusinessDomain.domain_owner note -- and a story
cannot be moved into someone's domain to hand them the decision.

These close the paths the earlier build left open:
  * a story with NO domain could be approved by any Domain Owner;
  * any writer (a Domain Owner included) could re-point a story's domain,
    even mid-review, and then approve it;
  * an Admin could assign another company's domain id to a member.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jde_mcp_server import backlog

from .conftest import TEST_PASSWORD, _apply_csrf_header, headers, place_in_owned_domain


def _member(user_id: str, email: str, name: str, roles: list[str], domains: list[str], company: str = "bwm") -> TestClient:
    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user(email, TEST_PASSWORD, name, user_id=user_id)
    membership_service.create_membership(user_id, company, roles, domains, created_by="u-hendro")
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": TEST_PASSWORD}).status_code == 200
    _apply_csrf_header(c)
    return c


def _story(client, story_id: str) -> str:
    from jde_api_service.services.registry import get_customer_link_service

    backlog.propose_to_backlog(story_id, "As a clerk I want X", {}, "Low", source="Business")
    get_customer_link_service().link(story_id, "bwm")
    client.get(f"/changes/{story_id}/domain-review", headers=headers("bwm"))
    return story_id


def test_a_story_without_a_domain_has_no_domain_owner(client):
    owner = _member("u-own", "own@test.local", "Owen Owner", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    sid = _story(client, "S-NO-DOMAIN")
    r = owner.post(f"/changes/{sid}/domain-review/start", headers=headers("bwm"), json={})
    assert r.status_code == 403
    assert "no business domain" in r.json()["detail"]


def test_a_domain_owner_cannot_move_a_story_into_their_own_domain(client):
    owner = _member("u-own", "own@test.local", "Owen Owner", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    sid = _story(client, "S-MOVE")
    r = owner.post(
        f"/changes/{sid}/domain-review/assign-domain", headers=headers("bwm"),
        json={"businessDomainId": "DOM-BWM-WAREHOUSE"},
    )
    assert r.status_code == 403


def test_the_domain_cannot_change_once_domain_owner_review_has_started(client):
    sid = _story(client, "S-STARTED")
    place_in_owned_domain(client, sid)
    assert client.post(f"/changes/{sid}/domain-review/start", headers=headers("bwm"), json={}).status_code == 200
    r = client.post(
        f"/changes/{sid}/domain-review/assign-domain", headers=headers("bwm"),
        json={"businessDomainId": "DOM-BWM-WAREHOUSE"},
    )
    assert r.status_code == 409


def test_assigned_owners_are_derived_from_assignments_not_the_free_text_note(client):
    from jde_api_service.services import membership_service

    _member("u-own", "own@test.local", "Owen Owner", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    # Assigned but without the domain_owner role: not an owner.
    _member("u-pm", "pm@test.local", "Pat Manager", ["product_manager"], ["DOM-BWM-WAREHOUSE"])
    # Domain owner role and assignment, but deactivated: not an owner.
    _member("u-gone", "gone@test.local", "Gina Gone", ["domain_owner"], ["DOM-BWM-WAREHOUSE"])
    gone = membership_service.get_membership_by_id(
        next(m["membership_id"] for m in membership_service.list_company_members("bwm") if m["user_id"] == "u-gone")
    )
    membership_service.set_membership_status(gone["id"], "inactive", actor_user_id="u-hendro")

    domains = {d["id"]: d for d in client.get("/business-domains", headers=headers("bwm")).json()}
    assert domains["DOM-BWM-WAREHOUSE"]["assignedOwners"] == ["Owen Owner"]
    assert domains["DOM-BWM-CREDIT"]["assignedOwners"] == []


def test_member_domains_must_belong_to_the_same_company(client):
    from jde_api_service.services import membership_service

    ellen = next(m for m in membership_service.list_company_members("vdb") if m["user_id"] == "u-ellen")
    r = client.put(
        f"/admin/users/{ellen['membership_id']}/roles", headers=headers("vdb"),
        json={"roles": ["domain_owner"], "domainIds": ["DOM-BWM-WAREHOUSE"], "expectedRevision": ellen["revision"]},
    )
    assert r.status_code == 422
