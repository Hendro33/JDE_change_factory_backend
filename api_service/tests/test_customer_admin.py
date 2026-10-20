"""Administrator tools for test customers: clear a customer's work while keeping
its settings, copy a working set-up to another customer, read the JDE estate."""

from __future__ import annotations

from .conftest import TEST_PASSWORD, _apply_csrf_header, headers


def test_clear_work_removes_the_sample_history_and_keeps_the_settings(isolated_dirs):
    from fastapi.testclient import TestClient

    from jde_api_service.main import app
    from jde_api_service.persistence.db import connection, ensure_schema
    from jde_api_service.sample_data import hoogwegt
    from jde_api_service.services import auth_service
    from jde_mcp_server import docstore

    ensure_schema()
    auth_service.create_user("admin@test.local", TEST_PASSWORD, "Admin", user_id="u-admin")
    hoogwegt.load("admin@test.local")
    docstore.put("engagement_scope", "hoogwegt", {"customer_id": "hoogwegt", "revision": 3, "tools_release": "9.2"})

    with TestClient(app) as client:
        assert client.post("/auth/login", json={"email": "admin@test.local", "password": TEST_PASSWORD}).status_code == 200
        _apply_csrf_header(client)
        h = {"X-Customer-Id": "hoogwegt"}
        assert client.get("/changes", headers=h).json()
        wrong = client.post("/admin/customer/clear-work", headers=h, json={"confirmName": "nope"})
        assert wrong.status_code == 422 and client.get("/changes", headers=h).json()

        r = client.post("/admin/customer/clear-work", headers=h,
                        json={"confirmName": "Hoogwegt Arnhem", "removeSampleData": True})
        assert r.status_code == 200, r.text
        out = r.json()
        assert "CR-HW-013" in out["stories"] and out["sample_people_deactivated"]
        assert client.get("/changes", headers=h).json() == []
        assert client.get("/business-domains", headers=h).json() == []
        assert docstore.get("engagement_scope", "hoogwegt")["revision"] == 3
        with connection() as conn:
            left = conn.execute("SELECT kind, doc_id FROM documents WHERE doc_id LIKE 'CR-HW-%'").fetchall()
            assert [tuple(x) for x in left] == []
            active = conn.execute("SELECT u.email FROM company_memberships m JOIN users u ON u.id = m.user_id "
                                  "WHERE m.company_id = 'hoogwegt' AND m.status = 'active'").fetchall()
            assert [a["email"] for a in active] == ["admin@test.local"]


def test_copy_setup_gives_a_new_customer_the_connection_to_test_again(client):
    from jde_api_service.discovery import profile_service
    from jde_mcp_server import docstore

    from ._discovery import ready_company
    from .test_stage1_execution_safeguards import _full_scope, _save_scope

    ready_company(client, "vdb")
    _save_scope(client, "vdb", _full_scope())
    source = profile_service.load("vdb")
    assert profile_service.is_active(source)

    r = client.post("/admin/customer/copy-setup", headers=headers("bwm"), json={"sourceCustomerId": "vdb"})
    assert r.status_code == 200, r.text
    copied = profile_service.load("bwm")
    assert copied is not None and copied["config"] == source["config"]
    assert profile_service.credential_storage(copied) == "encrypted"
    assert not profile_service.is_active(copied)  # tested again in its new customer first
    assert docstore.get("engagement_scope", "bwm")["customer_id"] == "bwm"

    same = client.post("/admin/customer/copy-setup", headers=headers("bwm"), json={"sourceCustomerId": "bwm"})
    assert same.status_code == 422


def test_copy_setup_needs_admin_in_both_customers(client, ellen_client):
    r = ellen_client.post("/admin/customer/copy-setup", headers=headers("vdb"), json={"sourceCustomerId": "bwm"})
    assert r.status_code == 403


def test_read_estate_saves_a_reference_document(client):
    from ._discovery import ready_company

    ready_company(client, "vdb")
    r = client.post("/admin/jde/estate", headers=headers("vdb"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["reads"]) == 12 and out["markdown"].startswith("# JD Edwards estate")
    assert any(x["rows"] for x in out["reads"]), out["reads"]
    docs = client.get("/admin/jde/artifacts", headers=headers("vdb")).json()
    assert any(d["artifactId"] == out["artifact_id"] and d["kind"] == "reference_document" for d in docs)


def test_read_estate_needs_a_connection(client):
    r = client.post("/admin/jde/estate", headers=headers("nhd"))
    assert r.status_code == 409
