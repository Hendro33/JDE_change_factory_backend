"""Register K13: one page asks for the customer's changes several times at
once; the list is derived once, and any write through the API clears it."""

from __future__ import annotations

from .conftest import headers


def test_the_change_list_is_derived_once_and_cleared_by_a_write(client, monkeypatch):
    from jde_api_service.services import change_service

    monkeypatch.setenv("JDE_LIST_CACHE_SECONDS", "60")
    calls = {"n": 0}
    real = change_service.ChangeService._list_for_customer

    def counting(self, customer_id):
        calls["n"] += 1
        return real(self, customer_id)

    monkeypatch.setattr(change_service.ChangeService, "_list_for_customer", counting)
    change_service.bump_data_version()
    for path in ("/changes", "/work", "/figures"):
        assert client.get(path, headers=headers("vdb")).status_code == 200
    assert calls["n"] == 1
    r = client.post("/change-requests", headers=headers("vdb"),
                    json={"title": "Cache check", "businessSource": "Business", "rawContent": "x"})
    assert r.status_code in (200, 201), r.text
    ids = [c["id"] for c in client.get("/changes", headers=headers("vdb")).json()]
    assert r.json()["id"] in ids and calls["n"] == 2
