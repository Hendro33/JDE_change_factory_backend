from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MCP_SERVER_SRC = _HERE.parent.parent / "mcp_server"
if str(_MCP_SERVER_SRC) not in sys.path:
    sys.path.insert(0, str(_MCP_SERVER_SRC))

# Never a real credential -- fixed, used only against an isolated,
# throwaway SQLite file per test (isolated_dirs below).
TEST_PASSWORD = "test-password-not-real-0000"  # noqa: S105 -- test fixture, not a real secret


@pytest.fixture()
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets its own throwaway directories for both this
    service's own data (change requests, customer links, the SQLite
    database) and mcp_server's stores (backlog, changes, evidence) --
    no test can see another test's or a developer's real local data."""
    import dataclasses

    from jde_api_service.config import settings as api_settings
    from jde_mcp_server import backlog as backlog_module
    from jde_mcp_server import approval as approval_module
    from jde_mcp_server import config as mcp_config_module

    api_data_dir = tmp_path / "api_data"
    backlog_dir = tmp_path / "backlog"
    change_dir = tmp_path / "changes"
    evidence_dir = tmp_path / "evidence"

    monkeypatch.setattr(api_settings, "data_dir", str(api_data_dir))
    monkeypatch.setattr(backlog_module, "BACKLOG_DIR", str(backlog_dir))
    monkeypatch.setattr(approval_module, "CHANGE_DIR", str(change_dir))
    # The one shared simulated DEV estate: per test, never shared between runs.
    monkeypatch.setenv("JDE_SIM_ESTATE_DIR", str(tmp_path / "sim_estate"))
    # The execution gate reads each company's saved scope and the
    # story -> company links from this service's own data directory
    # (main._wire_execution_gate does the same at startup).
    from jde_mcp_server import scope as scope_module

    for env_name, attr, sub in (
        ("JDE_COMPANY_SCOPE_DIR", "COMPANY_SCOPE_DIR", "engagement_scope"),
        ("JDE_STORY_COMPANY_DIR", "STORY_COMPANY_DIR", "customer_links"),
    ):
        monkeypatch.setenv(env_name, str(api_data_dir / sub))
        monkeypatch.setattr(scope_module, attr, str(api_data_dir / sub))
    # The gate re-reads the approver's current roles from this database.
    monkeypatch.setenv("JDE_AUTH_DB_PATH", str(api_data_dir / "jde.sqlite3"))
    monkeypatch.setenv("JDE_WRITE_PAUSE_FILE", str(tmp_path / "WRITE_PAUSED"))
    monkeypatch.setenv("JDE_DESIGN_BASELINE_DIR", str(api_data_dir / "design_baselines"))
    # Discovery: a fresh simulated estate and closed circuit breakers per test;
    # live discovery stays switched off unless a test turns it on.
    from jde_api_service.discovery import transport as discovery_transport

    discovery_transport._breakers.clear()
    monkeypatch.delenv("JDE_DISCOVERY_LIVE_ENABLED", raising=False)
    monkeypatch.delenv("JDE_DISCOVERY_ALLOWED_HOSTS", raising=False)
    # mcp_server's Settings is frozen (by design, and it isn't ours to
    # modify) -- rebind the module-level `settings` name to a fresh
    # instance instead of mutating the existing one. change_service.py
    # reads `mcp_config.settings.evidence_dir` fresh on every call
    # (rather than capturing the settings object at import time), so it
    # sees this rebind.
    monkeypatch.setattr(
        mcp_config_module,
        "settings",
        dataclasses.replace(mcp_config_module.settings, evidence_dir=str(evidence_dir)),
    )
    # A cross-origin cookie policy would refuse the TestClient's
    # same-origin requests -- irrelevant to what these tests verify.
    monkeypatch.setattr(api_settings, "cookie_secure", False)
    # Stored credentials are encrypted with a key from the environment
    # (services/credential_crypto.py); a fresh throwaway key per test.
    from cryptography.fernet import Fernet

    monkeypatch.setenv("JDE_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("JDE_CREDENTIAL_KEY_PREVIOUS", raising=False)

    # Schema migrations normally run via the app's startup lifespan
    # (main.py) -- applied here too so a test that talks to a service
    # directly (never starting the app via the `client` fixture) still
    # gets a real SQLite schema to write into.
    from jde_api_service.persistence.db import ensure_schema

    ensure_schema()

    # bcrypt's cost factor is deliberately slow in production (that's
    # the whole point) -- tests hash the same fixed TEST_PASSWORD many
    # times over, so drop the cost here only. Production code
    # (auth_service.hash_password) is untouched; this only patches the
    # bcrypt module bcrypt.gensalt() is imported from.
    import bcrypt

    _real_gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *a, **kw: _real_gensalt(rounds=4))

    return {
        "api_data_dir": api_data_dir,
        "backlog_dir": backlog_dir,
        "change_dir": change_dir,
        "evidence_dir": evidence_dir,
    }


def _apply_csrf_header(c) -> None:
    """login()/accept-invitation set a CSRF cookie alongside the session
    cookie (double-submit pattern -- see dependencies.verify_csrf_if_unsafe).
    A real browser's JS reads that cookie and echoes it as X-CSRF-Token
    on every mutating request; TestClient doesn't do that automatically,
    so every fixture below does it once after login, exactly like the
    frontend's own httpApi.ts does."""
    from jde_api_service.services import auth_service

    token = c.cookies.get(auth_service.CSRF_COOKIE_NAME)
    assert token, "login() must set the CSRF cookie"
    c.headers["X-CSRF-Token"] = token


