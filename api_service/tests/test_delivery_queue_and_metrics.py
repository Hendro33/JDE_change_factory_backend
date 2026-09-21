"""
Tests for the Delivery Queue (Increment: Continuous Delivery Flow) and
the redefined, unambiguous dashboard totals. No Sprint concept exists
anywhere here -- the queue is a plain ordered list one human decision
(Application Manager approval) adds entries to.
"""

from __future__ import annotations

import json

import claude_agent_sdk as sdk

from .conftest import headers
from .test_domain_governance import _fake_enhance_success


def _total(metrics: dict, key: str) -> int:
    return next(t["value"] for t in metrics["totals"] if t["key"] == key)


def _seed_and_enhance(client, monkeypatch, source_reference: str) -> str:
    monkeypatch.setattr(sdk, "query", _fake_enhance_success)
    r = client.post(
        "/change-requests",
        headers=headers(customer="bwm"),
        json={
            "title": "Default delivery date is wrong",
            "businessSource": "Support / Topdesk",
            "sourceReference": source_reference,
            "rawContent": "When our sales team enters a new sales order...",
        },
    )
    request_id = r.json()["id"]
    client.post(f"/changes/{request_id}/enhance", headers=headers(customer="bwm"))
    return request_id


def _through_domain_owner_approval(client, change_id: str) -> None:
    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer="bwm"))
    client.post(f"/changes/{change_id}/domain-review/start", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})
    client.post(f"/changes/{change_id}/domain-review/approve", headers=headers(customer="bwm"), json={"decidedBy": "Ellen Vos"})


def test_delivery_queue_starts_empty(client):
    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    assert r.status_code == 200
    assert r.json() == []


def test_application_manager_approval_adds_to_delivery_queue_not_a_sprint(client, monkeypatch):
    change_id = _seed_and_enhance(client, monkeypatch, "Topdesk T001")
    _through_domain_owner_approval(client, change_id)

    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    assert r.json() == []  # not queued yet -- Application Manager hasn't acted

    r = client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro", "note": "Ready for Jade to pick up."},
    )
    assert r.status_code == 200

    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    entries = r.json()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["changeId"] == change_id
    assert entry["customerId"] == "bwm"
    assert entry["position"] == 1
    assert entry["status"] == "queued"
    assert entry["addedBy"] == "Hendro"
    # No sprint vocabulary anywhere on the wire shape.
    assert "sprint" not in json.dumps(entry).lower()


def test_domain_owner_approval_alone_never_queues_the_change(client, monkeypatch):
    change_id = _seed_and_enhance(client, monkeypatch, "Topdesk T001")
    _through_domain_owner_approval(client, change_id)

    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    assert r.json() == []


def test_delivery_queue_is_customer_scoped(client, monkeypatch):
    change_id = _seed_and_enhance(client, monkeypatch, "Topdesk T001")
    _through_domain_owner_approval(client, change_id)
    client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )

    r = client.get("/delivery-queue", headers=headers(customer="vdb"))
    assert r.json() == []

    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    assert len(r.json()) == 1


def test_delivery_queue_entries_are_ordered_and_add_is_idempotent(client, monkeypatch):
    from jde_api_service.services.registry import get_delivery_queue_service

    c1 = _seed_and_enhance(client, monkeypatch, "Topdesk T001")
    _through_domain_owner_approval(client, c1)
    client.post(f"/changes/{c1}/domain-review/application-manager-approve", headers=headers(customer="bwm"), json={"decidedBy": "Hendro"})

    c2 = _seed_and_enhance(client, monkeypatch, "Topdesk T004")
    _through_domain_owner_approval(client, c2)
    client.post(f"/changes/{c2}/domain-review/application-manager-approve", headers=headers(customer="bwm"), json={"decidedBy": "Hendro"})

    r = client.get("/delivery-queue", headers=headers(customer="bwm"))
    entries = r.json()
    assert [e["changeId"] for e in entries] == [c1, c2]
    assert [e["position"] for e in entries] == [1, 2]

    # Calling add() again for an already-queued change is a no-op --
    # same position, not duplicated or reordered.
    service = get_delivery_queue_service()
    before = service.get(c1)
    again = service.add(c1, "bwm", "Someone Else")
    assert again == before


def test_dashboard_totals_are_unambiguous_and_derived(client, monkeypatch):
    r = client.get("/metrics", headers=headers(customer="bwm"))
    metrics = r.json()
    keys = {t["key"] for t in metrics["totals"]}
    assert keys == {
        "incoming_requests", "awaiting_domain_owner", "awaiting_application_manager",
        "awaiting_exact_change_approval", "in_delivery", "awaiting_business_validation", "completed",
    }
    # BicycleWorks pilot seed: 11 requests, none enhanced yet in this
    # isolated test run.
    assert _total(metrics, "incoming_requests") == 11
    assert _total(metrics, "awaiting_domain_owner") == 0
    assert _total(metrics, "awaiting_application_manager") == 0
    assert _total(metrics, "in_delivery") == 0

    # _seed_and_enhance creates a NEW ChangeRequest (a 12th record,
    # distinct from the 11 pilot-seeded ones) and enhances it straight
    # through to BACKLOG_READY -- so it never shows up as "incoming"
    # here, and the seeded 11 are untouched.
    change_id = _seed_and_enhance(client, monkeypatch, "Topdesk T001")

    r = client.get("/metrics", headers=headers(customer="bwm"))
    metrics = r.json()
    assert _total(metrics, "incoming_requests") == 11
    # Nobody has opened the Domain Owner review yet -- still honestly "awaiting".
    assert _total(metrics, "awaiting_domain_owner") == 1
    assert _total(metrics, "awaiting_application_manager") == 0

    _through_domain_owner_approval(client, change_id)
    r = client.get("/metrics", headers=headers(customer="bwm"))
    metrics = r.json()
    assert _total(metrics, "awaiting_domain_owner") == 0  # Domain Owner acted
    assert _total(metrics, "awaiting_application_manager") == 1  # Gate 1 now open
    assert _total(metrics, "in_delivery") == 0  # Application Manager hasn't acted

    client.post(
        f"/changes/{change_id}/domain-review/application-manager-approve",
        headers=headers(customer="bwm"),
        json={"decidedBy": "Hendro"},
    )
    r = client.get("/metrics", headers=headers(customer="bwm"))
    metrics = r.json()
    assert _total(metrics, "awaiting_application_manager") == 0  # state is now APPROVED
    assert _total(metrics, "in_delivery") == 1
