"""
Login/logout, password reset, invitations (create/accept/expire/revoke),
multi-role and domain-owner scoping, Dashboard Viewer's read-only
restriction, the last-Admin protection, and Jira settings surviving a
simulated backend restart.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from .conftest import TEST_PASSWORD, _apply_csrf_header, headers


def _token_from(preview_url: str, param: str) -> str:
    return parse_qs(urlsplit(preview_url).query)[param][0]


# ---------------------------------------------------------------------
# CSRF -- the double-submit cookie check (dependencies.verify_csrf_if_unsafe).
# `client` already carries a valid X-CSRF-Token (conftest.py's
# _apply_csrf_header, mirroring what a real browser's JS does after
# reading the jde_csrf cookie) -- these tests specifically UNDO that to
# prove the check is real, not just present.
# ---------------------------------------------------------------------
def test_write_without_csrf_header_is_refused(client):
    csrf_cookie = client.cookies.get("jde_csrf")
    assert csrf_cookie, "login must set the CSRF cookie"
    del client.headers["X-CSRF-Token"]
    try:
        r = client.put("/admin/jira-integration", headers=headers(customer="vdb"), json={
            "baseUrl": "https://x.atlassian.net", "projectKey": "X", "pickupStatus": "Ready",
            "postPickupStatus": "In Progress", "jadeIdField": "customfield_1", "requestTypeField": "",
        })
        assert r.status_code == 403
    finally:
        client.headers["X-CSRF-Token"] = csrf_cookie


def test_write_with_mismatched_csrf_header_is_refused(client):
    original = client.headers["X-CSRF-Token"]
    client.headers["X-CSRF-Token"] = "a-value-that-does-not-match-the-cookie"
    try:
        r = client.put("/admin/jira-integration", headers=headers(customer="vdb"), json={
            "baseUrl": "https://x.atlassian.net", "projectKey": "X", "pickupStatus": "Ready",
            "postPickupStatus": "In Progress", "jadeIdField": "customfield_1", "requestTypeField": "",
        })
        assert r.status_code == 403
    finally:
        client.headers["X-CSRF-Token"] = original


def test_reads_never_require_a_csrf_header(client):
    del client.headers["X-CSRF-Token"]
    try:
        assert client.get("/session").status_code == 200
        assert client.get("/admin/jira-integration/status", headers=headers(customer="vdb")).status_code == 200
    finally:
        client.headers["X-CSRF-Token"] = client.cookies.get("jde_csrf")


# ---------------------------------------------------------------------
# Login / logout / password reset
# ---------------------------------------------------------------------
def test_login_rejects_wrong_password(client):
    r = client.post("/auth/login", json={"email": "hendro@test.local", "password": "wrong-password"})
    assert r.status_code == 401


def test_login_rejects_unknown_email(client):
    r = client.post("/auth/login", json={"email": "nobody@test.local", "password": TEST_PASSWORD})
    assert r.status_code == 401


def test_forgot_password_never_reveals_whether_an_email_is_registered(client):
    known = client.post("/auth/forgot-password", json={"email": "hendro@test.local"})
    unknown = client.post("/auth/forgot-password", json={"email": "nobody@test.local"})
    assert known.status_code == 200 and unknown.status_code == 200
    assert known.json()["ok"] is True and unknown.json()["ok"] is True
    # Dev-preview mode surfaces a link only when the account exists --
    # see email_service.py's own docstring on this being an accepted
    # prototype trade-off.
    assert known.json()["previewUrl"] is not None
    assert unknown.json()["previewUrl"] is None


def test_password_reset_end_to_end_and_revokes_existing_sessions(client):
    forgot = client.post("/auth/forgot-password", json={"email": "hendro@test.local"})
    token = _token_from(forgot.json()["previewUrl"], "resetToken")

    reset = client.post("/auth/reset-password", json={"token": token, "newPassword": "a-new-password-999"})
    assert reset.status_code == 200

    # The session that existed before the reset is now dead.
    r = client.get("/session")
    assert r.status_code == 401

    # Old password no longer works; new one does.
    assert client.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD}).status_code == 401
    relogin = client.post("/auth/login", json={"email": "hendro@test.local", "password": "a-new-password-999"})
    assert relogin.status_code == 200


def test_reset_token_is_single_use(client):
    forgot = client.post("/auth/forgot-password", json={"email": "hendro@test.local"})
    token = _token_from(forgot.json()["previewUrl"], "resetToken")

    first = client.post("/auth/reset-password", json={"token": token, "newPassword": "first-new-password"})
    assert first.status_code == 200
    second = client.post("/auth/reset-password", json={"token": token, "newPassword": "second-new-password"})
    assert second.status_code == 400


# ---------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------
def test_invite_preview_accept_new_user_end_to_end(client):
    invite = client.post(
        "/admin/users/invite",
        headers=headers(customer="vdb"),
        json={"email": "newbie@test.local", "roles": ["domain_owner"], "domainIds": ["DOM-BWM-WAREHOUSE"]},
    )
    assert invite.status_code == 200
    body = invite.json()
    assert body["status"] == "pending"
    token = _token_from(body["previewUrl"], "acceptInvitation")

    preview = client.get(f"/auth/invitation/{token}/preview")
    assert preview.status_code == 200
    assert preview.json() == {
        "email": "newbie@test.local", "companyName": "Van den Berg Logistiek",
        "roles": ["domain_owner"], "requiresPassword": True, "valid": True, "reason": None,
    }

    accept = client.post("/auth/accept-invitation", json={"token": token, "password": "a-brand-new-password"})
    assert accept.status_code == 200
    assert accept.json()["email"] == "newbie@test.local"

    # The new account can log in on its own -- via a SEPARATE client, so
    # this doesn't overwrite `client`'s own Hendro session cookie (the
    # accept-invitation call above already logged `client` in as newbie).
    import fastapi.testclient

    from jde_api_service.main import app

    separate = fastapi.testclient.TestClient(app)
    login = separate.post("/auth/login", json={"email": "newbie@test.local", "password": "a-brand-new-password"})
    assert login.status_code == 200

    # And shows up in the company's member list with the granted role
    # (checked back as Hendro, an Admin, not as newbie -- who isn't one).
    hendro = fastapi.testclient.TestClient(app)
    hendro.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD})
    listing = hendro.get("/admin/users", headers=headers(customer="vdb"))
    member = next(m for m in listing.json()["members"] if m["email"] == "newbie@test.local")
    assert member["status"] == "active"
    assert member["roles"] == ["domain_owner"]
    assert member["domainIds"] == ["DOM-BWM-WAREHOUSE"]


def test_expired_invitation_cannot_be_accepted(client, monkeypatch):
    from jde_api_service.services import invitation_service

    monkeypatch.setattr(invitation_service, "INVITATION_TTL_DAYS", -1)
    invite = client.post(
        "/admin/users/invite", headers=headers(customer="vdb"),
        json={"email": "late@test.local", "roles": ["dashboard_viewer"], "domainIds": []},
    )
    token = _token_from(invite.json()["previewUrl"], "acceptInvitation")

    preview = client.get(f"/auth/invitation/{token}/preview")
    assert preview.json()["valid"] is False
    assert preview.json()["reason"] == "invitation is expired"

    accept = client.post("/auth/accept-invitation", json={"token": token, "password": "whatever-1234"})
    assert accept.status_code == 400


def test_revoked_invitation_cannot_be_accepted(client):
    invite = client.post(
        "/admin/users/invite", headers=headers(customer="vdb"),
        json={"email": "revoke-me@test.local", "roles": ["dashboard_viewer"], "domainIds": []},
    )
    token = _token_from(invite.json()["previewUrl"], "acceptInvitation")
    invitation_id = invite.json()["id"]

    revoked = client.post(f"/admin/users/invitations/{invitation_id}/revoke", headers=headers(customer="vdb"))
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    accept = client.post("/auth/accept-invitation", json={"token": token, "password": "whatever-1234"})
    assert accept.status_code == 400


def test_resend_invitation_invalidates_the_old_link(client):
    invite = client.post(
        "/admin/users/invite", headers=headers(customer="vdb"),
        json={"email": "resend-me@test.local", "roles": ["dashboard_viewer"], "domainIds": []},
    )
    old_token = _token_from(invite.json()["previewUrl"], "acceptInvitation")

    resent = client.post(f"/admin/users/invitations/{invite.json()['id']}/resend", headers=headers(customer="vdb"))
    new_token = _token_from(resent.json()["previewUrl"], "acceptInvitation")
    assert new_token != old_token

    assert client.get(f"/auth/invitation/{old_token}/preview").json()["valid"] is False
    assert client.get(f"/auth/invitation/{new_token}/preview").json()["valid"] is True


def test_invitation_management_requires_admin_role(viewer_client):
    r = viewer_client.post(
        "/admin/users/invite", headers=headers(customer="vdb"),
        json={"email": "x@test.local", "roles": ["dashboard_viewer"], "domainIds": []},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------
# Multiple roles, domain-owner scoping
# ---------------------------------------------------------------------
def test_domain_owner_is_scoped_to_assigned_domains_only(client):
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user("scoped-owner@test.local", TEST_PASSWORD, "Scoped Owner", user_id="u-scoped")
    membership_service.create_membership(
        "u-scoped", "bwm", ["domain_owner"], ["DOM-BWM-WAREHOUSE"], created_by="u-hendro"
    )
    import fastapi.testclient

    from jde_api_service.main import app

    scoped = fastapi.testclient.TestClient(app)
    scoped.post("/auth/login", json={"email": "scoped-owner@test.local", "password": TEST_PASSWORD})
    _apply_csrf_header(scoped)

    # Seed a change/story and assign it to a DIFFERENT domain than the
    # one this Domain Owner is assigned to.
    from jde_mcp_server import backlog

    from jde_api_service.services.registry import get_customer_link_service

    backlog.propose_to_backlog("S-SCOPE-TEST", "story text", {}, "Low", source="Business")
    get_customer_link_service().link("S-SCOPE-TEST", "bwm")
    client.get(f"/changes/S-SCOPE-TEST/domain-review", headers=headers(customer="bwm"))
    client.post(
        "/changes/S-SCOPE-TEST/domain-review/assign-domain", headers=headers(customer="bwm"),
        json={"businessDomainId": "DOM-BWM-CREDIT", "uncertain": False, "note": ""},
    )

    r = scoped.post(
        "/changes/S-SCOPE-TEST/domain-review/start", headers=headers(customer="bwm"),
        json={"decidedBy": "Scoped Owner"},
    )
    assert r.status_code == 403

    # Re-assign to the domain this Domain Owner IS assigned to -- now allowed.
    client.post(
        "/changes/S-SCOPE-TEST/domain-review/assign-domain", headers=headers(customer="bwm"),
        json={"businessDomainId": "DOM-BWM-WAREHOUSE", "uncertain": False, "note": ""},
    )
    r = scoped.post(
        "/changes/S-SCOPE-TEST/domain-review/start", headers=headers(customer="bwm"),
        json={"decidedBy": "Scoped Owner"},
    )
    assert r.status_code == 200


def test_product_manager_role_required_for_application_manager_gate(viewer_client, client):
    from jde_mcp_server import backlog

    from jde_api_service.services.registry import get_customer_link_service

    backlog.propose_to_backlog("S-PM-TEST", "story text", {}, "Low", source="Business")
    get_customer_link_service().link("S-PM-TEST", "vdb")
    client.get("/changes/S-PM-TEST/domain-review", headers=headers(customer="vdb"))

    r = viewer_client.post(
        "/changes/S-PM-TEST/domain-review/application-manager-approve",
        headers=headers(customer="vdb"), json={"decidedBy": "Viv Viewer"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------
# Dashboard Viewer: read-only
# ---------------------------------------------------------------------
def test_dashboard_viewer_can_read_but_not_write(viewer_client):
    ok = viewer_client.get("/changes", headers=headers(customer="vdb"))
    assert ok.status_code == 200

    blocked = viewer_client.post(
        "/change-requests", headers=headers(customer="vdb"),
        json={"title": "T", "businessSource": "Business", "sourceReference": "", "rawContent": "R"},
    )
    assert blocked.status_code == 403


# ---------------------------------------------------------------------
# Last-Admin protection
# ---------------------------------------------------------------------
def test_cannot_demote_the_last_active_admin(client):
    # "mrv" -- Hendro is the ONLY member (Ellen isn't); on "vdb" Ellen's
    # admin role would otherwise make this a false negative.
    listing = client.get("/admin/users", headers=headers(customer="mrv")).json()
    hendro_membership = next(m for m in listing["members"] if m["email"] == "hendro@test.local")

    r = client.put(
        f"/admin/users/{hendro_membership['membershipId']}/roles", headers=headers(customer="mrv"),
        json={"roles": ["domain_owner"], "domainIds": []},  # dropping admin
    )
    assert r.status_code == 409


def test_cannot_deactivate_the_last_active_admin(client):
    listing = client.get("/admin/users", headers=headers(customer="mrv")).json()
    hendro_membership = next(m for m in listing["members"] if m["email"] == "hendro@test.local")

    r = client.post(f"/admin/users/{hendro_membership['membershipId']}/deactivate", headers=headers(customer="mrv"))
    assert r.status_code == 409


def test_deactivating_a_non_admin_membership_succeeds_and_blocks_access(client, ellen_client):
    listing = client.get("/admin/users", headers=headers(customer="vdb")).json()
    ellen_membership = next(m for m in listing["members"] if m["email"] == "ellen@test.local")

    r = client.post(f"/admin/users/{ellen_membership['membershipId']}/deactivate", headers=headers(customer="vdb"))
    assert r.status_code == 200
    assert r.json()["status"] == "inactive"

    # Ellen's EXISTING session is still a valid login -- but her company
    # access is gone immediately, without her needing to log out/in.
    denied = ellen_client.get("/changes", headers=headers(customer="vdb"))
    assert denied.status_code == 403


# ---------------------------------------------------------------------
# Jira settings survive a simulated backend restart -- two independent
# TestClient app instances (separate startup lifecycles) pointed at the
# exact same isolated_dirs SQLite file, proving durability the same way
# stopping/restarting a real uvicorn process would.
# ---------------------------------------------------------------------
def test_jira_settings_survive_a_simulated_restart(client, isolated_dirs):
    import fastapi.testclient

    from jde_api_service.main import app

    client.put(
        "/admin/jira-integration", headers=headers(customer="vdb"),
        json={
            "baseUrl": "https://durable-test.atlassian.net", "projectKey": "DUR",
            "pickupStatus": "Ready", "postPickupStatus": "In Progress",
            "jadeIdField": "customfield_1", "requestTypeField": "", "updatedBy": "Hendro",
        },
    )
    client.put(
        "/admin/jira-credentials", headers=headers(customer="vdb"),
        json={"email": "bot@durable-test.com", "apiToken": "durable-token-value", "updatedBy": "Hendro"},
    )

    # A second, independent app lifecycle -- exactly what happens when
    # a real deployment's process restarts -- pointed at the same data
    # directory (isolated_dirs' monkeypatched settings.data_dir is
    # still in effect for this test).
    with fastapi.testclient.TestClient(app) as restarted:
        login = restarted.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD})
        assert login.status_code == 200, "the users table itself must also have survived the restart"

        status = restarted.get("/admin/jira-integration/status", headers=headers(customer="vdb"))
        assert status.json() == {"mockMode": False, "credentialsConfigured": True, "configConfigured": True}

        config = restarted.get("/admin/jira-integration", headers=headers(customer="vdb"))
        assert config.json()["baseUrl"] == "https://durable-test.atlassian.net"
        assert config.json()["projectKey"] == "DUR"