def _create_member(user_id: str, email: str, display_name: str, company_ids: list[str]) -> None:
    """Creates a real user with every role on each given company --
    the same broad access the old static "u-hendro"/"u-ellen" demo
    identities implicitly had, now backed by real rows in the
    company_memberships/membership_roles tables (see
    services/membership_service.py) instead of a JSON entitlement list."""
    from jde_api_service.models.auth import ALL_ROLES
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user(email, TEST_PASSWORD, display_name, user_id=user_id)
    for company_id in company_ids:
        membership_service.create_membership(user_id, company_id, ALL_ROLES, created_by=user_id)


@pytest.fixture()
def client(isolated_dirs):
    """Logged in as Hendro (all roles, on every seeded company) --
    the default identity almost every test uses, matching the old
    "u-hendro"/"ConsultIQ Consultant" demo persona's broad access.

    Also creates Ellen (all roles, "vdb" only -- the old
    "u-ellen"/"Application Manager" demo persona) up front, unconditionally,
    same as the old static seed_identities.json always had both
    personas available regardless of which one a given test actually
    used -- a handful of tests assert on Ellen's existence/entitlement
    without themselves logging in as her (see e.g.
    test_admin_api.py::test_customer_profile_is_customer_scoped).
    Tests that need to act AS Ellen use the ellen_client fixture below,
    which logs in as this same already-created user."""
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    # `with` triggers FastAPI's startup lifecycle (schema migration,
    # company seeding, pilot-dataset seeding included) the same way a
    # real `uvicorn` run does -- without it, tests would see different
    # behaviour than production.
    with TestClient(app) as c:
        _create_member("u-hendro", "hendro@test.local", "Hendro", ["vdb", "nhd", "mrv", "bwm"])
        _create_member("u-ellen", "ellen@test.local", "Ellen Vos", ["vdb"])
        login = c.post("/auth/login", json={"email": "hendro@test.local", "password": TEST_PASSWORD})
        assert login.status_code == 200, login.text
        _apply_csrf_header(c)
        yield c


@pytest.fixture()
def ellen_client(client):
    """A SECOND, narrower session (Ellen, entitled to "vdb" only) for
    the handful of tests that specifically verify cross-customer
    isolation. Shares the same underlying database as `client` (same
    isolated_dirs, via the dependency on the `client` fixture, which
    already created Ellen's user row) but carries its own, separately
    authenticated session cookie."""
    from fastapi.testclient import TestClient
    from jde_api_service.main import app

    c = TestClient(app)  # no `with` -- schema/seeding already ran via the `client` fixture above
    login = c.post("/auth/login", json={"email": "ellen@test.local", "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    _apply_csrf_header(c)
    return c


@pytest.fixture()
def viewer_client(client):
    """A THIRD identity holding ONLY the dashboard_viewer role on "vdb"
    -- for tests verifying that role's read-only restriction
    (require_write_access) and its exclusion from Admin/Domain-Owner/
    Product-Manager-gated actions."""
    from fastapi.testclient import TestClient
    from jde_api_service.main import app
    from jde_api_service.services import auth_service, membership_service

    auth_service.create_user("viewer@test.local", TEST_PASSWORD, "Viv Viewer", user_id="u-viewer")
    membership_service.create_membership("u-viewer", "vdb", ["dashboard_viewer"], created_by="u-hendro")

    c = TestClient(app)
    login = c.post("/auth/login", json={"email": "viewer@test.local", "password": TEST_PASSWORD})
    assert login.status_code == 200, login.text
    _apply_csrf_header(c)
    return c


def headers(customer: str | None = "vdb") -> dict:
    h: dict = {}
    if customer is not None:
        h["X-Customer-Id"] = customer
    return h


def place_in_owned_domain(client, change_id: str, customer: str = "bwm", domain_id: str = "DOM-BWM-ORDER-FULFIL") -> None:
    """What triage does before a Domain Owner can act: the story is placed
    in a business domain (Product Manager/Admin), and the fixture's
    Domain Owners are assigned to that domain (Admin > Users). Without
    both, require_domain_owner_access refuses -- a story with no domain,
    or a Domain Owner without the assignment, has no authority."""
    from jde_api_service.persistence.db import connection

    client.get(f"/changes/{change_id}/domain-review", headers=headers(customer=customer))
    r = client.post(
        f"/changes/{change_id}/domain-review/assign-domain", headers=headers(customer=customer),
        json={"businessDomainId": domain_id, "uncertain": False, "note": ""},
    )
    assert r.status_code == 200, r.text
    with connection() as conn:
        for user_id in ("u-hendro", "u-ellen"):
            row = conn.execute(
                "SELECT id FROM company_memberships WHERE user_id = ? AND company_id = ?", (user_id, customer)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO domain_assignments (membership_id, business_domain_id) VALUES (?, ?)",
                    (row["id"], domain_id),
                )
