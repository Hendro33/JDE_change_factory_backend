"""
Finishing first-time setup: the temporary setup account creates the owner's
own administrator account and is switched off in the same step.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import TEST_PASSWORD

OWNER_PW = "owner-chosen-Passw0rd"


def _as_setup_account(monkeypatch):
    from jde_api_service.config import settings

    monkeypatch.setattr(settings, "bootstrap_admin_email", "hendro@test.local")


def test_only_the_setup_account_is_offered_the_handover(client, ellen_client, monkeypatch):
    _as_setup_account(monkeypatch)
    assert client.get("/auth/setup-handover").json()["isSetupAccount"] is True
    assert ellen_client.get("/auth/setup-handover").json()["isSetupAccount"] is False
    r = ellen_client.post("/auth/setup-handover", json={"email": "x@example.com", "password": OWNER_PW})
    assert r.status_code == 422 and "setup account" in r.json()["detail"]


def test_weak_or_taken_details_are_refused(client, monkeypatch):
    _as_setup_account(monkeypatch)
    assert client.post("/auth/setup-handover", json={"email": "owner@example.com", "password": "short"}).status_code == 422
    r = client.post("/auth/setup-handover", json={"email": "ellen@test.local", "password": OWNER_PW})
    assert r.status_code == 422 and "already exists" in r.json()["detail"]
    assert client.get("/auth/me").status_code == 200  # nothing changed


def test_handover_creates_the_owner_admin_and_disables_the_setup_account(client, monkeypatch):
    from jde_api_service.main import app

    _as_setup_account(monkeypatch)
    r = client.post("/auth/setup-handover",
                    json={"email": "h.owner@example.com", "displayName": "Owner", "password": OWNER_PW})
    assert r.status_code == 200 and r.json()["customers"] == 4 and OWNER_PW not in r.text
    # The setup account's session is gone and it can no longer sign in.
    assert client.get("/auth/me").status_code == 401
    with TestClient(app) as other:
        assert other.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD}).status_code == 401
        # The owner signs in with their own password and is Admin of every customer.
        assert other.post("/auth/login", json={"email": "h.owner@example.com", "password": OWNER_PW}).status_code == 200
        session = other.get("/session").json()
        assert len(session["customers"]) == 4 and all("admin" in c["roles"] for c in session["customers"])
    # Bootstrap never re-creates or re-enables it.
    from jde_api_service.services import auth_service, bootstrap_service

    monkeypatch.setattr("jde_api_service.config.settings.bootstrap_admin_password", "anything-at-all-123")
    bootstrap_service.ensure_bootstrap_admin()
    assert auth_service.get_user_by_email("hendro@test.local").is_active is False
