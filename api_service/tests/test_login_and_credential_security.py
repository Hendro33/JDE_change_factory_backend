"""
Before public deployment or real-token use:

  * sign-in is rate limited per account and per client, and the limits
    survive a restart;
  * stored Jira API tokens are encrypted with a key that lives only in
    the server environment -- never in the database or its backups;
  * the anonymous forgot-password endpoint never returns a reset link,
    and the Admin-issued link cannot reach an account that also belongs
    to a company the Admin does not administer.
"""

from __future__ import annotations

import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from .conftest import TEST_PASSWORD, _apply_csrf_header, headers


def _anon():
    from jde_api_service.main import app

    return TestClient(app)


def _login(c, email: str, password: str):
    return c.post("/auth/login", json={"email": email, "password": password})


def _db_path() -> str:
    from jde_api_service.persistence.db import db_path

    return db_path()


# ---------------------------------------------------------------------
# Sign-in rate limiting
# ---------------------------------------------------------------------
def test_an_account_is_locked_after_repeated_failures_even_with_the_right_password(client):
    from jde_api_service.services import login_throttle

    c = _anon()
    for _ in range(login_throttle.ACCOUNT_LIMIT):
        assert _login(c, "ellen@test.local", "wrong").status_code == 401
    r = _login(c, "ellen@test.local", TEST_PASSWORD)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    # Another account from the same client is unaffected.
    assert _login(c, "hendro@test.local", TEST_PASSWORD).status_code == 200


def test_a_successful_sign_in_clears_the_account_failures(client):
    from jde_api_service.services import login_throttle

    c = _anon()
    for _ in range(login_throttle.ACCOUNT_LIMIT - 1):
        _login(c, "ellen@test.local", "wrong")
    assert _login(c, "ellen@test.local", TEST_PASSWORD).status_code == 200
    for _ in range(login_throttle.ACCOUNT_LIMIT - 1):
        _login(c, "ellen@test.local", "wrong")
    assert _login(c, "ellen@test.local", TEST_PASSWORD).status_code == 200


def test_one_client_spraying_many_accounts_is_limited(client):
    from jde_api_service.services import login_throttle

    c = _anon()
    for i in range(login_throttle.CLIENT_LIMIT):
        _login(c, f"nobody{i}@test.local", "wrong")
    assert _login(c, "hendro@test.local", TEST_PASSWORD).status_code == 429


def test_the_limit_survives_a_restart_and_expires_after_the_window(client):
    from jde_api_service.persistence.db import connection
    from jde_api_service.services import login_throttle

    c = _anon()
    for _ in range(login_throttle.ACCOUNT_LIMIT):
        _login(c, "ellen@test.local", "wrong")
    from jde_api_service.main import app

    with TestClient(app) as restarted:
        assert _login(restarted, "ellen@test.local", TEST_PASSWORD).status_code == 429
    with connection() as conn:
        conn.execute("UPDATE login_failures SET failed_at = ?", (time.time() - login_throttle.WINDOW_SECONDS - 1,))
    assert _login(_anon(), "ellen@test.local", TEST_PASSWORD).status_code == 200


# ---------------------------------------------------------------------
# Jira credential encryption
# ---------------------------------------------------------------------
def _save_token(client, token: str = "real-looking-token-123"):
    return client.put(
        "/admin/jira-credentials", headers=headers("vdb"), json={"email": "bot@example.com", "apiToken": token}
    )


def _stored_token() -> str:
    conn = sqlite3.connect(_db_path())
    try:
        return conn.execute("SELECT api_token FROM jira_credentials WHERE company_id = 'vdb'").fetchone()[0]
    finally:
        conn.close()


def test_the_token_is_stored_encrypted_and_the_database_file_never_holds_it(client):
    assert _save_token(client).status_code == 200
    stored = _stored_token()
    assert stored.startswith("enc:v1:")
    assert "real-looking-token-123" not in stored
    # A backup is a copy of this file: the token is not in it anywhere.
    assert b"real-looking-token-123" not in open(_db_path(), "rb").read()

    from jde_api_service.services.registry import get_jira_credentials_service

    assert get_jira_credentials_service().get_for_customer("vdb").api_token == "real-looking-token-123"
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialStorage"] == "encrypted"


