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


def test_customer_id_header_cannot_grant_access_beyond_entitlement(client, ellen_client):
    _seed_story_for(client, "S-VDB-ONLY", "vdb")

    # Ellen is only entitled to vdb -- asking for nhd must be refused,
    # not silently scoped to vdb, and not leak nhd's (empty) data either.
    r = ellen_client.get("/changes", headers=headers(customer="nhd"))
    assert r.status_code == 403

    r = ellen_client.get("/changes/S-VDB-ONLY", headers=headers(customer="nhd"))
    assert r.status_code == 403


def test_entitled_customer_switch_correctly_scopes_each_view(client):
    _seed_story_for(client, "S-VDB", "vdb")
    _seed_story_for(client, "S-NHD", "nhd")

    # u-hendro (consultant) is entitled to both.
    r = client.get("/changes", headers=headers(customer="vdb"))
    assert [c["id"] for c in r.json()] == ["S-VDB"]

    r = client.get("/changes", headers=headers(customer="nhd"))
    assert [c["id"] for c in r.json()] == ["S-NHD"]

    # Cross-customer lookup by id must 404, not return the other customer's record.
    r = client.get("/changes/S-NHD", headers=headers(customer="vdb"))
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


def _customer_scoped_routes():
    """Every API route that reads or changes a customer's data: its
    operation takes the X-Customer-Id header (the customer context)."""
    from jde_api_service.main import app

    out = []
    for path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            if any(p.get("in") == "header" and p.get("name", "").lower() == "x-customer-id"
                   for p in op.get("parameters") or []):
                out.append((method.upper(), path))
    return out


def test_a_user_of_one_customer_can_never_reach_another_customers_data_on_any_route(client, ellen_client):
    """Ellen belongs to vdb only. For EVERY customer-scoped route -- stories,
    delivery, technical work, discovery and JDE connection, AI, Jira, users,
    settings, process framework, knowledge -- asking for nhd is refused
    before anything is read or changed."""
    import re

    _seed_story_for(client, "S-NHD-ISO", "nhd")
    routes = _customer_scoped_routes()
    assert len(routes) > 80, len(routes)  # the sweep really covers the API
    for method, path in routes:
        url = re.sub(r"\{[^}]+\}", "S-NHD-ISO", path)
        r = ellen_client.request(method, url, headers=headers("nhd"), json={})
        assert r.status_code == 403, f"{method} {path} answered {r.status_code} for another customer"
