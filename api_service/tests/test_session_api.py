from __future__ import annotations


def test_default_identity_is_hendro_with_four_companies(client):
    r = client.get("/session")
    assert r.status_code == 200
    body = r.json()
    assert body["userId"] == "u-hendro"
    assert body["email"] == "hendro@test.local"
    assert {c["id"] for c in body["customers"]} == {"vdb", "nhd", "mrv", "bwm"}
    # Every seeded company lists Hendro's roles on THAT company --
    # the fixture gives him every role everywhere.
    for c in body["customers"]:
        assert set(c["roles"]) == {"admin", "dashboard_viewer", "domain_owner", "product_manager"}


def test_single_company_persona_sees_only_its_own_company(ellen_client):
    r = ellen_client.get("/session")
    assert r.status_code == 200
    body = r.json()
    assert body["userId"] == "u-ellen"
    assert [c["id"] for c in body["customers"]] == ["vdb"]


def test_session_requires_a_valid_session_cookie(client):
    import fastapi.testclient
    from jde_api_service.main import app

    # No cookie at all.
    anon = fastapi.testclient.TestClient(app)
    r = anon.get("/session")
    assert r.status_code == 401

    # A garbage cookie value must fail closed, not be treated as some
    # default/anonymous identity.
    anon.cookies.set("jde_session", "not-a-real-session-token")
    r = anon.get("/session")
    assert r.status_code == 401


def test_logout_revokes_the_session_immediately(client):
    r = client.get("/session")
    assert r.status_code == 200

    r = client.post("/auth/logout")
    assert r.status_code == 200

    # The exact same client/cookie must now be refused -- logout is a
    # real server-side revocation, not just a client-side cookie clear
    # (see auth_service.revoke_session).
    r = client.get("/session")
    assert r.status_code == 401
