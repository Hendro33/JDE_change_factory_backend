"""
Cross-cutting security tests for the requirements the task called out
explicitly: X-Customer-Id is an assertion, never a grant, and every
customer-scoped call verifies entitlement server-side.
"""

from __future__ import annotations

from jde_mcp_server import backlog

from .conftest import headers


def _seed_story_for(client, story_id: str, customer_id: str) -> None:
    from jde_api_service.services.registry import get_customer_link_service

    backlog.propose_to_backlog(story_id, "story text", {}, "Low", source="Business")
    get_customer_link_service().link(story_id, customer_id)


def test_customer_id_header_cannot_grant_access_beyond_entitlement(client):
    _seed_story_for(client, "S-VDB-ONLY", "vdb")

    # Ellen is only entitled to vdb -- asking for nhd must be refused,
    # not silently scoped to vdb, and not leak nhd's (empty) data either.
    r = client.get("/changes", headers=headers(user="u-ellen", customer="nhd"))
    assert r.status_code == 403

    r = client.get("/changes/S-VDB-ONLY", headers=headers(user="u-ellen", customer="nhd"))
    assert r.status_code == 403


def test_entitled_customer_switch_correctly_scopes_each_view(client):
    _seed_story_for(client, "S-VDB", "vdb")
    _seed_story_for(client, "S-NHD", "nhd")

    # u-hendro (consultant) is entitled to both.
    r = client.get("/changes", headers=headers(user="u-hendro", customer="vdb"))
    assert [c["id"] for c in r.json()] == ["S-VDB"]

    r = client.get("/changes", headers=headers(user="u-hendro", customer="nhd"))
    assert [c["id"] for c in r.json()] == ["S-NHD"]

    # Cross-customer lookup by id must 404, not return the other customer's record.
    r = client.get("/changes/S-NHD", headers=headers(user="u-hendro", customer="vdb"))
    assert r.status_code == 404


def test_metrics_activity_and_backlog_are_all_customer_scoped(client):
    _seed_story_for(client, "S-VDB-M", "vdb")
    _seed_story_for(client, "S-NHD-M", "nhd")

    r = client.get("/metrics", headers=headers(customer="vdb"))
    assert next(t["value"] for t in r.json()["totals"] if t["key"] == "awaiting_domain_owner") == 1

    r = client.get("/activity", headers=headers(customer="mrv"))
    assert r.json() == []

    r = client.get("/backlog", headers=headers(customer="nhd"))
    assert [c["id"] for c in r.json()] == ["S-NHD-M"]


def test_missing_customer_header_is_rejected_on_every_scoped_endpoint(client):
    for method, path in [
        ("get", "/changes"),
        ("get", "/changes/whatever"),
        ("get", "/backlog"),
        ("get", "/metrics"),
        ("get", "/activity"),
    ]:
        r = getattr(client, method)(path, headers=headers(customer=None))
        assert r.status_code == 422, f"{method.upper()} {path} should require X-Customer-Id"
