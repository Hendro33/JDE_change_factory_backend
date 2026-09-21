from __future__ import annotations

from .conftest import headers


def test_create_direct_change_request_appears_in_changes_and_get(client):
    r = client.post(
        "/change-requests",
        headers=headers(),
        json={
            "title": "Fix default document type",
            "businessSource": "Support / Topdesk",
            "sourceReference": "Topdesk #4521",
            "rawContent": "Clerks keep correcting the document type by hand.",
        },
    )
    assert r.status_code == 201
    created = r.json()
    assert created["id"].startswith("CR-")
    assert created["customerId"] == "vdb"
    assert created["sourceType"] == "DIRECT"
    assert created["requester"] == "Hendro"  # server-resolved, not client-supplied

    r = client.get(f"/changes/{created['id']}", headers=headers())
    assert r.status_code == 200
    change = r.json()
    assert change["state"] == "RECEIVED"
    assert change["title"] == "Fix default document type"
    assert change["source"] == "Support / Topdesk"
    assert change["originalRequest"] == "Clerks keep correcting the document type by hand."

    r = client.get("/changes", headers=headers())
    assert len(r.json()) == 1
    assert r.json()[0]["id"] == created["id"]


def test_change_request_is_scoped_to_the_customer_it_was_created_under(client):
    r = client.post(
        "/change-requests",
        headers=headers(customer="vdb"),
        json={"title": "T", "businessSource": "Business", "sourceReference": "", "rawContent": "R"},
    )
    created = r.json()

    # A different, entitled customer for the same caller must not see it.
    r = client.get(f"/changes/{created['id']}", headers=headers(customer="nhd"))
    assert r.status_code == 404

    r = client.get("/changes", headers=headers(customer="nhd"))
    assert r.json() == []


def test_change_request_requester_cannot_be_spoofed_via_body(ellen_client):
    r = ellen_client.post(
        "/change-requests",
        headers=headers(customer="vdb"),
        json={
            "title": "T",
            "businessSource": "Business",
            "sourceReference": "",
            "rawContent": "R",
            "requester": "Someone Else",  # extra field the model doesn't accept
        },
    )
    assert r.status_code == 201
    assert r.json()["requester"] == "Ellen Vos"


def test_create_change_request_requires_entitled_customer(ellen_client):
    r = ellen_client.post(
        "/change-requests",
        headers=headers(customer="nhd"),  # Ellen is not entitled to nhd
        json={"title": "T", "businessSource": "Business", "sourceReference": "", "rawContent": "R"},
    )
    assert r.status_code == 403


def test_create_change_request_requires_customer_header(client):
    r = client.post(
        "/change-requests",
        headers=headers(customer=None),
        json={"title": "T", "businessSource": "Business", "sourceReference": "", "rawContent": "R"},
    )
    assert r.status_code == 422
