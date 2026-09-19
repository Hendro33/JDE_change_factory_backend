from __future__ import annotations

from .conftest import headers


def test_default_identity_is_consultant_with_four_customers(client):
    r = client.get("/session")
    assert r.status_code == 200
    body = r.json()
    assert body["userId"] == "u-hendro"
    assert body["role"] == "ConsultIQ Consultant"
    assert {c["id"] for c in body["customers"]} == {"vdb", "nhd", "mrv", "bwm"}
    assert body["activeCustomerId"] == "vdb"


def test_single_customer_persona_sees_only_its_own_customer(client):
    r = client.get("/session", headers=headers(user="u-ellen", customer=None))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "Application Manager"
    assert [c["id"] for c in body["customers"]] == ["vdb"]


def test_unknown_identity_is_rejected(client):
    r = client.get("/session", headers=headers(user="u-does-not-exist", customer=None))
    assert r.status_code == 401


def test_session_never_reveals_customer_data_client_did_not_request(client):
    # Sanity: the entitlement list is exactly the server's table, not
    # something the client could have widened by asking.
    r = client.get(
        "/session",
        headers={"X-Demo-User-Id": "u-ellen", "X-Requested-Customers": "vdb,nhd,mrv"},
    )
    body = r.json()
    assert [c["id"] for c in body["customers"]] == ["vdb"]