def test_without_a_key_nothing_is_saved(client, monkeypatch):
    monkeypatch.delenv("JDE_CREDENTIAL_KEY")
    r = _save_token(client)
    assert r.status_code == 409
    assert "not configured" in r.json()["detail"]
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialStorage"] == "none"
    assert status["credentialEncryptionAvailable"] is False


def test_a_token_under_a_key_the_server_does_not_have_is_unreadable_and_unused(client, monkeypatch):
    _save_token(client)
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())  # the old key is gone
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialStorage"] == "unreadable"
    assert status["credentialsConfigured"] is False
    assert status["mockMode"] is True  # falls back to the mock gateway, never a half-working live one


def test_key_rotation_re_encrypts_on_start(client, monkeypatch):
    from jde_api_service.main import app
    from jde_api_service.services import credential_crypto
    from jde_api_service.services.registry import get_jira_credentials_service

    old_key = __import__("os").environ["JDE_CREDENTIAL_KEY"]
    _save_token(client)
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("JDE_CREDENTIAL_KEY", new_key)
    monkeypatch.setenv("JDE_CREDENTIAL_KEY_PREVIOUS", old_key)
    with TestClient(app):  # restart
        pass
    assert credential_crypto.stored_key_id(_stored_token()) == credential_crypto.current_key_id()
    monkeypatch.delenv("JDE_CREDENTIAL_KEY_PREVIOUS")  # the old key can now be retired
    assert get_jira_credentials_service().get_for_customer("vdb").api_token == "real-looking-token-123"


def test_a_legacy_plaintext_token_is_reported_then_encrypted_on_start(client):
    from jde_api_service.main import app
    from jde_api_service.persistence.db import connection
    from jde_api_service.services.registry import get_jira_credentials_service

    with connection() as conn:
        conn.execute(
            "INSERT INTO jira_credentials (company_id, email, api_token, updated_at, updated_by, revision) "
            "VALUES ('vdb', 'bot@example.com', 'legacy-plain-token', '2026-09-01T00:00:00+00:00', 'Old Build', 1)"
        )
    status = client.get("/admin/jira-integration/status", headers=headers("vdb")).json()
    assert status["credentialStorage"] == "plaintext (legacy)"
    with TestClient(app):  # restart with a key configured
        pass
    assert _stored_token().startswith("enc:v1:")
    assert get_jira_credentials_service().get_for_customer("vdb").api_token == "legacy-plain-token"


# ---------------------------------------------------------------------
# Password reset links
# ---------------------------------------------------------------------
def _members(client, company):
    return client.get("/admin/users", headers=headers(company)).json()["members"]


def test_an_admin_cannot_reset_someone_who_also_belongs_to_a_company_they_do_not_administer(client, ellen_client):
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user("shared@test.local", TEST_PASSWORD, "Sam Shared", user_id="u-shared")
    membership_service.create_membership("u-shared", "vdb", ["product_manager"], created_by="u-hendro")
    membership_service.create_membership("u-shared", "bwm", ["product_manager"], created_by="u-hendro")
    mid = next(m["membershipId"] for m in _members(client, "vdb") if m["email"] == "shared@test.local")

    # Ellen is Admin of vdb only; Sam also belongs to bwm.
    r = ellen_client.post(f"/admin/users/{mid}/password-reset-link", headers=headers("vdb"))
    assert r.status_code == 403
    # Hendro is Admin of both.
    r = client.post(f"/admin/users/{mid}/password-reset-link", headers=headers("vdb"))
    assert r.status_code == 200 and "resetToken=" in r.json()["previewUrl"]


def test_only_an_admin_can_issue_a_reset_link(client, viewer_client):
    mid = next(m["membershipId"] for m in _members(client, "vdb") if m["email"] == "ellen@test.local")
    assert viewer_client.post(f"/admin/users/{mid}/password-reset-link", headers=headers("vdb")).status_code == 403
