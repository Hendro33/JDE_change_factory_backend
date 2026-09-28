"""
Jade starts empty: no demo customers, requests, business domains or agent
pack assignments are created by the product. The setup account (bootstrap
Admin) gets one ordinary first customer to start from.
"""

from __future__ import annotations


def _start(monkeypatch, **env):
    from fastapi.testclient import TestClient

    from jde_api_service.config import settings
    from jde_api_service.main import app

    for key, value in env.items():
        monkeypatch.setattr(settings, key, value)
    with TestClient(app):
        pass


def test_a_fresh_installation_has_no_customers_or_data(isolated_dirs, monkeypatch):
    from jde_api_service.persistence.db import connection

    _start(monkeypatch, bootstrap_admin_email="", bootstrap_admin_password="")
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM agent_pack_assignments").fetchone()[0] == 0


def test_the_setup_account_gets_one_ordinary_first_customer(isolated_dirs, monkeypatch):
    from jde_api_service.persistence.db import connection
    from jde_api_service.services import membership_service

    _start(monkeypatch, bootstrap_admin_email="setup@test.local", bootstrap_admin_password="setup-password-1234",
           bootstrap_customer_name="Acme Manufacturing")
    with connection() as conn:
        rows = conn.execute("SELECT id, name, is_demo, archived_at FROM companies").fetchall()
        user = conn.execute("SELECT id FROM users WHERE email = 'setup@test.local'").fetchone()
    assert len(rows) == 1 and rows[0]["name"] == "Acme Manufacturing"
    assert not rows[0]["is_demo"] and rows[0]["archived_at"] is None
    assert "admin" in membership_service.roles_for(user["id"], rows[0]["id"])
    # A restart never creates a second one.
    _start(monkeypatch, bootstrap_admin_email="setup@test.local", bootstrap_admin_password="setup-password-1234")
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1


def test_existing_demo_customers_are_archived_not_deleted(isolated_dirs, monkeypatch, tmp_path):
    """An installation from before this release: its demo customers (and
    their data) stay in the database, but no one can open them."""
    import os

    from jde_api_service.config import settings
    from jde_api_service.persistence import db

    if os.environ.get("JDE_DATABASE_URL"):
        monkeypatch.setenv("JDE_DATABASE_SCHEMA", os.environ["JDE_DATABASE_SCHEMA"] + "_old")
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "older_installation"))
    monkeypatch.setenv("JDE_AUTH_DB_PATH", str(tmp_path / "older_installation" / "jde.sqlite3"))
    from jde_api_service.persistence.migrations import MIGRATIONS
    from jde_api_service.services import auth_service, membership_service
    from jde_api_service.services.customer_service import get_registry

    conn = db.get_connection()
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, sql in MIGRATIONS:
        if version >= 14:
            break
        conn.executescript(db._postgres_ddl(sql) if db.engine() == "postgres" else sql)
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, 'x')", (version,))
    conn.execute("INSERT INTO companies (id, name, short_name, tools_release, environment, created_at, is_demo) "
                 "VALUES ('vdb', 'Van den Berg', 'VdB', '', '', 'x', 1)")
    conn.commit()
    conn.close()
    db.ensure_schema()
    with db.connection() as c:
        assert c.execute("SELECT archived_at FROM companies WHERE id = 'vdb'").fetchone()[0] is not None
    assert get_registry().get_customer("vdb") is None
    user = auth_service.create_user("old@test.local", "an-old-password-1", "Old")
    membership_service.create_membership(user.id, "vdb", ["admin"], created_by=user.id)
    assert membership_service.roles_for(user.id, "vdb") == frozenset()
    assert membership_service.companies_for_user(user.id) == []
