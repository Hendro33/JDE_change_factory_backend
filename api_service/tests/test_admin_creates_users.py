"""Administration › Users & roles: the Administrator creates a person with a password and sets a
new password, without e-mail or links (Hendro, 24 Oct)."""

from __future__ import annotations

import fastapi.testclient

from .conftest import headers


def _signin(email: str, password: str):
    from jde_api_service.main import app

    c = fastapi.testclient.TestClient(app)
    return c, c.post("/auth/login", json={"email": email, "password": password})


def test_the_admin_creates_a_domain_owner_who_can_sign_in_with_that_password(client):
    r = client.post("/admin/users", headers=headers(customer="vdb"), json={
        "email": "do-test@test.local", "displayName": "DO tester", "password": "temp-pass-123", "roles": ["domain_owner"]})
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["domain_owner"]
    c, login = _signin("do-test@test.local", "temp-pass-123")
    assert login.status_code == 200, login.text
    session = c.get("/session", headers=headers(customer="vdb")).json()
    assert "domain_owner" in str(session) and "product_manager" not in str(session.get("roles", ""))


def test_a_short_password_or_no_role_is_refused_with_the_reason(client):
    r = client.post("/admin/users", headers=headers(customer="vdb"), json={
        "email": "short@test.local", "password": "abc", "roles": ["domain_owner"]})
    assert r.status_code == 422 and "at least 8" in r.json()["detail"]
    r = client.post("/admin/users", headers=headers(customer="vdb"), json={
        "email": "norole@test.local", "password": "long-enough-1", "roles": []})
    assert r.status_code == 422


def test_the_admin_sets_a_new_password_and_old_sessions_end(client):
    member = client.post("/admin/users", headers=headers(customer="vdb"), json={
        "email": "am-test@test.local", "password": "first-pass-123", "roles": ["product_manager"]}).json()
    c, login = _signin("am-test@test.local", "first-pass-123")
    assert login.status_code == 200
    r = client.post(f"/admin/users/{member['membershipId']}/password", headers=headers(customer="vdb"),
                    json={"password": "second-pass-456"})
    assert r.status_code == 200, r.text
    assert c.get("/auth/me").status_code == 401                       # the old session ended
    assert _signin("am-test@test.local", "first-pass-123")[1].status_code == 401
    assert _signin("am-test@test.local", "second-pass-456")[1].status_code == 200


def test_an_existing_login_is_added_to_another_customer_without_changing_its_password(client):
    client.post("/admin/users", headers=headers(customer="vdb"), json={
        "email": "both@test.local", "password": "keep-this-123", "roles": ["domain_owner"]})
    r = client.post("/admin/users", headers=headers(customer="bwm"), json={
        "email": "both@test.local", "password": "ignored-999", "roles": ["domain_owner"]})
    assert r.status_code == 200, r.text
    assert _signin("both@test.local", "keep-this-123")[1].status_code == 200
    assert _signin("both@test.local", "ignored-999")[1].status_code == 401
